"""Places found on the web, put on the map: located from the OSM index or their address without
guessing, kept under a stable id, drawn as a layer, opened in the inspector and usable as a place
in routes. The geocoders answer from answers captured on 26 Nov 2026 (no network)."""

import json

import pytest
from conftest import load

from chatbot import geo as geo_mod
from chatbot.entities import Entities
from chatbot.geo import Places, haversine_m
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, resolve_place, run_tool
from chatbot.web import Throttle
from chatbot.webplaces import (
    PLACER,
    WebPlaces,
    address_areas,
    address_postcode,
    address_streets,
    by_postcode,
    clean_url,
    respell,
    street_only,
    streets_apart,
    web_id,
)

GEO = load("geocode_web_places.json")
SINGING = {"name": "The Art of Singing Studio", "address": "Yiannaki Talioti 7, Strovolos",
           "phone": "+357 99 475795", "website": "artofsinging.example",
           "source_url": "https://example.com/singing-teachers", "note": "Voice lessons from beginner up"}
LYCEUM = {"name": "Music Lyceum", "note": "Classical voice"}
NO_ADDRESS = {"name": "A.MUS.ED", "note": "One-to-one lessons"}
TOWN_ONLY = {"name": "Tatiana Stupak School of Music", "address": "Strovolos, Nicosia"}
STREET = (35.1487, 33.3708)  # where the captured answers put Γιαννάκη Ταλιώτη


class FakeGeocoders:
    """Nominatim and Photon, answering the captured queries and nothing for any other."""

    def __init__(self, nominatim: bool = True):
        self.nominatim = nominatim
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, *, user_agent, params=None, data=None, timeout=30.0):
        q = params["q"]
        if url.endswith("/search"):
            self.calls.append(("nominatim", q))
            return GEO["nominatim"].get(q, []) if self.nominatim else []
        self.calls.append(("photon", q))
        return GEO["photon"].get(q, {"features": []})


@pytest.fixture
def geocoders(monkeypatch):
    fake = FakeGeocoders()
    monkeypatch.setattr(geo_mod, "get_json", fake)
    return fake


def make_places(tmp_path, monkeypatch, places=()):
    monkeypatch.setattr(geo_mod, "CACHE_DIR", tmp_path)
    p = Places(Settings())
    p._places = list(places)
    p._nominatim = Throttle(0)  # the real one waits a second between requests
    return p


def test_spelling_and_address_helpers():
    assert respell("Yiannaki Talioti 7, Strovolos") == "Giannaki Talioti 7, Strovolos"
    assert respell("Ayios Dometios, Yeoryiou Griva") == "Agios Dometios, Georgiou Griva"
    assert street_only("Yiannaki Talioti 7, 2014 Strovolos, Cyprus") == "Yiannaki Talioti, Strovolos"
    assert street_only("Ledras") is None
    assert address_streets("Office 3, 12 Makariou Ave, Strovolos, Cyprus") == ["Makariou"]
    assert address_streets("Yiannaki Talioti 7") == ["Yiannaki Talioti"]
    assert clean_url("www.example.com/a") == "https://www.example.com/a"
    assert clean_url("javascript:alert(1)") is None
    assert web_id("Music  Lyceum", None) == web_id("music lyceum", "") and web_id("x", None).startswith("web-")


