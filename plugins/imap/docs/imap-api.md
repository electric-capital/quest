# IMAP Mail API

## Overview

Read-only email access to any IMAP server the administrator allows, packaged as the in-tree `plugins/imap` plugin (plugin id `imap`; see [Plugins](../../../docs/architecture/plugins.md)). It is the first `form`-kind, multi-connection plugin: a user adds up to ten mailboxes (`CONNECTION_LIMIT` in `plugins/imap/manifest.py`), each its own `user_service_credentials` row identified by an account address, and every tool picks one with an optional `connection` argument. The generic machinery (connection rows, default row, `resolve_connection`, the `/auth/connections/{service}` routes, the "Connected accounts" prompt paragraph, the Data Connections cards) is described in [User Connections](../../../docs/architecture/user-connections.md); this doc covers what the plugin adds. Setup: [imap-setup.md](imap-setup.md).

Three properties shape the plugin:

1. **The admin list is the egress boundary.** Users never type a host. The admin card lists the allowed server configurations; the user's form offers them as a select, and every connect re-resolves the row's `server` against the CURRENT list, so removing a line cuts off its connections on the next tool call.
2. **TLS only.** Implicit TLS or STARTTLS, certificate and hostname verified, TLS 1.2 floor; no plaintext mode, no verification opt-out.
3. **Read-only.** Folders are opened with `EXAMINE`, every message read is `BODY.PEEK`, and FETCH item lists that could set `\Seen` are refused. There are no action requests and no write tools (no send, draft, move, delete, flag).

## Key Files

| File | Description |
|------|-------------|
| `plugins/imap/manifest.py` | Manifest: the "IMAP servers" credential field, the `form` user connection (`server` select / `username` / `password`, `field_options`, `verify`, `connected`, `row_warning` hooks, `CONNECTION_LIMIT`), the `system:imap` skill with `imap_skill_content()` substituting the user's accounts, tools, script allowlist |
| `plugins/imap/settings.py` | Stdlib-only admin list parsing (`parse_server_line`, `parse_allowed_servers`, `ServerConfig.line()` canonical form), `validate_imap_admin` (card `credential_validate`), `imap_is_configured`, fresh `load_allowed_servers()`, `resolve_server()`, the per-row hooks (`imap_field_options`, `imap_connected`, `imap_row_warning`, `is_legacy_row`) and `account_for()` |
| `plugins/imap/upstream.py` | Transport: `ImapSettings` from a row, `check_host_allowed`, `run_imap()` (one session per call on a dedicated thread pool, cancellable connect, call timeout), `make_tls_context()`, `QuestIMAP4` / `QuestIMAP4_SSL` transport subclasses (bounded reads, debug pinned off), `_login` (LOGIN or AUTHENTICATE PLAIN), `ImapSession` (`list_folders`, `status`, `examine`, `check_uidvalidity`, `uid_search`, `uid_fetch` + `check_fetch_items`), `ImapError`, and the `verify_imap_connection` hook |
| `plugins/imap/protocol.py` | Stdlib-only response parsing (FETCH, LIST, STATUS, ENVELOPE, BODYSTRUCTURE), modified UTF-7 folder names, safe quoting; nesting bounded by `MAX_NESTING` |
| `plugins/imap/tools.py` | The five tool handlers + specs, argument validation, budgets, attachment save path rules, `SCRIPT_TOOL_NAMES` |
| `plugins/imap/render.py` | Message markdown rendering (`render_message_markdown`, `render_batch_markdown`), attachment enumeration shared by render and save (`list_attachments`, `attachment_payload`), the parse guard `message_too_complex`, URL-cache keys |
| `plugins/imap/instructions.md` | The `system:imap` skill body (LLM-facing; `{{CONNECTIONS_LIST}}` / `{{CONNECTION_EXAMPLE}}` placeholders) |
| `plugins/imap/tests/` | Plugin suite; `fake_imaplib.py` is an in-memory IMAP server swapped in for `IMAP4_CLASS` / `IMAP4_SSL_CLASS` |

## Configuration

### Admin card

Settings > Service Credentials > IMAP mailbox (store file `data/service_credentials/imap.json`, encrypted at rest, written by the generic `PUT /admin/service-credentials/imap`). One required textarea, `allowed_hosts`, labelled "IMAP servers" with a `help` line under the label: one server configuration per line, `host[:port] [ssl|starttls]`.

