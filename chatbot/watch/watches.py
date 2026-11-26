"""Watches: rules someone asked the twin to keep an eye on, raised the moment they come true.

"Tell me when bin YW1394 passes 90%", "when the air turns poor near me", "if any lamp on
Mnasiadou goes offline", "any new high-severity incident". A rule names a kind of thing, which
ones (ids, a street, or a circle around a point), and a condition: a reading above or below a
value, or a state among some. It is checked whenever fresh readings of that kind arrive (and
incident rules whenever the incidents change), and it fires when the condition turns true for
a device, once per crossing: it fires again only after it has been false.

The chatbot never saves a watch. `create_watch` makes a proposal; the page shows it, and only a
person's click saves it (`Watches.confirm`). Watches are shared by every page of this twin.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

from chatbot.city import KINDS, DeviceState
from chatbot.geo import haversine_m
from chatbot.memory import Memory, Unavailable
from chatbot.settings import TIMEZONE
from chatbot.watch.detectors import RANK

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id VARCHAR PRIMARY KEY,
    created_ts TIMESTAMPTZ,
    by_name VARCHAR,
    label VARCHAR,
    rule VARCHAR,                    -- JSON
    armed VARCHAR,                   -- JSON: device -> whether the condition held last time
    deleted BOOLEAN DEFAULT false
);
CREATE TABLE IF NOT EXISTS watch_fires (
    watch VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    device VARCHAR NOT NULL,
    message VARCHAR,
    value DOUBLE,
    PRIMARY KEY (watch, ts, device)
);
"""

# The readings a watch can compare, per kind: name -> (DeviceState detail key, unit, words).
METRICS: dict[str, dict[str, tuple[str, str, str]]] = {
    "waste_bin": {"fill_pct": ("level_pct", "%", "fill")},
    "air_station": {"caqi": ("caqi", "", "CAQI"), "noise_db": ("noise_db", " dB", "noise"),
                    "pm2_5": ("pm2_5", " µg/m³", "PM2.5"), "pm10": ("pm10", " µg/m³", "PM10"),
                    "no2": ("no2", " µg/m³", "NO2")},
    "street_light": {"power_w": ("power_w", " W", "power")},
}
STATES: dict[str, tuple[str, ...]] = {
    "street_light": ("on", "off", "offline"),
    "parking_sensor": ("free", "occupied"),
    "waste_bin": ("low", "medium", "high", "full"),
    "air_station": ("very_low", "low", "medium", "high", "very_high"),
    "info_screen": ("ok", "no_heartbeat", "not_reported"),
    "camera": ("connected", "disconnected"),
}
STATE_WORDS = {"very_low": "very good", "low": "good", "medium": "moderate", "high": "poor",
               "very_high": "very poor", "no_heartbeat": "without a heartbeat",
               "not_reported": "not reporting"}
NOUN = {"street_light": "lamp", "parking_sensor": "parking bay", "waste_bin": "bin",
        "air_station": "air station", "info_screen": "info screen", "camera": "camera"}
PROPOSAL_TTL = timedelta(minutes=30)
MAX_WATCHES = 50
MAX_FIRES = 200


class WatchError(ValueError):
    """A rule that can't be watched; the message says what to change."""


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _local(t: datetime | int | None) -> str | None:
    if t is None:
        return None
    if isinstance(t, int):
        t = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
    return t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


