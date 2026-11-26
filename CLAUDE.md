# Nicosia digital twin: notes for agents

A digital twin of Nicosia: a FastAPI backend (`chatbot/`) with a Claude agent and its tools, a
React control-room page (`chatbot/web/`, built into `chatbot/static/`), and a data collector
(`collector/`) that records the Smart Nicosia API into DuckDB. Start with the
[README](README.md), then [docs/digital-twin-roadmap.md](docs/digital-twin-roadmap.md) for
what is being built next and in what order.

## Commands

```bash
uv run pytest                     # all Python tests (offline, fixtures in tests/fixtures)
uv run chatbot                    # http://127.0.0.1:8000 (CHATBOT_PORT to change)
cd chatbot/web && npm ci          # once per checkout or worktree
npm run dev                       # Vite on 5180, proxies /api to CHATBOT_URL (default :8000)
npm run typecheck && npm run lint
npm run build                     # writes chatbot/static (committed, see below)
```

## How the code is laid out

- `chatbot/agent.py` streams a turn, runs tools in parallel and loops. History is append-only.
- `chatbot/prompt.py` is the system prompt. It must stay byte-for-byte stable within a
  deployment (prompt cache); anything per turn goes in the user turn's `<context>` block
  (`format_context` in `agent.py`).
- `chatbot/tools/` holds every tool, one module per domain (`city`, `places`, `routes`,
  `buildings`, `maps`, `charts`, `memory`, `entities`): `@tool(name, description, schema)` on
  a handler registers it. `registry.py` puts the domains in order (the prompt cache keys on it:
  add a domain at the end of `DOMAINS`) and runs them. Data tools return compact JSON and park big
  results in the session store (`layer_ref` for map layers, `data_ref` for chart rows). UI tools
  push a command to the page through `ctx.emit`.
- `chatbot/routes/` holds the API, one router per area; add a module and list it in
  `routers()`. `chatbot/server.py` keeps the app, the password guard and the page.
- `chatbot/entities.py` is the twin's entity model: every kind in one shape (state, readings,
  attributes, relations, sources) plus a 24-hour series and search, for the inspector and
  `get_entity`. A new kind registers a `Kind` resolver.
- `chatbot/moments.py` is the city at a past moment, for the city clock: every device of a kind
  replayed from the twin's memory (only the memory, never the city API), six hours at a time
  and cached. `?at=` on `/api/overview`, `/api/lamps` and `/api/entities/{id}` reads it, and
  `set_city_time` too. A moment the memory holds nothing for is unknown, never a guess.
- `chatbot/mobility/` is live mobility: the bus timetables and live feed, departures and
  trips, and road speeds against free flow ([docs/live-mobility.md](docs/live-mobility.md)). One
  `Mobility` on the Agent shares each feed's fetch between pages, tools and the inspector.
- `collector/interpret.py` is the one place that turns raw readings into meaning (a lamp is on
  only if it reported in the last 70 minutes, and so on). Reuse it; never reinterpret elsewhere.
