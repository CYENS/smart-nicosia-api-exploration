"""Weather (Open-Meteo), traffic counts, city speed and alarms (Smart Nicosia).

Traffic, speed and alarm answers are handed to the twin's memory as they are fetched
(`city.record`), finished buckets only, so a day still counting never lands in the store.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector import parse
from collector.http import ApiError

from chatbot.city import CityData
from chatbot.settings import NICOSIA_CENTER, TIMEZONE, Settings
from chatbot.web import get_json

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)
HOUR_FMT = "%Y-%m-%dT%H:%M"  # a local hour as a chart and a time-lapse frame both read it
OBJECT_TYPES = {0: "people", 1: "bike", 2: "car", 3: "motorcycle", 4: "bus", 5: "truck"}
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 61: "light rain",
    63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    80: "light showers", 81: "showers", 82: "violent showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}
PERIODS = ("today", "yesterday", "last_7_days", "last_30_days")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# What the map draws the weather from (lib/weather on the page): cloud by layer, how far one
# sees, rain and snow, and the wind that carries them. Open-Meteo's names, now and by the hour.
SKY_FIELDS = ("cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,visibility,"
              "precipitation,rain,showers,snowfall,wind_speed_10m,wind_direction_10m,weather_code")
WET_HOURS = 3  # rain this long ago still leaves the streets wet
# The keys of the weather the map draws, as the strip passes them to the page.
MAP_KEYS = ("cloud_pct", "cloud_low_pct", "cloud_mid_pct", "cloud_high_pct", "visibility_m",
            "rain_mm", "snow_cm", "wet_mm", "wind_kmh", "wind_from_deg", "code", "dust_ugm3", "aod")


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _rain_mm(row: dict) -> float:
    """What fell in the row's hour, in mm. Open-Meteo's sum and its parts can disagree (0.0
    precipitation beside 0.2 of rain), so the larger."""
    parts = sum(v for v in (_num(row, "rain"), _num(row, "showers")) if v)
    return round(max(_num(row, "precipitation") or 0.0, parts), 2)


def map_weather(row: dict, wet_mm: float | None = None) -> dict:
    """The weather as the map draws it, from one Open-Meteo row (an hour, or now). `wet_mm` is
    what fell over the last WET_HOURS hours, for wet streets after the rain has stopped."""
    def whole(key: str) -> int | None:
        v = _num(row, key)
        return round(v) if v is not None else None

    rain = _rain_mm(row)
    return {
        "cloud_pct": whole("cloud_cover"), "cloud_low_pct": whole("cloud_cover_low"),
        "cloud_mid_pct": whole("cloud_cover_mid"), "cloud_high_pct": whole("cloud_cover_high"),
        "visibility_m": whole("visibility"), "rain_mm": rain,
        "snow_cm": _num(row, "snowfall") or 0.0,
        "wet_mm": round(max(wet_mm if wet_mm is not None else rain, rain), 2),
        "wind_kmh": _num(row, "wind_speed_10m"), "wind_from_deg": whole("wind_direction_10m"),
        "code": whole("weather_code"),
    }


def _rows(hourly: dict) -> list[dict]:
    """Open-Meteo's hourly columns as one dict per hour."""
    keys = [k for k in hourly if k != "time"]
    return [{"time": t, **{k: hourly[k][i] for k in keys}} for i, t in enumerate(hourly["time"])]


def _wet(rows: list[dict], i: int) -> float:
    """What fell over the WET_HOURS hours up to and including row i."""
    return round(sum(_rain_mm(r) for r in rows[max(0, i - WET_HOURS + 1):i + 1]), 2)


