"""Follow-ups the model writes at the end of an answer, taken out of the text as it streams.

The system prompt asks for `<next>q1 | q2 | q3</next>` (suggested next questions) after a final
answer, `<choices>a | b</choices>` (the options) after a clarifying question, and
`<do>share_location | pick_on_map</do>` (actions only the user can do on the page, by key: the
page owns each one's label, icon and behaviour). They cost no extra call: the model writes them
in the same response. TagFilter sits on the text deltas and
holds back anything that could still become one of these tags, so the raw tag never reaches the
page, even when it arrives split across deltas. The history keeps the text unchanged.
"""

from __future__ import annotations

import re

TAGS = ("next", "choices", "do")
MAX_ITEMS = {"next": 3, "choices": 5, "do": 3}
MAX_CHARS = 80  # a chip longer than this is not a chip
MAX_TAG = 800  # an "opening tag" with no close this far on is just text
_OPEN = re.compile(r"<(next|choices|do)>")

# What the page can do when the user clicks an action chip (chatbot/web/src/lib/actions.ts has
# the label, icon and behaviour of each). The prompt lists the same keys; anything else is dropped.
ACTIONS = ("share_location", "pick_on_map", "city_3d", "go_live", "data_sources")


def parse_items(body: str, limit: int, allowed: tuple[str, ...] | None = None) -> list[str]:
    """`a | b | c` (or one per line) as up to `limit` short, distinct, non-empty items. With
    `allowed`, items are keys ("Share location" reads as share_location) and others are dropped."""
    out: list[str] = []
    for raw in re.split(r"[|\n,]" if allowed else r"[|\n]", body):
        item = re.sub(r"\s+", " ", raw).strip().lstrip("-*•").strip().strip('"`').strip()
        if allowed is not None:
            item = re.sub(r"[\s-]+", "_", item.lower())
            if item not in allowed:
                continue
        if item and len(item) <= MAX_CHARS and item.lower() not in (o.lower() for o in out):
            out.append(item)
    return out[:limit]


def merge_actions(*lists: list[str] | None) -> list[str]:
    """Actions from several places (the server's own, then the model's), each once, in order."""
    out: list[str] = []
    for items in lists:
        out += [a for a in items or () if a in ACTIONS and a not in out]
    return out[:MAX_ITEMS["do"]]


class TagFilter:
    """Feed it text deltas; it returns the text to show and collects the tags it took out."""

    def __init__(self) -> None:
        self.buf = ""
        self.found: dict[str, list[str]] = {}

    def _take(self, name: str, body: str) -> None:
        items = parse_items(body, MAX_ITEMS[name], ACTIONS if name == "do" else None)
        if items:
            self.found[name] = items

    def feed(self, text: str) -> str:
        self.buf += text
        out: list[str] = []
        while self.buf:
            i = self.buf.find("<")
            if i < 0:
                out.append(self.buf)
                self.buf = ""
                break
            out.append(self.buf[:i])
            self.buf = self.buf[i:]
            if m := _OPEN.match(self.buf):
                close = f"</{m.group(1)}>"
                end = self.buf.find(close, m.end())
                if end >= 0:
                    self._take(m.group(1), self.buf[m.end():end])
                    self.buf = self.buf[end + len(close):]
                    continue
                if len(self.buf) <= MAX_TAG:
                    break  # the rest of the tag is still on its way
            elif any(f"<{t}>".startswith(self.buf) for t in TAGS):
                break  # "<ne": could still become "<next>"
            out.append("<")  # an ordinary "<" ("a < b", "<br>")
            self.buf = self.buf[1:]
        return "".join(out)

    def flush(self) -> str:
        """The end of a text block: a tag cut off before its close still counts; a lone "<ne"
        was text after all."""
        rest, self.buf = self.buf, ""
        if m := _OPEN.match(rest):
            self._take(m.group(1), rest[m.end():])
            return ""
        return rest

