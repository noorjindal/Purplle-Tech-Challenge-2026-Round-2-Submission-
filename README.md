# Apex Store Intelligence

End-to-end retail analytics pipeline: raw CCTV → detection → structured events → real-time API → live dashboard.

**North-star metric:** Offline store conversion rate = unique purchasing visitors ÷ total unique visitors.

---

## Architecture Overview

```
CCTV Clips
    │
    ▼
detection/          ← YOLOv8s + ByteTrack + Re-ID (runs on your machine)
    │  emits events via HTTP or JSONL
    ▼
POST /events/ingest ← validates, deduplicates, stores (idempotent)
    │
    ▼
SQLite              ← events + POS baskets
    │
    ├── GET /stores/{id}/metrics
    ├── GET /stores/{id}/funnel
    ├── GET /stores/{id}/heatmap
    ├── GET /stores/{id}/anomalies
    └── GET /health
              │
              ▼
        dashboard/  ← web UI, polls every 3s (localhost:8080)
```

The **API + dashboard** run in Docker. The **detection pipeline** runs on your machine (needs OpenCV + ideally a GPU).

---

## Quick Start (5 commands)

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/store-intelligence.git
cd store-intelligence

# 2. Start the API and dashboard
docker compose up --build

# 3. Verify the API is healthy
curl localhost:8000/health

# 4. Load the sample events to populate the dashboard
curl -X POST localhost:8000/events/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"events\": $(python3 -c "import json; print(json.dumps([json.loads(l) for l in open('data/sample_events.jsonl') if l.strip()]))")}"

# 5. Open the live dashboard
open http://localhost:8080   # or visit in your browser
```

`docker compose up` requires no manual setup steps beyond `git clone`. POS data is auto-loaded from `data/pos_transactions.csv` on startup.

---

## Running the Detection Pipeline Against the Clips

The detection pipeline runs **on your machine** (not in Docker) because it needs direct video access and ideally a GPU. The API container must already be running when you do this.

### Install detection dependencies

```bash
pip install -r requirements-detection.txt
```

> **Model weight:** `yolov8s.pt` is not committed to the repo (22 MB).  
> It downloads automatically on first run via Ultralytics. Or manually:  
> `python -c "from ultralytics import YOLO; YOLO('yolov8s.pt')"`

### Process a single clip

```bash
python detection/detect.py \
  --clip path/to/entry_camera.mp4 \
  --store STORE_BLR_002 \
  --camera CAM_ENTRY_01 \
  --layout data/store_layout.json \
  --api http://localhost:8000 \
  --out events_entry.jsonl
```

| Flag | Description |
|---|---|
| `--clip` | Path to the `.mp4` video file |
| `--store` | Store ID from `store_layout.json` (e.g. `STORE_BLR_002`) |
| `--camera` | Camera ID (e.g. `CAM_ENTRY_01`, `CAM_FLOOR_01`, `CAM_BILLING_01`) |
| `--layout` | Path to `store_layout.json` |
| `--api` | URL of the running API (default: `http://localhost:8000`) |
| `--out` | Optional JSONL file to write events to for replay |
| `--realtime` | Add this flag to simulate real-time playback (for live dashboard, Part E) |

### Process all 3 camera angles for a store at once

```bash
./detection/run.sh path/to/store_clips/ STORE_BLR_002 http://localhost:8000
```

The script auto-discovers clips by role (`entry`, `floor`, `billing`) in the clips directory. Events are POSTed to `/events/ingest` in batches and also written to `events_<STORE>_<role>.jsonl` for replay.

### Replay events from a saved JSONL file (no video needed)

```bash
python3 -c "
import json, requests
events = [json.loads(l) for l in open('events_entry.jsonl') if l.strip()]
r = requests.post('http://localhost:8000/events/ingest', json={'events': events})
print(r.json())
"
```

---

## API Endpoints

All endpoints return JSON. `{id}` is a store ID (e.g. `STORE_BLR_002`).

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/ingest` | Accepts up to 500 events per batch. Validates, deduplicates, stores. Idempotent by `event_id`. Returns partial success on malformed events. |
| `GET` | `/stores/{id}/metrics` | Unique visitors, conversion rate, avg dwell per zone, queue depth, abandonment rate. Staff excluded. Real-time. |
| `GET` | `/stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase with counts and drop-off %. Session-deduped; re-entries don't double-count. |
| `GET` | `/stores/{id}/heatmap` | Zone visit frequency + avg dwell, normalised 0–100. Includes `data_confidence: LOW` flag if fewer than 20 sessions. |
| `GET` | `/stores/{id}/anomalies` | Active anomalies: queue spike, conversion drop vs 7-day avg, dead zone (no visits in 30 min). Severity: `INFO / WARN / CRITICAL`. Each anomaly includes a `suggested_action`. |
| `GET` | `/health` | Service status + last event timestamp per store. Returns `stale_feed: true` if lag > 10 minutes. |

