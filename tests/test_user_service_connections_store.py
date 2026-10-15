"""Tests for multi-connection rows in db/user_service_credential_store.py.

Covers the connection API (create / update / set-default / delete /
upsert-by-account, ConnectionExists / ConnectionLimit, normalizers, list
ordering), the exactly-one-default invariant, the single-connection API's
default-row semantics, and the ``service_credentials`` /
``service_connections`` pair on user dicts. Uses an isolated SQLite file per
test (schema from ``Base.metadata.create_all``), driven via ``asyncio.run``.
"""

import asyncio
import uuid

import pytest

from config.plugin_types import QuestPlugin, UserConnectionSpec


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(isolated_db):
    import db.user_service_credential_store as store_mod
    import db.user_store as user_store_mod

    user = _run(user_store_mod.create_user(
        email=f"test-{uuid.uuid4().hex}@example.com",
        name="Test",
        api_key=f"k-{uuid.uuid4().hex}",
    ))
    return {
        "store": store_mod,
        "user_store": user_store_mod,
        "engine": isolated_db.engine,
        "user": user,
        "uid": user["id"],
    }


def _accounts(store, uid, service="imap"):
    return [
        (r["account"], r["is_default"])
        for r in _run(store.list_connections(uid, service))
    ]


def _default_count(env, service="imap"):
    from sqlalchemy import text
    with env["engine"].connect() as conn:
        return conn.execute(text(
            "SELECT COUNT(*) FROM user_service_credentials "
            "WHERE user_id = :u AND service = :s AND is_default"
        ), {"u": env["uid"], "s": service}).scalar()


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def test_normalize_account(env):
    store = env["store"]
    assert store.normalize_account("  Work@Example.COM ") == "work@example.com"
    assert store.normalize_account("Home-Lab") == "home-lab"
    assert store.normalize_account("user@mail.example.org:993") == "user@mail.example.org:993"
    for bad in ("", "   ", "a" * 321, None, 5):
        with pytest.raises(ValueError):
            store.normalize_account(bad)
    assert store.normalize_account("a" * 320) == "a" * 320


@pytest.mark.parametrize("bad, reason", [
    ("home lab", "whitespace"),
    ("a\tb@x.com", "whitespace"),
    ("a\nb@x.com", "whitespace"),
    ("a\u00a0b@x.com", "whitespace"),        # no-break space
    ("a\x00b@x.com", "control"),
    ("a\x1bb@x.com", "control"),
    ("a\u200bb@x.com", "control"),           # zero-width space (format char)
    ('a"b@x.com', "'\"'"),
    ("a`b@x.com", "'`'"),
])
def test_normalize_account_rejects_unsafe_characters(env, bad, reason):
    with pytest.raises(ValueError, match=reason):
        env["store"].normalize_account(bad)


def test_create_connection_rejects_unsafe_account(env):
    store = env["store"]
    with pytest.raises(ValueError):
        _run(store.create_connection(env["uid"], "svc", account="two words"))


def test_normalize_description(env):
    store = env["store"]
    assert store.normalize_description(None) is None
    assert store.normalize_description("") is None
    assert store.normalize_description(" \n\t ") is None
    assert store.normalize_description("  work\n mailbox\t\tdaily ") == "work mailbox daily"
    assert store.normalize_description("x" * 200) == "x" * 200
    with pytest.raises(ValueError):
        store.normalize_description("x" * 201)
    with pytest.raises(ValueError):
        store.normalize_description(42)


# ---------------------------------------------------------------------------
# create / list / get
# ---------------------------------------------------------------------------

def test_first_connection_is_default_second_is_not(env):
    store, uid = env["store"], env["uid"]
    first = _run(store.create_connection(
        uid, "imap", account="Work@Example.com", description=" work\nbox ",
        oauth_blob={"values": {"password": "p1"}},
    ))
    assert first["account"] == "work@example.com"
    assert first["description"] == "work box"
    assert first["is_default"] is True
    assert isinstance(first["id"], int)
    assert first["oauth_blob"] == {"values": {"password": "p1"}}

    second = _run(store.create_connection(uid, "imap", account="home@example.com"))
    assert second["is_default"] is False
    assert second["description"] is None
    assert _accounts(store, uid) == [
        ("work@example.com", True), ("home@example.com", False),
    ]