def weather(s: Settings, now: datetime | None = None) -> dict:
    lat, lon = NICOSIA_CENTER
    f = get_json(s.open_meteo_url, user_agent=s.user_agent, params={
        "latitude": lat, "longitude": lon, "timezone": TIMEZONE, "forecast_days": 2,
        "past_days": 1,  # the hours just gone, for streets still wet after rain
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,uv_index,is_day,"
                   + SKY_FIELDS,
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,uv_index,"
                  "weather_code,precipitation,rain,showers",
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,uv_index_max",
    }, timeout=20)
    a = get_json(s.air_quality_url, user_agent=s.user_agent, params={
        "latitude": lat, "longitude": lon, "timezone": TIMEZONE,
        "current": "european_aqi,pm10,pm2_5,dust,aerosol_optical_depth,ozone,nitrogen_dioxide",
        "hourly": "dust,european_aqi", "forecast_days": 1,
    }, timeout=20)
    cur = f["current"]
    now_local = (now or datetime.now(TZ)).astimezone(TZ).replace(minute=0, second=0, microsecond=0)
    hours = []
    h = f["hourly"]
    # The hour now falls in is the row at its end (Open-Meteo sums the hour before each time).
    rows = _rows(h)
    end = (now_local + timedelta(hours=1)).strftime(HOUR_FMT)
    i_now = next((i for i, r in enumerate(rows) if r["time"][:16] == end), None)
    sky = map_weather(cur, _wet(rows, i_now) if i_now is not None else None)
    if i_now is not None:  # rain falling now, as an hourly rate (`current` is a 15 min sum)
        sky["rain_mm"] = max(sky["rain_mm"], _rain_mm(rows[i_now]))
        sky["wet_mm"] = max(sky["wet_mm"], sky["rain_mm"])
    for i, t in enumerate(h["time"]):
        ts = datetime.fromisoformat(t).replace(tzinfo=TZ)
        if now_local <= ts < now_local + timedelta(hours=12):
            hours.append({
                "time": t[:16], "temp_c": h["temperature_2m"][i],
                "feels_like_c": h["apparent_temperature"][i],
                "rain_chance_pct": h["precipitation_probability"][i], "uv": h["uv_index"][i],
                "sky": WEATHER_CODES.get(h["weather_code"][i], str(h["weather_code"][i])),
            })
    d = f["daily"]
    return {
        "source": "Open-Meteo (model forecast for central Nicosia)",
        "now": {
            "temp_c": cur["temperature_2m"], "feels_like_c": cur["apparent_temperature"],
            "humidity_pct": cur["relative_humidity_2m"], "uv": cur["uv_index"],
            "sky": WEATHER_CODES.get(cur["weather_code"], str(cur["weather_code"])),
            "daylight": bool(cur["is_day"]),
            **sky,
            "dust_ugm3": a["current"].get("dust"), "aod": a["current"].get("aerosol_optical_depth"),
        },
        "next_12_hours": hours,
        "days": [{"date": d["time"][i], "sunrise": d["sunrise"][i][11:], "sunset": d["sunset"][i][11:],
                  "max_c": d["temperature_2m_max"][i], "min_c": d["temperature_2m_min"][i],
                  "uv_max": d["uv_index_max"][i]} for i in range(len(d["time"]))],
        "air_model": {
            "european_aqi": a["current"].get("european_aqi"), "pm10": a["current"].get("pm10"),
            "pm2_5": a["current"].get("pm2_5"), "dust_ugm3": a["current"].get("dust"),
            "ozone": a["current"].get("ozone"), "no2": a["current"].get("nitrogen_dioxide"),
            "note": "regional model (CAMS); the city's own stations are in find_devices(air_station)",
        },
    }


WEATHER_PAST_DAYS = 62


def weather_history(s: Settings) -> dict[str, dict]:
    """The modelled weather hour by hour over the last WEATHER_PAST_DAYS days (Open-Meteo), keyed
    by local hour ('2026-11-21T21:00'), for the city clock: the strip's word and temperature, and
    what the map draws (map_weather). One request covers every moment the clock can show, so
    callers cache it for an hour."""
    lat, lon = NICOSIA_CENTER
    f = get_json(s.open_meteo_url, user_agent=s.user_agent, params={
        "latitude": lat, "longitude": lon, "timezone": TIMEZONE, "forecast_days": 1,
        "past_days": WEATHER_PAST_DAYS, "hourly": "temperature_2m,is_day," + SKY_FIELDS,
    }, timeout=20)
    rows = _rows(f["hourly"])
    return {r["time"][:16]: {"temp_c": r["temperature_2m"],
                             "sky": WEATHER_CODES.get(r["weather_code"], str(r["weather_code"])),
                             "daylight": bool(r["is_day"]), **map_weather(r, _wet(rows, i))}
            for i, r in enumerate(rows) if r["temperature_2m"] is not None}


def dust_history(s: Settings) -> dict[str, dict]:
    """Saharan dust (CAMS, through Open-Meteo's air quality API) hour by hour over the same days,
    keyed by local hour: the dust near the ground and the aerosol optical depth, the haze it
    makes in the sky. One request, cached for an hour like weather_history."""
    lat, lon = NICOSIA_CENTER
    a = get_json(s.air_quality_url, user_agent=s.user_agent, params={
        "latitude": lat, "longitude": lon, "timezone": TIMEZONE, "forecast_days": 1,
        "past_days": WEATHER_PAST_DAYS, "hourly": "dust,aerosol_optical_depth",
    }, timeout=20)
    h = a["hourly"]
    return {t[:16]: {"dust_ugm3": h["dust"][i], "aod": h["aerosol_optical_depth"][i]}
            for i, t in enumerate(h["time"])
            if h["dust"][i] is not None or h["aerosol_optical_depth"][i] is not None}


