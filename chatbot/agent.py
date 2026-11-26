"""The agent loop: stream a turn from Claude, run the tools it asks for, repeat until it answers.

History is append-only (Sonnet 5.5 binds thinking blocks to the exact conversation), so the
assistant content goes back unchanged and per-turn context rides in the user message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from chatbot.city import CityData
from chatbot.dls import DLS
from chatbot.entities import Entities
from chatbot.eoa import Registry
from chatbot.geo import Places
from chatbot.memory import Memory
from chatbot.mobility import Mobility
from chatbot.moments import Moments
from chatbot.prompt import SYSTEM_PROMPT
from chatbot.routing import Router
from chatbot.settings import TIMEZONE, Settings
from chatbot.store import SessionStore
from chatbot.tools import TOOL_DEFS, WEB_SEARCH, ToolContext, run_tool
from chatbot.watch import Watcher

log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)
BETAS = ["server-side-fallback-2026-07-01"]
Emit = Callable[[dict], None]


class Conversation:
    def __init__(self, cid: str):
        self.id = cid
        self.messages: list[dict] = []
        self.store = SessionStore()
        self.lock = asyncio.Lock()
        self.last_used = time.time()


def user_location(context: dict | None) -> dict | None:
    """The position the page shared (device GPS or set on the map), checked, or None."""
    loc = (context or {}).get("user_location")
    if not isinstance(loc, dict):
        return None
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    acc = loc.get("accuracy_m")
    return {"lat": lat, "lon": lon, "source": str(loc.get("source") or "shared")[:30],
            "accuracy_m": float(acc) if isinstance(acc, (int, float)) else None}


def _entity_ref(value: object) -> str | None:
    """A thing the page names ({"id", "kind", "name"}) as one short, plain line, or None."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return None

    def clean(v: object, n: int) -> str:
        return re.sub(r"[\x00-\x1f<>]+", " ", str(v or "")).strip()[:n]

    eid, kind, name = clean(value["id"], 64), clean(value.get("kind"), 32), clean(value.get("name"), 80)
    if not eid:
        return None
    what = ", ".join(x for x in (kind.replace("_", " "), f'"{name}"' if name else "") if x)
    return f"{eid} ({what})" if what else eid


_WALL = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")


def city_clock(value: object) -> str | None:
    """What the city clock shows, from the page ({"at", "range", "compare", "playing"}, local
    wall times), as one line: "live", or "viewing Sat 2026-11-21 21:00 ...". None when the page
    says nothing about it."""
    if not isinstance(value, dict):
        return None

    def wall(v: object) -> str | None:
        if not isinstance(v, str) or not _WALL.match(v):
            return None
        try:
            return f"{datetime.fromisoformat(v):%a %Y-%m-%d %H:%M}"
        except ValueError:
            return None

    at = wall(value.get("at"))
    parts = [f"viewing {at} (the map, the strip and the inspector show this moment)" if at else "live"]
    rng = value.get("range")
    if isinstance(rng, list) and len(rng) == 2 and (a := wall(rng[0])) and (b := wall(rng[1])):
        parts.append(f"{'playing' if value.get('playing') else 'paused on'} the range {a} to {b}")
    if cmp := wall(value.get("compare")):
        parts.append(f"comparing with {cmp} (a swipe across the map)")
    return "; ".join(parts)


def format_context(context: dict | None, now: datetime) -> str:
    lines = [f"local_time: {now.astimezone(TZ):%a %Y-%m-%d %H:%M} ({TIMEZONE})"]
    view = (context or {}).get("map_view") or {}
    if view.get("center"):
        lon, lat = view["center"]
        lines.append(f"map_view: center {lat:.5f}, {lon:.5f}; zoom {view.get('zoom', 0):.1f}")
    layers = (context or {}).get("layers_on_map")
    if layers:
        lines.append(f"layers_on_map: {', '.join(layers)}")
    if (context or {}).get("city_3d"):
        lines.append("city_3d: on")
    loc = user_location(context)
    if loc:
        how = f", ±{loc['accuracy_m']:.0f} m" if loc.get("accuracy_m") else ""
        lines.append(f"user_location: {loc['lat']:.5f}, {loc['lon']:.5f} "
                     f"({loc.get('source') or 'shared'}{how})")
    else:
        lines.append("user_location: not shared")
    if inspecting := _entity_ref((context or {}).get("inspecting")):
        lines.append(f"inspector_open: {inspecting}")
    if subject := _entity_ref((context or {}).get("subject")):
        lines.append(f"asking_about: {subject}")
    if clock := city_clock((context or {}).get("clock")):
        lines.append(f"city_clock: {clock}")
    errors = (context or {}).get("visual_errors")
    if errors:
        lines.append("visual_errors: " + "; ".join(str(e)[:300] for e in errors[:3]))
    return "<context>\n" + "\n".join(lines) + "\n</context>\n\n"