- Defaults (`parse_server_line`): no port and no mode = `993 ssl`; port `143` without a mode = `starttls`; any other port without a mode = `ssl`; `ssl` alone = port 993; `starttls` alone = port 143. Mode words are case-insensitive.
- Host: a DNS name (lower-cased, trailing dot dropped, non-ASCII must be written in punycode), an IPv4 literal, or a bracketed IPv6 literal (no zone ids). A scheme (`imaps://`), a path, a user name (`@`), an unbracketed IPv6 address, a mode other than `ssl`/`starttls` (there is no plaintext mode), a port outside 1-65535, and more than two words on a line are rejected.
- A host may appear on several lines with different port/mode; two lines naming the same configuration after defaults are applied are rejected (`Line N: duplicate of line M (...)`). Every parse error names its line; an empty list is rejected ("IMAP servers: list at least one server.").
- Saving rewrites every line to the canonical form `host:port mode` (`ServerConfig.line()`, e.g. `imap.example.com:993 ssl`, `[::1]:143 starttls`) via `validate_imap_admin`.
- `is_configured` (`imap_is_configured`) = at least one valid line; until then the connector row is `available: false` and hidden.

### Per-user connection

Settings > Data Connections > "+ Add Connection" > IMAP mailbox shows the `form` fields: `server` (a select whose choices are the canonical admin lines, served through the `field_options` hook `imap_field_options`), `username`, `password` (secret), plus the generic optional description. `POST /auth/connections/imap` validates the values (a `server` not among the current choices fails as "Not one of the allowed choices.") and runs `verify_imap_connection`:

1. `resolve_server()` against the current admin list ("Server not allowed" otherwise).
2. Account = `account_for(username, host)`: the username when it contains `@`, else `username@host`; stripped, lower-cased and checked by `normalize_account` (non-empty, at most 320 characters, no whitespace, no control characters, no `"` or backtick; single quotes are fine), else "This username cannot be used as an account name (...)".
3. A live connect + login + CAPABILITY under `VERIFY_TIMEOUT` (18 s, below the generic route's 20 s `VERIFY_TIMEOUT_SECONDS` so the plugin's own timeout fires first and shuts the socket).
4. The row stores `oauth_blob = {capabilities, verified_at, values: {server, username, password}}`; nothing of it is ever returned to the browser.

Verify attempts are rate-limited by the generic layer (10 per user per rolling 60 s, in memory; 429 `rate_limited` with `Retry-After`). Reconnect in the UI re-enters the password only (blank fields keep their stored values); a re-verify must yield the row's own account (400 `account_mismatch` otherwise). Adding an account that already exists answers 409 `connection_exists`; the eleventh add 400 `connection_limit`.

Hooks: `connected` (`imap_connected`) = the row holds server, username and password; `row_warning` (`imap_row_warning`) = "Server no longer allowed" on a row whose server is not on the current admin list, shown on its Data Connections card. Rows from before the server select (`host`/`port`/`security`, no `server`) are never migrated and fail closed the same way.

## Tools (`plugins/imap/tools.py`)

All five are `tool_call` plugin tools gated on `connected_services["imap"]` (admin list configured AND a connected row). None is `mutating`. Every tool takes the optional string `connection` (an account address; omitted = the default row; resolved by `config.user_connections.resolve_connection`, case-insensitive) and `intent_message`. Folder arguments are the decoded `name` from `imap_list_folders` (or its `raw_name`); `INBOX` is normalised case-insensitively. Text arguments are capped at 500 characters (folders 1000) and must not contain CR/LF/NUL.

