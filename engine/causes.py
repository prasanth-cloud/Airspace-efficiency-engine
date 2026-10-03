"""Why a flight wastes fuel: attribute each scored flight's excess CO2 to a cause.

A flight's excess burn is split into a lateral part (not flying the wind-aware
great circle) and a vertical part (not at the best flight level). The lateral
part is attributed to the first cause the evidence supports:

1. **congestion**: the destination has an FAA delay program, ground stop or
   arrival delay (FAA NAS Status), or the engine's own arrival queue gives
   this flight at least 5 minutes of delay, and the flight is within 250 NM of
   it, where controllers stretch paths to meter arrivals.
2. **weather**: a convective SIGMET (NOAA Aviation Weather Center) covers the
   aircraft or lies within 50 km of its great circle to the destination.
3. **airspace**: that great circle crosses a major military warning area or
   restricted zone (boundaries are approximate and their activation schedules
   are not checked, so this is "airspace in the way", not "airspace active").
4. **routing**: none of the above. Usually ATC route structure, airline
   flight-planning choices, or something the engine cannot see.

The vertical part is attributed to **flight_level**.

Every external feed is optional. When one cannot be reached, its cause is
simply never assigned, and the run says which feeds were used.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from .geo import great_circle_point, haversine_m

log = logging.getLogger(__name__)

FAA_NAS_STATUS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
AWC_SIGMET_URL = "https://aviationweather.gov/api/data/airsigmet"
FEED_CACHE_S = 10 * 60
REQUEST_TIMEOUT_S = 20

CONGESTION_RADIUS_M = 250 * 1852
QUEUE_DELAY_MIN = 5.0
WEATHER_BUFFER_M = 50_000
PATH_STEP_M = 25_000

CAUSES = ("congestion", "weather", "airspace", "routing", "flight_level")
CAUSE_LABELS = {
    "congestion": "Airport congestion",
    "weather": "Weather (convection)",
    "airspace": "Military / restricted airspace",
    "routing": "ATC routing or unexplained",
    "flight_level": "Non-optimal flight level",
}

# Major special-use airspace near East Coast routes, as approximate boxes
# (lat_min, lat_max, lon_min, lon_max). Boundaries are simplified from FAA
# charts; activation schedules are not checked. Warning areas typically extend
# into the cruise levels.
SPECIAL_USE_AIRSPACE: list[tuple[str, tuple[float, float, float, float]]] = [
    ("W-107/W-108 (Wallops, approx.)", (37.0, 38.6, -75.0, -73.4)),
    ("W-386/W-72 (Virginia Capes, approx.)", (35.6, 37.4, -75.4, -72.6)),
    ("W-122 (Cherry Point, approx.)", (33.6, 35.4, -77.2, -74.6)),
    ("W-177/W-161 (Charleston offshore, approx.)", (31.0, 33.2, -80.0, -77.4)),
    ("W-157/W-158 (Jacksonville offshore, approx.)", (29.0, 31.0, -80.6, -78.6)),
    ("W-497/R-2933 (Cape Canaveral, approx.)", (27.6, 29.2, -80.6, -79.0)),
    ("W-168/W-174 (Key West, approx.)", (24.0, 25.6, -82.8, -80.8)),
    ("W-151/W-470 (Eglin, approx.)", (28.4, 30.2, -87.0, -85.0)),
]
# Low-altitude zones such as the Washington DC SFRA (below FL180) or R-5002 are
# left out: scored flights are at or above 10,000 ft and mostly overfly them.


@dataclass
class HubDelay:
    kind: str          # e.g. "Ground Delay Program", "Ground Stop", "Arrival delay", "Engine queue"
    minutes: float
    reason: str = ""


@dataclass
class CauseContext:
    hub_delays: dict[str, HubDelay] = field(default_factory=dict)   # FAA 3-letter code -> delay
    sigmets: list[tuple[str, list[tuple[float, float]]]] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)                   # which feeds were used


# -- feeds ---------------------------------------------------------------------

def parse_nas_status(xml_text: str) -> dict[str, HubDelay]:
    """Airport delays from the FAA NAS Status XML, worst per airport."""
    delays: dict[str, HubDelay] = {}
    root = ET.fromstring(xml_text)

    def minutes(*texts: Optional[str]) -> float:
        """Largest duration among texts like "45 minutes" or "1 hour and 2 minutes"."""
        best = 0.0
        for text in texts:
            h = re.search(r"(\d+(?:\.\d+)?)\s*hour", text or "", re.I)
            m = re.search(r"(\d+(?:\.\d+)?)\s*min", text or "", re.I)
            best = max(best, (float(h.group(1)) * 60 if h else 0.0) + (float(m.group(1)) if m else 0.0))
        return best

    for delay_type in root.iter("Delay_type"):
        name = (delay_type.findtext("Name") or "").strip()
        if "Closure" in name:
            continue  # a closed airport has no arrivals to stretch
        for node in delay_type.iter():
            arpt = (node.findtext("ARPT") or "").strip().upper()
            if not arpt:
                continue
            reason = (node.findtext("Reason") or "").strip()
            if "Ground Stop" in name:
                kind, mins = "Ground Stop", 60.0
            elif "Ground Delay" in name:
                kind, mins = "Ground Delay Program", minutes(node.findtext("Avg")) or minutes(node.findtext("Max"))
            else:
                ad = node.find("Arrival_Departure")
                if ad is not None and ad.get("Type", "").lower().startswith("departure"):
                    continue  # departure delays do not stretch arrival paths
                kind = "Arrival delay"
                mins = minutes(ad.findtext("Max"), ad.findtext("Min")) if ad is not None else 0.0
            if mins <= 0 and kind != "Ground Stop":
                mins = 15.0  # listed without a figure
            if arpt not in delays or mins > delays[arpt].minutes:
                delays[arpt] = HubDelay(kind, mins, reason)
    return delays


def parse_sigmets(payload) -> list[tuple[str, list[tuple[float, float]]]]:
    """Convective SIGMET polygons from the AWC airsigmet JSON."""
    out = []
    for s in payload if isinstance(payload, list) else []:
        hazard = str(s.get("hazard") or "").upper()
        if "CONV" not in hazard and hazard not in ("TS", "TSRA"):
            continue
        coords = [(float(c["lat"]), float(c["lon"])) for c in s.get("coords") or []
                  if c.get("lat") is not None and c.get("lon") is not None]
        if len(coords) >= 3:
            name = "Convective SIGMET " + str(s.get("seriesId") or s.get("alphaChar") or "").strip()
            out.append((name.strip(), coords))
    return out


def _cached_get(cache_dir: Path, name: str, url: str, params: dict, offline: bool) -> Optional[str]:
    path = cache_dir / f"feed_{name}.txt"
    now = time.time()
    if path.exists() and now - path.stat().st_mtime < FEED_CACHE_S:
        return path.read_text(encoding="utf-8")
    if offline:
        return None
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S,
                            headers={"User-Agent": "AirspaceEfficiencyEngine/1.0"})
        resp.raise_for_status()
        text = resp.text
    except requests.RequestException as exc:
        log.warning("%s feed unavailable (%s).", name, str(exc)[:160])
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def load_context(cache_dir: Path, offline: bool = False, queues: Optional[list] = None) -> CauseContext:
    """Gather delay and weather evidence for this cycle."""
    ctx = CauseContext()
    text = _cached_get(cache_dir, "faa_nas_status", FAA_NAS_STATUS_URL, {}, offline)
    if text:
        try:
            ctx.hub_delays = parse_nas_status(text)
            ctx.feeds.append("FAA NAS Status")
        except ET.ParseError as exc:
            log.warning("Could not parse FAA NAS Status (%s).", exc)
    text = _cached_get(cache_dir, "awc_sigmets", AWC_SIGMET_URL, {"format": "json"}, offline)
    if text:
        try:
            ctx.sigmets = parse_sigmets(json.loads(text))
            ctx.feeds.append("NOAA AWC SIGMETs")
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Could not parse SIGMETs (%s).", exc)
    for q in queues or []:
        worst = max((a.delay_min for a in q.advisories), default=0.0)
        if worst >= QUEUE_DELAY_MIN and q.hub not in ctx.hub_delays:
            ctx.hub_delays[q.hub] = HubDelay("Engine queue", worst, "arrival demand above runway rate")
    if queues:
        ctx.feeds.append("engine arrival queue")
    return ctx


# -- geometry ------------------------------------------------------------------

def point_in_polygon(lat: float, lon: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _distance_to_polygon_m(lat: float, lon: float, poly: list[tuple[float, float]]) -> float:
    if point_in_polygon(lat, lon, poly):
        return 0.0
    kx = math.cos(math.radians(lat)) * 111_320
    ky = 110_540
    best = float("inf")
    for (y1, x1), (y2, x2) in zip(poly, poly[1:] + poly[:1]):
        ax, ay = (x1 - lon) * kx, (y1 - lat) * ky
        bx, by = (x2 - lon) * kx, (y2 - lat) * ky
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / ((dx * dx + dy * dy) or 1e-12)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _box(b: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    la0, la1, lo0, lo1 = b
    return [(la0, lo0), (la0, lo1), (la1, lo1), (la1, lo0)]


def _path(lat: float, lon: float, dlat: float, dlon: float) -> list[tuple[float, float]]:
    n = max(2, int(haversine_m(lat, lon, dlat, dlon) / PATH_STEP_M))
    return [great_circle_point(lat, lon, dlat, dlon, i / n) for i in range(n + 1)]


# -- attribution ---------------------------------------------------------------

def attribute(m, ctx: CauseContext) -> None:
    """Split ``m.waste_co2_kg_min`` into causes, in place."""
    if not m.waste_co2_kg_min or m.efficiency is None:
        return
    lat_loss = 1 - (m.lateral_eff if m.lateral_eff is not None else 1.0)
    vert_loss = 1 - (m.vertical_eff if m.vertical_eff is not None else 1.0)
    total = lat_loss + vert_loss
    if total <= 0:
        return
    m.waste_lateral_kg_min = m.waste_co2_kg_min * lat_loss / total
    m.waste_vertical_kg_min = m.waste_co2_kg_min * vert_loss / total

    cause, detail = "routing", ""
    if lat_loss > 0 and m.dest_lat is not None:
        cause, detail = _lateral_cause(m, ctx)
    m.lateral_cause, m.lateral_cause_detail = cause, detail
    if m.waste_vertical_kg_min > m.waste_lateral_kg_min:
        m.cause = "flight_level"
        m.cause_detail = f"best level FL{m.best_level_fl}" if m.best_level_fl else ""
    else:
        m.cause, m.cause_detail = cause, detail


def _lateral_cause(m, ctx: CauseContext) -> tuple[str, str]:
    delay = ctx.hub_delays.get((m.destination or "").upper())
    if delay is None and m.destination and len(m.destination) == 4 and m.destination.startswith("K"):
        delay = ctx.hub_delays.get(m.destination[1:].upper())
    if delay and m.dist_to_dest_m is not None and m.dist_to_dest_m <= CONGESTION_RADIUS_M:
        return "congestion", f"{m.destination} {delay.kind.lower()} {delay.minutes:.0f} min" + \
            (f" ({delay.reason})" if delay.reason else "")

    path = _path(m.lat, m.lon, m.dest_lat, m.dest_lon)
    for name, poly in ctx.sigmets:
        if any(_distance_to_polygon_m(la, lo, poly) <= WEATHER_BUFFER_M for la, lo in path[::2] + path[-1:]):
            return "weather", name

    for name, box in SPECIAL_USE_AIRSPACE:
        poly = _box(box)
        if point_in_polygon(m.lat, m.lon, poly) or point_in_polygon(m.dest_lat, m.dest_lon, poly):
            continue  # inside it already, or landing inside it: not what pushed the aircraft off the line
        if any(point_in_polygon(la, lo, poly) for la, lo in path):
            return "airspace", name
    return "routing", ""
