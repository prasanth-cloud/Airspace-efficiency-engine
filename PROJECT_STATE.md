# Global Predictive Airspace Efficiency Engine: Project State

This is the single source of truth for where the build stands. It is updated at the end of every phase.

**Mission:** Track live commercial flights, compute wind-optimal 3D trajectories, and quantify avoidable CO2 from routing, headwinds and arrival bottlenecks.
**Target environment:** Python 3.10+ on Windows, run from VS Code.
**Initial scope:** East Coast USA geofence, lat 24.0 to 48.0 N, lon -85.0 to -65.0.

## Phase status

| # | Phase | Status | Main output |
|---|-------|--------|-------------|
| 1 | Live telemetry and visual sandbox | Done (2026-10-03) | `flight_tracker.py` |
| 2 | Atmospheric fluid layer (GFS u/v winds) | Done (2026-10-03) | `engine/winds.py` |
| 3 | Great-circle carbon inefficiency math | Done (2026-10-03) | `engine/efficiency.py`, `engine/routes.py`, `engine/mapview.py` |
| 4 | Continuous automation and public scoreboard | Done (2026-10-03) | `engine/store.py`, `engine/dashboard.py`, `run_engine.py --loop` |
| 5 | Predictive arrival queueing | Done (2026-10-03) | `engine/queueing.py` |
| 6 | Automated dispatch API | Done (2026-10-03) | `engine/api.py`, `engine/flightplan.py`, `run_engine.py --api` |

All six phases have been tested offline with simulated and stubbed data (16 unit tests plus stubbed live runs). None of them has run against the real OpenSky, Open-Meteo or adsbdb services yet, because the build sandbox blocks those hosts.

## Contracts between phases

- **`Flight` dataclass** (Phase 1) feeds `FlightMetrics` (Phase 3). Add fields; never rename existing ones.
- **Units inside the engine** are SI: metres, m/s, and degrees true. Feet and knots are display-only.
- **Source flags.** Every run is tagged `LIVE`/`MOCK` for traffic and `GFS`/`SIMULATED` for winds. The scoreboard counts LIVE runs only.
- **Route source** is `opensky` (an airport pair OpenSky flight history saw this callsign fly, consistent with the aircraft's position), `adsbdb`, `simulated`, `unknown` or `mismatch` (a looked-up route the aircraft is not flying). Only `opensky`, `adsbdb` and `simulated` routes get a lateral score.
- **Scoring version.** Each run records `scoring_version` (`engine/efficiency.py`). The scoreboard and validation only use runs from the current version. Bump it whenever stored scores stop being comparable.
- **SQLite** (`data/engine.sqlite3`) is the hand-off between the poller and the API. It runs in WAL mode so both can work at the same time.

## Layout

```
flight_tracker.py      Phase 1 (standalone live map)
run_engine.py          CLI: one-shot, --loop, --api, --mock, --offline
engine/geo.py          Geodesy, ISA, ICAO coordinate format
engine/airports.py     Hubs, arrival rates, airline names
engine/winds.py        Phase 2
engine/routes.py       Origin/destination lookup (adsbdb, cached) and route choice
engine/history.py      OpenSky flight history and airport coordinates
engine/efficiency.py   Phase 3
engine/queueing.py     Phase 5
engine/store.py        Phase 4 storage
engine/dashboard.py    Phase 4 scoreboard
engine/mapview.py      Combined map
engine/flightplan.py   Phase 6 FPL strings
engine/aircraft.py     Aircraft type lookup and per-type fuel flow
engine/validation.py   Carbon model validation against published figures
engine/api.py          Phase 6 API
tests/test_engine.py   py -m unittest discover tests -v
data/                  Caches, SQLite DB, logs (git-ignored)
```

## Next refinements

- Run against live services on the owner's machine, then run `py run_engine.py --validate` to check live route inefficiency against the FAA benchmark.
- Widen the queueing view beyond the geofence so traffic from farther out is counted.
- Calibrate hub arrival rates against FAA ASPM data.

## Known constraints and open questions

- **OpenSky quotas.** Anonymous access is about 400 credits per day. A box larger than 400 square degrees costs 4 credits per call, and the East Coast box is 480. A 5-minute poller (Phase 4) makes 288 calls a day, which costs 1,152 credits, so it needs an OpenSky API client (`OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET`). Winds are cached for 3 hours to stay inside Open-Meteo's free limits.
- **Origin and destination.** OpenSky state vectors do not carry a route, so the engine looks callsigns up in the free adsbdb.com database and, with credentials, checks them against OpenSky's flight history (`/api/flights/all`, two-hour windows, four per cycle). Flights between two airports outside North America and the Caribbean are not kept in the history cache.
- **Git repo.** The code lives in github.com/prasanth-cloud/airspace-efficiency-engine. The shared project folder /mnt/project-files/airspace-engine/ mirrors it.

## Changelog

- **2026-10-03:** First London-box script.
- **2026-10-03:** Phase 1 for the East Coast box. Added a dark satellite basemap, plane icons that rotate to each aircraft's true track, simulated hub-to-hub fallback traffic, and strict geofence filtering.
- **2026-10-03:** Built Phases 2 to 6. Added the engine package, CLI, tests and README. Simulated traffic now includes doglegs and hub arrival banks.
- **2026-10-03:** Added aircraft type lookup with per-type fuel flow (`engine/aircraft.py`) and model validation (`engine/validation.py`, `--validate`). After validation, the 787-9 and 787-10 fuel flows were lowered to match the published figures.
- **2026-10-03:** First live run showed 51.5% route inefficiency against the 2.86% FAA benchmark, with 9% lookup coverage. There were two causes. Stale callsign routes were scored as if the aircraft were flying them, and per-run lookup caps (150 routes, 200 types) limited coverage. The fix rejects implausible routes, uses a 40 NM terminal radius, adds OpenSky's bulk aircraft database, raises the caps to 600 with a rate-limit breaker, and versions scores so the old run is ignored.
- **2026-10-03:** After PR #3, only about 19% of live observations had a trustworthy route. Added OpenSky flight history (`engine/history.py`). With API credentials, the engine learns the airport pairs each callsign flew in the last two days and where each aircraft last landed, and uses a pair the aircraft is plausibly flying as an `opensky` route. In a stubbed replay with 75% stale adsbdb routes and 80% history coverage, trustworthy routes rose from 34% to 89%. `--validate` now reports the OpenSky-confirmed share.
