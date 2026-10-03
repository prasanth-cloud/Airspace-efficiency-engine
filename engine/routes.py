"""Origin and destination resolution for live flights.

OpenSky state vectors carry no route, so callsigns are looked up in the free
adsbdb.com route database and cached on disk for a day. Simulated flights
already know their route. Flights whose route cannot be resolved are marked
``unknown`` and get no lateral routing score (scoring them against a guessed
destination would hide exactly the inefficiency we want to expose).

Callsign route databases go stale: flight numbers get reassigned, run in both
directions, or cover several legs. ``route_mismatch`` rejects a looked-up
route when the aircraft is plainly not flying it, so a wrong destination is
never scored as wasted fuel.

With OpenSky API credentials, ``history.FlightHistory`` adds the airport pairs
each callsign actually flew recently. A history pair the aircraft is plausibly
flying (preferring one that starts where the aircraft last landed) is used as an
``opensky`` route; otherwise the adsbdb route stands, still subject to the
mismatch test.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

from .airports import lookup
from .geo import EARTH_RADIUS_M, angle_diff, cross_track_m, haversine_m, initial_bearing

if TYPE_CHECKING:
    from .history import FlightHistory

log = logging.getLogger(__name__)

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
CACHE_TTL_S = 24 * 3600
NEGATIVE_TTL_S = 6 * 3600
MAX_LOOKUPS_PER_RUN = 600
LOOKUP_WORKERS = 8
REQUEST_TIMEOUT_S = 10

# Route plausibility: how far off a route an aircraft can be before the route is
# judged to belong to a different flight
MISMATCH_MIN_CROSS_TRACK_M = 150_000
MISMATCH_CROSS_TRACK_FRACTION = 0.15     # of the route length, for long routes
MISMATCH_ALONG_TRACK_MARGIN = 0.10       # beyond either end of the route
MISMATCH_HEADING_DEG = 90                # flying away from the destination
MISMATCH_HEADING_MIN_DIST_M = 40 * 1852     # the terminal radius; closer in, vectoring is normal


@dataclass
class Endpoint:
    code: str  # ICAO where known, e.g. KATL
    iata: str
    name: str
    lat: float
    lon: float


@dataclass
class Route:
    origin: Endpoint
    destination: Endpoint
    source: str  # "opensky", "adsbdb" or "simulated"


class RouteResolver:
    def __init__(self, cache_dir: Path, offline: bool = False, history: Optional["FlightHistory"] = None):
        self.cache_file = cache_dir / "route_cache.json"
        self.offline = offline
        self.history = history
        self._rate_limited = threading.Event()
        self.cache: dict[str, dict] = {}
        try:
            if self.cache_file.exists():
                self.cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable route cache (%s).", exc)

    # -- public ----------------------------------------------------------------

    def resolve_many(self, flights: list) -> dict[str, Optional[Route]]:
        """Return {callsign: Route or None} for every flight."""
        result = self._resolve_adsbdb(flights)
        if self.history is not None:
            confirmed = 0
            for f in flights:
                if f.route:
                    continue
                route = choose_route(f, result.get(f.callsign), self.history)
                result[f.callsign] = route
                confirmed += bool(route and route.source == "opensky")
            log.info("OpenSky flight history confirmed routes for %d of %d flights.", confirmed, len(flights))
        return result

    def _resolve_adsbdb(self, flights: list) -> dict[str, Optional[Route]]:
        result: dict[str, Optional[Route]] = {}
        to_lookup: list[str] = []
        now = time.time()

        for f in flights:
            if f.route:  # simulated traffic carries "AAA to BBB"
                result[f.callsign] = _route_from_text(f.route)
                continue
            entry = self.cache.get(f.callsign)
            ttl = CACHE_TTL_S if entry and entry.get("route") else NEGATIVE_TTL_S
            if entry and now - entry.get("ts", 0) < ttl:
                result[f.callsign] = _route_from_json(entry.get("route"))
            else:
                to_lookup.append(f.callsign)

        if self.offline:
            for cs in to_lookup:
                result[cs] = None
            return result

        batch, skipped = to_lookup[:MAX_LOOKUPS_PER_RUN], to_lookup[MAX_LOOKUPS_PER_RUN:]
        if batch:
            log.info("Looking up %d routes on adsbdb (%d cached, %d deferred to later runs).",
                     len(batch), len(flights) - len(to_lookup), len(skipped))
            self._rate_limited = threading.Event()
            with requests.Session() as session, ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
                session.headers["User-Agent"] = "AirspaceEfficiencyEngine/1.0"
                for cs, route_json in zip(batch, pool.map(lambda c: self._lookup(session, c), batch)):
                    if route_json is not False:  # False = transient error, do not cache
                        self.cache[cs] = {"ts": now, "route": route_json}
                    result[cs] = _route_from_json(route_json) if route_json else None
            self._save()
            if self._rate_limited.is_set():
                log.warning("adsbdb rate limit reached; remaining routes will be looked up on later runs.")
        for cs in skipped:
            result[cs] = None
        return result

    # -- internals -------------------------------------------------------------

    def _lookup(self, session: requests.Session, callsign: str):
        """Route dict, None if unknown, or False on a transient failure."""
        if self._rate_limited.is_set():
            return False  # stop hammering the API once it has asked us to back off
        try:
            resp = session.get(ADSBDB_URL.format(callsign=callsign), timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                self._rate_limited.set()
                return False
            resp.raise_for_status()
            fr = (resp.json().get("response") or {})
            fr = fr.get("flightroute") if isinstance(fr, dict) else None
            if not fr:
                return None
            o, d = fr.get("origin") or {}, fr.get("destination") or {}
            if None in (o.get("latitude"), o.get("longitude"), d.get("latitude"), d.get("longitude")):
                return None
            return {"origin": _endpoint_json(o), "destination": _endpoint_json(d)}
        except (requests.RequestException, ValueError, AttributeError) as exc:
            log.debug("Route lookup failed for %s: %s", callsign, exc)
            return False

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write route cache (%s).", exc)


def choose_route(f, adsbdb_route: Optional[Route], history: "FlightHistory") -> Optional[Route]:
    """Best route for a live flight from OpenSky history and adsbdb.

    Candidates in order: history pairs that start where the aircraft last
    landed; the adsbdb route when OpenSky also saw that pair or that departure;
    other history pairs; finally the adsbdb route on its own. The first one the
    aircraft is plausibly flying wins. When none fits, the adsbdb route is
    returned unchanged so the efficiency step records it as a mismatch.
    """
    pairs = history.routes_for(f.callsign)
    hint = history.departure_hint(f.icao24)
    adsb_pair = (adsbdb_route.origin.code, adsbdb_route.destination.code) if adsbdb_route else None
    candidates: list[Route] = []

    def add_pair(dep: str, arr: str) -> None:
        o, d = history.endpoint(dep), history.endpoint(arr)
        if o and d:
            candidates.append(Route(o, d, "opensky"))

    for dep, arr in pairs:
        if dep == hint:
            add_pair(dep, arr)
    if adsbdb_route and (adsb_pair in pairs or (hint and adsb_pair[0] == hint)):
        candidates.append(Route(adsbdb_route.origin, adsbdb_route.destination, "opensky"))
    for dep, arr in pairs:
        if dep != hint:
            add_pair(dep, arr)
    for route in candidates:
        if not route_mismatch(f.latitude, f.longitude, f.true_track_deg, route):
            return route
    return adsbdb_route


def route_mismatch(lat: float, lon: float, track_deg: Optional[float], route: Route) -> Optional[str]:
    """Why the aircraft cannot be flying ``route``, or None if it plausibly is.

    Three tests, each loose enough to keep genuine detours, weather deviations
    and vectoring:
    * more than max(150 km, 15% of the route length) to the side of the
      origin-destination great circle;
    * more than 10% of the route length beyond either end of it;
    * outside the 40 NM terminal area and heading more than 90 degrees away
      from it (typically the same flight number in the opposite direction).
    """
    o, d = route.origin, route.destination
    length = haversine_m(o.lat, o.lon, d.lat, d.lon)
    if length < 1_000:
        return "origin and destination coincide"
    xt = cross_track_m(lat, lon, o.lat, o.lon, d.lat, d.lon)
    if abs(xt) > max(MISMATCH_MIN_CROSS_TRACK_M, MISMATCH_CROSS_TRACK_FRACTION * length):
        return f"{abs(xt) / 1000:.0f} km off the route"

    d13 = haversine_m(o.lat, o.lon, lat, lon) / EARTH_RADIUS_M
    cos_ratio = math.cos(d13) / max(math.cos(xt / EARTH_RADIUS_M), 1e-9)
    along = math.acos(max(-1.0, min(1.0, cos_ratio))) * EARTH_RADIUS_M
    if abs(angle_diff(initial_bearing(o.lat, o.lon, lat, lon), initial_bearing(o.lat, o.lon, d.lat, d.lon))) > 90:
        along = -along  # behind the origin
    fraction = along / length
    if fraction < -MISMATCH_ALONG_TRACK_MARGIN or fraction > 1 + MISMATCH_ALONG_TRACK_MARGIN:
        return "beyond the ends of the route"

    if track_deg is not None and haversine_m(lat, lon, d.lat, d.lon) > MISMATCH_HEADING_MIN_DIST_M:
        off = abs(angle_diff(track_deg, initial_bearing(lat, lon, d.lat, d.lon)))
        if off > MISMATCH_HEADING_DEG:
            return f"heading {off:.0f} degrees away from the destination"
    return None


def _endpoint_json(a: dict) -> dict:
    return {
        "code": a.get("icao_code") or a.get("iata_code") or "ZZZZ",
        "iata": a.get("iata_code") or "",
        "name": a.get("name") or "",
        "lat": float(a["latitude"]),
        "lon": float(a["longitude"]),
    }


def _route_from_json(data: Optional[dict]) -> Optional[Route]:
    if not data:
        return None
    return Route(Endpoint(**data["origin"]), Endpoint(**data["destination"]), "adsbdb")


def _route_from_text(text: str) -> Optional[Route]:
    parts = [p.strip() for p in text.split(" to ")]
    if len(parts) != 2:
        return None
    o, d = lookup(parts[0]), lookup(parts[1])
    if not o or not d:
        return None
    return Route(Endpoint(o.icao, o.iata, o.name, o.lat, o.lon),
                 Endpoint(d.icao, d.iata, d.name, d.lat, d.lon), "simulated")
