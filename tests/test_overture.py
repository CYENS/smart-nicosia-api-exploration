"""Overture Maps places: cut from a release with DuckDB, kept by the quality rules, searched after
the OSM index, opened in the inspector, and taken by web places before any geocoding.

tests/fixtures/overture_places_slice.parquet holds 40 real rows of release 2026-11-18.0 in
Nicosia, with the release's own schema, and two altered copies: Bulevard marked closed, and a
copy of Ithaki Venue moved to Limassol. No network: the release listing and the S3 read are
replaced where a test needs them."""

import struct
from collections import Counter
from types import SimpleNamespace

import pytest
from conftest import FIXTURES

from chatbot import geo as geo_mod
from chatbot import overture as O
from chatbot import sources
from chatbot.entities import Entities
from chatbot.geo import Places, haversine_m, text_score
from chatbot.routes import sources as sources_routes
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, resolve_place, run_tool
from chatbot.webplaces import WebPlaces, names_agree

SLICE = FIXTURES / "overture_places_slice.parquet"
ELEFTHERIA = (35.1707, 33.3606)  # Eleftheria Square
ITHAKI = (35.17378, 33.37069)  # Ithaki Venue's door, Nikiforou Foka 33, 1016
# OSM's own copies of two cafés Overture has too (from the places index, 26 Nov 2026).
OSM = [
    {"id": "n13418297272", "category": "cafe", "name": "La Croissanterie",
     "name_local": "La Croissanterie", "lat": 35.170245, "lon": 33.361176},
    {"id": "n6255644748", "category": "cafe", "name": "Caffè Nero", "name_local": "Caffè Nero",
     "lat": 35.171635, "lon": 33.361821, "opening_hours": "Mo-Su 09:00-21:00",
     "street": "Ονασαγόρου"},
    {"id": "n100", "category": "museum", "name": "Leventis Museum", "lat": 35.1712, "lon": 33.3612},
]


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return O.extract(str(SLICE))


@pytest.fixture
def ov(tmp_path, rows) -> O.Overture:
    o = O.Overture(tmp_path / "overture_places.json")
    places, dropped = O.curate(rows)
    o._use(places, "2026-11-18.0", 1795680000.0, 1795680000.0, dropped)
    return o


@pytest.fixture
def no_geocoders(monkeypatch):
    """Nominatim and Photon, finding nothing: a place placed here was placed without them."""
    calls = []

    def fake(url, *, user_agent, params=None, data=None, timeout=30.0):
        calls.append(params.get("q"))
        return [] if url.endswith("/search") else {"features": []}

    monkeypatch.setattr(geo_mod, "get_json", fake)
    return calls


def make_places(tmp_path, monkeypatch, ov, osm=OSM) -> Places:
    monkeypatch.setattr(geo_mod, "CACHE_DIR", tmp_path)
    p = Places(Settings(), overture=ov)
    p._places = [dict(x) for x in osm]
    return p


# -- the data ------------------------------------------------------------------------------------


def test_the_extraction_query_cuts_nicosia_out_of_a_release(rows):
    sql = O.extraction_sql(O.release_glob("2026-11-18.0"))
    assert ("read_parquet('s3://overturemaps-us-west-2/release/2026-11-18.0/theme=places/"
            "type=place/*')") in sql
    assert "bbox.xmin BETWEEN $lon_min AND $lon_max" in sql and "taxonomy.primary" in sql
    assert len(rows) == 40  # the copy moved to Limassol is outside the bbox
    assert "00000000-0000-4000-8000-00000000ab01" not in {r["id"] for r in rows}
    ithaki = next(r for r in rows if r["name"] == "Ithaki Venue")
    assert (ithaki["kind"], ithaki["address"], ithaki["postcode"]) == (
        "dance_club", "Nikiforou Foka 33", "1016")
    assert O.point_of(ithaki["geometry"], ithaki["bbox"]) == pytest.approx(ITHAKI, abs=1e-5)
    assert ithaki["hierarchy"] == ["arts_and_entertainment", "nightlife_venue", "dance_club"]
    assert all(s["dataset"] != "Overture" for r in rows for s in r["sources"])  # its own stamp


