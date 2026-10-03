"""Aircraft type lookup and per-type fuel flow.

Live aircraft are identified by their ICAO24 transponder address. The type is
looked up in the free adsbdb.com aircraft database, with OpenSky's aircraft
metadata as a second source, and cached on disk for 30 days.

Fuel flows are typical cruise values in kg/h for a mid-weight aircraft at its
optimum level. They are engineering estimates from public operator and
manufacturer figures, not manufacturer performance data;
``engine/validation.py`` checks them against published trip-fuel figures.
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

log = logging.getLogger(__name__)

ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}"
OPENSKY_METADATA_URL = "https://opensky-network.org/api/metadata/aircraft/icao/{icao24}"
CACHE_TTL_S = 30 * 24 * 3600
NEGATIVE_TTL_S = 3 * 24 * 3600
MAX_LOOKUPS_PER_RUN = 200
REQUEST_TIMEOUT_S = 10


@dataclass(frozen=True)
class AircraftPerformance:
    icao_type: str
    name: str
    cruise_fuel_kg_h: float
    wake: str  # ICAO wake turbulence category: L, M, H or J

    @property
    def cruise_fuel_kg_min(self) -> float:
        return self.cruise_fuel_kg_h / 60


def _p(code: str, name: str, ff: float, wake: str) -> tuple[str, AircraftPerformance]:
    return code, AircraftPerformance(code, name, ff, wake)


PERFORMANCE: dict[str, AircraftPerformance] = dict([
    # Airbus single aisle
    _p("A319", "Airbus A319", 2300, "M"), _p("A320", "Airbus A320", 2500, "M"),
    _p("A321", "Airbus A321", 2800, "M"), _p("A19N", "Airbus A319neo", 1950, "M"),
    _p("A20N", "Airbus A320neo", 2100, "M"), _p("A21N", "Airbus A321neo", 2400, "M"),
    _p("BCS1", "Airbus A220-100", 1650, "M"), _p("BCS3", "Airbus A220-300", 1800, "M"),
    # Boeing single aisle
    _p("B737", "Boeing 737-700", 2350, "M"), _p("B738", "Boeing 737-800", 2550, "M"),
    _p("B739", "Boeing 737-900", 2650, "M"), _p("B37M", "Boeing 737 MAX 7", 1950, "M"),
    _p("B38M", "Boeing 737 MAX 8", 2150, "M"), _p("B39M", "Boeing 737 MAX 9", 2250, "M"),
    _p("B752", "Boeing 757-200", 3200, "M"), _p("B753", "Boeing 757-300", 3500, "M"),
    # Regional jets and turboprops
    _p("E170", "Embraer 170", 1500, "M"), _p("E75L", "Embraer 175", 1550, "M"),
    _p("E75S", "Embraer 175", 1550, "M"), _p("E190", "Embraer 190", 1800, "M"),
    _p("E195", "Embraer 195", 1900, "M"), _p("E290", "Embraer E190-E2", 1500, "M"),
    _p("E295", "Embraer E195-E2", 1600, "M"), _p("CRJ2", "Bombardier CRJ200", 1100, "M"),
    _p("CRJ7", "Bombardier CRJ700", 1300, "M"), _p("CRJ9", "Bombardier CRJ900", 1400, "M"),
    _p("DH8D", "De Havilland Dash 8-400", 800, "M"), _p("AT76", "ATR 72-600", 700, "M"),
    # Wide bodies
    _p("A332", "Airbus A330-200", 5600, "H"), _p("A333", "Airbus A330-300", 5700, "H"),
    _p("A339", "Airbus A330-900neo", 5000, "H"), _p("A359", "Airbus A350-900", 5800, "H"),
    _p("A35K", "Airbus A350-1000", 6800, "H"), _p("A388", "Airbus A380-800", 11000, "J"),
    _p("A306", "Airbus A300-600", 5400, "H"), _p("B762", "Boeing 767-200", 4500, "H"),
    _p("B763", "Boeing 767-300", 4800, "H"), _p("B764", "Boeing 767-400", 5200, "H"),
    _p("B772", "Boeing 777-200", 6800, "H"), _p("B77L", "Boeing 777-200LR/F", 7300, "H"),
    _p("B77W", "Boeing 777-300ER", 7500, "H"), _p("B788", "Boeing 787-8", 4900, "H"),
    _p("B789", "Boeing 787-9", 5000, "H"), _p("B78X", "Boeing 787-10", 5450, "H"),
    _p("B744", "Boeing 747-400", 10500, "H"), _p("B748", "Boeing 747-8", 9800, "H"),
    _p("MD11", "McDonnell Douglas MD-11", 8000, "H"),
    # Business jets
    _p("C56X", "Cessna Citation Excel", 700, "M"), _p("CL60", "Bombardier Challenger 600", 1000, "M"),
    _p("GLF5", "Gulfstream V", 1500, "M"), _p("GLF6", "Gulfstream G650", 1600, "M"),
    _p("GL7T", "Bombardier Global 7500", 1800, "M"),
])

# Used when the type is unknown: an A320 / 737-800 class single aisle
DEFAULT_PERFORMANCE = AircraftPerformance("ZZZZ", "Unknown (single-aisle reference)", 2400, "M")


def performance_for(icao_type: Optional[str]) -> tuple[AircraftPerformance, bool]:
    """(performance, is_known_type) for an ICAO type designator."""
    if icao_type:
        perf = PERFORMANCE.get(icao_type.strip().upper())
        if perf:
            return perf, True
    return DEFAULT_PERFORMANCE, False


class AircraftResolver:
    """Resolves ICAO24 addresses to ICAO type designators, with a disk cache."""

    def __init__(self, cache_dir: Path, offline: bool = False):
        self.cache_file = cache_dir / "aircraft_cache.json"
        self.offline = offline
        self.cache: dict[str, dict] = {}
        try:
            if self.cache_file.exists():
                self.cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable aircraft cache (%s).", exc)

    def resolve_many(self, flights: list) -> dict[str, Optional[str]]:
        """Return {icao24: icao_type or None} for every flight."""
        result: dict[str, Optional[str]] = {}
        todo: list[str] = []
        now = time.time()
        for f in flights:
            if getattr(f, "aircraft_type", None):  # simulated traffic carries its type
                result[f.icao24] = f.aircraft_type
                continue
            entry = self.cache.get(f.icao24)
            ttl = CACHE_TTL_S if entry and entry.get("type") else NEGATIVE_TTL_S
            if entry and now - entry.get("ts", 0) < ttl:
                result[f.icao24] = entry.get("type")
            else:
                todo.append(f.icao24)

        if self.offline or not todo:
            for icao24 in todo:
                result[icao24] = None
            return result

        batch, skipped = todo[:MAX_LOOKUPS_PER_RUN], todo[MAX_LOOKUPS_PER_RUN:]
        log.info("Looking up %d aircraft types (%d cached).", len(batch), len(flights) - len(todo))
        with requests.Session() as session, ThreadPoolExecutor(max_workers=6) as pool:
            session.headers["User-Agent"] = "AirspaceEfficiencyEngine/1.0"
            for icao24, found in zip(batch, pool.map(lambda a: self._lookup(session, a), batch)):
                if found is not False:  # False = transient failure, retry next run
                    self.cache[icao24] = {"ts": now, "type": found}
                result[icao24] = found or None
        for icao24 in skipped:
            result[icao24] = None
        self._save()
        return result

    def _lookup(self, session: requests.Session, icao24: str):
        """ICAO type string, None if unknown in both sources, or False on a transient failure."""
        transient = False
        try:
            resp = session.get(ADSBDB_AIRCRAFT_URL.format(icao24=icao24), timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 200:
                body = resp.json().get("response")
                aircraft = body.get("aircraft") if isinstance(body, dict) else None
                code = (aircraft or {}).get("icao_type")
                if code:
                    return code.strip().upper()
            elif resp.status_code != 404:
                transient = True
        except (requests.RequestException, ValueError, AttributeError) as exc:
            log.debug("adsbdb aircraft lookup failed for %s: %s", icao24, exc)
            transient = True

        try:
            resp = session.get(OPENSKY_METADATA_URL.format(icao24=icao24), timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 200:
                code = (resp.json() or {}).get("typecode")
                if code:
                    return code.strip().upper()
                return False if transient else None
            if resp.status_code == 404:
                return False if transient else None
        except (requests.RequestException, ValueError, AttributeError) as exc:
            log.debug("OpenSky metadata lookup failed for %s: %s", icao24, exc)
        return False

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write aircraft cache (%s).", exc)
