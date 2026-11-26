"""Chat basics: follow-up tags taken out of the stream, and Stop, Retry and Edit going back to
the start of a turn while the history stays a valid conversation. Offline, against a scripted
fake of the Claude streaming API."""

import asyncio
import itertools
import json
import threading
from types import SimpleNamespace

import pytest

from chatbot import agent as agent_mod
from chatbot.agent import Agent
from chatbot.followups import ACTIONS, TagFilter, merge_actions, parse_items
from chatbot.prompt import SYSTEM_PROMPT
from chatbot.routes import chat as chat_routes
from chatbot.settings import Settings

from conftest import utc

ANSWER = "Lamp YL1015 is on.\n\n<next>Walk me there | How was it at 19:00? | Show it in 3D</next>"
NEXT = ["Walk me there", "How was it at 19:00?", "Show it in 3D"]


# -- the tag filter -----------------------------------------------------------------------------


def run_filter(pieces):
    f = TagFilter()
    shown = "".join(f.feed(p) for p in pieces) + f.flush()
    return shown, f.found


def test_tags_come_out_however_the_deltas_split_them():
    for cut in range(len(ANSWER) + 1):
        for cut2 in range(cut, len(ANSWER) + 1, 7):
            shown, found = run_filter([ANSWER[:cut], ANSWER[cut:cut2], ANSWER[cut2:]])
            assert shown == "Lamp YL1015 is on.\n\n", (cut, cut2)
            assert found == {"next": NEXT}
    shown, found = run_filter(list(ANSWER))  # one character at a time
    assert shown == "Lamp YL1015 is on.\n\n" and found == {"next": NEXT}


def test_the_tag_is_never_shown_half_arrived():
    f = TagFilter()
    shown = ""
    for ch in ANSWER:
        shown += f.feed(ch)
        assert "<" not in shown and "next" not in shown.lower()


def test_ordinary_angle_brackets_pass_through():
    text = "a < b, 3<4, <b>bold</b>, <nextdoor> and <choice> stay; <ne"
    shown, found = run_filter([text[:20], text[20:]])
    assert shown == text and found == {}


def test_a_tag_cut_off_at_the_end_still_counts_and_choices_are_read():
    shown, found = run_filter(["Which one?\n<choices>Makariou Avenue, Nicosia | Makariou Ave", "nue, Larnaca"])
    assert shown == "Which one?\n"
    assert found == {"choices": ["Makariou Avenue, Nicosia", "Makariou Avenue, Larnaca"]}


def test_items_are_short_distinct_and_capped():
    assert parse_items(" a |  | A | - b\n c | d ", 3) == ["a", "b", "c"]
    assert parse_items("x" * 81 + " | ok", 3) == ["ok"]


# -- the agent: a scripted fake of the streaming API --------------------------------------------


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def final(content, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage,
                           model="claude-sonnet-5-5")


class Hang:
    """A step that streams `text` and then never finishes (until it is cancelled)."""

    def __init__(self, text=""):
        self.text = text


class FakeStream:
    def __init__(self, step, chunk):
        self.step, self.chunk = step, chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            step = self.step
            blocks = [block("text", text=step.text)] if isinstance(step, Hang) else step.content
            for b in blocks:
                yield SimpleNamespace(type="content_block_start", content_block=b)
                if b.type == "text":
                    for i in range(0, len(b.text), self.chunk):
                        yield SimpleNamespace(type="content_block_delta",
                                              delta=SimpleNamespace(type="text_delta", text=b.text[i:i + self.chunk]))
                yield SimpleNamespace(type="content_block_stop")
            if isinstance(step, Hang):
                await asyncio.Event().wait()
        return gen()

    async def get_final_message(self):
        return self.step


class FakeClaude:
    def __init__(self, script, chunk=4):
        self.script = list(script)
        self.chunk = chunk
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(list(kwargs["messages"]))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeStream(step, self.chunk)


def make_agent(script, chunk=4):
    fake = FakeClaude(script, chunk)
    city = SimpleNamespace(now=lambda: utc(2026, 10, 20, 9, 0))
    agent = Agent(Settings(), city, None, None, client=fake)
    return fake, agent, agent.conversation(None)


async def turn(agent, conv, text, events=None, replace=None):
    await agent.run_turn(conv, text, None, (events if events is not None else []).append, replace=replace)
    await asyncio.sleep(0)  # let thread-safe emits land


