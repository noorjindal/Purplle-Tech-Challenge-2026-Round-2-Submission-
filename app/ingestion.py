"""Ingestion: validate -> dedup -> store, with PARTIAL SUCCESS. One malformed
event in a batch of 500 does not fail the whole batch; the bad index is reported
in a structured error list. Idempotency is enforced at the storage layer via
INSERT OR IGNORE on the deterministic event_id."""
from __future__ import annotations

from app.models import CanonicalEvent, IngestItemError, IngestResponse
from app.storage import Storage


def ingest_batch(raw_events: list[dict], storage: Storage) -> IngestResponse:
    valid: list[CanonicalEvent] = []
    errors: list[IngestItemError] = []

    for i, raw in enumerate(raw_events):
        try:
            valid.append(CanonicalEvent.from_raw(raw))
        except Exception as e:  # noqa: BLE001 - want the index + message, not a crash
            errors.append(IngestItemError(index=i, error=str(e)[:200]))

    # dedup within the batch itself before hitting storage
    seen: set[str] = set()
    deduped: list[CanonicalEvent] = []
    in_batch_dupes = 0
    for ev in valid:
        if ev.event_id in seen:
            in_batch_dupes += 1
            continue
        seen.add(ev.event_id)
        deduped.append(ev)

    accepted, store_dupes = storage.insert_events(deduped)

    return IngestResponse(
        received=len(raw_events),
        accepted=accepted,
        duplicates=in_batch_dupes + store_dupes,
        rejected=len(errors),
        errors=errors,
    )
