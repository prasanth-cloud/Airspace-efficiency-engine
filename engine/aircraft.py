"""Aircraft type lookup and per-type fuel flow.

Live aircraft are identified by their ICAO24 transponder address. Types come
from, in order:

1. OpenSky's bulk aircraft database (``aircraftDatabase.csv``, about half a
   million aircraft). It is downloaded once into ``data/`` and refreshed every
   30 days, then reduced to a small ICAO24-to-type index that loads in
   well under a second. You can also download the CSV yourself and drop it
   into ``data/``.
2. Per-aircraft lookups for anything the bulk file lacks: adsbdb.com, then
   OpenSky's metadata endpoint, cached on disk for 30 days.

Fuel flows are typical cruise values in kg/h for a mid-weight aircraft at its
optimum level. They are engineering estimates from public operator and
manufacturer figures, not manufacturer performance data;
``engine/validation.py`` checks them against published trip-fuel figures.
"""

from __future__ import annotations

import csv
import json
import logging
import threading
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
OPENSKY_AIRCRAFT_DB_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
AIRCRAFT_DB_CSV = "aircraftDatabase.csv"
AIRCRAFT_DB_INDEX = "aircraft_types.tsv"
AIRCRAFT_DB_MAX_AGE_S = 30 * 24 * 3600
AIRCRAFT_DB_RETRY_S = 24 * 3600
MAX_LOOKUPS_PER_RUN = 600
LOOKUP_WORKERS = 8
REQUEST_TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 120

_index_cache: dict[str, tuple[float, dict[str, str]]] = {}  # per-process: path -> (mtime, index)


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


def build_type_index(csv_path: Path, index_path: Path) -> int:
    """Reduce the OpenSky aircraft CSV to 'icao24<TAB>typecode' lines; returns the row count."""
    count = 0
    tmp = index_path.with_suffix(".tmp")
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as src, \
            tmp.open("w", encoding="utf-8") as out:
        reader = csv.DictReader(src)
        fields = {name.strip().strip("'").lower(): name for name in (reader.fieldnames or [])}
        icao_col, type_col = fields.get("icao24"), fields.get("typecode")
        if not icao_col or not type_col:
            raise ValueError(f"{csv_path.name} has no icao24/typecode columns")
        for row in reader:
            icao = (row.get(icao_col) or "").strip().strip("'").lower()
            code = (row.get(type_col) or "").strip().strip("'").upper()
            if len(icao) == 6 and code:
                out.write(f"{icao}\t{code}\n")
                count += 1
    tmp.replace(index_path)
    return count


def load_type_index(index_path: Path) -> dict[str, str]:
    if not index_path.exists():
        return {}
    mtime = index_path.stat().st_mtime
    cached = _index_cache.get(str(index_path))
    if cached and cached[0] == mtime:
        return cached[1]
    index: dict[str, str] = {}
    with index_path.open(encoding="utf-8") as fh:
        for line in fh:
            icao, _, code = line.rstrip("\n").partition("\t")
            if code:
                index[icao] = code
    _index_cache[str(index_path)] = (mtime, index)
    return index


def ensure_type_index(data_dir: Path, offline: bool = False) -> dict[str, str]:
    """Load the bulk ICAO24-to-type index, downloading or rebuilding it when stale."""
    csv_path, index_path = data_dir / AIRCRAFT_DB_CSV, data_dir / AIRCRAFT_DB_INDEX
    marker = data_dir / "aircraft_db_last_attempt"
    now = time.time()
    try:
        csv_age = now - csv_path.stat().st_mtime if csv_path.exists() else None
        last_attempt = float(marker.read_text()) if marker.exists() else 0.0
        if not offline and (csv_age is None or csv_age > AIRCRAFT_DB_MAX_AGE_S) and now - last_attempt > AIRCRAFT_DB_RETRY_S:
            data_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(now))
            log.info("Downloading the OpenSky aircraft database (one-off, refreshed monthly)...")
            tmp = csv_path.with_suffix(".part")
            with requests.get(OPENSKY_AIRCRAFT_DB_URL, stream=True, timeout=DOWNLOAD_TIMEOUT_S,
                              headers={"User-Agent": "AirspaceEfficiencyEngine/1.0"}) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            tmp.replace(csv_path)
        if csv_path.exists() and (not index_path.exists() or index_path.stat().st_mtime < csv_path.stat().st_mtime):
            n = build_type_index(csv_path, index_path)
            log.info("Indexed %d aircraft types from %s.", n, csv_path.name)
    except (requests.RequestException, OSError, ValueError, csv.Error) as exc:
        log.warning("Aircraft database unavailable (%s); using per-aircraft lookups only. "
                    "You can download it yourself from %s into %s.", exc, OPENSKY_AIRCRAFT_DB_URL, data_dir)
    try:
        return load_type_index(index_path)
    except OSError as exc:
        log.warning("Could not read aircraft type index (%s).", exc)
        return {}


class AircraftResolver:
    """Resolves ICAO24 addresses to ICAO type designators, with a disk cache."""

    def __init__(self, cache_dir: Path, offline: bool = False, type_index: Optional[dict[str, str]] = None):
        self.cache_file = cache_dir / "aircraft_cache.json"
        self.offline = offline
        self.type_index = type_index if type_index is not None else ensure_type_index(cache_dir, offline)
        self._rate_limited = threading.Event()
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
            bulk = self.type_index.get(f.icao24.lower())
            if bulk:
                result[f.icao24] = bulk
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
        log.info("Looking up %d aircraft types (%d known, %d deferred to later runs).",
                 len(batch), len(flights) - len(todo), len(skipped))
        self._rate_limited.clear()
        with requests.Session() as session, ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
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
        if self._rate_limited.is_set():
            return False
        transient = False
        try:
            resp = session.get(ADSBDB_AIRCRAFT_URL.format(icao24=icao24), timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 429:
                self._rate_limited.set()
                return False
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