def assert_valid(messages):
    """What the API accepts as a conversation to continue: user first, roles alternating, every
    tool_use answered in the very next message, and nothing after the last finished answer."""
    if not messages:
        return
    assert messages[0]["role"] == "user" and messages[-1]["role"] == "assistant"
    for a, b in itertools.pairwise(messages):
        assert a["role"] != b["role"]
    for i, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        uses = {b.id for b in m["content"] if b.type == "tool_use"}
        if uses:
            answered = {r["tool_use_id"] for r in messages[i + 1]["content"] if r.get("type") == "tool_result"}
            assert answered == uses


def texts(events):
    return "".join(e["text"] for e in events if e["type"] == "text")


def test_follow_ups_are_taken_out_of_the_stream_and_kept_in_history():
    msg = final([block("text", text=ANSWER)])
    for chunk in (1, 3, 1000):
        _, agent, conv = make_agent([msg], chunk)
        events = []
        asyncio.run(turn(agent, conv, "is YL1015 on?", events))
        assert texts(events) == "Lamp YL1015 is on.\n\n"
        assert [e for e in events if e["type"] == "follow_ups"] == [{"type": "follow_ups", "next": NEXT}]
        assert conv.messages[-1]["content"] is msg.content  # history keeps the answer as written
        assert events[0] == {"type": "turn", "index": 0}


def test_choices_come_with_a_clarifying_question():
    ask = "Which Makariou Avenue?\n<choices>Makariou Avenue, Nicosia | Makariou Avenue, Larnaca</choices>"
    _, agent, conv = make_agent([final([block("text", text=ask)])], 5)
    events = []
    asyncio.run(turn(agent, conv, "parking on Makariou", events))
    assert texts(events) == "Which Makariou Avenue?\n"
    fu = next(e for e in events if e["type"] == "follow_ups")
    assert fu["choices"] == ["Makariou Avenue, Nicosia", "Makariou Avenue, Larnaca"] and "next" not in fu


def test_only_a_finished_answer_offers_follow_ups():
    early = final([block("text", text="Looking. <next>Too early</next>"),
                   block("tool_use", id="t1", name="map_clear", input={})], "tool_use")
    done = final([block("text", text="Cleared.")])
    _, agent, conv = make_agent([early, done], 2)
    events = []
    asyncio.run(turn(agent, conv, "clear the map", events))
    assert texts(events) == "Looking. Cleared."
    assert not any(e["type"] == "follow_ups" for e in events)


def answer(text):
    return final([block("text", text=text)])


def test_retry_replaces_the_latest_turn_and_keeps_the_prefix():
    fake, agent, conv = make_agent([answer("One."), answer("Two."), answer("Two again.")])

    async def go():
        await turn(agent, conv, "first")
        before = list(conv.messages)
        await turn(agent, conv, "second")
        assert conv.turns == [0, 2] and len(conv.messages) == 4
        events = []
        await turn(agent, conv, "second, asked again", events, replace=1)
        assert events[0] == {"type": "turn", "index": 1}
        return before

    before = asyncio.run(go())
    assert conv.turns == [0, 2] and len(conv.messages) == 4
    assert conv.messages[:2] == before  # the cached prefix, unchanged
    assert "second, asked again" in conv.messages[2]["content"][0]["text"]
    assert conv.messages[3]["content"][0].text == "Two again."
    sent = fake.calls[-1]
    assert sent[:2] == before and len(sent) == 3  # Claude saw the first turn and the new question
    assert_valid(conv.messages)


def test_edit_of_the_first_turn_starts_over():
    _, agent, conv = make_agent([answer("One."), answer("Two."), answer("Edited.")])

    async def go():
        await turn(agent, conv, "first")
        await turn(agent, conv, "second")
        await turn(agent, conv, "first, edited", replace=0)

    asyncio.run(go())
    assert conv.turns == [0] and len(conv.messages) == 2
    assert "first, edited" in conv.messages[0]["content"][0]["text"]
    assert_valid(conv.messages)


def test_replacing_a_turn_that_is_already_gone_just_asks():
    _, agent, conv = make_agent([answer("One."), answer("Again.")])

    async def go():
        await turn(agent, conv, "first")
        await turn(agent, conv, "second", replace=1)  # turn 1 was stopped: nothing to take out

    asyncio.run(go())
    assert conv.turns == [0, 2] and len(conv.messages) == 4
    assert_valid(conv.messages)


