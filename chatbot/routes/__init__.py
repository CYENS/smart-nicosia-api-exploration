"""The server's routes, one router per area. server.py includes every router in ROUTERS; a later
feature adds its own module here and puts its router at the end of the list."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from chatbot.agent import Agent


def agent_of(request: Request) -> Agent:
    """The app's one Agent, which holds the data sources and the conversations."""
    return request.app.state.agent


def routers() -> list:
    from chatbot.routes import (buildings, chat, clock, devices, entities, live, memory, mobility,
                                sources, watch)

    return [live.router, memory.router, chat.router, buildings.router, entities.router,
            clock.router, mobility.router, sources.router, devices.router, watch.router]
