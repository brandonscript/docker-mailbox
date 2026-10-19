"""Small SQLite store for the bits that must survive restarts and be shared
between uvicorn workers: the audit log, the send rate limiter and the
"new since last read" checkpoints.

One short-lived connection per call (WAL mode, busy timeout) keeps it safe
across threads and processes without a pool. Message bodies are never stored.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import Conflict, NotFound, RateLimited

_SCHEMA = [
    # v1
    """
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        token TEXT NOT NULL,
        action TEXT NOT NULL,
        mailbox TEXT,
        folder TEXT,
        uids TEXT,
        outcome TEXT NOT NULL,
        details TEXT
    );
    CREATE INDEX audit_ts ON audit (ts);
    CREATE TABLE send_log (
        ts REAL NOT NULL,
        mailbox TEXT NOT NULL,
        token TEXT NOT NULL
    );
    CREATE INDEX send_log_mailbox ON send_log (mailbox, ts);
    CREATE INDEX send_log_token ON send_log (token, ts);
    CREATE TABLE cursors (
        token TEXT NOT NULL,
        name TEXT NOT NULL,
        mailbox TEXT NOT NULL,
        folder TEXT NOT NULL,
        uidvalidity TEXT NOT NULL,
        last_uid INTEGER NOT NULL,
        updated REAL NOT NULL,
        PRIMARY KEY (token, name, mailbox, folder)
    );
    """,
    """
    CREATE TABLE deliveries (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        recipient TEXT NOT NULL,
        mailbox TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL,
        lease_until REAL NOT NULL DEFAULT 0,
        lease TEXT,
        UNIQUE(event_id, recipient)
    );
    CREATE INDEX deliveries_due ON deliveries(status, next_attempt);
    CREATE TABLE draft_sends (
        mailbox TEXT NOT NULL,
        folder TEXT NOT NULL,
        uidvalidity TEXT NOT NULL,
        uid TEXT NOT NULL,
        PRIMARY KEY(mailbox, folder, uidvalidity, uid)
    );
    """,
    """
    CREATE TABLE watchers (
        id TEXT PRIMARY KEY,
        updated REAL NOT NULL
    );
    """,
]

WINDOW_SECONDS = 3600
INITIALIZATION_TIMEOUT_SECONDS = 10
INITIALIZATION_RETRY_SECONDS = 0.05
DELIVERY_LEASE_SECONDS = 60
DELIVERY_RETRY_SECONDS = (30, 60, 300, 1800, 7200, 43200)


class State:
    def __init__(self, path: str) -> None:
        self.path = path
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self._ensure()
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            yield conn
        finally:
            conn.close()

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                Path(self.path).chmod(0o600)
                deadline = time.monotonic() + INITIALIZATION_TIMEOUT_SECONDS
                while True:
                    try:
                        conn.execute("PRAGMA journal_mode = WAL")
                        break
                    except sqlite3.OperationalError as err:
                        code = getattr(err, "sqlite_errorcode", 0) & 0xFF
                        if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                            raise
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(INITIALIZATION_RETRY_SECONDS)
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > len(_SCHEMA):
                    raise RuntimeError("state database was created by a newer mailboxd version")
                try:
                    for i, ddl in enumerate(_SCHEMA[version:], start=version + 1):
                        for stmt in ddl.split(";"):
                            if stmt.strip():
                                conn.execute(stmt)
                        conn.execute(f"PRAGMA user_version = {i}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
            self._ready = True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def watcher_heartbeat(self, worker: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM watchers WHERE updated<?", (time.time() - 90,))
            conn.execute(
                "INSERT INTO watchers(id,updated) VALUES(?,?)"
                " ON CONFLICT(id) DO UPDATE SET updated=excluded.updated",
                (worker, time.time()),
            )

    def remove_watcher(self, worker: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM watchers WHERE id=?", (worker,))

    def watcher_healthy(self, maximum_age: int = 90) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM watchers WHERE updated>=? LIMIT 1", (time.time() - maximum_age,)
            ).fetchone()
        return row is not None

    def enqueue_notifications(
        self,
        event: dict[str, Any],
        recipients: list[str],
        folder: str,
        uidvalidity: str,
        uid: int,
    ) -> None:
        """Commit recipient deliveries and the discovery checkpoint together."""
        now = time.time()
        payload = json.dumps(event, ensure_ascii=False)
        with self._transaction() as conn:
            for recipient in recipients:
                conn.execute(
                    "INSERT INTO deliveries(id,event_id,recipient,mailbox,payload,next_attempt)"
                    " VALUES(?,?,?,?,?,?) ON CONFLICT(event_id,recipient) DO NOTHING",
                    (
                        uuid.uuid4().hex,
                        event["event_id"],
                        recipient,
                        event["mailbox"],
                        payload,
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO cursors(token,name,mailbox,folder,uidvalidity,last_uid,updated)"
                " VALUES('watch','watch',?,?,?,?,?)"
                " ON CONFLICT(token,name,mailbox,folder) DO UPDATE SET"
                " last_uid=CASE WHEN cursors.uidvalidity=excluded.uidvalidity"
                " THEN MAX(cursors.last_uid,excluded.last_uid) ELSE excluded.last_uid END,"
                " uidvalidity=excluded.uidvalidity,updated=excluded.updated",
                (event["mailbox"], folder, uidvalidity, uid, now),
            )

    def claim_delivery(self) -> dict[str, Any] | None:
        now = time.time()
        with self._transaction() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM deliveries WHERE (status='pending' AND next_attempt<=?)"
                " OR (status='inflight' AND lease_until<=?) ORDER BY next_attempt,id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            delivery = dict(row)
            lease = uuid.uuid4().hex
            conn.execute(
                "UPDATE deliveries SET status='inflight',lease=?,lease_until=?,"
                " attempts=attempts+1 WHERE id=?",
                (lease, now + DELIVERY_LEASE_SECONDS, row["id"]),
            )
            delivery.update(lease=lease, attempts=row["attempts"] + 1)
            return delivery

    def finish_delivery(self, delivery: dict[str, Any], success: bool) -> None:
        attempt = delivery["attempts"]
        status = "delivered" if success else "pending"
        delay = 0
        if not success:
            if attempt > len(DELIVERY_RETRY_SECONDS):
                status = "dead"
            else:
                delay = DELIVERY_RETRY_SECONDS[attempt - 1]
        with self._conn() as conn:
            conn.execute(
                "UPDATE deliveries SET status=?,next_attempt=?,lease=NULL,lease_until=0"
                " WHERE id=? AND lease=? AND status='inflight'",
                (status, time.time() + delay, delivery["id"], delivery["lease"]),
            )

    def list_deliveries(self, mailbox: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,event_id,status,attempts,next_attempt FROM deliveries"
                " WHERE mailbox=? ORDER BY next_attempt,id LIMIT ? OFFSET ?",
                (mailbox, limit + 1, offset),
            ).fetchall()
        return {
            "deliveries": [dict(row) for row in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "hasMore": len(rows) > limit,
        }

    def replay_delivery(self, mailbox: str, delivery_id: str) -> None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status FROM deliveries WHERE mailbox=? AND id=?", (mailbox, delivery_id)
            ).fetchone()
            if row is None:
                raise NotFound("unknown webhook delivery")
            if row[0] == "inflight":
                raise Conflict("webhook delivery is currently in progress")
            conn.execute(
                "UPDATE deliveries SET status='pending',attempts=0,next_attempt=?,"
                " lease=NULL,lease_until=0 WHERE mailbox=? AND id=?",
                (time.time(), mailbox, delivery_id),
            )

    def claim_draft(self, mailbox: str, folder: str, uidvalidity: str, uid: str) -> None:
        """Do not automatically resend a draft after an ambiguous SMTP outcome."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO draft_sends(mailbox,folder,uidvalidity,uid) VALUES(?,?,?,?)",
                    (mailbox, folder, uidvalidity, uid),
                )
        except sqlite3.IntegrityError as err:
            raise Conflict("draft already sent or delivery outcome requires manual review") from err

    # ── Audit ────────────────────────────────────────────────────────────

    def audit(
        self,
        token: str,
        action: str,
        mailbox: str | None = None,
        folder: str | None = None,
        uids: list[str] | str | None = None,
        outcome: str = "ok",
        **details: Any,
    ) -> None:
        if isinstance(uids, list):
            uids = ",".join(uids)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit (ts, token, action, mailbox, folder, uids, outcome, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    token,
                    action,
                    mailbox,
                    folder,
                    uids,
                    outcome,
                    json.dumps(details, default=str, ensure_ascii=False) if details else None,
                ),
            )

    def list_audit(
        self,
        since: float | None = None,
        action: str | None = None,
        token: str | None = None,
        mailbox: str | None = None,
        limit: int = 100,
        allowed_mailboxes: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        q = "SELECT id, ts, token, action, mailbox, folder, uids, outcome, details FROM audit"
        where: list[str] = []
        args: list[Any] = []
        if allowed_mailboxes is not None:
            if not allowed_mailboxes:
                return []
            where.append("mailbox IN (" + ",".join("?" for _ in allowed_mailboxes) + ")")
            args.extend(sorted(allowed_mailboxes))
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if action:
            where.append("action = ?")
            args.append(action)
        if token:
            where.append("token = ?")
            args.append(token)
        if mailbox:
            where.append("mailbox = ?")
            args.append(mailbox)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "ts": r[1],
                    "token": r[2],
                    "action": r[3],
                    "mailbox": r[4],
                    "folder": r[5],
                    "uids": r[6],
                    "outcome": r[7],
                    "details": json.loads(r[8]) if r[8] else None,
                }
            )
        return out

    # ── Send rate limit ──────────────────────────────────────────────────

    def check_and_record_send(
        self, mailbox: str, token: str, mailbox_limit: int, token_limit: int
    ) -> None:
        """Atomically refuse (RateLimited) or record one send. A limit of 0 is unlimited."""
        now = time.time()
        floor = now - WINDOW_SECONDS
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM send_log WHERE ts < ?", (floor,))
                for column, query, value, limit in (
                    (
                        "mailbox",
                        "SELECT COUNT(*), MIN(ts) FROM send_log WHERE mailbox = ? AND ts >= ?",
                        mailbox,
                        mailbox_limit,
                    ),
                    (
                        "token",
                        "SELECT COUNT(*), MIN(ts) FROM send_log WHERE token = ? AND ts >= ?",
                        token,
                        token_limit,
                    ),
                ):
                    if limit <= 0:
                        continue
                    count, oldest = conn.execute(query, (value, floor)).fetchone()
                    if count >= limit:
                        retry = int(max(1, (oldest or now) + WINDOW_SECONDS - now))
                        raise RateLimited(
                            f"send limit reached for {column} {value!r}: {limit} per hour",
                            retry_after=retry,
                        )
                conn.execute(
                    "INSERT INTO send_log (ts, mailbox, token) VALUES (?, ?, ?)",
                    (now, mailbox, token),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ── Checkpoints ──────────────────────────────────────────────────────

    def get_cursor(
        self, token: str, name: str, mailbox: str, folder: str
    ) -> tuple[str, int] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT uidvalidity, last_uid FROM cursors"
                " WHERE token = ? AND name = ? AND mailbox = ? AND folder = ?",
                (token, name, mailbox, folder),
            ).fetchone()
        return (str(row[0]), int(row[1])) if row else None

    def set_cursor(
        self, token: str, name: str, mailbox: str, folder: str, uidvalidity: str, last_uid: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cursors (token, name, mailbox, folder, uidvalidity, last_uid, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (token, name, mailbox, folder) DO UPDATE SET"
                " uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid,"
                " updated = excluded.updated",
                (token, name, mailbox, folder, uidvalidity, last_uid, time.time()),
            )
