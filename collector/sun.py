"""Solar elevation, sunrise and sunset (NOAA approximation, accurate to a minute or two).

Nicosia's street lamps are powered from about sunset to shortly before sunrise, so "is it night?"
decides whether a silent lamp is off (day) or offline (night).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

NICOSIA = (35.1725, 33.3650)
# Geometric horizon plus refraction and the sun's radius: the "official" sunrise/sunset.
SUNSET_ELEVATION_DEG = -0.833


def solar_elevation(when: datetime, lat: float = NICOSIA[0], lon: float = NICOSIA[1]) -> float:
    """Sun elevation above the horizon in degrees at instant `when` (timezone-aware)."""
    t = when.astimezone(timezone.utc)
    day_of_year = t.timetuple().tm_yday
    hours = t.hour + t.minute / 60 + t.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hours - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_minutes = hours * 60 + eqtime + 4 * lon
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat_r = math.radians(lat)
    cos_zenith = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(
        hour_angle
    )
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def is_night(when: datetime, lat: float = NICOSIA[0], lon: float = NICOSIA[1]) -> bool:
    """True between sunset and sunrise."""
    return solar_elevation(when, lat, lon) < SUNSET_ELEVATION_DEG


def last_crossing(
    when: datetime,
    lat: float = NICOSIA[0],
    lon: float = NICOSIA[1],
    *,
    max_hours: float = 20,
) -> datetime | None:
    """The most recent sunrise or sunset at or before `when` (to within a few seconds)."""
    night = is_night(when, lat, lon)
    step = timedelta(minutes=10)
    later = when
    earlier = when - step
    limit = when - timedelta(hours=max_hours)
    while is_night(earlier, lat, lon) == night:
        later = earlier
        earlier -= step
        if earlier < limit:
            return None
    # Bisect between `earlier` (other phase) and `later` (same phase as `when`).
    while later - earlier > timedelta(seconds=5):
        mid = earlier + (later - earlier) / 2
        if is_night(mid, lat, lon) == night:
            later = mid
        else:
            earlier = mid
    return later


def night_started(
    when: datetime, lat: float = NICOSIA[0], lon: float = NICOSIA[1]
) -> datetime | None:
    """When the current night began (the last sunset), or None if it is day."""
    if not is_night(when, lat, lon):
        return None
    return last_crossing(when, lat, lon)


def night_fraction(day: datetime, lat: float = NICOSIA[0], lon: float = NICOSIA[1]) -> float:
    """Share of the 24 hours starting at `day` that is night (sampled every 10 minutes)."""
    samples = 144
    nights = sum(is_night(day + timedelta(minutes=10 * i), lat, lon) for i in range(samples))
    return nights / samples
