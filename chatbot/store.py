"""Per-conversation storage for map layers and datasets.

Data tools put big results here and hand the model a short reference (`layer_ref`, `data_ref`).
The browser fetches layers by reference, and `show_chart` fills a chart's data from a dataset,
so neither the GeoJSON nor the numbers pass through the model.
"""

from __future__ import annotations

import itertools
import re
import threading

# Colours per state, shared with the frontend legend. Chosen for contrast on light and dark maps.
STATE_COLORS: dict[str, str] = {
    "on": "#f5b700", "off": "#64748b", "offline": "#dc2626", "unknown": "#a3a3a3",
    "free": "#16a34a", "occupied": "#dc2626",
    "low": "#16a34a", "medium": "#eab308", "high": "#f97316", "full": "#b91c1c",
    "very_low": "#16a34a",  "very_high": "#7e22ce",
    "connected": "#2563eb", "disconnected": "#dc2626",
    "ok": "#16a34a", "no_heartbeat": "#f97316", "not_reported": "#dc2626",
    "place": "#7c3aed", "stop": "#0f766e", "route": "#2563eb", "alarm": "#dc2626",
    "camera": "#0ea5e9",
    "dangerous": "#dc2626", "needs_repair": "#f59e0b",
}


class SessionStore:
    def __init__(self) -> None:
        self.layers: dict[str, dict] = {}
        self.datasets: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _ref(self, prefix: str, hint: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", hint.lower()).strip("_")[:24] or "item"
        with self._lock:
            return f"{prefix}{next(self._ids)}_{slug}"

    def add_layer(self, hint: str, features: list[dict], *, geometry: str,
                  title: str, legend: dict[str, str] | None = None,
                  value_fields: list[str] | None = None, frames: dict | None = None) -> str:
        """`frames` makes a time-lapse layer: per-frame states and values for each feature."""
        ref = self._ref("L", hint)
        self.layers[ref] = {
            "type": "FeatureCollection",
            "features": features,
            "meta": {"ref": ref, "title": title, "geometry": geometry,
                     "legend": legend or {}, "value_fields": value_fields or [],
                     "count": len(features), "frames": len(frames["times"]) if frames else 0},
        }
        if frames:
            self.layers[ref]["frames"] = frames
        return ref

    def add_dataset(self, hint: str, rows: list[dict], *, title: str,
                    link: dict | None = None) -> str:
        """`link` names the field that ties a row to the map: {"key": "id"} matches feature ids,
        {"key": "frame", "layer": ref} matches a time-lapse frame. Charts use it for highlights."""
        ref = self._ref("D", hint)
        self.datasets[ref] = {"title": title, "rows": rows, "link": link}
        return ref


def point(lon: float, lat: float, props: dict) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props}


def legend_for(states: list[str]) -> dict[str, str]:
    return {s: STATE_COLORS.get(s, STATE_COLORS["unknown"]) for s in dict.fromkeys(states)}