def test_points_come_from_wkb_or_else_the_bbox():
    assert O.point_of(struct.pack("<BIdd", 1, 1, 33.37, 35.17)) == (35.17, 33.37)
    assert O.point_of(struct.pack(">BIdd", 0, 1, 33.37, 35.17)) == (35.17, 33.37)
    box = {"xmin": 33.0, "xmax": 33.2, "ymin": 35.0, "ymax": 35.2}
    assert O.point_of(None, box) == pytest.approx((35.1, 33.1))
    assert O.short_id("d5564327-fe43-4e04-aafe-559207dd70e2") == "ov-d5564327fe43"


def test_the_quality_rules(rows):
    places, dropped = O.curate(rows)
    by_name = {p["name"]: p for p in places}
    count = Counter(p["name"] for p in places)
    # A town-centre point seven places share, on as many streets, is no door, however sure
    # Overture is of the place (Kavana 144 at 0.87, a dance studio at 0.65).
    assert dropped["on a placeholder point"] == 7
    assert "Kavana 144" not in by_name and "Little Mover and Shaker" not in by_name
    # Confidence below 0.6, or closed, is left out.
    assert "Stage One Dance Studio" not in by_name and dropped["below the confidence threshold"]
    assert "Bulevard" not in by_name and dropped["closed"] == 1
    assert min(p["confidence"] for p in places) >= O.MIN_CONFIDENCE
    # One Meta record twice, 700 m apart: the copy Meta updated last stands.
    assert count["Φρουταρια Ιθακη - Froutaria Ithaki"] == 1
    assert by_name["Φρουταρια Ιθακη - Froutaria Ithaki"]["gers"] == "06b195bc-3410-4b90-b8ed-df5c3882eaff"
    assert count["Oxi Neighborhood Nicosia"] == 1  # the same, 30 m apart
    assert by_name["Oxi Neighborhood Nicosia"]["gers"] == "18215581-73dc-4050-9377-8caeeff174f9"
    assert dropped["an older copy of a record"] == 2
    # One name twice within 50 m, in two scripts, is one place.
    assert "Theatro Polis" in by_name and "Θέατρο Πόλης ΟΠΑΠ" not in by_name
    assert dropped["the same place twice"] == 1
    # A han and the shop inside it share a name, but aren't alike: two places.
    assert {"Büyük Han", "Büyük Han 121"} <= set(by_name)
    # Each keeps a stable id, its kind, the twin's category where they meet, its address as given.
    ithaki = by_name["Ithaki Venue"]
    assert ithaki["id"] == "ov-d5564327fe43" and ithaki["kind"] == "dance_club"
    assert "category" not in ithaki  # a dance club isn't one of the OSM index's categories
    assert by_name["Downtown Live"]["category"] == "theatre"
    assert by_name["Michalis Kyriakoudes Pharmacy"]["category"] == "pharmacy"
    assert "Şht. Mustafa Abdullah Sk" in by_name["AKARE Dance and Sport Center"]["address"]
    assert "utm_" not in ithaki["website"]
    assert len({p["id"] for p in places}) == len(places)


def test_free_text_matching():
    assert text_score("bars", ["Kafeneio"], ["bar"]) == 2
    assert text_score("bar", ["Kostas"], ["barber"]) == 0  # a kind matches whole words only
    assert text_score("dance", ["Danceworx"], ["fashion store"]) == 4  # the name holds it
    assert text_score("dance classes", ["Danceworx"], ["fashion store"]) == 3  # a word starts so
    assert text_score("dance school", ["Oxana's Ballet Studio"], ["dance studio"]) == 2
    assert text_score("dance school", ["Elena Clark School of Dance"], ["dance studio"]) == 4
    assert text_score("pharmacies", ["Kyriakoudes"], ["pharmacy"]) == 2
    assert text_score("Ledras", ["Λήδρας Street Cafe"], []) == 4  # Greek or Latin, either way
    assert text_score("yoga", ["Tango Studio"], ["dance studio"]) == 0


# -- searching -----------------------------------------------------------------------------------


