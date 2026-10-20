"""Interpretation rules, checked against payloads captured from the live API."""

from datetime import timedelta

import pytest

from collector import interpret as I
from conftest import load, ms, sample, utc


def latest(name):
    return I.latest_from_records(load(name))


def attrs(name):
    return {r["key"]: r["value"] for r in load(name)}


# -- lamps -------------------------------------------------------------------------------------
# YL1015 last reported at 2026-10-19 03:21:09Z (06:21 local), just before dawn: Dimmed/100/True.

LAMP = "latest_lamp_YL1015.json"
LAMP_REPORT = utc(2026, 10, 19, 3, 21, 9)


def test_lamp_on_when_reported_within_70_minutes():
    r = I.lamp(latest(LAMP), LAMP_REPORT + timedelta(minutes=29), night=True)
    assert r.state == "on"
    assert not r.stale
    assert r.detail["level"] == 100
    assert r.detail["power_w"] == 54
    assert r.age_s == pytest.approx(29 * 60, abs=1)


def test_lamp_still_shows_dimmed_at_noon_but_is_off():
    """The research gotcha: at noon 'latest' still says Dimmed/100/True from the night."""
    data = latest(LAMP)
    assert data["state"][1] == "Dimmed" and data["level"][1] == "100"
    r = I.lamp(data, utc(2026, 10, 19, 9, 0), night=False)
    assert r.state == "off"
    assert r.stale
    assert "by day" in r.reason


def test_lamp_offline_when_silent_at_night():
    night_since = utc(2026, 10, 19, 15, 8)
    r = I.lamp(latest(LAMP), utc(2026, 10, 19, 20, 0), night=True, night_since=night_since)
    assert r.state == "offline"
    assert r.stale


def test_lamp_unknown_just_after_dusk_before_first_report():
    night_since = utc(2026, 10, 19, 15, 8)
    r = I.lamp(latest(LAMP), night_since + timedelta(minutes=30), night=True,
               night_since=night_since)
    assert r.state == "unknown"
    assert "no report yet tonight" in r.reason


def test_lamp_powered_but_dark_reports_online_level_0():
    """YL1490 reports all day; by day it sends Online / 0 / False."""
    data = I.latest_from_records(load("range_lamp_YL1490.json"))
    assert data["state"][1] == "Online"
    report = I.ms_to_dt(data["state"][0])
    r = I.lamp(data, report + timedelta(minutes=10), night=False)
    assert r.state == "off"
    assert not r.stale
    assert "powered" in r.reason


def test_lamp_uses_the_sun_when_night_not_given():
    data = {"level": (ms(utc(2026, 10, 19, 20, 30)), "100")}
    assert I.lamp(data, utc(2026, 10, 19, 21, 0)).state == "on"
    assert I.lamp(data, utc(2026, 10, 19, 23, 0)).state == "offline"  # 23:00Z = 02:00 local
    assert I.lamp(data, utc(2026, 10, 20, 9, 0)).state == "off"  # noon local


def test_lamp_from_january_sample_payload():
    r = I.lamp(I.latest_from_records(sample("getLatestTelemetry.json")),
               utc(2026, 1, 8, 4, 30), night=True)
    assert r.state == "on"
    assert r.detail["power_w"] == 54.2


def test_lamp_without_readings_is_unknown():
    r = I.lamp({}, utc(2026, 10, 19, 9, 0), night=False)
    assert (r.state, r.stale, r.as_of) == ("unknown", True, None)


# -- parking -----------------------------------------------------------------------------------

PARKING = "latest_parking_YP974.json"  # status 1 at 08:42:35Z, heartbeat (vbatt) at 02:43Z


def test_parking_status_1_is_occupied():
    r = I.parking(latest(PARKING), utc(2026, 10, 19, 9, 0))
    assert r.state == "occupied"
    assert not r.stale
    assert r.detail["is_pwd"] is False
    assert r.detail["vbatt"] == 3.66
    assert "vendor" in r.reason


def test_parking_status_0_is_free():
    data = dict(latest(PARKING))
    data["status"] = (data["status"][0], "0")
    assert I.parking(data, utc(2026, 10, 19, 9, 0)).state == "free"


def test_parking_goes_stale_after_inactivity_timeout():
    r = I.parking(latest(PARKING), utc(2026, 10, 19, 14, 30))
    assert r.state == "occupied" and r.stale


def test_parking_last_activity_keeps_it_fresh():
    r = I.parking(latest(PARKING), utc(2026, 10, 19, 14, 30),
                  last_activity=utc(2026, 10, 19, 14, 0))
    assert not r.stale


# -- bins --------------------------------------------------------------------------------------


def test_bin_fill_band_and_pickup_details():
    r = I.bin_fill(latest("latest_bin_YW1394.json"), utc(2026, 10, 19, 9, 0))
    assert r.state == "medium"
    assert not r.stale
    assert r.detail["level_pct"] == 44
    assert r.detail["level_before_pickup"] == 73
    assert r.detail["level_after_pickup"] == 7
    assert r.detail["last_pickup"].startswith("2026-10-07")


def test_bin_stale_after_missed_reports():
    r = I.bin_fill(latest("latest_bin_YW1394.json"), utc(2026, 10, 19, 14, 0))
    assert r.stale


