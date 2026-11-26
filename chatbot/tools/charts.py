"""Chart tools: Vega-Lite charts filled from a data_ref and wired to the map and the time line,
and custom visuals drawn by model-written JavaScript in a sandbox."""

from __future__ import annotations

import copy
import json
import logging
import threading
from collections.abc import Callable

import jsonschema
from jsonschema.exceptions import best_match

from chatbot.settings import CACHE_DIR, Settings
from chatbot.store import STATE_COLORS
from chatbot.tools.base import ToolContext, ToolError, ToolSet, obj
from chatbot.tools.buildings import HEIGHT_BANDS, STATUSES
from chatbot.tools.city import KIND_STATES
from chatbot.web import WebError, get_json

log = logging.getLogger(__name__)
TOOLS = ToolSet()
tool = TOOLS.tool

VISUAL_LIBRARIES = ["d3", "plot", "echarts", "three"]
MAX_VISUAL_DATA_CHARS = 3_000_000

VEGA_LITE_SCHEMA_URL = "https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/build/vega-lite-schema.json"
_vl_validator = None
_vl_lock = threading.Lock()


def vega_lite_validator(settings: Settings):
    """The Vega-Lite JSON Schema validator, downloaded once and cached (None if unavailable)."""
    global _vl_validator
    with _vl_lock:
        if _vl_validator is not None:
            return _vl_validator or None
        path = CACHE_DIR / "vega-lite-6.5.0-schema.json"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(get_json(VEGA_LITE_SCHEMA_URL,
                                                    user_agent=settings.user_agent, timeout=30)),
                                encoding="utf-8")
            schema = json.loads(path.read_text(encoding="utf-8"))
            _vl_validator = jsonschema.Draft7Validator(schema)
        except (OSError, ValueError, WebError) as e:
            log.warning("Vega-Lite schema unavailable, charts get light checks only: %s", e)
            _vl_validator = False
        return _vl_validator or None


COMPOSITE = ("mark", "layer", "concat", "hconcat", "vconcat", "facet", "repeat", "spec")
# Keys that belong to a single view when a unit spec is turned into a layer.
UNIT_KEYS = ("mark", "encoding", "params", "transform", "projection")
FRAME_FIELDS = {"frame", "label"}  # non-temporal x fields of get_history rows
NOW_RULE = {"type": "rule", "color": "#e4ebf4", "strokeWidth": 1.5, "strokeDash": [4, 3]}


def spec_error(validator, spec: dict, has_data_ref: bool) -> str | None:
    """The most specific Vega-Lite schema error, or None if the spec is valid."""
    # Validate with a named placeholder instead of the rows, so errors quote the spec only.
    probe = {**spec, "data": {"name": "rows"}} if has_data_ref or "data" not in spec else spec
    err = best_match(validator.iter_errors(probe))
    if err is None:
        return None
    while err.context:
        deeper = best_match(err.context)
        if deeper is None:
            break
        err = deeper
    where = "/".join(map(str, err.absolute_path)) or "(root)"
    return f"invalid Vega-Lite spec at {where}: {err.message[:240]}"


def first_unit(spec: dict) -> dict | None:
    """The view to wire: the spec itself if it is a single view, else its first layer with an
    encoding. Facet and concat specs aren't wired."""
    if "layer" in spec:
        layers = spec["layer"] if isinstance(spec["layer"], list) else []
        return next((u for u in layers if isinstance(u, dict) and "mark" in u
                     and isinstance(u.get("encoding"), dict)), None)
    return spec if "mark" in spec and isinstance(spec.get("encoding"), dict) else None


def param_names(spec: dict, unit: dict) -> set:
    return {p.get("name") for p in spec.get("params", []) + unit.get("params", [])
            if isinstance(p, dict)}


def add_layer(spec: dict, unit: dict, layer: dict) -> None:
    """Append a layer, turning a single-view spec into [the view, layer] first. Selection
    params go with the view; variable params (like `_hl`) must stay at the top of a layer spec."""
    if unit is spec:
        view = {k: spec.pop(k) for k in UNIT_KEYS if k in spec}
        params = view.pop("params", [])
        selections = [p for p in params if isinstance(p, dict) and "select" in p]
        if selections:
            view["params"] = selections
        if rest := [p for p in params if p not in selections]:
            spec["params"] = rest
        spec["layer"] = [view]
    spec["layer"].append(layer)


