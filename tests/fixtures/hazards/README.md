# Hazard fixtures (real, fetched once on 27 November 2026)

| File | What | From |
| --- | --- | --- |
| `emsc_2025-11-12.json` | EMSC FDSN events 12 and 13 Nov 2025 around Cyprus: the M5.2 (09:31 UTC) and M5.3 (14:23 UTC) about 90 km south-west of Nicosia, with aftershocks | `seismicportal.eu/fdsnws/event/1/query?format=json&minlat=34.4&maxlat=35.8&minlon=32&maxlon=34.7&starttime=2025-11-12&endtime=2025-11-14` |
| `emsc_30d_2026-11-27.json` | The 30 days before 27 Nov 2026: 29 small quakes, largest M3.2 at 106 km, none felt in the city (nothing should page) | same, `starttime=2026-10-28` |
| `emsc_felt_counts_2025-11-12.json` | Felt-report counts for the two M5 quakes (3,491 and 4,478) | `seismicportal.eu/testimonies-ws/api/search?unids=[...]&includeTestimonies=false&format=json` |
| `emsc_felt_20251112_0000180.zip` | The felt reports of the M5.2 (lon, lat, time, source, raw and corrected intensity), repacked without EMSC's PDF; 817 of them within 25 km of Nicosia | same with `includeTestimonies=true` (EMSC sends a zip); CC BY 4.0 |
| `meteoalarm_2024-08-13.xml` | Two Yellow high-temperature warnings, Cyprus (EMMA CY001) | Wayback copy of `feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-cyprus` |
| `meteoalarm_2025-07-25.xml` | Three Orange (Severe) high-temperature warnings, 24 to 26 July 2025 | same |
| `meteoalarm_2026-01-24.xml` | A Yellow thunderstorm warning, 23 to 24 Jan 2026 | same |
| `meteoalarm_empty_2026-11-27.xml` | The live feed with no warning in force | the live feed |
| `meteoalarm_api_cyprus_empty.json` | The JSON API for Cyprus, empty | `feeds.meteoalarm.org/api/v1/warnings/feeds-cyprus` |
| `meteoalarm_api_greece_2026-11-22.json` | Two real Greek wind warnings in the JSON API's shape (full CAP: headline, description, awareness level and type), for the JSON parser | `.../feeds-greece`, trimmed to two |
| `effis_fwi_2025-07-23.png` | EFFIS fire weather index classes over Cyprus on 23 July 2025 (Extreme and Very extreme) | WMS 1.1.1 GetMap `LAYERS=mf010.fwi&SRS=EPSG:4326&BBOX=32.2,34.5,34.7,35.75&WIDTH=250&HEIGHT=125&TIME=2025-07-23` |
| `effis_fwi_2026-11-27.png` | The same for 27 Nov 2026 (Moderate and High, a little Very high) | same, `TIME=2026-11-27` |

EFFIS legend colours (RGBA) to classes: (156,255,192) Low under 11.2; (205,226,78) Moderate 11.2 to 21.3;
(230,172,0) High 21.3 to 38; (217,112,16) Very high 38 to 50; (173,6,14) Extreme 50 to 70; (58,0,21)
Very extreme over 70; transparent is no data (the sea). `GetFeatureInfo` on `mf010.query` answers
with empty values, so the classes are read from the image.

`data/cyprus_areas.json` is the island and its six districts (geoBoundaries gbOpen CYP ADM0 and
ADM1, simplified, from OpenStreetMap, ODbL), for the warning area and the quake's district.
