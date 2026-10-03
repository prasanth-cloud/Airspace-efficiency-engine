"""One engine cycle: telemetry -> winds -> routes -> efficiency -> queueing ->
storage -> map and scoreboard. Used by both the one-shot run and the Phase 4
poller."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import flight_tracker as ft
from .dashboard import build_scoreboard
from .efficiency import FlightMetrics, analyse_flight
from .mapview import build_engine_map
from .queueing import HubQueue, plan_arrivals
from .routes import RouteResolver
from .store import Store
from .winds import WindField, load_wind_field

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "engine.sqlite3"


@dataclass
class EngineConfig:
    bbox: dict
    data_dir: Path = DATA_DIR
    output_dir: Path = PROJECT_DIR
    force_mock: bool = False
    offline_winds: bool = False
    offline_routes: bool = False
    airlines_only: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engine.sqlite3"


@dataclass
class RunResult:
    run_id: int
    source: str
    winds: WindField
    metrics: list[FlightMetrics]
    queues: list[HubQueue]
    map_path: Path
    scoreboard_path: Path


def run_once(cfg: EngineConfig, store: Store | None = None) -> RunResult:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    store = store or Store(cfg.db_path)
    now = datetime.now(timezone.utc)

    # Phase 1: telemetry
    flights, source = ft.get_flights(cfg.bbox, force_mock=cfg.force_mock, airlines_only=cfg.airlines_only)

    # Phase 2: winds (simulated traffic still gets real GFS winds when reachable)
    winds = load_wind_field(cfg.bbox, cfg.data_dir, offline=cfg.offline_winds)

    # Phase 3: routes and efficiency
    resolver = RouteResolver(cfg.data_dir, offline=cfg.offline_routes or source != "LIVE")
    routes = resolver.resolve_many(flights)
    metrics = [analyse_flight(f, routes.get(f.callsign), winds) for f in flights]
    scored = [m for m in metrics if m.efficiency is not None]
    log.info("Scored %d of %d flights; excess burn %.0f kg CO2/min.",
             len(scored), len(metrics), sum(m.waste_co2_kg_min for m in scored))

    # Phase 5: arrival queueing
    queues = plan_arrivals(metrics, now=now)
    for q in queues:
        if q.overloaded:
            log.info("%s over capacity: %d inbound, %d advisories, %.0f kg CO2 saved by speed control.",
                     q.hub, q.inbound, len(q.advisories), q.total_co2_saved_kg)

    # Phase 4: persist and publish
    run_id = store.save_run(now.strftime("%Y-%m-%dT%H:%M:%SZ"), source, winds.source, winds.valid_time,
                            metrics, queues)
    map_path = build_engine_map(metrics, winds, queues, cfg.bbox, source, cfg.output_dir / "index.html")
    scoreboard_path = build_scoreboard(store, cfg.output_dir / "scoreboard.html")
    (cfg.data_dir / "arrival_advisories_latest.json").write_text(
        json.dumps({"run_id": run_id, "generated_utc": now.isoformat(), "source": source,
                    "hubs": [q.to_dict() for q in queues]}, indent=2),
        encoding="utf-8")
    log.info("Run %d saved. Map: %s  Scoreboard: %s", run_id, map_path, scoreboard_path)
    return RunResult(run_id, source, winds, metrics, queues, map_path, scoreboard_path)
