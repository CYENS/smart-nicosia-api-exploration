# Measured weather and reference air

Addition 1 of [what the twin should add next](next-additions-research.md): real instruments
beside the twin's modelled surfaces. Status: sources probed and fixtures saved (27 November
2026, about 22:15 Cyprus time); the code is being built in this branch.

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
