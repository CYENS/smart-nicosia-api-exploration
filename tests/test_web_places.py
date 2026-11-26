"""Places found on the web, put on the map: located from the OSM index or their address without
guessing, kept under a stable id, drawn as a layer, opened in the inspector and usable as a place
in routes. The geocoders answer from answers captured on 26 Nov 2026 (no network)."""

import pytest
from conftest import load

from chatbot import geo as geo_mod
from chatbot.entities import Entities
from chatbot.geo import Places
from chatbot.settings import Settings
from chatbot.store import SessionStore
from chatbot.tools import ToolContext, resolve_place, run_tool
from chatbot.web import Throttle
from chatbot.webplaces import (
    WebPlaces,
    address_streets,
    clean_url,
    respell,
    street_only,
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
    assert layer["meta"]["sources"] == ["web_search", "geocoding", "openstreetmap"]
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
