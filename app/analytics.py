"""Analytics computations for the /metrics, /funnel, /heatmap, /anomalies endpoints.
All operate on sessions (staff already excluded by the caller) and handle the
zero-traffic / zero-purchase edge cases without crashing or returning null."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean

from app.sessions import Session, build_sessions, correlate_conversions
from app.storage import Storage

CONFIDENCE_MIN_SESSIONS = 20  # heatmap data_confidence flag threshold


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _customer_sessions(storage: Storage, store_id: str, since=None) -> list[Session]:
    rows = storage.events_for_store(store_id, since=since, include_staff=False)
    sessions = build_sessions(rows)
    baskets = storage.pos_baskets(store_id, since=since)
    correlate_conversions(sessions, baskets)
    return sessions


def compute_metrics(storage: Storage, store_id: str, since=None) -> dict:
    sessions = _customer_sessions(storage, store_id, since)
    unique_visitors = len(sessions)
    converted = sum(1 for s in sessions if s.converted)
    conversion_rate = (converted / unique_visitors) if unique_visitors else 0.0

    # avg dwell per zone (ms -> seconds)
    zone_dwells: dict[str, list[int]] = {}
    for s in sessions:
        for z, ms in s.zone_dwell_ms.items():
            zone_dwells.setdefault(z, []).append(ms)
    avg_dwell_per_zone = {z: round(mean(v) / 1000, 1) for z, v in zone_dwells.items()}

    billing = [s for s in sessions if s.reached_billing]
    abandoned = sum(1 for s in billing if s.abandoned_queue and not s.converted)
    abandonment_rate = (abandoned / len(billing)) if billing else 0.0

    # current queue depth = billing sessions in last 5 min that haven't converted/abandoned
    cutoff = _now() - timedelta(minutes=5)
    queue_depth = sum(
        1 for s in billing
        if (s.end or s.start) >= cutoff and not s.converted and not s.abandoned_queue
    )

    return {
        "store_id": store_id,
        "unique_visitors": unique_visitors,
        "converted_visitors": converted,
        "conversion_rate": round(conversion_rate, 4),
        "avg_dwell_per_zone_sec": avg_dwell_per_zone,
        "current_queue_depth": queue_depth,
        "abandonment_rate": round(abandonment_rate, 4),
        "computed_at": _now().isoformat(),
        "zero_traffic": unique_visitors == 0,
    }


def compute_funnel(storage: Storage, store_id: str, since=None) -> dict:
    sessions = _customer_sessions(storage, store_id, since)
    entered = len(sessions)
    visited_zone = sum(1 for s in sessions if s.revenue_zones_visited)
    reached_billing = sum(1 for s in sessions if s.reached_billing)
    purchased = sum(1 for s in sessions if s.converted)

    def drop(a: int, b: int) -> float:
        return round((1 - b / a) * 100, 1) if a else 0.0

    return {
        "store_id": store_id,
        "unit": "session",
        "stages": [
            {"stage": "entry", "count": entered, "drop_off_pct": 0.0},
            {"stage": "zone_visit", "count": visited_zone, "drop_off_pct": drop(entered, visited_zone)},
            {"stage": "billing_queue", "count": reached_billing, "drop_off_pct": drop(visited_zone, reached_billing)},
            {"stage": "purchase", "count": purchased, "drop_off_pct": drop(reached_billing, purchased)},
        ],
        "overall_conversion_pct": round((purchased / entered) * 100, 1) if entered else 0.0,
    }


def compute_heatmap(storage: Storage, store_id: str, since=None) -> dict:
    sessions = _customer_sessions(storage, store_id, since)
    visits: dict[str, int] = {}
    dwell: dict[str, list[int]] = {}
    for s in sessions:
        for z in s.zones_visited:
            visits[z] = visits.get(z, 0) + 1
        for z, ms in s.zone_dwell_ms.items():
            dwell.setdefault(z, []).append(ms)

    max_visits = max(visits.values()) if visits else 0
    max_dwell = max((mean(v) for v in dwell.values()), default=0)

    zones = []
    for z in set(list(visits) + list(dwell)):
        v = visits.get(z, 0)
        d = mean(dwell[z]) if z in dwell else 0
        zones.append({
            "zone_id": z,
            "visit_count": v,
            "visit_score": round(100 * v / max_visits, 1) if max_visits else 0.0,
            "avg_dwell_sec": round(d / 1000, 1),
            "dwell_score": round(100 * d / max_dwell, 1) if max_dwell else 0.0,
        })
    zones.sort(key=lambda x: x["visit_score"], reverse=True)

    return {
        "store_id": store_id,
        "zones": zones,
        "session_count": len(sessions),
        "data_confidence": "LOW" if len(sessions) < CONFIDENCE_MIN_SESSIONS else "OK",
    }


def compute_anomalies(storage: Storage, store_id: str) -> dict:
    """queue spike, conversion drop vs 7-day avg, dead zone (no visits 30 min)."""
    anomalies = []
    now = _now()

    today = _customer_sessions(storage, store_id, since=now - timedelta(hours=24))
    week = _customer_sessions(storage, store_id, since=now - timedelta(days=7))

    # 1. queue spike
    m = compute_metrics(storage, store_id, since=now - timedelta(hours=24))
    if m["current_queue_depth"] >= 5:
        sev = "CRITICAL" if m["current_queue_depth"] >= 8 else "WARN"
        anomalies.append({
            "type": "BILLING_QUEUE_SPIKE", "severity": sev,
            "value": m["current_queue_depth"],
            "suggested_action": "Open an additional billing counter; current queue exceeds comfort threshold.",
        })

    # 2. conversion drop vs 7-day avg
    today_conv = (sum(1 for s in today if s.converted) / len(today)) if today else 0.0
    week_conv = (sum(1 for s in week if s.converted) / len(week)) if week else 0.0
    if week_conv > 0 and today_conv < 0.7 * week_conv:
        anomalies.append({
            "type": "CONVERSION_DROP", "severity": "WARN",
            "value": round(today_conv, 3), "baseline": round(week_conv, 3),
            "suggested_action": "Today's conversion is well below the 7-day average; check staffing and stock on high-traffic zones.",
        })

    # 3. dead zone: a revenue zone with no visit in last 30 min during open hours
    recent = storage.events_for_store(store_id, since=now - timedelta(minutes=30),
                                      include_staff=False)
    recent_zones = {e["zone_id"] for e in recent if e.get("zone_id")}
    all_recent_zones = {e["zone_id"] for e in
                        storage.events_for_store(store_id, since=now - timedelta(days=7))
                        if e.get("zone_id")}
    dead = all_recent_zones - recent_zones
    for z in sorted(dead):
        anomalies.append({
            "type": "DEAD_ZONE", "severity": "INFO", "zone_id": z,
            "suggested_action": f"No visits to {z} in the last 30 min; consider a promotion or staff redirect.",
        })

    return {
        "store_id": store_id,
        "active_anomalies": anomalies,
        "count": len(anomalies),
        "computed_at": now.isoformat(),
    }
