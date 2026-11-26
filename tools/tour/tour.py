# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.50"]
# ///
"""Record a tour of the Nicosia digital twin, for the showcase video (see README.md here).

Drives a running twin in headless Chromium (on the GPU) at 1600x840, DPR 2, and records it with
the CDP screencast (sharper than Playwright's own video). Writes frames/ (jpeg) with their times,
and markers.json (captions and the stretches to fast-forward) for build.py to cut the video.

    uv run tools/tour/tour.py OUT_DIR [--base URL] [--only=live,mobility] [--no-questions]

The scenes that ask the assistant (traffic, memory, replay, trip) spend API credit, one question
each; --no-questions skips them.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import Page, async_playwright

BASE = "http://127.0.0.1:8000"
W, H = 1600, 840
OLD_TOWN = "#/@35.1712,33.3622,15.1z,50p,-12b"

CURSOR = """
(() => {
  const boot = () => {
    if (document.getElementById('tour-cursor')) return;
    const c = document.createElement('div');
    c.id = 'tour-cursor';
    c.innerHTML = `<svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2.5l15.5 9.2-6.9 1.5 3.9 7.3-3 1.6-3.9-7.3-5.1 4.6z"
      fill="#ffffff" stroke="#0e1828" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
    Object.assign(c.style, {position: 'fixed', left: '0', top: '0', zIndex: '2147483647', pointerEvents: 'none',
      transform: 'translate(-100px,-100px)', filter: 'drop-shadow(0 1px 2px rgba(0,0,0,.55))', transition: 'none'});
    document.documentElement.appendChild(c);
    document.addEventListener('mousemove', (e) => { c.style.transform = `translate(${e.clientX - 3}px, ${e.clientY - 2}px)`; }, true);
    document.addEventListener('mousedown', (e) => {
      const r = document.createElement('div');
      Object.assign(r.style, {position: 'fixed', left: `${e.clientX - 18}px`, top: `${e.clientY - 18}px`, width: '36px', height: '36px',
        borderRadius: '50%', border: '2px solid #ffae45', zIndex: '2147483646', pointerEvents: 'none', opacity: '0.9',
        transition: 'transform .45s ease-out, opacity .45s ease-out', transform: 'scale(.4)'});
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.25)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 600);
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
"""


class Tour:
    def __init__(self, page: Page, out: Path, dry: bool, base: str, fast: float):
        self.page, self.out, self.dry, self.base, self.fast = page, out, dry, base, fast
        self.markers: list[dict] = []
        self.frames: list[tuple[float, str]] = []
        self.x, self.y = W / 2, H / 2
        (out / "frames").mkdir(parents=True, exist_ok=True)

    # -- recording ---------------------------------------------------------------------------
    async def start(self):
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": 1920,
                                                     "maxHeight": 1008, "everyNthFrame": 1})

    def _frame(self, ev):
        n = len(self.frames)
        name = f"frames/{n:06d}.jpg"
        (self.out / name).write_bytes(base64.b64decode(ev["data"]))
        self.frames.append((ev["metadata"].get("timestamp") or time.time(), name))
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))

    async def stop(self):
        await self.cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.3)
        (self.out / "frames.json").write_text(json.dumps(self.frames))
        (self.out / "markers.json").write_text(json.dumps(self.markers, indent=1))

    def mark(self, **m):
        self.markers.append({"t": time.time(), **m})
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    def caption(self, title: str, sub: str = ""):
        self.mark(type="caption", title=title, sub=sub)

    def speed(self, x: float):
        self.mark(type="speed", x=x)

    # -- acting ------------------------------------------------------------------------------
    async def hold(self, s: float):
        await self.page.wait_for_timeout(int(s * 1000))

    async def move(self, x: float, y: float, steps: int = 28):
        await self.page.mouse.move(x, y, steps=steps)
        self.x, self.y = x, y

    async def point(self, loc, dx: float = 0.5, dy: float = 0.5, steps: int = 28):
        await loc.scroll_into_view_if_needed(timeout=8000)
        box = await loc.bounding_box()
        if not box:
            raise RuntimeError(f"no box for {loc}")
        await self.move(box["x"] + box["width"] * dx, box["y"] + box["height"] * dy, steps)
        return box

    async def click(self, loc, pause: float = 0.35, **kw):
        await self.point(loc, **kw)
        await self.hold(pause)
        await self.page.mouse.click(self.x, self.y)

    async def key(self, keys: str, pause: float = 0.5):
        await self.page.keyboard.press(keys)
        await self.hold(pause)

    async def type(self, text: str, delay: int = 32):
        await self.page.keyboard.type(text, delay=delay)

    async def search(self, text: str, then: float = 0.8):
        await self.key("ControlOrMeta+k", 0.6)
        await self.type(text, 55)
        await self.hold(then)
        await self.key("Enter", 0.3)

    async def ask(self, question: str, timeout_s: float = 240):
        """Type a question, send it and wait for the answer; the wait is fast-forwarded."""
        if self.dry:
            self.mark(type="skipped_question", q=question)
            return False
        box = self.page.get_by_role("textbox", name="Message")
        await self.click(box)
        await self.type(question, 28)
        await self.hold(0.5)
        await self.key("Enter", 0.8)
        self.speed(self.fast)
        working = self.page.get_by_role("button", name="Working")
        try:
            await working.wait_for(state="visible", timeout=10000)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if not await working.is_visible():
                break
            await self.hold(0.5)
        self.speed(1)
        await self.hold(1.2)
        return True

    async def new_chat(self):
        b = self.page.get_by_role("button", name="New chat")
        if await b.is_visible() and await b.is_enabled():
            await self.click(b)
            await self.hold(0.8)


async def scene(t: Tour, name: str, fn):
    try:
        await fn(t)
    except Exception as e:  # noqa: BLE001 - one failed scene leaves the rest of the tour
        t.mark(type="error", scene=name, error=str(e)[:300])
        await t.page.keyboard.press("Escape")
        await t.hold(0.5)


# -- scenes ---------------------------------------------------------------------------------

async def live_city(t: Tour):
    t.caption("A live twin of Nicosia", "Street lights, parking, bins, air, weather and the registers, read from the city right now")
    await t.hold(1.5)
    strip = t.page.locator("header [data-readout]")
    for i in range(min(await strip.count(), 6)):
        await t.point(strip.nth(i), steps=18)
        await t.hold(0.9 if i != 1 else 2.2)
    await t.move(W * 0.55, H * 0.55, 30)
    await t.hold(1.0)


async def mobility(t: Tour):
    t.caption("Buses and road speeds, live", "Every bus from the national feed, gliding along its route; roads coloured against free flow")
    await t.key("Alt+2", 0.5)
    await t.hold(5.5)
    line = t.page.locator("aside[data-workspace] button[aria-label^='Line 2,']")
    if not await line.count():
        line = t.page.locator("aside[data-workspace] button[aria-label^='Line ']")
    await t.click(line.first)
    await t.hold(3.0)
    t.caption("Open any bus", "How late it runs and why we think so, its next stops, and the route ahead")
    bus = t.page.locator("aside[data-inspector] button").filter(has_text="Bus ").first
    await t.click(bus)
    await t.hold(6.0)
    stops = t.page.locator("aside[data-inspector] [aria-label='Next stops'] button")
    stop = stops.nth(1) if await stops.count() > 1 else stops.first
    t.caption("Every stop has a departure board", "Live times in sodium, from the feed; the rest from the timetable")
    await t.click(stop)
    await t.hold(5.0)
    await t.key("Escape", 0.6)
    back = t.page.get_by_role("button", name="Back to Live")
    if await back.is_visible():
        await t.click(back)
    await t.hold(1.0)


async def inspect_anything(t: Tour):
    t.caption("Search and inspect anything", "Ctrl K finds devices, streets, places and register file numbers")
    await t.hold(0.8)
    await t.search("540/2024", then=1.6)
    await t.hold(4.5)
    t.caption("Every device has a story", "Its state, its last 24 hours against the usual day, and what is around it")
    await t.search("YL1015", then=1.4)
    await t.hold(3.5)
    spark = t.page.locator("aside[data-inspector] [role='group'][tabindex='0']").first
    if await spark.count():
        box = await t.point(spark, dx=0.1)
        for k in range(1, 9):
            await t.move(box["x"] + box["width"] * (0.1 + 0.1 * k), box["y"] + box["height"] * 0.5, 8)
            await t.hold(0.15)
    around = t.page.locator("aside[data-inspector] button, aside[data-inspector] [data-slot='item']").filter(has_text="Parking bay").first
    if await around.count():
        await t.click(around)
        await t.hold(4.0)
    await t.key("Escape", 0.8)


async def city_clock(t: Tour):
    t.caption("One clock for the whole twin", "Go back to any moment the twin remembers: lamps, parking, bins and air as they were")
    await t.search("Back to the whole city", then=0.8)
    await t.hold(2.5)
    await t.move(W * 0.55, H * 0.58, 20)
    for _ in range(4):
        await t.page.mouse.wheel(0, -260)
        await t.hold(0.35)
    await t.hold(1.5)
    await t.page.evaluate("document.activeElement && document.activeElement.blur()")
    await t.key("t", 0.8)  # the page's own shortcut: the keyboard on the clock
    await t.key("PageDown", 1.6)  # a day back
    hour = datetime.now(ZoneInfo("Asia/Nicosia")).hour  # then an hour at a time to 21:00
    for _ in range(abs(21 - hour)):
        await t.key("Shift+ArrowRight" if hour < 21 else "Shift+ArrowLeft", 0.45)
    label = await t.page.locator("[data-clock-part='track'] [role='slider'], [data-clock-part='track'] input").first.get_attribute("aria-valuetext")
    t.mark(type="note", clock=label)
    await t.hold(4.0)
    t.caption("Compare with last week", "A swipe across the map: the same time last week on the left, last night on the right")
    await t.click(t.page.get_by_role("button", name="Compare two moments"))
    await t.hold(0.8)
    await t.click(t.page.get_by_role("menuitemradio", name="The same time last week"))
    await t.hold(2.5)
    thumb = t.page.locator("[data-slot] [role='slider'], [role='slider']").filter(has_not=t.page.locator("[aria-label='City clock']"))
    handle = None
    for i in range(await thumb.count()):
        cand = thumb.nth(i)
        if (await cand.get_attribute("aria-label") or "") != "City clock" and await cand.is_visible():
            handle = cand
    if handle is not None:
        box = await t.point(handle)
        await t.page.mouse.down()
        for x in (box["x"] - 260, box["x"] + 220, box["x"] - 60):
            await t.page.mouse.move(x, box["y"] + box["height"] / 2, steps=45)
            await t.hold(0.6)
        await t.page.mouse.up()
    await t.hold(2.0)
    await t.click(t.page.get_by_role("button", name="Compare two moments (on)"))
    await t.hold(0.6)
    await t.click(t.page.get_by_role("menuitemradio", name="Nothing"))
    await t.hold(0.6)
    await t.key("l", 1.5)


async def ask_traffic(t: Tour):
    await t.hold(1.0)
    t.caption("Ask in plain words", "Answers come back as visuals: the street lit up, a column per camera, a chart on the stage")
    if await t.ask("How busy does Ledras Street get on foot? Chart the people per hour yesterday."):
        await t.hold(3.0)
        t.caption("The chart drives the map", "Hover an hour and the street, the columns and the clock replay it")
        plot = t.page.locator("section[aria-label='Visual stage'] svg.marks").first
        if await plot.count():
            box = await t.point(plot, dx=0.12, dy=0.55)
            for k in range(1, 15):
                await t.move(box["x"] + box["width"] * (0.12 + 0.055 * k), box["y"] + box["height"] * 0.55, 10)
                await t.hold(0.35)
        await t.hold(2.0)


async def ask_memory(t: Tour):
    await t.new_chat()
    t.caption("The twin remembers", "Weeks of readings in its own memory, compared with the usual for that hour and weekday")
    if await t.ask("How full is parking on Makariou on weekday evenings compared with usual?"):
        await t.hold(6.0)


async def ask_replay(t: Tour):
    await t.new_chat()
    t.caption("Replay a night", "A range on the clock plays back: the lamps, the strip and the chart follow it")
    if await t.ask("Replay the street lights last night from 17:00 to 07:00 with a chart of lit lamps"):
        await t.hold(10.0)


async def ask_trip(t: Tour):
    await t.new_chat()
    t.caption("Plan a bus trip", "Walks in grey, the ride in its line's colour, numbered stops, and an honest note when walking wins")
    if await t.ask("Can I take the bus from Eleftheria Square to the Cyprus Museum?"):
        await t.hold(6.0)


SCENES = [("live", live_city), ("mobility", mobility), ("inspect", inspect_anything), ("clock", city_clock),
          ("traffic", ask_traffic), ("memory", ask_memory), ("replay", ask_replay), ("trip", ask_trip)]


async def main():
    ap = argparse.ArgumentParser(description="Record a tour of the twin (see tools/tour/README.md).")
    ap.add_argument("out", type=Path, help="a new folder for this run's frames and markers")
    ap.add_argument("--base", default=os.environ.get("TOUR_BASE", BASE), help=f"the twin's address (default {BASE})")
    ap.add_argument("--only", default="", help=f"scenes to record, comma separated: {', '.join(n for n, _ in SCENES)}")
    ap.add_argument("--no-questions", action="store_true", help="skip the scenes' questions to the assistant")
    ap.add_argument("--fast", type=float, default=8.0, help="how much faster waits for an answer play (default 8)")
    ap.add_argument("--chrome", default=os.environ.get("TOUR_CHROME"),
                    help="a Chrome or Chromium to use instead of Playwright's own")
    a = ap.parse_args()
    only = [s for s in a.only.split(",") if s]
    unknown = set(only) - {n for n, _ in SCENES}
    if unknown:
        sys.exit(f"unknown scenes: {', '.join(sorted(unknown))}")
    # The full Chromium in headless mode draws WebGL on the GPU (the headless shell would not).
    args = ["--enable-gpu", "--ignore-gpu-blocklist"] + (["--use-angle=metal"] if sys.platform == "darwin" else [])
    async with async_playwright() as p:
        how = {"executable_path": a.chrome} if a.chrome else {"channel": "chromium"}
        b = await p.chromium.launch(headless=True, args=args, **how)
        ctx = await b.new_context(viewport={"width": W, "height": H}, device_scale_factor=2,
                                  timezone_id="Asia/Nicosia", locale="en-GB", reduced_motion="no-preference")
        await ctx.add_init_script(CURSOR)
        page = await ctx.new_page()
        await page.goto(a.base.rstrip("/") + "/" + OLD_TOWN)
        await page.get_by_text("Lamps lit").first.wait_for(timeout=30000)
        await page.wait_for_timeout(9000)  # the strip, the tiles and the lamps
        t = Tour(page, a.out, a.no_questions, a.base.rstrip("/"), a.fast)
        await t.move(W * 0.6, H * 0.5, 1)
        await t.start()
        t.mark(type="start")
        await t.hold(1.0)
        for name, fn in SCENES:
            if only and name not in only:
                continue
            t.mark(type="scene", name=name)
            await scene(t, name, fn)
        t.caption("", "")
        await t.hold(1.5)
        t.mark(type="end")
        await t.stop()
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
