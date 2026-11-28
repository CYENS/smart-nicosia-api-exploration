# iNicosia Frontier Digital Twin Research (2026 Empirical Audit)

*Researched 2026-11-27. Strictly empirical: every URL, status code, schema, Nicosia record count, update timestamp, and licence claim below was observed directly via live HTTP probes on `2026-11-27` or verified in the repository codebase on `origin/main` (`81f8b0f`). Unprobed or blocked sources are marked explicitly.*

---

## Executive Summary & Context Boundary

This document extends the twin's prior research foundations without duplicating them or the active build queue on `main`:

- **Already covered elsewhere (not repeated here):** core Smart Nicosia IoT sensors & 82-question catalogue ([digital-twin-research.md](digital-twin-research.md)); Met AWS XML, EEA Parquet air, TSOC `#production_graph_data`, duty pharmacies, Visit Cyprus events, EMSC quakes, Meteoalarm Atom, EFFIS fire WMS, Landsat LST, Kontur H3, `adsb.lol`/ISS, Overture `2026-11-18.0`, and CyNAP traffic core ([next-additions-research.md](next-additions-research.md)); scenario lab contracts `C1`–`C4` and `P1`–`P10` math ([simulation-research.md](simulation-research.md)); 3D Rungs 0–8, `EL/` 1 m LiDAR terrain, and `BU_Buildings` 3D heights ([3d-twin-research.md](3d-twin-research.md)).
- **In-flight on open PRs / branches (treated as existing substrates to build upon):** #30 browser e2e, #31 realistic 3D Nicosia, #34 collectors admin, #36 camera traffic lines, #43 road reports, #45 3D buses, #46 measured weather & reference air, #47 hazards watch, #48 live power, plus active Park/Charge/Hire, Street Rules, Tonight in Nicosia, Heat/People/Slopes/15-min, and Living Sky/ITS work.

### Key Empirical Breakthroughs

1. **All 26 Cyprus DLS INSPIRE MapServers mapped & `/query` mystery solved:** The 9 Map-only services (`PD`, `NZ`, `LU`, `US`, `SU`, `ACMF`, `MR`, `PF`, `SR`) reject REST `/query` and `/identify` (`code 400`) intentionally — **not** because the data is locked or paid. Every one of them works anonymously through ArcGIS `/export`, standard ArcGIS `WMSServer` (`/inspire/services/INSPIRE/<SVC>/MapServer/WMSServer`), Predefined Atom (`serviceatoma1.xml` -> `datasetatoma1.xml`), and direct bulk GML/GeoTIFF files under `https://eservices.dls.moi.gov.cy/inspire_downloads/`. Meanwhile, all 9 `InspireFeatureDownload` services (`AD`, `AU`, `CP`, `GE`, `GN`, `HY`, `LC`, `PS`, `TN`) serve anonymous **OGC WFS 2.0 `GetFeature`** (`200 OK`) in addition to `/query`.
2. **Critical Nicosia truth checks on DLS INSPIRE (avoiding multi-day traps):**
   - `PD_PopulationDistribution` serves **2011 Census** figures (`<gml:beginPosition>2011-10-01</gml:beginPosition>`), NOT 2021; it must be joined with live **CYSTAT PxWeb 2021 Census JSON** (`cystatdb23px.cystat.gov.cy`) to modernize quarter/postal demographics. Also its WMS layers enforce tight `minScale` cutoffs (`9,449`–`20,000`), returning blank PNGs when zoomed out.
   - `US_UtilityAndGovernmentalServices` (`US3_SewerNetwork`, `US4_WaterNetwork`) covers **ONLY Limassol sewers and Larnaka water pipes** — **zero Nicosia underground pipes exist in DLS**. Moreover, all 165 `US1` features in Nicosia (`1000_LEFKOSIA`) contain zero inline facility names or fire hydrants, linking instead via `BU2.BU.*` foreign keys to `BU_Buildings` LandmarkAreas; direct **Fire Stations CSV** and **Hospitals CSV** on `data.gov.cy` are cleaner and already geolocated.
   - `LU_LandUse` `InspireView` WMS `GetCapabilities` crashes with `Java heap space` OOM, and `PS_ProtectedSites` `InspireView` `GetFeatureInfo` throws an ambiguous-namespace error; standard `WMSServer` and `/export` work cleanly as drop-in replacements.
3. **High-value live endpoints unlocked in Part B:**
   - `data.gov.cy` upgraded off CKAN (`/api/3/action/package_show` -> 404) to a single **10.34 MB DCAT-AP JSON-LD catalog** (`/catalog.json`, `1,959` datasets, `4,059` distributions) plus a working **Datastore JSON API** (`/api/action/datastore/search.json?resource_id=<uuid>`) for CSV-backed tables.
   - **Cyprus DoM 2 km WRFv4 forecasts** (`https://dom.org.cy/WRF4/{00Z,12Z}/02/Point_Values/Point_Values_LEFKOSIA.txt`) update twice daily (`~06:31Z`, `~18:31Z`) with 3-hourly Nicosia temperature, RH, wind, precip `RR`, and pressure — while `weather.cyi.ac.cy` Doppler radar GIFs failed DNS completely.
   - **GSD Lefkosia Ground Stability Layer** (`7daa559e26e140799513918468495e5f` on ArcGIS Online) serves 16 anonymous public INSPIRE geohazard polygons across Nicosia — including `69.7 km²` of **`1_ObservedPSI` shrink-swell clay (`confidence: High`)** — compensating for EU Login gating on Copernicus EGMS.
   - **Copernicus Sentinel-2 L2A** via Planetary Computer STAC (`T36SWE`/`T36SWD`, latest `2026-11-25`, `<6%` cloud, anonymous SAS token) + DoE **20 m Tree Type Density GeoTIFF** (`04_Density_TreeTypes.zip`, 9.19 MB) + DoE **Nicosia Strategic Noise Maps** (2017 SHP 8.21 MB + 2022 GPKG 345 MB) + **Cyprus Police WordPress REST API** (`categories=5`, 3,285 road posts).

---

### Ranked Top 10 Frontier Additions for iNicosia

| Rank | Capability / Dataset | Frontier Pillar | Primary Live Verified Sources | Impact on Nicosia Twin | Effort | Depends On / Extends |
|---|---|---|---|---|---|---|
| **1** | **Pedieos Flash-Flood, Drainage & Clay Subsidence Layer** | Part A (`NZ`, `HY`, `EF`) + Part B (`GSD PSI`) | `NZ.gml` (29/14/12 flood polygons), `HY` L1/L7/L10, `EF` `r6-1-2-90` (`Pedhiaios @ Lefkosia`), GSD `7daa559e...` (`1_ObservedPSI`) | Turns rain alerts & road closures into spatial flood-risk & structural clay-heave exposure along the Pedieos corridor | Medium | PR #43, #46, #47 |
| **2** | **Elderly 65+ Heat & Hazard Vulnerability + Zoning Overlay** | Part A (`PD`, `LU`, `CP`) + Part B (CYSTAT 2021) | `PD1_Quarters.gml` (`YT65_`), `SU` quarters, CYSTAT PxWeb `Census 2021`, `LU` `WMSServer` + TPH 2019 KMZ, `CP/L1` (132.7k parcels) | Vienna/Helsinki-style equitable heat/flood triage weighting older residents against heat islands, pharmacy access, & zoning | Medium | Heat/15-min, PR #46 |
| **3** | **Cross-System Compound Incident Engine (`watch/incidents` Cascade)** | Part C.1 | `chatbot/watch/` (`Book`, `LINKS`), Rain/WRF + `NZ` flood + `waze_alerts` + Police `cat=5`, `air_station` PM10 + schools/hospitals | Elevates single-device alarms into causal urban incident graphs (storm+flood+closure; dust+schools; peak grid+streetlights) | Medium | Watch / Incidents on `main`, PR #43, #46–#48 |
| **4** | **Twice-Daily 2 km WRF Forecast + 10 m Live Sentinel-2 NDVI Canopy** | Part B + Part C.4 (`#7`) | `dom.org.cy/WRF4/{00Z,12Z}/02/Point_Values/Point_Values_LEFKOSIA.txt`, Planetary Computer `sentinel-2-l2a` (`B04`/`B08`/`SCL`), `04_Density_TreeTypes.zip` | Official Cyprus NWP forecast w/ rolling backtest + seasonal urban vegetation / drought stress deltas over 20 m tree baseline | Medium | Environment twin, `clock.ts`, PR #46 |
| **5** | **Civic Emergency & Quiet-City Receptors (Hospitals, Fire, Heritage, Noise)** | Part A (`US1`, `PS`) + Part B (`data.gov.cy`) | `fire stations.csv`, `Νοσοκομεία CSV`, `23_Noise_Nicosia_DEN.zip` (`Lden`), Antiquities CSVs, `PS/L1`, `police_stations_f.zip` | Maps critical emergency/protected anchors + official strategic road/urban noise contours for triage & urban-design sandboxes | Low | Sources & inspector registries |
| **6** | **Anthropic Tool Search (`tool_search_tool_bm25`) Past 38 -> 60+ Tools** | Part C.4 (`#13`) | `chatbot/tools/registry.py` (38 tools, 14 domains), `anthropic 1.15.0` `betas=["advanced-tool-use-2025-11-20"]` | Prevents tool-selection accuracy collapse and token bloat as Parts A/B/C add 20+ geospatial, forecast & scenario tools | Low–Med | `chatbot/agent.py`, prompt cache layout |
| **7** | **Ambient Living-City Motion, Sunset Wave & Presenter Mode** | Part C.2 (`#11`) | `sky.ts`, `RainOverlay`, `NowLine`, `clock.ts`, `street_light` metered states, `23_Noise_Nicosia_DEN.zip` | Transforms static lobby screen into a calm living twin (pedestrian pulses, twilight lamp-lighting wave, scripted `?present=1` tour) | Medium | `workspaces.ts`, #31 3D Nicosia |
| **8** | **Interactive Co-Design Sandboxes (Drag-a-Tree & 15-Min Place-a-Service)** | Part C.3 | `scenarios/lab.py` (`C1`–`C4`), `P1`/`P5`/`P6`, `LU` setbacks, Sentinel-2 NDVI, `PD` 65+, `compare-swipe.tsx` | Sub-second interactive what-if co-design on the map with shareable Option A vs Option B split-swipe for municipal workshops | Med–High | `simulation-research.md`, #46, Heat/15-min |
| **9** | **Open Twin Standards: Read-Only OGC SensorThings API v1.1 + FastMCP** | Part C.4 (`#13`) | `collector/db.py` (`Memory`), `chatbot/entities.py`, EU LDT Toolbox / MIMs Plus patterns | Exposes live sensors, incidents & scenarios to municipal QGIS/BI tools and external MCP agents without glue code | Medium | `chatbot/server.py`, `TOOL_DEFS` schemas |
| **10** | **Bilingual EN/EL UX, Voice/Photo Reports & Multi-Worker Persistence Path** | Part C.4 (`#14` + Scale) | `GREEK_TO_LATIN`, Web Speech API, Claude Vision photo triage, `db.GATE` / Postgres-state + DuckDB-analytics split | Unlocks Greek-speaking municipal staff/citizens, field hazard capture, and breaks the single-process DuckDB/RAM ceiling | High | `agent.py`, `memory.py`, `dls.py` |

---

## Part A — Complete Audit of the 26 Cyprus DLS INSPIRE GIS Services

**Root endpoint:** `https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE?f=json` (`200 OK`, 26 MapServers).

### Architectural Mechanics & Gotchas Discovered

Four technical behaviours explain every `400`/`403`/`500` error previously encountered against DLS INSPIRE:

