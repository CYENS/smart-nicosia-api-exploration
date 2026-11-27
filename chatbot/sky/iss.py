"""International Space Station (NORAD 25544) TLE fetch and visible pass prediction over
Nicosia (35.1725 N, 33.365 E), using pure-Python SGP4 propagation (WGS-72).

TLEs come from CelesTrak (cached on disk for 12 hours). A visible pass requires:
- maximum elevation >= 10 degrees above the Nicosia horizon,
- Nicosia in civil twilight or darkness (sun altitude <= -4 deg),
- the ISS sunlit (outside Earth's cylindrical shadow) during at least part of the pass.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from chatbot.environment import sun as sun_mod
from chatbot.settings import CACHE_DIR, NICOSIA_CENTER, TIMEZONE

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
TLE_TTL_S = 12 * 3600
PASS_CACHE_S = 900.0
MIN_MAX_EL_DEG = 10.0
HORIZON_EL_DEG = 5.0
MAX_SUN_ALT_DEG = -4.0  # civil twilight / dark over Nicosia

# WGS-72 orbital constants used by standard SGP4 (Vallado 2006 / Spacetrack Report #3).
_RE = 6378.135  # Earth equatorial radius (km)
_MU = 398600.8  # km^3 / s^2
_XKE = 60.0 / math.sqrt(_RE ** 3 / _MU)  # 0.07436691613317342 rad/min^(3/2)
_J2 = 1.082616e-3
_J3 = -2.53881e-6
_J4 = -1.65597e-6
_CK2 = 0.5 * _J2
_CK4 = -0.375 * _J4
_QOMS2T = ((120.0 - 78.0) / _RE) ** 4
_S = 78.0 / _RE + 1.0
_WGS84_A = 6378.137
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)
_MIN_PER_DAY = 1440.0
_DEG2RAD = math.pi / 180.0
_RAD2DEG = 180.0 / math.pi
_TWOPI = 2.0 * math.pi
_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def _compass16(deg: float) -> str:
    return _COMPASS[int(((deg % 360.0) + 11.25) // 22.5) % 16]


def _fmod2p(x: float) -> float:
    return x % _TWOPI


def _parse_exp(s: str) -> float:
    """Parse TLE implicit-decimal scientific notation like ' 10075-3' -> 0.10075e-3."""
    s = s.strip()
    if not s:
        return 0.0
    sign = -1.0 if s[0] == "-" else 1.0
    if s[0] in "+-":
        s = s[1:]
    for i in range(1, len(s)):
        if s[i] in "+-":
            mant = s[:i]
            exp = int(s[i:])
            return sign * float("0." + mant) * (10.0 ** exp)
    return sign * float("0." + s)


def _epoch_to_datetime(epoch_yr_day: str) -> datetime:
    yr2 = int(epoch_yr_day[:2])
    year = 2000 + yr2 if yr2 < 57 else 1900 + yr2
    day_of_year = float(epoch_yr_day[2:])
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_of_year - 1.0)


@dataclass(frozen=True)
class TLE:
    name: str
    line1: str
    line2: str
    epoch: datetime
    bstar: float
    inclo: float  # rad
    nodeo: float  # rad
    ecco: float
    argpo: float  # rad
    mo: float  # rad
    no_kozai: float  # rad/min


def parse_tle(text: str) -> TLE:
    """Parse a 2-line or 3-line ISS TLE string."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    l1_idx = next((i for i, ln in enumerate(lines) if ln.startswith("1 ")), None)
    if l1_idx is None or l1_idx + 1 >= len(lines) or not lines[l1_idx + 1].startswith("2 "):
        raise ValueError("TLE text does not contain valid line 1 and line 2")
    name = lines[l1_idx - 1] if l1_idx > 0 else "ISS (ZARYA)"
    l1, l2 = lines[l1_idx], lines[l1_idx + 1]
    if len(l1) < 63 or len(l2) < 63:
        raise ValueError("TLE lines are truncated")

    epoch = _epoch_to_datetime(l1[18:32].strip())
    bstar = _parse_exp(l1[53:61])
    inclo = float(l2[8:16]) * _DEG2RAD
    nodeo = float(l2[17:25]) * _DEG2RAD
    ecco = float("0." + l2[26:33].strip())
    argpo = float(l2[34:42]) * _DEG2RAD
    mo = float(l2[43:51]) * _DEG2RAD
    no_rev_day = float(l2[52:63])
    no_kozai = no_rev_day * _TWOPI / _MIN_PER_DAY
    return TLE(name=name, line1=l1, line2=l2, epoch=epoch, bstar=bstar,
               inclo=inclo, nodeo=nodeo, ecco=ecco, argpo=argpo, mo=mo, no_kozai=no_kozai)