def _period_bounds(period: str, now: datetime) -> tuple[datetime, datetime, list[date]]:
    local = now.astimezone(TZ)
    today = local.date()
    midnight = datetime(today.year, today.month, today.day, tzinfo=TZ)
    if period == "today":
        return midnight, local, [today]
    if period == "yesterday":
        y = today - timedelta(days=1)
        return midnight - timedelta(days=1), midnight - timedelta(milliseconds=1), [y]
    days = 7 if period == "last_7_days" else 30
    first = today - timedelta(days=days - 1)
    return midnight - timedelta(days=days - 1), local, [first + timedelta(i) for i in range(days)]


def finished(rows: list[parse.TrafficRow], now: datetime) -> list[parse.TrafficRow]:
    """Only the buckets that have ended: a day or hour still counting isn't a figure to keep."""
    length = {"day": timedelta(days=1), "hour": timedelta(hours=1)}
    return [r for r in rows if r.bucket_start + length[r.granularity] <= now]


def cameras_matching(city: CityData, camera: str | None) -> list[dict]:
    """The traffic cameras whose name contains `camera`; every camera without one."""
    cams = city.traffic_cameras()
    if not camera:
        return cams
    q = camera.lower()
    return [c for c in cams if q in (c["name"] or "").lower()
            or q in (c.get("stream_name") or "").lower()]


def traffic_camera_hours(city: CityData, period: str, camera: str | None) -> list[dict]:
    """Each camera's counts hour by hour over one local day ('today' or 'yesterday'), for
    replaying the day on the map: [{"camera", "lat", "lon", "hours": {"2026-10-19T13:00":
    {"people": 12, "car": 3, "total": 15}}}]. One small request per camera, in parallel.
    Analytics that share a camera's name are one camera and are summed; cameras that counted
    nothing that day, or whose request failed, are left out."""
    start, end, days = _period_bounds(period, city.now())

    def one(cam: dict):
        try:
            payload = city.client.get_json(
                "getTrafficReports", group_by="hour", start_date=_ms(start), end_date=_ms(end),
                va_ids=json.dumps([cam["analytics_id"]], separators=(",", ":")))
        except ApiError as e:
            log.warning("hourly traffic for %s failed: %s", cam.get("name"), e)
            return cam, []
        return cam, parse.parse_traffic_buckets(payload, TZ, day=days[0],
                                                analytics_id=cam["analytics_id"])

    merged: dict[str, dict] = {}
    results = city.each(one, cameras_matching(city, camera))
    city.record("traffic", finished([r for _, rows in results for r in rows], city.now()))
    for cam, rows in results:
        if not rows:
            continue
        m = merged.setdefault(cam["name"] or str(cam["analytics_id"]), {
            "camera": cam["name"] or str(cam["analytics_id"]), "lat": None, "lon": None,
            "hours": defaultdict(lambda: defaultdict(int))})
        if m["lat"] is None:
            m["lat"], m["lon"] = cam.get("lat"), cam.get("lon")
        for r in rows:
            counts = m["hours"][r.bucket_start.strftime(HOUR_FMT)]
            counts[OBJECT_TYPES.get(r.object_type, str(r.object_type))] += r.total or 0
            counts["total"] += r.total or 0
    return [{**m, "hours": {h: dict(c) for h, c in sorted(m["hours"].items())}}
            for m in merged.values()]


