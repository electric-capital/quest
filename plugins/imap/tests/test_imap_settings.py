"""IMAP plugin settings: admin server-list parsing and per-row helpers."""

import re

import pytest

from plugins.imap import settings as s
from plugins.imap.settings import ServerConfig


def _row(**values):
    return {"account": "a@x.com", "oauth_blob": {"values": values}}


# -- parse_server_line: defaults --------------------------------------------


@pytest.mark.parametrize("line, expected", [
    # no port, no mode -> 993 ssl
    ("imap.example.com", ("imap.example.com", 993, "ssl")),
    # :143 without a mode -> starttls
    ("imap.example.com:143", ("imap.example.com", 143, "starttls")),
    # starttls without a port -> 143
    ("imap.example.com starttls", ("imap.example.com", 143, "starttls")),
    # ssl without a port -> 993
    ("imap.example.com ssl", ("imap.example.com", 993, "ssl")),
    # any other port without a mode -> ssl
    ("imap.example.com:993", ("imap.example.com", 993, "ssl")),
    ("imap.example.com:1993", ("imap.example.com", 1993, "ssl")),
    # explicit combinations are kept as written
    ("imap.example.com:143 ssl", ("imap.example.com", 143, "ssl")),
    ("imap.example.com:993 starttls", ("imap.example.com", 993, "starttls")),
    ("imap.example.com:1143 starttls", ("imap.example.com", 1143, "starttls")),
    # case and whitespace
    ("  IMAP.Example.COM:143   StartTLS ", ("imap.example.com", 143, "starttls")),
    ("imap.example.com\tSSL", ("imap.example.com", 993, "ssl")),
    ("imap.example.com STARTTLS", ("imap.example.com", 143, "starttls")),
    # trailing dot, IPv4, IPv6, underscores
    ("imap.example.com.", ("imap.example.com", 993, "ssl")),
    ("10.0.0.5:143", ("10.0.0.5", 143, "starttls")),
    ("[::1]", ("::1", 993, "ssl")),
    ("[2001:DB8::1]:143", ("2001:db8::1", 143, "starttls")),
    ("[::1]:1993 starttls", ("::1", 1993, "starttls")),
    ("mail_01.corp.internal", ("mail_01.corp.internal", 993, "ssl")),
])
def test_parse_server_line_defaults(line, expected):
    assert s.parse_server_line(line) == ServerConfig(*expected)


@pytest.mark.parametrize("config, line", [
    (ServerConfig("imap.example.com", 993, "ssl"), "imap.example.com:993 ssl"),
    (ServerConfig("imap.example.com", 143, "starttls"), "imap.example.com:143 starttls"),
    (ServerConfig("::1", 143, "starttls"), "[::1]:143 starttls"),
    (ServerConfig("10.0.0.5", 993, "ssl"), "10.0.0.5:993 ssl"),
])
def test_canonical_line_round_trips(config, line):
    assert config.line() == line
    assert s.parse_server_line(line) == config


@pytest.mark.parametrize("line, fragment", [
    ("imaps://imap.example.com", "scheme"),
    ("imap.example.com/inbox", "path"),
    ("user@imap.example.com", "user name"),
    ("imap.example.com:0", "out of range"),
    ("imap.example.com:65536", "out of range"),
    ("imap.example.com:abc", "invalid port"),
    ("imap.example.com:", "invalid port"),
    ("imap.example.com:-1 ssl", "invalid port"),
    ("::1", "brackets"),
    ("::1 ssl", "brackets"),
    ("[::1", "unclosed"),
    ("[not-an-ip]:993", "invalid IPv6"),
    ("[::1]993", "after ']'"),
    ("[fe80::1%eth0]", "zone ids"),
    ("-bad.example.com", "invalid host name"),
    ("-under_.example.com", "invalid host name"),
    ("ünicode.example.com", "punycode"),
    ("999.1.1.1", "invalid IPv4"),
    ("a" * 64 + ".example.com", "invalid host name"),
    # modes: only ssl and starttls
    ("imap.example.com plain", "use ssl or starttls"),
    ("imap.example.com:143 none", "use ssl or starttls"),
    ("imap.example.com tls", "use ssl or starttls"),
    ("imap.example.com:993 imaps", "use ssl or starttls"),
    ("imap example.com", "use ssl or starttls"),
    ("imap.example.com ssl starttls", "optional ssl or starttls"),
])
def test_parse_rejects_bad_lines_with_line_number(line, fragment):
    with pytest.raises(ValueError) as exc:
        s.parse_allowed_servers(f"ok.example.com\n\n{line}")
    assert str(exc.value).startswith("Line 3:")
    assert fragment in str(exc.value)