def test_a_place_found_only_as_far_as_its_street_is_pinned_on_the_street(tmp_path, monkeypatch, geocoders):
    web = WebPlaces(make_places(tmp_path, monkeypatch), tmp_path / "web.json")
    place, why = web.locate(SINGING, "Singing lessons")
    assert why is None
    assert place["precision"] == "street" and place["located_by"] == "its street (Nominatim)"
    assert place["matched"] == "Γιαννάκη Ταλιώτη"
    assert abs(place["lat"] - STREET[0]) < 0.01 and abs(place["lon"] - STREET[1]) < 0.01
    # The usual Latin spelling missed; the other spelling of Γιαννάκη found the street.
    assert ("nominatim", "Giannaki Talioti 7, Strovolos") in geocoders.calls
    assert place["website"] == "https://artofsinging.example"
    assert place["sources"] == ["https://example.com/singing-teachers"]
    assert place["topics"] == ["Singing lessons"]

    # Asked again (in this run or after a restart), it costs no request.
    before = len(geocoders.calls)
    again, _ = web.locate({**SINGING, "phone": None}, "Voice lessons")
    assert len(geocoders.calls) == before and again["id"] == place["id"]
    assert again["phone"] == "+357 99 475795" and again["topics"] == ["Singing lessons", "Voice lessons"]
    reread = WebPlaces(web.places, tmp_path / "web.json")
    assert reread.get(place["id"])["name"] == "The Art of Singing Studio"
    assert reread.named("the art of singing studio")["id"] == place["id"]


def test_a_house_number_on_another_street_is_not_taken_for_the_door(tmp_path, monkeypatch):
    fake = FakeGeocoders(nominatim=False)  # Nominatim finds nothing, so Photon answers
    monkeypatch.setattr(geo_mod, "get_json", fake)
    web = WebPlaces(make_places(tmp_path, monkeypatch), tmp_path / "web.json")
    # Photon answers "Giannaki Talioti 7" with the street and with number 7 of Giannaki Kapitani.
    hits = GEO["photon"]["Giannaki Talioti 7, Strovolos"]["features"]
    assert any(f["properties"].get("street") == "Giannaki Kapitani" for f in hits)
    place, _ = web.locate(SINGING)
    assert place["precision"] == "street" and place["located_by"] == "its street (Photon)"
    assert place["matched"] == "Γιαννάκη Ταλιώτη"


def test_a_place_in_the_osm_index_stands_at_its_door_and_vague_ones_are_not_placed(
        tmp_path, monkeypatch, geocoders):
    places = make_places(tmp_path, monkeypatch, [
        {"id": "n55", "category": "theatre", "name": "Music Lyceum", "lat": 35.1601, "lon": 33.3702}])
    web = WebPlaces(places, tmp_path / "web.json")
    place, _ = web.locate(LYCEUM)
    assert (place["precision"], place["osm_id"], place["lat"]) == ("exact", "n55", 35.1601)
    assert geocoders.calls == []
    # A town is not a position, and a name nobody maps can't be placed either.
    assert web.locate(TOWN_ONLY) == (None, "its address isn't on the map of Nicosia")
    assert web.locate(NO_ADDRESS) == (None, "no address in the result, and its name isn't on the map")


