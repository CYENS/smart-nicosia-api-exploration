"""A contact sheet of a recorded run, to check it before cutting the video: the frame some seconds
after each caption (and each error or skipped question). Needs ffmpeg.

    python3 tools/tour/peek.py RUN_DIR [SECONDS,SECONDS]   (default 2.5)

Writes RUN_DIR/sheet/sheet.jpg and lists the captions with their times.
"""
import bisect
import json
import subprocess
import sys
from pathlib import Path

run = Path(sys.argv[1]).resolve()  # ffmpeg's list reads paths from its own folder
offsets = [float(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "2.5").split(",")]
frames = json.loads((run / "frames.json").read_text())
markers = json.loads((run / "markers.json").read_text())
ts = [f[0] for f in frames]
picks = []
caps = [m for m in markers if m["type"] in ("caption", "error", "skipped_question")]
for i, m in enumerate(caps):
    for off in offsets:
        j = min(bisect.bisect_left(ts, m["t"] + off), len(frames) - 1)
        picks.append(run / frames[j][1])
out = run / "sheet"
out.mkdir(exist_ok=True)
lst = out / "list.txt"
lst.write_text("".join(f"file '{p}'\n" for p in picks))
cols = len(offsets) if len(offsets) > 1 else 3
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-vf", f"scale=640:-1,tile={cols}x{(len(picks) + cols - 1) // cols}:padding=4", "-frames:v", "1",
                str(out / "sheet.jpg")], check=True)
print(out / "sheet.jpg", len(picks), "frames")
for m in caps:
    print(round(m["t"] - markers[0]["t"], 1), m.get("title") or m.get("error") or m.get("q"))