### Example responses

```bash
# Metrics
curl localhost:8000/stores/STORE_BLR_002/metrics

# Conversion funnel
curl localhost:8000/stores/STORE_BLR_002/funnel

# Zone heatmap
curl localhost:8000/stores/STORE_BLR_002/heatmap

# Active anomalies
curl localhost:8000/stores/STORE_BLR_002/anomalies

# Health check
curl localhost:8000/health
```

---

## Running Tests

```bash
pip install -r requirements-api.txt pytest pytest-cov
python -m pytest tests/ --cov=app
```

**10 tests, ~82% statement coverage.** Covers:

- Ingest happy path with real `sample_events.jsonl` shape
- Idempotency — same payload twice yields 0 new accepts
- Partial success — one malformed event doesn't fail the batch
- Empty store returns zeroed metrics, not `null` or 500
- All-staff clip — staff events excluded from `unique_visitors`
- Zero-purchase store — `conversion_rate: 0.0` with no division error
- Re-entry not double-counted in the funnel
- Funnel drop-off is monotonically decreasing
- `/health` reports `stale_feed: true` for an old event
- Heatmap `data_confidence: LOW` when fewer than 20 sessions

Each test uses an isolated SQLite database in a temp directory.

---

## Project Layout

```
store-intelligence/
├── detection/
│   ├── detect.py          # Main detection + tracking (YOLOv8 + ByteTrack)
│   ├── tracker.py         # Re-ID / session logic / staff heuristics
│   ├── zones.py           # Zone polygon resolution from store_layout.json
│   ├── emit.py            # Event schema serialisation + HTTP emission
│   └── run.sh             # One command to process all clips → events
├── app/
│   ├── main.py            # FastAPI entrypoint + structured logging + middleware
│   ├── models.py          # Pydantic event schema + tolerant raw adapter
│   ├── ingestion.py       # Validate, dedup, store with partial success
│   ├── sessions.py        # Session reconstruction + POS correlation
│   ├── analytics.py       # metrics / funnel / heatmap / anomalies
│   ├── storage.py         # SQLite layer + StorageUnavailable → 503
│   └── pos_loader.py      # Auto-loads pos_transactions.csv on startup
├── dashboard/
│   └── index.html         # Live web UI, polls API every 3s
├── data/
│   ├── store_layout.json  # Zone definitions for each store
│   ├── pos_transactions.csv
│   └── sample_events.jsonl
├── tests/
│   └── test_api.py
├── docs/
│   ├── DESIGN.md
│   └── CHOICES.md
├── docker-compose.yml
├── Dockerfile
├── requirements-api.txt
└── requirements-detection.txt
```

---

## Configuration

All config is via environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|---|---|---|
| `STORE_DB_PATH` | `/data/store_intel.db` | SQLite database path |
| `POS_CSV_PATH` | `/data/pos_transactions.csv` | POS transaction file |
| `STORE_LAYOUT_PATH` | `/data/store_layout.json` | Zone layout file |

---

## Production Notes

- **Idempotency:** `POST /events/ingest` is safe to call twice with the same payload — duplicate `event_id`s are detected and counted in `duplicates`, not re-inserted.
- **Structured logging:** Every request logs `trace_id`, `store_id`, `endpoint`, `latency_ms`, `event_count`, `status_code` as JSON to stdout.
- **Graceful degradation:** Storage unavailable → HTTP 503 with structured JSON body. No raw stack traces in responses.
- **Zero-traffic safety:** All endpoints handle empty stores — no nulls, no crashes.
- **Staff exclusion:** All `is_staff: true` events are excluded from customer-facing metrics.

---

## Known Limitations

- `store_layout.json` zone polygons were reconstructed from layout PNGs (the challenge ZIP didn't include a JSON). Recalibrate polygons once real calibrated frames are available.
- Clip start timestamp is taken from a `--start` flag or a hardcoded fallback. Production would OCR the burned-in overlay. Documented in `DESIGN.md`.
- Compute-on-read is appropriate at challenge volumes; `DESIGN.md` covers the scale-out path for 40 live stores.

---

## Live Dashboard (Part E)

The dashboard is included and served automatically on port 8080 via `docker compose up`. It polls `/metrics`, `/funnel`, `/heatmap`, and `/anomalies` every 3 seconds. To see it update live, run the detection pipeline with `--realtime` against a clip while the containers are running:

```bash
python detection/detect.py \
  --clip path/to/entry.mp4 \
  --store STORE_BLR_002 \
  --camera CAM_ENTRY_01 \
  --layout data/store_layout.json \
  --api http://localhost:8000 \
  --realtime
```

Then open `http://localhost:8080` — metrics update within 3 seconds as events flow in.
