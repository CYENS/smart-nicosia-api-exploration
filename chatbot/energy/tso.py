"""The Transmission System Operator's generation page: what it publishes and how to read it.

The operator (ΔΣΜΚ, tsoc.org.cy) publishes the island grid's day on an ordinary web page, not an
API: a WordPress page whose chart reads a hidden table, `#production_graph_data`, with a row
every 15 minutes (local Nicosia time) and these columns, in MW:

- the available capacity of the conventional (oil-fired) stations,
- wind generation on the transmission system,
- the operator's estimate of distributed generation (rooftop solar, solar parks on the
  distribution network and a few small biomass plants),
- total demand,
- conventional generation.

Rows after the latest publication carry only the capacity. The archive page takes
`?startdt=DD-MM-YYYY&enddt=+Ndays` (1, 3, 7 or 15 days) and has the same table. A second table
lists the installed capacity by technology.

Everything here is pure: `parse_page` turns the HTML into readings or raises `LayoutChanged`
with what it could not find, so the caller keeps its last good copy. Columns are found by what
their headers mean (Greek or English), never by position.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Nicosia")

PAGE_URL = "https://tsoc.org.cy/electrical-system/total-daily-system-generation-on-the-transmission-system/"
ARCHIVE_URL = ("https://tsoc.org.cy/electrical-system/"
               "archive-total-daily-system-generation-on-the-transmission-system/")
ARCHIVE_SPANS = (1, 3, 7, 15)  # the days the archive's form offers

DATA_TABLE = "production_graph_data"
INSTALLED_TABLE = "production_graph_static_data2"

# What each column means, by words its header holds (accents and case ignored). Checked in this
# order, each header taking the first meaning still free: the capacity header also says
# "conventional", so it must be claimed before conventional generation.
COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("time", ("timestamp", "χρονοσημ", "ημερομην", "time", "date")),
    ("capacity_mw", ("ικανοτητα", "capacity", "available", "διαθεσιμ")),
    ("wind_mw", ("αιολικ", "wind")),
    ("distributed_mw", ("διεσπαρμ", "distributed", "embedded")),
    ("demand_mw", ("ζητηση", "demand", "load")),
    ("conventional_mw", ("συμβατικ", "conventional", "thermal")),
)
INSTALLED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("wind_mw", ("αιολικ", "wind")),
    ("solar_mw", ("φωτοβολτα", "photovolta", "solar", "pv")),
    ("biomass_mw", ("βιομαζ", "biomass")),
    ("conventional_mw", ("συμβατικ", "conventional", "thermal")),
)
VALUES = ("capacity_mw", "wind_mw", "distributed_mw", "demand_mw", "conventional_mw")
# What a value can plausibly be, MW: the island peaks near 1,300 MW; anything outside is a
# misread cell, kept as unknown.
PLAUSIBLE = {"demand_mw": (50.0, 2500.0), "capacity_mw": (0.0, 3000.0), "wind_mw": (0.0, 600.0),
             "distributed_mw": (0.0, 2500.0), "conventional_mw": (0.0, 2500.0)}
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M")


class LayoutChanged(ValueError):
    """The page no longer looks the way the twin reads it. The message says what is missing."""


@dataclass(frozen=True)
class Reading:
    """One quarter hour of the island grid, MW. `t` is the start of the quarter hour (aware)."""
    t: datetime
    demand_mw: float | None
    wind_mw: float | None
    distributed_mw: float | None
    conventional_mw: float | None
    capacity_mw: float | None = None

    @property
    def published(self) -> bool:
        """The operator has filled the row (later rows carry only the capacity)."""
        return self.demand_mw is not None

    def public(self) -> dict:
        d = asdict(self)
        d["t"] = self.t.astimezone(TZ).isoformat(timespec="minutes")
        return d


@dataclass
class Page:
    readings: list[Reading]  # published rows only, in time order
    installed: dict[str, float] = field(default_factory=dict)  # MW by technology
    columns: dict[str, str] = field(default_factory=dict)  # meaning -> the header as written
    skipped: int = 0  # rows that could not be read (a bad time, an impossible value)
    pending: int = 0  # rows of the day not yet published

    @property
    def latest(self) -> Reading | None:
        return self.readings[-1] if self.readings else None


# -- HTML ------------------------------------------------------------------------------------


class _Tables(HTMLParser):
    """Every table on the page: its id, header cells and rows of cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._stack: list[dict] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._head = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            t = {"id": a.get("id") or "", "headers": [], "rows": []}
            self.tables.append(t)
            self._stack.append(t)
        elif not self._stack:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._head = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            if self._head and not t["rows"] and (self._row is None or not self._row):
                t["headers"].append(text)
            elif self._row is not None:
                self._row.append(text)
            else:  # a cell outside any row (some themes skip the <tr> in the head)
                t["headers"].append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                t["rows"].append(self._row)
            self._row = None
        elif tag == "table":
            self._stack.pop()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _plain(text: str) -> str:
    """Lower case without accents, for matching headers by their words."""
    s = unicodedata.normalize("NFD", text or "").casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _meanings(headers: list[str], meanings=COLUMNS) -> dict[str, int]:
    """Which column holds what: meaning -> index."""
    out: dict[str, int] = {}
    taken: set[int] = set()
    plain = [_plain(h) for h in headers]
    for name, words in meanings:
        for i, h in enumerate(plain):
            if i not in taken and any(w in h for w in words):
                out[name] = i
                taken.add(i)
                break
    return out


