# The environment twin

Feature 5 of the [roadmap](digital-twin-roadmap.md): the ground, the sun and the shade, the air
and the noise as surfaces, and the weather you can see. Server code in `chatbot/environment/`,
page code in `chatbot/web/src/lib/environment/` and `chatbot/web/src/components/environment/`.

## Where it shows

- **The Layers sheet's Environment group**: terrain (with relief 1, 1.5 or 3 times), shadows,
  sun hours, the air and noise surface (PM2.5, NO₂, PM10, O₃, the stations' index, noise or
  temperature), wind and rain radar. Each row has its switch, a line on what it shows now and,
  while on, its legend or controls.
- **The Outside card**: the sun and the wind at the clock's moment, the shadows, wind, air and
  rain one press away, "What's it like here?", and "Open Environment" to go deeper.
- **The Environment workspace** (Alt 3, Ctrl K, the Layers sheet): the sun now, the same rows,
  and the sun hours of a square or the map's centre on any date, with a chart of its day.
- **The inspector, for a spot**: any point as `spot-<lat>-<lon>` (five decimals). Sun or shade,
  the hours of direct sun that day (quarter by quarter), the air and noise with how sure they
  are, the temperature and the wind, at the clock's moment.
- **The chat**: `sun_and_shade` (a moment or a whole day), `environment_surface` (a metric or
  the wind) and `plan_route` with `prefer: ["shade"]`.
- **Measuring stations** ([measured weather and reference air](measured-weather-air.md)): the
  Met Department's weather stations and the national network's two air stations, as a Layers
  row, the Outside card's "Measured" readout, two kinds in the inspector (`met-LEFKOSIA`,
  `aq-CY0004A`), and the chat's `measured_stations` and `compare_with_reference`.

## How it works

- **Sun**: SunCalc's formulas (`sun.py`), the same the page uses for its sky, so the server's
  shade and the map's shadows agree.
- **Shade** (`shade.py`): the DLS buildings rasterised to a height grid of about 2 m cells
  (Web Mercator zoom 16, the DLS's own tiling), with 160 m of margin so buildings outside still
  cast their shadows in. A cell is in shade when a building rises above the sun's ray towards
  it. Sun hours count 96 quarter hours. A walk is read point by point at the moment the walker
  passes. Only buildings cast shade: no trees, awnings or arcades, and the ground is flat.
- **The map's shadows** are drawn on the page from the 3D city's buildings as they load: each
  footprint swept away from the sun by its height, filled one building at a time and softened
  once. They follow the city clock.
- **Surfaces** (`surfaces.py`): the stations laid over the Open-Meteo model (CAMS for the air,
  corrected by the stations' mean miss). Each station keeps its own reading; between stations
  the miss is spread by inverse distance; how sure the estimate is (1 at a station, 0 beyond
  the metric's reach) sets how much of it is kept, and the page draws that as opacity. The
  uncertainty is the sensors' error plus the model's miss away from the stations, so a place far
  from every station is never surer than a station. Noise and the stations' index exist only
  near the stations and fade out between them. Official stations are ground truth: the model's
  miss is theirs alone when there is one, each station carries its own error (an instrument's
  small, a city sensor's larger), and the temperature rests on the Met thermometers, a city
  sensor joining only once its bias against them is measured. An official air station anchors
  only its own hour or the next, so live it is usually left out (its files run two to three
  hours behind) and the legend says so.
- **Wind**: Open-Meteo's 10 m wind on a 5 by 5 lattice about 55 km across, drawn as streaks
  that drift with it (still arrows with reduced motion).
- **Rain**: RainViewer's radar frames of the last two hours; the frame nearest the clock's
  moment is drawn.
- **Terrain**: the terrain model the Department of Lands and Surveys made from its 2019 LiDAR
  flight (a 1 m grid, CC BY 4.0), as Mapterhorn's open height tiles (keyless), raised and
  hillshaded, lit from where the sun is at the clock's moment. North of the buffer zone the
  tiles are filled from Copernicus GLO-30 (30 m).

## Being gentle

Open-Meteo is asked once per grid and local day (an hour's cache for today), the radar once in
five minutes, a surface is kept a minute live and an hour for the past, and the DLS tiles are
rasterised once and kept.