def test_the_tool_draws_them_lists_what_it_could_not_place_and_the_twin_knows_them(
        tmp_path, monkeypatch, geocoders):
    places = make_places(tmp_path, monkeypatch, [
        {"id": "n55", "category": "theatre", "name": "Music Lyceum", "lat": 35.1601, "lon": 33.3702}])
    web = WebPlaces(places, tmp_path / "web.json")
    events: list[dict] = []
    ctx = ToolContext(Settings(), None, places, None, SessionStore(), events.append, "c1",
                      user_location={"lat": 35.1700, "lon": 33.3600, "source": "gps"}, web_places=web)
    res, err = run_tool(ctx, "locate_web_places", {
        "topic": "Singing lessons", "places": [SINGING, NO_ADDRESS, LYCEUM]})
    assert not err, res
    assert (res["placed"], res["of"]) == (2, 3)
    assert [p["name"] for p in res["places"]] == ["Music Lyceum", "The Art of Singing Studio"]  # nearest first
    assert res["places"][0]["distance_m"] < res["places"][1]["distance_m"]
    assert res["not_placed"][0]["name"] == "A.MUS.ED" and "hint" in res

    layer = ctx.store.layers[res["layer_ref"]]
    assert layer["meta"]["title"] == "Singing lessons" and layer["meta"]["count"] == 2
    assert set(layer["meta"]["legend"]) == {"from the web", "on its street", "you"}
    assert layer["meta"]["sources"] == ["web_search", "geocoding", "openstreetmap", "overture"]
    you = layer["features"][-1]["properties"]  # the distances are from here; the camera fits it in
    assert (you["id"], len(layer["features"])) == ("you", 3)
    singing = next(f["properties"] for f in layer["features"] if f["properties"]["label"].startswith("The Art"))
    assert singing["state"] == "on its street" and "not at the door" in singing["popup"]
    data = ctx.store.datasets[res["data_ref"]]
    assert data["link"] == {"key": "id"} and {r["id"] for r in data["rows"]} == {p["id"] for p in res["places"]}

    _, err = run_tool(ctx, "show_on_map", {"layer_ref": res["layer_ref"], "style": "pulse"})
    assert not err and events[-1]["layer"]["legend"] == layer["meta"]["legend"]

    # The inspector, search and routes know them by id (and by name).
    sid = singing["id"]
    e = Entities(None, places, web_places=web).get(sid)
    assert e["kind"] == "web_place" and e["kind_label"] == "From the web"
    assert e["subtitle"] == "Singing lessons · Yiannaki Talioti 7, Strovolos"
    labels = {a["label"]: a["value"] for a in e["attributes"]}
    assert labels["Phone"] == "+357 99 475795" and labels["Website"] == "https://artofsinging.example"
    assert labels["On the map"] == ("On its street, Γιαννάκη Ταλιώτη, not at the door: the map "
                                    "knows the street but not the number")
    assert [(s["source"], s["url"]) for s in e["sources"]] == [
        ("web_search", "https://example.com/singing-teachers"), ("geocoding", None)]
    lyceum = Entities(None, places, web_places=web).get(res["places"][0]["id"])
    assert {"relation": "place", "label": "On OpenStreetMap", "id": "n55", "kind": "place",
            "name": "Music Lyceum"} in lyceum["relations"]
    assert sid in [h["id"] for h in Entities(None, places, web_places=web).search("singing")]
    assert resolve_place(ctx, sid)["id"] == sid
    assert resolve_place(ctx, "The Art of Singing Studio")["lat"] == e["center"][1]


def test_nothing_placed_means_no_layer(tmp_path, monkeypatch, geocoders):
    web = WebPlaces(make_places(tmp_path, monkeypatch), tmp_path / "web.json")
    ctx = ToolContext(Settings(), None, web.places, None, SessionStore(), [].append, "c1",
                      web_places=web)
    res, err = run_tool(ctx, "locate_web_places", {"places": [TOWN_ONLY]})
    assert not err and res["placed"] == 0 and "layer_ref" not in res
    assert res["not_placed"][0]["reason"] == "its address isn't on the map of Nicosia"


# Greater Nicosia has three streets called Nikiforou Foka: the avenue by Famagusta Gate (1016 and
# 1017), one in Pallouriotissa (1036) and one in Latsia (2236). OpenStreetMap names the avenue
# only in Greek, so Nominatim's Latin search finds the other two; Photon finds all three.
FAMAGUSTA_GATE = (35.1719, 33.3714)
ITHAKI = {"name": "Ithaki", "address": "Nikiforou Foka 33, 1016 Nicosia",
          "source_url": "https://www.travelgay.com/cyprus-gay-bars-clubs"}
ITHAKI_NO_POSTCODE = {**ITHAKI, "address": "Nikiforou Foka 33, Nicosia"}


def test_address_helpers_for_postcodes_areas_and_namesakes():
    assert address_postcode("Nikiforou Foka 33, 1016 Nicosia") == "1016"
    assert address_postcode("Emanuel Roidi 2, Strovolos, Nicosia 1095") == "1095"
    assert address_postcode("Nikiforou Foka 33, Nicosia") is None
    assert address_areas("Emanuel Roidi 2, Strovolos, Nicosia 1095") == ["Strovolos"]
    assert address_areas("Nikiforou Foka 33, 1016 Nicosia") == []
    hits = [{"lat": 35.1790, "lon": 33.3816, "postcode": "1036"},
            {"lat": 35.1728, "lon": 33.3704, "address": "Λεωφόρος Νικηφόρου Φωκά, Nicosia, 1017"},
            {"lat": 35.1710, "lon": 33.3690, "postcode": "1016"},
            {"lat": 35.1122, "lon": 33.3683}]
    assert by_postcode(hits, "1016") == [hits[2], hits[1]]  # the same postcode, then the next block
    assert by_postcode(hits, "9999") == [hits[3]]  # only one that doesn't say its postcode
    assert by_postcode(hits, None) == hits
    assert [len(g) for g in streets_apart(hits)] == [1, 2, 1]