@dataclass
class Rule:
    kind: str  # a device kind, or "incident"
    ids: list[str] | None = None
    street: str | None = None
    near: dict | None = None  # {lat, lon, radius_m, label}
    metric: str | None = None
    op: str | None = None  # above | below
    value: float | None = None
    states: list[str] | None = None
    severity: str | None = None  # incident rules: the least severity that counts

    def check(self) -> None:
        if self.kind == "incident":
            if self.severity and self.severity not in RANK:
                raise WatchError("severity must be high, medium or low")
            return
        if self.kind not in KINDS:
            raise WatchError(f"kind must be one of {', '.join([*KINDS, 'incident'])}")
        if self.metric:
            if self.metric not in METRICS.get(self.kind, {}):
                have = ", ".join(METRICS.get(self.kind, {})) or "none (use states)"
                raise WatchError(f"a {NOUN[self.kind]} has no reading {self.metric!r}; readings: {have}")
            if self.op not in ("above", "below") or self.value is None:
                raise WatchError("a reading needs op (above or below) and a value")
        elif self.states:
            bad = [s for s in self.states if s not in STATES[self.kind]]
            if bad:
                raise WatchError(f"a {NOUN[self.kind]} has no state {', '.join(bad)}; states: "
                                 f"{', '.join(STATES[self.kind])}")
        else:
            raise WatchError("give a condition: a metric with op and value, or states")
        if not (self.ids or self.street or self.near):
            raise WatchError("say which ones to watch: ids, a street, or near a place")

    def label(self) -> str:
        if self.kind == "incident":
            sev = {"high": "high-severity ", "medium": "medium or high ", None: "", "low": ""}[self.severity]
            where = self._where()
            return f"Any new {sev}incident{(' ' + where) if where else ''}"
        noun = NOUN[self.kind]
        if self.ids and len(self.ids) == 1:
            who = f"{noun.capitalize()} {self.ids[0]}"
        elif self.ids:
            who = f"{len(self.ids)} {noun}s ({', '.join(self.ids[:3])}{'...' if len(self.ids) > 3 else ''})"
        else:
            who = f"Any {noun} {self._where()}"
        return f"{who} {self.condition()}"

    def condition(self) -> str:
        if self.metric:
            _, unit, words = METRICS[self.kind][self.metric]
            v = f"{self.value:g}{unit}"
            if self.metric == "fill_pct":
                return f"passes {v}" if self.op == "above" else f"drops below {v}"
            return f"{words} {'above' if self.op == 'above' else 'below'} {v}"
        words = [STATE_WORDS.get(s, s.replace("_", " ")) for s in self.states or []]
        if self.kind == "air_station" and set(self.states or []) == {"high", "very_high"}:
            return "turns poor or worse"
        return f"turns {' or '.join(words)}"

    def _where(self) -> str:
        if self.street:
            return f"on {self.street}"
        if self.near:
            r = self.near.get("radius_m", 500)
            return f"within {r:g} m of {self.near.get('label') or 'the point'}"
        return ""

    def matches_device(self, s: DeviceState) -> bool:
        if s.kind != self.kind:
            return False
        if self.ids:
            return s.device.name.lower() in {i.lower() for i in self.ids}
        if self.street:
            return (s.street or "").lower() == self.street.lower()
        if self.near:
            if s.device.lat is None:
                return False
            return haversine_m(self.near["lat"], self.near["lon"], s.device.lat,
                               s.device.lon) <= self.near.get("radius_m", 500)
        return False

    def holds(self, s: DeviceState) -> tuple[bool | None, float | None]:
        """(whether the condition holds, the value compared). None when the reading can't
        tell (stale, or no value)."""
        i = s.interp
        if self.metric:
            if i.stale:
                return None, None
            key = METRICS[self.kind][self.metric][0]
            v = i.detail.get(key)
            if not isinstance(v, (int, float)):
                return None, None
            return (v > self.value) if self.op == "above" else (v < self.value), float(v)
        if i.state == "unknown":
            return None, None
        if i.stale and not (self.kind == "street_light" and i.state == "offline"):
            return None, None
        return i.state in (self.states or []), None

    def matches_incident(self, inc: dict) -> bool:
        if self.kind != "incident":
            return False
        if self.severity and RANK.get(inc["severity"], 0) < RANK[self.severity]:
            return False
        if self.street and self.street.lower() not in (inc.get("place") or "").lower():
            return False
        if self.near and inc.get("lat") is not None:
            return haversine_m(self.near["lat"], self.near["lon"], inc["lat"],
                               inc["lon"]) <= self.near.get("radius_m", 500)
        return True


@dataclass
class Watch:
    id: str
    rule: Rule
    label: str
    created: int
    by: str | None = None
    armed: dict[str, bool] = field(default_factory=dict)
    fires: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.rule.kind,
                "rule": {k: v for k, v in asdict(self.rule).items() if v not in (None, [])},
                "created": _local(self.created), "by": self.by,
                "watching": len(self.armed), "true_now": sorted(d for d, v in self.armed.items() if v),
                "last_fired": self.fires[-1]["at"] if self.fires else None,
                "fires": self.fires[-5:][::-1]}


