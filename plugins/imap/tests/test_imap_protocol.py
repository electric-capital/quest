"""IMAP response parsing and argument encoding (plugins/imap/protocol.py)."""

import pytest

from plugins.imap import protocol as p


# -- FETCH ----------------------------------------------------------------


def test_fetch_single_literal():
    data = [(b"1 (UID 5 FLAGS (\\Seen) BODY[] {12}", b"Subject: x\r\n"), b")"]
    assert p.parse_fetch_response(data) == {
        5: {"UID": 5, "FLAGS": ["\\Seen"], "BODY[]": b"Subject: x\r\n"},
    }


def test_fetch_multiple_literals_and_messages():
    data = [
        (b"1 (UID 7 BODY[HEADER] {6}", b"A: b\r\n"),
        (b" BODY[TEXT] {5}", b"hello"),
        b" RFC822.SIZE 42)",
        b"2 (UID 9 FLAGS () RFC822.SIZE 3)",
        (b"3 (UID 11 BODY[1]<0> {0}", b""),
        b")",
    ]
    got = p.parse_fetch_response(data)
    assert got[7] == {"UID": 7, "BODY[HEADER]": b"A: b\r\n", "BODY[TEXT]": b"hello", "RFC822.SIZE": 42}
    assert got[9] == {"UID": 9, "FLAGS": [], "RFC822.SIZE": 3}
    assert got[11] == {"UID": 11, "BODY[1]<0>": b""}


def test_fetch_literal_body_containing_parens_and_newlines():
    body = b") (UID 99 FLAGS (\\Deleted))\r\n* 4 FETCH (UID 100)\r\n"
    data = [(b"1 (UID 5 BODY[] {%d}" % len(body), body), b" FLAGS (\\Seen))"]
    got = p.parse_fetch_response(data)
    assert list(got) == [5]
    assert got[5]["BODY[]"] == body
    assert got[5]["FLAGS"] == ["\\Seen"]


def test_fetch_peek_key_normalised_and_header_fields_section():
    data = [(b'1 (UID 3 BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)] {4}', b"x\r\n\r"), b")"]
    got = p.parse_fetch_response(data)
    assert got[3]["BODY[HEADER.FIELDS (FROM SUBJECT)]"] == b"x\r\n\r"


def test_fetch_drops_responses_without_uid():
    data = [b"4 (FLAGS (\\Seen))", b"5 (UID 8 FLAGS ())"]
    assert list(p.parse_fetch_response(data)) == [8]


def test_fetch_envelope_with_nil_nested_lists_and_literal_subject():
    data = [
        (b'1 (UID 2 ENVELOPE ("Mon, 5 Oct 2026 10:00:00 +0000" {8}', "Grüß".encode()),
        b' (("Bob \\"B\\"" NIL "bob" "example.org")) NIL NIL '
        b'((NIL NIL "alice" "example.com")("Carol" NIL "carol" "example.net")) '
        b'NIL NIL NIL "<id@x>") INTERNALDATE "05-Oct-2026 10:00:00 +0000")',
    ]
    got = p.parse_fetch_response(data)[2]
    env = got["ENVELOPE"]
    assert env[1] == "Grüß"
    assert env[2] == [["Bob \"B\"", None, "bob", "example.org"]]
    assert env[3] is None
    assert got["INTERNALDATE"] == "05-Oct-2026 10:00:00 +0000"
    decoded = p.decode_envelope(env)
    assert decoded["subject"] == "Grüß"
    assert decoded["from"] == ['Bob "B" <bob@example.org>']
    assert decoded["to"] == ["alice@example.com", "Carol <carol@example.net>"]
    assert decoded["cc"] == []
    assert decoded["message_id"] == "<id@x>"


def test_decode_envelope_rfc2047_and_groups():
    env = [
        None, "=?utf-8?q?Gr=C3=BC=C3=9Fe?=",
        [["=?iso-8859-1?q?J=F6rg?=", None, "jorg", "example.de"]],
        None, None,
        [["undisclosed", None, "undisclosed-recipients", None], [None, None, None, None]],
        None, None, None, None,
    ]
    out = p.decode_envelope(env)
    assert out["subject"] == "Grüße"
    assert out["from"] == ["Jörg <jorg@example.de>"]
    assert out["to"] == []
    assert p.decode_envelope(None)["subject"] is None


