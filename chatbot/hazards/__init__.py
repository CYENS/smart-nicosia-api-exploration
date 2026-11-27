"""Natural hazards around the city: earthquakes (EMSC, with the Cyprus Geological Survey
Department's solutions, and the felt reports people send EMSC), official weather warnings
(Meteoalarm, from the Department of Meteorology) and fire danger (EFFIS).

- `feeds`: the parsers, the places they are told against and how loud each hazard is.
- `service`: `Hazards`, which reads the feeds on their own cadence and records them into the
  twin's memory, so they replay with the city clock.
- `entities`: an earthquake (eq-...), a warning (wx-...) and a day's fire danger (fire-...) as
  things the inspector opens and `get_entity` reads.

The watch raises them as incidents in chatbot/watch/hazards.py.
"""

from chatbot.hazards import entities as _entities  # noqa: F401 - registers the hazard kinds
from chatbot.hazards.feeds import Hazard
from chatbot.hazards.service import Hazards

__all__ = ["Hazard", "Hazards"]
