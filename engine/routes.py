"""Origin and destination resolution for live flights.

OpenSky state vectors carry no route, so callsigns are looked up in the free
adsbdb.com route database and cached on disk for a day. Simulated flights
already know their route. Flights whose route cannot be resolved are marked
``unknown`` and get no lateral routing score (scoring them against a guessed
destination would hide exactly the inefficiency we want to expose).
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from .airports import lookup

log = logging.getLogger(__name__)

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
CACHE_TTL_S = 24 * 3600
NEGATIVE_TTL_S = 6 * 3600
MAX_LOOKUPS_PER_RUN = 150
REQUEST_TIMEOUT_S = 10


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
    source: str  # "adsbdb" or "simulated"


class RouteResolver:
    def __init__(self, cache_dir: Path, offline: bool = False):
        self.cache_file = cache_dir / "route_cache.json"
        self.offline = offline
        self.cache: dict[str, dict] = {}
        try:
            if self.cache_file.exists():
                self.cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable route cache (%s).", exc)

    # -- public ----------------------------------------------------------------

    def resolve_many(self, flights: list) -> dict[str, Optional[Route]]:
        """Return {callsign: Route or None} for every flight."""
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
            log.info("Looking up %d routes on adsbdb (%d cached).", len(batch), len(flights) - len(to_lookup))
            with requests.Session() as session, ThreadPoolExecutor(max_workers=6) as pool:
                session.headers["User-Agent"] = "AirspaceEfficiencyEngine/1.0"
                for cs, route_json in zip(batch, pool.map(lambda c: self._lookup(session, c), batch)):
                    if route_json is not False:  # False = transient error, do not cache
                        self.cache[cs] = {"ts": now, "route": route_json}
                    result[cs] = _route_from_json(route_json) if route_json else None
            self._save()
        for cs in skipped:
            result[cs] = None
        return result

    # -- internals -------------------------------------------------------------

    def _lookup(self, session: requests.Session, callsign: str):
        """Route dict, None if unknown, or False on a transient failure."""
        try:
            resp = session.get(ADSBDB_URL.format(callsign=callsign), timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
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
