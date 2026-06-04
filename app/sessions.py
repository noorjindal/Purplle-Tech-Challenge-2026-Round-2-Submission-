"""Sessionization: the funnel and metrics endpoints operate on SESSIONS, not raw
events (challenge: 'Session is the unit, not raw events. Re-entries must not
double-count a visitor.').

A session = one visit. Re-entry (same visitor_id appearing again after an EXIT)
is treated as the SAME unique visitor for the conversion denominator, but its
in-store activity is merged so dwell/zone stats aren't double counted."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.models import ENTRY_TYPES, EXIT_TYPES


@dataclass
class Session:
    visitor_id: str
    store_id: str
    start: datetime
    end: Optional[datetime] = None
    zones_visited: set[str] = field(default_factory=set)
    revenue_zones_visited: set[str] = field(default_factory=set)
    reached_billing: bool = False
    abandoned_queue: bool = False
    zone_dwell_ms: dict[str, int] = field(default_factory=dict)
    reentry_count: int = 0
    converted: bool = False
    is_staff: bool = False

    @property
    def dwell_total_ms(self) -> int:
        return sum(self.zone_dwell_ms.values())


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def build_sessions(events: list[dict]) -> list[Session]:
    """events: rows from storage (already staff-filtered if desired), ASC by ts.
    Groups by visitor_id; a REENTRY/ENTRY after an EXIT extends the same logical
    visitor rather than minting a new unique visitor."""
    by_visitor: dict[str, Session] = {}
    for e in events:
        vid = e["visitor_id"]
        etype = e["event_type"]
        ts = _parse(e["ts"])
        s = by_visitor.get(vid)
        if s is None:
            s = Session(visitor_id=vid, store_id=e["store_id"], start=ts,
                        is_staff=bool(e["is_staff"]))
            by_visitor[vid] = s

        if etype in {t.value for t in ENTRY_TYPES}:
            if etype == "reentry":
                s.reentry_count += 1
            s.start = min(s.start, ts)
        elif etype in {t.value for t in EXIT_TYPES}:
            s.end = ts if s.end is None else max(s.end, ts)
        elif etype in {"zone_entered", "zone_dwell"}:
            if e.get("zone_id"):
                s.zones_visited.add(e["zone_id"])
                if e.get("is_revenue_zone"):
                    s.revenue_zones_visited.add(e["zone_id"])
                if e.get("dwell_ms"):
                    s.zone_dwell_ms[e["zone_id"]] = s.zone_dwell_ms.get(e["zone_id"], 0) + int(e["dwell_ms"])
        elif etype in {"queue_joined", "queue_completed"}:
            s.reached_billing = True
        elif etype == "queue_abandoned":
            s.reached_billing = True
            s.abandoned_queue = True
    return list(by_visitor.values())


def correlate_conversions(sessions: list[Session], baskets: list[dict],
                          window_min: int = 5) -> None:
    """A visitor who reached billing within `window_min` BEFORE a POS basket
    timestamp counts as converted for that session. Mutates sessions in place.

    No customer_id exists in POS data, so correlation is time-window + store only.
    We match each basket to at most one unconverted billing session to avoid
    inflating conversion when one basket could greedily claim many visitors."""
    window = timedelta(minutes=window_min)
    billing_sessions = sorted(
        [s for s in sessions if s.reached_billing and not s.is_staff],
        key=lambda s: s.end or s.start,
    )
    for b in sorted(baskets, key=lambda x: x["ts"]):
        bts = _parse(b["ts"])
        best: Optional[Session] = None
        for s in billing_sessions:
            ref = s.end or s.start
            if bts - window <= ref <= bts and not s.converted:
                if best is None or (ref > (best.end or best.start)):
                    best = s
        if best is not None:
            best.converted = True