class _SGP4:
    """Near-Earth SGP4 propagator initialised for one TLE."""

    def __init__(self, tle: TLE):
        self.tle = tle
        e0 = tle.ecco
        xno = tle.no_kozai
        cosio = math.cos(tle.inclo)
        theta2 = cosio * cosio
        x3thm1 = 3.0 * theta2 - 1.0
        eosq = e0 * e0
        betao2 = 1.0 - eosq
        betao = math.sqrt(betao2)

        a1 = (_XKE / xno) ** (2.0 / 3.0)
        del1 = 1.5 * _CK2 * x3thm1 / (a1 * a1 * betao * betao2)
        ao = a1 * (1.0 - del1 * (1.0 / 3.0 + del1 * (1.0 + 134.0 / 81.0 * del1)))
        delo = 1.5 * _CK2 * x3thm1 / (ao * ao * betao * betao2)
        self.xnodp = xno / (1.0 + delo)
        self.aodp = ao / (1.0 - delo)

        perige = (self.aodp * (1.0 - e0) - 1.0) * _RE
        s4 = _S
        qoms24 = _QOMS2T
        if perige < 156.0:
            s4_km = max(20.0, perige - 78.0)
            s4 = s4_km / _RE + 1.0
            qoms24 = ((120.0 - s4_km) / _RE) ** 4
        self.isimp = 1 if (self.aodp * (1.0 - e0) < (220.0 / _RE + 1.0)) else 0

        pinvsq = 1.0 / (self.aodp * self.aodp * betao2 * betao2)
        tsi = 1.0 / (self.aodp - s4)
        self.eta = self.aodp * e0 * tsi
        etasq = self.eta * self.eta
        eeta = e0 * self.eta
        psisq = abs(1.0 - etasq)
        coef = qoms24 * (tsi ** 4)
        coef1 = coef / (psisq ** 3.5)
        c2 = coef1 * self.xnodp * (
            self.aodp * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
            + 0.75 * _CK2 * tsi / psisq * x3thm1 * (8.0 + 3.0 * etasq * (8.0 + etasq))
        )
        self.c1 = tle.bstar * c2
        sinio = math.sin(tle.inclo)
        a3ovk2 = -_J3 / _CK2
        self.c3 = coef * tsi * a3ovk2 * self.xnodp * sinio / e0 if e0 > 1e-4 else 0.0
        self.x1mth2 = 1.0 - theta2
        self.c4 = 2.0 * self.xnodp * coef1 * self.aodp * betao2 * (
            self.eta * (2.0 + 0.5 * etasq)
            + e0 * (0.5 + 2.0 * etasq)
            - 2.0 * _CK2 * tsi / (self.aodp * psisq) * (
                -3.0 * x3thm1 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                + 0.75 * self.x1mth2 * (2.0 * etasq - eeta * (1.0 + etasq)) * math.cos(2.0 * tle.argpo)
            )
        )
        self.c5 = 2.0 * coef1 * self.aodp * betao2 * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)

        theta4 = theta2 * theta2
        temp1 = 3.0 * _CK2 * pinvsq * self.xnodp
        temp2 = temp1 * _CK2 * pinvsq
        temp3 = 1.25 * _CK4 * pinvsq * pinvsq * self.xnodp
        self.xmdot = self.xnodp + 0.5 * temp1 * betao * x3thm1 + 0.0625 * temp2 * betao * (13.0 - 78.0 * theta2 + 137.0 * theta4)
        x1m5th = 1.0 - 5.0 * theta2
        self.omgdot = -0.5 * temp1 * x1m5th + 0.0625 * temp2 * (7.0 - 114.0 * theta2 + 395.0 * theta4) + temp3 * (3.0 - 36.0 * theta2 + 49.0 * theta4)
        xhdot1 = -temp1 * cosio
        self.xnodot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * theta2) + 2.0 * temp3 * (3.0 - 7.0 * theta2)) * cosio
        self.omgcof = tle.bstar * self.c3 * math.cos(tle.argpo)
        self.xmcof = -2.0 / 3.0 * coef * tle.bstar / eeta if e0 > 1e-4 else 0.0
        self.xnodcf = 3.5 * betao2 * xhdot1 * self.c1
        self.t2cof = 1.5 * self.c1
        self.xlcof = 0.125 * a3ovk2 * sinio * (3.0 + 5.0 * cosio) / (1.0 + cosio) if abs(cosio + 1.0) > 1.5e-12 else 0.125 * a3ovk2 * sinio * (3.0 + 5.0 * cosio) / 1.5e-12
        self.aycof = 0.25 * a3ovk2 * sinio
        self.delmo = (1.0 + self.eta * math.cos(tle.mo)) ** 3
        self.sinmo = math.sin(tle.mo)
        self.x7thm1 = 7.0 * theta2 - 1.0
        self.cosio = cosio
        self.sinio = sinio
        if not self.isimp:
            c1sq = self.c1 * self.c1
            self.d2 = 4.0 * self.aodp * tsi * c1sq
            temp = self.d2 * tsi * self.c1 / 3.0
            self.d3 = (17.0 * self.aodp + s4) * temp
            self.d4 = 0.5 * temp * self.aodp * tsi * (221.0 * self.aodp + 31.0 * s4) * self.c1
            self.t3cof = self.d2 + 2.0 * c1sq
            self.t4cof = 0.25 * (3.0 * self.d3 + self.c1 * (12.0 * self.d2 + 10.0 * c1sq))
            self.t5cof = 0.2 * (3.0 * self.d4 + 12.0 * self.c1 * self.d3 + 6.0 * self.d2 * self.d2 + 15.0 * c1sq * (2.0 * self.d2 + c1sq))

    def propagate(self, at: datetime) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Return TEME (r_km, v_km_s) at UTC datetime `at`."""
        tsince = (at - self.tle.epoch).total_seconds() / 60.0
        t = self.tle
        xmdf = t.mo + self.xmdot * tsince
        omgadf = t.argpo + self.omgdot * tsince
        xnoddf = t.nodeo + self.xnodot * tsince
        omega = omgadf
        xmp = xmdf
        tsq = tsince * tsince
        xnode = xnoddf + self.xnodcf * tsq
        tempa = 1.0 - self.c1 * tsince
        tempe = t.bstar * self.c4 * tsince
        templ = self.t2cof * tsq
        if not self.isimp:
            delomg = self.omgcof * tsince
            delm = self.xmcof * (((1.0 + self.eta * math.cos(xmdf)) ** 3) - self.delmo)
            temp = delomg + delm
            xmp = xmdf + temp
            omega = omgadf - temp
            tcube = tsq * tsince
            tfour = tsince * tcube
            tempa -= self.d2 * tsq + self.d3 * tcube + self.d4 * tfour
            tempe += t.bstar * self.c5 * (math.sin(xmp) - self.sinmo)
            templ += self.t3cof * tcube + tfour * (self.t4cof + tsince * self.t5cof)

        a = self.aodp * tempa * tempa
        e = max(1e-6, min(0.999999, t.ecco - tempe))
        xl = xmp + omega + xnode + self.xnodp * templ
        beta = math.sqrt(1.0 - e * e)
        xn = _XKE / (a ** 1.5)

        axn = e * math.cos(omega)
        temp = 1.0 / (a * beta * beta)
        xll = temp * self.xlcof * axn
        aynl = temp * self.aycof
        xlt = xl + xll
        ayn = e * math.sin(omega) + aynl

        capu = _fmod2p(xlt - xnode)
        temp2 = capu
        for _ in range(10):
            sinepw = math.sin(temp2)
            cosepw = math.cos(temp2)
            temp3 = axn * sinepw
            temp4 = ayn * cosepw
            temp5 = axn * cosepw
            temp6 = ayn * sinepw
            epw = (capu - temp4 + temp3 - temp2) / (1.0 - temp5 - temp6) + temp2
            if abs(epw - temp2) <= 1e-12:
                break
            temp2 = epw
        else:
            epw = temp2

        ecose = temp5 + temp6
        esine = temp3 - temp4
        elsq = axn * axn + ayn * ayn
        temp = 1.0 - elsq
        pl = a * temp
        r = a * (1.0 - ecose)
        temp1 = 1.0 / r
        rdot = _XKE * math.sqrt(a) * esine * temp1
        rfdot = _XKE * math.sqrt(pl) * temp1
        temp2 = a * temp1
        betal = math.sqrt(temp)
        temp3 = 1.0 / (1.0 + betal)
        cosu = temp2 * (cosepw - axn + ayn * esine * temp3)
        sinu = temp2 * (sinepw - ayn - axn * esine * temp3)
        u = math.atan2(sinu, cosu)
        sin2u = 2.0 * sinu * cosu
        cos2u = 2.0 * cosu * cosu - 1.0
        temp = 1.0 / pl
        temp1 = _CK2 * temp
        temp2 = temp1 * temp

        rk = r * (1.0 - 1.5 * temp2 * betal * (3.0 * self.cosio ** 2 - 1.0)) + 0.5 * temp1 * self.x1mth2 * cos2u
        uk = u - 0.25 * temp2 * self.x7thm1 * sin2u
        xnodek = xnode + 1.5 * temp2 * self.cosio * sin2u
        xinck = t.inclo + 1.5 * temp2 * self.cosio * self.sinio * cos2u
        rdotk = rdot - xn * temp1 * self.x1mth2 * sin2u
        rfdotk = rfdot + xn * temp1 * (self.x1mth2 * cos2u + 1.5 * (3.0 * self.cosio ** 2 - 1.0))

        sinuk, cosuk = math.sin(uk), math.cos(uk)
        sinik, cosik = math.sin(xinck), math.cos(xinck)
        sinnok, cosnok = math.sin(xnodek), math.cos(xnodek)
        xmx = -sinnok * cosik
        xmy = cosnok * cosik
        ux = xmx * sinuk + cosnok * cosuk
        uy = xmy * sinuk + sinnok * cosuk
        uz = sinik * sinuk
        vx = xmx * cosuk - cosnok * sinuk
        vy = xmy * cosuk - sinnok * sinuk
        vz = sinik * cosuk

        km_per_min_to_km_s = _RE / 60.0
        pos = (rk * ux * _RE, rk * uy * _RE, rk * uz * _RE)
        vel = (
            (rdotk * ux + rfdotk * vx) * km_per_min_to_km_s,
            (rdotk * uy + rfdotk * vy) * km_per_min_to_km_s,
            (rdotk * uz + rfdotk * vz) * km_per_min_to_km_s,
        )
        return pos, vel


def _julian_day(at: datetime) -> float:
    at_u = at.astimezone(timezone.utc)
    return at_u.timestamp() / 86400.0 + 2440587.5


def _gmst(jd: float) -> float:
    """Greenwich Mean Sidereal Time in radians."""
    tut1 = (jd - 2451545.0) / 36525.0
    sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * tut1
        + 0.093104 * tut1 * tut1
        - 6.2e-6 * tut1 * tut1 * tut1
    )
    return ((sec % 86400.0) / 86400.0) * _TWOPI


def _sun_eci_unit(jd: float) -> tuple[float, float, float]:
    """Approximate unit vector from Earth to the Sun in ECI (accurate to ~0.01 deg)."""
    n = jd - 2451545.0
    L = _fmod2p((280.460 + 0.9856474 * n) * _DEG2RAD)
    g = _fmod2p((357.528 + 0.9856003 * n) * _DEG2RAD)
    lam = L + (1.915 * math.sin(g) + 0.020 * math.sin(2.0 * g)) * _DEG2RAD
    eps = (23.439 - 0.0000004 * n) * _DEG2RAD
    return (math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam))


def _is_sunlit(r_eci: tuple[float, float, float], jd: float) -> bool:
    """True when the satellite is outside Earth's cylindrical shadow."""
    sx, sy, sz = _sun_eci_unit(jd)
    proj = r_eci[0] * sx + r_eci[1] * sy + r_eci[2] * sz
    if proj >= 0.0:
        return True
    perp2 = (r_eci[0] ** 2 + r_eci[1] ** 2 + r_eci[2] ** 2) - proj * proj
    return perp2 > (_RE * _RE)


