"""Record the browser tests' API fixtures from a running chatbot, trimmed small.

    uv run python tests/e2e/record_fixtures.py [CHATBOT_URL]    (default http://127.0.0.1:8000)

Only GET endpoints the page reads on its own are recorded; /api/chat never is (its streams are
written by hand in streams.py). Long lists are cut to a handful of features, so the fixtures stay
small and the tests stay fast. The moment of the recording is kept in meta.json: the tests set
the page's clock to it, so "now" on the page matches the data. Re-record when an endpoint's shape
changes, then run `uv run --group e2e pytest -m e2e` and fix what the new data moved.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parent / "fixtures"
NICOSIA = ZoneInfo("Asia/Nicosia")
DEVICE = "YL1015"  # the street light the deep link and the inspector open
SEARCH = "YL10"  # what the search test types

# How many features each list keeps.
KEEP = {
    "devices/parking_sensor": 24, "devices/waste_bin": 16, "devices/air_station": 6,
    "devices/camera": 8, "alarms": 8, "lamps": 30, "roads": 12, "transit/stops": 30,
    "dangerous": 12,
}


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=60) as r:  # noqa: S310 - a local server
        return json.loads(r.read())


def trim(body, n: int):
    if isinstance(body, dict) and isinstance(body.get("features"), list):
        body["features"] = body["features"][:n]
    return body


def save(name: str, body) -> None:
    path = OUT / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{path.relative_to(OUT)}: {path.stat().st_size:,} bytes")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # A moment a day back, on the quarter hour, as the clock asks for it (Nicosia wall time).
    past = (now - timedelta(days=1)).astimezone(NICOSIA)
    past = past.replace(minute=past.minute - past.minute % 15).strftime("%Y-%m-%dT%H:%M")
    q = urllib.parse.urlencode

    health = get(base, "/api/health")
    health["has_credentials"] = True  # the conversation shows no "no API key" alert
    save("health", health)
    save("overview", get(base, "/api/overview"))
    save("overview_past", get(base, f"/api/overview?{q({'at': past})}"))
    save("sources", get(base, "/api/sources"))
    save("clock", get(base, "/api/clock?days=31"))
    for kind in ("parking_sensor", "waste_bin", "air_station"):
        save(f"clock_states/{kind}", get(base, f"/api/clock/states?{q({'kind': kind, 'at': past})}"))
    for name, n in KEEP.items():
        try:
            save(name, trim(get(base, f"/api/{name}"), n))
        except Exception as e:  # noqa: BLE001 - a missing optional endpoint is not fatal
            print(f"{name}: skipped ({e})")
    save("lamps_past", trim(get(base, f"/api/lamps?{q({'at': past})}"), KEEP["lamps"]))
    save("mobility", get(base, "/api/mobility"))
    buses = get(base, "/api/buses")
    buses["buses"] = buses["buses"][:6]
    buses["count"] = len(buses["buses"])
    save("buses", buses)
    shape_id = buses["buses"][0]["shape_id"] if buses["buses"] else None
    if shape_id:
        save("transit/shape", get(base, f"/api/transit/shapes/{urllib.parse.quote(shape_id)}"))
    save(f"entities/{DEVICE}", get(base, f"/api/entities/{DEVICE}?series=false"))
    save(f"entities/{DEVICE}_past", get(base, f"/api/entities/{DEVICE}?{q({'series': 'false', 'at': past})}"))
    save(f"entities/{DEVICE}_series", get(base, f"/api/entities/{DEVICE}/series"))
    metric = "power_kw"  # what the inspector's sparkline asks a street light for
    until = now.astimezone(NICOSIA).strftime("%Y-%m-%dT%H:%M")
    save(f"entities/{DEVICE}_history", get(base, f"/api/history/{DEVICE}?{q({'hours': 24, 'metric': metric, 'until': until})}"))
    search = get(base, f"/api/entities/search?{q({'q': SEARCH})}")
    search["results"] = search["results"][:6]
    save("search", search)
    save("meta", {
        "recorded_at": now.isoformat().replace("+00:00", "Z"),
        "past": past,
        "device": DEVICE,
        "search": SEARCH,
        "from": base,
    })


if __name__ == "__main__":
    main()
