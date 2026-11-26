# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.50"]
# ///
"""Cut recorded tour runs into the showcase video: the runs in order, the waits for answers sped
up, a caption band under the page, a title card and an end card. 1920x1080, 30 fps, H.264.
Needs ffmpeg (brew install ffmpeg). See README.md here.

    uv run tools/tour/build.py OUT.mp4 RUN_DIR [RUN_DIR ...]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parents[2]
FONTS = REPO / "chatbot/web/node_modules/@fontsource-variable"  # npm ci in chatbot/web
SANS = FONTS / "sofia-sans/files/sofia-sans-latin-wght-normal.woff2"
SANS_GREEK = FONTS / "sofia-sans/files/sofia-sans-greek-wght-normal.woff2"
COND = FONTS / "sofia-sans-condensed/files/sofia-sans-condensed-latin-wght-normal.woff2"
FPS = 30
UI_H, BAND = 1008, 72
TITLE_S, END_S = 4.0, 6.5
WORK = Path(".")  # set in main(): a folder next to the video for the cards and captions


def font(path: Path) -> str:
    """The font inline (Chrome won't load file:// fonts into a page from set_content); none when
    the page's packages aren't installed, and the cards fall back to the system's sans."""
    if not path.exists():
        return "local('Helvetica Neue')"
    return "url('data:font/woff2;base64," + base64.b64encode(path.read_bytes()).decode() + "')"


CSS = f"""
@font-face {{ font-family: Sofia; src: {font(SANS)}; font-weight: 100 900; }}
@font-face {{ font-family: Sofia; src: {font(SANS_GREEK)}; font-weight: 100 900; unicode-range: U+0370-03FF; }}
@font-face {{ font-family: SofiaCond; src: {font(COND)}; font-weight: 100 900; }}
* {{ margin: 0; box-sizing: border-box; }}
body {{ font-family: Sofia, sans-serif; color: #e4ebf4; background: transparent; -webkit-font-smoothing: antialiased; }}
.band {{ width: 1920px; height: {BAND}px; background: #0e1828; border-top: 1px solid rgba(138,160,190,.22);
  display: flex; align-items: center; gap: 18px; padding: 0 40px; }}
.dot {{ width: 12px; height: 12px; border-radius: 50%; background: #ffae45; box-shadow: 0 0 10px 2px rgba(255,174,69,.55); flex: none; }}
.t {{ font-size: 27px; font-weight: 640; letter-spacing: -.01em; white-space: nowrap; }}
.s {{ font-size: 23px; color: #8798b0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ff {{ display: inline-flex; align-items: center; gap: 10px; height: 40px; padding: 0 16px; border-radius: 999px;
  background: #15233a; border: 1px solid rgba(255,174,69,.45); color: #ffae45; font-family: SofiaCond, Sofia, sans-serif; font-size: 22px; font-weight: 560; }}
.card {{ width: 1920px; height: 1080px; background: radial-gradient(1200px 700px at 30% 35%, #15233a 0%, #0e1828 70%);
  display: flex; flex-direction: column; justify-content: center; padding: 0 180px; gap: 26px; }}
.ring {{ width: 64px; height: 64px; border-radius: 50%; border: 7px solid #ffae45; box-shadow: 0 0 34px 4px rgba(255,174,69,.35); }}
.h1 {{ font-size: 92px; font-weight: 700; letter-spacing: -.02em; line-height: 1; }}
.lead {{ font-size: 36px; color: #8798b0; max-width: 1300px; line-height: 1.3; }}
.small {{ font-size: 24px; color: #8798b0; font-family: SofiaCond, Sofia, sans-serif; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px 70px; font-size: 30px; max-width: 1500px; }}
.grid div::before {{ content: ''; display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: #ffae45; margin-right: 16px; vertical-align: middle; }}
.h2 {{ font-size: 56px; font-weight: 700; letter-spacing: -.015em; }}
.credits {{ font-size: 23px; color: #8798b0; line-height: 1.45; max-width: 1560px; border-top: 1px solid rgba(138,160,190,.22); padding-top: 22px; }}
"""

TITLE = """<div class="card"><div class="ring"></div><div class="h1">Nicosia digital twin</div>
<div class="lead">A live model of the city you can ask in plain words: its sensors, buses and roads, every building, and a memory of weeks.</div>
<div class="small">Recorded on live data, {day}</div></div>"""

END = """<div class="card"><div class="h2">What you saw</div><div class="grid">
<div>The live strip: lamps, parking, bins, air, weather</div><div>Live buses, stops and road speeds</div>
<div>Search and inspect anything</div><div>One city clock: back to any moment</div>
<div>Compare with last week by swiping</div><div>Answers as maps and linked charts</div>
<div>Weeks of memory against the usual</div><div>Bus trips planned on the live network</div></div>
<div class="credits">Data: Smart Nicosia platform (Municipality of Nicosia) · Cyprus National Access Point: live buses, timetables and road speeds (CC BY 4.0) ·
Open-Meteo (CC BY 4.0) · OpenStreetMap contributors (ODbL), OpenFreeMap and OpenMapTiles · Valhalla routing · Department of Lands and Surveys · ΕΟΑ dangerous-building registers</div></div>"""


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def timeline(runs: list[Path]):
    """Frames with their output durations, captions and fast-forward stretches on the output clock."""
    items: list[tuple[str, float]] = []
    captions: list[dict] = []
    fast: list[tuple[float, float, float]] = []
    out_t = TITLE_S
    for run in runs:
        frames = json.loads((run / "frames.json").read_text())
        markers = json.loads((run / "markers.json").read_text())
        t0 = next(m["t"] for m in markers if m["type"] == "start")
        t1 = next(m["t"] for m in markers if m["type"] == "end")
        speeds = sorted((m["t"], m["x"]) for m in markers if m["type"] == "speed")

        def speed_at(t):
            x = 1.0
            for when, v in speeds:
                if when <= t:
                    x = v
            return x

        def to_out(t, base=out_t):  # integrate 1/speed from t0 to t
            total, cur, x = 0.0, t0, 1.0
            for when, v in speeds + [(t, None)]:
                if when > t:
                    when = t
                if when > cur:
                    total += (when - cur) / x
                    cur = when
                if v is None or when >= t:
                    break
                x = v
            return base + total

        fs = [(ts, name) for ts, name in frames if t0 - 0.05 <= ts <= t1]
        if not fs or fs[0][0] > t0:
            first = max((f for f in frames if f[0] <= t0), default=frames[0], key=lambda f: f[0])
            fs.insert(0, (t0, first[1]))
        for i, (ts, name) in enumerate(fs):
            nxt = fs[i + 1][0] if i + 1 < len(fs) else t1
            d = to_out(nxt) - to_out(max(ts, t0))
            if d > 0:
                items.append((str(run / name), d))
        run_end = to_out(t1)
        caps = [m for m in markers if m["type"] == "caption"]
        for i, c in enumerate(caps):
            if not c["title"]:
                continue
            end = to_out(caps[i + 1]["t"]) if i + 1 < len(caps) else run_end
            captions.append({"title": c["title"], "sub": c["sub"], "start": to_out(c["t"]), "end": end})
        on = None
        for when, v in speeds:
            if v > 1 and on is None:
                on = (when, v)
            elif v <= 1 and on is not None:
                fast.append((to_out(on[0]), to_out(when), on[1]))
                on = None
        out_t = run_end
    return items, captions, fast, out_t


async def render_pngs(captions, fast, day: str, chrome: str | None):
    WORK.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, **({"executable_path": chrome} if chrome else {}))
        page = await b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)

        async def shot(html, path, w, h, transparent=False):
            await page.set_viewport_size({"width": w, "height": h})
            await page.set_content(f"<style>{CSS}</style>{html}")
            await page.evaluate("Promise.all([document.fonts.load('600 24px SofiaCond'), document.fonts.load('600 24px Sofia'), document.fonts.load('600 24px Sofia', 'ΕΟΑ')]).then(() => document.fonts.ready)")
            await page.wait_for_timeout(120)
            await page.screenshot(path=str(path), omit_background=transparent)

        await shot(TITLE.format(day=day), WORK / "title.png", 1920, 1080)
        await shot(END, WORK / "end.png", 1920, 1080)
        for i, c in enumerate(captions):
            await shot(f'<div class="band"><div class="dot"></div><div class="t">{esc(c["title"])}</div>'
                       f'<div class="s">{esc(c["sub"])}</div></div>', WORK / f"cap{i:02d}.png", 1920, BAND)
        for i, (_, _, x) in enumerate(fast):
            await shot(f'<div style="padding:4px"><span class="ff">▶▶ {x:g}× while the twin answers</span></div>',
                       WORK / f"ff{i:02d}.png", 420, 48, transparent=True)
        await b.close()


