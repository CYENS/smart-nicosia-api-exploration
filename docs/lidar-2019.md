# The city as the 2019 LiDAR flight measured it

In 2019 the Department of Lands and Surveys (DLS) flew a laser scanner over the
government-controlled part of Cyprus. Two of its products are open files: the **surface model**
(everything the laser saw from above: roofs, trees, the ground between them) and the **terrain
model** (the bare ground). The twin's terrain already comes from the terrain model
([#38](https://github.com/CYENS/smart-nicosia-api-exploration/pull/38)). This is what the
surface model adds, and how.

## What someone gets

- **What changed between 2014 and 2019.** The DLS 3D buildings were surveyed from the 2014
  aerial photographs. The flight measured the same footprints five years later. Ask "what was
  knocked down around Eleftheria Square since 2014?" and the assistant answers with the
  buildings in 3D (taller in green, lower in amber, gone in red at the height they had) and a
  linked chart (`find_height_changes`). The same layer is a row in the Layers sheet, under
  Buildings: **Changed since 2014**.
- **Measured heights on every building's card**: the top and the eave as measured in 2019
  beside the surveyed height, how much of the footprint is built on, and one line on what
  changed. `get_buildings_3d` rows carry `height_2019_m` too.
- **The flight as a data source** in the data sources panel and the map's credits.

In greater Nicosia, south of the buffer zone: 74,550 of 75,658 DLS buildings measured. 71,760
are the same to within a floor, 1,167 are taller, 1,500 are gone and 44 are lower. Of those of
house size (50 m2 and up), which is what the map and the answers show unless asked otherwise:
770 taller, 394 gone, 42 lower.

## The data

| | |
| --- | --- |
| Publisher | Department of Lands and Surveys, Republic of Cyprus |
| Where | `https://eservices.dls.moi.gov.cy/inspire_downloads/EL/rasters/2019_DSM/` and `2019_DTM/`, linked from the [elevation record](https://www.data.gov.cy/el/dataset/ypsometria-inspire) of the national open data portal |
| Licence | CC BY 4.0, as that record states |
| Form | GeoTIFF sheets of 2.5 km, 32-bit heights above sea level, LZW, with their position in their own tags |
| Grid | Longitude and latitude on ETRS89. Surface model: 4.99e-6 degrees a cell, about 0.45 m by 0.55 m at Nicosia (four cells a square metre). Terrain model: twice that |
| Gaps | A cell nobody measured holds the lowest 32-bit float. About 1% of a sheet, at its edges |
| Names | `ETRS89_<x>_<y>.tif`, the sheet's south-west corner on the national grid (EPSG:6312) in hundreds of metres |
| Size | Greater Nicosia is 27 sheets of each: 0.9 GB of surface model, 0.2 GB of terrain model |
| Reach | The whole flight, not only "the city areas"; nothing north of the buffer zone |

These are plain files behind a public record. They are not the INSPIRE WFS download services,
which the twin still keeps away from.

## Running it

The sheets are heavy, so they stay out of git, in `data/dls_lidar/` (or wherever
`DLS_LIDAR_DIR` points). What the twin reads is two small files made from them, which are in
git, so a deployment needs no sheet.

```bash
uv run python -m chatbot.lidar fetch      # the 54 sheets for greater Nicosia, once (1.2 GB)
uv run python -m chatbot.lidar measure    # every building, into data/lidar_*_2019.json.gz
```

- `fetch` reads the two folder listings, then downloads one sheet at a time with a second's
  pause, skips what is already on disk at its listed size, and stops at the first answer that is
  not a sheet (a web page, a wrong size). `--bbox lon_min,lat_min,lon_max,lat_max` for another
  place.
- `measure` reads the DLS buildings of the box (from the twin's tile cache, asking DLS only for
  tiles it lacks), measures them a sheet at a time (about 2.5 s a sheet), and writes
  `data/lidar_buildings_2019.json.gz` (0.7 MB: every measured building) and
  `data/lidar_changes_2019.json.gz` (0.2 MB: the changed ones with their footprints). The same
  heights make the same bytes, so a rerun changes the files only when a height changed.

## How a building is measured

For each footprint: the surface model's cells whose centres fall inside it (even-odd over every
ring, so courtyards are out), each minus the terrain model under it. That gives a few hundred to
a few thousand heights above the ground per building, and from them:

| Kept | What it is |
| --- | --- |
| `top` | The 95th percentile. This is the survey's roof height: on 16,500 buildings the median difference is 0.1 m, two in three are within 1 m and four in five within 2 m |
| `eave` | The 10th percentile, which is the survey's eave height (median difference 0.1 m) |
| `mid`, `upper` | The median and the 75th percentile: how much of the roof is up there |
| `peak` | The single highest cell: a spire, a stair tower, a mast |
| `built` | The share of the footprint with something standing more than 1.5 m above the ground |
| `cover` | The share of the footprint the laser has a value for |
| `ground` | The terrain model under it. It agrees with the survey's ground height (median difference 0.2 m) |

## 2014 against 2019

The survey gives one height for a building and the laser thousands, so the comparison is
cautious (`chatbot.lidar.compare`):

- **Gone**: less than 15% of the footprint still has anything standing on it.
- **Taller**: the measured top is a floor (3 m) or more above the surveyed roof, and a quarter
  of the footprint is above the old roof. A tree leaning over a house is not an extra floor.
- **Lower**: even the highest cell is a floor or more below the surveyed roof. A church is not
  lower for having a spire that is one part in a hundred of its footprint.
- **A footprint under 30 m2** (a shed, a garage) can only be gone or the same: a tree over a
  shed and a taller shed look alike from above.
- **Unknown**: less than 60% of the footprint was measured.

## Known limits

- **New buildings on empty ground are not in it.** There is no footprint to measure. Finding
  them needs the surface model searched outside the footprints, and trees told from roofs.
- **A measured top counts whatever stands on the roof**: a water tank, a mast, a crane on a
  site, a tree that covers a quarter of a small roof.
- **Inside the walls DLS models whole blocks as one building**, so a block's measurement mixes
  its houses and courtyards. The cadastral house outlines of #31 are what would split them.
- **The 3D city is still drawn at the 2014 heights.** The measured heights are on the cards, in
  the rows and in the change layer; drawing the city from them belongs with roof shapes.
- **Nothing north of the buffer zone was flown.**
- **Two dates only.** 2014 and 2019; a building that went in 2020 still stands here.

## What comes next from the same sheets

Roof planes per building (pitch and ridge), the old town's houses at their own heights once
#31's outlines are in, trees as measured crowns, and buildings that are new since 2014. See
step 3 of [the 3D plan](3d-twin-research.md).
