"""Replay real past hazards into a scratch memory, moved to now, to watch the twin raise them.

For reviewing and demos only, never on the everyday memory: it writes the 12 November 2025 M5.2
earthquake and its first aftershocks (EMSC, with their real felt reports) so the M5.2 happened
`--ago` minutes ago, the Orange heat warnings of 24 to 26 July 2025 (Meteoalarm) so the first is in
force now, and the extreme fire danger of 23 July 2025 (EFFIS) as today's and yesterday's. Each
row keeps `replay_of`, the moment it really happened. The payloads are the test fixtures.

    uv run python tools/hazards_replay.py data/hazards_replay.duckdb
    CHATBOT_HAZARDS=0 COLLECTOR_DB=data/hazards_replay.duckdb CHATBOT_PORT=8270 uv run chatbot

With CHATBOT_HAZARDS=0 the server doesn't read the live feeds (which would withdraw the replayed
warning at once), so the inbox, the map and the chat see the replay alone.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from chatbot.hazards import Hazards
from chatbot.hazards import feeds as F
from chatbot.memory import Memory
from chatbot.settings import Settings

FX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hazards"


def moved(h: F.Hazard, by: timedelta) -> F.Hazard:
    return replace(h, start=h.start + by, end=h.end + by if h.end else None,
                   issued=h.issued + by if h.issued else None,
                   details={**h.details, "replay_of": F.wall(h.start)})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", type=Path, help="a scratch memory file (created if missing)")
    ap.add_argument("--ago", type=float, default=40.0, help="minutes since the M5.2 (default 40)")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    hz = Hazards(Settings(), Memory(a.db), now=lambda: now)

    quakes = F.parse_emsc(json.loads((FX / "emsc_2025-11-12.json").read_text(encoding="utf-8")))
    main_shock = next(q for q in quakes if q.id == "eq-20251112_0000180")
    by = now - timedelta(minutes=a.ago) - main_shock.start
    reports = F.parse_felt_zip((FX / "emsc_felt_20251112_0000180.zip").read_bytes())
    felt = F.felt_summary(reports, 3491, now)
    n = 0
    for q in quakes:
        q = moved(q, by)
        if q.start > now:
            continue  # the M5.3 came five hours later: not yet
        if q.id == main_shock.id:
            q = F.with_felt(q, felt)
        hz._put(q)
        n += 1

    warnings = F.parse_meteoalarm_atom((FX / "meteoalarm_2025-07-25.xml").read_text(encoding="utf-8"))
    first = min(warnings, key=lambda w: w.start)
    by_w = now - timedelta(hours=1) - first.start
    for w in warnings:
        w = moved(w, by_w)
        if (w.issued or w.start) <= now:
            hz._put(w)

    today = now.astimezone(F.TZ).date()
    png = (FX / "effis_fwi_2025-07-23.png").read_bytes()
    for day in (today - timedelta(days=1), today):
        fire = F.parse_fire_png(png, day, now)
        fire.details = {**fire.details, "replay_of": date(2025, 7, 23).isoformat(), "forecast": False}
        hz._put(fire)
    assert hz.save(), "the memory was busy; try again"
    print(f"replayed into {a.db}: {n} quakes (the M5.2 {a.ago:.0f} min ago), the first Orange heat "
          f"warning in force, extreme fire danger yesterday and today")


if __name__ == "__main__":
    main()
