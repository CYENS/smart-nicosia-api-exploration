"""How big the system prompt and the tool list are, in tokens, as the model in .env counts them.

Uses the Claude API's token counting endpoint, which is free (it needs the API key in .env).

    uv run tools/chat_check/prompt_size.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from chatbot.prompt import SYSTEM_PROMPT  # noqa: E402
from chatbot.settings import load_settings  # noqa: E402
from chatbot.tools import TOOL_DEFS, WEB_SEARCH  # noqa: E402


def main() -> None:
    s = load_settings()
    client = anthropic.Anthropic()
    hi = [{"role": "user", "content": "hi"}]
    tools = TOOL_DEFS + ([WEB_SEARCH] if s.web_search else [])

    def count(**kw) -> int:
        return client.messages.count_tokens(model=s.model, messages=hi, **kw).input_tokens

    bare = count()
    system = count(system=SYSTEM_PROMPT) - bare
    both = count(system=SYSTEM_PROMPT, tools=tools) - bare
    print(json.dumps({"model": s.model, "tools": len(TOOL_DEFS), "system_prompt_tokens": system,
                      "tool_list_tokens": both - system, "together_tokens": both,
                      "system_prompt_chars": len(SYSTEM_PROMPT),
                      "tool_list_chars": len(json.dumps(TOOL_DEFS, ensure_ascii=False))}, indent=1))


if __name__ == "__main__":
    main()
