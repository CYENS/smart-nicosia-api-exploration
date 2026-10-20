# Chatbot client

The Nicosia digital twin's page: a control room for the city. React, Vite, Tailwind 4 and
shadcn/ui (base-nova, Base UI primitives), with MapLibre for the map and Vega-Lite for charts.

The built page lives in `../static` and is committed, so `uv run chatbot` serves it without Node.
Rebuild it after changing anything here:

```bash
npm install
npm run build
```

For live editing, run the chatbot (port 8000, or set `CHATBOT_URL`) and the Vite dev server,
which proxies `/api` to it:

```bash
npm run dev
```

## Where things are

- `src/lib/map/engine.ts`: the map and everything the assistant draws on it (layers, the DLS 3D
  city, the registers, time-lapses, linked highlights, callouts).
- `src/lib/map/basemap.ts`: the Night base map (OpenFreeMap dark, recoloured) and Day (liberty).
- `src/lib/map/sky.ts`: the real sun and moon over Nicosia (SunCalc), and the light, sky colours
  and dusk/dawn switch the map takes from them.
- `src/lib/chat.ts`: the conversation stream from `/api/chat` and the stage's contents.
- `src/lib/charts.ts`, `src/lib/visual.ts`: charts and the sandbox for custom visuals.
- `src/components/`: the strip, the conversation, the stage, the map panel and the time-lapse.
- `src/index.css`: the design tokens. Night `#0e1828`, console `#15233a`, ink `#e4ebf4`,
  sodium `#ffae45` for anything live or in focus, signal `#ff5a4e` for alarms and danger.

Add shadcn components with `npx shadcn@latest add <name>` (the repo's `.mcp.json` also offers
the shadcn MCP server).
