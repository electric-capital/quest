"""IMAP plugin settings: admin allow-list parsing and per-row helpers."""

import pytest

from plugins.imap import settings as s
from plugins.imap.settings import AllowedHost


def _row(**values):
    return {"account": "a@x.com", "oauth_blob": {"values": values}}


# -- parse_allowed_hosts / validate_imap_admin ----------------------------


def test_parse_basic_lines_case_whitespace_and_duplicates():
    text = "  IMAP.Example.COM \n\nmail.example.org:993\nimap.example.com\nmail.example.org:993\n"
    assert s.parse_allowed_hosts(text) == [
        AllowedHost("imap.example.com", None),
        AllowedHost("mail.example.org", 993),
    ]


def test_parse_trailing_dot_and_ipv4_and_ipv6():
    got = s.parse_allowed_hosts("imap.example.com.\n10.0.0.5:143\n[::1]:993\n[2001:DB8::1]")
    assert got == [
        AllowedHost("imap.example.com", None),
        AllowedHost("10.0.0.5", 143),
        AllowedHost("::1", 993),
        AllowedHost("2001:db8::1", None),
    ]
    assert [e.line() for e in got] == [
        "imap.example.com", "10.0.0.5:143", "[::1]:993", "[2001:db8::1]",
    ]


def test_same_host_different_ports_kept():
    got = s.parse_allowed_hosts("h.example.com:993\nh.example.com\nh.example.com:1993")
    assert len(got) == 3


@pytest.mark.parametrize("line, fragment", [
    ("imaps://imap.example.com", "scheme"),
    ("imap.example.com/inbox", "path"),
    ("user@imap.example.com", "user name"),
    ("imap.example.com:0", "out of range"),
    ("imap.example.com:65536", "out of range"),
    ("imap.example.com:abc", "invalid port"),
    ("imap.example.com:", "invalid port"),
    ("::1", "brackets"),
    ("[::1", "unclosed"),
    ("[not-an-ip]:993", "invalid IPv6"),
    ("[::1]993", "after ']'"),
    ("imap example.com", "whitespace"),
    ("-bad.example.com", "invalid host name"),
    ("bad host.example.com", "whitespace"),
    ("-under_.example.com", "invalid host name"),
    ("ünicode.example.com", "punycode"),
    ("999.1.1.1", "invalid IPv4"),
    ("a" * 64 + ".example.com", "invalid host name"),
])
def test_parse_rejects_bad_lines_with_line_number(line, fragment):
    with pytest.raises(ValueError) as exc:
        s.parse_allowed_hosts(f"ok.example.com\n\n{line}")
    assert str(exc.value).startswith("Line 3:")
    assert fragment in str(exc.value)


def test_validate_imap_admin_normalises():
    out = s.validate_imap_admin({"allowed_hosts": "B.example.com\n\n[::1]:993\nb.example.com"})
    assert out == {"allowed_hosts": "b.example.com\n[::1]:993"}


def test_validate_imap_admin_rejects_empty_and_bad():
    with pytest.raises(ValueError):
        s.validate_imap_admin({"allowed_hosts": "  \n "})
    with pytest.raises(ValueError, match="Line 1"):
        s.validate_imap_admin({"allowed_hosts": "http://x"})


def test_is_configured():
    assert s.imap_is_configured({"allowed_hosts": "imap.example.com"}) is True
    assert s.imap_is_configured({"allowed_hosts": ""}) is False
    assert s.imap_is_configured({}) is False
    assert s.imap_is_configured({"allowed_hosts": "imap.example.com\nhttp://bad"}) is False
    assert s.imap_is_configured(None) is False


# -- allow-list lookups ---------------------------------------------------


def test_allowed_ports_and_is_allowed():
    allowed = s.parse_allowed_hosts("a.example.com\nb.example.com:1993\n[::1]:993")
    assert s.allowed_ports("A.Example.com", allowed) == {993, 143}
    assert s.allowed_ports("b.example.com", allowed) == {1993}
    assert s.allowed_ports("c.example.com", allowed) == set()
    assert s.is_allowed("[::1]", 993, allowed)
    assert not s.is_allowed("::1", 143, allowed)
    assert not s.is_allowed("b.example.com", 993, allowed)