def _observer_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.15) -> tuple[float, float, float]:
    lat = lat_deg * _DEG2RAD
    lon = lon_deg * _DEG2RAD
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    return (
        (n + alt_km) * cos_lat * math.cos(lon),
        (n + alt_km) * cos_lat * math.sin(lon),
        (n * (1.0 - _WGS84_E2) + alt_km) * sin_lat,
    )


def _teme_to_ecef(r: tuple[float, float, float], gmst: float) -> tuple[float, float, float]:
    cg, sg = math.cos(gmst), math.sin(gmst)
    return (cg * r[0] + sg * r[1], -sg * r[0] + cg * r[1], r[2])


def _ecef_to_geodetic(r: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = r
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - _WGS84_E2))
    for _ in range(6):
        sin_lat = math.sin(lat)
        n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
        lat = math.atan2(z + _WGS84_E2 * n * sin_lat, p)
    sin_lat = math.sin(lat)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-9 else abs(z) - n * (1.0 - _WGS84_E2)
    return lat * _RAD2DEG, lon * _RAD2DEG, alt


def _topocentric(r_sat_ecef: tuple[float, float, float], obs_ecef: tuple[float, float, float],
                 lat_deg: float, lon_deg: float) -> tuple[float, float, float]:
    """Return (azimuth_deg, elevation_deg, range_km) seen from observer."""
    dx = r_sat_ecef[0] - obs_ecef[0]
    dy = r_sat_ecef[1] - obs_ecef[1]
    dz = r_sat_ecef[2] - obs_ecef[2]
    lat, lon = lat_deg * _DEG2RAD, lon_deg * _DEG2RAD
    slat, clat = math.sin(lat), math.cos(lat)
    slon, clon = math.sin(lon), math.cos(lon)
    south = slat * clon * dx + slat * slon * dy - clat * dz
    east = -slon * dx + clon * dy
    up = clat * clon * dx + clat * slon * dy + slat * dz
    rng = math.sqrt(south * south + east * east + up * up)
    el = math.asin(max(-1.0, min(1.0, up / max(rng, 1e-9)))) * _RAD2DEG
    az = (math.atan2(east, -south) * _RAD2DEG) % 360.0
    return az, el, rng


