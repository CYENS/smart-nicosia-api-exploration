# Measured weather and reference air

Addition 1 of [what the twin should add next](next-additions-research.md): real instruments
beside the twin's modelled surfaces. Code in `chatbot/environment/stations.py` (the feeds, the
memory, the comparison), `chatbot/environment/station_kinds.py` (the two kinds) and, on the
page, `lib/environment/stations.ts` and `components/environment/stations.tsx`.

## Where it shows

- **The Layers sheet, Environment, "Measuring stations"**: the Met stations as blue dots with
  their temperature, the two official air stations as rings with their NO₂. It follows the
  clock: a moment the memory holds nothing for shows the stations faded, "no reading then".
- **The Outside card, "Measured now"**: the centre's thermometer with its age, the rain of the
  last hour, the wind measured at 10 m and the traffic station's newest hour; "Stations" puts
  them on the map.
- **The inspector**: `met-LEFKOSIA` and `aq-CY0004A` with their readings, their 24 hours (the
  temperature every 10 minutes, NO₂ by the hour) and the city sensors compared with them; a city
  air sensor gets "Against the official stations, past 7 days".
- **The surfaces**: the air and temperature surfaces rest on the official stations where that
  is honest, and the legend names them (see the environment twin).
- **The chat**: `measured_stations` (the instruments beside the model, with a day's chart),
  `compare_with_reference` (each city sensor's bias, RMSE and correlation against its nearest
  official station), and `get_weather`'s `measured`.

## How it works

- **Reading**: the server's poller reads the Met file every 10 minutes and the EEA files every
  20, each with `If-None-Match`, so a file comes down again only once it changed (the Met file
  every 10 minutes, the EEA files about hourly). A copy of each is kept in
  `data/chatbot_cache/stations/` for a restart. Pages and tools never ask the services: they
  read what the poller read and the memory recorded.
- **The memory**: every reading goes into the `station_readings` table (station, key, ts,
  value, source) of the twin's memory, so the clock replays the stations and `run_sql` reaches
  them. Each EEA file holds the year so far: its first read fills 35 days, later reads the last
  3 (unverified hours can still change). The Met file holds only now, so its history grows from
  when the twin starts reading it.
- **At a moment**: a station's reading is its newest within half an hour (Met) or within three
  hours after its hour ended (EEA); older, the station shows as unknown, never as its last value.
- **The comparison**: each city sensor's hourly readings from the memory against its nearest
  official station's (NO₂, O₃ and PM10 against the traffic or residential station, temperature
  against the nearest Met thermometer), over 7 days: bias, bias in %, RMSE, r and the hours
  paired. At least 12 hours, or it says too few. A sensor kilometres from its reference also
  differs for real, which the answers say.

## Limits

- The Met readings start when the twin first reads them, so the first day's comparisons of
  temperature say "too few hours".
- Only the two Nicosia air stations: Agia Marina (CY0002R, the only PM2.5 reference) is 35 km
  out and left for later; the national network's own real-time API needs a letter.
- The EEA's hours are not yet verified and run two to three hours behind.

## Department of Meteorology stations

- Feed: `https://www.dom.org.cy/AWS/OpenData/CyDoM.xml`, one XML file of about 105 KB, no key.
  Apache serves `Last-Modified` and `ETag`, so conditional requests work.
- Seen: readings stamped `2026-11-27 22:00 (Local Time)` in a file modified at 22:05 local:
  every 10 minutes, 5 to 15 minutes behind. Local time has no offset (read as Asia/Nicosia).
- Within 15 km of the centre: `LEFKOSIA` (35.16437, 33.35607), `ATHALASSA` (35.14096,
  33.39658), `VISITOR` (35.14448, 33.40339, by the Athalassa park visitor centre), `KEPNIC`
  (35.15239, 33.31228, a rain gauge only) and `MAMMARI` (35.17236, 33.21488, 13 km west, the
  nearest global radiation meter).
- Units: air temperature at 1.2 m and at 5 cm in °C, humidity %, rain in mm per 10 minutes (and
  24 hours at some), **wind at 10 m in knots** (at 2 m in m/s), direction in degrees, global and
  direct radiation in W/m², pressure in hPa. Station sets differ: KEPNIC has one reading.
- Reuse terms: the Department lists the feed on its home page as "Automatic Weather Station
  Readings XML API". The file says "Weather Data Copyright The Cyprus Department of
  Meteorology" and that it is "intended as a guide only". No open licence is stated anywhere on
  dom.org.cy or data.gov.cy (checked 27 November 2026), and nothing forbids reuse. The twin
  credits the Department and quotes the note; a formal confirmation can be asked of the
  Department (the address on its About page).

## Reference air: the European Environment Agency's up-to-date files

- Listing: `POST https://eeadmz1-downloads-api-appservice.azurewebsites.net/ParquetFile/urls`
  with `{"countries":["CY"],"cities":["Lefkosia"],"pollutants":[],"dataset":1,"source":"Api"}`
  returns a CSV (with a byte order mark) of Parquet URLs. Lefkosia has two stations:
  - `CY0004A`: SO2 (1), PM10 (5), O3 (7), NO2 (8), NOx (9), CO (10), benzene (20).
  - `CY0006A`: SO2, O3, NO2, NOx, CO. No particles.
- Which is which: the EEA's station register (discomap) and the reporting archive (CDR) did not
  answer that evening. The national 2020 annual report (Department of Labour Inspection, table
  of station characteristics) gives the Nicosia traffic station at the Strovolos police station,
  35°09'07" N 33°20'52" E, and the residential station at the Apostolos Loukas school,
  Strovolos, 35°07'37" N 33°19'54" E. The data settle the codes: CY0004A has the higher NO2
  (23 against 16 µg/m³ this year) and the lower ozone (64 against 76), and carries PM10 and
  benzene, so **CY0004A is the traffic station and CY0006A the residential one**.
- Files: `https://eeadmz1batchservice02.blob.core.windows.net/airquality-p/CY/SPO-CY0004A_00008_100.parquet`
  and so on, one per station and pollutant, about 440 KB each, holding the year so far, one row
  per hour: Samplingpoint, Pollutant, Start, End, Value (decimal), Unit (`ug.m-3`), AggType
  (`hour`), Validity (1 valid, -1 not), Verification (3, not verified), ResultTime. Azure serves
  `ETag`, so a file is downloaded again only once it changes (about hourly).
- Times: the EEA gives hourly data in UTC+1. The newest hour seen was 18:00 to 19:00 (19:00 to
  20:00 in Nicosia), written at 20:15 (UTC+1), the blob modified at 21:28 Nicosia time: two to
  three hours behind now.
- Licence: EEA data, reuse with credit (CC BY 4.0, the EEA's standard terms). The national
  network's own real-time API (`https://www.airquality.dli.mlsi.gov.cy/`) refuses plain clients
  (403) and needs the Department of Labour Inspection's agreement.

## Fixtures

`tests/fixtures/stations/`: the whole Met XML of 22:00 on 27 November, the EEA listing, and the
five Lefkosia Parquet files used (NO2 and O3 at both stations, PM10 at CY0004A) cut to the hours
from 18 November.