def test_search_by_category_is_osm_first_without_overtures_twins(tmp_path, monkeypatch, ov):
    places = make_places(tmp_path, monkeypatch, ov)
    twins = ov.twins(places.all())
    croissanterie = next(p for p in ov.all() if p["name"] == "La Croissanterie Ledras")
    assert twins[croissanterie["id"]] == "n13418297272"  # the same café, 50 m or less apart
    found, source = places.find("cafe", near=ELEFTHERIA, radius_m=1000, limit=50)
    assert source == "OpenStreetMap and Overture Maps (cached)"
    names = [(p["name"], p["from"]) for p in found]
    assert ("La Croissanterie", "OpenStreetMap") in names
    assert ("La Croissanterie Ledras", "Overture Maps") not in names  # OSM wins
    assert ("Brew Lab", "Overture Maps") in names  # OSM's test index lacks it
    assert [p["distance_m"] for p in found] == sorted(p["distance_m"] for p in found)
    found, _ = places.find("cafe", limit=50)  # without a point: OSM's first
    assert [p["from"] for p in found][:2] == ["OpenStreetMap"] * 2


def test_search_by_text_reads_names_kinds_and_addresses(tmp_path, monkeypatch, ov):
    places = make_places(tmp_path, monkeypatch, ov)
    found, _ = places.find(query="dance schools", near=ELEFTHERIA, radius_m=1500)
    names = {p["name"] for p in found}
    assert {"Tango Studio, Nicosia", "Latin Dance School of Nicosia", "Oxana's Ballet Studio",
            "Flying Dolls"} <= names
    assert all(p["from"] == "Overture Maps" for p in found)
    assert "Stage One Dance Studio" not in names  # below the threshold
    assert "Central Night Club" not in names  # 'schools' are studios and schools, not clubs
    found, _ = places.find(query="dance club", near=ELEFTHERIA, radius_m=1500)
    assert {p["name"] for p in found} == {"Central Night Club", "Ithaki Venue", "S//IP"}
    found, _ = places.find(query="Nikiforou Foka")
    assert [p["name"] for p in found] == ["Ithaki Venue"]
    assert found[0]["address"] == "Nikiforou Foka 33, 1016 Λευκωσία"
    assert found[0]["confidence"] == "high" and found[0]["id"] == "ov-d5564327fe43"
    found, _ = places.find(query="yoga")
    assert [p["name"] for p in found] == ["Ashtanga Nicosia - Yoga Center"]
    found, _ = places.find(query="museum")  # OSM's category reads as text too
    assert [p["id"] for p in found] == ["n100"]


def test_find_places_answers_with_a_layer_and_linked_rows(tmp_path, monkeypatch, ov):
    places = make_places(tmp_path, monkeypatch, ov)
    ctx = ToolContext(Settings(), SimpleNamespace(now=lambda: __import__("datetime").datetime.now()),
                      places, None, SessionStore(), lambda e: None, "c1")
    res, err = run_tool(ctx, "find_places", {"query": "dance", "near_lat": ELEFTHERIA[0],
                                             "near_lon": ELEFTHERIA[1], "radius_m": 1200})
    assert not err, res
    assert res["matched_by_index"] == {"Overture Maps": res["matched"]}
    layer = ctx.store.layers[res["layer_ref"]]
    data = ctx.store.datasets[res["data_ref"]]
    assert data["link"] == {"key": "id"}
    assert {f["properties"]["id"] for f in layer["features"]} == {r["id"] for r in data["rows"]}
    assert layer["meta"]["sources"] == ["overture"]  # only what this answer drew from
    tango = next(f["properties"] for f in layer["features"] if f["properties"]["label"].startswith("Tango"))
    assert "From Overture Maps" in tango["popup"] and "Dance studio" in tango["popup"]
    # An Overture place is a place for routes, by its id or its name.
    assert resolve_place(ctx, "ov-d5564327fe43")["lat"] == pytest.approx(ITHAKI[0], abs=1e-5)
    assert resolve_place(ctx, "Ithaki Venue")["id"] == "ov-d5564327fe43"
    assert resolve_place(ctx, "Caffè Nero")["id"] == "n6255644748"  # OSM's first