class Watches:
    """The saved watches, their proposals waiting for a click, and what they fired."""

    def __init__(self, memory: Memory | None, now: Callable[[], datetime]):
        self.memory = memory
        self.now = now
        self.lock = threading.RLock()
        self.items: dict[str, Watch] = {}
        self.proposals: dict[str, tuple[float, Watch]] = {}
        self.loaded = False

    # -- storage ---------------------------------------------------------------------------------

    def load(self) -> None:
        if self.loaded:
            return
        rows, fires = [], []
        if self.memory is not None and self.memory.exists():
            try:
                with self.memory.reading(timeout=5.0) as con:
                    rows = con.execute(
                        """SELECT id, epoch_ms(created_ts), by_name, label, rule, armed FROM watches
                           WHERE NOT coalesce(deleted, false) ORDER BY created_ts""").fetchall()
                    fires = con.execute(
                        """SELECT watch, epoch_ms(ts), device, message, value FROM watch_fires
                           ORDER BY ts""").fetchall()
            except duckdb.CatalogException:
                rows, fires = [], []
            except Unavailable:
                return
        with self.lock:
            for wid, created, by, label, rule, armed in rows:
                try:
                    r = Rule(**json.loads(rule))
                    self.items[wid] = Watch(wid, r, label, created, by, json.loads(armed or "{}"))
                except (TypeError, ValueError) as e:
                    log.warning("watch %s can't be read: %s", wid, e)
            for wid, t, device, message, value in fires:
                if wid in self.items:
                    self.items[wid].fires.append({"at": _local(t), "t_ms": t, "device": device,
                                                  "message": message, "value": value})
            self.loaded = True

    def _write(self, fn) -> None:
        if self.memory is None:
            return
        try:
            with self.memory.writing(timeout=5.0) as con:
                con.execute(SCHEMA)
                fn(con)
        except Exception as e:  # noqa: BLE001 - the watch still works in this process
            log.warning("watches: could not save: %s", e)

    def _save(self, w: Watch, deleted: bool = False) -> None:
        self._write(lambda con: con.execute(
            "INSERT OR REPLACE INTO watches VALUES (?, ?, ?, ?, ?, ?, ?)",
            [w.id, datetime.fromtimestamp(w.created / 1000, tz=timezone.utc), w.by, w.label,
             json.dumps(asdict(w.rule)), json.dumps(w.armed), deleted]))

    # -- proposals and saving ---------------------------------------------------------------------

    def propose(self, rule: Rule, states: list[DeviceState]) -> dict:
        """A watch waiting for a person's click, with what holds right now."""
        rule.check()
        now = self.now()
        w = Watch(uuid.uuid4().hex[:10], rule, rule.label(), _ms(now))
        matched = [s for s in states if rule.matches_device(s)] if rule.kind != "incident" else []
        now_true = []
        for s in matched:
            ok, v = rule.holds(s)
            if ok is not None:
                w.armed[s.device.name] = ok
            if ok:
                now_true.append({"id": s.device.name, "value": v, "state": s.interp.state})
        if rule.kind != "incident" and not matched and (rule.ids or rule.street):
            raise WatchError(f"no {NOUN[rule.kind]} matches; check the id or street name")
        with self.lock:
            cutoff = now.timestamp() - PROPOSAL_TTL.total_seconds()
            self.proposals = {k: v for k, v in self.proposals.items() if v[0] >= cutoff}
            self.proposals[w.id] = (now.timestamp(), w)
        return {"proposal_id": w.id, "label": w.label, "rule": w.public()["rule"],
                "watching": len(matched), "true_now": now_true,
                "values_now": [{"id": s.device.name, "state": s.interp.state,
                                "value": rule.holds(s)[1]} for s in matched[:6]]}

    def confirm(self, proposal_id: str, by: str | None = None) -> dict:
        with self.lock:
            hit = self.proposals.pop(proposal_id, None)
            if hit is None:
                raise WatchError("that proposal has expired or was already saved; ask again")
            if len(self.items) >= MAX_WATCHES:
                raise WatchError(f"there are already {MAX_WATCHES} watches; delete one first")
            w = hit[1]
            w.by = (by or "").strip()[:60] or None
            w.created = _ms(self.now())
            self.items[w.id] = w
        self._save(w)
        return w.public()

    def delete(self, wid: str) -> bool:
        with self.lock:
            w = self.items.pop(wid, None)
        if w is None:
            return False
        self._save(w, deleted=True)
        return True

    def list(self) -> list[dict]:
        self.load()
        with self.lock:
            return [w.public() for w in sorted(self.items.values(), key=lambda w: -w.created)]

    # -- checking --------------------------------------------------------------------------------

    def check_states(self, kind: str, states: list[DeviceState]) -> list[dict]:
        """Fresh readings of one kind: the watches that fire."""
        self.load()
        now = self.now()
        out = []
        changed = []
        with self.lock:
            for w in self.items.values():
                if w.rule.kind != kind:
                    continue
                before = dict(w.armed)
                for s in states:
                    if not w.rule.matches_device(s):
                        continue
                    ok, v = w.rule.holds(s)
                    if ok is None:
                        continue
                    was = w.armed.get(s.device.name)
                    w.armed[s.device.name] = ok
                    if ok and was is False:
                        out.append(self._fire(w, s.device.name, now, self._message(w, s, v), v,
                                              {"id": s.device.name, "kind": s.kind, "name": s.label,
                                               "lat": s.device.lat, "lon": s.device.lon}))
                if w.armed != before:
                    changed.append(w)
        for w in changed:
            self._save(w)
        return out

    def check_incidents(self, changes: list[dict]) -> list[dict]:
        self.load()
        now = self.now()
        out = []
        with self.lock:
            for w in self.items.values():
                if w.rule.kind != "incident":
                    continue
                for c in changes:
                    inc = c["incident"]
                    if c["change"] not in ("new", "escalated", "reopened"):
                        continue
                    if inc["status"] == "snoozed" or not w.rule.matches_incident(inc):
                        continue
                    verb = {"new": "New", "escalated": "Now", "reopened": "Again"}[c["change"]]
                    out.append(self._fire(w, inc["id"], now, f"{verb} {inc['severity']}: {inc['title']}",
                                          None, {"id": inc["id"], "kind": "incident",
                                                 "name": inc["title"], "lat": inc.get("lat"),
                                                 "lon": inc.get("lon")}))
        return out

    def _message(self, w: Watch, s: DeviceState, v: float | None) -> str:
        r = w.rule
        name = f"{NOUN[r.kind].capitalize()} {s.device.name}"
        if r.metric:
            _, unit, words = METRICS[r.kind][r.metric]
            at = s.interp.as_of.astimezone(TZ).strftime("%H:%M") if s.interp.as_of else "now"
            verb = "passed" if r.op == "above" else "dropped below"
            if r.metric == "fill_pct":
                return f"{name} {verb} {r.value:g}%: {v:g}% at {at}"
            return f"{name}: {words} {v:g}{unit} at {at} ({'above' if r.op == 'above' else 'below'} {r.value:g}{unit})"
        word = STATE_WORDS.get(s.interp.state, s.interp.state.replace("_", " "))
        where = f" on {s.street}" if s.street and not r.ids else ""
        return f"{name}{where} is {word} now"

    def _fire(self, w: Watch, device: str, now: datetime, message: str, value: float | None,
              entity: dict) -> dict:
        fire = {"at": _local(now), "t_ms": _ms(now), "device": device, "message": message,
                "value": value}
        w.fires.append(fire)
        del w.fires[:-MAX_FIRES]
        self._write(lambda con: con.execute(
            "INSERT OR IGNORE INTO watch_fires VALUES (?, ?, ?, ?, ?)",
            [w.id, now, device, message, value]))
        return {"type": "watch", "watch": {"id": w.id, "label": w.label}, "message": message,
                "at": fire["at"], "entity": entity}