# -- parse_allowed_servers / validate_imap_admin ------------------------------


def test_parse_list_keeps_admin_order_and_skips_blank_lines():
    text = "  IMAP.Example.COM \n\nmail.example.org:143\n   \n[::1] starttls\n"
    assert s.parse_allowed_servers(text) == [
        ServerConfig("imap.example.com", 993, "ssl"),
        ServerConfig("mail.example.org", 143, "starttls"),
        ServerConfig("::1", 143, "starttls"),
    ]


def test_same_host_on_several_lines_with_different_port_or_mode():
    got = s.parse_allowed_servers(
        "h.example.com\nh.example.com:143\nh.example.com:143 ssl\nh.example.com:1993"
    )
    assert [e.line() for e in got] == [
        "h.example.com:993 ssl", "h.example.com:143 starttls",
        "h.example.com:143 ssl", "h.example.com:1993 ssl",
    ]


@pytest.mark.parametrize("text, message", [
    ("a.example\nb.example\na.example", "Line 3: duplicate of line 1 (a.example:993 ssl)"),
    # Duplicates only after the defaults / normalisation are applied.
    ("a.example\na.example:993 ssl", "Line 2: duplicate of line 1 (a.example:993 ssl)"),
    ("a.example:993\n\nA.Example. SSL", "Line 3: duplicate of line 1 (a.example:993 ssl)"),
    ("a.example:143\na.example starttls", "Line 2: duplicate of line 1 (a.example:143 starttls)"),
    ("[::1]:993\n[0:0::1] ssl", "Line 2: duplicate of line 1 ([::1]:993 ssl)"),
])
def test_duplicate_configurations_rejected(text, message):
    with pytest.raises(ValueError) as exc:
        s.parse_allowed_servers(text)
    assert str(exc.value) == message


def _entry(host, port="", security=""):
    return {"host": host, "port": port, "security": security}


@pytest.mark.parametrize("entry, expected", [
    (_entry("imap.example.com"), ("imap.example.com", 993, "ssl")),
    (_entry(" IMAP.Example.com. ", " 993 ", " SSL "), ("imap.example.com", 993, "ssl")),
    (_entry("imap.example.com", "", "starttls"), ("imap.example.com", 143, "starttls")),
    # An empty mode follows the port rule (143 -> starttls, else ssl).
    (_entry("imap.example.com", "143"), ("imap.example.com", 143, "starttls")),
    (_entry("imap.example.com", "1143"), ("imap.example.com", 1143, "ssl")),
    (_entry("imap.example.com", "1143", "starttls"), ("imap.example.com", 1143, "starttls")),
    # The form's default mode wins over the port rule.
    (_entry("imap.example.com", "143", "ssl"), ("imap.example.com", 143, "ssl")),
    (_entry("10.0.0.25"), ("10.0.0.25", 993, "ssl")),
    (_entry("::1", "143"), ("::1", 143, "starttls")),
    (_entry("[2001:DB8::1]", "", "starttls"), ("2001:db8::1", 143, "starttls")),
    ({"host": "imap.example.com"}, ("imap.example.com", 993, "ssl")),
])
def test_parse_server_entry(entry, expected):
    cfg = s.parse_server_entry(entry)
    assert (cfg.host, cfg.port, cfg.security) == expected
    assert cfg.entry() == {"host": expected[0], "port": str(expected[1]), "security": expected[2]}


