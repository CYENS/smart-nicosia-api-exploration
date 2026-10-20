"""Weather (Open-Meteo), traffic counts, city speed and alarms (Smart Nicosia)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collector import parse

from chatbot.city import CityData
from chatbot.settings import NICOSIA_CENTER, TIMEZONE, Settings
from chatbot.web import get_json

TZ = ZoneInfo(TIMEZONE)
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


def weather(s: Settings) -> dict:
    lat, lon = NICOSIA_CENTER
    f = get_json(s.open_meteo_url, user_agent=s.user_agent, params={
        "latitude": lat, "longitude": lon, "timezone": TIMEZONE, "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
                   "weather_code,wind_speed_10m,uv_index,is_day",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,uv_index,"
                  "weather_code",
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,uv_index_max",
    }, timeout=20)
    a = get_json(s.air_quality_url, user_agent=s.user_agent, params={
        "latitude": lat, "longitude": lon, "timezone": TIMEZONE,
        "current": "european_aqi,pm10,pm2_5,dust,ozone,nitrogen_dioxide",
        "hourly": "dust,european_aqi", "forecast_days": 1,
    }, timeout=20)
    cur = f["current"]
    now_local = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    hours = []
    h = f["hourly"]
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
            "humidity_pct": cur["relative_humidity_2m"], "rain_mm": cur["precipitation"],
            "wind_kmh": cur["wind_speed_10m"], "uv": cur["uv_index"],
            "sky": WEATHER_CODES.get(cur["weather_code"], str(cur["weather_code"])),
            "daylight": bool(cur["is_day"]),
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


def traffic(city: CityData, period: str, group_by: str, camera: str | None) -> dict:
    """Counts of people, bikes, cars, motorcycles, buses and trucks from the video analytics."""
    start, end, days = _period_bounds(period, city.now())
    cams = city.traffic_cameras()
    chosen = []
    if camera:
        q = camera.lower()
        chosen = [c for c in cams if q in (c["name"] or "").lower()
                  or q in (c.get("stream_name") or "").lower()]
        if not chosen:
            return {"error": f"no camera matches {camera!r}",
                    "cameras": sorted({c["name"] for c in cams})}
    va_ids = json.dumps([c["analytics_id"] for c in chosen])
    params = {"va_ids": va_ids, "start_date": _ms(start), "end_date": _ms(end)}

    if group_by == "camera":
        payload = city.client.get_json("getTrafficReports", group_by="stream", **params)
        rows = parse.parse_traffic_stream(payload, TZ)
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
            label = "hour"
            note = None
            # A real local timestamp, so charts get a time axis that can sync with a time-lapse.
            key = lambda r: r.bucket_start.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
        else:
            payload = city.client.get_json("getTrafficReports", group_by="hour", **params)
            rows = parse.parse_traffic_buckets(payload, TZ, day=days[-1])
            label = "hour_of_day"
            note = f"each hour is the sum over {len(days)} days (an hour-of-day profile)"
            key = lambda r: r.bucket_start.strftime("%H:%M")  # noqa: E731
    else:
        payload = city.client.get_json("getTrafficReports", group_by="day", **params)
        rows = parse.parse_traffic_buckets(payload, TZ)
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
        return parse.parse_speed(payload, TZ)

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