def is_temporal(x: object) -> bool:
    return isinstance(x, dict) and bool(x.get("field")) and (
        x.get("type") == "temporal" or "timeUnit" in x)


def link_chart(spec: dict, key: str) -> bool:
    """Wire a chart to the map, in place: the page sets the `_hl` param to the value hovered on
    the map (or the time-lapse frame), and the other marks fade, or, for a time-lapse chart
    over frames or frame labels, a rule marks the frame. (Charts over real time follow the
    time-lapse through add_time_rule instead; the page reads hovers from Vega's pointer events.)
    Only single-view and layered specs are wired; returns whether this one was."""
    unit = first_unit(spec)
    if unit is None or "_hl" in param_names(spec, unit):
        return False
    x = unit["encoding"].get("x")
    if key == "frame" and is_temporal(x):
        return False
    if key == "frame" and isinstance(x, dict) and x.get("field") in FRAME_FIELDS:
        add_layer(spec, unit, {
            "mark": NOW_RULE, "transform": [{"filter": "_hl != null && datum.frame === _hl"}],
            "encoding": {"x": {k: x[k] for k in ("field", "type", "sort") if k in x}}})
    elif "opacity" not in unit["encoding"]:
        k = json.dumps(key)
        unit["encoding"]["opacity"] = {
            "condition": {"test": f"_hl == null || datum[{k}] == null || datum[{k}] === _hl",
                          "value": 1},
            "value": 0.2}
    spec.setdefault("params", []).append({"name": "_hl", "value": None})
    return True


HOUR_UNITS = {"hours", "hoursminutes", "yearmonthdatehours", "yearmonthdatehoursminutes"}


def keep_real_hours(spec: dict, rows: list[dict]) -> None:
    """Undo a time unit that only hides the date of a chart over real hours, in place. An axis
    of `timeUnit: "hours"` over one day's rows looks the same as the hours themselves, but its
    moments belong to no day, so the chart can't follow a time-lapse or lead it. Lines, areas
    and points get the plain field; bars keep a unit, for their width, but one with the date.
    Left alone where the unit does group something (several days, or minutes into hours)."""
    for unit in [spec, *[u for u in spec.get("layer", []) if isinstance(u, dict)]]:
        x = (unit.get("encoding") or {}).get("x")
        if not isinstance(x, dict) or x.get("timeUnit") not in HOUR_UNITS:
            continue
        moments = [r.get(x.get("field")) for r in rows]
        if not moments or not all(isinstance(m, str) and len(m) >= 16 for m in moments):
            continue
        one_day = len({m[:10] for m in moments}) == 1
        if (not one_day and not x["timeUnit"].startswith("year")) or (
                not x["timeUnit"].endswith("minutes") and any(m[14:16] != "00" for m in moments)):
            continue
        mark = unit.get("mark")
        if (mark.get("type") if isinstance(mark, dict) else mark) in ("bar", "rect"):
            x["timeUnit"] = "yearmonthdate" + x["timeUnit"].removeprefix("yearmonthdate")
        else:
            del x["timeUnit"]
        x["type"] = "temporal"
        if one_day and isinstance(x.get("axis", {}), dict):  # the hours alone, as was asked for
            x["axis"] = {"format": "%H:%M", **x.get("axis", {})}


SCRUB_MARKS = {"line", "area", "point", "circle", "square", "bar", "trail"}
SERIES_CHANNELS = ("color", "detail", "strokeDash", "shape", "size", "row", "column", "facet")
FLAG_FIELD = "_flag"


