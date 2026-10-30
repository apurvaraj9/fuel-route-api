"""Normalise highway names so station addresses can be matched to the route.

Station addresses in the price list look like "I-44, EXIT 283 & US-69" or
"SR-703/SR-292"; routing services describe roads as "I 44;US 69" (OSRM `ref`)
or "Interstate 44, I 44" (OpenRouteService step names). Both are reduced to
the same keys: "I-44", "US-69", "SR-292" (any state route), "CR-466", "FM-1960".
"""

from __future__ import annotations

import re

from .states import US_STATES

_TYPE_ALIASES = {
    "I": "I",
    "IH": "I",
    "INTERSTATE": "I",
    "US": "US",
    "USHWY": "US",
    "SR": "SR",
    "SH": "SR",
    "ST": "SR",
    "HWY": "SR",
    "HWYY": "SR",
    "HIGHWAY": "SR",
    "RT": "SR",
    "RTE": "SR",
    "ROUTE": "SR",
    "STATE": "SR",
    "CR": "CR",
    "FM": "FM",
}
_NOT_ROADS = {"EXIT", "MM", "MILE", "MILEPOST", "MARKER", "POST", "BOX", "SUITE", "STE"}

# A road token with an optional "/12/14" list of more numbers of the same road type.
_ADDRESS_TOKEN = re.compile(r"\b([A-Z]+)[- ]?(\d{1,4}[A-Z]?)\b((?:\s*/\s*\d{1,4}[A-Z]?\b)*)")
_REF_TOKEN = re.compile(r"\b(INTERSTATE|[A-Z]{1,3})\s*-?\s*(\d{1,4}[A-Z]?)\b")


def _road_type(prefix: str) -> str | None:
    prefix = prefix.upper()
    if prefix in _NOT_ROADS:
        return None
    if prefix in _TYPE_ALIASES:
        return _TYPE_ALIASES[prefix]
    if prefix in US_STATES:  # "WI 21", "CA 99" style state routes
        return "SR"
    return None


def parse_address_highways(address: str) -> tuple[str, ...]:
    """'I-39, EXIT 111/US-51 & SR-72' -> ('I-39', 'US-51', 'SR-72')."""
    found: list[str] = []
    for match in _ADDRESS_TOKEN.finditer(address.upper()):
        road_type = _road_type(match.group(1))
        if road_type is None:
            continue
        numbers = [match.group(2)] + re.findall(r"\d{1,4}[A-Z]?", match.group(3) or "")
        for number in numbers:
            key = f"{road_type}-{number}"
            if key not in found:
                found.append(key)
    return tuple(found)


def parse_route_refs(text: str | None) -> frozenset[str]:
    """'I 80;US 6' or 'Interstate 80, I 80' -> {'I-80', 'US-6'}."""
    if not text:
        return frozenset()
    refs = set()
    for match in _REF_TOKEN.finditer(text.upper()):
        road_type = _road_type(match.group(1))
        if road_type is not None:
            refs.add(f"{road_type}-{match.group(2)}")
    return frozenset(refs)