class Agent:
    def __init__(self, settings: Settings, city: CityData, places: Places, router: Router,
                 client: anthropic.AsyncAnthropic | None = None, dls: DLS | None = None,
                 registers: Registry | None = None, memory: Memory | None = None,
                 mobility: Mobility | None = None):
        self.settings = settings
        self.memory = memory
        self.city = city
        self.places = places
        self.router = router
        self.dls = dls
        self.registers = registers
        self._client = client
        # Buses, stops and road speeds: one fetch of each feed for every page and answer.
        self.mobility = mobility or Mobility(settings, memory, router)
        self.entities = Entities(city, places, dls, registers, memory,  # shared with the inspector
                                 mobility=self.mobility)
        self.moments = Moments(city, memory)  # the city at past moments, for the clock
        self.watcher = Watcher(city, memory)  # alerts, incidents and watches
        self.entities.watcher = self.watcher  # incidents open by their id (INC-12)
        self.tools = TOOL_DEFS + ([WEB_SEARCH] if settings.web_search else [])
        self.conversations: dict[str, Conversation] = {}

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(max_retries=3)
        return self._client

    def conversation(self, cid: str | None) -> Conversation:
        if cid and cid in self.conversations:
            conv = self.conversations[cid]
        else:
            conv = Conversation(cid or uuid.uuid4().hex[:12])
            self.conversations[conv.id] = conv
        conv.last_used = time.time()
        return conv

    async def run_turn(self, conv: Conversation, text: str, context: dict | None,
                       emit: Emit) -> None:
        loop = asyncio.get_running_loop()

        def emit_threadsafe(event: dict) -> None:
            loop.call_soon_threadsafe(emit, event)

        tool_ctx = ToolContext(self.settings, self.city, self.places, self.router, conv.store,
                               emit_threadsafe, conv.id, self.dls, self.registers,
                               user_location(context), memory=self.memory,
                               entities=self.entities, moments=self.moments,
                               mobility=self.mobility, watcher=self.watcher)
        conv.messages.append({"role": "user", "content": [
            {"type": "text", "text": format_context(context, self.city.now()) + text}]})

        json_retries = 0
        for _ in range(self.settings.max_steps):
            try:
                final = await self._stream(conv, emit)
            except ValueError:
                # Tool input JSON the SDK couldn't parse at all (eager streaming): no tool_use
                # block to answer, so re-issue the same request, a bounded number of times.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            json_retries = 0
            conv.messages.append({"role": "assistant", "content": final.content})
            u = final.usage
            emit({"type": "usage", "input": u.input_tokens, "output": u.output_tokens,
                  "cache_read": u.cache_read_input_tokens or 0,
                  "cache_write": u.cache_creation_input_tokens or 0, "model": final.model})

            if final.stop_reason == "refusal":
                emit({"type": "error", "message": "Claude declined this request."})
                return
            if final.stop_reason == "pause_turn":  # a long web search; resume it
                continue
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if not tool_uses:
                if final.stop_reason == "max_tokens":
                    emit({"type": "error", "message": "The answer was cut off (max_tokens)."})
                return
            if final.stop_reason == "max_tokens":
                # A truncated tool input parses as a valid partial object; never run it.
                conv.messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                     "content": "not run: the tool input was cut off by max_tokens"}
                    for b in tool_uses]})
                emit({"type": "error", "message": "The answer was cut off (max_tokens)."})
                return
            results = await asyncio.gather(*(self._run_tool(tool_ctx, b, emit) for b in tool_uses))
            conv.messages.append({"role": "user", "content": list(results)})
        emit({"type": "error", "message": f"Stopped after {self.settings.max_steps} steps."})

    async def _stream(self, conv: Conversation, emit: Emit):
        async with self.client.beta.messages.stream(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=self.tools,
            messages=conv.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.settings.effort},
            cache_control={"type": "ephemeral"},
            betas=BETAS,
            fallbacks="default",
        ) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    b = event.content_block
                    if b.type in ("tool_use", "server_tool_use"):
                        emit({"type": "tool_start", "id": b.id, "name": b.name})
                    elif b.type == "web_search_tool_result":
                        emit({"type": "tool_end", "id": b.tool_use_id, "name": "web_search",
                              "ok": isinstance(b.content, list),
                              "summary": f"{len(b.content)} results"
                              if isinstance(b.content, list) else "search failed"})
                    elif b.type == "thinking":
                        emit({"type": "thinking_start"})
                    elif b.type == "fallback":
                        emit({"type": "notice", "message": "Switched to a fallback model."})
                elif event.type == "content_block_delta":
                    d = event.delta
                    if d.type == "text_delta":
                        emit({"type": "text", "text": d.text})
                    elif d.type == "thinking_delta" and d.thinking:
                        emit({"type": "thinking", "text": d.thinking})
            return await stream.get_final_message()

    async def _run_tool(self, ctx: ToolContext, block, emit: Emit) -> dict:
        result, is_error = await asyncio.to_thread(run_tool, ctx, block.name, block.input)
        summary = result.pop("_summary", None)
        emit({"type": "tool_end", "id": block.id, "name": block.name, "ok": not is_error,
              "summary": result.get("error", "")[:160] if is_error else (summary or "")})
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": is_error,
                "content": json.dumps(result, ensure_ascii=False, default=str)}


NO_KEY = ("No Anthropic API key found. Copy .env.example to .env in the project folder, set "
          "ANTHROPIC_API_KEY, then restart the chatbot.")


def describe_api_error(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return ("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in the .env file, "
                "then restart the chatbot.")
    if isinstance(e, TypeError) and "authentication" in str(e).lower():
        return NO_KEY  # the SDK raises this before any request when no credentials exist
    if isinstance(e, anthropic.PermissionDeniedError):
        return "This API key isn't allowed to use the model."
    if isinstance(e, anthropic.NotFoundError):
        return "The model wasn't found. Check CHATBOT_MODEL."
    if isinstance(e, anthropic.RateLimitError):
        return "The Claude API is rate-limiting us. Try again in a minute."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}: {getattr(e, 'message', e)}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API. Check the internet connection."
    if isinstance(e, anthropic.AnthropicError):
        msg = str(e)
        if "api_key" in msg or "auth" in msg.lower():
            return NO_KEY
        return f"Claude client error: {msg}"
    return f"Unexpected error: {type(e).__name__}: {e}"