def scrub_layers(spec: dict, unit: dict, rows: list[dict] | None) -> list[dict]:
    """A dot on the series and a flag above it reading the time and the value, where a row sits
    at the `_t` moment. With the rule they make a scrubber: it rides along the line as a
    time-lapse plays and jumps to the hour the pointer is over. Only for one plain series over
    real moments (a row per moment, no transforms, nothing split by colour); other charts keep
    the rule alone and get []. The flag stands above the plot, clear of the data, and turns to
    the left near the right edge."""
    enc = unit["encoding"]
    x, y = enc["x"], enc.get("y")
    mark = unit.get("mark")
    if (not rows or "timeUnit" in x or "transform" in spec or "transform" in unit
            or (unit is not spec and "encoding" in spec)
            or (mark.get("type") if isinstance(mark, dict) else mark) not in SCRUB_MARKS
            or not isinstance(y, dict) or not y.get("field") or y.get("type") != "quantitative"
            or any(k in y for k in ("aggregate", "bin", "timeUnit"))
            or any(isinstance(enc.get(c), dict) and "field" in enc[c] for c in SERIES_CHANNELS)):
        return []
    others = [u["encoding"].get("y") for u in spec.get("layer", [])
              if isinstance(u, dict) and u is not unit and isinstance(u.get("encoding"), dict)]
    if any(not isinstance(o, dict) or o.get("field") != y["field"] for o in others):
        return []
    moments = [r.get(x["field"]) for r in rows]
    if (not all(isinstance(m, str) and len(m) >= 10 for m in moments)
            or len(set(moments)) != len(moments)
            or not all(isinstance(r.get(y["field"]), (int, float)) for r in rows)):
        return []
    moments.sort()
    timed = len(moments[0]) > 10
    one_day = len({m[:10] for m in moments}) == 1
    time_format = "%d %b" if not timed else "%H:%M" if one_day else "%a %H:%M"
    f, yf = json.dumps(x["field"]), json.dumps(y["field"])
    here = [{"filter": f"_t != null && +toDate(datum[{f}]) === _t"}]
    at_x = {k: x[k] for k in ("field", "type", "title") if k in x}
    near_end = f"_t > +toDate({json.dumps(moments[int(len(moments) * 0.88)])})"
    dot = {
        "transform": here,
        "mark": {"type": "point", "filled": True, "size": 110, "opacity": 1, "color": "#ffae45",
                 "stroke": "#15233a", "strokeWidth": 2},
        "encoding": {"x": at_x,
                     "y": {k: y[k] for k in ("field", "type", "title", "scale", "axis") if k in y}}}
    flag = {
        "transform": [*here, {"calculate": f"timeFormat(toDate(datum[{f}]), "
                                           f"{json.dumps(time_format)}) + ' · ' + "
                                           f"format(datum[{yf}], ',')", "as": FLAG_FIELD}],
        "mark": {"type": "text", "baseline": "bottom", "fontSize": 13, "fontWeight": 600,
                 "color": "#e4ebf4", "align": {"expr": f"{near_end} ? 'right' : 'center'"},
                 "dx": {"expr": f"{near_end} ? 6 : 0"}},
        "encoding": {"x": at_x, "y": {"value": -5},
                     "text": {"field": FLAG_FIELD, "type": "nominal"}}}
    return [dot, flag]


def has_scrubber(spec: dict) -> bool:
    """Whether add_time_rule gave the chart a flag (the page leaves room for it above the plot)."""
    return any(isinstance(u, dict) and isinstance(u.get("encoding"), dict)
               and isinstance(u["encoding"].get("text"), dict)
               and u["encoding"]["text"].get("field") == FLAG_FIELD
               for u in spec.get("layer", []))


def add_time_rule(spec: dict, rows: list[dict] | None = None) -> str | None:
    """Give a chart over time a vertical rule at the `_t` param (epoch ms), in place. The page
    sets `_t` to the time-lapse's moment while one plays, to the moment under the pointer
    while the chart is hovered, and to the current time otherwise (null when that is outside
    the chart). The rule reuses the x field's name, type, timeUnit and title, so the axis and
    its title stay as they were. With the chart's `rows`, one plain series also gets a dot and
    a value flag at that moment (see scrub_layers). Returns the x field."""
    unit = first_unit(spec)
    if unit is None or "_t" in param_names(spec, unit):
        return None
    x = unit["encoding"].get("x")
    if not is_temporal(x):
        return None
    field = x["field"]
    scrubber = scrub_layers(spec, unit, rows)  # read before add_layer moves the view
    add_layer(spec, unit, {
        "data": {"values": [{}]},
        "transform": [{"filter": "_t != null"}, {"calculate": "_t", "as": field}],
        "mark": NOW_RULE,
        "encoding": {"x": {k: x[k] for k in ("field", "type", "timeUnit", "title") if k in x}}})
    spec["layer"].extend(scrubber)
    spec.setdefault("params", []).append({"name": "_t", "value": None})
    return field


