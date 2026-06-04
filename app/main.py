"""FastAPI entrypoint for the Store Intelligence API.

Production-aware:
 - structured JSON access log per request (trace_id, store_id, endpoint,
   latency_ms, event_count, status_code)
 - StorageUnavailable -> HTTP 503 with structured body, never a raw stack trace
 - /health reports last-event lag per store with STALE_FEED warning
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app import analytics
from app.ingestion import ingest_batch
from app.models import ErrorBody, IngestRequest
from app.pos_loader import load_pos_csv
from app.storage import StorageUnavailable, get_storage

STALE_FEED_MIN = 10

# ---- structured logging ----
logger = logging.getLogger("store_intel")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def log_event(**fields):
    logger.info(json.dumps(fields, default=str))


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage = get_storage()
    pos_path = os.environ.get("POS_CSV_PATH", "/data/pos_transactions.csv")
    try:
        n = load_pos_csv(pos_path, storage)
        log_event(event="startup", pos_baskets_loaded=n)
    except Exception as e:  # noqa: BLE001
        log_event(event="startup_pos_load_failed", error=str(e))
    yield


app = FastAPI(title="Apex Store Intelligence API", version="1.0", lifespan=lifespan)

# Allow the dashboard (served on :8080) to call the API (:8000) from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # take-home scope; tighten to the dashboard origin in prod
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id", uuid.uuid4().hex[:12])
    start = time.perf_counter()
    request.state.trace_id = trace_id
    request.state.event_count = 0
    try:
        response = await call_next(request)
        status = response.status_code
    except StorageUnavailable as e:
        status = 503
        body = ErrorBody(error="storage_unavailable", detail=str(e)[:200], trace_id=trace_id)
        response = JSONResponse(status_code=503, content=body.model_dump())
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    log_event(
        trace_id=trace_id,
        endpoint=request.url.path,
        method=request.method,
        store_id=request.path_params.get("store_id") if hasattr(request, "path_params") else None,
        event_count=getattr(request.state, "event_count", 0),
        latency_ms=latency_ms,
        status_code=status,
    )
    response.headers["x-trace-id"] = trace_id
    return response


def _guard(fn, *args, trace_id=None, **kwargs):
    """Wrap analytics calls so a storage failure returns 503, not a 500 stack trace."""
    try:
        return fn(*args, **kwargs)
    except StorageUnavailable as e:
        return JSONResponse(
            status_code=503,
            content=ErrorBody(error="storage_unavailable", detail=str(e)[:200],
                              trace_id=trace_id).model_dump(),
        )


@app.post("/events/ingest")
async def ingest(req: IngestRequest, request: Request):
    request.state.event_count = len(req.events)
    storage = get_storage()
    try:
        result = ingest_batch(req.events, storage)
    except StorageUnavailable as e:
        return JSONResponse(
            status_code=503,
            content=ErrorBody(error="storage_unavailable", detail=str(e)[:200],
                              trace_id=request.state.trace_id).model_dump(),
        )
    return result.model_dump()


@app.get("/stores/{store_id}/metrics")
async def metrics(store_id: str, request: Request):
    return _guard(analytics.compute_metrics, get_storage(), store_id,
                  trace_id=request.state.trace_id)


@app.get("/stores/{store_id}/funnel")
async def funnel(store_id: str, request: Request):
    return _guard(analytics.compute_funnel, get_storage(), store_id,
                  trace_id=request.state.trace_id)


@app.get("/stores/{store_id}/heatmap")
async def heatmap(store_id: str, request: Request):
    return _guard(analytics.compute_heatmap, get_storage(), store_id,
                  trace_id=request.state.trace_id)


@app.get("/stores/{store_id}/anomalies")
async def anomalies(store_id: str, request: Request):
    return _guard(analytics.compute_anomalies, get_storage(), store_id,
                  trace_id=request.state.trace_id)


@app.get("/health")
async def health():
    storage = get_storage()
    ok = storage.health_check()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "storage_unavailable"},
        )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = storage.last_event_ts_per_store()
    stores = []
    for sid, ts in last.items():
        lag_min = (now - datetime.fromisoformat(ts)).total_seconds() / 60
        stores.append({
            "store_id": sid,
            "last_event": ts,
            "lag_minutes": round(lag_min, 1),
            "stale_feed": lag_min > STALE_FEED_MIN,
        })
    return {
        "status": "ok",
        "stores": stores,
        "checked_at": now.isoformat(),
    }
