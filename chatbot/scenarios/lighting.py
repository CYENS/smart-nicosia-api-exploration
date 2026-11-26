"""Street lighting policy: what switching, dimming, part-night switch-off and an LED retrofit
would save on the 240 lamps the twin measures, against what they draw today.

(The model lands in the next commits; this module already registers the scenario with its
parameters, so the lab's contract can be built against.)
"""

from __future__ import annotations

from chatbot.scenarios.base import Period, Result, Scenario, ScenarioContext, ScenarioError
from chatbot.scenarios.registry import register


def _times() -> list[str]:
    """Half hours through the night, 18:00 to 07:30."""
    out = []
    for k in range(28):
        h = (18 + k // 2) % 24
        out.append(f"{h:02d}:{30 * (k % 2):02d}")
    return out


TIMES = _times()

PARAMS = {
    "type": "object",
    "properties": {
        "switch_by_sun": {
            "type": "boolean", "default": False, "title": "Switch by sunset and sunrise",
            "description": "Switch on at civil dusk plus an offset and off at civil dawn minus an "
                           "offset, in place of today's switching.", "x-group": "switching"},
        "dusk_offset_min": {
            "type": "integer", "default": 0, "minimum": -30, "maximum": 60, "x-step": 5, "x-unit": "min",
            "title": "Switch on after civil dusk",
            "description": "Minutes after civil dusk (the sun 6° below the horizon, about 25 minutes "
                           "after sunset) the lamps come on. Negative is before.",
            "x-group": "switching", "x-when": "switch_by_sun"},
        "dawn_offset_min": {
            "type": "integer", "default": 0, "minimum": -30, "maximum": 60, "x-step": 5, "x-unit": "min",
            "title": "Switch off before civil dawn",
            "description": "Minutes before civil dawn (about 25 minutes before sunrise) the lamps go "
                           "off. Negative is after.",
            "x-group": "switching", "x-when": "switch_by_sun"},
        "dim": {
            "type": "boolean", "default": False, "title": "Dim late at night",
            "description": "Dim every lamp to one level in one window of the night.", "x-group": "dimming"},
        "dim_level": {
            "type": "integer", "default": 50, "minimum": 10, "maximum": 90, "x-step": 5, "x-unit": "%",
            "title": "Dim to", "description": "The light level while dimmed, as a share of full light.",
            "x-group": "dimming", "x-when": "dim"},
        "dim_from": {"type": "string", "enum": TIMES, "default": "01:00", "title": "Dim from",
                     "description": "When the dimming starts.", "x-group": "dimming", "x-when": "dim"},
        "dim_to": {"type": "string", "enum": TIMES, "default": "05:00", "title": "Dim until",
                   "description": "When full light comes back (or dawn, if earlier).",
                   "x-group": "dimming", "x-when": "dim"},
        "part_night": {
            "type": "boolean", "default": False, "title": "Switch off quiet streets late at night",
            "description": "Switch off the lamps on streets where the cameras count almost no one "
                           "in that window.", "x-group": "part_night"},
        "off_from": {"type": "string", "enum": TIMES, "default": "01:00", "title": "Off from",
                     "description": "When the quiet streets go dark.", "x-group": "part_night",
                     "x-when": "part_night"},
        "off_to": {"type": "string", "enum": TIMES, "default": "05:00", "title": "Off until",
                   "description": "When they light again (or dawn, if earlier).", "x-group": "part_night",
                   "x-when": "part_night"},
        "quiet_max_people": {
            "type": "number", "default": 5, "minimum": 0, "maximum": 50, "x-step": 1,
            "x-unit": "people an hour", "title": "A street counts as quiet below",
            "description": "The average people an hour its camera counts in that window, over the "
                           "period.", "x-group": "part_night", "x-when": "part_night"},
        "include_unmeasured": {
            "type": "boolean", "default": False, "title": "Also switch off streets no camera counts",
            "description": "Off by default: without a camera nobody knows who walks there at night.",
            "x-group": "part_night", "x-when": "part_night"},
        "retrofit": {
            "type": "boolean", "default": False, "title": "Replace non-LED lamps with LED",
            "description": "Replace the lamps whose power at full light shows they are not LED.",
            "x-group": "retrofit"},
        "retrofit_min_w": {
            "type": "integer", "default": 90, "minimum": 40, "maximum": 200, "x-step": 5, "x-unit": "W",
            "title": "Count a lamp as non-LED from",
            "description": "A lamp drawing at least this at full light counts as a sodium or similar "
                           "lamp. 90 W is above every LED class the city runs today.",
            "x-group": "retrofit", "x-when": "retrofit"},
        "safety_radius_m": {
            "type": "integer", "default": 50, "minimum": 20, "maximum": 150, "x-step": 10, "x-unit": "m",
            "title": "Safety check distance",
            "description": "A dimmed or dark lamp this close to a bus stop or a dangerous building is "
                           "flagged.", "x-group": "safety"},
        "price_eur_kwh": {
            "type": "number", "default": 0.20, "minimum": 0.20, "maximum": 0.32, "x-step": 0.01,
            "x-unit": "EUR/kWh", "x-assumption": True, "title": "Electricity price",
            "x-source": "The lamps' own records: 101 lamps carry 0.20 EUR/kWh, 21 carry 0.32, the "
                        "other 118 none.",
            "description": "What a kWh costs the city."},
        "co2_kg_kwh": {
            "type": "number", "default": 0.55, "minimum": 0.47, "maximum": 0.63, "x-step": 0.01,
            "x-unit": "kg CO2/kWh", "x-assumption": True, "title": "Grid emission factor",
            "x-source": "EAC's 2023 energy mix: 0.627 kg per kWh sold. Ember's 2025 data: about "
                        "0.49, falling as solar grows.",
            "description": "CO2 emitted for each kWh the lamps draw from the Cyprus grid."},
        "dim_power_floor_pct": {
            "type": "integer", "default": 10, "minimum": 0, "maximum": 25, "x-step": 1, "x-unit": "%",
            "x-assumption": True, "title": "Power kept when dimmed",
            "x-source": "Typical LED driver curves. These lamps never dimmed in the data, so it "
                        "can't be measured here yet.",
            "description": "The share of full power a dimmed lamp still draws whatever its level; the "
                           "rest falls in step with the light."},
        "led_power_pct": {
            "type": "integer", "default": 55, "minimum": 43, "maximum": 74, "x-step": 1, "x-unit": "%",
            "x-assumption": True, "title": "LED power for the same light",
            "x-source": "US DOE GATEWAY demonstrations (PNNL, New York City, 2011): LED luminaires "
                        "used 26% to 57% less than the sodium lamps they replaced.",
            "description": "An LED lamp's power as a share of the lamp it replaces."},
        "led_cost_eur": {
            "type": "integer", "default": 400, "minimum": 250, "maximum": 600, "x-step": 10,
            "x-unit": "EUR per lamp", "x-assumption": True, "title": "LED lamp, installed",
            "x-source": "A planning figure for an LED street luminaire and its fitting; replace it "
                        "with the city's own tender prices.",
            "description": "What replacing one lamp costs."},
    },
}


class Lighting(Scenario):
    id = "lighting"
    title = "Street lighting policy"
    summary = ("What switching by the sun, dimming, switching off quiet streets or an LED retrofit "
               "would save on the lamps the twin measures, against what they draw today.")
    question = "How much energy, money and CO2 would a different street lighting policy save, and where would it leave streets darker?"
    params = PARAMS
    main_kpi = "cost"
    sources = ("smart_nicosia", "twin_memory", "bus_timetable", "registers")

    def prepare(self, ctx: ScenarioContext, period: Period):
        raise ScenarioError("the lighting model isn't built yet")

    def evaluate(self, data, params: dict) -> Result:
        raise ScenarioError("the lighting model isn't built yet")


LIGHTING = register(Lighting())
