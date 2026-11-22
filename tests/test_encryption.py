"""Tests for config/encryption.py: key file lifecycle, password resolution,
the value envelope, and the encrypted credential-file stores.

The repo-root conftest gives the whole session a throwaway key file and a
fixed test password; tests that need their own key file point
``QUEST_ENCRYPTION_KEY_FILE`` at tmp_path and reset the cache.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from config import encryption


@pytest.fixture()
def own_key(monkeypatch, tmp_path):
    """Fresh key file under tmp_path with a known password; restores the
    session key afterwards."""
    key_file = tmp_path / "encryption_key.json"
    monkeypatch.setenv("QUEST_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.setenv("QUEST_ENCRYPTION_PASSWORD", "correct horse")
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD_FILE", raising=False)
    encryption.reset_cache()
    yield key_file
    encryption.reset_cache()
    monkeypatch.undo()
    encryption.reset_cache()
    encryption.load_data_key(create=True)


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------

def test_create_key_file_is_private_and_wraps_a_random_key(own_key):
    data = encryption.create_key_file("correct horse", own_key)
    assert own_key.exists()
    assert stat.S_IMODE(os.stat(own_key).st_mode) == 0o600
    on_disk = json.loads(own_key.read_text())
    assert on_disk["version"] == 1
    assert on_disk["kdf"]["name"] == "argon2id"
    assert on_disk["key_id"] == data["key_id"] and len(data["key_id"]) == 8
    dek, key_id = encryption.unwrap_key_file("correct horse", on_disk)
    assert len(dek) == 32 and key_id == data["key_id"]


def test_wrong_password_is_rejected_clearly(own_key):
    data = encryption.create_key_file("correct horse", own_key)
    with pytest.raises(encryption.WrongPasswordError):
        encryption.unwrap_key_file("battery staple", data)


def test_create_refuses_to_overwrite_existing_key_file(own_key):
    encryption.create_key_file("correct horse", own_key)
    with pytest.raises(encryption.EncryptionError, match="Refusing to overwrite"):
        encryption.create_key_file("correct horse", own_key)


def test_load_without_create_requires_existing_file(own_key):
    with pytest.raises(encryption.KeyFileMissingError):
        encryption.load_data_key(create=False)
    dek, key_id = encryption.load_data_key(create=True)
    assert own_key.exists()
    encryption.reset_cache()
    assert encryption.load_data_key(create=False) == (dek, key_id)


def test_rotate_password_keeps_the_data_key(own_key):
    encryption.load_data_key(create=True)
    envelope = encryption.encrypt_str("hello", "t.c")
    encryption.rotate_password("correct horse", "new password", own_key)
    with pytest.raises(encryption.WrongPasswordError):
        encryption.unwrap_key_file("correct horse", encryption.read_key_file(own_key))
    os.environ["QUEST_ENCRYPTION_PASSWORD"] = "new password"
    encryption.reset_cache()
    assert encryption.decrypt_str(envelope, "t.c") == "hello"


# ---------------------------------------------------------------------------
# Password resolution
# ---------------------------------------------------------------------------

def test_resolve_password_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("QUEST_ENV", "prod")
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD", raising=False)
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD_FILE", raising=False)
    # Nothing set, no prompt allowed -> None
    assert encryption.resolve_password() is None
    # Password file
    pw_file = tmp_path / "pw"
    pw_file.write_text("from-file\n")
    monkeypatch.setenv("QUEST_ENCRYPTION_PASSWORD_FILE", str(pw_file))
    assert encryption.resolve_password() == "from-file"
    # Env var wins over the file
    monkeypatch.setenv("QUEST_ENCRYPTION_PASSWORD", "from-env")
    assert encryption.resolve_password() == "from-env"


def test_resolve_password_missing_file_is_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD", raising=False)
    monkeypatch.setenv("QUEST_ENCRYPTION_PASSWORD_FILE", str(tmp_path / "nope"))
    with pytest.raises(encryption.PasswordNotAvailableError):
        encryption.resolve_password()


def test_local_mode_falls_back_to_the_dev_sentinel(monkeypatch):
    monkeypatch.setenv("QUEST_ENV", "local")
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD", raising=False)
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD_FILE", raising=False)
    assert encryption.resolve_password() == encryption.LOCAL_DEV_PASSWORD
    # An explicit password still wins in local mode
    monkeypatch.setenv("QUEST_ENCRYPTION_PASSWORD", "explicit")
    assert encryption.resolve_password() == "explicit"


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_sentinel_password_refused_outside_local_mode(monkeypatch, tmp_path, env):
    monkeypatch.setenv("QUEST_ENV", env)
    key_file = tmp_path / "k.json"
    with pytest.raises(encryption.SentinelPasswordError):
        encryption.create_key_file(encryption.LOCAL_DEV_PASSWORD, key_file)
    # ... and when unlocking a file that was created under it in local mode
    monkeypatch.setenv("QUEST_ENV", "local")
    data = encryption.create_key_file(encryption.LOCAL_DEV_PASSWORD, key_file)
    monkeypatch.setenv("QUEST_ENV", env)
    with pytest.raises(encryption.SentinelPasswordError):
        encryption.unwrap_key_file(encryption.LOCAL_DEV_PASSWORD, data)


def test_sentinel_password_accepted_in_local_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("QUEST_ENV", "local")
    data = encryption.create_key_file(encryption.LOCAL_DEV_PASSWORD, tmp_path / "k.json")
    dek, _ = encryption.unwrap_key_file(encryption.LOCAL_DEV_PASSWORD, data)
    assert len(dek) == 32


# ---------------------------------------------------------------------------
# Value envelope
# ---------------------------------------------------------------------------

def test_encrypt_str_roundtrip_and_format():
    envelope = encryption.encrypt_str("s3cret", "users.api_key")
    assert encryption.is_encrypted(envelope)
    prefix, key_id, payload = envelope.split(":", 2)
    assert prefix + ":" == encryption.ENVELOPE_PREFIX
    assert key_id == encryption.get_data_key()[1]
    assert "s3cret" not in envelope
    assert encryption.decrypt_str(envelope, "users.api_key") == "s3cret"


def test_fresh_nonce_per_call():
    assert encryption.encrypt_str("same", "x") != encryption.encrypt_str("same", "x")


def test_aad_mismatch_is_rejected():
    envelope = encryption.encrypt_str("token", "users.google_oauth")
    with pytest.raises(encryption.EncryptionError, match="failed authentication"):
        encryption.decrypt_str(envelope, "users.ramp_oauth")


def test_tampered_ciphertext_is_rejected():
    envelope = encryption.encrypt_str("token", "c")
    prefix, key_id, payload = envelope.split(":", 2)
    flipped = payload[:-2] + ("A" if payload[-2] != "A" else "B") + payload[-1]
    with pytest.raises(encryption.EncryptionError):
        encryption.decrypt_str(f"{prefix}:{key_id}:{flipped}", "c")


def test_foreign_key_id_is_a_clear_error():
    envelope = encryption.encrypt_str("token", "c")
    prefix, _key_id, payload = envelope.split(":", 2)
    with pytest.raises(encryption.KeyMismatchError):
        encryption.decrypt_str(f"{prefix}:deadbeef:{payload}", "c")


def test_plaintext_is_not_an_envelope():
    assert not encryption.is_encrypted("plain-token")
    assert not encryption.is_encrypted(None)
    assert not encryption.is_encrypted({"a": 1})
    with pytest.raises(encryption.EncryptionError):
        encryption.decrypt_str("plain-token", "c")


def test_encrypt_json_roundtrip():
    doc = {"access_token": "abc", "scopes": ["a", "b"], "n": 1}
    envelope = encryption.encrypt_json(doc, "users.google_oauth")
    assert "abc" not in envelope
    assert encryption.decrypt_json(envelope, "users.google_oauth") == doc


def test_hash_api_key_is_sha256_hex():
    import hashlib
    assert encryption.hash_api_key("k") == hashlib.sha256(b"k").hexdigest()


# ---------------------------------------------------------------------------
# Encrypted credential files
# ---------------------------------------------------------------------------

@pytest.fixture()
def store_dirs(monkeypatch, tmp_path):
    import config.service_credentials as sc
    import config.inference_providers as ip
    svc = tmp_path / "service_credentials"
    inf = tmp_path / "inference_credentials"
    monkeypatch.setattr(sc, "SERVICE_CREDENTIALS_DIR", svc)
    monkeypatch.setattr(ip, "INFERENCE_CREDENTIALS_DIR", inf)
    monkeypatch.setattr(sc, "LEGACY_CREDENTIALS_FILE", tmp_path / "server_credentials.json")
    return sc, ip, svc, inf


def test_service_credentials_written_encrypted_and_read_back(store_dirs):
    sc, _ip, svc, _inf = store_dirs
    creds = {"client_id": "id", "client_secret": "topsecret"}
    sc.write_service_credentials("ramp", creds)
    on_disk = json.loads((svc / "ramp.json").read_text())
    assert set(on_disk) == {"encrypted"}
    assert encryption.is_encrypted(on_disk["encrypted"])
    assert "topsecret" not in (svc / "ramp.json").read_text()
    assert sc.read_service_credentials("ramp") == creds


def test_service_credentials_plaintext_file_still_readable_and_sweep_encrypts(store_dirs):
    sc, _ip, svc, _inf = store_dirs
    svc.mkdir()
    (svc / "ramp.json").write_text(json.dumps({"client_id": "legacy", "client_secret": "s"}))
    assert sc.read_service_credentials("ramp") == {"client_id": "legacy", "client_secret": "s"}
    assert sc.encrypt_plaintext_credential_files() == ["ramp"]
    assert set(json.loads((svc / "ramp.json").read_text())) == {"encrypted"}
    assert sc.read_service_credentials("ramp") == {"client_id": "legacy", "client_secret": "s"}
    # Idempotent
    assert sc.encrypt_plaintext_credential_files() == []


def test_service_credentials_file_bound_to_its_service(store_dirs):
    sc, _ip, svc, _inf = store_dirs
    sc.write_service_credentials("ramp", {"client_id": "id"})
    (svc / "google_oauth.json").write_text((svc / "ramp.json").read_text())
    assert sc.read_service_credentials("google_oauth") is None


def test_inference_credentials_written_encrypted_and_swept(store_dirs):
    _sc, ip, _svc, inf = store_dirs
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or-secret"})
    raw = (inf / "openrouter.json").read_text()
    assert "sk-or-secret" not in raw and set(json.loads(raw)) == {"encrypted"}
    assert ip.read_inference_credentials("openrouter") == {"api_key": "sk-or-secret"}
    assert ip.effective_api_key("openrouter") == ("sk-or-secret", "store")
    # Plaintext pre-baked file
    (inf / "openrouter.json").write_text(json.dumps({"api_key": "plain"}))
    assert ip.effective_api_key("openrouter") == ("plain", "store")
    assert ip.encrypt_plaintext_inference_files() == ["openrouter"]
    assert ip.effective_api_key("openrouter") == ("plain", "store")


# ---------------------------------------------------------------------------
# Masked TTY prompt
# ---------------------------------------------------------------------------

def _wait_until_reading(master: int, slave: int, shown: bytearray) -> None:
    """Block until _read_masked on ``slave`` is ready for keystrokes.

    Seeing the prompt is not enough: _read_masked writes the prompt and
    *then* switches the terminal to raw mode with TCSAFLUSH, which throws
    away any input that arrived in between. Keys typed in that window are
    lost and the read blocks forever, so wait for the mode switch itself
    (ICANON off), which is the same call that does the flush.
    """
    import termios
    import time

    while not shown.endswith(b": "):
        shown.extend(os.read(master, 1024))
    deadline = time.monotonic() + 30
    while termios.tcgetattr(slave)[3] & termios.ICANON:
        assert time.monotonic() < deadline, "terminal never left canonical mode"
        time.sleep(0.001)


def _masked_read(keys: bytes) -> tuple[str, str]:
    """Run _read_masked on a pty, typing ``keys`` once it is reading.

    Returns (password, everything the terminal displayed).
    """
    import pty
    import threading

    master, slave = pty.openpty()
    shown = bytearray()

    def type_keys():
        _wait_until_reading(master, slave, shown)
        os.write(master, keys)

    typist = threading.Thread(target=type_keys, daemon=True)
    typist.start()
    try:
        with os.fdopen(slave, "w", closefd=False) as out:
            password = encryption._read_masked("Password: ", slave, out)
        typist.join(timeout=5)
        os.set_blocking(master, False)
        try:
            while chunk := os.read(master, 1024):
                shown.extend(chunk)
        except OSError:
            pass
    finally:
        os.close(master)
        os.close(slave)
    return password, shown.decode()


@pytest.mark.skipif(os.name != "posix", reason="needs a pty")
class TestMaskedPrompt:
    def test_each_character_shows_as_a_star(self):
        password, shown = _masked_read(b"hunter2\n")
        assert password == "hunter2"
        assert "Password: *******" in shown
        assert "hunter2" not in shown

    def test_backspace_removes_one_character_and_its_star(self):
        password, shown = _masked_read(b"abcd\x7f\x7fx\n")
        assert password == "abx"
        assert shown.count("*") == 5
        assert shown.count("\b \b") == 2

    def test_ctrl_u_clears_the_line(self):
        password, shown = _masked_read(b"wrong\x15right\n")
        assert password == "right"
        assert shown.count("\b \b") == 5

    def test_multibyte_character_is_one_star(self):
        password, shown = _masked_read("pä€\n".encode())
        assert password == "pä€"
        assert shown.count("*") == 3

    def test_terminal_settings_are_restored(self):
        import pty
        import termios

        master, slave = pty.openpty()
        try:
            before = termios.tcgetattr(slave)
            import threading

            def press_ctrl_d():
                _wait_until_reading(master, slave, bytearray())
                os.write(master, b"\x04")

            # Nothing typed: Ctrl-D on an empty line ends the read with EOFError.
            threading.Thread(target=press_ctrl_d, daemon=True).start()
            with os.fdopen(slave, "w", closefd=False) as out:
                with pytest.raises(EOFError):
                    encryption._read_masked("Password: ", slave, out)
            assert termios.tcgetattr(slave) == before
        finally:
            os.close(master)
            os.close(slave)