def iss_state(prop: _SGP4, at: datetime, obs_lat: float, obs_lon: float) -> dict:
    """Instantaneous ISS ground position and sky look angles from Nicosia at `at`."""
    jd = _julian_day(at)
    gmst = _gmst(jd)
    r_teme, v_teme = prop.propagate(at)
    r_ecef = _teme_to_ecef(r_teme, gmst)
    lat, lon, alt_km = _ecef_to_geodetic(r_ecef)
    obs = _observer_ecef(obs_lat, obs_lon)
    az, el, rng = _topocentric(r_ecef, obs, obs_lat, obs_lon)
    speed_kms = math.sqrt(sum(v * v for v in v_teme))
    sunlit = _is_sunlit(r_teme, jd)
    sun_alt = sun_mod.position(at, obs_lat, obs_lon).altitude
    return {
        "at": at.astimezone(TZ).isoformat(timespec="seconds"),
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "altitude_km": round(alt_km, 1),
        "speed_kms": round(speed_kms, 2),
        "azimuth_deg": round(az, 1),
        "direction": _compass16(az),
        "elevation_deg": round(el, 1),
        "range_km": round(rng, 1),
        "above_horizon": el > 0.0,
        "sunlit": sunlit,
        "visible_now": el >= HORIZON_EL_DEG and sunlit and sun_alt <= MAX_SUN_ALT_DEG,
    }


