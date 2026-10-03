"""Phase 6: Automated dispatch API (FastAPI).

Serves the latest engine run from SQLite to airline dispatch systems:
speed-reduction matrices per hub, per-flight efficiency advisories and
optimised ICAO flight-plan strings. Interactive docs at /docs.

Security: set ENGINE_API_KEYS to a comma-separated list of keys; clients send
one in the X-API-Key header. With no keys set the API runs open, which is only
appropriate on 127.0.0.1 for development.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import PlainTextResponse
from fastapi.security import APIKeyHeader

from . import __version__
from .airports import AIRPORTS
from .flightplan import build_flight_plan
from .store import Store

log = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(db_path: Path) -> FastAPI:
    store = Store(db_path)
    keys = [k.strip() for k in os.environ.get("ENGINE_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        log.warning("ENGINE_API_KEYS is not set: the API is running without authentication.")

    def require_key(key: Optional[str] = Security(api_key_header)) -> None:
        if keys and not (key and any(secrets.compare_digest(key, k) for k in keys)):
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")

    def latest() -> dict:
        run = store.latest_run()
        if not run:
            raise HTTPException(status_code=503, detail="No engine runs recorded yet; start the engine first")
        return run

    app = FastAPI(title="Airspace Efficiency Engine Dispatch API", version=__version__,
                  description="Real-time routing efficiency, arrival speed control and optimised flight plans "
                              "for the East Coast USA. All advisories are decision support, not ATC clearances.")
    auth = [Depends(require_key)]

    @app.get("/health")
    def health() -> dict:
        run = store.latest_run()
        return {"status": "ok", "version": __version__, "latest_run": run["ts_utc"] if run else None}

    @app.get("/v1/snapshot", dependencies=auth)
    def snapshot() -> dict:
        run = latest()
        return {"run": run, "hubs": store.hub_status(run["id"])}

    @app.get("/v1/flights", dependencies=auth)
    def flights(rating: Optional[str] = Query(None, pattern="^(efficient|moderate|wasteful|unscored)$"),
                airline: Optional[str] = Query(None, min_length=3, max_length=3),
                min_waste_kg_min: float = 0.0, limit: int = Query(500, ge=1, le=5000)) -> dict:
        run = latest()
        rows = store.flights(run["id"])
        if rating:
            rows = [r for r in rows if r["rating"] == rating]
        if airline:
            rows = [r for r in rows if r["airline_code"] == airline.upper()]
        if min_waste_kg_min > 0:
            rows = [r for r in rows if (r["waste_co2_kg_min"] or 0) >= min_waste_kg_min]
        rows.sort(key=lambda r: r["waste_co2_kg_min"] or 0, reverse=True)
        return {"run_id": run["id"], "source": run["source"], "count": len(rows[:limit]), "flights": rows[:limit]}

    @app.get("/v1/flights/{callsign}", dependencies=auth)
    def flight(callsign: str) -> dict:
        run = latest()
        row = store.flight(run["id"], callsign)
        if not row:
            raise HTTPException(status_code=404, detail=f"{callsign.upper()} not in the latest snapshot")
        adv = [a for a in store.advisories(run["id"]) if a["callsign"] == row["callsign"]]
        return {"run_id": run["id"], "source": run["source"], "flight": row, "arrival_advisory": adv[0] if adv else None}

    @app.get("/v1/flights/{callsign}/flight-plan", dependencies=auth, response_class=PlainTextResponse)
    def flight_plan(callsign: str, from_present_position: bool = False) -> str:
        run = latest()
        row = store.flight(run["id"], callsign)
        if not row:
            raise HTTPException(status_code=404, detail=f"{callsign.upper()} not in the latest snapshot")
        adv = [a for a in store.advisories(run["id"]) if a["callsign"] == row["callsign"] and a["absorbed_min"] > 0]
        try:
            return build_flight_plan(row, advised_kt=adv[0]["advised_kt"] if adv else None,
                                     from_present_position=from_present_position)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/v1/hubs", dependencies=auth)
    def hubs() -> dict:
        run = latest()
        return {"run_id": run["id"], "source": run["source"], "hubs": store.hub_status(run["id"])}

    @app.get("/v1/hubs/{hub}/speed-matrix", dependencies=auth)
    def speed_matrix(hub: str) -> dict:
        hub = hub.upper()
        if hub not in AIRPORTS:
            raise HTTPException(status_code=404, detail=f"Unknown hub {hub}")
        run = latest()
        status = [h for h in store.hub_status(run["id"]) if h["hub"] == hub]
        return {"run_id": run["id"], "source": run["source"], "hub": hub,
                "status": status[0] if status else None, "advisories": store.advisories(run["id"], hub)}

    @app.get("/v1/scoreboard", dependencies=auth)
    def scoreboard(days: int = Query(7, ge=1, le=90), include_simulated: bool = False) -> dict:
        return {"days": days, "include_simulated": include_simulated,
                "airlines": store.scoreboard(days=days, include_simulated=include_simulated)}

    return app
