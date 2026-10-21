"""Synthetic imaplib FETCH response shapes; no provider connection required."""

from typing import Any

import pytest

from mailboxd.imap_client import _parse_header_fetch

HEADER = (
    b"From: sender@example.test\r\n"
    b"Subject: synthetic UID 999 RFC822.SIZE 999 FLAGS (spoofed)\r\n"
    b"Message-ID: <synthetic@example.test>\r\n\r\n"
)


def literal(prefix: bytes = b"3 (") -> tuple[bytes, bytes]:
    return (
        prefix
        + b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)] {"
        + str(len(HEADER)).encode()
        + b"}",
        HEADER,
    )


@pytest.mark.parametrize(
    ("prefix", "tail"),
    [
        (b"3 (", b" FLAGS (\\Seen) RFC822.SIZE 200 UID 42)"),
        (b"3 (UID 42 RFC822.SIZE 200 FLAGS (\\Seen) ", b")"),
        (b"3 (UID 42 ", b" RFC822.SIZE 200 FLAGS (\\Seen))"),
        (b"3 (FLAGS (\\Seen) ", b" UID 42 RFC822.SIZE 200)"),
    ],
)
def test_attributes_before_or_after_literal(prefix: bytes, tail: bytes) -> None:
    rows = _parse_header_fetch([literal(prefix), tail])
    assert len(rows) == 1
    row = rows[0]
    assert (row["uid"], row["size"], row["flags"]) == ("42", 200, ["\\Seen"])
    assert row["from"] == "sender@example.test"
    assert row["subject"] == "synthetic UID 999 RFC822.SIZE 999 FLAGS (spoofed)"
    assert row["message_id"] == "<synthetic@example.test>"


def test_multiple_messages_keep_their_own_attributes() -> None:
    rows = _parse_header_fetch(
        [
            literal(),
            b" FLAGS (\\Seen) RFC822.SIZE 200 UID 42)",
            literal(b"4 ("),
            b" UID 43 RFC822.SIZE 300 FLAGS ())",
        ]
    )
    assert [(r["uid"], r["size"], r["flags"]) for r in rows] == [
        ("42", 200, ["\\Seen"]),
        ("43", 300, []),
    ]


@pytest.mark.parametrize("tail", [[], [b")"]])
def test_literal_contents_are_never_metadata(tail: list[Any]) -> None:
    row = _parse_header_fetch([literal(), *tail])[0]
    assert (row["uid"], row["size"], row["flags"]) == ("", None, [])


@pytest.mark.parametrize("closing", [[], [b")"]])
def test_unsolicited_fetch_is_not_a_continuation(closing: list[Any]) -> None:
    rows = _parse_header_fetch(
        [
            literal(),
            *closing,
            b"9 (UID 99 RFC822.SIZE 999 FLAGS (\\Flagged))",
            literal(b"4 ("),
            b" UID 43 RFC822.SIZE 300 FLAGS ())",
        ]
    )
    assert [(r["uid"], r["size"], r["flags"]) for r in rows] == [
        ("", None, []),
        ("43", 300, []),
    ]


def test_empty_and_nonliteral_responses_are_ignored() -> None:
    assert _parse_header_fetch([]) == []
    assert _parse_header_fetch([None, b")", (), b"9 (UID 99 FLAGS ())"]) == []