def test_first_connection_is_default_even_with_make_default_false(env):
    store, uid = env["store"], env["uid"]
    row = _run(store.create_connection(uid, "imap", account="a", make_default=False))
    assert row["is_default"] is True


def test_make_default_moves_the_default(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    row = _run(store.create_connection(uid, "imap", account="b", make_default=True))
    assert row["is_default"] is True
    assert _accounts(store, uid) == [("b", True), ("a", False)]
    assert _default_count(env) == 1


def test_connection_exists_is_case_insensitive(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="work@example.com"))
    with pytest.raises(store.ConnectionExists) as exc:
        _run(store.create_connection(uid, "imap", account="  WORK@example.COM"))
    assert exc.value.service == "imap"
    assert exc.value.account == "work@example.com"
    # The same account on another service or for another user is fine.
    _run(store.create_connection(uid, "other", account="work@example.com"))


def test_connection_limit(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a", limit=2))
    _run(store.create_connection(uid, "imap", account="b", limit=2))
    with pytest.raises(store.ConnectionLimit) as exc:
        _run(store.create_connection(uid, "imap", account="c", limit=2))
    assert exc.value.service == "imap"
    assert exc.value.limit == 2
    # No cap without a limit.
    _run(store.create_connection(uid, "imap", account="c"))
    assert len(_run(store.list_connections(uid, "imap"))) == 3


def test_create_rejects_invalid_account_and_description(env):
    store, uid = env["store"], env["uid"]
    with pytest.raises(ValueError):
        _run(store.create_connection(uid, "imap", account="   "))
    with pytest.raises(ValueError):
        _run(store.create_connection(uid, "imap", account="a", description="x" * 201))
    assert _run(store.list_connections(uid, "imap")) == []


def test_list_ordering_default_first_then_created(env):
    store, uid = env["store"], env["uid"]
    for name in ("a", "b", "c", "d"):
        _run(store.create_connection(uid, "imap", account=name))
    c = _run(store.get_connection(uid, "imap", account="c"))
    _run(store.set_default_connection(uid, "imap", c["id"]))
    assert [a for a, _ in _accounts(store, uid)] == ["c", "a", "b", "d"]


def test_list_all_connections_groups_by_service(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    _run(store.create_connection(uid, "imap", account="b", make_default=True))
    _run(store.upsert_credential(uid, "acme", secret="sk"))
    allc = _run(store.list_all_connections(uid))
    assert set(allc) == {"imap", "acme"}
    assert [r["account"] for r in allc["imap"]] == ["b", "a"]
    assert allc["acme"][0]["secret"] == "sk"
    assert allc["acme"][0]["account"] == ""


def test_get_connection_by_account_and_id(env):
    store, uid = env["store"], env["uid"]
    a = _run(store.create_connection(uid, "imap", account="a@x.com"))
    b = _run(store.create_connection(uid, "imap", account="b@x.com"))
    assert _run(store.get_connection(uid, "imap", account=" A@X.com "))["id"] == a["id"]
    assert _run(store.get_connection(uid, "imap", row_id=b["id"]))["account"] == "b@x.com"
    assert _run(store.get_connection(uid, "imap", account="a@x.com", row_id=b["id"])) is None
    assert _run(store.get_connection(uid, "other", row_id=a["id"])) is None
    assert _run(store.get_connection(uid, "imap", account="nope")) is None
    with pytest.raises(ValueError):
        _run(store.get_connection(uid, "imap"))


def test_rows_are_scoped_to_their_user(env):
    store, uid = env["store"], env["uid"]
    other = _run(env["user_store"].create_user(
        email=f"o-{uuid.uuid4().hex}@example.com", name="O", api_key=f"k-{uuid.uuid4().hex}",
    ))
    mine = _run(store.create_connection(uid, "imap", account="a"))
    theirs = _run(store.create_connection(other["id"], "imap", account="a"))
    assert theirs["is_default"] is True
    assert _run(store.get_connection(other["id"], "imap", row_id=mine["id"])) is None
    assert _run(store.update_connection(other["id"], "imap", mine["id"], secret="x")) is None
    assert _run(store.set_default_connection(other["id"], "imap", mine["id"])) is None
    assert _run(store.delete_connection(other["id"], "imap", mine["id"])) is False
    assert _run(store.get_connection(uid, "imap", row_id=mine["id"])) is not None


# ---------------------------------------------------------------------------
# update / set default / delete
# ---------------------------------------------------------------------------

def test_update_connection_is_partial(env):
    store, uid = env["store"], env["uid"]
    row = _run(store.create_connection(
        uid, "imap", account="a", description="d", secret="s",
        oauth_blob={"values": {"k": 1}},
    ))
    updated = _run(store.update_connection(uid, "imap", row["id"], secret="s2"))
    assert updated["secret"] == "s2"
    assert updated["description"] == "d"
    assert updated["oauth_blob"] == {"values": {"k": 1}}
    assert updated["account"] == "a" and updated["is_default"] is True

    updated = _run(store.update_connection(
        uid, "imap", row["id"], description="  new\ndesc ", oauth_blob=None,
    ))
    assert updated["description"] == "new desc"
    assert updated["oauth_blob"] is None
    assert updated["secret"] == "s2"

    updated = _run(store.update_connection(uid, "imap", row["id"], description=""))
    assert updated["description"] is None

    with pytest.raises(ValueError):
        _run(store.update_connection(uid, "imap", row["id"], description="x" * 201))
    assert _run(store.update_connection(uid, "imap", 999999, secret="x")) is None


def test_set_default_moves_it(env):
    store, uid = env["store"], env["uid"]
    a = _run(store.create_connection(uid, "imap", account="a"))
    b = _run(store.create_connection(uid, "imap", account="b"))
    moved = _run(store.set_default_connection(uid, "imap", b["id"]))
    assert moved["is_default"] is True
    assert _accounts(store, uid) == [("b", True), ("a", False)]
    assert _default_count(env) == 1
    # Already default: a no-op that still returns the row.
    again = _run(store.set_default_connection(uid, "imap", b["id"]))
    assert again["id"] == b["id"] and again["is_default"] is True
    # And back.
    _run(store.set_default_connection(uid, "imap", a["id"]))
    assert _accounts(store, uid) == [("a", True), ("b", False)]
    assert _run(store.set_default_connection(uid, "imap", 999999)) is None


def test_delete_default_promotes_oldest_remaining(env):
    store, uid = env["store"], env["uid"]
    a = _run(store.create_connection(uid, "imap", account="a"))
    _run(store.create_connection(uid, "imap", account="b"))
    _run(store.create_connection(uid, "imap", account="c"))
    d = _run(store.create_connection(uid, "imap", account="d", make_default=True))
    assert _run(store.delete_connection(uid, "imap", d["id"])) is True
    # The oldest remaining ("a"), not the next in list order.
    assert _accounts(store, uid) == [("a", True), ("b", False), ("c", False)]
    assert _run(store.delete_connection(uid, "imap", a["id"])) is True
    assert _accounts(store, uid) == [("b", True), ("c", False)]
    assert _default_count(env) == 1


def test_delete_non_default_leaves_default(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    b = _run(store.create_connection(uid, "imap", account="b"))
    assert _run(store.delete_connection(uid, "imap", b["id"])) is True
    assert _accounts(store, uid) == [("a", True)]
    assert _run(store.delete_connection(uid, "imap", b["id"])) is False


def test_delete_last_connection_leaves_nothing(env):
    store, uid = env["store"], env["uid"]
    a = _run(store.create_connection(uid, "imap", account="a"))
    assert _run(store.delete_connection(uid, "imap", a["id"])) is True
    assert _run(store.list_connections(uid, "imap")) == []
    # A fresh first row becomes the default again.
    assert _run(store.create_connection(uid, "imap", account="b"))["is_default"] is True


def test_partial_unique_index_forbids_two_defaults(env):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    b = _run(store.create_connection(uid, "imap", account="b"))
    with pytest.raises(IntegrityError):
        with env["engine"].begin() as conn:
            conn.execute(
                text("UPDATE user_service_credentials SET is_default = 1 WHERE id = :i"),
                {"i": b["id"]},
            )


# ---------------------------------------------------------------------------
# Concurrent-first-row races (forced by hiding existing rows from one read)
# ---------------------------------------------------------------------------

def _hide_rows_once(monkeypatch, store, name, empty):
    """Make store.<name> pretend no rows exist on its first call only, as if
    a concurrent writer committed between our read and our commit."""
    real = getattr(store, name)
    calls = {"n": 0}

    async def fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return empty
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, name, fake)
    return calls


def test_create_retries_when_a_concurrent_first_row_took_the_default(env, monkeypatch):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    calls = _hide_rows_once(monkeypatch, store, "_list_service_rows", [])
    # First attempt thinks it is the first row, claims the default and trips
    # the partial index; the retry sees "a" and creates a non-default row.
    row = _run(store.create_connection(uid, "imap", account="b"))
    assert calls["n"] == 2
    assert row["is_default"] is False
    assert _accounts(store, uid) == [("a", True), ("b", False)]


def test_create_reraises_after_two_lost_races(env, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))

    async def always_empty(*args, **kwargs):
        return []

    monkeypatch.setattr(store, "_list_service_rows", always_empty)
    with pytest.raises(IntegrityError):
        _run(store.create_connection(uid, "imap", account="b"))
    assert _accounts(store, uid) == [("a", True)]


def test_create_lost_race_on_same_account_is_connection_exists(env, monkeypatch):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    _hide_rows_once(monkeypatch, store, "_list_service_rows", [])
    with pytest.raises(store.ConnectionExists):
        _run(store.create_connection(uid, "imap", account="A"))


def test_upsert_credential_retries_when_a_concurrent_create_took_the_default(env, monkeypatch):
    store, uid = env["store"], env["uid"]
    first = _run(store.create_connection(uid, "imap", account="a", secret="old"))
    calls = _hide_rows_once(monkeypatch, store, "_default_row", None)
    row = _run(store.upsert_credential(uid, "imap", secret="new"))
    assert calls["n"] == 2
    assert row["id"] == first["id"] and row["secret"] == "new"
    assert _accounts(store, uid) == [("a", True)]


def test_upsert_connection_by_account_survives_default_race(env, monkeypatch):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "gmail", account="a"))
    _hide_rows_once(monkeypatch, store, "_list_service_rows", [])
    row = _run(store.upsert_connection_by_account(uid, "gmail", account="b", secret="s"))
    assert row["is_default"] is False and row["secret"] == "s"


