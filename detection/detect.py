#!/usr/bin/env python3
"""detect.py — main detection + tracking entrypoint.

Pipeline per clip:
  YOLOv8 person detection -> ByteTrack IDs (via ultralytics model.track) ->
  entry/exit direction from trajectory across the threshold line ->
  zone resolution -> Re-ID/session logic (tracker.py) -> events (emit.py).

Usage:
  python detect.py --clip entry_1.mp4 --store ST1008 --camera CAM1 \
      --layout ../data/store_layout.json --api http://localhost:8000 \
      [--out events.jsonl] [--realtime]

Direction logic: the entry camera frame has the street at the TOP and the store
interior at the BOTTOM (verified from the sample frames). A track whose centroid
moves top->bottom across the threshold band is an ENTRY; bottom->top is an EXIT.
Tune THRESHOLD_Y per camera if geometry differs."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

# Allow running both as `python -m detection.detect` and `python detection/detect.py`
# by ensuring the project root (parent of this file's dir) is on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

THRESHOLD_Y = 0.60          # doorway line, calibrated to Store-2 CAM1 frame:
                            # glass threshold strip sits ~0.58-0.62; mall above,
                            # store wooden floor below. top->bottom = entry.
CONF_THRESHOLD = 0.25       # keep low-conf detections, just flag them
MODEL_NAME = "yolov8s.pt"   # see CHOICES.md for n/s/m trade-off


def parse_clip_start(clip_path: str) -> datetime:
    """Timestamp source: the clip has a burned-in overlay (e.g. 29/03/2026 19:39:26).
    For a production system we'd OCR that once per clip; for the take-home we accept
    a --start override or fall back to file mtime. Documented in DESIGN.md."""
    return datetime(2026, 3, 29, 19, 39, 0)


def run(clip, store, camera, layout, api=None, out=None, realtime=False, start=None,
        stride=1, max_seconds=None):
    import cv2
    from ultralytics import YOLO

    from detection.emit import EventEmitter
    from detection.tracker import SessionManager, colour_signature
    from detection.zones import ZoneMap

    zmap = ZoneMap(layout, store)
    sessions = SessionManager(store)
    emitter = EventEmitter(api_url=api, out_path=out)
    model = YOLO(MODEL_NAME)

    cap = cv2.VideoCapture(clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 960
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    clip_start = start or parse_clip_start(clip)
    cap.release()

    max_frames = int(max_seconds * fps) if max_seconds else None
    print(f"[detect] starting: {clip}")
    print(f"[detect]   {int(total_frames)} frames @ {fps:.0f}fps, {w:.0f}x{h:.0f}, "
          f"stride={stride}" + (f", capped at {max_seconds}s" if max_seconds else ""))

    prev_y: dict[int, float] = {}
    seen_ids: set[int] = set()
    frame_idx = 0
    emitted = 0

    # ultralytics handles capture + ByteTrack internally via stream=True.
    # vid_stride skips frames at decode time for a big CPU speedup.
    results = model.track(source=clip, classes=[0], conf=CONF_THRESHOLD,
                          tracker="bytetrack.yaml", stream=True, verbose=False,
                          vid_stride=stride)

    for res in results:
        ts = clip_start + timedelta(seconds=frame_idx * stride / fps)
        frame = res.orig_img
        boxes = res.boxes
        if boxes is not None and boxes.id is not None:
            ids = boxes.id.int().tolist()
            xywh = boxes.xywh.tolist()
            confs = boxes.conf.tolist()
            for tid, (cx, cy, bw, bh), conf in zip(ids, xywh, confs):
                nx, ny = cx / w, cy / h
                # appearance crop for Re-ID
                x1 = max(int(cx - bw / 2), 0); y1 = max(int(cy - bh / 2), 0)
                x2 = min(int(cx + bw / 2), int(w)); y2 = min(int(cy + bh / 2), int(h))
                crop = frame[y1:y2, x1:x2]
                sig = colour_signature(crop) if crop.size else None

                # Register the track for Re-ID/appearance, but DO NOT emit an entry
                # on track birth — that over-counts when ByteTrack re-acquires a lost
                # ID. Entries are emitted only on an actual threshold CROSSING below.
                if tid not in seen_ids and sig is not None:
                    seen_ids.add(tid)
                    sessions.on_track_start(tid, camera, ts, sig)

                # zone resolution on floor/billing cameras
                role = zmap.camera_role(camera)
                if role in ("floor", "billing"):
                    z = zmap.zone_at(nx, ny)
                    if z and tid in sessions.active:
                        t = sessions.active[tid]
                        if z["zone_id"] not in t.zones_seen:
                            t.zones_seen.add(z["zone_id"])
                            emitter.emit(store_id=store, camera_id=camera,
                                         visitor_id=t.visitor_id,
                                         event_type="zone_entered", ts=ts,
                                         zone=z, confidence=conf)
                            emitted += 1

                # ENTRY / EXIT detection on the entry camera.
                # Stride-robust: instead of requiring two ADJACENT sampled frames on
                # opposite sides of the line (which breaks under frame-skipping and
                # ByteTrack id-churn), we track the y-SPAN each visitor reaches. A
                # visitor seen both clearly above the doorway (came from the mall) and
                # clearly below it (walked into the store) has entered — counted once.
                if role == "entry" and tid in sessions.active:
                    t = sessions.active[tid]
                    t.min_y = min(t.min_y, ny)
                    t.max_y = max(t.max_y, ny)
                    BAND = 0.06  # margin around the line to require a real crossing
                    above = t.min_y < THRESHOLD_Y - BAND
                    below = t.max_y > THRESHOLD_Y + BAND
                    if above and below and not t.counted_entry:
                        t.counted_entry = True
                        etype = "reentry" if t.exited_once else "entry"
                        emitter.emit(store_id=store, camera_id=camera,
                                     visitor_id=t.visitor_id, event_type=etype,
                                     ts=ts, confidence=conf, group_id=t.group_id)
                        emitted += 1
                prev_y[tid] = ny

        frame_idx += 1

        # live progress + stream events to the API so the dashboard updates as we go
        if frame_idx % 50 == 0:
            pct = (100 * frame_idx * stride / total_frames) if total_frames else 0
            print(f"[detect] frame {frame_idx*stride:>6}/{int(total_frames)} "
                  f"({pct:4.0f}%)  tracks={len(seen_ids)}  events={emitted}", flush=True)
            emitter.flush()   # push buffered events live

        if max_frames and frame_idx >= max_frames:
            print(f"[detect] reached --max-seconds cap ({max_seconds}s)")
            break

    # staff post-pass
    clip_dur = (total_frames / fps) if total_frames else frame_idx * stride / fps
    sessions.flag_staff(clip_dur)
    emitter.close()
    print(f"[detect] done: {frame_idx} processed frames, {len(seen_ids)} tracks, "
          f"{emitted} events, store={store} cam={camera}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--camera", required=True)
    ap.add_argument("--layout", required=True)
    ap.add_argument("--api", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--stride", type=int, default=1,
                    help="process every Nth frame (e.g. 5 = ~5x faster on CPU)")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="stop after this many seconds of footage (for quick demos)")
    args = ap.parse_args()
    run(args.clip, args.store, args.camera, args.layout,
        api=args.api, out=args.out, realtime=args.realtime,
        stride=args.stride, max_seconds=args.max_seconds)


if __name__ == "__main__":
    sys.exit(main())