1. **Two distinct ArcGIS-for-INSPIRE deployment profiles exist across the 26 services:**
   - **9 WFS-Backed Interactive Services** (`AD`, `AU`, `CP`, `GE`, `GN`, `HY`, `LC`, `PS`, `TN`): advertise `supportedExtensions: InspireFeatureDownload, InspireView, WMSServer` and `capabilities: Data,Map,Query`. Contrary to informal notes that INSPIRE WFS might be paywalled, anonymous **OGC WFS 2.0 (`MapServer/exts/InspireFeatureDownload/service`) returned `200 OK`** for both `GetCapabilities` and `GetFeature&COUNT=1` (`hy-p:WFDRiver`: 10,596 B; `ad:Address`: 6,238 B). Their Predefined Atom `serviceatoma1.xml` returns `404` and they have no `/inspire_downloads/<THEME>/` static dumps.
   - **17 Predefined-Download Services** (`ACMF`, `AM`, `BR`, `BU`, `EF`, `EL1`, `HB`, `LU`, `MR`, `NZ`, `OI`, `PD`, `PF`, `SD`, `SR`, `SU`, `US`): advertise `InspirePredefinedDatasetDownload, InspireView, WMSServer`. Eight of them (`AM`, `BR`, `BU`, `EF`, `EL1`, `HB`, `OI`, `SD`) also list `Query` in service capabilities, though in practice only `BU` and `EF` execute `/query` (`AM`, `HB`, and `SD` reject `/query` with `code 400: Requested operation is not supported by this service`). The remaining **9 Map-Only services** (`ACMF`, `LU`, `MR`, `NZ`, `PD`, `PF`, `SR`, `SU`, `US`) disable `/query` and `/identify` entirely (`code 400`) — yet serve **full anonymous GML/GeoTIFF files** under `https://eservices.dls.moi.gov.cy/inspire_downloads/<THEME>/` (discovered via `serviceatoma1.xml` -> `datasetatoma1.xml`), plus live **WMS** and ArcGIS **`/export`**.
2. **Group Layer ID trap on `/query`:** Group Layers (`HY/0 WFD-River`, `HY/6 Catchment`, `HY/9 Hydrographic Network`, `AU/1`, `AU/6`, `CP/0`, `GE/0`, `GE/2`, `GN/0`, `PS/0,2,4,6`) return `400 Invalid or missing input parameters` if queried directly. Always query leaf **Feature Layer** IDs (`HY/1,4,7,8,10`, `AU/7,8`, `CP/1,2`, `PS/1,3,5,7`, `BU/1,2,4,6`, `EF/0,1,3`). `maxRecordCount` is **`1000`** across queryable layers; paginate with `resultOffset`.
3. **Standard `WMSServer` beats `exts/InspireView/service`:**
   - `exts/InspireView/service` uses INSPIRE layer names (e.g., `NZ.RiskZone_Floodplain_100`), suffers a **`java.lang.OutOfMemoryError: Java heap space` exception** on `LU_LandUse` `GetCapabilities`, returns **`Could not resolve ambiguous namspace for Feature Type "ps:ProtectedSite"`** on `PS_ProtectedSites` `GetFeatureInfo`, and returned `403` on certain WMS 1.3.0 requests (`PD` `InspireView` `GetMap`/`GFI`).
   - Standard ArcGIS **`WMSServer`** (`https://eservices.dls.moi.gov.cy/inspire/services/INSPIRE/<SVC>/MapServer/WMSServer` — note `/inspire/services/` without `/rest/`) works **cleanly with `200 OK`** across `LU_LandUse` (`GetCapabilities` 6,213 B + `GetFeatureInfo` zoning attributes), `NZ_NaturalRiskZones` (`GetFeatureInfo`), `PD_PopulationDistribution` (`GetFeatureInfo`), and `SU_StatisticalUnits` (`GetFeatureInfo`), using numeric layer IDs (`0`, `1`, `2`, ...) and WMS 1.1.1 `SRS=EPSG:4326` (`lon,lat`).
4. **Coordinate Reference System (CRS) split:** Bulk GML files in `/inspire_downloads/` and ArcGIS MapServer native extents on `PD`, `NZ`, `LU`, `US`, `SU`, `EF`, `EL1`, `BU`, `OI` use **`EPSG:3048` (ETRS89 / TM36 / UTM Zone 36N in metres; Nicosia ~ `520,000–546,000 E, 3,880,000–3,900,000 N`)**, whereas `AU`, `CP`, `AD`, `HY`, `GE`, `GN`, `LC`, `PS`, `TN` publish native geographic extents in **`EPSG:4258` (ETRS89)**. (`police_stations_f.zip` and older survey shapefiles use **`CGRS_1993_LTM`**, false easting `200,000`, false northing `-3,500,000`, `CM 33°E`, scale `0.99995`). Note also that central Nicosia sits astride the UN Buffer Zone; cadastral/administrative layers cover Republic of Cyprus government-controlled areas seamlessly and thin north of the Buffer Zone.

---

### Master 26-Service DLS INSPIRE Capability Table

| # | Service (`.../INSPIRE/<Service>/MapServer`) | Layers | REST `capabilities` | INSPIRE Extensions | REST `/query` | WMS (`InspireView` / `WMSServer`) | WFS (`InspireFeatureDownload`) | Atom / `/inspire_downloads/` | Nicosia Coverage (Verified Count) | Verdict for iNicosia |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `ACMF_AtmConditions_MeteoGeoFeatures` | 2 | `Map` | PredefDL, View, WMS | 400 (Disabled) | Advertised | No | `/ACMF/` (`11` files: GML + `m_t_81_10_13.tif` climate grids) | Cyprus-wide raster | **Skip** (Met AWS & WRFv4 are live) |
| 2 | `AD_Addresses` | 1 | `Map,Query,Data` | FeatDL, View, WMS | **200** | 200 | **200** (`6,238 B` sample) | Atom 404 / None | Already in `chatbot/dls.py` | **In production** |
| 3 | `AM_AreaManagement` | 5 | `Map,Query` | PredefDL, View, WMS | 400 (Unsupported) | Advertised | No | Atom **200** / `/AM/AM.gml` (`1.14 MB`, `312` zones) | Bathing/management zones | **Low priority** |
| 4 | `AU_AdmUnits` | 11 | `Data,Map,Query` | FeatDL, View, WMS | **200** (Leafs) | 200 | **200** (`28,433 B` caps) | Atom 404 / None | **74 quarters** (`L7`), **34 municipalities** (`L8`) | **Use** (join base for `PD` & CYSTAT) |
| 5 | `BR_BioGeographicalRegions` | 1 | `Map,Query` | PredefDL, View, WMS | Not tested | Advertised | No | None | Mediterranean region polygon | **Skip** |
| 6 | `BU_Buildings` | 7 | `Data,Map,Query` | PredefDL, View, WMS | **200** | 200 | No | Atom **200** / `/BU/` (`1,598` files) | `L1: 7,334`, `L2: 64,498`, `L4: 21,507` landmarks, **`L6: 89,700` 3D heights** | **In 3D/DLS** (`L4` joins `US1`) |
| 7 | `CP_CadastralParcels` | 3 | `Data,Map,Query` | FeatDL, View, WMS | **200** | 200 | Advertised | Atom 404 / None | **132,718 parcels** (`L1`), **74 blocks** (`L2`) | **High** (exposure & zoning join) |
| 8 | `EF_EnvironmentalFacilities` | 7 | `Data,Map,Query` | PredefDL, View, WMS | **200** | 200 | No | Atom **200** / `/EF/EF.gml` (`2.36 MB`, `865` EMFs) | **`L3: 3` river stns** (`Pedhiaios @ Lefkosia`), `L0: 2` groundwater | **High** (hydrometry sites) |
| 9 | `EL1` | 1 | `Map,Query,Data` | PredefDL, View, WMS | Not re-tested | Advertised | No | `/EL/` (per `3d-twin-research.md`) | 1 m LiDAR DEM | **In production** (`terrain.ts`) |
| 10 | `GE_Geology` | 4 | `Data,Map,Query` | FeatDL, View, WMS | **200** | Advertised | Advertised | None | `L1: 378/29,296` units, `L3: 49/2,279` | **Medium** (pair w/ GSD PSI) |
| 11 | `GN_GeoNames` | 2 | `Data,Map,Query` | FeatDL, View, WMS | **200** | Advertised | Advertised | None | `L1: 1,844/58,082` toponyms | **Medium** (bilingual search index) |
| 12 | `HB_HabitatBiotop` | 2 | `Map,Query` | PredefDL, View, WMS | 400 (Unsupported) | Advertised | No | `/HB/HB.gml` (`23.61 MB`) | Habitat polygons | **Low** |
| 13 | `HY_Hydrography` | 11 | `Data,Map,Query` | FeatDL, View, WMS | **200** (Leafs) | 200 | **200** (`10,596 B` `WFDRiver`) | Atom 404 / None | `L1: 6` WFD rivers, `L7: 9` drainage basins, `L10: 58` links | **High** (flash-flood network) |
| 14 | `LC_LandCover` | 1 | `Data,Map,Query` | FeatDL, View, WMS | **200** | 200 | Advertised | Atom 404 / None | `L0: 168/2,885` polygons | **Low** (CORINE-coarse; S2 10m better) |
| 15 | `LU_LandUse` | 1 | `Map` | PredefDL, View, WMS | 400 (Disabled) | `InspireView` **OOM**, `WMSServer` **200** | No | Atom **200** / `/LU/LU.gml` (`93.64 MB`) | Town planning zones (`Κα-α` Walled City verified via `WMSServer` GFI) | **High** (prefer 2019 KMZ / `/export` / `WMSServer`) |
| 16 | `MR_MineralResources` | 5 | `Map` | PredefDL, View, WMS | Disabled | Advertised | No | `/MR/MR.gml` (`1.08 MB`) | Quarries / mineral occurrences | **Skip** |
| 17 | `NZ_NaturalRiskZones` | 5 | `Map` | PredefDL, View, WMS | 400 (Disabled) | **200** (`WMSServer` GFI **200**) | No | Atom **200** / `/NZ/NZ.gml` (`10.59 MB`, **`4,348`** feats) | **`29`/`14`/`12`** Nicosia 20/100/500-yr Floodplains; `28` hist. quakes | **Top Priority #1** |
| 18 | `OI_Orthoimagery` | 1 | `Data,Map,Query` | PredefDL, View, WMS | Not tested | Advertised | No | `/OI/rasters/` (`2,214` GeoTIFF/ovr files) | High-res national orthophotos | **Handled in #31 / 3D doc** |
| 19 | `PD_PopulationDistribution` | 66 | `Map` | PredefDL, View, WMS | 400 (Disabled) | `WMSServer` GFI **200**; scale-gated GetMap | No | Atom **200** / `/PD/` (`7` GMLs, **`2011-10-01` census**) | Full Cyprus & Nicosia quarters/postcodes/districts | **High** (reconcile w/ CYSTAT 2021) |
| 20 | `PF_ProductionFacility` | 2 | `Map` | PredefDL, View, WMS | Disabled | Advertised | No | `/PF/PF.gml` (`0.19 MB`) | E-PRTR industrial sites | **Medium** (combine w/ Air alerts) |
| 21 | `PS_ProtectedSites` | 8 | `Data,Map,Query` | FeatDL, View, WMS | **200** | `GetCapabilities` 200; **GFI bug** (`ps:ProtectedSite`) | Advertised | Atom 404 / None | `L1: 30/1,279` Natura/protected polygons (`Pentadaktylos`), `L3: 6/445` | **Medium** (use `/query`) |
| 22 | `SD_SpeciesDistribution` | 3 | `Map,Query` | PredefDL, View, WMS | 400 (Unsupported) | Advertised | No | `/SD/SD.gml` (`6.63 MB`) | Species grid cells | **Skip** |
| 23 | `SR_SeaRegions` | 3 | `Map` | PredefDL, View, WMS | Disabled | Advertised | No | None | Marine only (Nicosia landlocked) | **Skip** |
| 24 | `SU_StatisticalUnits` | 5 | `Map` | PredefDL, View, WMS | 400 (Disabled) | **200** (`Merge_Crimes` L0 GFI **200**) | No | Atom **200** / `/SU/SU.gml` (`63.09 MB`) | District crime (`ΑΔΕ ΛΕΥΚΩΣΙΑΣ`, `1,996` total), Quarter/PoCode geos | **Medium** (polygons useful; crime too coarse) |
| 25 | `TN_TransportNetwork` | 3 | `Data,Map,Query` | FeatDL, View, WMS | **200** | 200 | Advertised | Atom 404 / None | `L0: 1/63` (`Aerodromio Lefkosias`), `L1/L2: 24,451/128,353` road links | **Low** (CyNAP + Overture richer) |
| 26 | `US_UtilityAndGovernmentalServices` | 29 | `Map` | PredefDL, View, WMS | 400 (Disabled) | **200** (L20/21/27 empty GFI at spot test) | No | Atom **200** / `/US/` (`321` GMLs) | `US1` schools/hospitals/offices in Nicosia (link to `BU2`); **`US3`/`US4` NO Nicosia** | **Partial** (`US1`+CSVs yes; `US3/4` no) |