def stop_during(agent, conv, text, ready, after=lambda: None):
    """Start a turn, wait until `ready()` says it is mid-way, cancel it (Stop), call `after()`
    and return the turn's events."""
    events = []

    async def go():
        task = asyncio.create_task(turn(agent, conv, text, events))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if ready(events):
                break
        else:
            raise AssertionError("the turn never got there")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        after()

    asyncio.run(go())
    return events


def test_stop_mid_text_leaves_the_history_as_before_the_turn():
    fake, agent, conv = make_agent([answer("One."), Hang("Half an ans"), answer("Fine.")])
    asyncio.run(turn(agent, conv, "first"))
    before = list(conv.messages)

    events = stop_during(agent, conv, "second", lambda ev: texts(ev) == "Half an ans")
    assert conv.messages == before and conv.turns == [0]
    assert events[0] == {"type": "turn", "index": 1}

    asyncio.run(turn(agent, conv, "third"))
    assert fake.calls[-1][:2] == before and len(fake.calls[-1]) == 3
    assert conv.turns == [0, 2]
    assert_valid(conv.messages)


def test_stop_while_tools_run_leaves_no_tool_use_without_its_result(monkeypatch):
    started, release = threading.Event(), threading.Event()
    real = agent_mod.run_tool

    def slow_tool(ctx, name, args):
        if name == "map_clear":
            started.set()
            release.wait(5)
        return real(ctx, name, args)

    monkeypatch.setattr(agent_mod, "run_tool", slow_tool)
    calls = final([block("text", text="Clearing and flying."),
                   block("tool_use", id="t1", name="map_clear", input={}),
                   block("tool_use", id="t2", name="map_fly_to", input={"center": [33.36, 35.17]})],
                  "tool_use")
    fake, agent, conv = make_agent([answer("One."), calls, answer("Fresh.")])
    try:
        asyncio.run(turn(agent, conv, "first"))
        before = list(conv.messages)
        def gone_while_the_tool_still_runs():
            # Mid-turn the history held the assistant's tool_use with no result yet: all of it
            # went, before the tool's thread even finished.
            assert conv.messages == before and conv.turns == [0]
            release.set()

        stop_during(agent, conv, "clear and fly", lambda ev: started.is_set(), gone_while_the_tool_still_runs)
        assert_valid(conv.messages)
    finally:
        release.set()

    asyncio.run(turn(agent, conv, "again"))
    assert fake.calls[-1][:2] == before
    assert conv.messages[-1]["content"][0].text == "Fresh."
    assert_valid(conv.messages)


def test_a_failed_turn_is_taken_out_too():
    _, agent, conv = make_agent([answer("One."), RuntimeError("API down")])
    asyncio.run(turn(agent, conv, "first"))
    before = list(conv.messages)
    with pytest.raises(RuntimeError):
        asyncio.run(turn(agent, conv, "second"))
    assert conv.messages == before and conv.turns == [0]


# -- the route: Stop, and the page going away ---------------------------------------------------


def route_request(agent):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent=agent)))


