"""Offline tests for Tonight in Nicosia: duty pharmacies, DLS AD_Addresses, events, opening_hours,
privacy stripping, DuckDB clock replay, API routes, entity resolution, and whats_on_tonight."""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from chatbot.entities import Entities
from chatbot.memory import Memory
from chatbot.routes import sources as sources_route
from chatbot.server import app
from chatbot.settings import NICOSIA_CENTER, TIMEZONE, Settings, load_settings
from chatbot.store import SessionStore
from chatbot.tonight import Tonight
from chatbot.tonight.events import NICOSIA_CITIES, EventsCalendar, parse_visitcyprus_events, sanitize_copy, split_events_for_moment
from chatbot.tonight.geocode import DLS_ADDRESSES_QUERY, DLS_STREETS_QUERY, DLSAddressClient, TonightGeocoder
from chatbot.tonight.hours import evaluate_opening_hours
from chatbot.tonight.pharmacies import DutyRosterStore, discover_csv_url, duty_date_for, parse_duty_csv
from chatbot.tools import HANDLERS, ToolContext, run_tool

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tonight"
TZ = ZoneInfo(TIMEZONE)


def _settings(tmp_path: Path) -> Settings:
    return load_settings()


class _FakePlaces:
    ready = True
    overture = None

    def __init__(self, rows: list[dict], overture=None):
        self._rows = rows
        self._by_id = {r["id"]: r for r in rows}
        self.overture = overture

    def all(self) -> list[dict]:
        return self._rows

    def by_id(self, pid: str) -> dict | None:
        return self._by_id.get(pid)

    def near(self, lat: float, lon: float, radius_m: float = 500, category: str | None = None, limit: int = 20):
        return [r for r in self._rows if not category or r.get("category") == category][:limit]

    def named(self, name: str, near=None) -> list[dict]:
        return [r for r in self._rows if name.lower() in (r.get("name") or "").lower()]

    def geocode(self, q: str, limit: int = 4) -> list[dict]:
        return []


class _FakeOverture:
    ready = True

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def all(self) -> list[dict]:
        return self._rows

    def named(self, name: str, near=None) -> list[dict]:
        return [r for r in self._rows if name.lower() in (r.get("name") or "").lower()]


class _FakeRouter:
    def route(self, points, profile="walk", avoid=()):
        return {
            "distance_km": 0.42,
            "duration_min": 6,
            "legs": [{
                "directions": [
                    "Walk south on Digeni Akrita for 220 m.",
                    "Turn left onto Mnasiadou; pharmacy is on your right.",
                ],
            }],
        }


def _dls_post_from_fixtures():
    t4 = json.loads((FIXTURES / "dls_ad_addresses_t4.json").read_text(encoding="utf-8"))
    l0 = json.loads((FIXTURES / "dls_ad_addresses_l0.json").read_text(encoding="utf-8"))

    t4_features = t4.get("features") or []
    l0_features = l0.get("features") or []

    def post(url: str, body: dict | bytes):
        if isinstance(body, (bytes, bytearray)):
            parsed = urllib.parse.parse_qs(body.decode("utf-8"))
            where = (parsed.get("where") or [""])[0]
        else:
            where = str(body.get("where") or "")
        if url == DLS_STREETS_QUERY:
            pat = ""
            if "LIKE" in where and "'" in where:
                pat = where.split("'")[1].strip("%")
            matched = [
                f for f in t4_features
                if pat and pat in str((f.get("attributes") or {}).get("name") or "").upper()
            ]
            return {"features": matched}
        if url == DLS_ADDRESSES_QUERY:
            if "RID IN (" in where:
                inside = where.split("RID IN (", 1)[1].split(")", 1)[0]
                want_rids = {int(x.strip()) for x in inside.split(",") if x.strip().isdigit()}
                matched = [
                    f for f in l0_features
                    if (f.get("properties") or {}).get("RID") in want_rids
                ]
                return {"type": "FeatureCollection", "features": matched}
            if "postcode=" in where:
                return {"type": "FeatureCollection", "features": []}
        raise AssertionError(f"unexpected URL in offline DLS test: {url} {body}")

    return post