def recorded_on(run: Path) -> str:
    start = next(m["t"] for m in json.loads((run / "markers.json").read_text()) if m["type"] == "start")
    d = datetime.fromtimestamp(start, ZoneInfo("Asia/Nicosia"))
    return f"{d:%A} {d.day} {d:%B %Y}"


def main():
    global WORK
    ap = argparse.ArgumentParser(description="Cut recorded tour runs into the video (see tools/tour/README.md).")
    ap.add_argument("out", type=Path, help="the video to write, e.g. data/tour/nicosia-twin-tour.mp4")
    ap.add_argument("runs", type=Path, nargs="+", help="run folders from tour.py, in the order to play them")
    ap.add_argument("--chrome", default=os.environ.get("TOUR_CHROME"), help="a Chrome to draw the cards with")
    a = ap.parse_args()
    out, runs = a.out.resolve(), [r.resolve() for r in a.runs]  # ffmpeg's lists read paths from their own folder
    WORK = out.parent / f"{out.stem}-parts"
    if not SANS.exists():
        print(f"note: {FONTS} is missing (npm ci in chatbot/web): the cards use the system's sans")
    items, captions, fast, end_t = timeline(runs)
    asyncio.run(render_pngs(captions, fast, recorded_on(runs[0]), a.chrome))
    # 1. The page frames, at their output durations, as a 30 fps clip under an empty band.
    lst = WORK / "frames.txt"
    lst.write_text("".join(f"file '{f}'\nduration {d:.5f}\n" for f, d in items) + f"file '{items[-1][0]}'\n")
    body = WORK / "body.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"fps={FPS},scale=1920:{UI_H}:flags=lanczos,pad=1920:1080:0:0:color=0x0e1828,format=yuv420p",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "14", str(body)], check=True)
    # 2. Captions and fast-forward badges over the band, then the cards around it.
    body_s = end_t - TITLE_S
    inputs = ["-i", str(body)]
    graph = []
    n = 1
    last = "[0:v]"
    for i, c in enumerate(captions):
        d = max(0.5, c["end"] - c["start"])
        inputs += ["-loop", "1", "-t", f"{d:.3f}", "-i", str(WORK / f"cap{i:02d}.png")]
        st = c["start"] - TITLE_S
        graph.append(f"[{n}:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1,fade=t=out:st={max(0, d - 0.3):.3f}:d=0.3:alpha=1,"
                     f"setpts=PTS-STARTPTS+{st:.3f}/TB[c{n}]")
        graph.append(f"{last}[c{n}]overlay=0:{UI_H}:eof_action=pass[v{n}]")
        last, n = f"[v{n}]", n + 1
    for i, (a, b, _) in enumerate(fast):
        d = max(0.3, b - a)
        inputs += ["-loop", "1", "-t", f"{d:.3f}", "-i", str(WORK / f"ff{i:02d}.png")]
        graph.append(f"[{n}:v]format=rgba,setpts=PTS-STARTPTS+{a - TITLE_S:.3f}/TB[f{n}]")
        graph.append(f"{last}[f{n}]overlay=W-w-32:{UI_H + (BAND - 48) // 2}:eof_action=pass[v{n}]")
        last, n = f"[v{n}]", n + 1
    inputs += ["-loop", "1", "-t", str(TITLE_S), "-i", str(WORK / "title.png")]
    ti = n
    inputs += ["-loop", "1", "-t", str(END_S), "-i", str(WORK / "end.png")]
    ei = n + 1
    graph.append(f"{last}format=yuv420p,fade=t=in:st=0:d=0.4,fade=t=out:st={body_s - 0.4:.3f}:d=0.4[main]")
    graph.append(f"[{ti}:v]fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.6,fade=t=out:st={TITLE_S - 0.5}:d=0.5[title]")
    graph.append(f"[{ei}:v]fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.6,fade=t=out:st={END_S - 0.8}:d=0.8[end]")
    graph.append("[title][main][end]concat=n=3:v=1:a=0[outv]")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(graph),
                    "-map", "[outv]", "-r", str(FPS), "-c:v", "libx264", "-preset", "slow", "-crf", "19",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True).stdout.strip()
    print(f"{out}: {float(dur):.1f} s, {out.stat().st_size / 1e6:.1f} MB, {len(captions)} captions, {len(fast)} fast stretches")


if __name__ == "__main__":
    main()