def test_status_response():
    assert p.parse_status_response([b'"INBOX" (MESSAGES 3 UNSEEN 1 UIDVALIDITY 77)']) == {
        "MESSAGES": 3, "UNSEEN": 1, "UIDVALIDITY": 77,
    }
    # Folder name sent as a literal.
    data = [(b"{3}", b"a(b"), b" (MESSAGES 0 UNSEEN 0)"]
    assert p.parse_status_response(data) == {"MESSAGES": 0, "UNSEEN": 0}
    assert p.parse_status_response([]) == {}


# -- LIST -----------------------------------------------------------------


def test_list_lines():
    assert p.parse_list_line(b'(\\HasNoChildren) "/" "INBOX"') == {
        "name": "INBOX", "raw_name": "INBOX", "delimiter": "/",
        "flags": ["\\HasNoChildren"], "special_use": None,
    }
    sent = p.parse_list_line(b'(\\HasNoChildren \\Sent) "." "Gesendete &AMQ-"')
    assert sent["name"] == "Gesendete Ä"
    assert sent["raw_name"] == "Gesendete &AMQ-"
    assert sent["delimiter"] == "."
    assert sent["special_use"] == "\\Sent"
    nil = p.parse_list_line(b"(\\Noselect) NIL Archive")
    assert nil["delimiter"] is None and nil["name"] == "Archive"
    literal = p.parse_list_line((b'(\\HasChildren) "/" {7}', b'a "b" c'))
    assert literal["name"] == 'a "b" c'
    assert p.parse_list_line(b"garbage") is None
    assert p.parse_list_line(b'(\\HasNoChildren) "/" "inbox"')["raw_name"] == "INBOX"


# -- modified UTF-7 ----------------------------------------------------------


@pytest.mark.parametrize("name, encoded", [
    ("INBOX", "INBOX"),
    ("Entwürfe", "Entw&APw-rfe"),
    ("A&B", "A&-B"),
    ("台北", "&U,BTFw-"),
    ("~peter/mail/日本語/台北", "~peter/mail/&ZeVnLIqe-/&U,BTFw-"),
    ("😀 smile", "&2D3eAA- smile"),
])
def test_modified_utf7_round_trip(name, encoded):
    assert p.encode_folder_name(name) == encoded
    assert p.decode_folder_name(encoded) == name


def test_modified_utf7_malformed_kept():
    assert p.decode_folder_name("bad&@@@-x") == "bad&@@@-x"
    assert p.decode_folder_name("open&ABC") == "open&ABC"


# -- quoting --------------------------------------------------------------


def test_quote():
    assert p.quote_imap_string('a "b" \\c') == '"a \\"b\\" \\\\c"'
    assert p.quote_folder("Entwürfe") == '"Entw&APw-rfe"'


@pytest.mark.parametrize("bad", ["a\r\nb", "x\nA1 DELETE INBOX", "nul\0", "cr\r"])
def test_quote_rejects_crlf_nul(bad):
    with pytest.raises(ValueError):
        p.quote_imap_string(bad)
    with pytest.raises(ValueError):
        p.quote_folder(bad)


def test_quote_rejects_non_ascii():
    with pytest.raises(ValueError, match="literal"):
        p.quote_imap_string("Grüße")


# -- BODYSTRUCTURE ----------------------------------------------------------

_MULTIPART = [
    [
        ["TEXT", "PLAIN", ["CHARSET", "utf-8"], None, None, "7BIT", 14, 1, None, None, None, None],
        ["TEXT", "HTML", ["CHARSET", "utf-8"], None, None, "QUOTED-PRINTABLE", 40, 2, None, None, None, None],
        "ALTERNATIVE", ["BOUNDARY", "b2"], None, None, None,
    ],
    ["APPLICATION", "PDF", ["NAME", "x.pdf"], None, None, "BASE64", 1000, None,
     ["ATTACHMENT", ["FILENAME", "Invoice 10.pdf"]], None, None],
    ["IMAGE", "PNG", None, "<img1>", None, "BASE64", 500, None, ["INLINE", None], None, None],
    ["MESSAGE", "RFC822", None, None, None, "7BIT", 300,
     [None, "fwd", None, None, None, None, None, None, None, None],
     ["TEXT", "PLAIN", None, None, None, "7BIT", 10, 1], 12, None, ["ATTACHMENT", None], None, None],
    "MIXED", ["BOUNDARY", "b1"], None, None, None,
]


