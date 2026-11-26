"""Memory tools: query_history and run_sql, over the twin's own store. They live in
chatbot/memory_tools.py (with the history the inspector's band reads); this module puts them in
the registry."""

from __future__ import annotations

from chatbot import memory_tools
from chatbot.tools.base import Tool, ToolSet

TOOLS = ToolSet(Tool(d, memory_tools.HANDLERS[d["name"]]) for d in memory_tools.TOOL_DEFS)
