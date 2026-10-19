from datetime import timedelta

from collector import sun
from conftest import utc


def test_noon_elevation_in_october():
    # Solar noon in Nicosia on 19 Oct is about 09:31Z; 90 - 35.2 - 10.2 = 44.6 degrees.
    assert 43 < sun.solar_elevation(utc(2026, 10, 19, 9, 31)) < 46


def test_day_and_night():
    assert not sun.is_night(utc(2026, 10, 19, 9, 0))  # 12:00 local
    assert sun.is_night(utc(2026, 10, 19, 20, 0))  # 23:00 local


def test_sunset_time_matches_the_almanac():
    # Sunset in Nicosia on 19 Oct 2026 is about 18:06 local (15:06Z); lamps came on ~18:18.
    sunset = sun.night_started(utc(2026, 10, 19, 18, 0))
    assert utc(2026, 10, 19, 15, 0) < sunset < utc(2026, 10, 19, 15, 15)


def test_night_started_is_none_by_day():
    assert sun.night_started(utc(2026, 10, 19, 9, 0)) is None


def test_night_started_after_midnight_points_to_previous_evening():
    started = sun.night_started(utc(2026, 10, 20, 1, 0))
    assert started.date() == utc(2026, 10, 19).date()


def test_night_fraction_by_season():
    assert 0.50 < sun.night_fraction(utc(2026, 10, 19)) < 0.56
    assert 0.37 < sun.night_fraction(utc(2026, 6, 21)) < 0.43
    assert 0.57 < sun.night_fraction(utc(2026, 12, 21)) < 0.63


def test_last_crossing_bounded():
    t = utc(2026, 10, 19, 12, 0)
    crossing = sun.last_crossing(t)
    assert t - timedelta(hours=12) < crossing < t
