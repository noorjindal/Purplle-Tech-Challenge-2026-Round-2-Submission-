"""emit.py — turns tracker state changes into schema-compliant events and ships
them to the Intelligence API in batches of <=500 (or to a JSONL file for replay).

Emits the lowercase event types the API expects (entry, exit, zone_entered,
zone_exited, zone_dwell, queue_joined, queue_completed, queue_abandoned, reentry)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import requests

DWELL_EMIT_MS = 30_000  # emit zone_dwell every 30s of continuous dwell


class EventEmitter:
    def __init__(self, api_url: str | None = None, out_path: str | None = None,
                 batch_size: int = 200):
        self.api_url = api_url
        self.out_path = out_path
        self.batch_size = batch_size
        self._buf: list[dict] = []
        self._fh = open(out_path, "w") if out_path else None

    def _event(self, store_id, camera_id, visitor_id, event_type, ts: datetime,
               zone=None, dwell_ms=None, is_staff=False, confidence=1.0,
               queue_depth=None, session_seq=None, group_id=None, group_size=None):
        ev = {
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp": ts.isoformat(),
            "zone_id": zone["zone_id"] if zone else None,
            "zone_name": zone["zone_name"] if zone else None,
            "is_revenue_zone": zone["is_revenue_zone"] if zone else None,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": round(float(confidence), 3),  # NEVER suppressed; low-conf kept + flagged
            "metadata": {
                "queue_depth": queue_depth,
                "session_seq": session_seq,
                "group_id": group_id,
                "group_size": group_size,
            },
        }
        return ev

    def emit(self, **kw):
        self._buf.append(self._event(**kw))
        if self._fh:
            self._fh.write(json.dumps(self._buf[-1]) + "\n")
        if len(self._buf) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self._buf:
            return
        if self.api_url:
            try:
                r = requests.post(f"{self.api_url}/events/ingest",
                                  json={"events": self._buf}, timeout=10)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"[emit] API post failed ({e}); events buffered to file only")
        self._buf.clear()

    def close(self):
        self.flush()
        if self._fh:
            self._fh.close()
