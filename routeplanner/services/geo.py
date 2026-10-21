"""Small geometry helpers: distances, polyline decoding, route-to-point matching."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

EARTH_RADIUS_MILES = 3958.8
METERS_PER_MILE = 1609.344
MILES_PER_DEG_LAT = math.radians(1) * EARTH_RADIUS_MILES  # ~69.1


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(h)))


def _short_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance; accurate to well under 0.1% for the sub-mile steps of a route polyline."""
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return MILES_PER_DEG_LAT * math.sqrt(dlat * dlat + dlon * dlon)


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google encoded polyline into [(lat, lon), ...]."""
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    factor = 10**precision
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= length:
                    raise ValueError("Malformed polyline")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coords.append((lat / factor, lon / factor))
    return coords


def encode_polyline(coords: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def simplify(coords: list[tuple[float, float]], tolerance_deg: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker simplification (iterative). Used only for the map/response size."""
    if len(coords) < 3:
        return list(coords)
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    tol2 = tolerance_deg * tolerance_deg
    while stack:
        start, end = stack.pop()
        (ay, ax), (by, bx) = coords[start], coords[end]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, best_i = -1.0, -1
        for i in range(start + 1, end):
            py, px = coords[i]
            if seg2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d2 > best:
                best, best_i = d2, i
        if best > tol2:
            keep[best_i] = True
            stack.append((start, best_i))
            stack.append((best_i, end))
    return [c for c, k in zip(coords, keep) if k]


@dataclass(frozen=True)
class RouteMatch:
    route_mile: float  # distance along the route of the closest route point
    off_route_miles: float  # straight-line distance from the point to the route


class RouteLine:
    """A route polyline with cumulative mileage and a grid index for fast matching.

    Vertices closer than `min_spacing_miles` to the previous kept vertex are
    dropped to keep matching fast. Points are matched to the nearest kept
    vertex; with the full geometry returned by OSRM/ORS (vertices well under a
    mile apart) the error is a fraction of a mile.
    """

    CELL_DEG = 0.1  # ~7 x 5 mile cells

    def __init__(self, coords: list[tuple[float, float]], total_miles: float | None = None,
                 min_spacing_miles: float = 0.25):
        if len(coords) < 2:
            raise ValueError("A route needs at least two points")
        points = [coords[0]]
        cumulative = [0.0]
        last_lat, last_lon = coords[0]
        for lat, lon in coords[1:]:
            d = _short_distance_miles(last_lat, last_lon, lat, lon)
            if d >= min_spacing_miles:
                points.append((lat, lon))
                cumulative.append(cumulative[-1] + d)
                last_lat, last_lon = lat, lon
        if points[-1] != coords[-1]:
            points.append(coords[-1])
            cumulative.append(cumulative[-1] + _short_distance_miles(last_lat, last_lon, *coords[-1]))

        # Scale geometric mileage to the provider's road distance so that
        # "route mile" positions add up to the reported trip distance.
        geometric = cumulative[-1] or 1.0
        scale = (total_miles / geometric) if total_miles else 1.0
        self.points = points
        self.cumulative = [c * scale for c in cumulative]
        self.length_miles = self.cumulative[-1]

        self._grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (lat, lon) in enumerate(points):
            self._grid[self._cell(lat, lon)].append(i)

    @classmethod
    def _cell(cls, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / cls.CELL_DEG), math.floor(lon / cls.CELL_DEG))

    def match(self, lat: float, lon: float, max_miles: float) -> RouteMatch | None:
        """Closest route vertex to (lat, lon) if it is within `max_miles`.

        Candidate vertices come from the surrounding grid cells; they are
        compared with a cheap flat-earth distance and only the winner gets an
        exact haversine distance.
        """
        cos_lat = max(math.cos(math.radians(lat)), 0.2)
        pad_lat = math.ceil(max_miles / (self.CELL_DEG * MILES_PER_DEG_LAT))
        pad_lon = math.ceil(max_miles / (self.CELL_DEG * MILES_PER_DEG_LAT * cos_lat))
        cy, cx = self._cell(lat, lon)
        points = self.points
        best_d2, best_i = math.inf, -1
        for dy in range(-pad_lat, pad_lat + 1):
            for dx in range(-pad_lon, pad_lon + 1):
                for i in self._grid.get((cy + dy, cx + dx), ()):
                    plat, plon = points[i]
                    d2 = (plat - lat) ** 2 + ((plon - lon) * cos_lat) ** 2
                    if d2 < best_d2:
                        best_d2, best_i = d2, i
        if best_i < 0:
            return None
        distance = haversine_miles(lat, lon, *points[best_i])
        if distance > max_miles:
            return None
        return RouteMatch(route_mile=self.cumulative[best_i], off_route_miles=distance)
