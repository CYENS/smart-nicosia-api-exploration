# Dangerous buildings and the DLS 3D city

Research notes for the two data sets added to the chatbot on 20 Oct 2026: the dangerous-building
registers of Cyprus's District Local Government Organisations (ΕΟΑ, Επαρχιακοί Οργανισμοί
Αυτοδιοίκησης) and the Department of Lands and Surveys (DLS, Τμήμα Κτηματολογίου και
Χωρομετρίας) 3D buildings model. Code: [chatbot/eoa.py](../chatbot/eoa.py) and
[chatbot/dls.py](../chatbot/dls.py).

## What the registers are

Since the 2024 local-government reform the five EOAs handle dangerous buildings under the Streets
and Buildings Regulation Law. Article 15B declares a building **dangerous** (επικίνδυνη);
article 15A says it **needs repair** (χρήζει επισκευής). A listing is a case on a public
register, not an engineering verdict that a building will collapse.

Each EOA publishes in its own way. None offers an API or a download, so each needs its own reader.

| EOA | Where | Format | Records (20 Oct 2026) | How a record is placed |
| --- | --- | --- | --- | --- |
| Nicosia (ndlgo.org.cy) | [map page](https://ndlgo.org.cy/wp-content/uploads/2026/07/EOA_Xartis_Epikindynes_Oikodomes.html), [register page](https://ndlgo.org.cy/wp-content/uploads/2026/07/EOA_Landing_Epikindynes_Oikodomes.html) | Self-contained HTML; the records are a JSON literal (`const BUILDINGS`), coordinates in `const COORDS` (later literals override earlier ones) | 711 (656 dangerous, 55 needs repair) | Hand-placed coordinates, plus the DLS parcel `OBJECTID` in the notes field |
| Limassol (eoalemesos.org.cy) | [WebGIS](https://experience.arcgis.com/experience/e367fdbcfe1949968c07246f577b472a/) | ArcGIS Online feature service `EOA_WebGIS_EXT_v1`, layer 13 "ΤΕΜΑΧΙΑ ΕΠΙΚΙΝΔΥΝΩΝ ΟΙΚΟΔΟΜΩΝ" (polygons; layer 14 is the same as points) | 163 (all dangerous), on 145 distinct plots | The plot polygon itself |
| Larnaca (eoal.org.cy) | [table](https://eoal.org.cy/epikindyna-ktiria/) | WordPress plugin "dangerous-buildings-manager": the page posts `action=dbm_load_buildings` with a public nonce to `admin-ajax.php`, 10 rows a page | 20 (all declared dangerous) | Sheet/plan and parcel only (no block, no coordinates) |
| Famagusta (eoaa.org.cy) | [tables](https://eoaa.org.cy/%CE%B5%CF%80%CE%B9%CE%BA%CE%AF%CE%BD%CE%B4%CF%85%CE%BD%CE%B5%CF%82-%CE%BF%CE%B9%CE%BA%CE%BF%CE%B4%CE%BF%CE%BC%CE%AD%CF%82-%CE%B5%CF%80%CE%B1%CF%81%CF%87%CE%AF%CE%B1%CF%82-%CE%B1%CE%BC%CE%BC%CE%BF/) | Two HTML tables, one per article (15A, 15B) | 31 (26 dangerous, 5 needs repair) | Sheet/plan, block and parcel |
| Paphos | none found | | | |

Larnaca's table also carries the declaration date, a short description of the danger, whether it
threatens users only or passers-by too, instructions to the public, and whether the building is
listed (διατηρητέα). The press reports much larger "potentially dangerous" counts (889 and 1,101
for Larnaca, about 1,100 for Limassol); those are inspection lists, not the published registers.

## What DLS publishes

All of these are public ArcGIS REST services that answer anonymous `query` requests. Only the
view and query services are used. The INSPIRE *download* (WFS) services are the paid ones and
are never called.

| Layer | URL | What it holds |
| --- | --- | --- |
| Parcels | `eservices.dls.moi.gov.cy/arcgis/rest/services/National/CadastralMap_EN/MapServer/0` | 1.6 million plots: `SHEET`, `PLAN_NBR`, `BLCK_CODE`, `PARCEL_NBR`, `DIST_CODE` (1 Nicosia, 3 Famagusta, 4 Larnaca, 5 Limassol, 6 Paphos). Coordinates carry a Z value. |
| **3D buildings** | `eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE/BU_Buildings/MapServer/6` ("BU3.Building_All_Footprints") | 564,454 footprints island-wide: `H_ROOF_A_1` roof height above ground (m), `H_EAVE_B_1` eave height, `H_MSL_m_1` ground height above sea level, `FLOOR_QTY`, `GND_FLOOR`, `ROOF_TYPE`, `B_TYPE` (use), `VOL_CubicM`, `AREA_sqm`. 52,397 in greater Nicosia. |
| Cadastral buildings | `.../CadastralMap_EN/MapServer/28` | 458,120 surveyed outlines, no heights. Finer than the 3D model in the old town (1,877 against 120 in one block of the walled city). |
| INSPIRE buildings | `.../INSPIRE/BU_Buildings/MapServer/1, 2, 4` | Surveyed buildings (with a height code and floor number) and landmark areas. |
| INSPIRE addresses | `.../INSPIRE/AD_Addresses/MapServer/0` | 80,934 address points (28,681 in greater Nicosia), with tables for street names, numbers and an address-to-parcel link. Not used yet. |

The 3D model is LoD1: one height per footprint. In the walled city DLS models whole blocks as a
single building (median 686 m² there), so a block's roof height is its tallest part.

## How the platform links them

1. Read each register (live once a day; on failure, the last good copy; failing that, the seed
   in `data/eoa_registers_seed.json`, captured on 20 Oct 2026).
2. Resolve each record to its DLS parcel: Nicosia by `OBJECTID`, Limassol from its own polygon,
   Larnaca and Famagusta by sheet/plan/parcel (and block) within the district. Greek `Ε` in plan
   codes becomes Latin `E`; resurvey plans (`2-290-379`) are on sheet `0`.
3. Place each record: Nicosia's published point, otherwise the parcel's centre.
4. Find the 3D buildings on each parcel, from z16 tiles of the 3D model cached on disk:
   - **on the plot**: the footprint's centre is inside it (562 records);
   - **spanning plots**: one footprint up to 3x the plot's size covers the plot's centre (53);
   - **old-town block**: a footprint over 3x the plot's size. The plot is drawn at the block's
     eave height and its floors are unknown, flagged `height_estimated` (170);
   - **at the edge**: a centre within 2 m of the boundary (25);
   - **none**: an empty, demolished or unmodelled plot (111).

Result on 20 Oct 2026: 925 records, 921 placed, 810 with a 3D building, 163 at five floors or
more, the tallest 44.7 m and 16 floors (Stasinou 44, Strovolos).

## Things to know

- **Nicosia's site asks for a human check (CAPTCHA) after a few quick requests from one
  address.** It did during this research. The reader detects the check, never tries to get past
  it, and falls back to the cached copy or the seed. Keep live reads to one a day.
- **Four records can't be placed** because their parcel reference isn't in the cadastre as
  published: LAR-184 (plan `63Ε1` parcel 2656; the parcel exists on `63E2`), LAR-205 and LAR-69
  (both `1-2565-3670` parcel 119, which isn't on that plan) and FAM-15B-25 (`2-281-374` block 7
  parcel 53). Official identifiers are not corrected automatically; these are reported as "not on
  the map", not as errors.
- Nicosia's markers were placed by hand and sit 10-20 m from the building; the parcel outline
  is exact. Small urban plots are about 9 m in radius, so a marker can fall on the neighbour.
- Limassol lists some buildings more than once (163 records on 145 plots); the map draws each
  building once.
- Requests to DLS are throttled (4 a second at most, from a pool of 4) and identify the app.
  The first registry build takes about 2 minutes; after that the tile cache makes it seconds.

## Sources

- [ΕΟΑ Λευκωσίας: digital register and interactive map](https://www.cna.org.cy/article/10681330/eoa-lefkosias-se-leitoyrgia-psifiako-mitroo-kai-diadrastikos-chartis-epikindynon-oikodomon) (CNA, July 2026)
- [Larnaca: 335 classified, where the public list is](https://cyprusnews.eu/editors-choice/larnaka-epikindynes-oikodomes-335-katalogos/)
- [ΕΟΑ Λάρνακας: the real data and what it inherited](https://eoal.org.cy/epikindynes-oikodomes-ta-pragmatika-dedomena-kai-i-katastasi-pou-parelave-o-eoa-larnakas/)
- [ΕΟΑ Αμμοχώστου re-evaluates its dangerous buildings](https://www.politis.com.cy/politis-news/koinonia/998854/eoa-ammokhwstoy-epanaksiologhi-tis-epikindines-oikodomes)
- [Limassol: 60 buildings need evacuation](https://cyprusnews.eu/editors-choice/lemesos-60-oikodomes-chrizoun-ekkenosis-mitroo-1100/)
- [DLS ArcGIS REST services](https://eservices.dls.moi.gov.cy/arcgis/rest/services) and [INSPIRE services](https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE)