*(Additional non-MapServer folders observed under `https://eservices.dls.moi.gov.cy/inspire_downloads/`: `AF/`, `HH/`, `SO/`, `ER/`, `OF/`. Full directory inventory saved in session probe logs.)*

---

### Part A Theme Deep Dives

#### 1. `PD_PopulationDistribution` + `SU_StatisticalUnits` (+ CYSTAT 2021 Bridge)

- **Service & Downloads:** 66-layer `Map`-only service (`L1`–`9` district age/sex, `L10`–`18` community `vill`, `L19`–`27` quarter `qrtr1`, `L28`–`36` postcode `poc1`, `L37`–`50` employed `PD2`/unemployed `PD3`, `L51`–`65` workplace `PD4` Primary/Secondary/Tertiary). Bulk GMLs under `/inspire_downloads/PD/`: `PD1_PopulationAtResidencePlace_Quarters.gml` (`65.62 MB`), `..._POCodes.gml` (`22.60 MB`), `..._Communities.gml` (`56.71 MB`), `..._Districts.gml` (`4.39 MB`), `PD2_EmployedPopulation.gml` (`24.12 MB`), `PD3_UnemployedPopulation.gml` (`24.13 MB`), `PD4_PopulationAtWorkPlace.gml` (`27.79 MB`).
- **Observed Schema (MapServer layers & `WMSServer` `GetFeatureInfo` XML):** 107 attributes including quarter codes (`QRTR_CCD`, `QRTR_NM_E`, `QRTR_NM_G`) or village/district IDs (`VIL_CCD_1`, `DIST_CODE`), 5-year cohorts (`YT0_4`..`YT80_`, `YM*`, `YF*`), single-year cohorts (`YM0`..), summary brackets **`YT0_14`**, **`YT15_24`**, **`YT25_64`**, **`YT65_`**, `YM65_`, `YF65_`, economic activity (`TACTPR`, `TEMPPR`, `TUNEPR`, `TNABPR`), plus citizenship/birth (`TCOC_*`, `TPOB_*`) and institutional population (`HHOLDTPOP`, `INSTTPOP`). Example live `GetFeatureInfo` values (`LEFKOSIA` unit `VIL_CCD_1=1000`): `YT0_14=6833`, `YT15_24=7178`, `YT25_64=32889`, **`YT65_=8112`** (`YF65_=4600`, `YM65_=3512`).
- **Two Critical Caveats Found Live:**
  1. **2011 Census Vintage:** Direct GML header inspection on `PD1_Quarters`, `PD1_POCodes`, and `PD4_WorkPlace` confirms `<gml:beginPosition>2011-10-01</gml:beginPosition>` across all three files (`PD1_old.gml` in `history/` is `142 MB`). Nicosia's `YT65_` spatial distribution still serves well for neighborhood vulnerability weighting, but headline headcounts are 15 years old. **Recommended architectural fix:** extract **quarter/postcode polygons** from `AU_AdmUnits/L7`, `SU.gml` (`SU.VectorStatisticalUnit_Quarter`/`PoCode`), or `PD1_Quarters.gml` / `PD1_POCodes.gml`, and join with live **CYSTAT PxWeb `Census of Population and Housing 2021`** tables (verified live in Part B!).
  2. **WMS Scale Cutoffs & CRS Trap:** `Men_qrtr_*` / `qrtr1` have `minScale: 20000`, `poc1` / `Woman_poc_*` have `minScale: 10000` (`maxScaleDenominator: 9449.4` in WMS). Zoomed-out `GetMap` returns blank 1,880 B / 2,198 B transparent PNGs, whereas `LAYERS=36` zoomed to `33.356,35.169,33.366,35.177` returned a **4,461 B** rendered thematic PNG. Also note `WMSServer` `GetFeatureInfo` uses ArcGIS numeric IDs and hit `DIST_CODE=1` on coarse boxes — parse `PD1_Quarters.gml` & `PD1_POCodes.gml` offline once into DuckDB/GeoJSON rather than relying on runtime WMS `GetFeatureInfo`.
- **`SU_StatisticalUnits` (`Merge_Crimes`):** `SU_VectorStatisticalUnit_Merge_Crimes` (`L0` in `WMSServer`) returned **police division totals only**: `NAMES_AR_1="ΑΔΕ ΛΕΥΚΩΣΙΑΣ"`, `NAMES_AREA="1085 Sq. Km"`, `MURDERS=2`, `ROBBERIES_=31`, `DRUGS=307`, `BURGLARY_H=876`, `THEFTS=216`, `OTHER_SERI=449`, `TOTAL=1996` (entire Nicosia district police command — **zero intra-urban street/quarter resolution**, making it useful only for district context KPIs).

#### 2. `NZ_NaturalRiskZones` + `HY_Hydrography` + `EF_EnvironmentalFacilities` (Flash-Flood & Hydrometry Stack)

