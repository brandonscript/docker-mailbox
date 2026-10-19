"""Persistent delivery guarantees through SQLite, HTTP, IMAP and service boundaries."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_services_fakes import make_raw, make_service, principal
from test_watch import FakeSession, check, make_cfg

from mailboxd import watch
from mailboxd.config import Config, MailboxConfig
from mailboxd.errors import Conflict
from mailboxd.server import create_app
from mailboxd.services import Service
from mailboxd.state import DELIVERY_LEASE_SECONDS, DELIVERY_RETRY_SECONDS, State

pytest_plugins = ("test_imap_session", "test_watch")


def test_request_size_limit_prevents_dispatch() -> None:
    cfg = Config.model_validate(
        {
            "auth": {"tokens": ["EXAMPLE-DO-NOT-USE"]},
            "limits": {"request_max_bytes": 128},
        }
    )
    with TestClient(create_app(cfg)) as client:
        response = client.post(
            "/mcp",
            content=b"x" * 129,
            headers={
                "Authorization": "Bearer EXAMPLE-DO-NOT-USE",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def seed(state: State, count: int = 1, recipients: list[str] | None = None) -> None:
    for uid in range(1, count + 1):
        state.enqueue_notifications(
            {"event_id": f"event-{uid}", "mailbox": "m", "uid": str(uid)},
            recipients if recipients is not None else ["recipient"],
            "INBOX",
            "7",
            uid,
        )


@pytest.mark.parametrize("count", [0, 1, 5, 13])
def test_delivery_page_walk(tmp_path: Path, count: int) -> None:
    state = State(str(tmp_path / "state.db"))
    seed(state, count)
    expected = state.list_deliveries("m", 100)["deliveries"]
    collected = []
    for offset in range(0, count + 5, 5):
        page = state.list_deliveries("m", 5, offset)
        assert page["hasMore"] == (offset + 5 < count)
        collected.extend(page["deliveries"])
    assert collected == expected
    assert len({row["id"] for row in collected}) == count
    assert state.list_deliveries("m", 5, count)["deliveries"] == []
    assert state.list_deliveries("m", 5, 999)["deliveries"] == []


def test_retry_survives_restart(tmp_path: Path, hook_server: Any, monkeypatch: Any) -> None:
    cfg, state, mb = make_cfg(hook_server.url, tmp_path)
    state.set_cursor("watch", "watch", "m", "INBOX", "7", 1)
    hook_server.status = 500
    assert check(cfg, state, mb, FakeSession([1, 2])) == 2
    assert watch.deliver_pending(cfg, state)
    row = state.list_deliveries("m")["deliveries"][0]
    assert row["status"] == "pending" and row["attempts"] == 1
    first_id = {k.lower(): v for k, v in hook_server.items[0][0].items()}["x-mailboxd-event-id"]
    reopened = State(state.path)
    assert not watch.deliver_pending(cfg, reopened)
    hook_server.status = 200
    monkeypatch.setattr("mailboxd.state.time.time", lambda: row["next_attempt"] + 1)
    assert watch.deliver_pending(cfg, reopened)
    assert reopened.list_deliveries("m")["deliveries"][0]["status"] == "delivered"
    assert len(hook_server.items) == 2
    assert {k.lower(): v for k, v in hook_server.items[1][0].items()}[
        "x-mailboxd-event-id"
    ] == first_id
    assert reopened.get_cursor("watch", "watch", "m", "INBOX") == ("7", 2)


def test_notification_enqueue_is_idempotent_and_checkpoint_monotonic(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.db"))
    seed(state, 3)
    seed(state, 1)
    assert len(State(state.path).list_deliveries("m")["deliveries"]) == 3
    assert state.get_cursor("watch", "watch", "m", "INBOX") == ("7", 3)


def test_enqueue_failure_rolls_back_checkpoint_and_all_recipients(tmp_path: Path) -> None:
    import sqlite3

    state = State(str(tmp_path / "state.db"))
    with pytest.raises(sqlite3.IntegrityError):
        seed(state, recipients=["valid", None])
    assert state.list_deliveries("m")["deliveries"] == []
    assert state.get_cursor("watch", "watch", "m", "INBOX") is None


def test_concurrent_claim_and_stale_worker_cannot_finish(tmp_path: Path, monkeypatch: Any) -> None:
    path = str(tmp_path / "state.db")
    seed(State(path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: State(path).claim_delivery(), range(8)))
    claimed = [row for row in claims if row is not None]
    assert len(claimed) == 1
    first = claimed[0]
    import time

    now = time.time()
    monkeypatch.setattr("mailboxd.state.time.time", lambda: now + DELIVERY_LEASE_SECONDS + 1)
    state = State(path)
    recovered = state.claim_delivery()
    assert recovered is not None and recovered["lease"] != first["lease"]
    state.finish_delivery(first, True)
    assert state.list_deliveries("m")["deliveries"][0]["status"] == "inflight"
    state.finish_delivery(recovered, True)
    assert State(path).list_deliveries("m")["deliveries"][0]["status"] == "delivered"


def test_exhaustion_and_scoped_admin_replay(tmp_path: Path, monkeypatch: Any) -> None:
    import time

    now = [time.time()]
    monkeypatch.setattr("mailboxd.state.time.time", lambda: now[0])
    path = str(tmp_path / "state.db")
    state = State(path)
    seed(state)
    for attempt in range(len(DELIVERY_RETRY_SECONDS) + 1):
        row = state.claim_delivery()
        assert row is not None
        state.finish_delivery(row, False)
        now[0] += DELIVERY_RETRY_SECONDS[min(attempt, len(DELIVERY_RETRY_SECONDS) - 1)] + 1
    row = state.list_deliveries("m")["deliveries"][0]
    assert row["status"] == "dead" and state.claim_delivery() is None
    cfg = Config.model_validate(
        {
            "state": {"path": path},
            "mailboxes": [
                {"name": name, "imap": {"host": "h", "username": "u", "password": "p"}}
                for name in ("m", "hidden")
            ],
            "auth": {
                "tokens": [
                    {
                        "name": "admin",
                        "token": "EXAMPLE-DO-NOT-USE-admin",
                        "scopes": ["admin"],
                        "mailboxes": ["m"],
                    },
                    {"name": "reader", "token": "EXAMPLE-DO-NOT-USE-reader", "scopes": ["read"]},
                ]
            },
        }
    )
    with TestClient(create_app(cfg)) as client:
        headers = {"Authorization": "Bearer EXAMPLE-DO-NOT-USE-admin"}
        base = "/mailboxes/m/webhook-deliveries"
        assert client.get(base, headers=headers).json()["deliveries"][0]["status"] == "dead"
        assert (
            client.get("/mailboxes/hidden/webhook-deliveries", headers=headers).status_code == 404
        )
        assert client.post(f"{base}/{row['id']}/replay", headers=headers).status_code == 200
        reader = {"Authorization": "Bearer EXAMPLE-DO-NOT-USE-reader"}
        assert client.post(f"{base}/{row['id']}/replay", headers=reader).status_code == 403
        for query in ("limit=0", "limit=501", "offset=-1", "offset=999999999999999999999999999"):
            assert client.get(base + "?" + query, headers=headers).status_code == 422
    reopened = State(path)
    assert reopened.list_deliveries("m")["deliveries"][0]["status"] == "pending"
    assert reopened.claim_delivery()["event_id"] == "event-1"


def test_scoped_admin_cannot_read_other_mailbox_audit(tmp_path: Path, monkeypatch: Any) -> None:
    svc, _, _ = make_service(monkeypatch, tmp_path)
    svc.state.audit("a", "move", "main")
    svc.state.audit("b", "move", "ro")
    entries = svc.audit_log(principal("admin", mailboxes=["main"]))["entries"]
    assert [row["mailbox"] for row in entries] == ["main"]


def test_permanent_delete_without_uidplus_preserves_all_trash(start: Any, tmp_path: Path) -> None:
    sink, imap = start(folders={"Trash": {}})
    # Seed through IMAP APPEND so fixture layout is not part of the assertion.
    from mailboxd.imap_client import session

    with session(imap) as conn:
        conn.append("Trash", make_raw(), [])
        conn.append("Trash", make_raw("keep"), [r"\Deleted"])
    before = sink.uids("Trash")
    cfg = Config(mailboxes=[MailboxConfig(name="m", imap=imap)])
    svc = Service(cfg, State(str(tmp_path / "state.db")))
    with pytest.raises(Conflict):
        svc.delete(principal("delete"), "m", [str(before[0])], "Trash", True)
    assert sink.uids("Trash") == before
    assert sink.plain_expunges == []


def test_webhook_redirect_is_not_followed(hook_server: Any) -> None:
    from mailboxd.config import WebhookConfig

    hook_server.status = 302
    hook = WebhookConfig(url=hook_server.url, secret="EXAMPLE-DO-NOT-USE")
    assert watch.deliver(hook, b'{"event_id":"redirect-test"}') is False
    assert len(hook_server.items) == 1 and hook_server.gets == 0


def test_watch_process_wires_imap_discovery_to_durable_delivery(
    start: Any,
    hook_server: Any,
    tmp_path: Path,
) -> None:
    import threading
    import time

    from mailboxd.config import WatchConfig, WebhookConfig

    _, imap = start({1: make_raw("old"), 2: make_raw("new")})
    cfg = Config(
        mailboxes=[MailboxConfig(name="m", imap=imap)],
        watch=WatchConfig(
            webhooks=[WebhookConfig(url=hook_server.url, secret="EXAMPLE-DO-NOT-USE")]
        ),
    )
    state = State(str(tmp_path / "state.db"))
    state.set_cursor("watch", "watch", "m", "INBOX", "1", 1)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(watch.run, cfg, state, stop)
        try:
            deadline = time.monotonic() + 5
            while not hook_server.items and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(hook_server.items) == 1
        finally:
            stop.set()
        assert future.result(timeout=15) == 0
    assert State(state.path).list_deliveries("m")["deliveries"][0]["status"] == "delivered"


def test_draft_cleanup_failure_cannot_duplicate_delivery(tmp_path: Path, monkeypatch: Any) -> None:
    from mailboxd.imap_client import ImapError
    from mailboxd.schemas import ComposeRequest

    svc, _, smtp = make_service(monkeypatch, tmp_path)
    request = ComposeRequest(to=["recipient@example.org"], subject="test", body_text="test")
    uid = svc.create_draft(principal("draft"), "main", request)["uid"]

    def fail_cleanup(*args: Any) -> None:
        raise ImapError("cleanup unavailable")

    monkeypatch.setattr(svc, "_remove", fail_cleanup)
    result = svc.send_draft(principal("send"), "main", uid)
    assert result["warnings"] == ["draft_not_removed"]
    with pytest.raises(Conflict):
        svc.send_draft(principal("send"), "main", uid)
    assert len(smtp.sent) == 1


@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_first_use_captures_every_writer(tmp_path: Path, iteration: int) -> None:
    path = str(tmp_path / f"concurrent-{iteration}.db")

    def write(number: int) -> None:
        State(path).audit(f"actor-{number}", "test", "m")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    assert {row["token"] for row in State(path).list_audit()} == {f"actor-{n}" for n in range(8)}


def test_watcher_health_persists_expires_and_cleans_up(tmp_path: Path, monkeypatch: Any) -> None:
    import time

    from mailboxd.cli import main

    path = str(tmp_path / "state.db")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("state:\n  path: " + path + "\n")
    state = State(path)
    assert main(["watch-health", "--config", str(cfg)]) == 1
    state.watcher_heartbeat("worker")
    assert main(["watch-health", "--config", str(cfg)]) == 0
    now = time.time()
    monkeypatch.setattr("mailboxd.state.time.time", lambda: now + 91)
    assert main(["watch-health", "--config", str(cfg)]) == 1
    State(path).watcher_heartbeat("worker")
    assert state.watcher_healthy()
    state.remove_watcher("worker")
    assert not State(path).watcher_healthy()