# ---------------------------------------------------------------------------
# upsert_connection_by_account
# ---------------------------------------------------------------------------

def test_upsert_connection_by_account_creates_then_updates_in_place(env):
    store, uid = env["store"], env["uid"]
    created = _run(store.upsert_connection_by_account(
        uid, "gmail", account="Me@Example.com", oauth_blob={"t": 1},
    ))
    assert created["account"] == "me@example.com"
    assert created["is_default"] is True
    assert created["description"] is None

    _run(store.update_connection(uid, "gmail", created["id"], description="mine"))
    updated = _run(store.upsert_connection_by_account(
        uid, "gmail", account="ME@example.com", oauth_blob={"t": 2},
    ))
    assert updated["id"] == created["id"]
    assert updated["oauth_blob"] == {"t": 2}
    assert updated["description"] == "mine"  # omitted -> unchanged

    other = _run(store.upsert_connection_by_account(
        uid, "gmail", account="other@example.com", secret="s", description="x",
    ))
    assert other["id"] != created["id"]
    assert other["is_default"] is False
    assert other["description"] == "x"
    assert len(_run(store.list_connections(uid, "gmail"))) == 2


def test_upsert_connection_by_account_respects_limit_on_create_only(env):
    store, uid = env["store"], env["uid"]
    _run(store.upsert_connection_by_account(uid, "gmail", account="a", limit=1))
    # Updating the existing account is not a new row.
    _run(store.upsert_connection_by_account(uid, "gmail", account="a", secret="s", limit=1))
    with pytest.raises(store.ConnectionLimit):
        _run(store.upsert_connection_by_account(uid, "gmail", account="b", limit=1))