| Tool | Arguments | Result |
|------|-----------|--------|
| `imap_list_folders` | -- | JSON `{account, count, folders[], truncated?, note?}`; each folder `{name, delimiter, flags, special_use, messages, unseen, uidvalidity}` from LIST + one `STATUS (MESSAGES UNSEEN UIDVALIDITY)`, plus `raw_name` when the server's modified-UTF-7 spelling is non-canonical, `selectable: false` for `\Noselect` / `\NonExistent`, or a per-folder `status_error` (the listing continues). Capped at `MAX_FOLDERS` 200 |
| `imap_search_messages` | `folder` (default `INBOX`), `from`, `to`, `subject`, `text`, `since` / `before` (`YYYY-MM-DD`, `before` exclusive and later than `since`), `unseen` (bool), `limit` 1-50 (default 20) | Server-side `UID SEARCH`, criteria ANDed (no criteria = `ALL`); ASCII values quoted, a non-ASCII value sent as a UTF-8 literal with `CHARSET UTF-8` (one search per non-ASCII value, UID sets intersected). JSON `{account, folder, uidvalidity, total_matches, returned, messages[], note?}`, rows for the highest `limit` UIDs, newest first: `{uid, date, from, to, subject, size, flags, has_attachments}` from ENVELOPE / BODYSTRUCTURE, text fields cut at 500 characters, flags at 100 characters and 50 per row; an undecodable summary yields `{uid, size, flags, parse_error}` |
| `imap_get_messages` | `folder` (required), `uids` (1-20, list or comma string), `uidvalidity`, `include_html`, `include_urls` | One UID: a markdown document (see Rendering) or a JSON error `{error, message, uid, folder}`. Several: a `# IMAP messages batch` document, `## Batch errors` first (`imap_message_not_found`, `imap_batch_too_large`, `imap_output_limit`, `imap_render_failed`), then `===`-separated messages in request order |
| `imap_get_message_urls` | `folder`, `uid` (required), `uidvalidity`, `url_ids` (up to 200; omitted = every mapping) | No server contact, but the row is still resolved to settings, so a connection whose server was removed answers `imap_host_not_allowed`. JSON `{account, folder, uid, uidvalidity?, urls, errors?}` from the per-conversation URL cache; `url_mapping_not_found` when the message was not rendered with replacement in this conversation, `no_conversation_context` from scripts |
| `imap_save_attachment` | `folder`, `uid` (required), `uidvalidity`, exactly one of `attachment_index` (1-based) / `filename` (exact, case-insensitive), `save_as` (a name only; `filename` and `save_as` up to 1000 characters) | Writes into the conversation workspace (see Attachments). JSON `{status, path, filename, size_bytes, content_type, account, folder, uid, uidvalidity, attachment_index, message}` |

**Message identity** is `(folder, uidvalidity, uid)`. The optional integer `uidvalidity` on `imap_get_messages` / `imap_save_attachment` is checked after `EXAMINE` (`ImapSession.check_uidvalidity`): a mismatch fails with `imap_uidvalidity_changed` (`expected` / `current`) instead of addressing another message. On `imap_get_message_urls` it selects the cache entry of that rendering instead.

### `imap_get_messages` budgets

The session only downloads; rendering runs in a thread after the session closes so CPU work cannot eat the call timeout.

- A message whose `RFC822.SIZE` (or actual downloaded size) is above `MAX_RENDER_MESSAGE_BYTES` (25 MB) is never handed to the MIME parser: headers plus an attachment list built from BODYSTRUCTURE (encoded sizes), with a note when the message is too large to save attachments from.
- `MAX_BATCH_BYTES` (60 MB) of raw bytes per call; further UIDs are `imap_batch_too_large` batch errors.
- `MAX_OUTPUT_CHARS` (400,000 characters of markdown) per call; once spent, further messages are `imap_output_limit` batch errors, and a first message that alone exceeds it is cut by `_fit_markdown` (header block kept, attachment list cut to a quarter of the budget, body cut with a note).

### Attachments

`imap_save_attachment` downloads the whole message (`BODY.PEEK[]`), enumerates parts with the same `list_attachments` the renderer uses (so `attachment_index` means the same part), and writes the decoded payload to `imap-attachments/<account>/<name>` in the calling conversation's own workspace (also in project conversations; never the project workspace). It then publishes `file_list_changed` for the conversation scope.

- Limits: `MAX_ATTACHMENT_BYTES` 50 MB decoded; messages above `MAX_SAVE_MESSAGE_BYTES` (50 MB x 1.4 + 1 MB, about 71 MB) are refused before download and again on the actual size (`imap_attachment_too_large`, `size`); the message must pass `message_too_complex` (`imap_message_too_complex`).
- Names: `safe_filename` (path separators and control characters replaced with `_`, leading dots removed, at most 200 UTF-8 bytes with a short extension kept). An attached `message/*` without a filename is listed and saved as `<subject>.eml` (`forwarded-message.eml` without a subject, render.py `_attached_message_name`); any other unnamed part defaults to `attachment-<index><ext>` (`.eml` for a `message/rfc822` that is not a container). `account_dir_name` appends an 8-hex SHA-256 suffix when cleaning changed the account. Files are created `O_EXCL | O_NOFOLLOW` with a `-2`, `-3`, ... suffix on collision, and a symlink in the destination path is refused (`invalid_destination`); filesystem errors are `save_failed`.
- Not-found answers `imap_attachment_not_found` with an `available` list (up to 200 entries: `attachment_index`, `filename`, `mime`, `approx_size`); an ambiguous `filename` asks for `attachment_index`.
- Requires a conversation (`no_conversation_context` otherwise), which is why the tool is not in the script bridge.