def test_bodystructure_multipart_with_attachment():
    parts = p.bodystructure_parts(_MULTIPART)
    assert parts == [
        {"section": "1.1", "mime": "text/plain", "filename": None, "size": 14, "disposition": None},
        {"section": "1.2", "mime": "text/html", "filename": None, "size": 40, "disposition": None},
        {"section": "2", "mime": "application/pdf", "filename": "Invoice 10.pdf", "size": 1000,
         "disposition": "attachment"},
        {"section": "3", "mime": "image/png", "filename": None, "size": 500, "disposition": "inline"},
        {"section": "4", "mime": "message/rfc822", "filename": None, "size": 300,
         "disposition": "attachment"},
    ]
    assert p.bodystructure_has_attachments(_MULTIPART) is True


def test_bodystructure_single_part():
    bs = ["TEXT", "PLAIN", ["CHARSET", "us-ascii"], None, None, "7BIT", 5, 1]
    assert p.bodystructure_parts(bs) == [
        {"section": "1", "mime": "text/plain", "filename": None, "size": 5, "disposition": None},
    ]
    assert p.bodystructure_has_attachments(bs) is False
    assert p.bodystructure_parts(None) == []
    assert p.bodystructure_has_attachments([]) is False


def test_bodystructure_filename_from_name_param_rfc2047_and_rfc2231():
    bs = [
        ["TEXT", "PLAIN", None, None, None, "7BIT", 1, 1, None, None, None, None],
        ["APPLICATION", "OCTET-STREAM", ["NAME", "=?utf-8?q?R=C3=A9sum=C3=A9.pdf?="],
         None, None, "BASE64", 9, None, None, None, None],
        ["APPLICATION", "PDF", None, None, None, "BASE64", 9, None,
         ["ATTACHMENT", ["FILENAME*0*", "utf-8''%E2%82%AC%20", "FILENAME*1*", "rate.pdf"]], None, None],
        "MIXED", None, None, None, None,
    ]
    names = [part["filename"] for part in p.bodystructure_parts(bs)]
    assert names == [None, "Résumé.pdf", "€ rate.pdf"]


def test_bodystructure_string_disposition_pymap_form():
    """pymap sends the disposition as the raw header string."""
    bs = [
        ["TEXT", "PLAIN", ["CHARSET", "utf-8"], None, None, "7BIT", 14, 1, None, None, None, None],
        ["APPLICATION", "PDF", None, None, None, "BASE64", 10, None,
         'attachment; filename="invoice 2026-10.pdf"', None, None],
        ["IMAGE", "PNG", None, None, None, "BASE64", 10, None,
         "inline; filename*=utf-8''bild%C3%A4.png", None, None],
        "MIXED", ["BOUNDARY", "b"], None, None, None,
    ]
    parts = p.bodystructure_parts(bs)
    assert parts[1]["disposition"] == "attachment"
    assert parts[1]["filename"] == "invoice 2026-10.pdf"
    assert parts[2]["disposition"] == "inline"
    assert parts[2]["filename"] == "bildä.png"
    assert p.bodystructure_has_attachments(bs) is True
    text_only = [
        ["TEXT", "PLAIN", None, None, None, "7BIT", 1, 1, None, "inline", None, None],
        ["TEXT", "HTML", None, None, None, "7BIT", 1, 1, None, None, None, None],
        "ALTERNATIVE", None, None, None, None,
    ]
    assert p.bodystructure_has_attachments(text_only) is False


