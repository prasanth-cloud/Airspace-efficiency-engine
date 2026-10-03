# Global Predictive Airspace Efficiency Engine

This engine tracks live commercial flights over the East Coast USA and works out how much CO2 is burned beyond what physics allows.

For every aircraft it compares the actual flight with an ideal one:

- **Ideal path:** the wind-adjusted great-circle route to the destination, flown at the best flight level.
- **Arrival pressure:** whether the destination hub is over capacity.
- **Speed control:** how much fuel would be saved by slowing down at cruise instead of holding.

Results are published in three ways:

- a map (`index.html`)
- an airline scoreboard (`scoreboard.html`)
- a dispatch API, which also generates ICAO flight plans

## Quick start (Windows, VS Code terminal)

```
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt

py run_engine.py                 # one cycle: map, scoreboard, arrival advisories
py run_engine.py --loop          # background engine, every 5 minutes (Ctrl+C to stop)
py run_engine.py --api           # dispatch API, docs at http://127.0.0.1:8000/docs
py run_engine.py --offline       # demo with no network: simulated traffic, winds and routes
py flight_tracker.py             # Phase 1 live map only
py -m unittest discover tests -v # test suite
```

### Optional environment variables

| Variable | Purpose |
|---|---|
| `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` | OpenSky API client. Gives higher rate limits and is needed for `--loop`. |
| `ENGINE_API_KEYS` | Comma-separated keys for the dispatch API. Clients send one in the `X-API-Key` header. |

## Phases

| # | Module | What it does |
|---|---|---|
| 1 | `flight_tracker.py` | Pulls live OpenSky state vectors inside lat 24 to 48, lon -85 to -65. Shows a dark satellite map with directional icons. Falls back to simulated traffic on HTTP 429 or any API failure. |
| 2 | `engine/winds.py` | Builds a NOAA GFS u/v wind grid (2 degrees, 10 pressure levels) through Open-Meteo. Interpolates it to each aircraft's lat/lon/pressure altitude, then derives true airspeed, headwind/tailwind and the groundspeed minus airspeed delta. |
| 3 | `engine/efficiency.py` | Scores lateral efficiency (wind-aware great-circle closure) and flight level efficiency (best level from FL280 to FL410 at constant Mach). Turns the result into the Carbon Waste Metric in kg CO2 per minute. |
| 4 | `engine/store.py`, `engine/dashboard.py`, `run_engine.py --loop` | Runs a 5-minute poller that stores results in SQLite. Builds the airline scoreboard from LIVE data only. |
| 5 | `engine/queueing.py` | Projects arrivals 3 hours ahead at the priority hubs and runs a first-come, first-served runway slot model. Builds a speed-reduction matrix that absorbs delay at cruise instead of holding. |
| 6 | `engine/api.py`, `engine/flightplan.py` | FastAPI B2B endpoints. Generates ICAO FPL strings with great-circle DCT routing. |

Origin and destination for live flights come from adsbdb.com and are cached for a day. Flights with no known route are not scored laterally.

## Assumptions to keep in mind

- **Fuel model.** Aircraft type is not in the public feed, so every aircraft uses a single-aisle reference of 40 kg fuel per minute in cruise. Each kg of fuel produces 3.16 kg of CO2.
- **Hub capacity.** Arrival rates are approximate good-weather planning values, set in `engine/airports.py`.
- **Not operational.** Advisories and flight plans are decision support. They are not ATC clearances or filed flight plans.

See `PROJECT_STATE.md` for status, the contracts between phases, and open questions.