_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _number(text: str) -> float | None:
    m = _NUMBER.search((text or "").replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _wall(text: str) -> datetime | None:
    text = " ".join((text or "").split())
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _aware(wall: datetime, seen: set[datetime]) -> datetime | None:
    """A Nicosia wall time as an instant. On the night clocks go back the repeated hour comes
    twice: the second time it is the later instant. A wall time that never happened (clocks
    going forward) is skipped."""
    for fold in (0, 1):
        t = wall.replace(tzinfo=TZ, fold=fold)
        utc = t.astimezone(timezone.utc)
        if utc.astimezone(TZ).replace(tzinfo=None) != wall:
            return None  # a time that doesn't exist that day
        if utc not in seen:
            seen.add(utc)
            return utc
    return None


def _find_table(tables: list[dict]) -> dict | None:
    by_id = next((t for t in tables if t["id"] == DATA_TABLE), None)
    if by_id is not None:
        return by_id
    # The id changed: any table with a time column and a demand column.
    for t in tables:
        m = _meanings(t["headers"])
        if "time" in m and "demand_mw" in m:
            return t
    return None


def parse_page(html: str) -> Page:
    """The grid's readings on a generation page (today's or the archive's)."""
    if not html or "<table" not in html.lower():
        raise LayoutChanged("the page has no tables")
    p = _Tables()
    p.feed(html)
    p.close()
    table = _find_table(p.tables)
    if table is None:
        raise LayoutChanged(f"no table {DATA_TABLE!r} and none with a time and a demand column")
    cols = _meanings(table["headers"])
    missing = [name for name in ("time", "demand_mw") if name not in cols]
    if missing:
        raise LayoutChanged(f"the data table has no {' or '.join(missing)} column "
                            f"(headers: {table['headers'][:8]})")
    readings: list[Reading] = []
    seen: set[datetime] = set()
    skipped = pending = 0
    for row in table["rows"]:
        if len(row) <= cols["time"]:
            skipped += 1
            continue
        wall = _wall(row[cols["time"]])
        t = _aware(wall, seen) if wall else None
        if t is None:
            skipped += 1
            continue
        vals: dict[str, float | None] = {}
        for name in VALUES:
            i = cols.get(name)
            v = _number(row[i]) if i is not None and i < len(row) else None
            lo, hi = PLAUSIBLE[name]
            if v is not None and not lo <= v <= hi:
                v = None
                skipped += 1 if name == "demand_mw" else 0
            vals[name] = v
        r = Reading(t=t, **vals)
        if r.published:
            readings.append(r)
        else:
            pending += 1
    readings.sort(key=lambda r: r.t)
    if not readings and not pending:
        raise LayoutChanged("the data table has no rows the twin can read")
    installed: dict[str, float] = {}
    inst = next((t for t in p.tables if t["id"] == INSTALLED_TABLE), None)
    if inst is not None:
        for row in inst["rows"]:
            if len(row) >= 2:
                m = _meanings([row[0]], INSTALLED)
                v = _number(row[1])
                if m and v is not None:
                    installed[next(iter(m))] = v
    return Page(readings=readings, installed=installed,
                columns={k: table["headers"][i] for k, i in cols.items() if i < len(table["headers"])},
                skipped=skipped, pending=pending)


def archive_url(first: date, days: int) -> str:
    """The archive page for `days` days from `first` (the form's own choices: 1, 3, 7 or 15)."""
    if days not in ARCHIVE_SPANS:
        raise ValueError(f"the archive offers {ARCHIVE_SPANS} days")
    return f"{ARCHIVE_URL}?startdt={first:%d-%m-%Y}&enddt=%2B{days}days"