# ---------------------------------------------------------------------------
# Single-connection API over multi-row services
# ---------------------------------------------------------------------------

def test_single_connection_api_unchanged_for_legacy_services(env):
    store, uid = env["store"], env["uid"]
    row = _run(store.upsert_credential(uid, "acme", secret="sk-1"))
    assert row["account"] == "" and row["is_default"] is True
    assert row["description"] is None
    row = _run(store.upsert_credential(uid, "acme", oauth_blob={"a": 1}))
    assert row["secret"] is None and row["oauth_blob"] == {"a": 1}
    assert len(_run(store.list_connections(uid, "acme"))) == 1
    assert _run(store.get_credential(uid, "acme"))["oauth_blob"] == {"a": 1}
    assert set(_run(store.list_credentials(uid))) == {"acme"}
    assert _run(store.delete_credential(uid, "acme")) is True
    assert _run(store.get_credential(uid, "acme")) is None


def test_single_connection_api_reads_and_writes_the_default(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a", secret="sa"))
    b = _run(store.create_connection(uid, "imap", account="b", secret="sb"))
    assert _run(store.get_credential(uid, "imap"))["secret"] == "sa"
    _run(store.set_default_connection(uid, "imap", b["id"]))
    assert _run(store.get_credential(uid, "imap"))["secret"] == "sb"
    assert _run(store.list_credentials(uid))["imap"]["account"] == "b"

    # upsert_credential replaces the default row's credential, keeping its
    # account and leaving the other row alone.
    row = _run(store.upsert_credential(uid, "imap", secret="sb2"))
    assert row["id"] == b["id"] and row["account"] == "b"
    assert _run(store.get_connection(uid, "imap", account="a"))["secret"] == "sa"


def test_delete_credential_removes_every_row(env):
    store, uid = env["store"], env["uid"]
    _run(store.create_connection(uid, "imap", account="a"))
    _run(store.create_connection(uid, "imap", account="b"))
    _run(store.upsert_credential(uid, "acme", secret="x"))
    assert _run(store.delete_credential(uid, "imap")) is True
    assert _run(store.list_connections(uid, "imap")) == []
    assert _run(store.get_credential(uid, "acme")) is not None
    assert _run(store.delete_all_credentials(uid)) == 1


# ---------------------------------------------------------------------------
# User dict
# ---------------------------------------------------------------------------

def _plugin(pid):
    return QuestPlugin(
        id=pid, label=pid.title(),
        user_connection=UserConnectionSpec(
            kind="api_key", connected=lambda row: bool(row.get("secret")),
        ),
    )


def test_user_dict_carries_default_and_all_connections(env, monkeypatch):
    store, user_store, uid = env["store"], env["user_store"], env["uid"]
    email = env["user"]["email"]
    from config import plugins as plugins_mod

    _run(store.create_connection(uid, "imap", account="a", secret="sa"))
    b = _run(store.create_connection(uid, "imap", account="b", secret="sb"))
    _run(store.set_default_connection(uid, "imap", b["id"]))
    _run(store.upsert_credential(uid, "acme", secret="sk"))

    monkeypatch.setattr(plugins_mod, "_LOADED", [])
    user = _run(user_store.get_user_by_email(email))
    assert "service_credentials" not in user
    assert "service_connections" not in user

    monkeypatch.setattr(plugins_mod, "_LOADED", [_plugin("acme")])
    user = _run(user_store.get_user_by_email(email))
    assert set(user["service_credentials"]) == {"imap", "acme"}
    assert user["service_credentials"]["imap"]["account"] == "b"
    assert user["service_credentials"]["imap"]["secret"] == "sb"
    assert user["service_credentials"]["acme"]["secret"] == "sk"
    assert [r["account"] for r in user["service_connections"]["imap"]] == ["b", "a"]
    assert user["service_connections"]["acme"] == [user["service_credentials"]["acme"]]
    # The shim row IS the list's default dict, so in-place token refreshes
    # (GitHub/Twitter mutate service_credentials[svc]["oauth_blob"]) show
    # through service_connections too.
    for svc in ("imap", "acme"):
        assert user["service_credentials"][svc] is user["service_connections"][svc][0]

    other = _run(user_store.create_user(
        email=f"e-{uuid.uuid4().hex}@example.com", name="E", api_key=f"k-{uuid.uuid4().hex}",
    ))
    fetched = _run(user_store.get_user_by_email(other["email"]))
    assert "service_credentials" not in fetched
    assert "service_connections" not in fetched


def test_create_all_builds_the_partial_default_index(env):
    from sqlalchemy import text
    with env["engine"].connect() as conn:
        indexes = dict(conn.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'user_service_credentials'"
        )).fetchall())
    assert "ix_user_service_credentials_user_id_service" not in indexes
    assert "UNIQUE" in indexes["ix_user_service_credentials_user_service_account"]
    sql = indexes["ix_user_service_credentials_user_service_default"]
    assert "UNIQUE" in sql and "WHERE is_default" in sql
    assert "ix_user_service_credentials_user_id" in indexes