# -- the inspector and search ---------------------------------------------------------------------


def test_an_overture_place_opens_in_the_inspector(tmp_path, monkeypatch, ov):
    places = make_places(tmp_path, monkeypatch, ov)
    ents = Entities(None, places)
    e = ents.get("ov-d5564327fe43")
    assert (e["kind"], e["kind_label"], e["name"]) == ("overture_place", "Place", "Ithaki Venue")
    assert e["center"] == pytest.approx([ITHAKI[1], ITHAKI[0]], abs=1e-5)
    attrs = {a["label"]: a["value"] for a in e["attributes"]}
    assert attrs["Confidence"] == "High (0.96 of 1)"
    assert attrs["Address"] == "Nikiforou Foka 33, 1016 Λευκωσία" and attrs["Kind"] == "Dance club"
    assert attrs["Overture id"] == "d5564327-fe43-4e04-aafe-559207dd70e2"
    src = e["sources"][0]
    assert src["source"] == "overture" and src["url"] == O.ATTRIBUTION
    assert "release 2026-11-18.0" in src["detail"] and "Meta" in src["detail"]
    sip = ents.get("ov-205cdb205308")
    assert {a["label"]: a["value"] for a in sip["attributes"]}["Confidence"].startswith("Good")
    # A place OSM has too says so, and Ctrl K shows OSM's copy only.
    twin = next(p for p in ov.all() if p["name"] == "La Croissanterie Ledras")
    rel = ents.get(twin["id"])["relations"]
    assert rel[0]["label"] == "On OpenStreetMap" and rel[0]["id"] == "n13418297272"
    hits = ents.search("ithaki")
    assert hits[0]["id"] == "ov-d5564327fe43" and hits[0]["kind"] == "overture_place"
    assert not any(h["id"] == twin["id"] for h in ents.search("croissanterie"))


# -- web places ----------------------------------------------------------------------------------


def test_a_web_result_takes_overtures_point_before_any_geocoding(tmp_path, monkeypatch, ov,
                                                                  no_geocoders):
    places = make_places(tmp_path, monkeypatch, ov, osm=[])
    web = WebPlaces(places, tmp_path / "web.json")
    place, why = web.locate({"name": "Ithaki", "address": "Nikiforou Foka 33, 1016 Nicosia"},
                            "LGBT-friendly bars")
    assert why is None and place["precision"] == "exact" and place["located_by"] == "Overture Maps"
    assert haversine_m(*ITHAKI, place["lat"], place["lon"]) < 50
    assert place["overture_id"] == "ov-d5564327fe43" and place["phone"] == "+35722250500"
    # Names and streets as the web writes them (from a live answer, 26 Nov 2026): the telling
    # words of a name, a street whose vowels are spelt otherwise, no postcode.
    bar, _ = web.locate({"name": "Ithaki Bar", "address": "Nikiforou Foka 33, Nicosia"})
    assert bar["overture_id"] == "ov-d5564327fe43"
    downtown, _ = web.locate({"name": "DownTown Live", "address": "Emanuel Roidi 2, Nicosia"})
    assert downtown["overture_id"] == "ov-836981895bfa"
    assert haversine_m(35.16492, 33.34973, downtown["lat"], downtown["lon"]) < 50
    # A near spelling counts only when the postcode and the street both agree.
    sip, _ = web.locate({"name": "S//P (SLIP)", "address": "Theofanous Theodotou, 1065, Nicosia"})
    assert sip["overture_id"] == "ov-205cdb205308"
    assert no_geocoders == []
    unsure, _ = web.locate({"name": "S//P (SLIP)", "address": "Theofanous Theodotou, Nicosia"})
    assert unsure is None or unsure.get("overture_id") is None
    # The web place relates to the Overture place in the inspector.
    e = Entities(None, places, web_places=web).get(place["id"])
    assert any(r["id"] == "ov-d5564327fe43" for r in e["relations"])
    assert e["sources"][-1]["source"] == "overture"