def sse(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return json.loads(chunk[6:])


def test_the_stop_route_ends_the_turn_and_says_so():
    _, agent, conv = make_agent([answer("One."), Hang("Half")])
    asyncio.run(turn(agent, conv, "first"))
    before = list(conv.messages)
    req = route_request(agent)

    async def go():
        res = await chat_routes.chat(chat_routes.ChatRequest(message="second", conversation_id=conv.id), req)
        stream = res.body_iterator
        hello = sse(await anext(stream))
        assert hello["type"] == "conversation" and hello["id"] == conv.id and hello["run"] in conv.runs
        seen = [sse(await anext(stream)) for _ in range(2)]  # the turn number, the half answer
        assert seen[0] == {"type": "turn", "index": 1} and seen[1]["text"] == "Half"
        assert await chat_routes.stop(conv.id, hello["run"], req) == {"stopped": True}
        rest = [sse(c) async for c in stream]
        assert [e["type"] for e in rest] == ["stopped", "done"]
        assert await chat_routes.stop(conv.id, hello["run"], req) == {"stopped": False}
        assert not conv.runs

    asyncio.run(go())
    assert conv.messages == before and conv.turns == [0]


def test_closing_the_stream_stops_the_turn():
    _, agent, conv = make_agent([Hang("Half")])
    req = route_request(agent)

    async def go():
        res = await chat_routes.chat(chat_routes.ChatRequest(message="first", conversation_id=conv.id), req)
        stream = res.body_iterator
        hello = sse(await anext(stream))
        task = agent.conversations[hello["id"]].runs[hello["run"]]
        await anext(stream)  # the turn number
        await stream.aclose()  # the page went away
        await asyncio.wait_for(task, 1)

    asyncio.run(go())
    assert conv.messages == [] and conv.turns == [] and not conv.runs


# -- actions: <do> and the ones the server offers itself ----------------------------------------


def test_do_holds_known_action_keys_only():
    shown, found = run_filter(["Share your spot and I'll plan it.\n<next>Walk from Eleftheria Square</next>\n",
                               "<do>share_location | Pick on map | open_settings | share_location</do>"])
    assert shown == "Share your spot and I'll plan it.\n\n"
    assert found == {"next": ["Walk from Eleftheria Square"], "do": ["share_location", "pick_on_map"]}
    assert parse_items("city_3d, go_live ,data_sources, `rm -rf`", 3, ACTIONS) == ["city_3d", "go_live", "data_sources"]
    assert run_filter(["<do>delete_everything</do>"]) == ("", {})  # nothing left: no tag at all


def test_do_comes_out_however_the_deltas_split_it():
    text = "Which one?\n<choices>From here | From Eleftheria Square</choices>\n<do>share_location|pick_on_map</do>"
    for cut in range(len(text) + 1):
        shown, found = run_filter([text[:cut], text[cut:]])
        assert shown == "Which one?\n\n", cut
        assert found == {"choices": ["From here", "From Eleftheria Square"], "do": ["share_location", "pick_on_map"]}


def test_every_action_is_in_the_prompt_and_merging_keeps_order():
    for key in ACTIONS:
        assert key in SYSTEM_PROMPT
    assert "right-click" not in SYSTEM_PROMPT.replace("right-clicking the map, the buttons do it", "")
    assert merge_actions(["share_location", "pick_on_map"], ["go_live", "share_location", "nope"]) == \
        ["share_location", "pick_on_map", "go_live"]


def test_a_tool_that_needs_the_location_makes_the_server_offer_it():
    # The model calls where_am_i with no location shared, then forgets the <do> tag.
    ask = final([block("tool_use", id="t1", name="where_am_i", input={})], "tool_use")
    reply = final([block("text", text="Your location isn't shared yet.\n<next>Walk from Eleftheria Square</next>")])
    fake, agent, conv = make_agent([ask, reply])
    events = []
    asyncio.run(turn(agent, conv, "where am I?", events))
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["ok"] is False and end["summary"] == "Your location isn't shared yet"
    result = json.loads(conv.messages[2]["content"][0]["content"])
    assert "_actions" not in result and "share it" in result["error"] and "right-click" not in result["error"]
    assert next(e for e in events if e["type"] == "follow_ups") == {
        "type": "follow_ups", "next": ["Walk from Eleftheria Square"], "do": ["share_location", "pick_on_map"]}


def test_the_server_offers_location_even_with_no_tags_at_all():
    ask = final([block("tool_use", id="t1", name="plan_transit", input={"to_place": "the studio"})], "tool_use")
    reply = final([block("text", text="I need your starting point.")])
    _, agent, conv = make_agent([ask, reply])
    agent.mobility.require_timetable = lambda: None  # no feed needed to know there's no origin
    events = []
    asyncio.run(turn(agent, conv, "bus to the studio", events))
    fu = [e for e in events if e["type"] == "follow_ups"]
    assert fu == [{"type": "follow_ups", "do": ["share_location", "pick_on_map"]}]


def test_no_actions_without_a_reason():
    _, agent, conv = make_agent([answer("Lamp YL1015 is on.\n<next>How was it at 19:00?</next>")])
    events = []
    asyncio.run(turn(agent, conv, "is YL1015 on?", events))
    assert next(e for e in events if e["type"] == "follow_ups") == {"type": "follow_ups", "next": ["How was it at 19:00?"]}