def traffic(city: CityData, period: str, group_by: str, camera: str | None) -> dict:
    """Counts of people, bikes, cars, motorcycles, buses and trucks from the video analytics."""
    start, end, days = _period_bounds(period, city.now())
    cams = city.traffic_cameras()
    chosen = []
    if camera:
        chosen = cameras_matching(city, camera)
        if not chosen:
            return {"error": f"no camera matches {camera!r}",
                    "cameras": sorted({c["name"] for c in cams})}
    # Compact separators: the API fails with HTTP 500 on "[111, 102]" but takes "[111,102]".
    va_ids = json.dumps([c["analytics_id"] for c in chosen], separators=(",", ":"))
    params = {"va_ids": va_ids, "start_date": _ms(start), "end_date": _ms(end)}

    if group_by == "camera":
        payload = city.client.get_json("getTrafficReports", group_by="stream", **params)
        rows = parse.parse_traffic_stream(payload, TZ)
        city.record("traffic", finished(rows, city.now()))  # per camera, line and day
        per: dict[int, dict] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            per[r.analytics_id][OBJECT_TYPES.get(r.object_type, str(r.object_type))] += r.total or 0
        by_id = {c["analytics_id"]: c for c in cams}
        out = []
        for va, counts in per.items():
            c = by_id.get(va, {})
            out.append({"camera": c.get("name") or str(va), "lat": c.get("lat"),
                        "lon": c.get("lon"), **counts, "total": sum(counts.values())})
        out.sort(key=lambda r: -r["total"])
        return {"period": period, "group_by": "camera", "rows": out,
                "note": "counts are line crossings, so one person can be counted at several "
                        "cameras; cameras without coordinates have lat/lon null"}

    if group_by == "hour_of_day":
        if period in ("today", "yesterday"):
            payload = city.client.get_json("getTrafficReports", group_by="hour", **params)
            rows = parse.parse_traffic_buckets(payload, TZ, day=days[0])
            if len(chosen) <= 1:  # the city as a whole, or one camera: both worth keeping
                own = chosen[0]["analytics_id"] if chosen else parse.ALL_CAMERAS
                city.record("traffic", finished(
                    [dataclasses.replace(r, analytics_id=own) for r in rows], city.now()))
            label = "hour"
            note = None
            # A real local timestamp, so charts get a time axis that can sync with a time-lapse.
            key = lambda r: r.bucket_start.strftime(HOUR_FMT)  # noqa: E731
        else:
            payload = city.client.get_json("getTrafficReports", group_by="hour", **params)
            rows = parse.parse_traffic_buckets(payload, TZ, day=days[-1])
            label = "hour_of_day"
            note = f"each hour is the sum over {len(days)} days (an hour-of-day profile)"
            key = lambda r: r.bucket_start.strftime("%H:%M")  # noqa: E731
    else:
        payload = city.client.get_json("getTrafficReports", group_by="day", **params)
        rows = parse.parse_traffic_buckets(payload, TZ)
        if not chosen:  # per-camera days are stored per counting line, from group_by=stream
            city.record("traffic", finished(rows, city.now()))
        label, note = "day", None
        key = lambda r: r.bucket_start.strftime("%Y-%m-%d")  # noqa: E731

    table: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        table[key(r)][OBJECT_TYPES.get(r.object_type, str(r.object_type))] += r.total or 0
    out = [{label: k, **v, "total": sum(v.values())} for k, v in sorted(table.items())]
    result = {"period": period, "group_by": label,
              "cameras": [c["name"] for c in chosen] or "all cameras", "rows": out}
    if note:
        result["note"] = note
    return result


def speed(city: CityData, hours: int) -> dict:
    """City-wide average speed per hour, now and the same hours a week ago."""
    now = city.now()
    start = now - timedelta(hours=hours)

    def fetch(a: datetime, b: datetime):
        payload = city.client.get_json(
            "generalReports", group_by="ONE_HOUR", line_type="AVERAGE_SPEED",
            start_date=_ms(a), end_date=_ms(b),
        )
        rows = parse.parse_speed(payload, TZ)
        city.record("speeds", [r for r in rows if r.bucket_start + timedelta(hours=1) <= now],
                    "ONE_HOUR", "AVERAGE_SPEED")
        return rows

    current = fetch(start, now)
    week_ago = {r.bucket_start + timedelta(days=7): r.value
                for r in fetch(start - timedelta(days=7), now - timedelta(days=7))}
    rows = [{"hour": r.bucket_start.strftime("%Y-%m-%dT%H:%M"), "speed_kmh": r.value,
             "same_hour_last_week_kmh": week_ago.get(r.bucket_start)} for r in current]
    return {"source": "Smart Nicosia speed cameras (one city-wide average, no per-street speed)",
            "rows": rows}


def alarms(city: CityData, limit: int) -> dict:
    data = city.client.get_json("getAlarms", entityType="TENANT", entityName="CYTA",
                                state="ACTIVE_UNACK", limit=min(limit, 20))
    city.record("alarms", {"ACTIVE_UNACK": data or []})
    by_name = {d.name: d for d in city.devices()}
    out = []
    for a in data or []:
        p = parse.parse_alarm(a)
        d = by_name.get(p["device"])
        start = p["start_ms"] or p["created_ms"]
        out.append({
            "device": p["device"], "device_type": p["device_type"], "alarm": p["type"],
            "severity": p["severity"],
            "since": datetime.fromtimestamp(start / 1000, tz=timezone.utc).astimezone(TZ)
            .strftime("%Y-%m-%d %H:%M") if start else None,
            "lat": d.lat if d else None, "lon": d.lon if d else None,
        })
    return {"note": "the newest active, unacknowledged alarms (the API returns at most 20)",
            "alarms": out}