@pytest.mark.parametrize("level,band", [
    (0, "low"), (29.9, "low"), (30, "medium"), (59, "medium"), (60, "high"), (79, "high"),
    (80, "full"), (100, "full"), (130, "full"), (None, None), (-1, None),
])
def test_bin_bands(level, band):
    assert I.bin_band(level) == band


# -- air quality -------------------------------------------------------------------------------

AIR = "latest_air_f60f85f2babb.json"  # hourly report at 09:00Z, CAQI 20 driven by NO2


def test_air_quality_band_and_readings():
    r = I.air_quality(latest(AIR), utc(2026, 10, 19, 9, 30))
    assert r.state == "very_low"
    assert not r.stale
    d = r.detail
    assert d["caqi"] == 20
    assert d["dominant_pollutant"] == "NO2"
    assert d["pm2_5"] == 3.9 and d["pm10"] == 9.4 and d["no2"] == 40
    assert d["noise_db"] == 61.2
    assert d["sub_indices"] == {"PM2.5": 7, "PM10": 9, "NO2": 20, "O3": 29, "SO2": 0}


def test_air_quality_stale_after_two_hours():
    assert I.air_quality(latest(AIR), utc(2026, 10, 19, 11, 30)).stale


@pytest.mark.parametrize("caqi,band", [
    (0, "very_low"), (24.9, "very_low"), (25, "low"), (49, "low"), (50, "medium"),
    (74, "medium"), (75, "high"), (100, "high"), (101, "very_high"), (None, None),
])
def test_caqi_bands(caqi, band):
    assert I.caqi_band(caqi) == band


def test_air_quality_ignores_na():
    data = {"caqi": (ms(utc(2026, 10, 19, 9)), "n/a")}
    r = I.air_quality(data, utc(2026, 10, 19, 9, 30))
    assert r.state == "unknown"


# -- screens, cameras, parking lots ------------------------------------------------------------


def test_infopoint_healthy_is_ok():
    r = I.infopoint(latest("latest_infopoint_davilla.json"), utc(2026, 10, 19, 9, 30))
    assert (r.state, r.stale) == ("ok", False)
    assert r.detail["plan_state"] == "PLAYER_HAS_LATEST_PLAN"


def test_infopoint_heartbeat_overdue():
    data = {"state": (ms(utc(2026, 10, 19, 9)), "HEARTBEAT_OVERDUE")}
    assert I.infopoint(data, utc(2026, 10, 19, 9, 10)).state == "no_heartbeat"


def test_camera_connected():
    r = I.camera(latest("latest_camera_km08.json"), utc(2026, 10, 19, 9, 30))
    assert (r.state, r.stale) == ("connected", False)


def test_parking_lot_stale_since_2023():
    r = I.parking_lot(latest("latest_parkinglot_17.json"), utc(2026, 10, 19, 9, 0))
    assert r.stale
    assert r.as_of.year == 2023
    assert r.detail["available"] == 3


def test_interpret_dispatches_by_type():
    assert I.interpret("Wastebin", latest("latest_bin_YW1394.json"),
                       utc(2026, 10, 19, 9)).state == "medium"
    with pytest.raises(ValueError):
        I.interpret("Toaster", {}, utc(2026, 10, 19, 9))


def test_every_interpretation_serialises():
    r = I.parking(latest(PARKING), utc(2026, 10, 19, 9, 0)).to_dict()
    assert r["as_of"].startswith("2026-10-19T08:42")


# -- online / offline from lastActivityTime ----------------------------------------------------


def test_parking_online_from_last_activity_and_timeout():
    a = attrs("attributes_parking_YP974.json")
    timeout = timedelta(milliseconds=int(a["inactivityTimeout"]))
    seen = int(a["lastActivityTime"])
    assert I.online_status("Parking", seen, utc(2026, 10, 19, 9), timeout=timeout).state == "online"
    off = I.online_status("Parking", seen, utc(2026, 10, 19, 15), timeout=timeout)
    assert off.state == "offline" and off.stale


def test_lamp_silent_by_day_is_unpowered_not_offline():
    seen = int(attrs("attributes_lamp_YL1015.json")["lastActivityTime"])
    assert I.online_status("Lighting", seen, utc(2026, 10, 19, 9), night=False).state == "unpowered"
    assert I.online_status("Lighting", seen, utc(2026, 10, 19, 21), night=True).state == "offline"


def test_never_seen_is_unknown():
    assert I.online_status("Wastebin", None, utc(2026, 10, 19, 9)).state == "unknown"


# -- categorical states for state_changes ------------------------------------------------------


@pytest.mark.parametrize("dtype,key,value,state", [
    ("Parking", "status", "1", "occupied"),
    ("Parking", "status", "0", "free"),
    ("Parking", "vbatt", "3.6", None),
    ("Lighting", "level", "100", "lit"),
    ("Lighting", "level", "0", "dark"),
    ("Wastebin", "level", "85", "full"),
    ("smartsense", "caqi", "n/a", None),
    ("smartsense", "caqi", "60", "medium"),
    ("Camera", "connectionState", "CONNECTED", "connected"),
    ("InfoPoints", "state", "HEALTHY", "healthy"),
    (None, "status", "1", None),
])
def test_classify(dtype, key, value, state):
    assert I.classify(dtype, key, value) == state
