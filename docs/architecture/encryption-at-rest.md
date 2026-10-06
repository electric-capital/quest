# Encryption at Rest

## Overview

Every secret Quest persists is encrypted before it reaches disk: the per-user
OAuth token blobs and API keys in the SQLite database, and the admin-managed
per-service / per-provider credential files. One random 256-bit
data-encryption key (DEK) protects everything; the DEK itself is stored only
in password-wrapped form in `<data_dir>/encryption_key.json`. The password is
supplied by the operator at startup (environment variable, password file, or
TTY prompt); local mode uses a fixed, public sentinel password because its
data directory is a throwaway.

This protects a copied database file, credential directory, or backup. It
does **not** protect against an attacker on the running host, who can read
the DEK from process memory or the password from the process environment.

## Key Files

- `config/encryption.py` -- the whole mechanism: password resolution
  (`resolve_password`), key file create/unwrap/rotate (`create_key_file`,
  `unwrap_key_file`, `rotate_password`), the process-wide loaded key
  (`load_data_key`, `get_data_key`), the value envelope (`encrypt_str` /
  `decrypt_str` / `encrypt_json` / `decrypt_json`, `is_encrypted`), the
  `hash_api_key` lookup digest, and the `python -m config.encryption
  init|check|rotate-password` CLI. Top-level imports are stdlib-only so
  `run.py` can import it; `cryptography` is imported lazily.
- `db/encrypted_types.py` -- `EncryptedText` / `EncryptedJSON` SQLAlchemy
  `TypeDecorator`s that encrypt on bind and decrypt on load, keeping the
  columns' declared SQL types so autogenerate sees no change; tolerate
  legacy plaintext on read with a one-time warning.
- `db/models.py` -- the encrypted columns: `users.api_key`,
  `users.google_oauth`, `users.google_services_oauth`,
  `users.airtable_token`, `users.ramp_oauth`,
  `user_service_credentials.secret`, `user_service_credentials.oauth_blob`
  (the Telegram session lives in an `oauth_blob` row since migration
  `d7a1f3c9e2b4`, which re-encrypted it from the dropped
  `users.telegram_session` column -- the per-column label means a
  ciphertext cannot simply be copied between columns);
  plus the plaintext `users.api_key_hash` lookup column maintained by the
  `@validates("api_key")` hook.
- `db/user_store.py` -- `get_user_by_api_key` queries `api_key_hash`;
  `get_user_by_slack_user_id` decrypts the Slack rows in Python instead of
  `json_extract` (SQL cannot see inside the ciphertext).
- `config/service_credentials.py`, `config/inference_providers.py` -- store
  files are written as `{"encrypted": "qenc1:..."}`; reads accept plaintext
  JSON too; `encrypt_plaintext_credential_files()` /
  `encrypt_plaintext_inference_files()` convert leftovers in place.
- `alembic/versions/c4e8a2d17f63_encrypt_secrets_at_rest.py` -- the
  one-shot in-place migration (see below).
- `run.py` -- `resolve_encryption_password()` + `unlock_encryption_key()`,
  step 3 of startup (before migrations).
- `quest.py` (`lifespan`) -- unlocks the key at boot (fail-fast) and runs
  the credential-file sweep, covering manual `uvicorn` startups.
- `conftest.py` (repo root) -- session-wide throwaway key file + test
  password for the test suite.

## Cryptography