def compute_visible_passes(
    tle: TLE,
    start: datetime,
    end: datetime,
    *,
    obs_lat: float = NICOSIA_CENTER[0],
    obs_lon: float = NICOSIA_CENTER[1],
    min_max_el_deg: float = MIN_MAX_EL_DEG,
) -> list[dict]:
    """Compute visible ISS passes over (obs_lat, obs_lon) within [start, end]."""
    prop = _SGP4(tle)
    obs = _observer_ecef(obs_lat, obs_lon)
    coarse_s = 60
    fine_s = 10

    def look(t: datetime) -> tuple[float, float, bool]:
        jd = _julian_day(t)
        r_teme, _ = prop.propagate(t)
        r_ecef = _teme_to_ecef(r_teme, _gmst(jd))
        az, el, _ = _topocentric(r_ecef, obs, obs_lat, obs_lon)
        sunlit = _is_sunlit(r_teme, jd)
        sun_alt = sun_mod.position(t, obs_lat, obs_lon).altitude
        return az, el, (sunlit and sun_alt <= MAX_SUN_ALT_DEG)

    # Scan coarsely for above-horizon windows, then sample finely to get peak & visible segments.
    passes: list[dict] = []
    t = start
    in_win = False
    win_start = start
    while t <= end:
        _, el, _ = look(t)
        if el >= 0.0 and not in_win:
            in_win = True
            win_start = t - timedelta(seconds=coarse_s)
        elif el < 0.0 and in_win:
            in_win = False
            p = _refine_pass(look, max(start, win_start), min(end, t), fine_s, min_max_el_deg)
            if p is not None:
                passes.append(p)
        t += timedelta(seconds=coarse_s)
    if in_win:
        p = _refine_pass(look, max(start, win_start), end, fine_s, min_max_el_deg)
        if p is not None:
            passes.append(p)
    return passes