def _build_offline_tonight(tmp_path: Path, now_dt: datetime | None = None):
    s = _settings(tmp_path)
    mem = Memory(tmp_path / "tonight_twin.duckdb")

    html = (FIXTURES / "data_gov_leykosia.html").read_text(encoding="utf-8")
    csv_bytes = (FIXTURES / "pharmacies_leykosia.csv").read_bytes()
    events_payload = json.loads((FIXTURES / "visitcyprus_events.json").read_text(encoding="utf-8"))

    # Inject a synthetic personal home phone column into a single row test double when needed;
    # here use the real sanitized CSV fixture through DutyRosterStore.
    roster = DutyRosterStore(
        s,
        cache_dir=tmp_path / "cache",
        fetch_text=lambda _url: html,
        fetch_bytes=lambda _url: csv_bytes,
    )
    calendar = EventsCalendar(
        s,
        cache_dir=tmp_path / "cache",
        fetch_json=lambda _url, _params: events_payload,
    )

    ov_rows = [
        {
            "id": "ov-kalaglantzia",
            "name": "Kalitsioni Pharmacy",
            "category": "pharmacy",
            "phones": ["+35722999999"],
            "street": "Larnakos",
            "postcode": "2101",
            "lat": 35.1505,
            "lon": 33.3910,
        },
        {
            "id": "ov-papamarkidou",
            "name": "Papamarkidou Pharmacy",
            "category": "pharmacy",
            "phones": ["22263646"],
            "street": "Eleftheron",
            "postcode": "2413",
            "lat": 35.1618,
            "lon": 33.3189,
        },
    ]
    osm_rows = [
        {
            "id": "osm:n1",
            "name": "Digeni Late Pharmacy",
            "category": "pharmacy",
            "street": "Digeni Akrita",
            "opening_hours": "24/7",
            "phone": "22450111",
            "lat": 35.1652,
            "lon": 33.3685,
        },
        {
            "id": "osm:n2",
            "name": "Midnight Plate",
            "category": "restaurant",
            "street": "Onasagorou",
            "opening_hours": "Mo-Su 19:30-02:00",
            "lat": 35.1710,
            "lon": 33.3612,
        },
        {
            "id": "osm:n3",
            "name": "Daytime Bakery",
            "category": "cafe",
            "street": "Ledras",
            "opening_hours": "Mo-Fr 07:00-15:00",
            "lat": 35.1720,
            "lon": 33.3615,
        },
    ]
    places = _FakePlaces(osm_rows, overture=_FakeOverture(ov_rows))
    dls_addr = DLSAddressClient(s, cache_dir=tmp_path / "cache", post_json=_dls_post_from_fixtures())
    geocoder = TonightGeocoder(
        s,
        places,
        overture=places.overture,
        dls_addr=dls_addr,
        cache_dir=tmp_path / "cache",
    )
    fixed_now = now_dt or datetime(2026, 11, 27, 21, 30, tzinfo=TZ)
    tonight = Tonight(
        s,
        places,
        memory=mem,
        router=_FakeRouter(),
        overture=places.overture,
        roster=roster,
        calendar=calendar,
        geocoder=geocoder,
        cache_dir=tmp_path / "cache",
        now=lambda: fixed_now,
    )
    return s, mem, places, tonight


# -- Unit tests --------------------------------------------------------------------------------


def test_duty_csv_discovery_and_strict_privacy_strip(tmp_path: Path):
    html = (FIXTURES / "data_gov_leykosia.html").read_text(encoding="utf-8")
    assert discover_csv_url(html) == "https://www.data.gov.cy/el/resource/4226/download/file"

    # Simulate upstream CSV with explicit personal home phone columns (English & Greek variants)
    secret_home = "99887766"
    secret_home_gr = "22776655"
    raw_csv = (
        "\ufeffDate,Day,Reg. No.,Surmame,Name,Address,Additional Address Info,"
        "Muniuciplity / Community,Pharmacy Tel. No.,House Tel. No.,Τηλ. Οικίας\n"
        f"27/11/26,ΠΑΡΑΣΚΕΥΗ,252,Αργυρού,Άννα,Λεωφ. Διγενή Ακρίτα 45,"
        f"Δίπλα από φούρνο,Λευκωσία,22112233,{secret_home},{secret_home_gr}\n"
    )
    rows = parse_duty_csv(raw_csv)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "duty-2026-11-27-252"
    assert row["pharmacy_tel"] == "22112233"
    serialized = json.dumps(row, ensure_ascii=False)
    assert secret_home not in serialized
    assert secret_home_gr not in serialized
    assert all("house" not in k.lower() and "home" not in k.lower() for k in row)

    # Also test DutyRosterStore disk persistence never leaks personal numbers
    s = _settings(tmp_path)
    store = DutyRosterStore(
        s,
        cache_dir=tmp_path / "cache",
        fetch_text=lambda _u: html,
        fetch_bytes=lambda _u: raw_csv.encode("utf-8"),
    )
    loaded = store.refresh()
    assert len(loaded) == 1
    cache_text = (tmp_path / "cache" / "tonight" / "duty_roster.json").read_text(encoding="utf-8")
    assert secret_home not in cache_text
    assert secret_home_gr not in cache_text
    assert "House Tel" not in cache_text


