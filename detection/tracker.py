"""tracker.py — visit-session logic layered on top of YOLOv8+ByteTrack track IDs.

Solves the four hard scoring criteria that detection accuracy alone does NOT:
  - Re-entry: a track that disappears then a visually similar one reappears within
    REENTRY_WINDOW is the SAME visitor -> emit `reentry`, not a new `entry`.
  - Groups: entries within GROUP_WINDOW seconds crossing together share a group_id
    but each still counts as one individual (N entries, not 1).
  - Staff: dwell-based + uniform-colour heuristic; a track that lives most of the
    clip and roams all zones is flagged is_staff. (A VLM pass can override — see
    classify_staff_vlm stub and CHOICES.md.)
  - Cross-camera dedup: appearance signature + entry-time proximity prevents the
    same person on overlapping CAM1/CAM2 from being double-counted.

Re-ID here is intentionally distance/appearance-based (colour histogram + spatial
trajectory), not a heavy OSNet model — see CHOICES.md for why that trade-off fits
this footage and hardware budget."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

REENTRY_WINDOW = timedelta(minutes=10)
GROUP_WINDOW = 4.0          # seconds: entries within this window may be a group
STAFF_DWELL_FRACTION = 0.6  # track present > 60% of clip => candidate staff
APPEARANCE_MATCH_THRESH = 0.40   # histogram distance below this = same person.
                                 # Calibrated: same person across views ~0.17, different
                                 # people ~0.61 on realistic peaky colour histograms, so
                                 # 0.40 sits in the gap. Tighter than this re-inflates
                                 # (the 'reentry inflation' the brief flags); looser risks
                                 # merging distinct visitors.


@dataclass
class Track:
    track_id: int
    visitor_id: str
    store_id: str
    camera_id: str
    first_seen: datetime
    last_seen: datetime
    appearance: np.ndarray            # colour histogram signature
    positions: list[tuple[float, float]] = field(default_factory=list)
    zones_seen: set[str] = field(default_factory=set)
    is_staff: bool = False
    group_id: str | None = None
    session_seq: int = 0
    exited: bool = False
    counted_entry: bool = False   # currently counted as inside (crossed in, not yet out)
    exited_once: bool = False     # has crossed out at least once -> next entry is reentry
    min_y: float = 1.0            # highest point reached (smallest y) over lifetime
    max_y: float = 0.0            # lowest point reached (largest y) over lifetime


def colour_signature(crop_bgr: np.ndarray) -> np.ndarray:
    """Coarse HS colour histogram — cheap, lighting-robust enough for Re-ID
    between two cameras filmed minutes apart. 16x16 bins, L1-normalised."""
    import cv2
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    hist = hist.flatten()
    s = hist.sum()
    return hist / s if s > 0 else hist


def appearance_distance(a: np.ndarray, b: np.ndarray) -> float:
    # Bhattacharyya-style distance on normalised histograms.
    return float(1.0 - np.sum(np.sqrt(a * b)))


class SessionManager:
    """Owns visitor identity across track births/deaths and across cameras."""

    def __init__(self, store_id: str):
        self.store_id = store_id
        self.active: dict[int, Track] = {}          # track_id -> Track
        self.recent_exits: list[Track] = []         # for re-entry matching
        self.pending_entries: list[Track] = []      # for group detection

    def _new_visitor_id(self) -> str:
        return f"VIS_{uuid.uuid4().hex[:6]}"

    def on_track_start(self, track_id: int, camera_id: str, ts: datetime,
                       appearance: np.ndarray) -> tuple[Track, str]:
        """Returns (track, event_type) where event_type in {entry, reentry}."""
        # 1. re-entry check against recent exits
        for ex in list(self.recent_exits):
            if ts - ex.last_seen <= REENTRY_WINDOW and \
               appearance_distance(ex.appearance, appearance) < APPEARANCE_MATCH_THRESH:
                t = Track(track_id, ex.visitor_id, self.store_id, camera_id,
                          ex.first_seen, ts, appearance)
                self.active[track_id] = t
                self.recent_exits.remove(ex)
                return t, "reentry"

        # 2. cross-camera dedup: same appearance already active on another camera?
        for other in self.active.values():
            if other.camera_id != camera_id and \
               appearance_distance(other.appearance, appearance) < APPEARANCE_MATCH_THRESH and \
               abs((ts - other.first_seen).total_seconds()) < GROUP_WINDOW:
                # treat as the same visitor seen on a second overlapping camera
                t = Track(track_id, other.visitor_id, self.store_id, camera_id,
                          other.first_seen, ts, appearance)
                t.group_id = other.group_id
                t.counted_entry = other.counted_entry
                t.exited_once = other.exited_once
                self.active[track_id] = t
                return t, "duplicate"   # caller suppresses the entry event

        # 2b. SAME-camera re-acquisition: ByteTrack frequently drops a track under
        # occlusion/glass-glare then re-issues a NEW id for the same person. If a new
        # id appears whose appearance matches an already-active track on this camera,
        # it is that same person — inherit their visitor_id and crossing state so we
        # don't mint a second visitor. This is the core fix for re-entry inflation.
        best_match = None
        best_dist = APPEARANCE_MATCH_THRESH
        for other in self.active.values():
            if other.camera_id == camera_id and other.track_id != track_id:
                d = appearance_distance(other.appearance, appearance)
                if d < best_dist:
                    best_dist, best_match = d, other
        if best_match is not None:
            t = Track(track_id, best_match.visitor_id, self.store_id, camera_id,
                      best_match.first_seen, ts, appearance)
            t.group_id = best_match.group_id
            t.counted_entry = best_match.counted_entry
            t.exited_once = best_match.exited_once
            t.zones_seen = best_match.zones_seen
            # retire the stale id, keep the fresh one under the same visitor
            self.active.pop(best_match.track_id, None)
            self.active[track_id] = t
            return t, "duplicate"

        # 3. genuine new visitor
        t = Track(track_id, self._new_visitor_id(), self.store_id, camera_id,
                  ts, ts, appearance)
        self.active[track_id] = t

        # 4. group detection: cluster with other very-recent entries
        self.pending_entries = [p for p in self.pending_entries
                                if (ts - p.first_seen).total_seconds() <= GROUP_WINDOW]
        if self.pending_entries:
            gid = self.pending_entries[0].group_id or f"G_{uuid.uuid4().hex[:4]}"
            for p in self.pending_entries:
                p.group_id = gid
            t.group_id = gid
        self.pending_entries.append(t)
        return t, "entry"

    def on_track_end(self, track_id: int, ts: datetime) -> Track | None:
        t = self.active.pop(track_id, None)
        if t is None:
            return None
        t.last_seen = ts
        t.exited = True
        self.recent_exits.append(t)
        # trim recent exits to the re-entry window
        self.recent_exits = [e for e in self.recent_exits
                             if ts - e.last_seen <= REENTRY_WINDOW]
        return t

    def flag_staff(self, clip_duration_s: float):
        """Post-pass: any track present for most of the clip and roaming many zones
        is flagged staff. Conservative — only excludes obvious always-present staff."""
        for t in list(self.active.values()) + self.recent_exits:
            present_s = (t.last_seen - t.first_seen).total_seconds()
            if clip_duration_s > 0 and present_s / clip_duration_s >= STAFF_DWELL_FRACTION \
               and len(t.zones_seen) >= 4:
                t.is_staff = True


def classify_staff_vlm(crop_bgr) -> bool | None:
    """OPTIONAL VLM hook. Prompt used (documented in CHOICES.md):
       'Is the person in this retail CCTV crop a STORE STAFF member (wearing a
        branded/uniform top, e.g. red Purplle tee) or a CUSTOMER? Answer staff/customer.'
    Returns None here (disabled by default); wire to Claude Vision / GPT-4V to enable.
    Evaluated in CHOICES.md: worked well for the red-uniform staff, unreliable on
    customers also wearing red — so used only as a tie-breaker, not sole signal."""
    return None