- **`NZ.gml` (`https://eservices.dls.moi.gov.cy/inspire_downloads/NZ/NZ.gml`, `10.59 MB`, `EPSG:3048`):**
  - Parsed completely in probe: contains **561 `HazardArea` polygons** and **3,787 `ObservedEvent` points** (`1997-03-30` to `2010-12-30` earthquake archive from `CY.0004-GSD.NZ`).
  - Nationwide breakdown of `HazardArea`: **`flood|returnPeriod=20`: 157**, **`flood|returnPeriod=100`: 79**, **`flood|returnPeriod=500`: 54**, `otherGeologicalHydrological`: 271.
  - Inside Greater Nicosia (`518000..548000 E, 3876000..3902000 N`): **`29` polygons for 20-year floodplain**, **`14` for 100-year floodplain**, **`12` for 500-year floodplain**, **`28` historical earthquakes**, and **`0` `otherGeologicalHydrological` polygons** (meaning landslide/rockfall hazards in `NZ.gml` lie outside central Nicosia; use GSD's `Lefkosia_ground_stability_layer` from Part B for Nicosia urban clay/subsidence!).
  - Live `WMSServer` `GetFeatureInfo` on Nicosia central Pedieos floodplain confirmed **`Code="C01" Name="Πεδιαίος" Name_Eng="PEDIAIOS" Shape.STArea="5761829.017199"`** (`5.76 km²` continuous central zone) + `895,818 m²` sub-reach, attributed to WDD Contract `01/2012` (July 2014 flood delineation).
- **`HY_Hydrography` (REST `/query` + WFS 2.0 `200 OK`):**
  - `L1` (`WFD-River [Line]`): **6** river reaches in Nicosia (`WDD.HG.WSB112..125`, namespace `CY.0006-WDD.HG`).
  - `L4` (`WFD-Lake [Line]`): **8** waterbodies; `L7` (`Catchment - drainage basin`): **9** basins (`WDD.HG.hB3-7-5`); `L8` (`river basin`): **3**; `L10` (`watercourse link`): **58** (`out of 148,045` islandwide).
  - Caveat: many `name` fields carry `name_void=1`; join basin/reach codes cleanly with `NZ` (`Code="C01"`) and `EF/L3` station names.
- **`EF_EnvironmentalFacilities` (`EF.gml` `2.36 MB` + REST `/query` `200 OK`):**
  - `EF.gml` holds **865 `EnvironmentalMonitoringFacility`** sites across `CY.0003-DE.EF` (`317`), `CY.0009-GFS.EF` (`376`), and **`CY.0006-WDD.EF` (`172`)**.
  - `EF/L3` (`River_monitoring_stations`, `45` nationwide) places **3 official WDD hydrometry gauges directly inside urban Nicosia**: **`Pedhiaios @ K. Deftera`** (`SIC=r6-1-2-38`, `525570 E, 3883200 N`), **`Pedhiaios @ Lefkosia`** (`SIC=r6-1-2-90`, `531970 E, 3891620 N`), and **`Vathys @ Athalassa`** (`SIC=r6-1-5-52`, `534824 E, 3886777 N`), alongside `EF/L0` (`2` groundwater wells in Nicosia bbox, e.g., `1981/045`).

#### 3. `LU_LandUse` + `AU_AdmUnits` + `CP_CadastralParcels` (Urban Fabric & Zoning Engine)

- **`LU_LandUse` (`LU.ExistingLandUse_plnz`):** 31 fields including `PLNZNT_NAME` (e.g., `Κα-α`), `PLNZNT_DESC`, **`PLNZNT_COVERAGE_RATE_QTY`** (`0.7`), **`PLNZNT_DENSITY_RATE_QTY`** (`1.4`), **`PLNZNT_HEIGHT_MSR`** (`8 m`), **`PLNZNT_STOREY_NO_QTY`** (`2`), `DEVP_DESC` (`ΣΧΕΔΙΟ ΠΕΡΙΟΧΗΣ ΚΕΝΤΡΟΥ ΛΕΥΚΩΣΙΑΣ`), and `LastEditedDate` (`2022-09-13` observed).
  - **Failure mode & bypass:** `exts/InspireView/service?REQUEST=GetCapabilities` crashes with `java.lang.OutOfMemoryError: Java heap space`. **Three clean alternatives work:** (1) `WMSServer` `GetCapabilities`/`GetMap`/`GetFeatureInfo` (`200 OK`), (2) ArcGIS `/export?f=image` (`200 OK`, `5,420 B` PNG), or (3) bulk download via **`/inspire_downloads/LU/LU.gml` (`93.64 MB`, `EPSG:3048`)** or **`data.gov.cy` TPH January 2019 KMZ (`35.70 MB`)** pre-filtered to Nicosia `DEVP_DESC` into DuckDB/SpatiaLite at build time.
- **`AU_AdmUnits` & `CP_CadastralParcels` (REST `/query` + WFS `200 OK`):**
  - `AU/L7` (`4thOrder` quarters/enories): **74** polygons in Nicosia (`nationalCode` e.g. `100008`, `102102` matching `VIL_CCD_1` prefixes `1000` Lefkosia, `1012` Strovolos, `1021` Lakatamia); `AU/L8` (`3rdOrder` municipalities): **34** polygons in Nicosia bbox.
  - `CP/L1` (`CadastralParcel`): **132,718** parcels in Nicosia (`1,585,808` islandwide) with `areaValue`, `nationalCadastralRef` (`1000-21/450301-1-1-290`), and `admUnit` (`maxScale=0, minScale=25000`). Essential for exact parcel-to-floodplain and parcel-to-zoning intersection queries!

#### 4. `US_UtilityAndGovernmentalServices` (Civic Infrastructure Truth Audit)

- **`US_UtilityAndGovernmentalServices` (`29` layers, `321` Atom/download GML files):**
  - **`US1` (Governmental & Social Services):** `US1_..._1000_LEFKOSIA.gml` (`299.6 KB`, **165** features) breaks down into `publicAdministrationOffice`: 86, `masterOrEquivalentEducation`: 27, `primaryEducation`: 21, `earlyChildhoodEducation`: 12, `educationNotElsewhereClassified`: 11, `generalHospital`: 6, `policeService`: 2; `1012_STROVOLOS.gml` (`294.3 KB`, **162** features) has 20 early-childhood, 26 primary, 7 master, 33 other edu, 61 offices, 15 hospitals. **CRITICAL GML CAVEAT:** Every single one of those 165 features in `1000_LEFKOSIA` has **zero inline `<name>` tags (`0`)**, **zero `<pos>` geometries (`0`)**, and instead binds via `xlink:href="BU2.BU.<id>"` (`165/165`) to `BU_Buildings` (`BU/L4` has 21,507 LandmarkAreas with `LANDMARKANAMEENG`/`LANDMARKANAMEGRK`, `L2` has footprints)! Also **0 fire stations and 0 fire hydrants** appear in the inspected Nicosia/Strovolos `US1` GMLs (and zoomed `WMSServer` `GetFeatureInfo` on `L20` hydrants & `GetMap` on `L21` fire stations returned empty).
  - **`US2` (Environmental Management Facilities):** Covers Nicosia district treatment/waste points (`US2_..._1000_LEFKOSIA.gml`, `1107_DALI`, `1121_AGIOS_SOZOMENOS`, `1232_PANO_DEFTERA`, `1109_LYTHRODONTAS`, `1404_KAKOPETRIA`).
  - **`US3` (Sewer) & `US4` (Water Pipes):** Direct inspection of all **321** files in `/inspire_downloads/US/` proves **100% of `US3` files belong to Limassol/Amathus (`5000..5202`) and 100% of `US4` files belong to Larnaka (`4000..4210`)**. Neither Nicosia Water Board nor Sewerage Board of Nicosia (SBN/EOA Nicosia) publishes pipes to DLS `US3`/`US4`. **Do NOT promise a Nicosia subsurface pipe twin from DLS INSPIRE.**

#### 5. `PS_ProtectedSites`, `LC_LandCover`, `TN_TransportNetwork`, `GE_Geology`, `GN_GeoNames`

- **`PS_ProtectedSites`:** REST `/query` works (`L1` **30** sites in Nicosia e.g. `FD.PS.PS31 Pentadaktylos`, `L3` **6** sites; field names carry verbose table prefixes `inspire.ESRI.psSiteS_designation.designation`); `InspireView` WMS `GetFeatureInfo` fails with an ambiguous-namespace error (`ps:ProtectedSite`). Use `/query` or WFS only.
- **`LC_LandCover`:** `/query` returns `168` Nicosia polygons (`CY.0003-DE.LC`), `class_code=null`, `class_label="CY_6"` — coarser than Copernicus Sentinel-2 10 m and `04_Density_TreeTypes.zip` 20 m.
- **`TN_TransportNetwork`:** `24,451` Nicosia road links (`L1`/`L2`), `L0` returns `Aerodromio Lefkosias`. Useful mainly as fallback centreline geometry where CyNAP/OSM lacks an INSPIRE ID.
- **`GE_Geology` & `GN_GeoNames`:** `378` surface geology polygons (`GE/L1`) and `1,844` GeoNames (`GN/L1`, e.g., `Spiliathkia`) queryable via `/query` + WFS.

#### DLS INSPIRE Licence Conflict Audit

Across DLS portals, **four contradictory licence statements** were directly observed on the same datasets:
1. `WMS GetCapabilities` (`InspireView` across `PD`, `NZ`, `US`, `LU`, etc.): `Fees: no conditions apply`, `AccessConstraints: None`.
2. `portal_geocatalog/rest/metadata/item/.../xml` (`NZ_WDD`, `LU`, `US_WDD`, `SU_Police`, `AU_DLS`): explicitly specifies **`CC BY-SA 4.0`** with **`no limitations`** on public access.
3. `serviceatoma1.xml` `<rights>` tag: `Copyright (c) 2015, DLS; all rights reserved` (legacy ArcGIS-for-INSPIRE template boilerplate).
4. `data.gov.cy` DCAT-AP metadata linking to DLS INSPIRE datasets (noted in `3d-twin-research.md`): **`CC BY 4.0`**.

**Practical stance matching [3d-twin-research.md](3d-twin-research.md):** Cite `DLS / WDD / GSD / CYSTAT (CC BY-SA 4.0 / CC BY 4.0 via DLS INSPIRE Geoportal)` clearly in `chatbot/sources.py`, store derived/joined municipal indicators rather than redistributing raw commercial cadastral dumps, and obtain written confirmation via `INSPIRE@dls.moi.gov.cy` before publishing raw downloadable GML mirrors publicly.

---

## Part B — Untapped Cyprus & European Open Datasets

### Live Probe Summary Matrix

| Dataset / Endpoint | Exact Verified URL | HTTP / Auth | Format | Freshness / Cadence | Nicosia Coverage | Licence | Verdict |
|---|---|---|---|---|---|---|---|
| **`data.gov.cy` DCAT Catalog** | `https://www.data.gov.cy/catalog.json` (also `/data.json`) | **200** / None | JSON-LD (`10.34 MB`) | Daily metadata (`1,959` datasets, `4,059` dists) | National & Nicosia (`47` Municipality datasets, etc.) | `CC BY 4.0` per record | **Ready** (replaces dead `/api/3/action`) |
| **`data.gov.cy` Datastore Search** | `https://www.data.gov.cy/api/action/datastore/search.json?resource_id=<uuid>&limit=N` | **200** / None | JSON | Matches uploaded CSV | Works on CSV-backed resources (fails on external `[api]` wrappers) | `CC BY 4.0` | **Ready** for file-backed tables |
| **CYSTAT PxWeb 2021 Census** | `https://cystatdb23px.cystat.gov.cy/api/v1/en/8.CYSTAT-DB/Population/Census%20of%20Population%20and%20Housing%202021/` | **200** / None | PxWeb JSON | Census 2021 & annual tables | District / Municipality / Postcode | Open gov / CYSTAT | **High** (modernizes 2011 DLS `PD`) |
| **DoM 2 km WRFv4 NWP Forecast** | `https://dom.org.cy/WRF4/{00Z,12Z}/02/Point_Values/Point_Values_LEFKOSIA.txt` | **200** / None | Space-delimited TXT (`2,791 B`) | **12-hourly** (`2026-11-27 18:31Z` verified) | **Lefkosia point**, 3-hourly `Temp, RH, WDIR, WSPEED, RR, SLP` (+72h) | DoM Copyright | **Top Priority** (Official CY forecast) |
| **Copernicus Sentinel-2 L2A (STAC + SAS)** | `https://planetarycomputer.microsoft.com/api/stac/v1/collections/sentinel-2-l2a` (+ `/api/sas/v1/token/sentinel-2-l2a`) | **200** / None (anon SAS) | COG GeoTIFF (`B04, B08, SCL`) | **2–5 days** (latest `2026-11-25`, `5.7%` cloud `T36SWE`) | Tiles `T36SWE` & `T36SWD` cover 100% of Nicosia at **10 m** | Copernicus / CC BY | **Top Priority** (Live NDVI & canopy loss) |
| **DoE Conifer/Broadleaf Tree Density** | `https://www.data.gov.cy/sites/default/files/04_Density_TreeTypes.zip` | **200** / None | ZIP (`9.19 MB`) / **20 m GeoTIFF** | Static baseline | All Cyprus / Nicosia `cy_tcd_ntl_z01_020m_...tif` | `CC BY 4.0` | **High** (shade & species baseline) |
| **GSD Lefkosia Ground Stability (`PSI`)** | `https://www.arcgis.com/sharing/rest/content/items/7daa559e26e140799513918468495e5f/data?f=json` | **200** / None | ArcGIS WebMap JSON (`EPSG:3857`) | Static INSPIRE GSD study | **16 polygons** in Lefkosia incl. **`69.7 km²` `1_ObservedPSI` shrink-swell clay** | GSD public ArcGIS Online | **Top Priority** (InSAR clay/subsidence) |
| **DoE Nicosia Strategic Noise (2017 & 2022)** | `.../23_Noise_Nicosia_DEN.zip` (`8.21 MB`) & `.../agglomerations-strategicnoisemaps-multipolycy-v2-gpkg_2026-03-03_1000.zip` (`345 MB`) | **200** / None | SHP (`9` `Lden` polygons `40–85 dB`) / GPKG | 5-yr EU Noise rounds (updated `2026-03`) | Urban Nicosia agglomeration + major roads | `CC BY 4.0` | **High** (ambient soundscape & health) |
| **Fire Stations & Hospitals CSVs** | `.../fire%20stations.csv` (`4.16 KB`) & `.../Κατάλογος Νοσοκομείων Κύπρου_1.csv` (`1.71 KB`) | **200** / None | CSV w/ `lat, lon` | Irregular static | **4** Nicosia fire stations + **2** major public hospitals (`Nicosia Gen.`, `Makarios III`) | `CC BY 4.0` | **Quick win** (clean emergency anchors) |
| **TPH 2019 Town Planning Zones KMZ** | `https://www.data.gov.cy/el/resource/2589/download/file` | **200** / None | KMZ (`35.70 MB`) | Jan 2019 (or live `LU` WMS) | Full Republic-controlled Nicosia zones | `CC BY 4.0` | **Use** w/ 2016 `.xlsx` code legend |
| **Cyprus Police News & Stations** | `https://www.cypruspolicenews.com/wp-json/wp/v2/posts?categories=5` & `.../police_stations_f.zip` (`6.29 KB`) | **200** / None | WP REST JSON / RSS & SHP | **Live daily** (`2026-11-27`) / Static `53` stns | Road network advisories (`3,285` posts, `cat=5`) + `CGRS_1993_LTM` stns | Public news / `CC BY 4.0` | **High** (needs NLP/street geocoding) |
| **CyNAP `waze_alerts`** | `https://fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/` | **200** / None | DATEX II v3 XML (`228 KB`) | Real-time | **194** active islandwide / **38 Nicosia** (`ROAD_CLOSED`, potholes) | CyNAP Open | **Ready** (no crossing queues inside) |
| **Dept. of Antiquities CSVs + HTML** | `.../Ελεγχόμενες περιοχές από το Τμήμα Αρχαιοτήτων_0.csv`, `.../Επισκέψιμα Αρχαία Μνημεία...csv`, `.../Μουσείων_2.csv`, `https://www.mcw.gov.cy/mcw/da/` | **200** / None | CSV & Lotus/Domino HTML (`50.9 KB`) | Irregular | **13** Nicosia controlled areas (incl. Venetian walls), **11** monuments, **5** museums | `CC BY 4.0` | **Usable with geocoding** (CSVs lack lat/lon) |
| **Civil Defence Stations CSV** | `.../Στοιχεία Επικοινωνίας Πολιτικής Άμυνας_2.csv` (`11.36 KB`) | **200** / None | CSV (`61` rows) | Irregular | **18** Nicosia offices/divisions (`John Kennedy 23`, `Pallouriotissa`) | `CC BY 4.0` | **Directory only** (shelters NOT open) |
| **Nicosia Municipality Discover RSS** | `https://www.nicosia.org.cy/rss/general/?path=/discover/&num=200` (& `bicycles/`) | **200** / None | RSS 2.0 XML (`41.7 KB`) | Irregular (`50` items) | Municipal pools, parking policy, bike paths, cultural sites (**`0/50` have `<geolocation>`**) | `CC BY 4.0` | **Low** (HTML scraping/geocoding needed) |

### Detailed Part B Probe Findings

1. **`data.gov.cy` Portal Migration & API:**
   - Legacy CKAN `/api/3/action/package_list` and `/api/3/action/package_search` both return `404` on the Drupal/EKAN rebuild, as do `/jsonapi` and `/api/1/metastore/`.
   - **Working replacement:** `https://www.data.gov.cy/catalog.json` returns **10,339,095 bytes (`1,959` datasets across `75` publishers, `4,059` distributions)** cleanly.
   - **Working Datastore query:** `/api/action/datastore/search.json?resource_id=6175703b-dcf6-4157-98f1-dd5471f54988&limit=3` returns `{"success": true, "result": {"total": 808, "records": [...]}}` for CSV/file uploads. Resources tagged `format: api` that merely wrap an external URL (such as CYSTAT census or police tables on `data.gov.cy`) return `Resource YYYY doesn't have a proper file path` on Datastore — query CYSTAT's native **PxWeb v1 JSON API** (`https://cystatdb23px.cystat.gov.cy/api/v1/en/8.CYSTAT-DB/`) directly instead.
2. **Department of Antiquities (Venetian Walls, Monuments & Heritage):**
   - `https://www.mcw.gov.cy/mcw/da/da.nsf/index_en/index_en?OpenDocument` (`200 OK`, `50.9 KB`) sits **outside** the `gov.cy` Azure WAF block. However, its structured Lotus Notes links are article HTML pages (`OpenDocument`).
   - Its `data.gov.cy` CSVs list **29 Controlled Areas** (`13` in Nicosia district incl. `Οχυρώσεις (Τάφρος – αντερείσματα)` [Venetian fortifications/moat] & `Καστελλιώτισσα`), **24 Visitable Ancient Monuments** (`11` Nicosia district incl. Kakopetria/Asinou UNESCO churches), and **17 Museums** (`5` Nicosia incl. `Cyprus Museum`, `Hadjigeorgakis Kornesios Mansion` at `Patriarchou Grigoriou 20`). **Limitation:** only museums include street addresses; none of the three CSVs include `lat, lon`. Join monument/museum rows with `AU`/`BU_Buildings` (`L4` landmarks) + Overture/OSM footprints to light up Venetian wall heritage polygons.
3. **Civil Defence & `SafeCY` Public Shelters:**
   - Verified via live HTTP and official UCY/Civil Defence documentation: `https://safecy.gov.cy/` and `cd.moi.gov.cy` do not resolve (NXDOMAIN / DNS failure), `www.gov.cy` & `www.moi.gov.cy` are behind Azure WAF (`403`), and **SafeCY intentionally distributes Cyprus shelter coordinates exclusively inside the iOS/Android mobile app** — no CSV, JSON, GeoJSON, or REST API exists on `data.gov.cy` or INSPIRE. The sole downloadable dataset (`...Πολιτικής Άμυνας_2.csv`) lists 61 administrative/first-aid contact addresses (`18` Nicosia). *(Gap honestly reported: shelter capacity/routing cannot be built today without an institutional Civil Defence agreement.)*
4. **Water Development Department (Reservoirs & Dams):**
   - `www.gov.cy/moa/wdd` and `www.moa.gov.cy` return **403 Azure WAF JS Challenge**.
   - On `data.gov.cy`, the downloadable dam storage CSV (`Αποθήκευση Νερου στα Φράγματα_4.csv`, `808` rows, also queryable via Datastore) ends at **`15/8/2022`**, and the monthly inflows CSV (`Monthly Inflows from 1988_29.csv`) ends at **`2022-23`** (`October–December` only). Until WDD opens an un-WAF'd endpoint matching its HTML tables on `gov.cy`, `data.gov.cy` dam CSVs serve only historical drought benchmarking, while live river floodplains (`NZ.gml`) and monitoring station locations (`EF.gml`/`L3`) remain active on DLS INSPIRE.
5. **Department of Meteorology — Doppler Radar vs 2 km WRFv4:**
   - **Doppler Radar & `DSM` TXT:** Although still linked from `data.gov.cy`, `http://weather.cyi.ac.cy/data/met/radar/doppler.gif`, `long.gif`, and `DSM_ATHALASSA.txt` **fail DNS (`Errno 11001 getaddrinfo failed`)**. (`Θέσεις Radar Τμήματος Μετεωρολογίας.csv`, `325 B`, confirms Larnaka `17608` at `34.93654°N, 33.56654°E` & Pafos `17602` at `34.77104°N, 32.54750°E`, while `dom.org.cy` homepage only links out to WMO.) **Keep RainViewer as live radar.**
   - **2 km WRFv4 CYTerra NWP:** **`https://dom.org.cy/WRF4/00Z/02/Point_Values/Point_Values_LEFKOSIA.txt`** and **`.../12Z/02/...`** return **`200 OK`**, freshly updated on **`Fri, 27 Nov 2026 18:31:36 GMT`**. Output gives 3-hourly rows out to +75h (`Time, Temp, RH, WDIR, WSPEED, RR, SLP`) — directly enabling an official-forecast curve and rolling forecast-vs-AWS backtest in Nicosia. (Directory indexing under `Point_Values/` is disabled, so fetch explicit city names like `LEFKOSIA`, `DALI`, `ATHIENOU` discovered on `data.gov.cy`.)
6. **Copernicus Sentinel-2 L2A & Ground Motion (`EGMS` vs `GSD PSI`):**
   - **Sentinel-2 L2A:** Microsoft Planetary Computer STAC (`collections/sentinel-2-l2a`) returned **10 scenes `<10%` cloud since June 2026** over Nicosia; `S2A_MSIL2A_20261125T083211_R021_T36SWE` (`5.67%` cloud) exposes anonymous Azure Blob COGs (`B04_10m.tif`, `B08_10m.tif`, `SCL_20m.tif`) signed on demand via `200 OK` `/api/sas/v1/token/sentinel-2-l2a`. Joining 10 m NDVI (`(B08 - B04)/(B08 + B04)`) with DoE's **20 m Tree Type Density GeoTIFF** (`04_Density_TreeTypes.zip`, `9.19 MB`) delivers live urban greenness & drought/pruning stress without manual tile accounts.
   - **Sentinel-1 EGMS vs GSD PSI:** `https://egms.land.copernicus.eu/egms.js` routes data calls through `ngvoc/login/NGLogin` (`check-login`, `/portal/api/`) — EU Login gated for automated ingestion. However, **Geological Survey Department (`Cartography_gsd`) published an anonymous ArcGIS Online WebMap (`7daa559e26e140799513918468495e5f`, `Lefkosia Ground Stability Layer`)** whose JSON embeds 16 vector polygons in `EPSG:3857` — including `PGGH_Lefkosia_005` (`3.2_Shrink-swell clays`, **`1_ObservedPSI`, `confidence: High`**, `69.67 km²`), `1.2_Tectonic movements` (`42.70 km²`), and `4.6_Made ground` (`0.19 km²`).
7. **Cyprus Police Traffic, Crossing Queues & Municipal/Research Portals:**
   - **Cyprus Police News (`cypruspolicenews.com/wp-json/wp/v2/`):** `200 OK`, unauthenticated WordPress REST JSON. Category `5` (`ΟΔΙΚΟ ΔΙΚΤΥΟ`) holds **3,285 posts** (multiple live bulletins from `2026-11-27`, plus Category `73` missing persons and `87` public gatherings), extractable cleanly for highway/arterial road closures via station/street NER. Also `police_stations_f.zip` gives **53 police stations** in `CGRS_1993_LTM`.
   - **Crossing-Point Queue Estimation (Agios Dometios / Ledra Palace):** `fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/` returns `200 OK` DATEX II v3 (`194` alerts total / `38` Nicosia — `139 ROAD_CLOSED`, `44 POT_HOLE` including `Kassandras` at `35.1727, 33.3421`), **HOWEVER `waze_traffic/` persistently returns `503 Service Unavailable`**. Without live jam segments at booth approaches, real-time crossing wait times **cannot** currently be derived accurately; CyNAP `bluetooth` corridor readers (`traffic4cyprus.org.cy/swarco3/api/Data/...`) remain the only potential proxy if sensors reach Agios Dometios approach streets (outside current verification).
   - **Nicosia Municipality Live Feeds:** `nicosia.org.cy/el-GR/rss/parkingspaces/` returns `200 OK` XML **but its `<updatedon>` is frozen at `Mon, 05 Dec 2022 11:09:47`** (`-1` spaces on D'Avila, Ledras, Phaneromeni; static `76` on Tripolis). `/discover/` RSS returns 50 items with `geolocation=""`; `/news/app/` returns `410 Gone`; `/events/` RSS is empty (`201 B`).
   - **KIOS (`kios.ucy.ac.cy`) & CYENS (`cyens.org.cy`):** Both websites return `200 OK`, but `kios.ucy.ac.cy/open-data/` is `404` and neither exposes an open live urban sensor REST API (`inicosia.cyens.org.cy` & KIOS water/traffic testbeds require institutional agreement; only research software/static water leak datasets sit on `github.com/KIOS-Research`).

---

## Rejected, Blocked, Stale & Caveated Sources

| Candidate | Observed Failure Mode / Blocker | Verdict | Replacement / Workaround |
|---|---|---|---|
| **DLS `US3_SewerNetwork` & `US4_WaterNetwork` (for Nicosia)** | 100% of `/inspire_downloads/US/` `US3_*` files are Limassol; 100% of `US4_*` are Larnaka; Nicosia pipes absent | **Rejected for Nicosia** | None public (requires EOA Nicosia institutional feed) |
| **SafeCY Civil Defence Shelters** | `safecy.gov.cy` NXDOMAIN; `gov.cy` Azure WAF `403`; shelter DB exists inside mobile app only, zero `data.gov.cy` export | **Blocked / Unavailable** | Only Civil Defence office CSV (`18` Nicosia contacts); cannot map shelters without formal Civil Defence agreement |
| **DoM Doppler Radar GIFs & `DSM_ATHALASSA.txt`** | `weather.cyi.ac.cy` links on `data.gov.cy` fail DNS (`Errno 11001 getaddrinfo failed`) | **Rejected (Dead)** | Use RainViewer radar + live `https://dom.org.cy/WRF4/.../Point_Values_LEFKOSIA.txt` |
| **CyNAP `waze_traffic/` (Crossing Queue Timing)** | Persistent `503 Service Unavailable` HTML error page on both probes (`2026-11-27`) | **Blocked (503 Down)** | Use `waze_alerts` for incidents; do **not** claim live crossing wait minutes |
| **Nicosia Municipality Live Parking RSS** | `nicosia.org.cy/el-GR/rss/parkingspaces/` is `200 OK` but **stale since `2022-12-05`** (`-1` spaces); `/news/app/` is `410 Gone` | **Rejected (Stale since 2022)** | Rely on twin's existing parking sensors + CyNAP parking/EV feeds |
| **WDD Reservoir / Dam Storage & `gov.cy` / `moa.gov.cy` / `moi.gov.cy`** | All `www.*.gov.cy` origins return `403` Azure WAF JS Challenge to automated HTTP clients; `data.gov.cy` CSV frozen at `2022-08-15` | **Blocked / Stale** | Historical baseline only; live river polygons via `NZ.gml` & station locs via `EF.gml` |
| **Copernicus EGMS Viewer InSAR API** | `egms.land.copernicus.eu/egms.js` enforces EU Login (`ngvoc/login/NGLogin`, `check-login`) | **Blocked (Auth Gated)** | Use GSD `Lefkosia_ground_stability_layer` (`7daa559e...`), which embeds `1_ObservedPSI` shrink-swell clay (`High` confidence) |
| **`LU_LandUse` `InspireView` `GetCapabilities` & `PS_ProtectedSites` `GFI`** | `LU` `InspireView` throws `java.lang.OutOfMemoryError: Java heap space`; `PS` `GetFeatureInfo` throws ambiguous `ps:ProtectedSite` namespace error | **Caveated (Broken endpoints)** | Use `LU` `WMSServer` / `/export` / TPH 2019 KMZ / `LU.gml`; query `PS` via REST `/query` or WFS |
| **`PD_PopulationDistribution` Currency & Scale Gate** | GML `<beginPosition>` proves **2011-10-01 census** (15 yrs old); `poc1`/`qrtr1` WMS render blank PNGs when `scale > 10k/20k` | **Caveated (Stale counts)** | Join quarter/postcode geometries with CYSTAT PxWeb `Census 2021` JSON |
| **`data.gov.cy` Legacy CKAN `/api/3/action/` & KIOS `/open-data/`** | Both return HTTP `404 Not Found` | **Rejected (Deprecated)** | Use `data.gov.cy/catalog.json` + `/api/action/datastore/search.json` |
| **`SU` `Merge_Crimes` as Street Safety Map** | Aggregates all serious crime into 1 polygon (`ΑΔΕ ΛΕΥΚΩΣΙΑΣ`, `1,085 km²`, `TOTAL=1,996`) | **Rejected for MapLayer** | District KPI / baseline context only |
| **TPH Listed Buildings (`gov.cy`) & Antiquities Geo-points** | `gov.cy` WAF `403` blocks TPH heritage pages; `data.gov.cy` Antiquities CSVs contain zero coordinates (`lat/lon`) | **Partial / Blocked** | Match named museums/walls against `AU`/`BU/L4`/Overture footprints cleanly in offline ingestion |

---

## Part C — State-of-the-Art "Live, Breathing & Autonomous" Capabilities (2025–2026 Frontier)

Modern leading twins (**Virtual Singapore**, **Helsinki 3D+ / Energy Atlas**, **Vienna Climate Resilience**, **Zurich Urban Twin**, and the **EU Local Digital Twin Toolbox + OGC MIMs Plus** architecture) separate themselves from sensor dashboards through four pillars: **(1) causal cross-domain correlation**, **(2) legible ambient animation**, **(3) sub-second participatory sandboxes**, and **(4) calibrated prediction + open machine interfaces**. Below is how each pillar maps cleanly onto iNicosia's actual code (`chatbot/watch/`, `chatbot/scenarios/`, `chatbot/web/src/lib/`, `chatbot/agent.py`, `collector/db.py`) and verified Part A/B data.

### C.1 Cross-System Cascading & Correlation Intelligence

Right now, `chatbot/watch/detectors.py` and `chatbot/watch/incidents.py` (`Book`, `LINKS`) correlate `Detection`s within `8` isolated families (`outage`, `lighting`, `power`, `parking`, `bins`, `air`, `other`, `feed`) using spatial `radius`/`within`/`same_street`. That structure is already primed for **multi-domain causal cascades** (`incident_links` / `cascade_rule` edges across families):

1. **Storm -> Pedieos Flash Flood -> Road & Clay Exposure Cascade:**
   - **Inputs:** RainViewer tile intensity + Met AWS 10-min rain (PR #46) + `DoM WRF4` next-6h precip (`RR >= 5 mm`) + **DLS `NZ.gml` 20/100-year Pedieos floodplains (`29`/`14` Nicosia polygons, `Code=C01`)** + `EF/L3` hydrometry gauge locations (`Pedhiaios @ Lefkosia` `r6-1-2-90`) + `waze_alerts` `ROAD_CLOSED` + `cypruspolicenews` (`cat=5`) + GSD `3.2_Shrink-swell clays` + **street-lamp/feeder outages**.
   - **Rule:** When intense convective rain or high-`RR` WRF forecast overlaps Nicosia, flag any active road closures, stalled buses (PR #45), or underground-fed streetlamp faults inside `NZ.RiskZone_Floodplain_20` polygons as a single `flood_cascade` incident, counting affected `CP/L1` parcels and `BU_Buildings` footprints along the Pedieos linear park.
2. **Heatwave -> Elderly 65+ Quarters -> Night Comfort & Pharmacy Cascade:**
   - **Inputs:** Met AWS + PR #46 temperature + Landsat LST (`next-additions-research.md` D9) + **Sentinel-2 NDVI green-deficit** (`04_Density_TreeTypes` baseline) + **DLS `PD` `YT65_` quarter share reconciled with CYSTAT 2021** + `AD` duty pharmacies + **`street_light` outages** (`lighting` family).
   - **Rule:** When heat advisory criteria hold (`T >= 38°C` or tropical night `Tmin >= 24°C`), automatically score every Nicosia quarter (`heat_exposure * pct_65plus * (1 - canopy_density)`) and elevate any street-lamp outage inside top-decile 65+ quarters from low-severity maintenance to a `vulnerable_night_safety` link (because elderly residents shift outdoor mobility after dusk), surfacing nearest open late-night pharmacy (`duty_pharmacies`) & shaded park routes.
3. **Saharan Dust / PM10 -> Schools & Hospital Receptor Triage:**
   - **Inputs:** DLI PM10 / CAQI (`air_station` / `look.ts` dust & AOD) + **`US1` + `data.gov.cy` Hospitals CSV (`Nicosia General`, `Makarios III`) + `US1` schools (`primaryEducation`, `earlyChildhoodEducation`: `33` in central Lefkosia + `46` in Strovolos linked to `BU2` footprints) + Strategic Noise `Lden` zones**.
   - **Rule:** When `PM10 > 50 µg/m³` or `caqi >= high`, `Watcher.evaluate()` attaches impacted school/paediatric receptors within the station/plume zone to the `air` incident so briefings immediately quantify sensitive exposure ("21 primary schools + Makarios III Children's Hospital downwind").
4. **Evening Grid Peak <-> Public Streetlighting & EV Demand Cascade:**
   - **Inputs:** TSOC live demand/wind/solar/grid carbon (PR #48) + CyNAP EV chargers + `street_light` metered `power_w` (`scenarios/lighting.py`) + **Nicosia Strategic Noise `>65 dB` corridors**.
   - **Rule:** Correlate dusk ramp (`17:00–21:00`, grid fossil peak) with smart dimming telemetry and EV charger occupancy, identifying corridors where aggressive dimming overlaps high pedestrian density (`Ledras`, `Makariou`) or roadwork alerts (`waze_alerts`), feeding real evening baseline error back into `lighting.py`.

### C.2 Ambient Living-City Motion & Presenter Mode (Roadmap #11)

Nicosia already possesses an extraordinary atmospheric base in `chatbot/web/src/lib/weather/rain.ts` (`RainOverlay`), `look.ts` (clouds, dust yellow haze, wet asphalt sheen), `sky.ts` (astronomical sun/moon), and `NowLine`. The frontier next step turns the idle control-room map into a **calm, scannable, living organism** without adding chart junk (`CLAUDE.md` minimalism):

1. **Dusk Lamp-Lighting & Dawn Fade Wave (Data-Driven Shader/Canvas):** Instead of uniform binary `on/off` points at twilight, interpolate actual feeder/gateway turn-on timestamps from `street_light` history over `sky.ts` civil twilight (`clock.ts` scrubbable at 15-min steps or smooth playback!), visually pulsing Nicosia's historic centre and ring roads awake across the 1 m LiDAR terrain (`terrain.ts`) and 3D buildings (#31).
2. **Modulated Pedestrian & Micro-Mobility Corridor Pulse:** Along key pedestrianized/commercial links (`Ledras`, `Onasagorou`, `Makariou`, `Eleftheria Square`, Pedieos greenway), render subtle particle/dash flow modulated by **real time-of-day**, **weather dampening** (`RainOverlay`/`windKmh`/`temp_c > 39°C` shifts activity toward evening), **bus stop arrivals** (PR #45), and **nearby parking fill**. Zero fake claims of individual-person tracking: explicitly styled and sourced as an atmospheric activity proxy anchored to public transit, parking, and weather. Respect `prefers-reduced-motion` (`this.still` pattern from `RainOverlay`).
3. **Autonomous Briefing & Lobby Presenter Mode (`?present=1`):** Add a single-button / URL-triggered Presenter loop synced to `workspaces.ts` and `clock.ts`: smoothly flies MapLibre cameras across 5 curated Nicosia chapters (*Old Town at Dusk*, *Pedieos Flood Corridor*, *Live Power & Grid Ramp*, *65+ Heat Equity*, *Active Watch Incidents*), gently advances topic chips (`topic-card.tsx`), and **yields instantaneously on any pointer, wheel, or key event** (zero modal lock-in).
4. **Data-Grounded Acoustic Feel (`23_Noise_Nicosia_DEN.zip` + `air_station.noise_db`):** Anchor optional spatial ambience cleanly in **DoE's official 2017/2022 `Lden` contours (`40–85 dB`)** weighted by live DLI `noise_db` (`watches.py` already defines `noise_db` in `METRICS["air_station"]`!) and current traffic flow — rendered cleanly as an `Outside` acoustic band readout or soft opt-in ambient Web Audio soundscape (**muted/off by default**, visual contour always primary).

### C.3 Interactive Decision Sandboxes & Co-Design (Building on `simulation-research.md`)

[simulation-research.md](simulation-research.md) designed the backend simulation contracts (`C1`–`C4`: `quick`, `evaluate`, `sweep`, `tradeoff`, `find_policy`, `changes`/`x-edit`/`probe`) and `P1`–`P10` models (`waste`, `lighting`, `Area.with_canopy`, OSM `48 ms` walk-graph). The 2025–2026 frontend/UX leap turns those backend contracts inside `scenarios/lab.tsx` from a form-submit modal ("Set up -> Run -> Read results") into **direct manipulation on the 3D Nicosia map**:

1. **Instant Slider Curves & Interactive Pareto Explorer (`P1` + `P2` UI):** Call `POST /api/scenarios/{id}/quick` (debounced `~120 ms`) on every parameter slider drag inside `lab.tsx`, streaming live delta badges and plotting a Vega-Lite **Pareto efficiency curve** (e.g., streetlight kWh saved vs dark-footpath share; waste truck km vs >85% overflow risk) where clicking any point on the curve snaps the sliders to that optimal non-dominated policy.
2. **Drag-a-Tree / Cool-Corridor Micro-Sandbox (`P6` Extended w/ Part A/B):** Users paint or drop tree clusters directly along Nicosia streets (`Ledras`, `Stasikratous`, school yards from `US1`). Backed by **Sentinel-2 NDVI deficit + `04_Density_TreeTypes.zip` baseline + `PD` `YT65_` quarter density + `LU_LandUse` zone/street width constraints**, `Area.with_canopy` computes shadow/surface-temperature delta, pedestrian-comfort minutes added, and Priority Heat-Equity score in real time.
3. **Place-a-Service 15-Minute City Editor (`P5` Extended w/ `PD`/`CYSTAT` + `US1`):** Drop a candidate mobile clinic, evening pharmacy, shaded bus shelter, EV charger, or green pocket park onto any `CP/L1` parcel -> `< 50 ms` incremental Dijkstra delta over Nicosia's walk graph recomputes walkable `5/10/15-min` catchments (`isochrones`) overlaid live with 65+ resident density (`PD`/`CYSTAT 2021`).
4. **Facilitated Municipal Co-Design Session Mode (`P7` + `compare-swipe.tsx`):** Generate deterministic deep-links (`#/s/<run_id>?optionB=<id>`) reusing Nicosia's `compare-swipe.tsx` split-screen slider so council officials or citizens drag the divider across **Policy Option A vs Option B** in live town-hall sessions, exporting one-click bilingual Greek/English assumption + backtest briefs (`brief.py`).

### C.4 Predictive Twin, Open Ecosystem & Architectural Scaling Path

Grounded directly in `pyproject.toml` (`anthropic 1.15.0`, `duckdb 1.5.6`, `fastapi 0.151.0`), `chatbot/tools/registry.py` (`38` tools, `14` domains), `chatbot/agent.py`, and `collector/db.py`:

1. **Forecasts & Self-Calibrating Prediction Ledger (`Roadmap #7`):**
   - Fuse **DoM `WRF4` 3-hourly Lefkosia forecasts**, **TSOC wind/solar net-load expectations** (PR #48), and **sensor/mobility autoregressive baselines** (parking decay, bin fill velocity, bus headway drift).
   - Store issued forecasts (`issue_ts, target_ts, horizon_h, metric, p10, p50, p90`) in a DuckDB `prediction_ledger` table joined automatically against subsequent observed sensor/AWS rows. Render **rolling 30-day MAE & empirical coverage bands** directly in `NowLine` / `charts.ts` so the twin transparently grades its own forecast skill before users trust "tomorrow's heat/overflow outlook".
2. **Scaling Claude Past 38 -> 60+ Tools with Anthropic Tool Search:**
   - **Verified ceiling:** `TOOL_DEFS` already holds **38 tools** across 14 domains (`registry.py`), plus `WEB_SEARCH`, sitting right inside the `30–50` tool threshold where upfront tool schemas bloat tokens and degrade tool-selection precision.
   - **Verified implementation in `anthropic>=1.15.0`:** Enable `betas=["advanced-tool-use-2025-11-20"]` alongside existing caching beta handling, keep ~`10–12` high-frequency core tools non-deferred (`defer_loading: false`: e.g., `overview`, `find_places`, `show_layer`, `fly_to`, `chart`, `get_incidents`) with stable `TOOL_DEFS` ordering for Anthropic prompt caching, inject **`{"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}`** (MUST never set `defer_loading: true` on the search tool itself, else `400`), and mark specialist INSPIRE/Sentinel-2/Scenario-sweep/Compliance tools `defer_loading: true` with keyword-rich descriptions (Greek + English domain nouns like `floodplain Pedieos`, `65+ elderly`, `WRF forecast`, `NDVI`, `planning zone`).
3. **Open Standards & Ecosystem (`Roadmap #13` — EU LDT Toolbox / MIMs Plus):**
   - **Read-Only OGC SensorThings API v1.1 (`GET /st/v1.1/Things`, `Locations`, `Datastreams`, `Observations`, `ObservedProperties`):** Project `DeviceState`, Air/Met/River gauging metadata (`EF/L3`), and DuckDB history onto OASIS/OGC SensorThings JSON (`$filter`, `$top`, `$orderby`) — directly interoperable with QGIS SensorThings plugin, Kepler.gl, and EU SIMPL/LDT data spaces.
   - **Read-Only MCP Server (`/mcp`, Streamable HTTP via FastMCP):** Expose `overview`, `incidents`, `scenarios`, `sources`, `layer_ref` metadata, and curated resources (`inicosia://sources`, `inicosia://architecture`) over stateless read-only MCP so researchers, municipal analysts, and external assistants can query iNicosia safely.
4. **Bilingual EN/EL UX & Multimodal Field Reporting (`Roadmap #14`):**
   - **Bilingual + Transliterated Search:** Centralize UI dictionary (`en` / `el`) across `web/src/lib/` + reuse `dls.py`'s `GREEK_TO_LATIN` (`ELOT 743`) normalizer across `search-providers.ts` and `AU`/`PD`/`GN_GeoNames` place lookup so typing `"Ledras"`, `"Λήδρας"`, or `"Pallouriotissa"` matches identically. Also localize `Prompt` / chart tooltips & `brief.py` exports.
   - **Voice & Photo Triage:** Optional browser Web Speech / server Whisper audio-to-ask-bar button (`composer.tsx`), plus image upload -> Claude Vision structured hazard classifier -> cross-check EXIF/click coordinates against `NZ` floodplain + `waze_alerts` + `AD_Addresses` -> **draft-only** municipal maintenance ticket or advisory watch (never auto-submitted without human confirmation, honoring privacy PII blurring).
5. **Breaking the Single-Worker DuckDB & In-Memory State Ceiling:**
   - **Current bottleneck verified in codebase:** `collector/db.py` documents that DuckDB permits only a single writer or non-mixed RO readers across processes, forcing single-process serialization behind `db.GATE` (`ReaderWait=1.5s`, `WriterWait=5.0s`, throwing `Unavailable("busy")`), while `chatbot/agent.py` stores `self.conversations` in Python RAM (`dict`). That prevents `uvicorn --workers > 1`, blue-green zero-downtime reloads, and heavy GIS/Sentinel raster jobs from sharing the FastAPI process cleanly.
   - **3-Step Graduation Path:**
     1. *Separate operational state from columnar analytics:* Move mutable transactional tables (`watches`, `watch_fires`, `incidents`, `incident_actions`, `prediction_ledger`, `conversations`, `scenarios`) to **SQLite WAL / PostgreSQL** so UI/chat writes never stall behind analytical DuckDB queries or `db.GATE`.
     2. *Decoupled analytics & worker processes:* Have `collector` write partitioned **Parquet / append-only DuckDB snapshots** synced asynchronously to read-only multi-worker query handles, + SSE fan-out (`Hub`) backed by Postgres `LISTEN/NOTIFY` or Redis Pub/Sub so `uvicorn --workers 4` shares live `watch` alerts and conversations seamlessly.
     3. *Out-of-process GIS/EO worker:* Run heavy GML ingestion (`LU.gml` 93 MB, `PD1_Quarters.gml` 65 MB), Sentinel-2 COG window reads, and OSM/canopy matrix precomputations in scheduled CLI/worker jobs writing compact GeoParquet / PMTiles artifacts consumed read-only by the API server.

---

## Ready-to-Launch Session Briefs

Each brief below is standalone and scoped to land cleanly via a single PR against `main` adhering to `CLAUDE.md` rules (`layer_ref`/`data_ref`, `chatbot/sources.py`, `TOOL_SOURCES`, `TOOL_LABELS`, offline fixture tests, Base UI `base-nova`, sentence-case UI text with no em or en dashes, and Playwright/lint verification).

---

### Session Brief 1 — Pedieos Flash-Flood, Hydrometry & Clay Ground-Stability Layer (`NZ` + `HY` + `EF` + `GSD PSI`)

**Goal:** Give iNicosia an authoritative hydrological & ground-hazard backbone connecting river rain, official 20/100/500-year Pedieos floodplains, WDD hydrometry gauges, and GSD's `1_ObservedPSI` shrink-swell clay instability map.

**Verified Data & Endpoints:**
- Floodplains & historical quakes: `https://eservices.dls.moi.gov.cy/inspire_downloads/NZ/NZ.gml` (`10.59 MB`, `EPSG:3048`; Nicosia box holds **29** 20-yr, **14** 100-yr, and **12** 500-yr Floodplain `HazardArea` polygons (`Code=C01 PEDIAIOS`) + 28 historic quakes) or `WMSServer` GFI.
- River & drainage basins: `HY_Hydrography/MapServer/{1,4,7,8,10}/query` (`6` WFD rivers, `9` drainage basins, `58` watercourse links in Nicosia) + `EF_EnvironmentalFacilities/MapServer/3/query` (**3 WDD gauges**: `Pedhiaios @ Lefkosia` `r6-1-2-90`, `Pedhiaios @ K. Deftera` `r6-1-2-38`, `Vathys @ Athalassa` `r6-1-5-52`).
- Clay/Subsidence InSAR layer: `https://www.arcgis.com/sharing/rest/content/items/7daa559e26e140799513918468495e5f/data?f=json` (16 polygons `EPSG:3857` -> `PGGH_Lefkosia_005` shrink-swell clays `69.67 km²`, `1_ObservedPSI High`, `1.2_Tectonic movements` `42.70 km²`, `4.6_Made ground` `0.19 km²`).

**Scope:**
1. Offline CLI builder/sync step (`collector/` or `chatbot/inspire_hazards.py`) converting `NZ.gml` Nicosia polygons, `HY` Nicosia reaches/basins, `EF/L3` river stations, and GSD `7daa559e...` polygons from `EPSG:3048`/`3857` into WGS84 GeoParquet / DuckDB spatial/JSON cache + `CP/L1` parcel flood-tag counts. Keep raw GML files out of git; add offline XML/JSON fixtures for tests.
2. Add `get_flood_risk` & `get_ground_stability` tools (`chatbot/tools/hazards.py` or `environment.py`, registered in `TOOL_DEFS`, `TOOL_SOURCES`, `TOOL_LABELS`) answering: *"Is address/parcel X inside the 20/100/500-year Pedieos floodplain or shrink-swell clay zone? Which WDD gauge & upstream drainage basin feed this street?"*
3. MapLibre fill/outline layers in `layers.ts` & `workspaces` / `Outside` topic drawer (`floodplain_20`, `floodplain_100`, `floodplain_500`, `pedieos_network`, `ground_stability_psi`), with clear legend and caveat badge (*DLS/WDD 2014 flood delineation; GSD INSPIRE PSI, not live water-level telemetry*).
4. Update `chatbot/sources.py` with DLS (`CC BY-SA 4.0` per geocatalog), WDD Contract `01/2012`, and GSD attribution.

**Acceptance Tests:** Offline unit tests parsing sample `NZ.gml` + GSD WebMap JSON + CRS reprojection (`EPSG:3048` & `3857` -> WGS84 within `<2 m`), `TOOL_SOURCES`/`TOOL_LABELS` parity test, frontend Vite build + Playwright visual screenshot check showing floodplains along Pedieos cleanly aligned under bridges/buildings.

---

### Session Brief 2 — Elderly 65+ Vulnerability, Zoning Quotas & Emergency Receptors (`PD` + `CYSTAT 2021` + `LU` + `US1`/CSVs)

**Goal:** Launch Nicosia's Vienna-style Heat/Hazard Equity & Urban Planning layer: join official quarter/postcode age profiles (`65+`), modernized via CYSTAT 2021 Census counts, with Town Planning Zones (`LU`) and geolocated hospitals, fire stations, and schools.

**Verified Data & Endpoints:**
- Demographics geometries & 2011 ratios: `PD1_PopulationAtResidencePlace_Quarters.gml` (`65.6 MB`) + `AU_AdmUnits/MapServer/{7,8}/query` (`74` quarters, `34` municipalities) -> `YT0_14, YT15_24, YT25_64, YT65_`, reconciled with **CYSTAT PxWeb `Census of Population and Housing 2021`** (`https://cystatdb23px.cystat.gov.cy/api/v1/en/8.CYSTAT-DB/Population/Census%20of%20Population%20and%20Housing%202021/`).
- Zoning: `https://www.data.gov.cy/el/resource/2589/download/file` (`35.7 MB` Jan 2019 TPH KMZ) + `LU_LandUse` `WMSServer` (`Coverage`, `Density`, `Height`, `AllowedStories`, `DEVP_DESC`) + `CP_CadastralParcels/MapServer/1/query` (`132,718` Nicosia parcels).
- Receptors: `https://www.data.gov.cy/sites/default/files/fire%20stations.csv`, `.../Κατάλογος Νοσοκομείων Κύπρου_1.csv`, plus `US1_..._{1000_LEFKOSIA,1012_STROVOLOS,...}.gml` joined via `BU2.BU.*` to `BU_Buildings/MapServer/4/query` (`21,507` LandmarkAreas with school/office names; do **NOT** ingest Limassol `US3` or Larnaka `US4`).

**Scope:**
1. Build an offline ingestion script generating compact Nicosia quarter/postcode JSON/DuckDB tables (`2011` shares scaled/updated to `2021` CYSTAT totals, with explicit vintage metadata so UI/chat never conflates 2011 & 2021).
2. Add live/cached zoning lookup (`wms/GetFeatureInfo` on `LU_LandUse/WMSServer` or indexed 2019 KMZ polygons) integrated directly into the **Building / Parcel Inspector** (`inspector/sections.tsx`), displaying planning zone (`Κα-α`), max density, site coverage, max floors/metres alongside existing height from `BU/L6`.
3. Add Emergency & Vulnerable Receptors map layer (`fire stations`, `hospitals`, `schools`, `elderly 65+ choropleth`) + `analyze_neighbourhood_vulnerability` tool returning `layer_ref` & `data_ref` ranking quarters by `(heat/PM10/flood score × 65+ share × distance to hospital/fire station/cooling park)`.
4. Strict privacy/fairness guardrails: show neighbourhood aggregates (`qrtr1`/`poc1`) only, never individual parcel ownership or crime micro-claims (`SU` `Merge_Crimes` excluded from quarter maps).

**Acceptance Tests:** Offline CSV/GML/KMZ join fixture tests validating all 4 Nicosia fire stations and 2 Nicosia hospitals land inside Greater Nicosia bbox, zero NaN ratios in CYSTAT 2021 scaling, inspector rendering test for zoning fields, and Playwright check of 65+ choropleth + accessibility contrast.

---

### Session Brief 3 — Official 2 km WRF Forecast, Live 10 m Sentinel-2 NDVI & Urban Noise (`dom.org.cy/WRF4` + `Planetary Computer` + `DoE Trees/Noise`)

**Goal:** Upgrade iNicosia's environment twin with official 72-hour WRF forecasting + self-calibrating backtest, seasonal 10 m Sentinel-2 NDVI canopy monitoring over the 20 m DoE forest density baseline, and official Nicosia `Lden` strategic noise contours.

**Verified Data & Endpoints:**
- Official Cyprus 2 km WRFv4 NWP: `https://dom.org.cy/WRF4/{00Z,12Z}/02/Point_Values/Point_Values_LEFKOSIA.txt` (plus `DALI`, `ATHIENOU`; `200 OK`, updated 2×/day, 3-hourly `Temp, RH, WDIR, WSPEED, RR, SLP`). Do **not** use dead `weather.cyi.ac.cy` radar GIF links.
- Sentinel-2 L2A COGs: Planetary Computer STAC (`https://planetarycomputer.microsoft.com/api/stac/v1/collections/sentinel-2-l2a`) + `/api/sas/v1/token/sentinel-2-l2a`, sampling Nicosia bbox on scenes `<15%` cloud (`B04_10m`, `B08_10m`, `SCL_20m` cloud mask) -> 10 m NDVI GeoTIFF/PNG/grid + `04_Density_TreeTypes.zip` (`9.19 MB` 20 m conifer/broadleaf baseline).
- Environmental Noise: `https://data.gov.cy/sites/default/files/23_Noise_Nicosia_DEN.zip` (`8.21 MB`, 9 `Lden` bands `40–85 dB`) & 2022 Agglomeration GPKG (`345 MB` pre-clipped offline to Nicosia).

**Scope:**
1. Scheduled collector job fetching latest `00Z`/`12Z` `Point_Values_LEFKOSIA.txt` into DuckDB `prediction_ledger` table, paired automatically with Met AWS / PR #46 measured temperature/rain/wind to track 24h/48h/72h MAE & rain hit-rate; surface forecast curve & accuracy badge in the `Outside` topic & `NowLine` tooltip (no em/en dashes!).
2. Monthly/on-demand EO worker script fetching latest cloud-free `T36SWE`/`T36SWD` Nicosia window (`NDVI = (B08 - B04)/(B08 + B04)` masked by `SCL`), outputting an ~200 KB Nicosia WebP/GeoTIFF overlay & quarter/park canopy change delta (`ΔNDVI` vs 12-month prior + baseline from `04_Density_TreeTypes.zip`).
3. Clipped Nicosia `23_Noise_Nicosia_DEN` (and 2022 `Lden`/`Lnight` GPKG summary) exposed as a `noise_lden` map layer + `Here` / `Outside` parcel decibel readout (`"Estimated road/city noise band: 60 to 65 dB Lden"`).
4. Add `get_official_forecast` and `get_green_noise_profile` tools with `data_ref`/`layer_ref`.

**Acceptance Tests:** Parser fixture tests for WRFv4 fixed/space table (`2026-11-27 12Z` sample), synthetic Sentinel-2 NDVI + `SCL` cloud masking test in numpy, `Lden` shapefile polygon containment test, and forecast ledger accuracy test.

---

### Session Brief 4 — Cross-System Compound Incident Engine & Live Police Road Triage (`watch/incidents` Cascade + `cypruspolicenews` + `waze_alerts`)

**Goal:** Turn `chatbot/watch/` (`Book`, `LINKS`, `detectors.py`) from isolated device-family alerts into causal cross-system urban intelligence, enriched with live Cyprus Police highway/road announcements.

**Verified Data & Endpoints:**
- Existing Watch backbone: `chatbot/watch/detectors.py`, `incidents.py`, `service.py`, `watches.py`.
- Road bulletins: `https://www.cypruspolicenews.com/wp-json/wp/v2/posts?categories=5&per_page=20` (`200 OK`, WP REST JSON, 3,285 road posts updated daily) + `https://fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/` (`200 OK`, DATEX II v3 XML, `38` active Nicosia incidents; skip `waze_traffic/` which returns `503`).
- Compound hazard/receptor layers from Briefs 1 & 2 (`NZ` floodplains, `PD`/`CYSTAT` `65+`, `schools`/`hospitals`, PR #46/#48 feeds).

**Scope:**
1. Extend `Book` / `LINKS` in `chatbot/watch/incidents.py` with declarative cross-family `CascadeRule` evaluators triggered inside `Watcher.evaluate()`:
   - `storm_flood_cascade` (heavy rain/WRF precip + `NZ` 20/100-yr floodplain overlap + `waze_alerts`/`police` road closure or lamp outage)
   - `heat_equity_cascade` (heat threshold + `65+` top-quartile quarter + dusk lamp outage or LST peak)
   - `dust_receptor_cascade` (`PM10`/`caqi >= high` + downwind `schools`/`hospitals` count)
   - `grid_lighting_cascade` (TSOC evening demand/carbon peak + streetlight `power_w`/EV corridor load).
2. Polite polling fetcher (every 5–10 min) for `cypruspolicenews` category `5`, filtering to Nicosia (`Λευκωσί*`, highway `Α1`, `Μόρφου`, `Στρόβολ*`, `Λακατάμ*`, street matching against Nicosia street index + `GREEK_TO_LATIN`) as advisory `road_bulletin` detections linked automatically into nearby `waze_alerts` & road incidents.
3. Upgrade `WatchCard` & Incident Inbox UI (`components/watch/`) to visualize causal badges (*"2 systems linked: rain risk + 3 road closures"*), expandable causal chain list, and confidence/freshness metadata.
4. Keep prompt safety & Watch rules intact: `create_watch` continues proposing watches requiring explicit user click (`Watches.confirm`).

**Acceptance Tests:** Fixture replay tests feeding synthetic synchronized rain + floodplain + closure events into `Book`, validating exact multi-family incident creation and no false cascades across disconnected districts; Greek police post geocoder tests (handling `Λεμεσού – Λευκωσίας`, Strovolos/Agia Varvara mentions without crashing on off-city districts); Watch SSE UI Playwright check.

---

### Session Brief 5 — Ambient Living City, Presenter Mode & Interactive Co-Design Sandboxes (`Roadmap #11` + `simulation-research` `P1/P5/P6/P7`)

**Goal:** Deliver iNicosia's signature "living, breathing" visual polish and turn the Scenario Lab into a live collaborative sandbox for planners and public workshops.

**Verified Hooks in Codebase:**
- Sky, rain & lighting look: `chatbot/web/src/lib/map/sky.ts`, `lib/weather/look.ts`, `lib/weather/rain.ts`, `lib/clock.ts`, `components/shell/now-line.tsx`.
- Scenario engine & UI: `chatbot/scenarios/{registry,lab,lighting,waste}.py` (`simulation-research.md` `C1`–`C4`, `P1`–`P7`), `web/src/components/scenarios/lab.tsx`, `web/src/components/clock/compare-swipe.tsx`, `lib/map/terrain.ts`.

**Scope:**
1. **Dusk Lamp-Lighting Wave & Pedestrian Corridor Pulse:** Canvas/WebGL layer (`registerLayerRenderer`) driven by `clock.ts` + metered lamp turn-on curves (`lighting`) + weather/time-of-day/transit-modulated subtle particle flow on core Old Town / Makariou corridors (`prefers-reduced-motion` static fallback matching `RainOverlay`).
2. **Scripted Autonomous Presenter Mode (`?present=1` + shell toggle):** 5-chapter looping Nicosia story tour coordinating MapLibre camera glides (`flyTo`), `clock.ts` time scrubbing, topic highlights, and Watch callouts; immediately yields on any user mouse/touch/keyboard input, announced cleanly via ARIA live region (`NowLine` pattern).
3. **Interactive Sandboxes inside `Scenario Lab`:**
   - Instant debounced `POST /api/scenarios/{id}/quick` slider response + clickable Pareto curve chart (`vega-lite`).
   - **Drag-a-Tree (`canopy`) sandbox** reusing `LU` corridor widths + Sentinel-2 NDVI deficit + `PD` `65+` weights (`simulation-research.md` `P6`).
   - **15-min Place-a-Service editor (`place_service`)** clicking the map to place a clinic/pharmacy/bus shelter -> instant walk-graph delta catchment (`P5`).
   - **Facilitated Split-Compare Co-Design (`compare-swipe.tsx`)** allowing Option A vs Option B side-by-side map swipe + shareable state URL `#/s/<id>`.
4. Strict visual restraint per `CLAUDE.md`: sentence case labels, no em/en dashes in copy, zero obstruction of primary sensor pins, toggles cleanly from Map Menu / Workspace strip.

**Acceptance Tests:** Vitest pure-function tests for ambient modulation math & `prefers-reduced-motion` bypass, Python tests for `quick` endpoint & `canopy`/`place_service` scenarios, and Playwright e2e verifying `?present=1` auto-yields on keypress + Option A/B split swipe renders without WebGL errors.

---

### Session Brief 6 — Open Twin Standards & Scale: Anthropic Tool Search, OGC SensorThings, Read-Only MCP, Bilingual & State Separation (`Roadmap #13` & `#14`)

**Goal:** Future-proof iNicosia's AI agent, external interoperability, multilingual accessibility, and multi-worker backend scaling before tool counts hit 50+ or traffic outgrows single-process DuckDB.

**Verified Codebase Anchors:**
- `pyproject.toml`: `anthropic>=1.15.0`, `duckdb>=1.5.6`, `fastapi>=0.151.0`, `uvicorn[standard]>=0.54.0`.
- `chatbot/tools/registry.py`: 38 tools across 14 fixed `DOMAINS`; `chatbot/agent.py` streams via `self.client.beta.messages.stream` + prompt caching; `collector/db.py` uses `db.GATE` (`ReaderWait=1.5s`, `WriterWait=5.0s`); `chatbot/dls.py` already has `GREEK_TO_LATIN`.

**Scope:**
1. **Anthropic Tool Search Migration (`registry.py` + `agent.py`):** Introduce `betas=["advanced-tool-use-2025-11-20"]`, keep ~12 core tools always loaded (`defer_loading: false`), mark specialized INSPIRE/EO/Scenario/Compliance tools `defer_loading: true`, and append `{"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}` (never deferred). Verify prompt-cache hit rates remain high across turns.
2. **Read-Only External Interfaces (`Roadmap #13`):**
   - `GET /st/v1.1/{Things,Locations,Datastreams,Observations,ObservedProperties}` implementing OGC SensorThings API v1.1 read subset over `DeviceState`, air/weather sensors, and `EF/L3` river stations (MIME `application/json`, `$top`, `$filter`).
   - Read-only **FastMCP Streamable-HTTP endpoint (`/mcp`)** exposing safe twin query tools + `inicosia://sources` & `inicosia://incidents` resources (rate-limited, read-only).
3. **Bilingual EN/EL UX & Multimodal Photo Triage (`Roadmap #14`):** String localization dictionary (`en` / `el`) across shell, topics & briefs; unified `GREEK_TO_LATIN` (`ELOT 743`) search index (`AU` quarters + `GN_GeoNames` + Overture); optional voice input & Claude Vision image-upload hazard classifier yielding draft-only advisory reports (`never` auto-filed without explicit confirmation).
4. **Decouple Operational State from DuckDB:** Extract `watches`, `watch_fires`, `incidents`, `incident_actions`, `prediction_ledger`, `conversations`, and `scenarios` into SQLite WAL (with clean SQLAlchemy/Postgres DSN switch) while leaving `collector` writing read-only Parquet/DuckDB analytical partitions, removing single-process `db.GATE` lock contention across multiple `uvicorn` workers.

**Acceptance Tests:** Mocked Anthropic SDK payload tests asserting valid `tool_search_tool_bm25_20251119` schema (and checking cache_control breakpoints + no deferred search tool), OGC SensorThings `$top`/`$filter` response validation tests, FastMCP inspect-tools & read-only enforcement tests, Greek/Latin transliteration round-trip tests, and concurrent read/write load tests against the separated state store.

---

## Verification Index — Key Live Endpoints Probed (2026-11-27)

- **DLS INSPIRE REST Root:** `https://eservices.dls.moi.gov.cy/inspire/rest/services/INSPIRE?f=json` (`200 OK`, 26 MapServers)
- **DLS INSPIRE Bulk Downloads:** `https://eservices.dls.moi.gov.cy/inspire_downloads/{PD,NZ,LU,US,SU,EF,AM,BU,ACMF,PF,HB,SD,MR,OI,AF,HH,SO,ER,OF}/` (`200 OK` directory listings)
- **DLS GeoCatalog Metadata / Licences:** `https://eservices.dls.moi.gov.cy/portal_geocatalog/opensearch?q=&f=json` (`200 OK`, 107 items) & `/portal_geocatalog/rest/metadata/item/<guid>/xml`
- **`data.gov.cy` DCAT-AP & Datastore:** `https://www.data.gov.cy/catalog.json` (`200 OK`, `10.34 MB`, `1,959` datasets) & `https://www.data.gov.cy/api/action/datastore/search.json?resource_id=6175703b-dcf6-4157-98f1-dd5471f54988&limit=3` (`200 OK`, `808` rows)
- **CYSTAT PxWeb API:** `https://cystatdb23px.cystat.gov.cy/api/v1/en/8.CYSTAT-DB/Population/Census%20of%20Population%20and%20Housing%202021/` (`200 OK`)
- **DoM 2 km WRFv4 NWP:** `https://dom.org.cy/WRF4/12Z/02/Point_Values/Point_Values_LEFKOSIA.txt` (`200 OK`, `2,791 B`, `Last-Modified: Fri, 27 Nov 2026 18:31:36 GMT`)
- **GSD Lefkosia Ground Stability (`PSI`):** `https://www.arcgis.com/sharing/rest/content/items/7daa559e26e140799513918468495e5f/data?f=json` (`200 OK`, 16 polygons)
- **Planetary Computer Sentinel-2 L2A:** `https://planetarycomputer.microsoft.com/api/stac/v1/collections/sentinel-2-l2a` & `/api/sas/v1/token/sentinel-2-l2a` (`200 OK`)
- **DoE Tree Density & Strategic Noise:** `https://www.data.gov.cy/sites/default/files/04_Density_TreeTypes.zip` (`200 OK`, `9.19 MB`) & `.../23_Noise_Nicosia_DEN.zip` (`200 OK`, `8.21 MB`) & `.../agglomerations-strategicnoisemaps-multipolycy-v2-gpkg_2026-03-03_1000.zip` (`200 OK`, `345.47 MB`)
- **Fire Stations & Hospitals:** `https://www.data.gov.cy/sites/default/files/fire%20stations.csv` (`200 OK`) & `https://www.data.gov.cy/sites/default/files/%CE%9A%CE%B1%CF%84%CE%AC%CE%BB%CE%BF%CE%B3%CE%BF%CF%82%20%CE%9D%CE%BF%CF%83%CE%BF%CE%BA%CE%BF%CE%BC%CE%B5%CE%AF%CF%89%CE%BD%20%CE%9A%CF%8D%CF%80%CF%81%CE%BF%CF%85_1.csv` (`200 OK`)
- **Police News REST & Stations:** `https://www.cypruspolicenews.com/wp-json/wp/v2/posts?categories=5` (`200 OK`) & `https://data.gov.cy/sites/default/files/police_stations_f.zip` (`200 OK`)
- **CyNAP `waze_alerts`:** `https://fixcyprus.cy/gnosis/open/api/nap/datasets/waze_alerts/` (`200 OK`; `waze_traffic/` returned `503`)
- **Department of Antiquities:** `https://www.mcw.gov.cy/mcw/da/da.nsf/index_en/index_en?OpenDocument` (`200 OK`) + `data.gov.cy` Controlled Areas / Monuments / Museums CSVs (`200 OK`)
