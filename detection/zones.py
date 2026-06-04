"""Zone resolution from store_layout.json. Maps a normalised (x,y) point in a
camera frame to a named zone via point-in-polygon. Used by the tracker to decide
zone_entered / zone_exited / dwell."""
from __future__ import annotations

import json
from pathlib import Path


def _point_in_poly(x: float, y: float, poly: list[list[float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


class ZoneMap:
    def __init__(self, layout_path: str, store_id: str):
        layout = json.loads(Path(layout_path).read_text())
        store = next((s for s in layout["stores"] if s["store_id"] == store_id), None)
        if store is None:
            raise ValueError(f"store {store_id} not in layout")
        self.store = store
        self.zones = store["zones"]

    def zone_at(self, x: float, y: float) -> dict | None:
        """x,y in normalised [0,1] frame coords. Returns the zone dict or None.
        THRESHOLD/AISLE zones are returned too; caller decides relevance."""
        for z in self.zones:
            if _point_in_poly(x, y, z["polygon"]):
                return z
        return None

    def camera_role(self, camera_id: str) -> str | None:
        for cam in self.store["cameras"]:
            if cam["camera_id"] == camera_id:
                return cam["role"]
        return None