def test_a_postcode_tells_streets_of_the_same_name_apart(tmp_path, monkeypatch, geocoders):
    web = WebPlaces(make_places(tmp_path, monkeypatch), tmp_path / "web.json")
    place, why = web.locate(ITHAKI, "Bars")
    assert why is None
    assert place["precision"] == "street" and "Φωκά" in place["matched"]
    assert haversine_m(place["lat"], place["lon"], *FAMAGUSTA_GATE) < 400  # not Pallouriotissa


def test_streets_of_the_same_name_with_nothing_to_tell_them_apart_are_not_guessed(
        tmp_path, monkeypatch, geocoders):
    web = WebPlaces(make_places(tmp_path, monkeypatch), tmp_path / "web.json")
    place, why = web.locate(ITHAKI_NO_POSTCODE, "Bars")
    assert place is None
    assert "3 streets called Nikiforou Foka" in why and "postcode" in why
    # A building called "Ithaki" in Strovolos is near none of them, so the name doesn't place it.


def test_a_place_kept_by_the_old_placer_is_placed_again(tmp_path, monkeypatch, geocoders):
    path = tmp_path / "web.json"
    wrong = {"lat": 35.178977, "lon": 33.38162, "precision": "street", "matched": "Nikiforou Foka",
             "located_by": "its street (Nominatim)", "sources": ["https://example.com/a"],
             "topics": ["Bars"], "phone": "+357 22 250500", "found_at": 1}
    stale = [{"id": web_id("Ithaki", a), "name": "Ithaki", "address": a, **wrong}
             for a in (ITHAKI["address"], ITHAKI_NO_POSTCODE["address"])]
    path.write_text(json.dumps({"places": stale}), encoding="utf-8")
    web = WebPlaces(make_places(tmp_path, monkeypatch), path)

    place, why = web.locate(ITHAKI, "Bars")  # moved to the avenue, keeping what was known
    assert why is None and haversine_m(place["lat"], place["lon"], *FAMAGUSTA_GATE) < 400
    assert place["phone"] == "+357 22 250500" and "https://example.com/a" in place["sources"]
    assert place["placer"] == PLACER

    place, why = web.locate(ITHAKI_NO_POSTCODE, "Bars")  # can't be told apart: the pin goes
    assert place is None and web.get(stale[1]["id"]) is None
    assert web.get(stale[0]["id"]) is not None


def test_places_that_were_right_stay_right(tmp_path, monkeypatch, geocoders):
    web = WebPlaces(make_places(tmp_path, monkeypatch), tmp_path / "web.json")
    # Downtown Live: its address's spelling isn't on the map, but the venue is, by name, on the
    # corner of Emmanouil Roidi.
    live, why = web.locate({"name": "DownTown Live",
                            "address": "Emanuel Roidi 2, Strovolos, Nicosia 1095"})
    assert why is None and live["precision"] == "exact"
    assert haversine_m(live["lat"], live["lon"], 35.16486, 33.35019) < 100
    # S//IP: Theofanous Theodotou, at its street (plus code 5995+6MP is 35.1680, 33.3586).
    slip, why = web.locate({"name": "S//IP Club", "address": "Theofanous Theodotou 18, 1065 Nicosia"})
    assert why is None and slip["precision"] == "street"
    assert haversine_m(slip["lat"], slip["lon"], 35.1680, 33.3586) < 150