def test_bodystructure_from_parsed_fetch():
    data = [
        b'1 (UID 4 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 14 1 NIL NIL NIL NIL)'
        b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL ("ATTACHMENT" ("FILENAME" "a b.pdf")) NIL NIL)'
        b' "MIXED" ("BOUNDARY" "x") NIL NIL NIL))'
    ]
    bs = p.parse_fetch_response(data)[4]["BODYSTRUCTURE"]
    assert [part["section"] for part in p.bodystructure_parts(bs)] == ["1", "2"]
    assert p.bodystructure_parts(bs)[1]["filename"] == "a b.pdf"


# -- hostile / unusual server data (review fixes) -------------------------------


def test_fetch_literal8_binary_marker():
    """RFC 3516 BINARY literals use ``~{n}``; the ``~`` must not become a value."""
    data = [(b"1 (UID 5 BINARY[1] ~{5}", b"hello"), b" BINARY.SIZE[1] 5)"]
    assert p.parse_fetch_response(data) == {
        5: {"UID": 5, "BINARY[1]": b"hello", "BINARY.SIZE[1]": 5},
    }


def test_fetch_deep_nesting_raises_parse_error_not_recursion_error():
    deep = b"(" * 5000 + b")" * 5000
    with pytest.raises(p.ImapParseError):
        p.parse_fetch_response([b"1 (UID 5 BODYSTRUCTURE " + deep + b")"])
    # Within the cap still parses.
    ok = b"(" * p.MAX_NESTING + b")" * p.MAX_NESTING
    assert 5 in p.parse_fetch_response([b"1 (UID 5 X " + ok + b")"])


@pytest.mark.parametrize("atom", ["9" * 5000, "²", "٣"])
def test_fetch_non_ascii_or_huge_digit_atoms_stay_strings(atom):
    data = [f"1 (UID {atom} RFC822.SIZE {atom})".encode()]
    assert p.parse_fetch_response(data) == {}  # no integer UID
    ((_seq, items),) = p.parse_fetch_messages(data)
    assert items["UID"] == atom and isinstance(items["UID"], str)
    assert p.parse_fetch_messages([f"{atom} (UID 1)".encode()]) == []


def test_bodystructure_walk_is_depth_bounded():
    leaf = ["TEXT", "PLAIN", None, None, None, "7BIT", 1, 1]
    bs = leaf
    for _ in range(200):
        bs = [bs, "MIXED"]
    assert p.bodystructure_parts(bs) == []  # deeper than MAX_NESTING: ignored, no RecursionError


def test_attachment_rule_text_types():
    """Only text/plain and text/html are body candidates (render.py agrees)."""
    bs = [
        ["TEXT", "PLAIN", None, None, None, "7BIT", 1, 1, None, None, None, None],
        ["TEXT", "HTML", None, None, None, "7BIT", 1, 1, None, None, None, None],
        ["TEXT", "CALENDAR", ["METHOD", "REQUEST"], None, None, "7BIT", 9, 1, None, None, None, None],
        ["TEXT", "PLAIN", ["NAME", "notes.txt"], None, None, "7BIT", 1, 1, None, None, None, None],
        "MIXED", None, None, None, None,
    ]
    assert [x["mime"] for x in p.bodystructure_attachment_parts(bs)] == ["text/calendar", "text/plain"]
    assert p.bodystructure_has_attachments(bs) is True
    assert p.bodystructure_has_attachments(bs[:2] + ["ALTERNATIVE"]) is False


def test_bodystructure_mime_tokens_sanitised():
    bs = ["TEXT`) evil", "PLAIN", None, None, None, "7BIT", 1, 1]
    assert p.bodystructure_parts(bs)[0]["mime"] == "application/octet-stream"


def test_raw_name_candidate():
    assert p.raw_name_candidate("INBOX") is None
    assert p.raw_name_candidate("Entwürfe") is None  # not ASCII: a decoded name
    assert p.raw_name_candidate("Entw&APw-rfe") is None  # canonical: decoded form is used
    assert p.raw_name_candidate("&AOk-&AOk-") == "&AOk-&AOk-"  # non-canonical run split
    assert p.raw_name_candidate("bad&zz-name") == "bad&zz-name"  # invalid run kept verbatim
    assert p.quote_raw_folder("&AOk-&AOk-") == '"&AOk-&AOk-"'
    with pytest.raises(ValueError):
        p.quote_raw_folder("a\r\nb")
