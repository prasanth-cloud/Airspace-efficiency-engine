"""Phase 6: Optimised ICAO flight-plan strings.

Builds an ICAO 4444 FPL message whose route is the great circle between origin
and destination written as DCT legs between lat/lon waypoints, flown at the
engine's best flight level and, when a Phase 5 advisory exists, at the
advised cruise speed.

These strings are advisory proposals in a format dispatch systems can parse.
The aircraft type and wake category come from the engine's type lookup; when
the type is unknown it is filed as ZZZZ with TYP/. Equipment is not in the
public feed, so a generic equipment string is used; dispatchers must replace
it before any real filing.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from .aircraft import performance_for
from .geo import M_TO_NM, MS_TO_KNOTS, format_icao_latlon, great_circle_path, haversine_m

WAYPOINT_SPACING_M = 120 * 1852  # one waypoint about every 120 NM
DEFAULT_LEVEL_FL = 350
EQUIPMENT = "SDFGRWY/S"


def _icao_code(code: Optional[str]) -> str:
    if code and len(code) == 4 and code.isalpha():
        return code.upper()
    if code and len(code) == 3 and code.isalpha():
        from .airports import lookup
        a = lookup(code)
        if a:
            return a.icao
    return "ZZZZ"


def build_flight_plan(row: dict, advised_kt: Optional[int] = None,
                      from_present_position: bool = False, now: Optional[datetime] = None) -> str:
    """Return an ICAO FPL string for a stored flight-metrics row.

    Raises ValueError when the flight has no resolved destination.
    """
    if row.get("dest_lat") is None or row.get("dest_lon") is None:
        raise ValueError(f"No destination known for {row.get('callsign')}; cannot build a flight plan")
    now = now or datetime.now(timezone.utc)

    if from_present_position or row.get("orig_lat") is None:
        start = (row["lat"], row["lon"])
        dep, dep_note = "ZZZZ", f"DEP/{format_icao_latlon(*start)}"
    else:
        start = (row["orig_lat"], row["orig_lon"])
        dep = _icao_code(row.get("origin"))
        dep_note = "" if dep != "ZZZZ" else f"DEP/{format_icao_latlon(*start)}"
    end = (row["dest_lat"], row["dest_lon"])
    dest = _icao_code(row.get("destination"))
    dest_note = "" if dest != "ZZZZ" else f"DEST/{format_icao_latlon(*end)}"

    level = row.get("best_level_fl") or DEFAULT_LEVEL_FL
    tas_kt = advised_kt or (round(row["tas_ms"] * MS_TO_KNOTS) if row.get("tas_ms") else 450)

    path = great_circle_path(*start, *end, step_m=WAYPOINT_SPACING_M)
    waypoints = [format_icao_latlon(la, lo) for la, lo in path[1:-1]]
    route = f"N{tas_kt:04d}F{level:03d} " + (" ".join(f"DCT {w}" for w in waypoints) + " DCT").strip()
    if not waypoints:
        route = f"N{tas_kt:04d}F{level:03d} DCT"

    dist_nm = haversine_m(*start, *end) * M_TO_NM
    eet_min = math.ceil(dist_nm / max(tas_kt, 1) * 60)
    eet = f"{eet_min // 60:02d}{eet_min % 60:02d}"

    type_code = (row.get("aircraft_type") or "").upper()
    perf, known = performance_for(type_code)
    if known:
        item9, type_note = f"{type_code}/{perf.wake}", ""
    else:
        item9, type_note = f"ZZZZ/{perf.wake}", f"TYP/{type_code or 'UNKNOWN'}"

    item18 = " ".join(x for x in [
        "PBN/A1B1C1D1", dep_note, dest_note, type_note,
        f"RMK/AIRSPACE EFFICIENCY ENGINE ADVISORY GREAT CIRCLE FL{level:03d}"
        + (f" SPEED CONTROL N{advised_kt:04d}" if advised_kt else ""),
    ] if x)

    callsign = row["callsign"].upper()
    return (f"(FPL-{callsign}-IS\n"
            f"-{item9}-{EQUIPMENT}\n"
            f"-{dep}{now:%H%M}\n"
            f"-{route}\n"
            f"-{dest}{eet}\n"
            f"-{item18})")
