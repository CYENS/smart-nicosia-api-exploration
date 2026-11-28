"""Tonight in Nicosia: pharmacies on duty, city events calendar, and open places."""

from __future__ import annotations

from chatbot.tonight import entities as _entities  # noqa: F401 - registers kinds, clock & search
from chatbot.tonight.service import Tonight

__all__ = ["Tonight"]