BAND_COLORS = {label: color for _, label, color in HEIGHT_BANDS}
NAMED_COLORS = {**STATE_COLORS, **BAND_COLORS}
COLOR_ORDERS = [*KIND_STATES.values(), STATUSES, list(BAND_COLORS)]
COLOR_FIELDS = ("state", "status", "register", "height_band")


def color_states(spec: dict, rows: list[dict]) -> None:
    """Colour a state-like field (state, status, register, height_band) with the map's colours
    (in place), so a full bin or a dangerous building is the same red in the chart as on the
    map. Leaves any scale the model chose alone."""
    for unit in [spec, *[u for u in spec.get("layer", []) if isinstance(u, dict)]]:
        color = (unit.get("encoding") or {}).get("color")
        if not (isinstance(color, dict) and color.get("field") in COLOR_FIELDS
                and "scale" not in color):
            continue
        field = color["field"]
        present = [s for s in dict.fromkeys(r.get(field) for r in rows) if s in NAMED_COLORS]
        if not present:
            continue
        order = next((v for v in COLOR_ORDERS if set(present) <= set(v)), present)
        values = [s for s in order if s in present]
        color["scale"] = {"domain": values, "range": [NAMED_COLORS[s] for s in values]}



@tool("show_chart",
      "Render a chart on the stage (a wide, dark panel along the bottom of the map) from a "
      "Vega-Lite v6 spec. Pass data_ref from a data tool "
      "and leave `data` out of the spec: the server fills it from the dataset, so every "
      "number comes from the tools. Use any Vega-Lite feature (layer, facet, concat, "
      "transform, timeUnit, selections). Keep axis titles readable and put units in titles. "
      "The stage themes the chart (dark, amber for a single series): set colours only "
      "where they carry meaning. "
      "Single-view and layered charts are linked to the map automatically: rows that carry a "
      "device id or camera highlight on the map when hovered (and the other way round). Any "
      "chart whose x is temporal (type temporal, e.g. a `time` or `hour` field) gets a "
      "line at the time-lapse's moment while one plays, and at the current time otherwise; "
      "hovering it moves the line and the time-lapse to that moment. One plain series "
      "over time (a row per moment, y a plain quantitative field, no transforms or colour "
      "split) also gets a dot on the line and a flag with the time and value there. Keep "
      "the id or camera field unaggregated in the linked view for this to work.",
      obj({
          "title": {"type": "string"},
          "spec": {"type": "object", "description": "Vega-Lite spec without data."},
          "data_ref": {"type": "string"},
      }, ["title", "spec"]))
def h_show_chart(ctx: ToolContext, a: dict) -> dict:
    spec = copy.deepcopy(a["spec"])  # never touch the tool input: it stays in the history
    if not any(k in spec for k in COMPOSITE):
        raise ToolError(f"spec needs one of {COMPOSITE}")
    spec.setdefault("$schema", "https://vega.github.io/schema/vega-lite/v6.json")
    if "mark" in spec or "layer" in spec:  # facet/concat/repeat specs don't take a width
        spec.setdefault("width", "container")
    validator = vega_lite_validator(ctx.settings)
    has_ref = bool(a.get("data_ref"))
    if validator is not None and (err := spec_error(validator, spec, has_ref)):
        raise ToolError(err)
    def wired(spec: dict, wire: Callable[[dict], object]) -> tuple[dict, object]:
        """Apply `wire` to a copy; keep it only if it did something and the spec stays valid."""
        trial = copy.deepcopy(spec)
        result = wire(trial)
        if result and (validator is None or not spec_error(validator, trial, True)):
            return trial, result
        return spec, None

    rows = link = time_field = None
    if has_ref:
        ds = ctx.store.datasets.get(a["data_ref"])
        if ds is None:
            raise ToolError(f"unknown data_ref {a['data_ref']!r}; known: {list(ctx.store.datasets)[-10:]}")
        rows = ds["rows"]
        color_states(spec, rows)
        keep_real_hours(spec, rows)
        if ds.get("link") and rows and ds["link"]["key"] in rows[0]:
            spec, ok = wired(spec, lambda s: link_chart(s, ds["link"]["key"]))
            link = ds["link"] if ok else None
        spec, time_field = wired(spec, lambda s: add_time_rule(s, rows))
        if not time_field:  # the dot and flag didn't pass: the rule alone still may
            spec, time_field = wired(spec, add_time_rule)
        spec["data"] = {"values": rows}
    elif "data" not in spec and "datasets" not in spec:
        raise ToolError("pass a data_ref (preferred) or include data in the spec")
    ctx.emit({"type": "ui", "action": "chart", "title": a["title"], "spec": spec, "link": link,
              "time_field": time_field, "scrub": has_scrubber(spec)})
    return {"ok": True, "rows_plotted": len(rows) if rows is not None else None,
            "linked_to_map": bool(link or time_field), "_summary": f"chart: {a['title']}"}


