"""Storage layer. SQLite by default (zero-config, satisfies `docker compose up`
with no external service); the connection string is the only thing that changes
to move to Postgres. Chosen for the take-home because the acceptance gate requires
the API to come up with no manual steps — an embedded DB removes a failure mode.

A raised StorageUnavailable maps to HTTP 503 with a structured body upstream,
satisfying the graceful-degradation requirement (Part C)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.models import CanonicalEvent

DB_PATH = os.environ.get("STORE_DB_PATH", "/data/store_intel.db")


class StorageUnavailable(RuntimeError):
    """Raised when the backing store cannot be reached -> HTTP 503."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL,
    camera_id     TEXT,
    visitor_id    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    ts            TEXT NOT NULL,          -- ISO-8601 UTC
    zone_id       TEXT,
    zone_name     TEXT,
    is_revenue_zone INTEGER,
    dwell_ms      INTEGER,
    is_staff      INTEGER NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL DEFAULT 1.0,
    metadata      TEXT,                   -- JSON blob
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_store_ts ON events(store_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_store_visitor ON events(store_id, visitor_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(store_id, event_type);

CREATE TABLE IF NOT EXISTS pos_transactions (
    basket_key   TEXT PRIMARY KEY,        -- store|date|time
    store_id     TEXT NOT NULL,
    ts           TEXT NOT NULL,
    basket_value REAL NOT NULL,
    item_count   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pos_store_ts ON pos_transactions(store_id, ts);
"""


class Storage:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self._init_schema()

    @contextmanager
    def _conn(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        except sqlite3.Error as e:
            raise StorageUnavailable(str(e)) from e
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ---- ingestion (idempotent) ----
    def insert_events(self, events: Iterable[CanonicalEvent]) -> tuple[int, int]:
        """Returns (accepted, duplicates). INSERT OR IGNORE makes a repeat
        payload a no-op on the second call -> idempotent by event_id."""
        accepted = duplicates = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            for ev in events:
                cur = c.execute(
                    """INSERT OR IGNORE INTO events
                       (event_id,store_id,camera_id,visitor_id,event_type,ts,zone_id,
                        zone_name,is_revenue_zone,dwell_ms,is_staff,confidence,metadata,ingested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ev.event_id, ev.store_id, ev.camera_id, ev.visitor_id,
                        ev.event_type.value, ev.timestamp.isoformat(), ev.zone_id,
                        ev.zone_name,
                        None if ev.is_revenue_zone is None else int(ev.is_revenue_zone),
                        ev.dwell_ms, int(ev.is_staff), ev.confidence,
                        json.dumps(ev.metadata.model_dump(exclude_none=True)), now,
                    ),
                )
                if cur.rowcount == 1:
                    accepted += 1
                else:
                    duplicates += 1
        return accepted, duplicates

    # ---- queries ----
    def events_for_store(self, store_id: str, since: Optional[datetime] = None,
                         include_staff: bool = False) -> list[dict]:
        q = "SELECT * FROM events WHERE store_id = ?"
        args: list = [store_id]
        if not include_staff:
            q += " AND is_staff = 0"
        if since:
            q += " AND ts >= ?"
            args.append(since.isoformat())
        q += " ORDER BY ts ASC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def last_event_ts_per_store(self) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT store_id, MAX(ts) AS last_ts FROM events GROUP BY store_id"
            ).fetchall()
        return {r["store_id"]: r["last_ts"] for r in rows}

    def pos_baskets(self, store_id: str, since: Optional[datetime] = None) -> list[dict]:
        q = "SELECT * FROM pos_transactions WHERE store_id = ?"
        args: list = [store_id]
        if since:
            q += " AND ts >= ?"
            args.append(since.isoformat())
        q += " ORDER BY ts ASC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def upsert_basket(self, basket_key: str, store_id: str, ts: str,
                     basket_value: float, item_count: int) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO pos_transactions (basket_key,store_id,ts,basket_value,item_count)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(basket_key) DO UPDATE SET
                     basket_value=excluded.basket_value, item_count=excluded.item_count""",
                (basket_key, store_id, ts, basket_value, item_count),
            )

    def health_check(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1")
            return True
        except StorageUnavailable:
            return False


# module-level singleton wired in main.py
_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
