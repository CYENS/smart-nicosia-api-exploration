"""Turn raw Smart Nicosia telemetry into meaning, with a staleness flag.

This is the one place that knows what the raw values mean, so the collector, the chatbot's
tools, alerts and dashboards all give the same answer. Every verdict says how old its newest
reading is and whether it is stale.

Rules (checked against the live API on 19 Oct 2026, see docs/digital-twin-research.md §2.2):

- Lamps report only while powered (hourly; a few report every 5 minutes) and are silent by day,
  so a lamp's "latest" values can be from last night. A lamp is **on** only if it reported in the
  last ~70 minutes with a level above 0. Silent by day means off; silent at night means offline.
  Lamps report `Dimmed`/`100`/`True` when lit and `Online`/`0`/`False` when powered but dark.
- Parking `status` 1 = occupied, 0 = free (still to be confirmed with the vendor).
- Bin `level` is fill %. The city raises an alarm above 80 %.
- smartsense `caqi` is on the CAQI scale (0-25 very low ... >100 very high).
- Online/offline compares `lastActivityTime` with the device's inactivity timeout.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from collector import sun

# key -> (timestamp in ms, raw value)
Latest = Mapping[str, tuple[int, str | None]]

LAMP_ON_WINDOW = timedelta(minutes=70)

# How long a device may stay silent before its readings count as stale. Lamps and parking match
# the platform's own `inactivityTimeout`; the others are about two missed reports.
STALE_AFTER: dict[str, timedelta] = {
    "Lighting": LAMP_ON_WINDOW,
    "Parking": timedelta(hours=5, minutes=15),  # sensors heartbeat every ~4 h
    "Wastebin": timedelta(hours=5),  # bins report every 2 h
    "smartsense": timedelta(hours=2),  # stations report on the hour
    "InfoPoints": timedelta(hours=2),  # screens report every ~50 min
    "Camera": timedelta(hours=1),  # connectionState every 15 min
    "ParkingLot": timedelta(hours=1),
}

# Keys that carry readings. `ts`, `active` and `isAlert` also change when the platform marks a
# device inactive, so they are not evidence that the device reported.
READING_KEYS: dict[str, tuple[str, ...]] = {
    "Lighting": ("state", "level", "last_lamp_status", "powerstats"),
    "Parking": ("status", "is_pwd", "vbatt", "temperature"),
    "Wastebin": (
        "level",
        "temperature",
        "vbatt",
        "pickup_time",
        "level_before_pickup",
        "level_after_pickup",
    ),
    "smartsense": (
        "caqi",
        "caqi_pollutant",
        "PM1",
        "PM2.5",
        "PM10",
        "NO",
        "NO2",
        "O3",
        "CO",
        "CO2",
        "SO2",
        "T",
        "RH",
        "P",
        "Sound_level",
        "eaqi-traffic",
        "eaqi-industrial",
        "eaqi-background",
        "eaqi-traffic_pollutant",
        "eaqi-industrial_pollutant",
        "eaqi-background_pollutant",
        "PM2.5_aqi",
        "PM10_aqi",
        "NO2_aqi",
        "O3_aqi",
        "SO2_aqi",
    ),
    "InfoPoints": ("state", "planState", "Active", "enabled"),
    "Camera": ("connectionState",),
    "ParkingLot": (
        "available_generic_positions",
        "occupied_generic_positions",
        "available_pwd_positions",
        "occupied_pwd_positions",
    ),
}

BIN_BANDS = ((80.0, "full"), (60.0, "high"), (30.0, "medium"), (0.0, "low"))
CAQI_BANDS = ((75.0, "high"), (50.0, "medium"), (25.0, "low"))  # >100 is very_high


@dataclass(frozen=True)
class Interpretation:
    device_type: str
    state: str
    stale: bool
    as_of: datetime | None  # newest reading (or sign of life) the verdict rests on
    age_s: float | None
    reason: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat() if self.as_of else None
        return d


# ---------------------------------------------------------------------------------------------
# Helpers


def ms_to_dt(ms: int | float | str | None) -> datetime | None:
    if ms in (None, ""):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def latest_from_records(records: Iterable[Mapping]) -> dict[str, tuple[int, str | None]]:
    """API records [{key, timestamp, value}, ...] -> {key: (ts_ms, value)}, newest per key."""
    out: dict[str, tuple[int, str | None]] = {}
    for r in records:
        ts = int(r["timestamp"])
        if r["key"] not in out or ts > out[r["key"]][0]:
            out[r["key"]] = (ts, r.get("value"))
    return out


def to_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def to_bool(value: object) -> bool | None:
    s = str(value).strip().lower() if value is not None else ""
    return {"true": True, "false": False, "1": True, "0": False}.get(s)


def _value(latest: Latest, key: str) -> str | None:
    item = latest.get(key)
    if item is None or item[1] in (None, "None", "n/a", ""):
        return None
    return item[1]


def _num(latest: Latest, key: str) -> float | None:
    return to_number(_value(latest, key))


def _newest(latest: Latest, keys: Iterable[str]) -> int | None:
    stamps = [latest[k][0] for k in keys if k in latest]
    return max(stamps) if stamps else None


def _as_ms(value: datetime | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(value)


def _freshness(
    device_type: str,
    latest: Latest,
    now: datetime,
    last_activity: datetime | int | None,
    stale_after: timedelta | None,
) -> tuple[datetime | None, float | None, bool]:
    stamps = [s for s in (_newest(latest, READING_KEYS[device_type]), _as_ms(last_activity)) if s]
    if not stamps:
        return None, None, True
    as_of = ms_to_dt(max(stamps))
    age = (now - as_of).total_seconds()
    limit = stale_after or STALE_AFTER[device_type]
    return as_of, age, age > limit.total_seconds()


def _band(value: float | None, bands: tuple[tuple[float, str], ...], floor: str) -> str | None:
    if value is None or value < 0:
        return None
    for threshold, name in bands:
        if value >= threshold:
            return name
    return floor


def bin_band(level: float | None) -> str | None:
    """Fill % -> low (<30), medium (30-59), high (60-79), full (>=80, the city's alarm level)."""
    return _band(level, BIN_BANDS, "low")


def caqi_band(caqi: float | None) -> str | None:
    """CAQI -> very_low (<25), low (25-49), medium (50-74), high (75-100), very_high (>100)."""
    if caqi is None or caqi < 0:
        return None
    if caqi > 100:
        return "very_high"
    return _band(caqi, CAQI_BANDS, "very_low")


def lamp_lit(level: float | None, lamp_status: bool | None, state: str | None) -> bool | None:
    if level is not None:
        return level > 0
    if lamp_status is not None:
        return lamp_status
    if state is not None:
        return state == "Dimmed"
    return None


def _powerstats(latest: Latest) -> dict:
    raw = _value(latest, "powerstats")
    if not raw:
        return {}
    try:
        p = json.loads(raw)
    except ValueError:
        return {}
    return {
        "power_w": to_number(p.get("last_pwr_act_avg", p.get("last_pwr_act"))),
        "voltage_v": to_number(p.get("last_volt_avg", p.get("last_volt"))),
        "power_factor_pct": to_number(p.get("last_pf")),
        "energy_act_counter": to_number(p.get("last_energy_act")),  # cumulative, unit unconfirmed
    }


# ---------------------------------------------------------------------------------------------
# Interpreters, one per device type


def lamp(
    latest: Latest,
    now: datetime,
    *,
    night: bool | None = None,
    night_since: datetime | None = None,
    on_window: timedelta = LAMP_ON_WINDOW,
    lat: float = sun.NICOSIA[0],
    lon: float = sun.NICOSIA[1],
) -> Interpretation:
    """on / off / offline / unknown. `night` defaults to the sun's position at `now`."""
    if night is None:
        night = sun.is_night(now, lat, lon)
        night_since = sun.night_started(now, lat, lon) if night else None

    newest = _newest(latest, READING_KEYS["Lighting"])
    level = _num(latest, "level")
    lamp_status = to_bool(_value(latest, "last_lamp_status"))
    state_raw = _value(latest, "state")
    detail = {
        "level": level,
        "state_raw": state_raw,
        "last_lamp_status": lamp_status,
        "night": night,
        **_powerstats(latest),
    }
    if newest is None:
        return Interpretation("Lighting", "unknown", True, None, None, "no lamp readings", detail)

    as_of = ms_to_dt(newest)
    age = now - as_of
    age_s = age.total_seconds()
    if age <= on_window:
        lit = lamp_lit(level, lamp_status, state_raw)
        if lit:
            return Interpretation(
                "Lighting", "on", False, as_of, age_s,
                f"reported {_fmt(age)} ago with level {_fmt_num(level)}", detail,
            )
        if lit is False:
            return Interpretation(
                "Lighting", "off", False, as_of, age_s,
                "reporting, but level 0: powered and dark", detail,
            )
        return Interpretation(
            "Lighting", "unknown", False, as_of, age_s, "recent report without a level", detail
        )

    if not night:
        return Interpretation(
            "Lighting", "off", True, as_of, age_s,
            f"silent for {_fmt(age)} by day: lamps report only while powered", detail,
        )
    if night_since is not None and as_of < night_since and now - night_since <= on_window:
        return Interpretation(
            "Lighting", "unknown", True, as_of, age_s,
            f"night began {_fmt(now - night_since)} ago; no report yet tonight", detail,
        )
    return Interpretation(
        "Lighting", "offline", True, as_of, age_s,
        f"silent for {_fmt(age)} at night (lit lamps report at least hourly)", detail,
    )


def parking(
    latest: Latest,
    now: datetime,
    *,
    last_activity: datetime | int | None = None,
    stale_after: timedelta | None = None,
) -> Interpretation:
    """occupied / free / unknown. Any report (status or heartbeat) keeps the status fresh."""
    as_of, age_s, stale = _freshness("Parking", latest, now, last_activity, stale_after)
    status = _value(latest, "status")
    state = {"1": "occupied", "0": "free"}.get(str(status), "unknown")
    detail = {
        "status_raw": status,
        "status_since": ms_to_dt(latest["status"][0]).isoformat() if "status" in latest else None,
        "is_pwd": to_bool(_value(latest, "is_pwd")),  # bay reserved for disabled drivers
        "vbatt": _num(latest, "vbatt"),
        "temperature_c": _num(latest, "temperature"),
    }
    reason = "status 1 = occupied, 0 = free (vendor confirmation pending)"
    if stale:
        reason += f"; no sign of life for {_fmt_s(age_s)}" if age_s is not None else "; no data"
    return Interpretation("Parking", state, stale, as_of, age_s, reason, detail)


def bin_fill(
    latest: Latest,
    now: datetime,
    *,
    last_activity: datetime | int | None = None,
    stale_after: timedelta | None = None,
) -> Interpretation:
    """low / medium / high / full / unknown from fill level %."""
    as_of, age_s, stale = _freshness("Wastebin", latest, now, last_activity, stale_after)
    level = _num(latest, "level")
    band = bin_band(level) or "unknown"
    pickup = _num(latest, "pickup_time")
    detail = {
        "level_pct": level,
        "temperature_c": _num(latest, "temperature"),
        "vbatt": _num(latest, "vbatt"),
        "last_pickup": ms_to_dt(pickup).isoformat() if pickup else None,
        "level_before_pickup": _num(latest, "level_before_pickup"),
        "level_after_pickup": _num(latest, "level_after_pickup"),
    }
    reason = f"fill {_fmt_num(level)} %"
    if stale:
        reason += f"; no report for {_fmt_s(age_s)}" if age_s is not None else "; no data"
    return Interpretation("Wastebin", band, stale, as_of, age_s, reason, detail)


def air_quality(
    latest: Latest,
    now: datetime,
    *,
    last_activity: datetime | int | None = None,
    stale_after: timedelta | None = None,
) -> Interpretation:
    """CAQI band (very_low ... very_high) plus the main readings."""
    as_of, age_s, stale = _freshness("smartsense", latest, now, last_activity, stale_after)
    caqi = _num(latest, "caqi")
    band = caqi_band(caqi) or "unknown"
    detail = {
        "caqi": caqi,
        "dominant_pollutant": _value(latest, "caqi_pollutant"),
        "pm1": _num(latest, "PM1"),
        "pm2_5": _num(latest, "PM2.5"),
        "pm10": _num(latest, "PM10"),
        "no2": _num(latest, "NO2"),
        "o3": _num(latest, "O3"),
        "so2": _num(latest, "SO2"),
        "co": _num(latest, "CO"),
        "co2_ppm": _num(latest, "CO2"),
        "temperature_c": _num(latest, "T"),
        "humidity_pct": _num(latest, "RH"),
        "pressure_kpa": _num(latest, "P"),
        "noise_db": _num(latest, "Sound_level"),
        "sub_indices": {
            k.removesuffix("_aqi"): _num(latest, k)
            for k in ("PM2.5_aqi", "PM10_aqi", "NO2_aqi", "O3_aqi", "SO2_aqi")
            if _num(latest, k) is not None
        },
    }
    reason = f"CAQI {_fmt_num(caqi)}"
    if detail["dominant_pollutant"]:
        reason += f", driven by {detail['dominant_pollutant']}"
    if stale:
        reason += f"; no report for {_fmt_s(age_s)}" if age_s is not None else "; no data"
    return Interpretation("smartsense", band, stale, as_of, age_s, reason, detail)


def infopoint(
    latest: Latest,
    now: datetime,
    *,
    last_activity: datetime | int | None = None,
    stale_after: timedelta | None = None,
) -> Interpretation:
    """ok / no_heartbeat / not_reported / ... from the player's health `state`."""
    as_of, age_s, stale = _freshness("InfoPoints", latest, now, last_activity, stale_after)
    raw = _value(latest, "state")
    state = {
        "HEALTHY": "ok",
        "HEARTBEAT_OVERDUE": "no_heartbeat",
        "NOT_REPORTED": "not_reported",
    }.get(raw or "", (raw or "unknown").lower())
    detail = {"state_raw": raw, "plan_state": _value(latest, "planState")}
    return Interpretation("InfoPoints", state, stale, as_of, age_s, f"player state {raw}", detail)


def camera(
    latest: Latest,
    now: datetime,
    *,
    last_activity: datetime | int | None = None,
    stale_after: timedelta | None = None,
) -> Interpretation:
    """connected / disconnected / ... from `connectionState`."""
    as_of, age_s, stale = _freshness("Camera", latest, now, last_activity, stale_after)
    raw = _value(latest, "connectionState")
    state = (raw or "unknown").lower()
    return Interpretation(
        "Camera", state, stale, as_of, age_s, f"connectionState {raw}", {"connection_raw": raw}
    )


def parking_lot(
    latest: Latest,
    now: datetime,
    *,
    last_activity: datetime | int | None = None,
    stale_after: timedelta | None = None,
) -> Interpretation:
    """available / full from bay counts. These lots stopped reporting in Feb 2023."""
    as_of, age_s, stale = _freshness("ParkingLot", latest, now, last_activity, stale_after)
    free = _num(latest, "available_generic_positions")
    detail = {
        "available": free,
        "occupied": _num(latest, "occupied_generic_positions"),
        "available_pwd": _num(latest, "available_pwd_positions"),
        "occupied_pwd": _num(latest, "occupied_pwd_positions"),
    }
    state = "unknown" if free is None else ("available" if free > 0 else "full")
    return Interpretation("ParkingLot", state, stale, as_of, age_s, f"{free} bays free", detail)


INTERPRETERS = {
    "Lighting": lamp,
    "Parking": parking,
    "Wastebin": bin_fill,
    "smartsense": air_quality,
    "InfoPoints": infopoint,
    "Camera": camera,
    "ParkingLot": parking_lot,
}


def interpret(device_type: str, latest: Latest, now: datetime, **kwargs) -> Interpretation:
    """Dispatch to the interpreter for `device_type`."""
    try:
        fn = INTERPRETERS[device_type]
    except KeyError:
        raise ValueError(f"No interpreter for device type {device_type!r}") from None
    return fn(latest, now, **kwargs)


def online_status(
    device_type: str,
    last_activity: datetime | int | None,
    now: datetime,
    *,
    timeout: timedelta | None = None,
    night: bool | None = None,
) -> Interpretation:
    """online / offline / unpowered / unknown from `lastActivityTime`.

    `timeout` is the device's own `inactivityTimeout` attribute when known. Lamps are judged by the
    70-minute rule instead, and a silent lamp by day is `unpowered`, not offline.
    """
    seen = ms_to_dt(_as_ms(last_activity))
    if seen is None:
        return Interpretation(device_type, "unknown", True, None, None, "never seen")
    age = now - seen
    age_s = age.total_seconds()
    if device_type == "Lighting":
        limit = LAMP_ON_WINDOW
        if age > limit:
            if night is None:
                night = sun.is_night(now)
            if not night:
                return Interpretation(
                    device_type, "unpowered", True, seen, age_s,
                    f"silent for {_fmt(age)} by day; lamps are unpowered by day",
                )
    else:
        limit = timeout or STALE_AFTER.get(device_type, timedelta(hours=2))
    if age <= limit:
        return Interpretation(device_type, "online", False, seen, age_s, f"active {_fmt(age)} ago")
    return Interpretation(
        device_type, "offline", True, seen, age_s,
        f"inactive for {_fmt(age)} (timeout {_fmt(limit)})",
    )


# ---------------------------------------------------------------------------------------------
# Categorical state of a single value, for the state_changes table

TRACKED_KEYS: dict[str, tuple[str, ...]] = {
    "Parking": ("status",),
    "Lighting": ("level",),
    "Wastebin": ("level",),
    "smartsense": ("caqi",),
    "Camera": ("connectionState",),
    "InfoPoints": ("state",),
}


def classify(device_type: str | None, key: str, value: str | None) -> str | None:
    """Categorical meaning of one raw value, or None if the key isn't tracked or is unreadable."""
    if device_type not in TRACKED_KEYS or key not in TRACKED_KEYS[device_type]:
        return None
    if value in (None, "None", "n/a", ""):
        return None
    if device_type == "Parking":
        return {"1": "occupied", "0": "free"}.get(str(value))
    if device_type == "Lighting":
        lvl = to_number(value)
        return None if lvl is None else ("lit" if lvl > 0 else "dark")
    if device_type == "Wastebin":
        return bin_band(to_number(value))
    if device_type == "smartsense":
        return caqi_band(to_number(value))
    return str(value).lower()


# ---------------------------------------------------------------------------------------------


def _fmt(delta: timedelta) -> str:
    return _fmt_s(delta.total_seconds())


def _fmt_s(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} days"


def _fmt_num(x: float | None) -> str:
    if x is None:
        return "?"
    return str(int(x)) if x == int(x) else f"{x:g}"