@tool("show_visual",
      "Draw a custom visual on the stage by writing JavaScript, for what Vega-Lite can't "
      "express well: radial clocks, sankeys, chord or network graphs, calendars, ridgelines, "
      "3D scenes, animations, bespoke SVG. Prefer show_chart for ordinary charts. The code "
      "runs in a locked sandbox (no network, no access to the page) as the body of an async "
      "function, with these globals: root (an empty div to draw in), WIDTH and HEIGHT (px), "
      "DATA[data_ref] (the rows of each data_refs entry; DATA_LIST holds them in order), "
      "LAYERS[layer_ref] (GeoJSON of each layer_refs entry; a get_history layer also has "
      "`frames`), COLORS (state colours, e.g. COLORS.on, COLORS.full), THEME {background, "
      "text, muted, grid, accent, categorical[], ramp[]}, highlight(value) (highlights the device or "
      "camera with that id on the map and in linked charts, or jumps the time-lapse to that "
      "frame number; null clears) and onHighlight(fn) (fn(value) runs when the user hovers "
      "something on the map or in a chart). Libraries load as globals: d3 (v7), Plot "
      "(Observable Plot 0.6), echarts (5.6), THREE (r159). The stage is dark (THEME.background, "
      "deep blue): draw text in THEME.text and the main series in THEME.accent (amber). WIDTH "
      "and HEIGHT are the space it gives the visual, about 900 by 320 px. Read every "
      "number from DATA or LAYERS, never type data into the code. Runtime errors come back in "
      "the next turn's <context> as visual_errors.",
      obj({
          "title": {"type": "string"},
          "code": {"type": "string", "minLength": 10, "maxLength": 30000},
          "data_refs": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
          "layer_refs": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
          "libraries": {"type": "array", "uniqueItems": True,
                        "items": {"type": "string", "enum": VISUAL_LIBRARIES}},
          "height": {"type": "integer", "minimum": 120, "maximum": 900,
                     "description": "Height in px if the stage can't size it (default 380); "
                                    "on the stage, HEIGHT is the space it actually has."},
      }, ["title", "code"]))
def h_show_visual(ctx: ToolContext, a: dict) -> dict:
    data, layers = {}, {}
    for ref in a.get("data_refs") or []:
        if ref not in ctx.store.datasets:
            raise ToolError(f"unknown data_ref {ref!r}; known: {list(ctx.store.datasets)[-10:]}")
        data[ref] = ctx.store.datasets[ref]["rows"]
    for ref in a.get("layer_refs") or []:
        if ref not in ctx.store.layers:
            raise ToolError(f"unknown layer_ref {ref!r}; known: {list(ctx.store.layers)[-10:]}")
        layers[ref] = {k: v for k, v in ctx.store.layers[ref].items() if k != "meta"}
    size = len(json.dumps(data, default=str)) + len(json.dumps(layers, default=str))
    if size > MAX_VISUAL_DATA_CHARS:
        raise ToolError(f"the data is too big for a visual ({size:,} characters); pass fewer refs")
    libraries = a.get("libraries") or []
    ctx.emit({"type": "ui", "action": "visual", "title": a["title"], "code": a["code"],
              "libraries": libraries, "height": a.get("height") or 380, "data": data,
              "data_order": list(data), "layers": layers, "colors": STATE_COLORS})
    return {"ok": True, "libraries": libraries, "rows": {k: len(v) for k, v in data.items()},
            "note": "rendered in the sandbox; any runtime error shows in the next <context>",
            "_summary": f"visual: {a['title']}"}
