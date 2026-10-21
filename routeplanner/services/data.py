"""Loading of the supplied fuel-price CSV and the offline US place gazetteer.

The fuel-price CSV has no coordinates, only City and State. Each station is
located at the centroid of its city, taken from `data/us_places.csv` (built
from public US ZIP-code centroids by `manage.py build_places`). Both files are
read once per process and kept in memory.
"""
from __future__ import annotations

import csv
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from .errors import FuelDataUnavailable
from .geo import haversine_miles

logger = logging.getLogger(__name__)

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_BY_NAME = {name.upper(): code for code, name in US_STATES.items()}

REQUIRED_COLUMNS = (
    "OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price",
)

_PREFIXES = {"ST": "SAINT", "STE": "SAINTE", "FT": "FORT", "MT": "MOUNT", "PT": "POINT"}


def normalize_city(name: str) -> str:
    """Canonical form used to join station cities, user input and the gazetteer."""
    text = re.sub(r"[.'`]", "", name.upper().strip())
    text = re.sub(r"[-/,]", " ", text)
    tokens = text.split()
    if tokens and tokens[0] in _PREFIXES:
        tokens[0] = _PREFIXES[tokens[0]]
    return " ".join(tokens)


def normalize_state(value: str) -> str | None:
    text = value.strip().upper().replace(".", "")
    if text in US_STATES:
        return text
    return STATE_BY_NAME.get(text)


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


class PlaceIndex:
    CELL_DEG = 1.0

    def __init__(self, places: list[Place]):
        self.places = places
        self._by_key: dict[tuple[str, str], Place] = {}
        self._by_compact_key: dict[tuple[str, str], Place] = {}
        self._grid: dict[tuple[int, int], list[Place]] = defaultdict(list)
        for place in places:
            key = normalize_city(place.name)
            self._by_key.setdefault((key, place.state), place)
            self._by_compact_key.setdefault((key.replace(" ", ""), place.state), place)
            self._grid[self._cell(place.lat, place.lon)].append(place)

    @classmethod
    def _cell(cls, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / cls.CELL_DEG), math.floor(lon / cls.CELL_DEG))

    def lookup(self, city: str, state: str) -> Place | None:
        key = normalize_city(city)
        return self._by_key.get((key, state)) or self._by_compact_key.get((key.replace(" ", ""), state))

    def nearest(self, lat: float, lon: float) -> tuple[Place, float] | None:
        cy, cx = self._cell(lat, lon)
        best: tuple[Place, float] | None = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for place in self._grid.get((cy + dy, cx + dx), ()):
                    d = haversine_miles(lat, lon, place.lat, place.lon)
                    if best is None or d < best[1]:
                        best = (place, d)
        return best


@dataclass(frozen=True)
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int
    price: float  # USD per gallon (lowest listed price for this station ID)
    price_rows: int  # how many CSV rows were listed for this station ID
    lat: float
    lon: float


@dataclass
class StationIndex:
    stations: list[Station]
    stats: dict
    CELL_DEG = 0.5

    def __post_init__(self):
        self.grid: dict[tuple[int, int], list[Station]] = defaultdict(list)
        for station in self.stations:
            self.grid[(math.floor(station.lat / self.CELL_DEG), math.floor(station.lon / self.CELL_DEG))].append(station)


@lru_cache(maxsize=1)
def get_places() -> PlaceIndex:
    return load_places(Path(settings.US_PLACES_CSV))


@lru_cache(maxsize=1)
def get_stations() -> StationIndex:
    return load_stations(Path(settings.FUEL_PRICES_CSV), get_places())


def clear_caches() -> None:
    get_places.cache_clear()
    get_stations.cache_clear()


def load_places(path: Path) -> PlaceIndex:
    if not path.is_file():
        raise FuelDataUnavailable(f"US place gazetteer not found at {path.name}; station locations cannot be resolved.")
    places = []
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                places.append(Place(row["city"], row["state"], float(row["lat"]), float(row["lon"])))
    except (KeyError, ValueError, csv.Error) as exc:
        raise FuelDataUnavailable(f"US place gazetteer {path.name} is malformed: {exc}") from exc
    if not places:
        raise FuelDataUnavailable(f"US place gazetteer {path.name} is empty.")
    return PlaceIndex(places)


def load_stations(path: Path, places: PlaceIndex) -> StationIndex:
    """Read the supplied fuel-price CSV.

    * Rows outside the 50 US states + DC (the file also lists Canadian
      provinces) are excluded: the trip is within the USA and those prices
      may be in a different currency/unit.
    * Several station IDs appear on multiple rows with different prices; the
      lowest listed retail price is used for that station.
    """
    if not path.is_file():
        raise FuelDataUnavailable(f"Fuel price file not found: {path.name}")
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise FuelDataUnavailable(f"Fuel price file is missing columns: {', '.join(missing)}")
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise FuelDataUnavailable(f"Fuel price file could not be read: {exc}") from exc

    stats = {"rows_total": len(rows), "rows_outside_us": 0, "rows_invalid_price": 0,
             "stations_without_location": 0}
    best: dict[int, dict] = {}
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        state = row["State"].strip().upper()
        if state not in US_STATES:
            stats["rows_outside_us"] += 1
            continue
        try:
            opis_id = int(row["OPIS Truckstop ID"])
            price = float(row["Retail Price"])
            rack_id = int(row["Rack ID"]) if row["Rack ID"].strip() else 0
        except ValueError:
            stats["rows_invalid_price"] += 1
            continue
        if not math.isfinite(price) or price <= 0:
            stats["rows_invalid_price"] += 1
            continue
        counts[opis_id] += 1
        if opis_id not in best or price < best[opis_id]["price"]:
            best[opis_id] = {"row": row, "price": price, "rack_id": rack_id, "state": state}

    stations = []
    for opis_id, item in best.items():
        row = item["row"]
        city = " ".join(row["City"].split())
        place = places.lookup(city, item["state"])
        if place is None:
            stats["stations_without_location"] += 1
            logger.warning("No location for station %s (%s, %s)", opis_id, city, item["state"])
            continue
        stations.append(Station(
            opis_id=opis_id,
            name=" ".join(row["Truckstop Name"].split()),
            address=" ".join(row["Address"].split()),
            city=city,
            state=item["state"],
            rack_id=item["rack_id"],
            price=item["price"],
            price_rows=counts[opis_id],
            lat=place.lat,
            lon=place.lon,
        ))
    if not stations:
        raise FuelDataUnavailable("Fuel price file contains no usable US stations.")
    stats["stations_usable"] = len(stations)
    logger.info("Loaded fuel prices: %s", stats)
    return StationIndex(stations=stations, stats=stats)