def test_load_allowed_hosts_reads_fresh(imap_admin_hosts):
    imap_admin_hosts.set("one.example.com")
    assert s.load_allowed_hosts() == [AllowedHost("one.example.com", None)]
    imap_admin_hosts.set("two.example.com:993")
    assert s.load_allowed_hosts() == [AllowedHost("two.example.com", 993)]
    imap_admin_hosts.set("http://broken")
    assert s.load_allowed_hosts() == []
    imap_admin_hosts.clear()
    assert s.load_allowed_hosts() == []
    assert imap_admin_hosts.reads == 4


def test_field_options_unique_hosts_in_admin_order(imap_admin_hosts):
    imap_admin_hosts.set("z.example.com:993\na.example.com\nz.example.com:143")
    assert s.imap_field_options() == {"host": ["z.example.com", "a.example.com"]}
    imap_admin_hosts.clear()
    assert s.imap_field_options() == {"host": []}


# -- row helpers ----------------------------------------------------------


@pytest.mark.parametrize("values, port", [
    ({"security": "ssl"}, 993),
    ({"security": "starttls"}, 143),
    ({"security": "ssl", "port": ""}, 993),
    ({"security": "ssl", "port": "  "}, 993),
    ({"security": "starttls", "port": "1143"}, 1143),
    ({"security": "ssl", "port": 995}, 995),
])
def test_effective_port(values, port):
    assert s.effective_port(values) == port


@pytest.mark.parametrize("values", [
    {"security": "ssl", "port": "x"},
    {"security": "ssl", "port": "70000"},
    {"security": "plain"},
    {},
])
def test_effective_port_errors(values):
    with pytest.raises(ValueError):
        s.effective_port(values)


def test_row_values_tolerates_missing_blob():
    assert s.row_values({}) == {}
    assert s.row_values({"oauth_blob": None}) == {}
    assert s.row_values({"oauth_blob": {"values": "x"}}) == {}
    assert s.row_values(_row(host="h")) == {"host": "h"}


def test_connected():
    assert s.imap_connected(_row(host="h", username="u", password="p")) is True
    assert s.imap_connected(_row(host="h", username="u", password="")) is False
    assert s.imap_connected(_row(host="h", username=" ", password="p")) is False
    assert s.imap_connected({"oauth_blob": None}) is False


def test_row_warning(imap_admin_hosts):
    imap_admin_hosts.set("imap.example.com\nalt.example.com:1993")
    ok = _row(host="imap.example.com", security="ssl", username="u", password="p")
    assert s.imap_row_warning(ok) is None
    starttls = _row(host="imap.example.com", security="starttls", username="u", password="p")
    assert s.imap_row_warning(starttls) is None
    wrong_port = _row(host="alt.example.com", security="ssl", username="u", password="p")
    assert s.imap_row_warning(wrong_port) == "Server no longer allowed"
    custom = _row(host="alt.example.com", port="1993", security="ssl", username="u", password="p")
    assert s.imap_row_warning(custom) is None
    # The admin removes the host: the existing row is flagged.
    imap_admin_hosts.set("alt.example.com:1993")
    assert s.imap_row_warning(ok) == "Server no longer allowed"
    # Rows without a host do not warn; malformed ports get their own warning.
    assert s.imap_row_warning({"oauth_blob": {}}) is None
    bad = _row(host="alt.example.com", port="nope", security="ssl")
    assert s.imap_row_warning(bad) == "Invalid port"


def test_underscore_hostnames_allowed(imap_admin_hosts):
    assert s.parse_allowed_hosts("mail_01.corp.internal\n_imap.example.com:993") == [
        s.AllowedHost("mail_01.corp.internal", None), s.AllowedHost("_imap.example.com", 993),
    ]
    imap_admin_hosts.set("mail_01.corp.internal")
    assert s.is_allowed("MAIL_01.corp.internal", 993)


def test_account_for():
    assert s.account_for("Alice@Example.COM", "imap.example.com") == "alice@example.com"
    assert s.account_for("Alice", "IMAP.example.com") == "alice@imap.example.com"
    assert s.account_for(" bob ", "imap.example.com") == "bob@imap.example.com"