## Rendering (`plugins/imap/render.py`)

Same LLM-facing shape as the Gmail Simple and Microsoft 365 renderers: a header block (Subject, From, To, Cc / Bcc / Reply-To when present, Date, Message-ID, Account, Folder, UID, UIDVALIDITY, Flags, `Replaced URL count` when URLs were replaced), `---`, `## Body`, `---`, `## Attachments` (`- **name** -- `mime`, ~size bytes[, inline], attachment_index: `N``). The module docstring records the decisions; the essentials:

- Only `text/plain` and `text/html` leaves without an `attachment` disposition and without a filename are body candidates. In `multipart/alternative` one alternative is chosen (plain text preferred, `include_html` prefers HTML); other multiparts contribute every candidate in order. Every other leaf (incl. `text/calendar`, `text/csv`, vCards) is an attachment; an attached `message/*` is one attachment saved as `.eml`.
- HTML goes through the shared Gmail pipeline (`api/gmail/helpers.py`), so no raw HTML reaches the model and remote images become inert text; `include_html` shows the raw HTML in a fenced block instead (no URL replacement then).
- URL replacement with `(#N#)` identifiers runs for HTML-derived AND plain-text bodies (Gmail / M365 replace only in HTML), only with a conversation id and `include_urls` false. The mapping lives in the per-conversation URL cache of `api/gmail/helpers.py` under `url_cache_key(account, folder, uidvalidity, uid)` plus a `latest_url_cache_key(account, folder, uid)` pointer rewritten on every replacement-mode render; if caching fails the message is re-rendered with full URLs.
- Caps: each body part and the body as a whole at `MAX_BODY_BYTES` (2 MB decoded, with a truncation note), `MAX_PARTS` 500 / `MAX_DEPTH` 64 in the part walk, header lines at 2,000 characters, listed filenames at 500. Before parsing, `message_too_complex` refuses raw messages with more than `MAX_BOUNDARY_LINES` (2,000) boundary-like lines (the stdlib parser is super-linear in part count) or above a byte limit: 25 MB on the render path (unreachable in practice, since `imap_get_messages` sends messages above 25 MB to `_huge_message_markdown`, which lists attachments from BODYSTRUCTURE) and `MAX_SAVE_MESSAGE_BYTES` on the save path. A message over the boundary limit renders headers only, and its attachments can be neither listed nor saved (`imap_message_too_complex`).
- Header values, flags, filenames and MIME types are made single-line (controls, line separators and bidi controls become spaces); hostile MIME never raises.

## Transport and Security (`plugins/imap/upstream.py`)

- **Allow-list re-check.** `settings_from_row` resolves the row's `server` against the current admin list, and `run_imap` re-checks `(host, port, security)` (`check_host_allowed`) right before connecting; a miss is `imap_host_not_allowed` (with the stored `server` when there is one). A row cannot reach a host that is not on the list, and the user never supplies a host.
- **TLS.** `make_tls_context()` = `ssl.create_default_context()` (system CAs, verification, hostname check) with `minimum_version` TLS 1.2. `ssl` lines use implicit TLS; `starttls` lines refuse a server that does not advertise STARTTLS ("plaintext IMAP is not supported").
- **Login.** `LOGIN` with the user name quoted; `AUTHENTICATE PLAIN` when either credential is non-ASCII or the server advertises `LOGINDISABLED` (refused when `AUTH=PLAIN` is missing). CR/LF/NUL in credentials are refused. Server/OS error messages are cut at 500 characters and a password of 3 or more characters is replaced with `***` in them; the password is excluded from `ImapSettings` repr, imaplib's command log is cleared after login, and imaplib debug output is pinned off.
- **Timeouts and bounds.** `SOCKET_TIMEOUT` 20 s per socket operation, `CALL_TIMEOUT` 60 s per tool call (on expiry the sockets are shut from the event loop side so the worker unblocks; connect and both TLS handshakes are registered and cancellable). Sessions run on a dedicated pool of `MAX_WORKERS` 8 threads. Server literals above `MAX_LITERAL_BYTES` (80 MB) and response lines above `MAX_LINE_BYTES` (32 MB) are refused (`imap_protocol_error`).
- **Read-only.** `EXAMINE` (`select(readonly=True)`) only; `check_fetch_items` rejects `BODY[` / `BINARY[` without `.PEEK`, bare `RFC822` and `RFC822.TEXT`. One session per call: connect, login, work, logout.
- **Verify rate limit.** Generic, per user, see Configuration; it keeps the form from being used to brute-force an upstream login through Quest.