def test_duty_date_0800_rollover():
    assert duty_date_for(datetime(2026, 11, 27, 21, 30, tzinfo=TZ)) == date(2026, 11, 27)
    # 03:30 AM still belongs to the 27 Nov overnight shift until 08:00
    assert duty_date_for(datetime(2026, 11, 28, 3, 30, tzinfo=TZ)) == date(2026, 11, 27)
    assert duty_date_for(datetime(2026, 11, 28, 7, 59, tzinfo=TZ)) == date(2026, 11, 27)
    # 08:00 switches to 28 Nov
    assert duty_date_for(datetime(2026, 11, 28, 8, 0, tzinfo=TZ)) == date(2026, 11, 28)


def test_opening_hours_evaluator_edge_cases():
    assert evaluate_opening_hours("24/7", datetime(2026, 11, 27, 3, 0, tzinfo=TZ)).is_open is True
    # Overnight range spanning midnight
    assert evaluate_opening_hours("Mo-Su 19:30-02:00", datetime(2026, 11, 27, 22, 0, tzinfo=TZ)).is_open is True
    assert evaluate_opening_hours("Mo-Su 19:30-02:00", datetime(2026, 11, 28, 1, 15, tzinfo=TZ)).is_open is True
    assert evaluate_opening_hours("Mo-Su 19:30-02:00", datetime(2026, 11, 28, 14, 0, tzinfo=TZ)).is_open is False

    # Semicolon weekday rules
    rule = "Mo-Fr 09:00-18:00; Sa 09:00-13:00"
    assert evaluate_opening_hours(rule, datetime(2026, 11, 25, 10, 0, tzinfo=TZ)).is_open is True
    assert evaluate_opening_hours(rule, datetime(2026, 11, 25, 20, 0, tzinfo=TZ)).is_open is False
    assert evaluate_opening_hours(rule, datetime(2026, 11, 28, 11, 0, tzinfo=TZ)).is_open is True
    assert evaluate_opening_hours(rule, datetime(2026, 11, 29, 11, 0, tzinfo=TZ)).is_open is False

    # Explicit off override
    assert evaluate_opening_hours("Mo-Su 08:00-22:00; Su off", datetime(2026, 11, 29, 12, 0, tzinfo=TZ)).is_open is False
    assert evaluate_opening_hours(None, datetime(2026, 11, 27, 20, 0, tzinfo=TZ)).is_open is None


