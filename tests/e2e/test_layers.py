"""Path 3: the Layers sheet by keyboard only. Tab to the layers button, open the sheet, move
through the City systems groups with the arrow keys (one group open at a time), switch a sensor
layer on, close with Esc, and the keyboard is back on the layers button."""

import re

import pytest
from playwright.sync_api import Page, expect

from .conftest import focused, open_twin, shot, tab_to

pytestmark = pytest.mark.e2e


def layers_button(page: Page):
    return page.get_by_role("group", name="Map controls").get_by_role("button", name="Layers", exact=True)


def group(sheet, name: str):
    # A closed group's header also says how many of its layers are on ("Sensors 1 on").
    return sheet.get_by_role("button", name=re.compile(rf"^{name}( \d+ on)?$"))


@pytest.mark.parametrize("size", ["desktop", "phone"])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_layers_sheet_by_keyboard(page: Page, size: str, motion: str):
    open_twin(page)
    tab_to(page, layers_button(page))
    page.keyboard.press("Enter")
    sheet = page.get_by_role("dialog", name="Layers")
    expect(sheet).to_be_visible()
    # The keyboard starts on the sheet's title, so a screen reader starts there.
    expect(sheet.get_by_role("heading", name="Layers")).to_be_focused()

    # Sensors is open to begin with; Tab reaches its header.
    sensors, buildings, alerts = (group(sheet, n) for n in ("Sensors", "Buildings", "Alerts"))
    expect(sensors).to_have_attribute("aria-expanded", "true")
    tab_to(page, sensors)

    # Down and Up move between the headers; Enter opens one and closes the one that was open.
    page.keyboard.press("ArrowDown")
    expect(buildings).to_be_focused()
    page.keyboard.press("Enter")
    expect(buildings).to_have_attribute("aria-expanded", "true")
    expect(sensors).to_have_attribute("aria-expanded", "false")
    page.keyboard.press("End")
    expect(alerts).to_be_focused()
    page.keyboard.press("Home")
    expect(sensors).to_be_focused()
    page.keyboard.press("Enter")
    expect(sensors).to_have_attribute("aria-expanded", "true")
    expect(buildings).to_have_attribute("aria-expanded", "false")

    # Tab goes into the open group: switch the parking sensors on with Space.
    parking = sheet.get_by_role("switch", name="Parking sensors")
    expect(parking).not_to_be_checked()
    tab_to(page, parking)
    page.keyboard.press("Space")
    expect(parking).to_be_checked()
    # The map controls say how many layers are on.
    shot(page, f"03-layers-sheet-{motion}")

    # Esc closes the sheet and gives the keyboard back to the layers button.
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(layers_button(page)).to_be_focused()
    assert focused(page) == "Layers"

    # The sensor layer stays on, and the sheet remembers it.
    page.keyboard.press("Enter")
    expect(page.get_by_role("dialog", name="Layers").get_by_role("switch", name="Parking sensors")).to_be_checked()
