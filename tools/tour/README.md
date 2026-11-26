# The showcase video

A captioned, three minute tour of the twin, recorded from a running copy on live data and cut
into a 1080p video. Re-record it when features land, so the video shows what the twin does now.

- `tour.py` drives the page in headless Chromium (on the GPU) and records it with the CDP
  screencast: frames, captions and the stretches to speed up while the assistant answers.
- `build.py` cuts one or more recorded runs into the video: the waits play 8 times faster and
  say so, a caption band runs under the page, and a title card and an end card with the data
  sources frame it.
- `peek.py` makes a contact sheet of a run, to check it before cutting.

## Before you record

- The twin running with a key and a filled memory: `uv run chatbot` on port 8000, after
  `collector backfill` (see [docs/twin-memory.md](../../docs/twin-memory.md)). The clock scene
  goes back to 21:00 last night, so the memory needs at least the last day.
- Chromium for Playwright, once: `uvx playwright install chromium`.
- ffmpeg: `brew install ffmpeg`.
- The page's fonts for the cards: `npm ci` in `chatbot/web` (without them the cards use the
  system's sans).

## Record and cut

Record in two runs, so the scenes that cost nothing can be redone freely:

```bash
uv run tools/tour/tour.py data/tour/a --only=live,mobility,inspect,clock --no-questions
uv run tools/tour/tour.py data/tour/b --only=traffic,memory,replay,trip
python3 tools/tour/peek.py data/tour/b 1.5,5
uv run tools/tour/build.py data/tour/nicosia-twin-tour.mp4 data/tour/a data/tour/b
```

The second run asks the assistant four questions, one per scene, which spends API credit. Use
`--no-questions` while trying changes to the scenes. Each run starts a fresh page, and
`data/tour/` is ignored by git.

Options: `--base` for another address (or `TOUR_BASE`), `--chrome` for a Chrome of your own (or
`TOUR_CHROME`), `--fast` for how much faster the waits play.

## The scenes

| Scene | What it shows |
| --- | --- |
| `live` | The strip's readouts, with a tooltip |
| `mobility` | The Mobility workspace (Alt 2): a line, one of its buses, a stop's departure board |
| `inspect` | Ctrl K by register file number, a street light with its 24 hours, a neighbour |
| `clock` | The clock scrubbed to 21:00 last night with the keyboard, then the swipe against last week |
| `traffic` | A question about Ledras Street; hovering the chart replays the map |
| `memory` | Parking on Makariou against the usual for the hour |
| `replay` | Last night's street lights replayed on the clock |
| `trip` | A bus trip from Eleftheria Square to the Cyprus Museum |

A scene clicks only inside the inspector and the workspace panels, never on the conversation's
suggested questions (a stray click there asks the assistant). A scene that fails is logged in the
run's `markers.json` as an error and the tour moves on; check with `peek.py` before cutting.