def _refine_pass(look, a: datetime, b: datetime, step_s: int, min_max_el_deg: float) -> dict | None:
    samples: list[tuple[datetime, float, float, bool]] = []
    cur = a
    while cur <= b:
        az, el, vis = look(cur)
        samples.append((cur, az, el, vis))
        cur += timedelta(seconds=step_s)
    visible = [s for s in samples if s[2] >= HORIZON_EL_DEG and s[3]]
    if not visible:
        return None
    peak = max(visible, key=lambda s: s[2])
    if peak[2] < min_max_el_deg:
        return None
    first, last = visible[0], visible[-1]
    start_local = first[0].astimezone(TZ)
    peak_local = peak[0].astimezone(TZ)
    end_local = last[0].astimezone(TZ)
    dur_s = max(step_s, round((last[0] - first[0]).total_seconds()))
    return {
        "start": start_local.isoformat(timespec="seconds"),
        "peak": peak_local.isoformat(timespec="seconds"),
        "end": end_local.isoformat(timespec="seconds"),
        "start_ms": int(first[0].timestamp() * 1000),
        "peak_ms": int(peak[0].timestamp() * 1000),
        "end_ms": int(last[0].timestamp() * 1000),
        "duration_s": dur_s,
        "duration_min": max(1, round(dur_s / 60)),
        "max_elevation_deg": round(peak[2]),
        "start_azimuth_deg": round(first[1]),
        "start_dir": _compass16(first[1]),
        "peak_azimuth_deg": round(peak[1]),
        "peak_dir": _compass16(peak[1]),
        "end_azimuth_deg": round(last[1]),
        "end_dir": _compass16(last[1]),
    }