- **Values**: AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`),
  fresh random 96-bit nonce per encryption. Envelope:
  `qenc1:<key_id>:<base64url(nonce || ciphertext || tag)>`. Anything not
  starting with `qenc1:` is plaintext.
- **Associated data**: each column / file passes a fixed label
  (`"users.google_oauth"`, `"service_credentials/ramp"`, ...) bound into the
  GCM tag, so a ciphertext copied into another column or file fails
  authentication. Row identity is not bound (the primary key is unknown at
  insert time).
- **Key wrapping**: the DEK is wrapped with AES-256-GCM under a key derived
  from the password by **Argon2id** (64 MiB, 3 passes, 4 lanes; parameters
  and salt stored in the key file so they can be raised later). A wrong
  password fails the wrap's tag check, so it is detected at startup rather
  than on the first row read.
- **`key_id`**: a random identifier minted with the key file and stamped
  into every envelope; a value from a different key file raises
  `KeyMismatchError` with a clear message instead of a bare tag failure.
- **`users.api_key`** cannot be hash-only (it is returned in plaintext by
  `GET /me` and rendered into the model-facing proxy preamble), so it is
  encrypted and looked up through the SHA-256 `api_key_hash` column.
  Sandbox containers no longer receive it: their `QUEST_API_KEY` is an
  in-memory per-run token (`chat/sandbox_tokens.py`) that is never stored.

## Password Resolution Flow

Startup (`run.py`):

1. `resolve_encryption_password()` runs right after the prod bootstrap
   wizard / local pre-baking, before the build steps -> `config/encryption.py`
   (`resolve_password`): `QUEST_ENCRYPTION_PASSWORD`, then the file named by
   `QUEST_ENCRYPTION_PASSWORD_FILE`, then (local mode only)
   `LOCAL_DEV_PASSWORD`, then a hidden TTY prompt (asked twice when the key
   file does not exist yet). No password on a non-TTY staging/prod start
   aborts with instructions.
2. Step 3 exports the password as `QUEST_ENCRYPTION_PASSWORD` for the
   subprocesses and runs `uv run python -m config.encryption init`, which
   creates the key file on first run (or verifies the password unlocks it)
   and encrypts any plaintext credential store files.
3. `alembic upgrade head`, the local seeder, and `uvicorn` inherit the
   password; each process unwraps the DEK once and caches it.

The local sentinel is refused outside local mode both when creating a key
file and when unlocking one (`_check_password_allowed`), so a local data dir
copied into a real deployment cannot leave production secrets behind a
password that lives in the repo. Local mode still honors an explicit
password from the environment.

## Migration

`c4e8a2d17f63` encrypts every existing row **in place** in a single
revision -- deployments may skip releases, so there is no intermediate
plaintext-fallback release. Safety comes from three things instead:

1. A `quest.db.pre-encryption-<timestamp>.bak` copy (sqlite3 online backup
   API) is written before any row changes when the tables are non-empty.
   Rollback without the password is "stop, move the file back, run the
   previous release".
2. The migration is idempotent: values already in the envelope are skipped,
   so an interrupted run is simply re-run.
3. The ORM types return legacy plaintext as-is, covering rows a still-running
   pre-upgrade process wrote after the migration.

The only schema change is the new `users.api_key_hash` column + unique
index. `downgrade()` decrypts everything back (needs the password) and drops
the column with native `DROP COLUMN` (see the batch-mode caveat in
`f3a9c5d81b42`).

## Constraints

- **Not encrypted**: `<data_dir>/secret_key` (cookie-signing key -- not an
  upstream credential), `<data_dir>/vertex-service-account.json` (read
  directly by the Google SDK via `GOOGLE_APPLICATION_CREDENTIALS`), the
  legacy `server_credentials.json` / `twitter_credentials.json` project-root
  files (never modified; migrated copies in the store are encrypted),
  `users.settings` (no secrets), and Quest Docs (`<data_dir>/docs/<id>/`
  bodies, assets and revisions plus the `docs` / `doc_shares` rows: user
  content, not secrets, so they stay plaintext like conversation workspaces
  and chat histories -- see [Quest Docs](quest-docs.md)).
  `inference_api_keys` were already hashed.
- **Password loss = data loss** for the encrypted values only; the key file
  is useless without it. Back up `encryption_key.json` with the database.
- **Rotation**: `uv run python -m config.encryption rotate-password` re-wraps
  the DEK (no row rewrites). Rotating the DEK itself is not implemented; the
  `key_id` in every envelope is the hook for doing it later.
- Sandbox containers only receive the explicit `-e` variables built in
  `chat/gemini_api/tool_handlers/sandbox.py`; the password is never passed through.
- SQL cannot filter on encrypted columns: any future lookup into a token blob
  must decrypt in Python like `get_user_by_slack_user_id`.

## Design Decisions

**Why envelope encryption (random DEK wrapped by a password-derived key)?**
Changing the password re-wraps one small file instead of re-encrypting every
row, and the DEK's entropy does not depend on password quality.

**Why AES-GCM + Argon2id via `cryptography`?** Already a transitive
dependency (google-auth, telethon), so no new supply chain; GCM is
authenticated so tampering or a wrong key fails loudly; Argon2id is the
current password-hashing recommendation and ships in the same package.
Fernet was rejected (AES-128-CBC, awkward token format); PyNaCl would add a
dependency.

**Why encrypt in place instead of new columns?** No dead columns, no SQLite
table rebuild (and the FK-cascade hazard of batch mode), pure data migration
that is idempotent by inspection of the `qenc1:` prefix.

**Why keep `users.api_key` encrypted rather than hashed?** `GET /me` and the
proxy preamble still hand out the plaintext, so a hash-only scheme would
force a regenerate-on-view flow. (The sandbox bridge, the original reason,
now mints its own ephemeral tokens.) The hash column gives O(1) lookup
without decrypting rows.

**Why a fixed local-mode password?** Local data dirs are throwaway and
pre-baked from `dev-config.json`; prompting on every `run.py --local` would
be a regression and `--keep-data` must keep working. The sentinel is public
by design and refused in staging/prod.

**Why a JSON wrapper for store files instead of a bare envelope string?**
`scripts/bootstrap_prod.py` and `run.py` are stdlib-only and only test for
the file's presence via a JSON-object read; keeping the file a JSON object
means they need no crypto.