- `chatbot/web/src/lib/map/engine.ts` draws everything on the map; `lib/chat.ts` handles the
  stream and the stage; `lib/inspector.ts` the open thing; `lib/deeplink.ts` the URL;
  `lib/clock.ts` the city clock (the moment the whole twin shows, ranges, playback, compare);
  `components/shell/` holds the calm layout (the ask bar, the topic chips and card, the now line,
  the map's controls and the Layers sheet); `components/` also holds the chat, stage, inspector
  and the clock. A new
  live layer, readout or chart follows the clock: read `useClock` (or `?at=` on the server)
  rather than assuming now.
  `TOOL_LABELS` in `lib/chat.ts` names each tool in the trace: add new tools there.
- `chatbot/web/src/lib/registries/` is where page features plug in instead of editing shared
  files: workspaces (opened from the Layers sheet, Ctrl K and Alt and a number), topics (the
  chips, four at most, and sections on their cards), the Layers sheet's system layer rows,
  inspector sections per kind, search providers (Ctrl K) and map click handlers. A new readout
  goes into a topic card or a Layers sheet row, not across the top: the default screen stays calm.

## House rules for features

- **Answers are visuals.** Every new data source gets a chat tool that returns a `layer_ref`
  and a `data_ref`, a map style that shows it well, and lines in the system prompt on which
  visual fits which question. Map switches are a fallback for browsing without a key.
- **Every number comes from a tool.** Charts are filled from a `data_ref` by the server.
- **Say where it comes from.** Every new data source gets an entry in `chatbot/sources.py`
  (publisher, licence, link, what it gives, a short map credit, and a freshness probe in
  `chatbot/routes/sources.py` if it is a live feed), and every tool that reads it lists it in
  `TOOL_SOURCES`, so its layers, charts and the data sources panel name it.
- **Linking.** Rows that carry an `id` (device, building, entity) link charts to the map;
  temporal x axes follow the time line. Keep new datasets linkable.
- **Be gentle with public services.** Cache (in `data/chatbot_cache/`), throttle, send the
  `user_agent` from settings, and never call Overpass per request. The city API budget is in
  the README.
- **Tests run offline**, on fixtures. Add fixtures for every new source.
- Every handler validates its input against its schema (done by `run_tool`) and raises
  `ToolError` for problems the model can fix.

## Design language and UX

- Dark control-room chrome: night `#0e1828` base, console `#15233a` panels, ink `#e4ebf4` and
  `#8798b0`, sodium `#ffae45` for anything live, now or selected, signal `#ff5a4e` for alarms
  and danger. Tokens are in `chatbot/web/src/index.css`; use them, not raw colours. Data
  layers keep their own colours (`STATE_COLORS` in `chatbot/store.py`).
- Type: Sofia Sans for reading, Sofia Sans Condensed (`font-condensed`, `font-readout`) for
  readouts, tabular figures. No monospace labels, no all-caps labels.
- The map is the hero. Panels float, can be hidden, and give the camera its padding
  (`lib/geometry.ts`, `lib/layout.ts`).
- Components are shadcn/ui, style base-nova on **Base UI** (use the `render` prop, not
  `asChild`), lucide icons. Look components up with the shadcn MCP server or skill and add them
  with `npx shadcn@latest add <name>` from `chatbot/web`; compose them rather than hand-rolling.
- **Interactions feel standard and look obvious.** Find each control and its canonical example
  through the shadcn MCP (`search_items_in_registries`, `get_item_examples_from_registries`,
  `view_items_in_registries`) and run `get_audit_checklist` when done; never hand-roll a list,
  menu, slider, toggle or tabs that shadcn has. Hover, focus, pressed and selected states must
  read at a glance on night, console and popover surfaces, with sodium for selected and
  current. Lists, menus, sliders, toggle groups and tabs follow the WAI-ARIA keyboard patterns
  (arrows inside, Home and End, Enter, Esc, Tab between groups); reuse `src/lib/roving.ts` for
  lists; closing a panel returns focus to whatever opened it. Drive these paths with the
  Playwright MCP and keep screenshots: the owner tries them by hand.
- UX checklist for every screen: keyboard reachable with visible focus; `aria-live` for live
  readouts; skeletons while loading; an empty state that says what to do next; errors that
  say what happened and what to try; works at 375 px wide; honours
  `prefers-reduced-motion`; hover never the only way to reach something.
- **Copy**: sentence case, plain words, the user's verbs. Never an em dash or an en dash in
  anything a user reads (labels, hints, toasts, errors), nor `--` as a stand-in.

## The built page

`chatbot/static/` is the output of `npm run build` and is committed, so `uv run chatbot` needs
no Node. Hashed file names mean every build conflicts with every other build, so:

- Commit the build once per branch, as the branch's last commit, named "Rebuild the page".
- If a merge (or a rebase before your first push) conflicts inside `chatbot/static/`, never
  edit it by hand: take either side (`git checkout --theirs -- chatbot/static && git add
  chatbot/static`), finish the merge, then `npm run build` and commit the result.

## Stacked pull requests

Features land as one stack of pull requests on `CYENS/smart-nicosia-api-exploration`, each based
on the one before it. Never merge a pull request and never push to `main`.

1. Find the stack tip: the open pull request whose head branch no other open pull request uses
   as its base.

   ```bash
   gh pr list --state open --json number,headRefName,baseRefName \
     --jq '(map(.baseRefName)) as $b | map(select(.headRefName as $h | $b | index($h) | not))'
   ```

2. Start your branch at the tip. **Push early and often**: push after every commit that works,
   and after the first real commit open your pull request as a draft with `--base <tip>`, so
   the owner can follow the work.
3. Never rewrite pushed history (auto mode refuses force pushes). To pick up changes from the
   tip or from a pull request below yours, merge it in: `git fetch origin && git merge
   origin/<branch>`. A rebase is fine only before your first push.
4. When the feature is done: find the tip again, merge it in and `gh pr edit --base <tip>` if it
   moved, rebuild the page as the last commit, run the tests, push, and `gh pr ready`. Body
   sections, in order: what changes for someone using it; how it is built; review it (a URL,
   numbered steps, what to expect); tested; known limits. Mention the pull request it stacks
   on.
5. When a pull request lower in the stack changes, the ones above it merge it in, top to
   bottom in order, each rebuilding the page as its last commit.

## Ports for parallel work

Two features can be in progress at once, each in its own worktree. Use your lane's ports so the
running copies don't collide with each other or with the main checkout (8000 and 5180):

| Lane | Backend | Page (Vite) | Launch configs |
| --- | --- | --- | --- |
| A | 8110 | 5210 | `lane-a-api`, `lane-a-web` |
| B | 8120 | 5220 | `lane-b-api`, `lane-b-web` |
| C | 8130 | 5230 | `lane-c-api`, `lane-c-web` |
| D | 8140 | 5240 | `lane-d-api`, `lane-d-web` |

Lanes C and D are for when A and B are still serving finished features for review. The whole
stack is served for review on 8300 from its own worktree.

A new worktree has no `.env` and no caches. If they are missing, copy `.env` and
`data/chatbot_cache/` from the main checkout (`.worktreeinclude` lists them), then `npm ci` in
`chatbot/web`. Questions to the chatbot spend the owner's Claude API credit: check features
with a handful of questions, not dozens.
