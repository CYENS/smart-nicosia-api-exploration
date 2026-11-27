"""The grid's carbon: how much CO2 a kWh drawn from the island's grid emits at a moment.

An estimate, said as one everywhere it is shown. The operator publishes what meets demand each
quarter hour: the oil-fired (conventional) stations, wind, and its estimate of distributed
generation (rooftop solar, solar parks and a few small biomass plants). Wind and solar emit
nothing where they generate, so a kWh's CO2 is the oil-fired share of demand times what a kWh
from those stations emits:

    carbon (kg CO2/kWh) = conventional MW / demand MW x CONVENTIONAL_KG_KWH

CONVENTIONAL_KG_KWH is 0.70: Ember puts the power sector's CO2 just under 3 Mt in 2023, for
about 4.2 TWh of fossil generation, and heavy fuel oil (IPCC 2006: 77.4 t CO2 per TJ) burnt at
about 40% efficiency gives the same. It spans 0.65 (the combined-cycle units carrying the load)
to 0.78 (steam units and engines at part load). Not counted: losses on the wires, the fuel's
life cycle, the biomass plants' own emissions.

When the twin has no record of the grid at a moment, YEARLY_FOSSIL_SHARE (Ember, 2023: 4.23 of
5.29 TWh) stands in for the share, and the answer says so.
"""

from __future__ import annotations

CONVENTIONAL_KG_KWH = 0.70
CONVENTIONAL_RANGE = (0.65, 0.78)
YEARLY_FOSSIL_SHARE = 0.80

METHOD = ("Estimated: the share of demand the oil-fired stations met in that quarter hour, times "
          f"{CONVENTIONAL_KG_KWH:.2f} kg CO2 for each kWh they generate (Ember's 2023 power-sector "
          "CO2 over its fossil generation; heavy fuel oil at about 40% efficiency gives the same). "
          "Wind and solar count as none.")
FALLBACK = (f"the twin has no record of the grid then, so the 2023 yearly fossil share "
            f"({YEARLY_FOSSIL_SHARE:.0%}) stands in")


def share(conventional_mw: float | None, demand_mw: float | None) -> float | None:
    """The oil-fired share of demand, 0 to 1, or None when either is unknown."""
    if conventional_mw is None or not demand_mw or demand_mw <= 0:
        return None
    return max(0.0, min(1.0, conventional_mw / demand_mw))


def intensity(conventional_mw: float | None, demand_mw: float | None,
              factor: float = CONVENTIONAL_KG_KWH) -> float | None:
    """kg CO2 per kWh drawn from the grid, or None when the moment is unknown."""
    s = share(conventional_mw, demand_mw)
    return None if s is None else s * factor


def renewable_share(wind_mw: float | None, distributed_mw: float | None,
                    demand_mw: float | None) -> float | None:
    """Wind and the operator's distributed estimate as a share of demand, 0 to 1."""
    if not demand_mw or demand_mw <= 0 or (wind_mw is None and distributed_mw is None):
        return None
    return max(0.0, min(1.0, ((wind_mw or 0.0) + (distributed_mw or 0.0)) / demand_mw))
