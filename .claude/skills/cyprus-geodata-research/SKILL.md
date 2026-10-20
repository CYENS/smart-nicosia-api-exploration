---
name: cyprus-geodata-research
description: Research and integrate Cyprus public geodata (DLS cadastre, 3D buildings and INSPIRE layers, EOA dangerous-building registers, municipal ArcGIS WebGIS apps, data.gov.cy). Use when finding, probing or wiring a Cyprus government data source into this platform, or when a register needs parsing or linking to parcels.
---

# Cyprus geodata research

The sources here rarely have APIs or downloads. They are ArcGIS REST services, ArcGIS Online
apps, WordPress tables and self-contained HTML pages, mostly in Greek. This is the playbook that
worked; the findings it produced are in `docs/dangerous-buildings-and-3d.md`.

## Search first, in both languages

- Use WebSearch in `extended` mode for Greek queries and anything from the last few months. Run
  Greek and English queries in the same turn: "ΕΟΑ Λάρνακας επικίνδυνες οικοδομές κατάλογος" finds
  what "Larnaca dangerous buildings list" does not.
- News articles give numbers; the authority's own page gives the data. Always follow through to
  the publisher's page, and treat press counts ("889 potentially dangerous") as a different thing
  from a published register.
- If Firecrawl or Tavily is connected, prefer it for JS-rendered pages. Otherwise `curl` for
  JSON and HTML, and the built-in browser or Playwright only when a page builds itself in JS.
- For library questions (MapLibre, FastAPI, Vega-Lite), use Context7, not web search.

## Recipes

**ArcGIS REST (DLS, municipalities).** Append `?f=json` to the root, a folder, a service and a
layer to list what is there; folder listings can be 403 while the services inside answer. Then:
`/query?where=1=1&returnCountOnly=true&f=json` for the size,
`outStatistics` with `groupByFieldsForStatistics` for distributions, `f=geojson&outSR=4326` for
geometry. Page with `resultOffset`/`resultRecordCount` and `orderByFields=OBJECTID` (max 1000).
POST long `where` clauses and polygons; send Esri JSON rings, not GeoJSON, as a query geometry.

**ArcGIS Online apps.** Take the item id from the app URL, read
`https://www.arcgis.com/sharing/rest/content/items/<id>/data?f=json`, and pull out the
`FeatureServer` URLs and web map item ids. The layers usually answer anonymous queries.

**WordPress tables.** Find the plugin's script in the page (`wp-content/plugins/<name>/...js`),
read its `admin-ajax.php` action and the nonce printed in the page (`var <x>_ajax = {...}`), and
post the same form the page posts. Read-only, one page at a time.

**Self-contained HTML maps.** Look for `const NAME = [...]` literals and parse them with
`json.JSONDecoder().raw_decode(page, index)`. Never evaluate the JavaScript. Later literals of the
same name override earlier ones.

## Etiquette (non-negotiable)

- Identify the app in the User-Agent, throttle (DLS: 4 requests a second at most), cache
  everything on disk, and refresh registers once a day.
- Never call the INSPIRE download (WFS, `InspireFeatureDownload`) services: they are the paid
  ones. The view and query services are free.
- If a site answers with a CAPTCHA or "user validation required" page, stop. Do not retry, do
  not try another user agent, do not try to get past it. Fall back to a cached copy and tell the
  user. `ndlgo.org.cy` does this after a few quick requests from one address.
- Don't "fix" official identifiers. A parcel reference that isn't in the cadastre is reported as
  not placed, never silently corrected and never called an error by the authority.

## Data quirks

- Registers type Greek capitals where DLS stores Latin letters (`63Ε1` for `63E1`); translate
  `ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ` to Latin before querying (`chatbot.dls.plan_code`).
- Resurvey plans look like `2-290-379` and always sit on sheet `0`.
- DLS district codes: 1 Nicosia, 2 Kyrenia, 3 Famagusta, 4 Larnaca, 5 Limassol, 6 Paphos.
- Parcel geometry comes back with Z values; drop them. Compute areas and centres in local
  metres, never in raw degrees (plots are about 20 m across).
- The 3D model is LoD1 and, inside Nicosia's walls, block-level: a block's roof height is its
  tallest part. Treat a footprint more than 3x a plot's size as a block.
- Match place names across scripts and spellings with `chatbot.geo.loose_match` ("Strovolos" and
  "ΣΤΡΟΒΟΛΟΣ", "Ayios" and "ΑΓΙΟΣ").

## In this repo (Windows)

- There is no `python` on PATH. Use `.venv/Scripts/python.exe -I` (or `uv run`), and call
  `sys.stdout.reconfigure(encoding="utf-8")` before printing Greek.
- Keep probe scripts and downloads in the session scratchpad, not the repo.
- Add a fixture trimmed from a real capture for every new parser (`tests/fixtures/eoa_*`), and
  test against a fake DLS (`tests/test_dls_eoa.py`), never the network.