def test_dls_ad_addresses_precision_and_no_wrong_municipality_trap(tmp_path: Path):
    _, mem, _, tonight = _build_offline_tonight(tmp_path)
    # 1) Phone match -> exact
    p_phone = tonight.geocoder.locate_pharmacy({
        "id": "duty-test-phone",
        "surname": "Παπαμαρκίδου",
        "first_name": "Αρετή",
        "address": "Ελευθέρων 4",
        "municipality": "Έγκωμη",
        "pharmacy_tel": "22263646",
    }, allow_network=True)
    assert p_phone["precision"] == "exact"

    # 2) Surname "Καλιτσιώνη" exists in Overture in Aglantzia, while this duty pharmacy is in Geri
    # on "Λεωφ. Δημήτρη Χρστόφια (Πρώην Λεωφ. Γερίου)" -> must NOT place at Aglantzia!
    p_geri = tonight.geocoder.locate_pharmacy({
        "id": "duty-test-geri",
        "surname": "Καλιτσιώνη (Δημητρίου)",
        "first_name": "Άντρεα",
        "address": "Λεωφ. Δημήτρη Χρστόφια 63",
        "directions": "(Πρώην Λεωφ. Γερίου, έναντι καφετέριας)",
        "municipality": "Γέρι",
        "pharmacy_tel": "22382559",
    }, allow_network=True)
    assert p_geri["precision"] == "street"
    assert "DLS" in p_geri["located_by"]
    assert p_geri["lat"] is not None
    assert abs(p_geri["lat"] - 35.1505) > 0.01  # clearly not Aglantzia

    # 3) Standard Nicosia avenue resolved from DLS Table 4 + Layer 0 fixtures
    p_street = tonight.geocoder.locate_pharmacy({
        "id": "duty-test-digeni",
        "surname": "Παπαδοπούλου",
        "first_name": "Μαρία",
        "address": "Λεωφ. Διγενή Ακρίτα 45",
        "municipality": "Λευκωσία",
        "pharmacy_tel": "22990001",
    }, allow_network=True)
    assert p_street["precision"] == "street"
    assert "DIGENI AKRITA" in p_street["located_by"]

    # 4) Municipality fallback & completely unplaced fallback
    p_area = tonight.geocoder.locate_pharmacy({
        "id": "duty-test-area",
        "surname": "Τεστ",
        "address": "Αγνώστου Οδού 1",
        "municipality": "Στρόβολος",
        "pharmacy_tel": "22000000",
    }, allow_network=False)
    assert p_area["precision"] == "area"

    p_none = tonight.geocoder.locate_pharmacy({
        "id": "duty-test-none",
        "surname": "Τεστ",
        "address": "Αγνώστου Οδού 1",
        "municipality": "Μακρινό Χωριό",
        "pharmacy_tel": "22000000",
    }, allow_network=False)
    assert p_none["precision"] == "unplaced"


def test_visitcyprus_events_cleaning_and_filtering():
    from chatbot.geo import norm

    payload = json.loads((FIXTURES / "visitcyprus_events.json").read_text(encoding="utf-8"))
    events = parse_visitcyprus_events(payload)
    assert events
    assert all(
        any(norm(c) in norm(f"{e.get('municipality', '')} {e.get('venue_name', '')} {e.get('venue_address', '')}") for c in NICOSIA_CITIES)
        or (e.get("venue_lat") is not None and 35.07 <= e["venue_lat"] <= 35.23)
        for e in events
    )
    assert all("limassol" not in (e.get("municipality") or "").lower() for e in events)
    for ev in events:
        for field in (ev["title"], ev["venue_name"], ev["description"]):
            assert "–" not in field
            assert "—" not in field
            assert "--" not in field
            assert "&#" not in field
    assert sanitize_copy("Fest – Night — Two--One") == "Fest, Night, Two, One"


def test_duckdb_tables_clock_replay_and_unknown_past_night(tmp_path: Path):
    _, mem, _, tonight = _build_offline_tonight(tmp_path)
    tonight.warm_up()
    with mem.reading() as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(duty_pharmacies)").fetchall()]
        assert cols
        assert all("house" not in c.lower() and "home" not in c.lower() for c in cols)
        n_ph = con.execute("SELECT COUNT(*) FROM duty_pharmacies").fetchone()[0]
        n_ev = con.execute("SELECT COUNT(*) FROM city_events").fetchone()[0]
        assert n_ph == 18  # 3 nights x 6 pharmacies in real fixture
        assert n_ev > 0

    # Replay past night inside fixture range -> known=True
    past = tonight.snapshot(at=datetime(2026, 11, 26, 22, 0, tzinfo=TZ), near=NICOSIA_CENTER, include_walking=True)
    assert past["known"] is True
    assert past["duty_date"] == "2026-11-26"
    assert len(past["pharmacies"]) == 6

    # Replay overnight 02:15 on 27 Nov -> belongs to 26 Nov shift
    overnight = tonight.snapshot(at=datetime(2026, 11, 27, 2, 15, tzinfo=TZ), near=NICOSIA_CENTER)
    assert overnight["duty_date"] == "2026-11-26"
    assert overnight["known"] is True

    # Replay night outside DuckDB memory -> never guess; known=False
    unknown = tonight.snapshot(at=datetime(2026, 8, 15, 22, 0, tzinfo=TZ), near=NICOSIA_CENTER)
    assert unknown["known"] is False
    assert unknown["pharmacies"] == []
    assert unknown["status"]["pharmacies"] == "unavailable"

    # Future closure study against persisted city_events
    future = tonight.snapshot(at=datetime(2026, 12, 11, 19, 30, tzinfo=TZ), near=NICOSIA_CENTER)
    assert len(future["events"]) + len(future["upcoming_events"]) > 0


