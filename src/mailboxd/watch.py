"""New-mail notifications: `mailboxd watch` posts signed webhooks.

One thread per IMAP mailbox. It waits with IMAP IDLE when the server has it
(imaplib in Python 3.12 has no idle(), so the protocol is spoken on the
connection's low-level send/readline), otherwise it polls. A cursor
(UIDVALIDITY + last UID) is kept in the State db so restarts neither miss
nor repeat mail. Webhook payloads carry headers and verdicts, never bodies.
"""

from __future__ import annotations

import hashlib
import hmac
import imaplib
import json
import logging
import select
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from . import imap_client, mime
from .config import Config, MailboxConfig, WebhookConfig
from .imap_client import ImapError, Session
from .state import State

log = logging.getLogger(__name__)

CURSOR_ACTOR = "watch"
CURSOR_NAME = "watch"
IDLE_REISSUE_SECONDS = 25 * 60
MAX_BACKOFF = 300.0
WEBHOOK_TIMEOUT = 10.0
WEBHOOK_ATTEMPTS = 1
DELIVERY_POLL_SECONDS = 1
HEARTBEAT_SECONDS = 10


# ── webhooks ─────────────────────────────────────────────────────────────


def sign(secret: str, body: bytes, timestamp: str) -> str:
    signed = timestamp.encode("ascii") + b"." + body
    return "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def recipient_key(hook: WebhookConfig) -> str:
    return hashlib.sha256(hook.url.encode()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def webhooks_for(cfg: Config, mailbox: str) -> list[WebhookConfig]:
    return [w for w in cfg.watch.webhooks if w.mailboxes == "*" or mailbox in w.mailboxes]


def deliver(
    hook: WebhookConfig,
    body: bytes,
    attempts: int = WEBHOOK_ATTEMPTS,
    timeout: float = WEBHOOK_TIMEOUT,
    sleep: Callable[[float], Any] = time.sleep,
) -> bool:
    """POST `body` signed with the webhook secret; retry with backoff."""
    opener = urllib.request.build_opener(_NoRedirect())
    for attempt in range(attempts):
        timestamp = str(int(time.time()))
        req = urllib.request.Request(
            hook.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Mailboxd-Signature": sign(hook.secret, body, timestamp),
                "X-Mailboxd-Timestamp": timestamp,
                "X-Mailboxd-Event-ID": str(json.loads(body)["event_id"]),
            },
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning("webhook rejected", extra={"status": resp.status})
        except (urllib.error.URLError, OSError) as e:
            log.warning("webhook failed", extra={"error_type": type(e).__name__})
        if attempt + 1 < attempts:
            sleep(2.0**attempt)
    return False


# ── events ───────────────────────────────────────────────────────────────


def _auth_summary(auth: dict[str, Any]) -> dict[str, Any]:
    dkim = auth.get("dkim") or []
    dkim_result = None
    if dkim:
        dkim_result = "pass" if any(d["result"] == "pass" for d in dkim) else dkim[0]["result"]
    return {
        "spf": (auth.get("spf") or {}).get("result"),
        "dkim": dkim_result,
        "dmarc": (auth.get("dmarc") or {}).get("result"),
        "trusted": bool(auth.get("trusted")),
    }


def build_event(cfg: Config, mb: MailboxConfig, folder: str, uid: int, msg: Any) -> dict[str, Any]:
    auth = mime.parse_authentication_results(msg, cfg.auth.trusted_authserv_id)
    return {
        "event": "message.new",
        "mailbox": mb.name,
        "folder": folder,
        "uid": str(uid),
        "from": mime.decode(msg.get("From")),
        "to": mime.decode(msg.get("To")),
        "subject": mime.decode(msg.get("Subject")),
        "date": mime.decode(msg.get("Date")),
        "message_id": mime.decode(msg.get("Message-ID")),
        "authentication": _auth_summary(auth),
        "signals": mime.security_signals(msg, auth),
    }


def _current(s: Session, folder: str) -> tuple[str, int]:
    uidvalidity = s.select(folder, readonly=True)
    uids = s.search("ALL")
    return uidvalidity, (uids[-1] if uids else 0)


def process_new(
    cfg: Config,
    state: State,
    mb: MailboxConfig,
    folder: str,
    last_uid: int,
    s: Session,
    uidvalidity: str = "",
) -> int:
    """Notify every UID above `last_uid` (folder already selected).

    Advances the stored cursor after each message and returns the new last UID.
    """
    hooks = webhooks_for(cfg, mb.name)
    # UID n:* always matches the highest message even when it is below n.
    new = [u for u in s.search(f"UID {last_uid + 1}:*") if u > last_uid]
    for uid in new:
        event = build_event(cfg, mb, folder, uid, s.fetch_message(str(uid), header_only=True))
        identity = json.dumps([mb.name, folder, uidvalidity, uid], separators=(",", ":"))
        event["event_id"] = hashlib.sha256(identity.encode()).hexdigest()
        event["uidvalidity"] = uidvalidity
        state.enqueue_notifications(
            event, [recipient_key(hook) for hook in hooks], folder, uidvalidity, uid
        )
        last_uid = uid
    return last_uid


def deliver_pending(cfg: Config, state: State) -> bool:
    """Attempt one leased delivery. Receivers deduplicate by stable event ID."""
    delivery = state.claim_delivery()
    if delivery is None:
        return False
    hook = next(
        (
            h
            for h in cfg.watch.webhooks
            if recipient_key(h) == delivery["recipient"]
            and (h.mailboxes == "*" or delivery["mailbox"] in h.mailboxes)
        ),
        None,
    )
    success = hook is not None and deliver(hook, delivery["payload"].encode(), attempts=1)
    state.finish_delivery(delivery, success)
    return True


def _delivery_loop(cfg: Config, state: State, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if deliver_pending(cfg, state):
                continue
        except sqlite3.Error as err:
            log.error("webhook queue unavailable", extra={"error_type": type(err).__name__})
        stop.wait(DELIVERY_POLL_SECONDS)


def check_folder(cfg: Config, state: State, mb: MailboxConfig, s: Session, folder: str) -> int:
    """Select `folder`, reconcile the cursor and notify new mail. Returns the last UID."""
    uidvalidity, top = _current(s, folder)
    cur = state.get_cursor(CURSOR_ACTOR, CURSOR_NAME, mb.name, folder)
    if cur is None or cur[0] != uidvalidity:
        # First run (or UIDVALIDITY reset): start from now, never replay history.
        state.set_cursor(CURSOR_ACTOR, CURSOR_NAME, mb.name, folder, uidvalidity, top)
        return top
    return process_new(cfg, state, mb, folder, cur[1], s, uidvalidity)


# ── IDLE ─────────────────────────────────────────────────────────────────


def _readable(conn: Any, timeout: float) -> bool:
    """True when a line can be read now: already buffered, or the socket is ready."""
    peek = getattr(conn.file, "peek", None)
    if peek is not None:
        previous = conn.sock.gettimeout()
        conn.sock.setblocking(False)
        try:
            if peek(1):
                return True
        except (OSError, ValueError):
            pass
        finally:
            conn.sock.settimeout(previous)
    ready, _, _ = select.select([conn.sock], [], [], timeout)
    return bool(ready)


def idle_wait(
    conn: Any,
    timeout: float,
    should_stop: Callable[[], bool] = lambda: False,
    slice_seconds: float = 1.0,
) -> bool:
    """Block in IMAP IDLE up to `timeout`s. True when the server reported new mail.

    Uses the connection's raw send/readline: tagged IDLE, wait for '+', watch
    untagged lines with select(), then DONE and drain to the tagged OK.
    """
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    first = conn.readline()
    if not first.startswith(b"+"):
        raise ImapError(f"IDLE refused: {first!r}")
    woke = False
    deadline = time.monotonic() + timeout
    while not woke and not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not _readable(conn, min(slice_seconds, remaining)):
            continue
        line = conn.readline().upper()
        if line.startswith(b"* ") and not line.startswith(b"* OK"):
            woke = any(k in line for k in (b"EXISTS", b"EXPUNGE", b"RECENT", b"FETCH"))
    conn.send(b"DONE\r\n")
    while True:
        line = conn.readline()
        if line.startswith(tag):
            break
    return woke


# ── per-mailbox loop ─────────────────────────────────────────────────────


def _wait(s: Session, cfg: Config, stop: threading.Event) -> None:
    if s.has("IDLE"):
        idle_wait(s.conn, IDLE_REISSUE_SECONDS, stop.is_set)
    else:
        stop.wait(cfg.watch.poll_seconds)


def watch_mailbox(cfg: Config, state: State, mb: MailboxConfig, stop: threading.Event) -> None:
    """Run until `stop` is set; reconnect with exponential backoff on errors."""
    imap = mb.imap
    if imap is None:
        return
    folder = cfg.watch.folder
    backoff = 1.0
    while not stop.is_set():
        try:
            with imap_client.session(imap) as s:
                backoff = 1.0
                log.info("watching mailbox", extra={"mailbox": mb.name, "idle": s.has("IDLE")})
                while not stop.is_set():
                    check_folder(cfg, state, mb, s, folder)
                    if stop.is_set():
                        break
                    _wait(s, cfg, stop)
        except (ImapError, OSError, imaplib.IMAP4.error) as e:
            log.warning(
                "watch connection failed",
                extra={
                    "mailbox": mb.name,
                    "error_type": type(e).__name__,
                    "retry_seconds": backoff,
                },
            )
            stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


def watched_mailboxes(cfg: Config) -> list[MailboxConfig]:
    return [m for m in cfg.mailboxes if m.imap is not None and webhooks_for(cfg, m.name)]


def run(cfg: Config, state: State | None = None, stop: threading.Event | None = None) -> int:
    """Start one thread per watched mailbox and block until `stop` is set."""
    state = state or State(cfg.state.path)
    stop = stop or threading.Event()
    targets = watched_mailboxes(cfg)
    if not targets:
        log.error("nothing to watch: need an IMAP mailbox and a matching watch.webhooks entry")
        return 2
    threads = [
        threading.Thread(
            target=watch_mailbox, args=(cfg, state, mb, stop), name=f"watch-{mb.name}", daemon=True
        )
        for mb in targets
    ]
    threads.append(
        threading.Thread(
            target=_delivery_loop, args=(cfg, state, stop), name="webhook-delivery", daemon=True
        )
    )
    for t in threads:
        t.start()
    worker = uuid.uuid4().hex
    status = 0
    next_heartbeat = 0.0
    try:
        while not stop.wait(0.5):
            if not all(thread.is_alive() for thread in threads):
                log.error("watch worker stopped unexpectedly")
                status = 1
                stop.set()
                break
            if time.monotonic() >= next_heartbeat:
                state.watcher_heartbeat(worker)
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
        state.remove_watcher(worker)
    return status
