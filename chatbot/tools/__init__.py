"""The tools Claude can call: city data, places, routing, weather, buildings, map control, charts
and the twin's entities.

Data tools return compact JSON and park big results in the session store (`layer_ref`,
`data_ref`). UI tools push a command to the browser through `ctx.emit` and return "ok". Every
input is validated against its schema before the handler runs.

One module per domain (city, places, routes, buildings, maps, charts, entities); the order the
model sees them in is set in registry.py.
"""

from chatbot.store import STATE_COLORS
from chatbot.tools.base import (NEAR, Tool, ToolContext, ToolError, ToolSet, local_now,
                                my_location, obj, resolve_place, resolve_point, tool_def)
from chatbot.tools.charts import (add_time_rule, color_states, has_scrubber, keep_real_hours,
                                  link_chart, spec_error, vega_lite_validator)
from chatbot.tools.registry import DOMAINS, HANDLERS, SCHEMAS, TOOL_DEFS, TOOLS, WEB_SEARCH, run_tool

__all__ = [
    "DOMAINS", "HANDLERS", "NEAR", "SCHEMAS", "STATE_COLORS", "TOOLS", "TOOL_DEFS", "Tool",
    "ToolContext", "ToolError", "ToolSet", "WEB_SEARCH", "add_time_rule", "color_states",
    "has_scrubber", "keep_real_hours", "link_chart", "local_now", "my_location", "obj",
    "resolve_place", "resolve_point", "run_tool", "spec_error", "tool_def", "vega_lite_validator",
]
