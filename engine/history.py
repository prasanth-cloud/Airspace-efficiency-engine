"""OpenSky flight history: what each callsign and each aircraft actually flew.

adsbdb maps a callsign to the route it was filed for at some point, which goes
stale. OpenSky's flights API records the departure and arrival airports it
observed for every flight, so with the owner's API client credentials the
engine learns:

* which airport pairs each callsign really flew in the last two days, and
* where each aircraft last landed, which is where its current leg departed.

A history route is only used when the aircraft is plausibly flying it (see
``routes.route_mismatch``), and pairs starting at the aircraft's last landing
are tried first.

History is pulled from ``/api/flights/all`` in two-hour windows (the API's
limit), a few windows per engine cycle, newest first, and cached on disk.
OpenSky publishes these flights in batches, so the most recent hours are often
not available yet; those windows are simply retried on later cycles.

Airport coordinates come from the 16 hubs in ``airports.py`` and from the
OurAirports public-domain airport list, downloaded once and refreshed every
three months.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from .airports import lookup

log = logging.getLogger(__name__)

OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/all"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

WINDOW_S = 2 * 3600                  # /flights/all accepts at most two hours per call
HISTORY_SPAN_S = 48 * 3600           # how far back callsign history is kept
WINDOWS_PER_RUN = 4                  # spread the backfill over several 5-minute cycles
FINAL_AFTER_S = 30 * 3600            # an empty window older than this is not retried
EMPTY_RETRY_S = 3600                 # how often an unpublished window is retried
DEPARTURE_HINT_MAX_AGE_S = 18 * 3600   # a landing older than this says little about the current leg
REQUEST_TIMEOUT_S = 60
RELEVANT_LAT = (5.0, 65.0)           # North America and the Caribbean; East Coast traffic touches it
RELEVANT_LON = (-135.0, -50.0)

AIRPORT_INDEX = "airports_index.json"
AIRPORT_DB_MAX_AGE_S = 90 * 24 * 3600
AIRPORT_DB_RETRY_S = 24 * 3600
AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}


def credentials_configured() -> bool:
    return bool(os.environ.get("OPENSKY_CLIENT_ID") and os.environ.get("OPENSKY_CLIENT_SECRET"))


# -- airport coordinates -------------------------------------------------------

def build_airport_index(csv_text: str) -> dict[str, list]:
    """{ICAO code: [iata, name, lat, lon]} from the OurAirports CSV."""
    index: dict[str, list] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        if row.get("type") not in AIRPORT_TYPES:
            continue
        try:
            lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        entry = [row.get("iata_code") or "", row.get("name") or "", lat, lon]
        for code in (row.get("gps_code"), row.get("ident"), row.get("icao_code")):
            code = (code or "").strip().upper()
            if len(code) == 4 and code.isalnum():
                index.setdefault(code, entry)
    return index


def ensure_airport_index(data_dir: Path, offline: bool = False) -> dict[str, list]:
    path, marker = data_dir / AIRPORT_INDEX, data_dir / "airports_db_last_attempt"
    now = time.time()
    try:
        age = now - path.stat().st_mtime if path.exists() else None
        last_attempt = float(marker.read_text()) if marker.exists() else 0.0
        if not offline and (age is None or age > AIRPORT_DB_MAX_AGE_S) and now - last_attempt > AIRPORT_DB_RETRY_S:
            data_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(now))
            log.info("Downloading the OurAirports airport list (one-off, refreshed quarterly)...")
            resp = requests.get(OURAIRPORTS_URL, timeout=REQUEST_TIMEOUT_S,
                                headers={"User-Agent": "AirspaceEfficiencyEngine/1.0"})
            resp.raise_for_status()
            index = build_airport_index(resp.content.decode("utf-8", errors="replace"))
            path.write_text(json.dumps(index), encoding="utf-8")
            log.info("Indexed %d airports.", len(index))
            return index
    except (requests.RequestException, OSError, ValueError, csv.Error) as exc:
        log.warning("Airport list unavailable (%s); only the built-in hubs have coordinates.", exc)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read airport index (%s).", exc)
        return {}


# -- flight history ------------------------------------------------------------

class FlightHistory:
    """Callsign routes and last landings observed by OpenSky, cached on disk.

    ``token_fn`` returns a bearer token or None; without one nothing is fetched
    and only the existing cache is used.
    """

    def __init__(self, cache_dir: Path, offline: bool = False,
                 token_fn: Optional[Callable[[requests.Session], Optional[str]]] = None,
                 airports: Optional[dict[str, list]] = None):
        self.cache_file = cache_dir / "opensky_history.json"
        self.offline = offline
        self.token_fn = token_fn
        self.airports = airports if airports is not None else ensure_airport_index(cache_dir, offline)
        self._rate_limited = threading.Event()
        # windows: {start: "done" or time of the last empty attempt}; flights: compact [callsign, icao24, dep, arr, first, last]
        self.data: dict = {"windows": {}, "flights": []}
        try:
            if self.cache_file.exists():
                loaded = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "flights" in loaded:
                    self.data = loaded
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable OpenSky history cache (%s).", exc)
        self._index()

    # -- lookups -----------------------------------------------------------------

    def routes_for(self, callsign: str) -> list[tuple[str, str]]:
        """Distinct (departure, arrival) ICAO pairs flown under this callsign, newest first."""
        return list(self._by_callsign.get(callsign.strip().upper(), {}))

    def departure_hint(self, icao24: str, now: Optional[float] = None) -> Optional[str]:
        """Where the aircraft's current leg most likely departed, if OpenSky saw it recently.

        That is the airport of its newest recorded landing, or the departure of
        a newest flight that has no arrival yet (the current leg itself).
        """
        hit = self._departure_hint.get(icao24.strip().lower())
        now = now or time.time()
        if hit and now - hit[1] <= DEPARTURE_HINT_MAX_AGE_S:
            return hit[0]
        return None

    def endpoint(self, code: str):
        """Endpoint for an ICAO code, or None when its coordinates are unknown."""
        from .routes import Endpoint  # routes imports this module
        hub = lookup(code)
        if hub:
            return Endpoint(hub.icao, hub.iata, hub.name, hub.lat, hub.lon)
        entry = self.airports.get(code)
        if entry:
            return Endpoint(code, entry[0], entry[1], float(entry[2]), float(entry[3]))
        return None

    @property
    def size(self) -> int:
        return len(self._by_callsign)

    # -- refresh -----------------------------------------------------------------

    def refresh(self, now: Optional[float] = None) -> int:
        """Fetch up to WINDOWS_PER_RUN missing two-hour windows. Returns windows fetched."""
        now = now or time.time()
        self._prune(now)
        if self.offline or self.token_fn is None:
            return 0
        missing = self._missing_windows(now)
        if not missing:
            return 0
        fetched = 0
        with requests.Session() as session:
            session.headers["User-Agent"] = "AirspaceEfficiencyEngine/1.0"
            token = self.token_fn(session)
            if not token:
                log.info("OpenSky flight history needs API client credentials; skipping.")
                return 0
            session.headers["Authorization"] = f"Bearer {token}"
            for start in missing[:WINDOWS_PER_RUN]:
                status = self._fetch_window(session, start, now)
                if status is None:
                    break  # rate limited or failing: try again next cycle
                self.data["windows"][str(start)] = "done" if status == "done" else now
                fetched += 1
        if fetched:
            self._index()
            self._save()
            log.info("OpenSky flight history: fetched %d window(s); %d callsigns known.", fetched, self.size)
        return fetched

    def _missing_windows(self, now: float) -> list[int]:
        """Never-tried windows first (newest first), then unpublished ones due for a retry."""
        newest = int(now // WINDOW_S) * WINDOW_S - WINDOW_S  # last complete window
        starts = range(newest, int(now - HISTORY_SPAN_S), -WINDOW_S)
        windows = self.data["windows"]
        fresh = [s for s in starts if str(s) not in windows]
        retry = [s for s in starts
                 if isinstance(windows.get(str(s)), (int, float))
                 and now - s < FINAL_AFTER_S and now - windows[str(s)] > EMPTY_RETRY_S]
        return fresh + retry

    def _fetch_window(self, session: requests.Session, start: int, now: float) -> Optional[str]:
        try:
            resp = session.get(OPENSKY_FLIGHTS_URL, params={"begin": start, "end": start + WINDOW_S},
                               timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 429:
                log.warning("OpenSky flight history rate limited; continuing on later cycles.")
                return None
            if resp.status_code == 404:
                return "empty"  # not published yet
            resp.raise_for_status()
            rows = resp.json() or []
        except (requests.RequestException, ValueError) as exc:
            log.warning("OpenSky flight history unavailable (%s).", _short(exc))
            return None
        added = 0
        for r in rows:
            cs = (r.get("callsign") or "").strip().upper()
            dep, arr = r.get("estDepartureAirport"), r.get("estArrivalAirport")
            icao24 = (r.get("icao24") or "").strip().lower()
            if not icao24 or not (dep or arr) or not self._relevant(dep, arr):
                continue
            self.data["flights"].append([cs, icao24, dep, arr, r.get("firstSeen") or start, r.get("lastSeen") or start])
            added += 1
        return "done" if added else "empty"

    # -- internals -----------------------------------------------------------------

    def _relevant(self, dep: Optional[str], arr: Optional[str]) -> bool:
        """Keep flights touching the Americas region; the worldwide feed is too big to cache whole."""
        if not self.airports:
            return True
        for code in (dep, arr):
            ep = self.endpoint(code) if code else None
            if ep and RELEVANT_LAT[0] <= ep.lat <= RELEVANT_LAT[1] and RELEVANT_LON[0] <= ep.lon <= RELEVANT_LON[1]:
                return True
        return False

    def _prune(self, now: float) -> None:
        cutoff = now - HISTORY_SPAN_S - WINDOW_S
        self.data["windows"] = {k: v for k, v in self.data["windows"].items() if int(k) >= cutoff}
        self.data["flights"] = [f for f in self.data["flights"] if f[5] >= cutoff]

    def _index(self) -> None:
        by_cs: dict[str, dict[tuple[str, str], int]] = {}
        hint: dict[str, tuple[str, int]] = {}
        seen: set[tuple] = set()
        for cs, icao24, dep, arr, first, last in sorted(self.data["flights"], key=lambda f: -f[5]):
            if (icao24, first) in seen:  # a flight spanning two windows is listed twice
                continue
            seen.add((icao24, first))
            if cs and dep and arr and dep != arr:
                by_cs.setdefault(cs, {}).setdefault((dep, arr), last)
            if icao24 not in hint and (arr or dep):
                hint[icao24] = (arr or dep, last)
        self._by_callsign = by_cs
        self._departure_hint = hint

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.data), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write OpenSky history cache (%s).", exc)


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) < 160 else text[:157] + "..."
