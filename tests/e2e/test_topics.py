"""Path 2: the topic chips. One topic at a time; the arrow keys move along the chips, Enter picks,
Esc lets the topic go; the picked topic's card says how that part of the city is and offers a
question to ask about it."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import focused, open_twin, shot, tab_to
from .twin import Twin, fixture

pytestmark = pytest.mark.e2e


def chip(page: Page, name: str):
    return page.get_by_role("group", name="Topics on the map").get_by_role("button", name=name, exact=True)


def card(page: Page):
    return page.locator("section[aria-labelledby=topic-card-title]")


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_chips_by_keyboard(page: Page, size: str):
    open_twin(page)
    tab_to(page, chip(page, "Buses"))
    page.keyboard.press("Enter")
    expect(chip(page, "Buses")).to_have_attribute("aria-pressed", "true")
    expect(card(page).get_by_role("heading", name="Buses now")).to_be_visible()

    # The arrows move the keyboard along the chips; Enter picks, and only one stays picked.
    page.keyboard.press("ArrowRight")
    assert focused(page) == "Parking"
    page.keyboard.press("Enter")
    expect(chip(page, "Parking")).to_have_attribute("aria-pressed", "true")
    expect(chip(page, "Buses")).to_have_attribute("aria-pressed", "false")
    expect(card(page).get_by_role("heading", name="Parking now")).to_be_visible()
    free = fixture("overview")["parking"]["free"]
    expect(card(page)).to_contain_text(f"{free} bays free")
    expect(card(page).get_by_role("button", name="Ask about it")).to_be_visible()
    shot(page, "02-topic-card")

    # Esc lets the topic go: no chip picked, no card, the keyboard still on the chips.
    page.keyboard.press("Escape")
    for name in ("Buses", "Parking", "Traffic", "Outside"):
        expect(chip(page, name)).to_have_attribute("aria-pressed", "false")
    expect(card(page)).to_have_count(0)
    assert focused(page) == "Parking"


def test_card_asks_about_its_topic(page: Page, twin: Twin):
    open_twin(page)
    chip(page, "Parking").click()
    card(page).get_by_role("button", name="Ask about it").click()
    conversation = page.get_by_role("complementary", name="Conversation")
    expect(conversation.get_by_text("Where can I park right now? Show the free spaces on the map")).to_be_visible()
    expect(conversation.get_by_role("button", name="Tell me more")).to_be_visible()
    assert [c["message"] for c in twin.chats] == ["Where can I park right now? Show the free spaces on the map"]


@pytest.mark.xfail(strict=True, reason=(
    "Bug: closing a topic's card gives the keyboard to the first chip (Buses), not the chip that "
    "opened it. focusChips() in topic-chips.tsx looks for the pressed chip after the topic is "
    "already let go, so it falls back to the first."))
def test_card_closes_back_to_the_chip_that_opened_it(page: Page):
    open_twin(page)
    chip(page, "Traffic").click()
    close = card(page).get_by_role("button", name="Close traffic now")
    close.focus()
    page.keyboard.press("Escape")
    expect(card(page)).to_have_count(0)
    expect(chip(page, "Traffic")).to_be_focused()
