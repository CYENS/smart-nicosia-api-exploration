"""Fixtures for the browser tests: the page served offline, one browser context per test at the
size and motion setting the test asks for, the twin's API answered from fixtures, and the page's
clock set to the moment the fixtures were recorded.

    uv run --group e2e playwright install chromium     # once
    uv run --group e2e pytest -m e2e                    # the browser tests
    uv run --group e2e pytest -m e2e --headed --slowmo 300 -k layers   # watch one

Screenshots of each path land in test-results/screens (the CI job keeps them); traces of failed
tests in test-results when run with --tracing retain-on-failure.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from .twin import META, PageServer, Twin

SIZES = {
    "desktop": {"viewport": {"width": 1280, "height": 800}},
    "phone": {"viewport": {"width": 375, "height": 812}, "is_mobile": True, "has_touch": True},
}
# Where the browser says it is: on Arnaldas, by the fixtures' parking sensors and street lights.
HERE = {"latitude": 35.16744, "longitude": 33.36118, "accuracy": 20}
SCREENS = Path("test-results") / "screens"

expect.set_options(timeout=7_000)


@pytest.fixture(scope="session")
def site():
    server = PageServer()
    yield server
    server.close()


@pytest.fixture
def size() -> str:
    """The window: "desktop" (1280 x 800) or "phone" (375 x 812). Parametrize to change it."""
    return "desktop"


@pytest.fixture
def motion() -> str:
    """prefers-reduced-motion: "no-preference" or "reduce". Parametrize to change it."""
    return "no-preference"


@pytest.fixture
def browser_context_args(browser_context_args, site, size, motion):
    return {
        **browser_context_args,
        **SIZES[size],
        "base_url": site.url,
        "reduced_motion": motion,
        "locale": "en-GB",
        "timezone_id": "Asia/Nicosia",
        "geolocation": HERE,
        "permissions": ["geolocation", "clipboard-read", "clipboard-write"],
    }


@pytest.fixture
def twin(context, site) -> Twin:
    t = Twin(site)
    t.install(context)
    yield t
    # Every request the page made was answered from the fixtures; a new endpoint needs one.
    assert not t.unknown, f"API calls with no fixture: {t.unknown}"


@pytest.fixture
def page(page: Page, twin: Twin, size: str) -> Page:
    """The page, open on the default screen, at the moment the fixtures were recorded."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.clock.set_system_time(datetime.fromisoformat(META["recorded_at"].replace("Z", "+00:00")))
    page.errors = errors  # type: ignore[attr-defined]
    return page


def open_twin(page: Page, hash_: str = "") -> Page:
    page.goto("/" + hash_)
    expect(page.get_by_role("region", name="Map of Nicosia")).to_be_visible()
    # The now line has the city's weather once the overview has come in.
    expect(page.get_by_role("button", name=re.compile(r"^Outside now"))).to_be_visible()
    return page


def tab_to(page: Page, target, most: int = 60, key: str = "Tab") -> None:
    """Press Tab (or `key`) until `target` has the keyboard, as someone without a mouse would."""
    for _ in range(most):
        if target.evaluate("(el) => el === document.activeElement"):
            return
        page.keyboard.press(key)
    raise AssertionError(f"{most} presses of {key} never reached {target}")


def focused(page: Page) -> str:
    """What has the keyboard, by its accessible name (or its text)."""
    return page.evaluate("""() => { const a = document.activeElement
        return a ? (a.getAttribute('aria-label') || a.textContent || a.tagName).trim() : '' }""")


def shot(page: Page, name: str) -> None:
    """Keep a screenshot of a path, for the pull request and the CI run."""
    SCREENS.mkdir(parents=True, exist_ok=True)
    w = page.viewport_size["width"] if page.viewport_size else 0
    page.screenshot(path=SCREENS / f"{name}-{w}.png")
