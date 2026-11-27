"""Live city state from the Smart Nicosia API, interpreted by collector.interpret.

Every device type is refreshed on demand (at most every `snapshot_ttl_s`) with one
getLatestTelemetry call per device, so a question about bins never touches the lamps. Device
names and streets come from getLatestAttribute and are cached on disk for a day.

Whatever is fetched is also handed to the twin's memory (`recorder`, see chatbot/memory.py),
which writes it to the collector's database in the background.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from collector import interpret, parse
from collector.config import Config
from collector.http import ApiError, Client
from collector.interpret import Interpretation
from collector.jobs import map_cameras, read_camera_csv

from chatbot.settings import CACHE_DIR, ROOT, TIMEZONE, Settings

log = logging.getLogger(__name__)

# Tool-facing names -> API device types.
KINDS: dict[str, str] = {
    "street_light": "Lighting",
    "parking_sensor": "Parking",
    "waste_bin": "Wastebin",
    "air_station": "smartsense",
    "info_screen": "InfoPoints",
    "camera": "Camera",
}
TYPE_TO_KIND = {v: k for k, v in KINDS.items()}
# Parking sensors' attributes carry nothing useful ("Parking Sensor 451"), so skip 303 calls.
ATTRIBUTE_TYPES = {"Lighting", "Wastebin", "smartsense", "InfoPoints", "Camera"}

DEVICES_TTL_S = 6 * 3600
HISTORY_MAX_PAGES = 6  # getTelemetryRange pages per device for one history request
ATTRIBUTES_TTL_S = 24 * 3600

TZ = ZoneInfo(TIMEZONE)


@dataclass(frozen=True)
class Device:
    name: str
    type: str
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class DeviceState:
    device: Device
    label: str
    street: str | None
    interp: Interpretation

    @property
    def kind(self) -> str:
        return TYPE_TO_KIND[self.device.type]

    def values(self) -> dict:
        """Numeric fields a map layer can colour, size or weight by."""
        d = self.interp.detail
        t = self.device.type
        if t == "Lighting":
            return {"power_w": d.get("power_w"), "lit": 1 if self.interp.state == "on" else 0}
        if t == "Parking":
            return {"occupied": {"occupied": 1, "free": 0}.get(self.interp.state)}
        if t == "Wastebin":
            return {"level_pct": d.get("level_pct"), "temperature_c": d.get("temperature_c")}
        if t == "smartsense":
            return {k: d.get(k) for k in
                    ("caqi", "noise_db", "pm2_5", "pm10", "no2", "o3", "temperature_c")}
        return {}

    def summary(self) -> dict:
        i = self.interp
        out = {
            "id": self.device.name,
            "kind": self.kind,
            "label": self.label,
            "street": self.street,
            "lat": self.device.lat,
            "lon": self.device.lon,
            "state": i.state,
            "stale": i.stale,
            "as_of": i.as_of.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if i.as_of else None,
            "age_min": round(i.age_s / 60) if i.age_s is not None else None,
            "why": i.reason,
        }
        d = i.detail
        t = self.device.type
        if t == "Lighting":
            out |= {"level": d.get("level"), "power_w": d.get("power_w")}
        elif t == "Parking":
            out |= {"disabled_bay": d.get("is_pwd"), "status_since": _local(d.get("status_since"))}
        elif t == "Wastebin":
            out |= {"level_pct": d.get("level_pct"), "temperature_c": d.get("temperature_c"),
                    "last_pickup": _local(d.get("last_pickup"))}
        elif t == "smartsense":
            out |= {k: d.get(k) for k in ("caqi", "dominant_pollutant", "pm2_5", "pm10", "no2",
                                          "o3", "noise_db", "temperature_c", "humidity_pct")}
        elif t == "InfoPoints":
            out |= {"plan_state": d.get("plan_state")}
        return {k: v for k, v in out.items() if v is not None}


def _local(iso: str | None) -> str | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _metadata(attrs: dict) -> dict:
    try:
        return json.loads(attrs.get("metadata") or "{}") or {}
    except ValueError:
        return {}


def label_and_street(device: Device, attrs: dict) -> tuple[str, str | None]:
    name = attrs.get("name")
    t = device.type
    if t == "Lighting" and name:
        return f"Street light {name}", re.sub(r"\s*\d+[A-Za-z]?$", "", name).strip() or None
    if t == "smartsense":
        # name is like "AirQ#203 (Κυριάκου Μάτση)": the part in brackets is the site.
        m = re.search(r"\((.+)\)", name or "")
        site = m.group(1) if m else (name or device.name)
        city = attrs.get("city")
        street = attrs.get("street")
        street = None if not street or street.lower() == "unknown" else street
        return f"Air station {site}" + (f", {city}" if city else ""), street
    if t == "Wastebin":
        extra = _metadata(attrs).get("extra") or {}
        kind = extra.get("trashtype")
        label = f"{kind} bin {name}" if kind and name else (name or device.name)
        return label, None
    if t == "Camera":
        return f"Camera {device.name}", attrs.get("location")
    if t == "Parking":
        return f"Parking sensor {device.name}", None
    return name or device.name, None


class CityData:
    """Thread-safe, lazily refreshed view of the city's devices."""

    def __init__(self, settings: Settings, client: Client | None = None, now=None,
                 recorder=None):
        self.settings = settings
        self.recorder = recorder  # chatbot.memory.Recorder, or None
        cfg = replace(Config(), request_delay_s=0.005, timeout_s=20.0, max_retries=1)
        self.client = client or Client(cfg)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._pool = ThreadPoolExecutor(settings.fetch_concurrency, thread_name_prefix="nicosia")
        self._lock = threading.Lock()
        self._type_locks = {t: threading.Lock() for t in KINDS.values()}
        self._devices: list[Device] = []
        self._devices_at = 0.0
        self._latest: dict[str, tuple[float, dict[str, dict]]] = {}
        self._attrs_path = CACHE_DIR / "attributes.json"
        self._attrs: dict[str, dict] = {}
        self._attrs_at: dict[str, float] = {}
        self._cameras: list[dict] = []
        self._cameras_at = 0.0
        self._history: dict[tuple, tuple[float, dict[str, list[dict]]]] = {}
        # Called with the device type after every fresh snapshot of it (the twin's watch).
        self.listeners: list = []
        self._load_attr_cache()

    # -- inventory -------------------------------------------------------------------------

    def now(self) -> datetime:
        return self._now()

    def now_ms(self) -> int:
        return int(self._now().timestamp() * 1000)

    def record(self, what: str, *args, **kwargs) -> None:
        """Hand what was fetched to the twin's memory. Never raises."""
        if self.recorder is None:
            return
        try:
            getattr(self.recorder, what)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - recording never breaks an answer
            log.warning("recording %s failed: %s", what, e)

    def devices(self) -> list[Device]:
        with self._lock:
            if self._devices and time.time() - self._devices_at < DEVICES_TTL_S:
                return self._devices
        payload = self.client.get_json(
            "getTenantDevices", entityType="TENANT", entityName="CYTA"
        )
        self.record("inventory", payload)
        devices = [
            Device(d["name"], d["type"], d["lat"], d["lon"]) for d in parse.parse_devices(payload)
        ]
        with self._lock:
            self._devices, self._devices_at = devices, time.time()
        return devices

    def devices_of(self, device_type: str) -> list[Device]:
        return [d for d in self.devices() if d.type == device_type]

    # -- attributes (names, streets), cached on disk -------------------------------------------

    def _load_attr_cache(self) -> None:
        try:
            data = json.loads(self._attrs_path.read_text(encoding="utf-8"))
            self._attrs = data.get("attrs", {})
            self._attrs_at = data.get("fetched_at", {})
        except (OSError, ValueError):
            pass

    def _save_attr_cache(self) -> None:
        self._attrs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._attrs_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"fetched_at": self._attrs_at, "attrs": self._attrs}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._attrs_path)

    def attributes(self, device_type: str) -> dict[str, dict]:
        devices = self.devices_of(device_type)
        if device_type not in ATTRIBUTE_TYPES:
            return {}
        fresh = time.time() - self._attrs_at.get(device_type, 0) < ATTRIBUTES_TTL_S
        missing = [d.name for d in devices if d.name not in self._attrs]
        if fresh and not missing:
            return {d.name: self._attrs.get(d.name, {}) for d in devices}
        names = [d.name for d in devices] if not fresh else missing

        def one(name: str):
            try:
                return name, self.client.latest_attributes(name)
            except ApiError as e:
                log.warning("attributes for %s failed: %s", name, e)
                return name, None

        fetched = {name: records for name, records in self._pool.map(one, names)
                   if records is not None}
        self.record("attributes", fetched)
        for name, records in fetched.items():
            self._attrs[name] = {r["key"]: r.get("value") for r in records}
        self._attrs_at[device_type] = time.time()
        self._save_attr_cache()
        return {d.name: self._attrs.get(d.name, {}) for d in devices}

    # -- latest readings -------------------------------------------------------------------

    def _latest_for(self, device_type: str) -> tuple[float, dict[str, dict]]:
        with self._type_locks[device_type]:
            cached = self._latest.get(device_type)
            if cached and time.time() - cached[0] < self.settings.snapshot_ttl_s:
                return cached
            keys = interpret.READING_KEYS[device_type]

            def one(device: Device):
                try:
                    return device.name, self.client.latest_telemetry(device.name, keys)
                except ApiError as e:
                    log.warning("latest for %s failed: %s", device.name, e)
                    return device.name, None

            results = dict(self._pool.map(one, self.devices_of(device_type)))
            fetched = {k: v for k, v in results.items() if v is not None}
            if results and not fetched:
                raise ApiError("getLatestTelemetry", {}, f"every {device_type} request failed")
            # A snapshot of the whole type, unless more than a few devices failed to answer.
            self.record("snapshot", device_type, fetched, self.now_ms(),
                        every_device=len(fetched) >= 0.95 * len(results))
            latest = {k: interpret.latest_from_records(v) for k, v in fetched.items()}
            entry = (time.time(), latest)
            self._latest[device_type] = entry
        for listener in self.listeners:
            try:
                listener(device_type)
            except Exception as e:  # noqa: BLE001 - a listener never breaks a snapshot
                log.warning("snapshot listener failed: %s", e)
        return entry

    def states(self, device_type: str) -> list[DeviceState]:
        attrs = self.attributes(device_type)
        _, latest = self._latest_for(device_type)
        now = self.now()
        out = []
        for d in self.devices_of(device_type):
            if d.name not in latest:
                continue
            a = attrs.get(d.name, {})
            label, street = label_and_street(d, a)
            out.append(DeviceState(d, label, street, interpret.interpret(d.type, latest[d.name], now)))
        return out

    def history(self, device_type: str, names: list[str], keys: tuple[str, ...],
                start_ms: int, end_ms: int) -> dict[str, list[dict]]:
        """Raw readings per device between two times (getTelemetryRange, paged), reused for
        `snapshot_ttl_s` so replaying the same time-lapse in another style costs nothing."""
        return self.history_complete(device_type, names, keys, start_ms, end_ms)[0]

    def history_complete(self, device_type: str, names: list[str], keys: tuple[str, ...],
                         start_ms: int, end_ms: int
                         ) -> tuple[dict[str, list[dict]], dict[str, int]]:
        """history(), plus from when each device's readings are complete: start_ms, or later
        when paging stopped at HISTORY_MAX_PAGES before reaching it."""
        cache_key = (device_type, tuple(sorted(names)), keys, start_ms // 60_000, end_ms // 60_000)
        with self._lock:
            cached = self._history.get(cache_key)
            if cached and time.time() - cached[0] < self.settings.snapshot_ttl_s:
                return cached[1]

        def one(name: str):
            try:
                records: list[dict] = []
                pages = last = 0
                for page in self.client.range_pages("DEVICE", name, keys, start_ms, end_ms,
                                                    max_pages=HISTORY_MAX_PAGES):
                    records.extend(page)
                    pages, last = pages + 1, len(page)
                # Paging runs backwards from end_ms: cut short, it holds only the newest part.
                cut = pages >= HISTORY_MAX_PAGES and last >= self.client.cfg.range_cap
                complete_from = min(int(r["timestamp"]) for r in records) if cut else start_ms
                return name, (records, complete_from)
            except ApiError as e:
                log.warning("history for %s failed: %s", name, e)
                return name, None

        results = dict(self._pool.map(one, names))
        found = {k: v[0] for k, v in results.items() if v is not None}
        if names and not found:
            raise ApiError("getTelemetryRange", {}, f"every {device_type} history request failed")
        complete = {k: v[1] for k, v in results.items() if v is not None}
        self.record("readings", device_type, found, [(k, a, end_ms) for k, a in complete.items()])
        with self._lock:
            if len(self._history) >= 8:
                self._history.pop(min(self._history, key=lambda k: self._history[k][0]))
            self._history[cache_key] = (time.time(), (found, complete))
        return found, complete

    def snapshot_age_s(self, device_type: str) -> float | None:
        cached = self._latest.get(device_type)
        return time.time() - cached[0] if cached else None

    # -- what is already known, without a request (search and the inspector's relations) --------

    def cached_devices(self) -> list[Device]:
        """The inventory as last fetched (empty before the first fetch)."""
        return self._devices

    def cached_label(self, device: Device) -> tuple[str, str | None]:
        """A device's label and street from the attribute cache (no request)."""
        return label_and_street(device, self._attrs.get(device.name, {}))

    def cached_states(self, device_type: str, max_age_s: float = 900) -> dict[str, DeviceState]:
        """The type's states from its last snapshot, if it is younger than `max_age_s` (the live
        strip refreshes every type every few minutes while a page is open); else {}."""
        cached = self._latest.get(device_type)
        if not cached or time.time() - cached[0] > max_age_s:
            return {}
        now = self.now()
        out = {}
        for d in self.devices_of_cached(device_type):
            if d.name in cached[1]:
                label, street = self.cached_label(d)
                out[d.name] = DeviceState(d, label, street,
                                          interpret.interpret(d.type, cached[1][d.name], now))
        return out

    def devices_of_cached(self, device_type: str) -> list[Device]:
        return [d for d in self._devices if d.type == device_type]

    def cached_traffic_cameras(self) -> list[dict]:
        """The traffic analytics cameras as last fetched (empty before the first fetch)."""
        return self._cameras

    def device_state(self, name: str) -> tuple[DeviceState, dict]:
        """Fresh readings and every attribute for one device (names are case-sensitive)."""
        device = next((d for d in self.devices() if d.name == name), None)
        if device is None:
            device = next((d for d in self.devices() if d.name.lower() == name.lower()), None)
        if device is None:
            raise KeyError(name)
        records = self.client.latest_telemetry(device.name, None)
        latest = interpret.latest_from_records(records)
        attr_records = self.client.latest_attributes(device.name)
        self.record("snapshot", device.type, {device.name: records}, self.now_ms(),
                    every_device=False)
        self.record("attributes", {device.name: attr_records})
        attrs = {r["key"]: r.get("value") for r in attr_records}
        label, street = label_and_street(device, attrs)
        state = DeviceState(device, label, street, interpret.interpret(device.type, latest, self.now()))
        return state, attrs

    # -- overview --------------------------------------------------------------------------

    def overview(self) -> dict:
        out = {}
        for kind, device_type in KINDS.items():
            try:
                states = self.states(device_type)
            except ApiError as e:
                out[kind] = {"error": str(e)}
                continue
            counts = Counter(s.interp.state for s in states)
            newest = max((s.interp.as_of for s in states if s.interp.as_of), default=None)
            entry = {
                "devices": len(states),
                "by_state": dict(counts.most_common()),
                "stale": sum(s.interp.stale for s in states),
                "newest_reading": newest.astimezone(TZ).strftime("%H:%M") if newest else None,
            }
            if device_type == "Lighting":
                lit = [s.interp.detail.get("power_w") or 0 for s in states if s.interp.state == "on"]
                entry["lit_power_kw"] = round(sum(lit) / 1000, 2)
            if device_type == "smartsense":
                entry["stations"] = [
                    {"id": s.device.name, "label": s.label, "caqi": s.interp.detail.get("caqi"),
                     "band": s.interp.state, "noise_db": s.interp.detail.get("noise_db")}
                    for s in states
                ]
            out[kind] = entry
        return out

    # -- traffic cameras -------------------------------------------------------------------

    def each(self, fn, items) -> list:
        """`fn` over `items` on the shared request pool, results in order (one request each)."""
        return list(self._pool.map(fn, items))

    def traffic_cameras(self) -> list[dict]:
        with self._lock:
            if self._cameras and time.time() - self._cameras_at < DEVICES_TTL_S:
                return self._cameras
        feeds = parse.parse_analytics(self.client.get_json("analytics"))
        cams = {d.name: (d.lat, d.lon) for d in self.devices_of("Camera")}
        rows, _ = map_cameras(feeds, cams, read_camera_csv(ROOT / "data" / "camera_locations.csv"))
        self.record("cameras", rows)
        with self._lock:
            self._cameras, self._cameras_at = rows, time.time()
        return rows