def test_routes_entities_search_and_tool_offline(tmp_path: Path):
    s, mem, places, tonight = _build_offline_tonight(tmp_path)
    tonight.warm_up()
    ents = Entities(city=None, places=places, dls=None, registers=None, memory=mem)
    ents.tonight = tonight

    city = SimpleNamespace(now=lambda: datetime(2026, 11, 27, 21, 30, tzinfo=TZ))
    agent = SimpleNamespace(
        settings=s,
        city=city,
        places=places,
        memory=mem,
        entities=ents,
        tonight=tonight,
        overture=places.overture,
        environment=None,
        registers=None,
        mobility=None,
        watcher=None,
    )

    # 1) API routes via FastAPI TestClient without entering app lifespan
    prev_agent = getattr(app.state, "agent", None)
    app.state.agent = agent
    try:
        client = TestClient(app)
        r_live = client.get("/api/tonight", params={"lat": 35.1695, "lon": 33.3645, "walk": "true"})
        assert r_live.status_code == 200
        body = r_live.json()
        assert body["duty_date"] == "2026-11-27"
        assert body["known"] is True
        assert len(body["pharmacies"]) == 6
        assert body["open_now"]
        first_id = body["pharmacies"][0]["id"]
        assert "house_tel" not in json.dumps(body).lower()

        r_walk = client.get("/api/tonight/walk", params={"id": first_id, "from_lat": 35.1695, "from_lon": 33.3645})
        assert r_walk.status_code == 200
        w = r_walk.json()
        assert w["id"] == first_id
        assert w["placed"] is True
        assert w["duration_min"] >= 1
        assert w["directions"]

        r_ent = client.get(f"/api/entities/{first_id}")
        assert r_ent.status_code == 200
        ent = r_ent.json()
        assert ent["kind"] == "duty_pharmacy"
        assert ent["tonight"]["on_duty"] is True

        r_search = client.get("/api/entities/search", params={"q": "pharmacy"})
        assert r_search.status_code == 200
        assert any(item["kind"] == "duty_pharmacy" for item in r_search.json()["results"])

        # Event entity resolution & search too
        first_ev_id = tonight.calendar.events[0]["id"]
        r_ev = client.get(f"/api/entities/{first_ev_id}")
        assert r_ev.status_code == 200
        ev_ent = r_ev.json()
        assert ev_ent["kind"] == "city_event"
        assert ev_ent["name"]
        assert ev_ent["tonight"]["venue_name"]

        r_bad = client.get("/api/tonight", params={"at": "not-a-timestamp"})
        assert r_bad.status_code == 400
    finally:
        app.state.agent = prev_agent

    # 2) Direct source probes in chatbot/routes/sources.py require no network
    p_ph = sources_route.PROBES["duty_pharmacies"](agent)
    p_dls = sources_route.PROBES["dls_addresses"](agent)
    p_vc = sources_route.PROBES["visit_cyprus"](agent)
    assert p_ph["state"] == "live"
    assert p_dls["state"] in ("live", "waiting")
    assert p_vc["state"] == "live"

    # 3) Tool handler returns layer_ref, data_ref, and clean sources
    assert "whats_on_tonight" in HANDLERS
    ctx = ToolContext(
        settings=s,
        city=city,
        places=places,
        router=_FakeRouter(),
        store=SessionStore(),
        emit=lambda _e: None,
        conversation_id="test-tonight",
        memory=mem,
        entities=ents,
        tonight=tonight,
    )
    res, err = run_tool(ctx, "whats_on_tonight", {"focus": "all", "near_lat": 35.1695, "near_lon": 33.3645})
    assert not err, res
    assert "layer_ref" in res
    assert "data_ref" in res
    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["legend"] == {"on duty": "#10b981", "event": "#ec4899", "open now": "#f59e0b"}
    assert "duty_pharmacies" in layer["meta"]["sources"]
    assert "dls_addresses" in layer["meta"]["sources"]
    assert "visit_cyprus" in layer["meta"]["sources"]
