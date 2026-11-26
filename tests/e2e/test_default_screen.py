"""Path 1: the default screen is calm. Topic chips, the now line and a way to ask; on a desktop
the conversation is open from the start, on a phone the ask bar sits on top of the map. Nothing
runs across the top as a readout strip."""

import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import open_twin, shot
from .twin import fixture

pytestmark = pytest.mark.e2e

CHIPS = ["Buses", "Parking", "Traffic", "Outside"]


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_default_screen(page: Page, size: str):
    open_twin(page)
    chips = page.get_by_role("group", name="Topics on the map")
    expect(chips.get_by_role("button")).to_have_text(CHIPS)
    for chip in chips.get_by_role("button").all():
        expect(chip).to_have_attribute("aria-pressed", "false")

    # The now line: the weather and the time, live.
    weather = fixture("overview")["weather"]
    outside = f"Outside now: {weather['sky'].capitalize()}, {round(weather['temp_c'])}°C"
    expect(page.get_by_role("button", name=re.compile("^" + re.escape(outside)))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Live, .*Show the city clock$"))).to_be_visible()
    expect(page.get_by_role("button", name="Layers", exact=True)).to_be_visible()

    conversation = page.get_by_role("complementary", name="Conversation")
    ask_bar = page.get_by_role("search", name="Ask about Nicosia")
    if size == "desktop":
        expect(conversation).to_be_visible()
        expect(conversation.get_by_role("textbox", name="Message")).to_be_visible()
        expect(ask_bar).to_have_count(0)
    else:
        expect(ask_bar.get_by_role("textbox", name="Ask about Nicosia")).to_be_visible()
        expect(conversation).to_have_count(0)

    # No readout strip across the top: the chips, the now line and the ask bar are all that sit
    # in the top band, over the map.
    top = page.evaluate("""() => [...document.querySelectorAll('.map-root > *')]
        .filter((el) => { const r = el.getBoundingClientRect(); return r.top < 60 && r.width > 4 && r.height > 4 && r.height < 200 && getComputedStyle(el).visibility !== 'hidden' })
        .map((el) => el.querySelector('#topic-chips') ? 'chips'
          : el.querySelector('[aria-label*="city clock"]') ? 'now line'
          : el.matches('form[role=search]') ? 'ask bar'
          : (el.getAttribute('aria-label') || el.textContent.slice(0, 60)))""")
    assert set(top) <= {"chips", "now line", "ask bar"}, f"something else across the top: {top}"
    assert ("chips" in top and "now line" in top) if size == "desktop" else "ask bar" in top
    assert not page.errors, page.errors
    shot(page, "01-default-screen")
