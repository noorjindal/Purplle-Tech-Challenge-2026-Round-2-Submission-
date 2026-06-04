# CHOICES.md

Three decisions, each with the options considered, what an LLM suggested, and what
was actually chosen and why. Where I overrode the AI, I say so.

---

## 1. Detection model — YOLOv8s + ByteTrack

**Options considered:** YOLOv8 (n/s/m), YOLOv9, RT-DETR, MediaPipe Pose.

**What the AI suggested:** When asked to "pick the strongest detector for occluded
top-down retail CCTV," the LLM leaned toward RT-DETR, citing its transformer backbone,
NMS-free decoding, and better small/occluded-object recall on COCO. That advice is
correct *in the abstract*.

**What I chose and why — I partially overrode it.** I went with **YOLOv8s + ByteTrack**.
The reasoning is specific to this footage and the scoring rubric:

- The clips are single-class (person), close-to-medium range, 960×1080 @ 25fps. That
  is exactly the regime where YOLOv8 is already accuracy-saturated; RT-DETR's edge
  mostly shows on dense multi-class scenes.
- The rubric explicitly says it scores *reasoning and edge-case handling, not detection
  rate*. The hard points — re-entry dedup, group counting, staff exclusion, cross-camera
  dedup — live in the **tracking/Re-ID layer**, not the detector. A heavier detector
  barely moves those scores.
- Ultralytics ships YOLOv8 **and** ByteTrack in one `model.track()` call. RT-DETR needs
  tracking wired separately — more surface area to defend in the follow-up interview.
- Part E wants *live* metrics. YOLOv8s runs near real-time on CPU; RT-DETR is sluggish
  without a GPU, which I can't assume on the grading machine.

**Where RT-DETR would win, documented honestly:** the crowded billing clip with heavy
overlap, *if* a GPU is available. So the design names RT-DETR as the drop-in alternative
and the exact condition under which I'd switch (`MODEL_NAME` is a one-line change in
`detect.py`). Picking the flashier model and not being able to say when it's wrong is the
trap; this is the opposite.

**Re-ID sub-choice:** distance/appearance-based (HS colour histogram + trajectory) rather
than a full OSNet/torchreid model. The clips are short (~100s), faces are blurred
(so face Re-ID is impossible by design), and the dominant Re-ID need is matching a person
who steps out and returns within minutes — a regime where a cheap, lighting-tolerant
colour signature is adequate and 100× cheaper. OSNet is named as the upgrade path if
recall on re-entry proves low against ground truth.

---

## 2. Event schema design — one canonical model + tolerant adapter

**Options considered:** (a) follow the PDF spec schema verbatim
(`visitor_id`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`…); (b) follow the actual
`sample_events.jsonl` shape (`id_token`/`track_id`, lowercase `entry`/`zone_entered`,
two different timestamp keys); (c) build a canonical internal model with an adapter that
ingests *either*.

**What the AI suggested:** Initially, to implement the PDF schema, since that is the
"official" contract. I overrode this after inspecting the data: the two sample files use
a **different** schema than the PDF, and they disagree with each other (entry/exit events
key on `id_token`/`store_code`/`event_timestamp`; zone/queue events key on
`track_id`/`store_id`/`event_time`). The PDF is aspirational; the data is reality.

**What I chose and why:** option (c). `app/models.py` defines one `CanonicalEvent` and a
`from_raw()` adapter that coerces both observed shapes (and the PDF shape) into it —
normalising `store_code`→`store_id`, the two timestamp keys, and the two visitor-id
conventions. This means the ingest endpoint accepts whatever the real grader sends without
guessing which schema the hidden `assertions.py` uses. `event_id` is made **deterministic**
(uuid5 over store+visitor+type+ts+zone) when absent, so the same physical event ingested
twice dedupes cleanly — directly serving the idempotency requirement.

**Trade-off accepted:** a tolerant adapter can mask a genuinely malformed event as a
"successfully coerced" one. Mitigated by keeping `confidence` and required fields strict,
and by returning structured per-index errors on the events that truly can't parse.

---

## 3. API architecture — SQLite + compute-on-read, single FastAPI service

**Options considered:** (a) Postgres + a streaming aggregator (Kafka/Redis) with
pre-computed materialised metrics; (b) SQLite with metrics computed on read; (c) in-memory
store only.

**What the AI suggested:** the "production" answer — Postgres + Redis + a worker that
maintains rolling aggregates, because metrics must be "real-time, not cached from
yesterday." Architecturally sound for 40 live stores.

**What I chose and why — I overrode it for this context.** **SQLite + compute-on-read in a
single FastAPI service.** The acceptance gate is unforgiving: `docker compose up` must
bring the API up with *no manual steps*. Every extra service (Postgres init, Redis, a
worker) is another thing that can fail on a clean grading machine and sink the whole
submission. SQLite is embedded, zero-config, and the connection string is the only thing
that changes to graduate to Postgres later (noted in `storage.py`). "Real-time" is
satisfied by computing metrics from raw events at request time rather than serving a stale
cache — at the data volumes here (hundreds–thousands of events/store) this is sub-10ms,
which the access logs confirm.

**Trade-off accepted:** compute-on-read does not scale to millions of events per store; at
that point the rolling-aggregate design becomes necessary. `DESIGN.md` states the migration
path. For a take-home judged on correctness and "does it come up cleanly," robustness beats
premature scale.