## Errors

Tool results are JSON `{"error": <code>, "message", ...}`.

| Code | Source |
|------|--------|
| `invalid_arguments` | Argument validation (also an unencodable folder name) |
| `connection_not_found` (`requested`, `available`) | `resolve_connection`: no row with that account |
| `imap_not_connected` | No connected IMAP row |
| `imap_host_not_allowed` | Row's server not on the current admin list (incl. legacy rows) |
| `imap_not_configured` | Row lacks username or password (defensive: tools answer `imap_not_connected` first; reachable only from verify-time settings) |
| `imap_auth_failed` | Login rejected, or no usable auth mechanism |
| `imap_connect_failed` | TCP / TLS / STARTTLS failure, connection lost |
| `imap_timeout` | Socket or call timeout |
| `imap_protocol_error` | Unparseable or oversized server response, failed command |
| `imap_folder_not_found` (`folder`) | EXAMINE / STATUS refused |
| `imap_uidvalidity_changed` (`folder`, `expected`, `current`) | `uidvalidity` mismatch |
| `imap_message_not_found` | No such UID (single fetch / save) |
| `imap_attachment_not_found` (`available`), `imap_attachment_too_large` (`size`), `imap_message_too_complex` | `imap_save_attachment` |
| `no_conversation_context`, `url_mapping_not_found` | URL lookup / save without a conversation; uncached message |
| `invalid_destination`, `save_failed` | Workspace write |

Batch-only codes in `imap_get_messages`: `imap_message_not_found`, `imap_batch_too_large`, `imap_output_limit`, `imap_render_failed`.

## Availability

- **Skill.** `system:imap` (`plugins/imap/instructions.md`), gated on `imap`: tools, search semantics, UID / UIDVALIDITY, output format and limits, attachment rules, an untrusted-email-content warning, error table, script usage. `imap_skill_content()` fills in the user's own account list (default marked) and a `connection` example naming a non-default account. With two or more connections the generic "Connected accounts" prompt paragraph also lists them with their descriptions.
- **Script bridge** (`POST /api/tool-call`, `SCRIPT_TOOL_NAMES`): `imap_list_folders`, `imap_search_messages`, `imap_get_messages`, `imap_get_message_urls`, with `connection` in `arguments`. Script calls carry no conversation context, so bodies keep full URLs and the URL lookup answers `no_conversation_context`. `imap_save_attachment` is tool-only.
- **Inference API runs:** available (no tool is `mutating`).
- **Public projects:** unavailable; the `imap_*` tools are not in `PUBLIC_TOOL_CALL_ALLOWLIST` (the loader in `config/plugins.py` forbids plugin tools there), so public-project dispatch rejects them.
- No `authed_get` service entry: the host is admin-configured at runtime and IMAP is not HTTP.

## Design Decisions

**Why does the admin list servers instead of users typing a host?** A user-supplied host would let any user make the Quest server open TCP connections to arbitrary internal addresses and offer it credentials. The admin list is the only egress boundary, and re-resolving `server` on every connect means removing a line revokes access immediately without touching user rows.

**Why the `form` kind?** IMAP needs three values (server, username, password) that must be checked together by a live login before anything is stored; `api_key` holds one secret and `oauth` would need plugin-served pages. The `form` kind with a `verify` hook covers it with no plugin routes or frontend code.

**Why one session per tool call?** No pooled connections to keep alive, re-authenticate or invalidate when the admin list or the password changes; each call re-checks the allow-list and starts clean. The cost is one TLS handshake + login per call.

**Why download first, render later?** MIME parsing and HTML conversion are CPU-heavy on hostile input; doing them after logout keeps them out of the 60 s call timeout and frees the connection early. The byte budget (60 MB) bounds memory, the character budget (400,000) bounds model context.

**Known limitation (generic layer).** A `PUT /auth/connections/imap/{id}` that changes `server` while leaving the password blank keeps the stored password and verifies it against the new (admin-allowed) server. The Settings UI never sends such a request (Reconnect submits only the password), but the API accepts it.
