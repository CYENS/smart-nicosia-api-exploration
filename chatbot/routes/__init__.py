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
    from chatbot.routes import (buildings, chat, clock, collectors, devices, entities, environment,
                                fabric, here, live, memory, mobility, reports, scenarios, sources,
                                traffic_lines, views, watch)
    from chatbot.routes import streets
    from chatbot.routes import facilities
    from chatbot.routes import energy
    from chatbot.routes import hazards
    from chatbot.routes import tonight
    from chatbot.routes import sky

    return [live.router, memory.router, chat.router, buildings.router, entities.router,
            clock.router, mobility.router, sources.router, devices.router, watch.router,
            environment.router, here.router, views.router, scenarios.router,
            streets.router, reports.router, facilities.router, energy.router, hazards.router,
            tonight.router, sky.router, fabric.router, collectors.router, traffic_lines.router]
