# Apex Store Intelligence

End-to-end pipeline: raw CCTV → structured behavioural events → real-time store
analytics API → live dashboard. North-star metric: **offline conversion rate**.

> **Note on schema:** the provided `sample_events.jsonl` and `pos_transactions.csv`
> use a different schema than the challenge PDF, and the two sample shapes differ from
> each other. This system follows the **data**, not the PDF, via a tolerant ingest
> adapter that accepts both. See `CHOICES.md` §2.

## Quick start (5 commands)

```bash
git clone <repo-url>
cd store-intelligence
docker compose up --build        # 1) API on :8000, dashboard on :8080
curl localhost:8000/health       # 2) verify it's up
# 3) load the sample events to see the dashboard populate:
curl -X POST localhost:8000/events/ingest -H 'content-type: application/json' \
     -d "{\"events\": $(python3 -c "import json,sys; print(json.dumps([json.loads(l) for l in open('data/sample_events.jsonl')]))")}"
open http://localhost:8080         # 4) live dashboard (or visit in browser)
```

`docker compose up` needs no manual steps beyond `git clone`. POS data is auto-loaded
from `data/pos_transactions.csv` on startup.

## Running the detection pipeline against the clips

The detection pipeline runs on **your** machine (it needs the video + ideally a GPU);
the API runs in Docker. Output events flow from one to the other over HTTP.

```bash
pip install -r requirements-detection.txt

# process one clip:
python detection/detect.py \
    --clip path/to/entry_1.mp4 --store ST1008 --camera CAM1 \
    --layout data/store_layout.json --api http://localhost:8000 \
    --out events_entry.jsonl

# or process all 3 camera angles for a store at once:
./detection/run.sh path/to/store_1008_clips ST1008 http://localhost:8000
```

Events are POSTed to `/events/ingest` in batches and also written to JSONL for replay.
The dashboard updates within 3 seconds as events arrive.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| POST | `/events/ingest` | Validates/dedups/stores ≤500 events; idempotent; partial success |
| GET | `/stores/{id}/metrics` | Unique visitors, conversion rate, dwell/zone, queue depth, abandonment |
| GET | `/stores/{id}/funnel` | Entry → zone → billing → purchase, session-deduped |
| GET | `/stores/{id}/heatmap` | Zone visit + dwell, normalised 0–100, low-confidence flag |
| GET | `/stores/{id}/anomalies` | Queue spike, conversion drop vs 7-day, dead zone |
| GET | `/health` | Status + per-store last-event lag + STALE_FEED warning |

## Tests

```bash
pip install -r requirements-api.txt pytest pytest-cov
python -m pytest tests/ --cov=app          # 10 tests, ~82% statement coverage
```

Covers: idempotency, partial-success ingest, empty store, all-staff exclusion,
zero-purchase, re-entry not double-counted, funnel monotonicity, stale feed.

## Layout

```
detection/   detect.py · tracker.py · zones.py · emit.py · run.sh
app/         main.py · models.py · ingestion.py · sessions.py · analytics.py
             · storage.py · pos_loader.py
dashboard/   index.html (live web UI)
data/        store_layout.json · pos_transactions.csv · sample_events.jsonl
tests/       test_api.py
DESIGN.md · CHOICES.md
```

## Known limitations
- `store_layout.json` zone polygons were reconstructed from the two layout PNGs (the
  challenge ZIP didn't include the JSON). Re-calibrate polygons once real frames exist.
- Clip start time is taken from a `--start` flag / hardcoded fallback; production would
  OCR the burned-in timestamp overlay. See `detect.py`.
- Compute-on-read is fine at take-home volumes; see `DESIGN.md` for the scale-out path.