@pytest.mark.parametrize("entry, message", [
    (_entry(""), "Host is required"),
    ({}, "Host is required"),
    ("imap.example.com", "server entry must be an object"),
    (_entry("imap.example.com:993"), "Host: write the port in the Port field"),
    (_entry("[::1]:993"), "Host: write the port in the Port field"),
    (_entry("imaps://imap.example.com"), "Host: no scheme"),
    (_entry("imap.example.com/x"), "Host: no path"),
    (_entry("me@imap.example.com"), "Host: no user name"),
    (_entry("[::1"), "Host: unclosed"),
    (_entry("mail.ex\u00e4mple.com"), "Host: non-ASCII"),
    (_entry("imap.example.com", "0"), "Port: port 0 is out of range"),
    (_entry("imap.example.com", "abc"), "Port: invalid port 'abc'"),
    (_entry("imap.example.com", "993", "plain"), "Security: unknown mode 'plain'"),
])
def test_parse_server_entry_rejects(entry, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        s.parse_server_entry(entry)


def test_parse_allowed_servers_accepts_entries_and_legacy_text():
    entries = s.parse_allowed_servers([_entry("b.example"), _entry("[::1]", "143")])
    assert [e.line() for e in entries] == ["b.example:993 ssl", "[::1]:143 starttls"]
    legacy = s.parse_allowed_servers("b.example\n[::1]:143")
    assert legacy == entries
    assert s.parse_allowed_servers(None) == []
    assert s.parse_allowed_servers([]) == []
    with pytest.raises(ValueError, match="must be a list of entries"):
        s.parse_allowed_servers({"host": "x"})


@pytest.mark.parametrize("entries, message", [
    ([_entry("a.example"), _entry("b.example"), _entry("a.example")],
     "Entry 3: duplicate of entry 1 (a.example:993 ssl)"),
    ([_entry("a.example"), _entry("A.Example.", "993", "SSL")],
     "Entry 2: duplicate of entry 1 (a.example:993 ssl)"),
    ([_entry("[::1]", "993"), _entry("0:0::1", "", "ssl")],
     "Entry 2: duplicate of entry 1 ([::1]:993 ssl)"),
    ([_entry("a.example"), _entry("x:y")], "Entry 2: Host: write the port in the Port field"),
])
def test_duplicate_and_bad_entries_rejected(entries, message):
    with pytest.raises(ValueError) as exc:
        s.parse_allowed_servers(entries)
    assert str(exc.value) == message


def test_validate_imap_admin_normalises_entries():
    out = s.validate_imap_admin({"allowed_hosts": [
        _entry("B.example.com"), _entry("::1", "143"),
        _entry("mail.example.org", "1143", "STARTTLS"), _entry("b.example.com", "143", "ssl"),
    ]})
    assert out == {"allowed_hosts": [
        {"host": "b.example.com", "port": "993", "security": "ssl"},
        {"host": "::1", "port": "143", "security": "starttls"},
        {"host": "mail.example.org", "port": "1143", "security": "starttls"},
        {"host": "b.example.com", "port": "143", "security": "ssl"},
    ]}
    # Saving the canonical entries again is a no-op.
    assert s.validate_imap_admin(out) == out


def test_validate_imap_admin_migrates_legacy_text():
    """A legacy text value (API callers, pre-baked configs) still saves,
    stored as entries."""
    out = s.validate_imap_admin({"allowed_hosts": "B.example.com\n\n[::1]:143"})
    assert out == {"allowed_hosts": [
        {"host": "b.example.com", "port": "993", "security": "ssl"},
        {"host": "::1", "port": "143", "security": "starttls"},
    ]}


def test_validate_imap_admin_rejects_empty_bad_and_duplicates():
    with pytest.raises(ValueError, match="add at least one server"):
        s.validate_imap_admin({"allowed_hosts": []})
    with pytest.raises(ValueError, match="add at least one server"):
        s.validate_imap_admin({"allowed_hosts": "  \n "})
    with pytest.raises(ValueError, match="add at least one server"):
        s.validate_imap_admin({})
    with pytest.raises(ValueError, match="Entry 1: Host: no scheme"):
        s.validate_imap_admin({"allowed_hosts": [_entry("http://x")]})
    with pytest.raises(ValueError, match="Line 1"):
        s.validate_imap_admin({"allowed_hosts": "http://x"})
    with pytest.raises(ValueError, match=re.escape("Entry 2: duplicate of entry 1")):
        s.validate_imap_admin({"allowed_hosts": [_entry("x.example"), _entry("x.example", "993")]})
    with pytest.raises(ValueError, match="use ssl or starttls"):
        s.validate_imap_admin({"allowed_hosts": [_entry("x.example", "143", "plain")]})


def test_imap_flatten_projects_legacy_text_into_entries():
    assert s.imap_flatten({"allowed_hosts": "imap.example.com\nmail.example.org:143", "x": 1}) == {
        "allowed_hosts": [
            {"host": "imap.example.com", "port": "993", "security": "ssl"},
            {"host": "mail.example.org", "port": "143", "security": "starttls"},
        ],
        "x": 1,
    }
    # Unparseable legacy text reads as no entries; entries pass through.
    assert s.imap_flatten({"allowed_hosts": "http://bad"}) == {"allowed_hosts": []}
    entries = [{"host": "a.example", "port": "993", "security": "ssl"}]
    assert s.imap_flatten({"allowed_hosts": entries}) == {"allowed_hosts": entries}
    assert s.imap_flatten({}) == {}
    assert s.imap_flatten(None) == {}


def test_is_configured():
    assert s.imap_is_configured({"allowed_hosts": [_entry("imap.example.com")]}) is True
    assert s.imap_is_configured({"allowed_hosts": []}) is False
    assert s.imap_is_configured({"allowed_hosts": [_entry("imap.example.com:993")]}) is False
    assert s.imap_is_configured({"allowed_hosts": [_entry("a.example"), _entry("a.example", "993")]}) is False
    # Legacy text cards stay configured until re-saved.
    assert s.imap_is_configured({"allowed_hosts": "imap.example.com"}) is True
    assert s.imap_is_configured({"allowed_hosts": "imap.example.com:993 ssl"}) is True
    assert s.imap_is_configured({"allowed_hosts": ""}) is False
    assert s.imap_is_configured({}) is False
    assert s.imap_is_configured({"allowed_hosts": "imap.example.com\nhttp://bad"}) is False
    assert s.imap_is_configured({"allowed_hosts": "a.example\na.example:993"}) is False
    assert s.imap_is_configured(None) is False


# -- admin list lookups ---------------------------------------------------------


def test_load_allowed_servers_reads_fresh(imap_admin_hosts):
    imap_admin_hosts.set("one.example.com")
    assert s.load_allowed_servers() == [ServerConfig("one.example.com", 993, "ssl")]
    imap_admin_hosts.set("two.example.com:143")
    assert s.load_allowed_servers() == [ServerConfig("two.example.com", 143, "starttls")]
    imap_admin_hosts.set("http://broken")
    assert s.load_allowed_servers() == []
    imap_admin_hosts.clear()
    assert s.load_allowed_servers() == []
    assert imap_admin_hosts.reads == 4


def test_field_options_are_canonical_lines_in_admin_order(imap_admin_hosts):
    imap_admin_hosts.set("z.example.com:143\na.example.com\nz.example.com\n[::1] starttls")
    assert s.imap_field_options() == {"server": [
        "z.example.com:143 starttls", "a.example.com:993 ssl", "z.example.com:993 ssl",
        "[::1]:143 starttls",
    ]}
    imap_admin_hosts.clear()
    assert s.imap_field_options() == {"server": []}


def test_resolve_server(imap_admin_hosts):
    allowed = s.parse_allowed_servers("a.example.com\nb.example.com:143\n[::1]:993")
    assert s.resolve_server("a.example.com:993 ssl", allowed) == ServerConfig("a.example.com", 993, "ssl")
    # Stored values are compared in canonical form.
    assert s.resolve_server("A.example.com", allowed) == ServerConfig("a.example.com", 993, "ssl")
    assert s.resolve_server("[::1]:993 ssl", allowed) == ServerConfig("::1", 993, "ssl")
    # Same host, different port / mode: not allowed.
    assert s.resolve_server("a.example.com:143 starttls", allowed) is None
    assert s.resolve_server("b.example.com:143 ssl", allowed) is None
    for bad in ("c.example.com:993 ssl", "http://a.example.com", "", "  ", None, 7, ["a"]):
        assert s.resolve_server(bad, allowed) is None
    # Without an explicit list the CURRENT admin card is read.
    imap_admin_hosts.set("a.example.com")
    assert s.resolve_server("a.example.com:993 ssl") is not None
    imap_admin_hosts.set("b.example.com")
    assert s.resolve_server("a.example.com:993 ssl") is None


# -- row helpers ----------------------------------------------------------


def test_row_values_tolerates_missing_blob():
    assert s.row_values({}) == {}
    assert s.row_values({"oauth_blob": None}) == {}
    assert s.row_values({"oauth_blob": {"values": "x"}}) == {}
    assert s.row_values(_row(server="h")) == {"server": "h"}


def test_connected():
    ok = {"server": "h.example:993 ssl", "username": "u", "password": "p"}
    assert s.imap_connected(_row(**ok)) is True
    assert s.imap_connected(_row(**{**ok, "password": ""})) is False
    assert s.imap_connected(_row(**{**ok, "username": " "})) is False
    assert s.imap_connected(_row(**{**ok, "server": ""})) is False
    assert s.imap_connected({"oauth_blob": None}) is False
    # A legacy host/port/security row is not connected.
    legacy = _row(host="h.example", port="", security="ssl", username="u", password="p")
    assert s.imap_connected(legacy) is False
    assert s.is_legacy_row(legacy) is True
    assert s.is_legacy_row(_row(**ok)) is False
    assert s.is_legacy_row({"oauth_blob": {}}) is False


def test_row_warning(imap_admin_hosts):
    imap_admin_hosts.set("imap.example.com\nimap.example.com:143\nalt.example.com:1993")
    ok = _row(server="imap.example.com:993 ssl", username="u", password="p")
    assert s.imap_row_warning(ok) is None
    starttls = _row(server="imap.example.com:143 starttls", username="u", password="p")
    assert s.imap_row_warning(starttls) is None
    custom = _row(server="alt.example.com:1993 ssl", username="u", password="p")
    assert s.imap_row_warning(custom) is None
    wrong_mode = _row(server="alt.example.com:1993 starttls", username="u", password="p")
    assert s.imap_row_warning(wrong_mode) == "Server no longer allowed"
    # The admin removes one line: only its rows are flagged.
    imap_admin_hosts.set("imap.example.com:143\nalt.example.com:1993")
    assert s.imap_row_warning(ok) == "Server no longer allowed"
    assert s.imap_row_warning(starttls) is None
    # Malformed stored values and legacy rows are flagged the same way.
    assert s.imap_row_warning(_row(server="nope://x", username="u")) == "Server no longer allowed"
    legacy = _row(host="imap.example.com", port="143", security="starttls", username="u", password="p")
    assert s.imap_row_warning(legacy) == "Server no longer allowed"
    # Rows without any values do not warn.
    assert s.imap_row_warning({"oauth_blob": {}}) is None


def test_account_for():
    assert s.account_for("Alice@Example.COM", "imap.example.com") == "alice@example.com"
    assert s.account_for("Alice", "IMAP.example.com") == "alice@imap.example.com"
    assert s.account_for(" bob ", "imap.example.com") == "bob@imap.example.com"
