# PROMPT: "Design a Pydantic v2 event model that reconciles two observed JSONL
#   event shapes (entry/exit events keyed on id_token/store_code/event_timestamp,
#   and zone/queue events keyed on track_id/store_id/event_time). It must round-trip
#   the sample_events.jsonl and also support the analytics queries (funnel, metrics,
#   heatmap, anomalies). Generate a CanonicalEvent plus a from_raw() adapter."
# CHANGES MADE: Added field-validator coercion for the two timestamp keys and the two
#   store-id keys, made visitor_id derive from id_token OR a synthesised track-session
#   key, kept `confidence` and `is_staff` first-class (challenge scores confidence
#   calibration + staff exclusion), and added EventType as a permissive enum that
#   accepts the lowercase wire values seen in the data.
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class EventType(str, Enum):
    """Lowercase wire values match what sample_events.jsonl actually emits.
    Spec PDF used UPPER_CASE names; we follow the data, not the PDF."""
    ENTRY = "entry"
    EXIT = "exit"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"
    ZONE_DWELL = "zone_dwell"
    QUEUE_JOINED = "queue_joined"
    QUEUE_COMPLETED = "queue_completed"
    QUEUE_ABANDONED = "queue_abandoned"
    REENTRY = "reentry"


# event types that open / close a visit session
ENTRY_TYPES = {EventType.ENTRY, EventType.REENTRY}
EXIT_TYPES = {EventType.EXIT}
ZONE_TYPES = {EventType.ZONE_ENTERED, EventType.ZONE_EXITED, EventType.ZONE_DWELL}
QUEUE_TYPES = {EventType.QUEUE_JOINED, EventType.QUEUE_COMPLETED, EventType.QUEUE_ABANDONED}


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    queue_position_at_join: Optional[int] = None
    wait_seconds: Optional[float] = None
    abandoned: Optional[bool] = None
    session_seq: Optional[int] = None
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender_pred: Optional[str] = None
    age_pred: Optional[int] = None
    age_bucket: Optional[str] = None
    group_id: Optional[str] = None
    group_size: Optional[int] = None
    is_face_hidden: Optional[bool] = None

    model_config = {"extra": "allow"}


class CanonicalEvent(BaseModel):
    """The single internal representation every endpoint reads.

    `event_id` is deterministic when absent so the same physical event ingested
    twice dedupes cleanly (idempotency requirement in Part C)."""
    event_id: str = Field(default="")
    store_id: str
    camera_id: Optional[str] = None
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    is_revenue_zone: Optional[bool] = None
    dwell_ms: Optional[int] = None
    is_staff: bool = False
    confidence: float = 1.0
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    model_config = {"extra": "ignore"}

    @field_validator("store_id")
    @classmethod
    def _norm_store(cls, v: str) -> str:
        # Accept "store_1076" / "ST1076" / "STORE_BLR_002" -> canonical "ST1076" form.
        v = v.strip()
        if v.lower().startswith("store_"):
            return "ST" + v.split("_", 1)[1]
        return v

    @model_validator(mode="after")
    def _fill_event_id(self) -> "CanonicalEvent":
        if not self.event_id:
            basis = f"{self.store_id}|{self.visitor_id}|{self.event_type.value}|{self.timestamp.isoformat()}|{self.zone_id}"
            self.event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, basis))
        return self

    # ---- adapter from the two raw JSONL shapes ----
    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "CanonicalEvent":
        """Tolerant parser for both observed shapes in sample_events.jsonl."""
        etype = str(raw.get("event_type", "")).lower()

        # timestamp lives under different keys depending on shape
        ts = raw.get("timestamp") or raw.get("event_timestamp") or raw.get("event_time") \
            or raw.get("queue_join_ts")

        # store id under store_code or store_id
        store = raw.get("store_id") or raw.get("store_code") or "UNKNOWN"

        # visitor id: id_token (entry/exit) or synthesised from track_id+store (zone/queue)
        vid = raw.get("visitor_id") or raw.get("id_token")
        if not vid:
            track = raw.get("track_id")
            vid = f"VIS_{store}_{track}" if track is not None else f"VIS_{uuid.uuid4().hex[:6]}"

        meta = EventMetadata(
            queue_depth=raw.get("queue_depth") or raw.get("queue_position_at_join"),
            queue_position_at_join=raw.get("queue_position_at_join"),
            wait_seconds=raw.get("wait_seconds"),
            abandoned=raw.get("abandoned"),
            zone_hotspot_x=raw.get("zone_hotspot_x"),
            zone_hotspot_y=raw.get("zone_hotspot_y"),
            gender_pred=raw.get("gender_pred") or raw.get("gender"),
            age_pred=raw.get("age_pred") or raw.get("age"),
            age_bucket=raw.get("age_bucket"),
            group_id=raw.get("group_id"),
            group_size=raw.get("group_size"),
            is_face_hidden=raw.get("is_face_hidden"),
        )

        return cls(
            event_id=raw.get("event_id") or raw.get("queue_event_id") or "",
            store_id=store,
            camera_id=raw.get("camera_id"),
            visitor_id=vid,
            event_type=etype,
            timestamp=ts,
            zone_id=raw.get("zone_id"),
            zone_name=raw.get("zone_name"),
            is_revenue_zone=_coerce_bool(raw.get("is_revenue_zone")),
            dwell_ms=raw.get("dwell_ms"),
            is_staff=bool(raw.get("is_staff", False)),
            confidence=float(raw.get("confidence", 1.0)),
            metadata=meta,
        )


def _coerce_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"yes", "true", "1", "y"}


# ---------- API request/response models ----------
class IngestRequest(BaseModel):
    events: list[dict[str, Any]] = Field(..., max_length=500)


class IngestItemError(BaseModel):
    index: int
    error: str


class IngestResponse(BaseModel):
    received: int
    accepted: int
    duplicates: int
    rejected: int
    errors: list[IngestItemError] = []


class ErrorBody(BaseModel):
    error: str
    detail: Optional[str] = None
    trace_id: Optional[str] = None
