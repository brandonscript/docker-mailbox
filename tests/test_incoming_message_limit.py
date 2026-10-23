"""Incoming RFC822 budgets are enforced before parsing, including service reuse."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import Mock

import imap_mock
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mailboxd import imap_client, watch
from mailboxd.config import Config, ImapConfig, LimitsConfig
from mailboxd.errors import TooLarge
from mailboxd.imap_client import ImapError, Session
from mailboxd.principal import Principal
from mailboxd.schemas import DraftUpdate, ReplyRequest
from mailboxd.server import create_app
from mailboxd.services import Service

CFG = ImapConfig(host="unused.invalid", username="test", password="test")


def scripted(size_data: list[Any], body: bytes = b"12345") -> tuple[Session, Mock]:
    conn = Mock()
    conn.uid.side_effect = [
        ("OK", size_data),
        ("OK", [(b"1 (UID 7 BODY[]<0> {" + str(len(body)).encode() + b"}", body), b")"]),
    ]
    return Session(conn, CFG, incoming_message_max_bytes=5), conn


def test_default_and_positive_config() -> None:
    assert LimitsConfig().incoming_message_max_bytes == 32 * 1024 * 1024
    assert LimitsConfig(incoming_message_max_bytes=1).incoming_message_max_bytes == 1
    for value in (0, -1):
        with pytest.raises(ValidationError):
            LimitsConfig(incoming_message_max_bytes=value)


def test_boundary_accepted_with_capped_peek() -> None:
    s, conn = scripted([b"1 (RFC822.SIZE 5 UID 7)"])
    assert s.fetch_raw("7") == b"12345"
    assert conn.uid.call_args_list[0].args == ("FETCH", "7", "(RFC822.SIZE)")
    assert conn.uid.call_args_list[1].args == ("FETCH", "7", "(BODY.PEEK[]<0.6>)")


def test_oversized_advertised_source_never_downloaded() -> None:
    s, conn = scripted([b"1 (UID 7 RFC822.SIZE 6)"])
    with pytest.raises(TooLarge) as err:
        s.fetch_raw("7")
    assert err.value.details == {"limit": 5, "size": 6}
    assert conn.uid.call_count == 1


@pytest.mark.parametrize("header_only", [False, True])
@pytest.mark.parametrize("body", [b"123456", b"12345678901234567890"])
def test_incorrect_size_or_ignored_partial_cannot_reach_parser(
    body: bytes, header_only: bool
) -> None:
    s, conn = scripted([b"1 (UID 7 RFC822.SIZE 5)"], body)
    with pytest.raises(TooLarge):
        s.fetch_message("7", header_only=header_only)
    section = "HEADER" if header_only else ""
    assert conn.uid.call_args.args == ("FETCH", "7", f"(BODY.PEEK[{section}]<0.6>)")


@pytest.mark.parametrize(
    "data",
    [
        [],
        [None],
        [b"1 (UID 7)"],
        [b"1 (UID 7 RFC822.SIZE -1)"],
        [b"1 (UID 8 RFC822.SIZE 1)"],
        [(b"1 (UID 7)", b"RFC822.SIZE 1")],
    ],
)
def test_missing_malformed_or_unrelated_size_fails_closed(data: list[Any]) -> None:
    s, conn = scripted(data)
    with pytest.raises(ImapError):
        s.fetch_raw("7")
    assert conn.uid.call_count == 1


def test_inconsistent_download_fails_instead_of_parsing_truncated_message() -> None:
    s, _ = scripted([b"1 (UID 7 RFC822.SIZE 5)"], b"1234")
    with pytest.raises(ImapError, match="size changed"):
        s.fetch_message("7")


def test_unsolicited_response_does_not_choose_size_or_body() -> None:
    s, conn = scripted([b"2 (UID 8 RFC822.SIZE 999)", b"1 (UID 7 RFC822.SIZE 5)"])
    conn.uid.side_effect = [
        ("OK", [b"2 (UID 8 RFC822.SIZE 999)", b"1 (UID 7 RFC822.SIZE 5)"]),
        (
            "OK",
            [(b"2 (UID 8 BODY[]<0> {1}", b"x"), b")", (b"1 (BODY[]<0> {5}", b"12345"), b" UID 7)"],
        ),
    ]
    assert s.fetch_raw("7") == b"12345"


@pytest.mark.parametrize("uid", ["1:9", "1,2", "1:*", "0", "-1", "7\r\nEXPUNGE"])
def test_raw_read_accepts_one_uid_only(uid: str) -> None:
    s, conn = scripted([])
    with pytest.raises(ImapError):
        s.fetch_raw(uid)
    conn.uid.assert_not_called()


@contextmanager
def live_service(tmp_path: Path, limit: int) -> Iterator[tuple[Service, imap_mock.Sink, Principal]]:
    raw = imap_mock.sample_message("oversized") + b"x" * 512
    sink, server, host, port = imap_mock.start(
        {1: raw}, caps=imap_mock.MODERN_CAPS, folders={"Drafts": {1: raw}}
    )
    cfg = Config.model_validate(
        {
            "state": {"path": str(tmp_path / "state.db")},
            "limits": {"incoming_message_max_bytes": limit},
            "mailboxes": [
                {
                    "name": "test",
                    "imap": {
                        "host": host,
                        "port": port,
                        "tls": "none",
                        "username": "u",
                        "password": "p",
                    },
                    "smtp": {
                        "host": "unused.invalid",
                        "username": "u",
                        "password": "p",
                        "from_address": "alice@example.com",
                    },
                }
            ],
        }
    )
    try:
        yield Service(cfg), sink, Principal("test", frozenset({"read", "draft", "send"}))
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "operation",
    [
        "message",
        "thread",
        "raw",
        "attachment",
        "reply",
        "reply_draft",
        "update_draft",
        "send_draft",
    ],
)
def test_service_paths_share_configured_limit_before_parsing_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with live_service(tmp_path, 128) as (svc, sink, principal):
        parse = Mock(side_effect=AssertionError("oversized source reached MIME parser"))
        monkeypatch.setattr(imap_client.email, "message_from_bytes", parse)
        with pytest.raises(TooLarge) as err:
            if operation == "message":
                svc.get_message(principal, "test", "1", reader=True)
            elif operation == "thread":
                svc.thread(principal, "test", "1")
            elif operation == "raw":
                svc.get_raw(principal, "test", "1")
            elif operation == "attachment":
                svc.get_attachment(principal, "test", "1", "1")
            elif operation in {"reply", "reply_draft"}:
                svc.reply(
                    principal,
                    "test",
                    "1",
                    ReplyRequest(
                        body_text="reply",
                        draft=operation == "reply_draft",
                    ),
                )
            elif operation == "update_draft":
                svc.update_draft(principal, "test", "1", DraftUpdate(body_text="new"))
            else:
                svc.send_draft(principal, "test", "1", dry_run=True)
        parse.assert_not_called()
        assert err.value.status == 413
        assert err.value.code == "too_large"
        assert err.value.details["limit"] == 128
        fetches = [c for c in sink.commands if "UID FETCH" in c]
        assert fetches and all(
            "(RFC822.SIZE)" in c or "BODY.PEEK[HEADER]<0.129>" in c for c in fetches
        )
        assert not sink.appended
        assert sink.flags(1) == set()
        assert sink.flags(1, "Drafts") == set()


def test_configured_limit_allows_source_and_peek_preserves_flags(tmp_path: Path) -> None:
    with live_service(tmp_path, 2048) as (svc, sink, principal):
        result = svc.get_message(principal, "test", "1", reader=True)
        assert result["subject"] == "oversized"
        assert any("BODY.PEEK[]<0.2049>" in c for c in sink.commands)
        assert sink.flags(1) == set()


def test_header_batch_is_capped_and_rejected_before_parsing() -> None:
    s, conn = scripted([])
    conn.uid.side_effect = None
    conn.uid.return_value = (
        "OK",
        [(b"1 (UID 7 BODY[HEADER.FIELDS (SUBJECT)]<0> {6}", b"123456"), b")"],
    )
    with pytest.raises(TooLarge):
        s.fetch_headers([7])
    assert "<0.6>" in conn.uid.call_args.args[2]


def test_header_listing_still_available_for_large_bodies(tmp_path: Path) -> None:
    with live_service(tmp_path, 256) as (svc, sink, principal):
        page = svc.list_messages(principal, "test")
        assert page["messages"][0]["subject"] == "oversized"
        assert page["messages"][0]["size"] > 256
        assert any("HEADER.FIELDS" in c and "<0.257>" in c for c in sink.commands)


@pytest.mark.parametrize(
    "path",
    [
        "/mailboxes/test/messages/1",
        "/mailboxes/test/messages/1/raw",
        "/mailboxes/test/messages/1/attachments/1",
        "/mailboxes/test/messages/1/thread",
    ],
)
def test_http_returns_typed_413_for_oversized_source(tmp_path: Path, path: str) -> None:
    with live_service(tmp_path, 128) as (svc, sink, _):
        svc.cfg.auth.tokens = ["EXAMPLE-DO-NOT-USE-size-test"]
        with TestClient(create_app(svc.cfg)) as client:
            response = client.get(
                path,
                headers={
                    "Authorization": "Bearer EXAMPLE-DO-NOT-USE-size-test",
                },
            )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "too_large"
        assert response.json()["error"]["details"]["limit"] == 128
        assert not any("BODY.PEEK[]" in c for c in sink.commands)


def test_mcp_returns_typed_error_for_oversized_source(tmp_path: Path) -> None:
    with live_service(tmp_path, 128) as (svc, sink, _):
        svc.cfg.auth.tokens = ["EXAMPLE-DO-NOT-USE-size-test"]
        with TestClient(create_app(svc.cfg)) as client:
            response = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer EXAMPLE-DO-NOT-USE-size-test",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_message", "arguments": {"mailbox": "test", "uid": "1"}},
                },
            )
        assert response.status_code == 200
        if response.headers["content-type"].startswith("application/json"):
            payload = response.json()
        else:
            payload = json.loads(
                next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
            )
        result = payload["result"]
        assert result["isError"] is True
        assert "too_large" in result["content"][0]["text"]
        assert not any("BODY.PEEK[]" in c for c in sink.commands)


def test_header_only_reads_allow_large_attachment_sources(tmp_path: Path) -> None:
    with live_service(tmp_path, 512) as (svc, sink, _):
        mb = svc.cfg.mailboxes[0]
        with svc._session(mb) as session:
            session.select("INBOX")
            assert session.fetch_message("1", header_only=True)["Subject"] == "oversized"
            with pytest.raises(TooLarge):
                session.fetch_raw("1")
        assert any("BODY.PEEK[HEADER]<0.513>" in c for c in sink.commands)
        assert not any("BODY.PEEK[]" in c for c in sink.commands)


def test_watcher_retries_oversized_headers_without_stopping_worker(tmp_path: Path) -> None:
    with live_service(tmp_path, 128) as (svc, _, _):
        mb = svc.cfg.mailboxes[0]
        svc.state.set_cursor(watch.CURSOR_ACTOR, watch.CURSOR_NAME, mb.name, "INBOX", "1", 0)
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        watch.watch_mailbox(svc.cfg, svc.state, mb, stop)
        stop.wait.assert_called_once_with(1.0)
        assert svc.state.get_cursor(watch.CURSOR_ACTOR, watch.CURSOR_NAME, mb.name, "INBOX")[1] == 0
