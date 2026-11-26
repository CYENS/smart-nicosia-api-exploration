"""Where the sun is over Nicosia at a moment, and when it rises and sets.

The same formulas as SunCalc (https://github.com/mourner/suncalc), which the page uses for its
sky, its 3D light and the building shadows, so the server's shade and the map's shadows agree to
the degree. Angles out are degrees; the bearing is from north, clockwise, towards the sun.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from chatbot.settings import TIMEZONE

TZ = ZoneInfo(TIMEZONE)
RAD = math.pi / 180
DAY_MS = 86_400_000
J1970 = 2440588
J2000 = 2451545
OBLIQUITY = RAD * 23.4397


@dataclass(frozen=True)
class SunPosition:
    altitude: float  # degrees above the horizon (negative: below)
    bearing: float  # degrees from north, clockwise, towards the sun

    @property
    def up(self) -> bool:
        return self.altitude > 0


def _days(at: datetime) -> float:
    ms = at.timestamp() * 1000
    return ms / DAY_MS - 0.5 + J1970 - J2000


def position(at: datetime, lat: float, lon: float) -> SunPosition:
    """The sun's altitude and bearing at `at` (an aware datetime) seen from (lat, lon)."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=TZ)
    lw = RAD * -lon
    phi = RAD * lat
    d = _days(at)
    m = RAD * (357.5291 + 0.98560028 * d)  # solar mean anomaly
    c = RAD * (1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m))
    ecl = m + c + RAD * 102.9372 + math.pi  # ecliptic longitude
    dec = math.asin(math.sin(ecl) * math.sin(OBLIQUITY))
    ra = math.atan2(math.sin(ecl) * math.cos(OBLIQUITY), math.cos(ecl))
    h = RAD * (280.16 + 360.9856235 * d) - lw - ra  # hour angle
    alt = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(h))
    az = math.atan2(math.sin(h), math.cos(h) * math.sin(phi) - math.tan(dec) * math.cos(phi))
    # SunCalc's azimuth is from south, positive towards west.
    return SunPosition(alt / RAD, (az / RAD + 180) % 360)


def shadow_step(sun: SunPosition) -> tuple[float, float, float] | None:
    """How a shadow falls: (east, north) unit step towards the sun and the rise of the sun's
    ray per metre (tan of its altitude). None while the sun is down."""
    if sun.altitude <= 0:
        return None
    b = sun.bearing * RAD
    return math.sin(b), math.cos(b), math.tan(sun.altitude * RAD)


def day_moments(day: date, step_min: int = 15) -> list[datetime]:
    """Every `step_min` minutes of a local day, as aware datetimes (on the clock's quarter
    hours), from 00:00 to 23:45."""
    start = datetime.combine(day, time(0, 0), TZ)
    return [(start + timedelta(minutes=k)).astimezone(TZ)
            for k in range(0, 24 * 60, step_min)]


def sun_times(day: date, lat: float, lon: float) -> dict:
    """Sunrise, solar noon and sunset (local HH:MM) of a day, found by stepping the sun's
    altitude minute by minute around the crossings. Sunrise and sunset are the top of the sun
    crossing the horizon (altitude -0.833°, refraction included), as in almanacs."""
    start = datetime.combine(day, time(0, 0), TZ)

    def alt(minute: float) -> float:
        return position(start + timedelta(minutes=minute), lat, lon).altitude

    horizon = -0.833
    coarse = [(m, alt(m)) for m in range(0, 24 * 60 + 1, 10)]
    rise = set_ = None
    for (m1, a1), (m2, a2) in zip(coarse, coarse[1:]):
        if a1 < horizon <= a2 and rise is None:
            rise = m1 + (m2 - m1) * (horizon - a1) / (a2 - a1)
        elif a1 >= horizon > a2 and set_ is None:
            set_ = m1 + (m2 - m1) * (a1 - horizon) / (a1 - a2)
    noon_m, noon_alt = max(coarse, key=lambda p: p[1])

    def hhmm(minute: float | None) -> str | None:
        if minute is None:
            return None
        return (start + timedelta(minutes=round(minute))).astimezone(TZ).strftime("%H:%M")

    return {"sunrise": hhmm(rise), "solar_noon": hhmm(noon_m), "sunset": hhmm(set_),
            "noon_altitude_deg": round(noon_alt, 1),
            "daylight_h": round(((set_ or 0) - (rise or 0)) / 60, 1) if rise and set_ else 0.0}


def utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc)