class ISSTracker:
    """Caches the CelesTrak ISS TLE for 12 hours and computes visible passes over Nicosia."""

    def __init__(self, fetch_text, *, cache_dir: Path | None = None, now=None,
                 ttl_s: float = TLE_TTL_S):
        self.fetch_text = fetch_text
        self.cache_dir = cache_dir or CACHE_DIR
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._tle: tuple[float, TLE] | None = None
        self._passes: tuple[float, str, list[dict]] | None = None
        self._read_at: float | None = None
        self._read_ok_at: float | None = None
        self._error: str | None = None
        self.fetches = 0

    def health(self) -> dict | None:
        """Last TLE read metadata for /api/sources (never fetches)."""
        if self._read_at is None:
            return None
        tle = self._tle[1] if self._tle else None
        return {
            "read_at": self._read_at,
            "read_ok_at": self._read_ok_at,
            "error": self._error,
            "tle_epoch": tle.epoch.astimezone(TZ).isoformat(timespec="minutes") if tle else None,
        }

    def tle(self) -> TLE:
        now_s = time.time()
        with self._lock:
            if self._tle and now_s - self._tle[0] < self.ttl_s:
                return self._tle[1]
            path = self.cache_dir / "iss_tle.txt"
            if path.exists() and now_s - path.stat().st_mtime < self.ttl_s:
                try:
                    parsed = parse_tle(path.read_text(encoding="utf-8"))
                    self._read_at = self._read_ok_at = path.stat().st_mtime
                    self._error = None
                    self._tle = (now_s, parsed)
                    return parsed
                except Exception:  # noqa: BLE001 - corrupt cache: refetch
                    pass
            self._read_at = now_s
            try:
                self.fetches += 1
                text = self.fetch_text(TLE_URL)
                parsed = parse_tle(text)
            except Exception as e:  # noqa: BLE001
                self._error = str(e)[:200]
                if path.exists():
                    log.warning("ISS TLE refresh failed, using cached copy: %s", self._error)
                    parsed = parse_tle(path.read_text(encoding="utf-8"))
                    self._tle = (now_s - self.ttl_s + 600, parsed)
                    return parsed
                raise
            self._read_ok_at = now_s
            self._error = None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self._tle = (now_s, parsed)
            return parsed

    def summary(self, at: datetime | None = None, *, look_ahead_days: int = 4,
                look_back_days: int = 7) -> dict:
        """Current ISS position (at `at` or now), upcoming visible passes, and recent passes
        for the City Clock track."""
        now_dt = at or self.now()
        try:
            tle = self.tle()
        except Exception as e:  # noqa: BLE001
            return {
                "status": "unavailable",
                "error": str(e)[:200],
                "position": None,
                "next_pass": None,
                "upcoming": [],
                "recent": [],
                "tle_epoch": None,
            }
        prop = _SGP4(tle)
        pos = iss_state(prop, now_dt, NICOSIA_CENTER[0], NICOSIA_CENTER[1])
        now_ms = int(now_dt.timestamp() * 1000)

        # Cache pass list for 15 min around current hour / TLE epoch.
        bucket = f"{tle.line1}:{int(self.now().timestamp()) // 900}:{look_back_days}:{look_ahead_days}"
        with self._lock:
            hit = self._passes
            all_passes = hit[2] if hit and hit[1] == bucket and time.time() - hit[0] < PASS_CACHE_S else None
        if all_passes is None:
            ref = self.now()
            all_passes = compute_visible_passes(
                tle,
                ref - timedelta(days=look_back_days),
                ref + timedelta(days=look_ahead_days),
            )
            with self._lock:
                self._passes = (time.time(), bucket, all_passes)

        upcoming = [p for p in all_passes if p["end_ms"] >= now_ms]
        recent = [p for p in all_passes if p["end_ms"] < now_ms]
        stale = bool(self._error)
        return {
            "status": "stale" if stale else "live",
            "error": self._error,
            "tle_epoch": tle.epoch.astimezone(TZ).isoformat(timespec="minutes"),
            "position": pos,
            "next_pass": upcoming[0] if upcoming else None,
            "upcoming": upcoming[:6],
            "recent": recent,
        }
