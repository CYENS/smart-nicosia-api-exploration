"""Watch: the twin's alerts and incidents (feature 6 of docs/digital-twin-roadmap.md).

- `detectors`: what the twin notices on its own in its memory, and the city's own alarms.
- `incidents`: those grouped by place and time, with severity, evidence and a history of what
  people did, stored in the twin's memory.
- `watches`: rules people asked the twin to keep an eye on, raised the moment they come true.
- `service`: the `Watcher` that runs them when fresh data arrives, and the live stream to pages.
"""

from chatbot.watch import entities as _entities  # noqa: F401 - registers the incident kind
from chatbot.watch.service import Hub, Watcher

__all__ = ["Hub", "Watcher"]