def test_overture_is_not_taken_when_the_address_disagrees(tmp_path, monkeypatch, ov, no_geocoders):
    web = WebPlaces(make_places(tmp_path, monkeypatch, ov, osm=[]), tmp_path / "web.json")
    # Another postcode is another place of that name (and the geocoders find nothing here).
    place, why = web.locate({"name": "Ithaki", "address": "Nikiforou Foka 33, 1036 Pallouriotissa"})
    assert place is None and why
    # With no address, only a name no other Overture place has.
    place, _ = web.locate({"name": "Ithaki"})  # Ithaki Venue and Froutaria Ithaki
    assert place is None
    place, _ = web.locate({"name": "Ithaki Venue"})
    assert place["overture_id"] == "ov-d5564327fe43"


def test_names_agree_on_their_telling_words():
    assert names_agree("Ithaki Bar", "Ithaki Venue") == 2
    assert names_agree("DownTown Live", "Downtown Live") == 2
    assert names_agree("S//P (SLIP)", "S//IP") == 1  # a near spelling
    assert names_agree("Lola Bar", "Lotus.") == 0
    assert names_agree("The Bar", "Bar 34") == 0  # nothing telling in common


# -- refreshing ----------------------------------------------------------------------------------


def test_a_refresh_failure_keeps_the_last_copy(tmp_path, monkeypatch, rows):
    path = tmp_path / "overture_places.json"
    asked = []
    monkeypatch.setattr(O, "latest_release", lambda: asked.append(1) or "2026-11-18.0")
    monkeypatch.setattr(O, "extract", lambda source, bbox=O.NICOSIA_BBOX: rows)
    ov = O.Overture(path)
    assert not ov.ready
    status = sources_routes._overture(SimpleNamespace(places=SimpleNamespace(overture=ov)))
    assert status["state"] == "waiting"
    ov.refresh_if_needed()
    n = len(ov.all())
    assert ov.ready and ov.release == "2026-11-18.0" and path.exists() and n > 20
    ov.refresh_if_needed()  # checked today: the bucket isn't asked again
    assert len(asked) == 1

    # A month on, the bucket can't be read: the copy stands, and the sources panel says so.
    ov.checked_at -= O.CHECK_EVERY_S + 1

    def down():
        raise OSError("HTTP 503 from S3")

    monkeypatch.setattr(O, "latest_release", down)
    ov.refresh_if_needed()
    assert ov.ready and len(ov.all()) == n and "503" in ov.error
    status = sources_routes._overture(SimpleNamespace(places=SimpleNamespace(overture=ov)))
    assert status["state"] == "stale" and status["release"] == "2026-11-18.0"
    assert "release 2026-11-18.0 of 18 Nov 2026" in status["note"] and "503" in status["note"]

    # A new release whose schema the query no longer fits fails the same way, and the retry
    # waits a day.
    ov._failed_at = 0.0
    monkeypatch.setattr(O, "latest_release", lambda: "2026-12-16.0")

    def changed(source, bbox=None):
        raise ValueError('Binder Error: "taxonomy" not found')

    monkeypatch.setattr(O, "extract", changed)
    ov.refresh_if_needed()
    assert ov.release == "2026-11-18.0" and "taxonomy" in ov.error
    monkeypatch.setattr(O, "extract", lambda *a, **k: pytest.fail("retried within the day"))
    ov.refresh_if_needed()

    # A restart reads the last good copy from disk.
    again = O.Overture(path)
    assert again.ready and again.release == "2026-11-18.0" and len(again.all()) == n
    assert again.get("ov-d5564327fe43")["name"] == "Ithaki Venue"
    status = sources_routes._overture(SimpleNamespace(places=SimpleNamespace(overture=again)))
    assert status["state"] == "live" and f"{n:,} places" in status["note"]


def test_overture_is_a_source_of_every_tool_that_reads_it():
    src = sources.BY_ID["overture"]
    assert src.publisher.startswith("Overture Maps Foundation") and "CDLA-Permissive-2.0" in src.licence
    assert src.url == O.ATTRIBUTION and src.credit
    for tool in ("find_places", "locate_web_places", "plan_route", "plan_transit", "reachable_area"):
        assert "overture" in sources.TOOL_SOURCES[tool], tool
