"""Replay device history for map time-lapses.

Past readings come from getTelemetryRange, then go through collector.interpret at evenly spaced
moments. So a frame says "on" or "full" by exactly the rules the live tools use, including lamp
night/day logic and staleness.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector import interpret, sun

from chatbot.city import DeviceState
from chatbot.settings import TIMEZONE

TZ = ZoneInfo(TIMEZONE)

# The keys a frame needs per device type: few, so a day of history is one request per device.
HISTORY_KEYS: dict[str, tuple[str, ...]] = {
    "Lighting": ("level", "powerstats"),
    "Parking": ("status", "vbatt"),  # vbatt is the ~4-hourly heartbeat that keeps status fresh
    "Wastebin": ("level",),
    "smartsense": ("caqi", "Sound_level", "PM2.5", "NO2"),
}
# Readings fetched from before the first frame, so the first frame knows each device's state.
# Lamps need a day to tell "offline" from "no data"; parking status changes only on arrivals.
LOOKBACK: dict[str, timedelta] = {
    "Lighting": timedelta(hours=24),
    "Parking": timedelta(hours=24),
    "Wastebin": timedelta(hours=6),
    "smartsense": timedelta(hours=3),
}
MAX_FRAMES = 200


def frame_times(now: datetime, hours: int, step_min: int) -> list[datetime]:
    """Moments every `step_min` minutes over the last `hours`, aligned to the step (18:00, 18:15)."""
    step = step_min * 60
    end = int(now.timestamp()) // step * step
    n = hours * 60 // step_min
    return [datetime.fromtimestamp(end - (n - k) * step, tz=timezone.utc) for k in range(n + 1)]


def replay(device_type: str, states: list[DeviceState], records: dict[str, list[dict]],
           times: list[datetime]) -> tuple[list[list[str]], dict[str, list[list[float | None]]]]:
    """Interpret every device at every moment.

    Returns (states[frame][device], values[field][frame][device]), where the fields are the
    numeric ones DeviceState.values() gives for the type. Stale readings (other than lamps, whose
    silence is meaningful) become "unknown" with no values, as the live tools treat them.
    """
    lamp_kwargs = []
    if device_type == "Lighting":
        for t in times:
            night = sun.is_night(t)
            lamp_kwargs.append({"night": night, "night_since": sun.night_started(t) if night else None})
    out_states: list[list[str]] = [[] for _ in times]
    out_values: dict[str, list[list[float | None]]] = {}
    for s in states:
        recs = sorted((int(r["timestamp"]), r["key"], r.get("value"))
                      for r in records.get(s.device.name, []))
        latest: dict[str, tuple[int, str | None]] = {}
        i = 0
        for k, t in enumerate(times):
            t_ms = t.timestamp() * 1000
            while i < len(recs) and recs[i][0] <= t_ms:
                latest[recs[i][1]] = (recs[i][0], recs[i][2])
                i += 1
            kwargs = lamp_kwargs[k] if lamp_kwargs else {}
            interp = interpret.interpret(device_type, latest, t, **kwargs)
            frame_state = DeviceState(s.device, s.label, s.street, interp)
            values = frame_state.values()
            state = interp.state
            if interp.stale and device_type != "Lighting":
                state, values = "unknown", dict.fromkeys(values)
            out_states[k].append(state)
            for field, v in values.items():
                rows = out_values.setdefault(field, [[] for _ in times])
                rows[k].append(round(v, 1) if isinstance(v, (int, float)) else None)
    return out_states, out_values


def frame_rows(device_type: str, times: list[datetime], states: list[list[str]],
               values: dict[str, list[list[float | None]]]) -> list[dict]:
    """One summary row per frame (counts per state and the headline average), for charts."""
    rows = []
    for k, t in enumerate(times):
        local = t.astimezone(TZ)
        counts = Counter(states[k])
        row: dict = {"frame": k, "time": local.strftime("%Y-%m-%dT%H:%M"),
                     "label": local.strftime("%a %H:%M"), **counts}

        def mean(field: str) -> float | None:
            vals = [v for v in values.get(field, [[]] * len(times))[k] if v is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        if device_type == "Lighting":
            watts = [v or 0 for v, st in zip(values["power_w"][k], states[k]) if st == "on"]
            row["lit_power_kw"] = round(sum(watts) / 1000, 2)
        elif device_type == "Parking":
            known = counts["occupied"] + counts["free"]
            row["occupancy_pct"] = round(100 * counts["occupied"] / known, 1) if known else None
        elif device_type == "Wastebin":
            row["avg_level_pct"] = mean("level_pct")
        elif device_type == "smartsense":
            row["avg_caqi"] = mean("caqi")
            row["avg_noise_db"] = mean("noise_db")
        rows.append(row)
    return rows
