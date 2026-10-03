"""Phase 4: SQLite persistence for runs, flight metrics and arrival advisories.

Every run is tagged with its telemetry source (LIVE or MOCK) and wind source
(GFS or SIMULATED). Scoreboard queries count LIVE runs only, so simulated data
can never leak into the public airline rankings.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from .efficiency import SCORING_VERSION, FlightMetrics
from .queueing import HubQueue

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT NOT NULL,
    source        TEXT NOT NULL,
    wind_source   TEXT NOT NULL,
    wind_valid    TEXT,
    n_flights     INTEGER NOT NULL,
    n_scored      INTEGER NOT NULL,
    total_waste_co2_kg_min REAL NOT NULL,
    scoring_version INTEGER NOT NULL DEFAULT 1,
    cause_feeds TEXT
);
CREATE TABLE IF NOT EXISTS flight_metrics (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    icao24 TEXT, callsign TEXT, airline TEXT, airline_code TEXT,
    lat REAL, lon REAL, alt_m REAL, gs_ms REAL, track_deg REAL, vrate_ms REAL,
    origin TEXT, destination TEXT, route_source TEXT,
    aircraft_type TEXT, fuel_basis TEXT, cruise_fuel_kg_min REAL,
    orig_lat REAL, orig_lon REAL, dest_lat REAL, dest_lon REAL, dist_to_dest_m REAL,
    wind_u_ms REAL, wind_v_ms REAL, tas_ms REAL, tailwind_ms REAL, crosswind_ms REAL,
    friction_delta_ms REAL, closure_ms REAL, ideal_closure_ms REAL,
    lateral_eff REAL, vertical_eff REAL, best_level_fl INTEGER, efficiency REAL,
    fuel_kg_min REAL, co2_kg_min REAL, waste_co2_kg_min REAL, phase TEXT, rating TEXT,
    waste_lateral_kg_min REAL, waste_vertical_kg_min REAL,
    lateral_cause TEXT, lateral_cause_detail TEXT, cause TEXT, cause_detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_fm_run ON flight_metrics(run_id);
CREATE INDEX IF NOT EXISTS ix_fm_airline ON flight_metrics(airline_code);
CREATE INDEX IF NOT EXISTS ix_fm_callsign ON flight_metrics(callsign);
CREATE TABLE IF NOT EXISTS arrival_advisories (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    hub TEXT, callsign TEXT, eta_utc TEXT, slot_utc TEXT, distance_nm REAL,
    delay_min REAL, absorbed_min REAL, holding_min REAL,
    current_kt INTEGER, advised_kt INTEGER, advised_mach REAL, co2_saved_kg REAL
);
CREATE INDEX IF NOT EXISTS ix_adv_run ON arrival_advisories(run_id);
CREATE TABLE IF NOT EXISTS hub_status (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    hub TEXT, arrival_rate INTEGER, inbound INTEGER, demand_bins TEXT,
    capacity_per_bin REAL, overloaded INTEGER, total_delay_min REAL, total_co2_saved_kg REAL
);
"""

# Columns added after the first release: (name, SQL type)
MIGRATIONS = [
    ("flight_metrics", "aircraft_type", "TEXT"),
    ("flight_metrics", "fuel_basis", "TEXT"),
    ("flight_metrics", "cruise_fuel_kg_min", "REAL"),
    ("runs", "scoring_version", "INTEGER NOT NULL DEFAULT 1"),  # runs before versioning count as 1
    ("runs", "cause_feeds", "TEXT"),                            # NULL for runs before cause attribution
    ("flight_metrics", "waste_lateral_kg_min", "REAL"),
    ("flight_metrics", "waste_vertical_kg_min", "REAL"),
    ("flight_metrics", "lateral_cause", "TEXT"),
    ("flight_metrics", "lateral_cause_detail", "TEXT"),
    ("flight_metrics", "cause", "TEXT"),
    ("flight_metrics", "cause_detail", "TEXT"),
]

METRIC_COLUMNS = [
    "icao24", "callsign", "airline", "lat", "lon", "alt_m", "gs_ms", "track_deg", "vrate_ms",
    "origin", "destination", "route_source", "aircraft_type", "fuel_basis", "cruise_fuel_kg_min",
    "orig_lat", "orig_lon", "dest_lat", "dest_lon",
    "dist_to_dest_m", "wind_u_ms", "wind_v_ms", "tas_ms", "tailwind_ms", "crosswind_ms",
    "friction_delta_ms", "closure_ms", "ideal_closure_ms", "lateral_eff", "vertical_eff",
    "best_level_fl", "efficiency", "fuel_kg_min", "co2_kg_min", "waste_co2_kg_min", "phase", "rating",
    "waste_lateral_kg_min", "waste_vertical_kg_min", "lateral_cause", "lateral_cause_detail", "cause", "cause_detail",
]


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        for table, column, sql_type in MIGRATIONS:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")  # lets the API read while the poller writes
        return conn

    # -- writes ---------------------------------------------------------------

    def save_run(self, ts_utc: str, source: str, wind_source: str, wind_valid: str,
                 metrics: list[FlightMetrics], queues: list[HubQueue],
                 cause_feeds: Optional[list[str]] = None) -> int:
        scored = [m for m in metrics if m.efficiency is not None]
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO runs (ts_utc, source, wind_source, wind_valid, n_flights, n_scored, "
                "total_waste_co2_kg_min, scoring_version, cause_feeds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_utc, source, wind_source, wind_valid, len(metrics), len(scored),
                 sum(m.waste_co2_kg_min or 0 for m in scored), SCORING_VERSION,
                 None if cause_feeds is None else ",".join(cause_feeds)),
            )
            run_id = cur.lastrowid
            placeholders = ", ".join("?" * (len(METRIC_COLUMNS) + 2))
            conn.executemany(
                f"INSERT INTO flight_metrics (run_id, airline_code, {', '.join(METRIC_COLUMNS)}) "
                f"VALUES ({placeholders})",
                [(run_id, m.callsign[:3], *(getattr(m, c) for c in METRIC_COLUMNS)) for m in metrics],
            )
            conn.executemany(
                "INSERT INTO arrival_advisories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, a.hub, a.callsign, a.eta_utc, a.slot_utc, a.distance_nm, a.delay_min,
                  a.absorbed_min, a.holding_min, a.current_kt, a.advised_kt, a.advised_mach,
                  a.co2_saved_kg) for q in queues for a in q.advisories],
            )
            conn.executemany(
                "INSERT INTO hub_status VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, q.hub, q.arrival_rate, q.inbound, ",".join(map(str, q.demand_bins)),
                  q.capacity_per_bin, int(q.overloaded), q.total_delay_min, q.total_co2_saved_kg)
                 for q in queues],
            )
        return run_id

    def prune(self, keep_days: int = 30) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM runs WHERE ts_utc < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                         (f"-{keep_days} days",))

    # -- reads ----------------------------------------------------------------

    def latest_run(self, live_only: bool = False) -> Optional[dict]:
        sql = "SELECT * FROM runs" + (" WHERE source = 'LIVE'" if live_only else "") + " ORDER BY id DESC LIMIT 1"
        with closing(self._connect()) as conn:
            row = conn.execute(sql).fetchone()
        return dict(row) if row else None

    def flights(self, run_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM flight_metrics WHERE run_id = ? ORDER BY callsign", (run_id,))
            return [dict(r) for r in rows]

    def flight(self, run_id: int, callsign: str) -> Optional[dict]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM flight_metrics WHERE run_id = ? AND callsign = ?",
                               (run_id, callsign.upper())).fetchone()
        return dict(row) if row else None

    def advisories(self, run_id: int, hub: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM arrival_advisories WHERE run_id = ?", [run_id]
        if hub:
            sql += " AND hub = ?"
            args.append(hub.upper())
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY hub, slot_utc", args)]

    def hub_status(self, run_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM hub_status WHERE run_id = ? ORDER BY hub", (run_id,))]

    def scoreboard(self, days: int = 7, min_samples: int = 5, include_simulated: bool = False) -> list[dict]:
        """Airline ranking by mean efficiency over scored LIVE observations."""
        source_filter = "" if include_simulated else "AND r.source = 'LIVE'"
        sql = f"""
            SELECT fm.airline_code AS code, MAX(fm.airline) AS airline,
                   COUNT(*) AS samples, COUNT(DISTINCT fm.callsign) AS flights,
                   AVG(fm.efficiency) AS mean_efficiency,
                   AVG(fm.lateral_eff) AS mean_lateral, AVG(fm.vertical_eff) AS mean_vertical,
                   AVG(fm.waste_co2_kg_min) AS mean_waste_kg_min,
                   SUM(fm.waste_co2_kg_min) AS sum_waste_kg_min,
                   AVG(CASE WHEN fm.rating = 'wasteful' THEN 1.0 ELSE 0.0 END) AS wasteful_share
            FROM flight_metrics fm JOIN runs r ON r.id = fm.run_id
            WHERE fm.efficiency IS NOT NULL {source_filter} AND r.scoring_version = {SCORING_VERSION}
              AND r.ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            GROUP BY fm.airline_code HAVING COUNT(*) >= ?
            ORDER BY mean_efficiency DESC
        """
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, (f"-{days} days", min_samples))]

    def route_waste(self, days: int = 7, min_samples: int = 3, include_simulated: bool = False,
                    limit: int = 25) -> dict:
        """Routes ranked by estimated weekly excess CO2, split by cause.

        Each observation stands for the minutes until the next run (capped at
        15). A route's weekly figure is its observed excess CO2 divided by the
        minutes observed, times the minutes in a week. Only runs with cause
        attribution count, and only the part of each flight inside the
        geofence that the engine scores.
        """
        source_filter = "" if include_simulated else "AND source = 'LIVE'"
        lateral = ", ".join(
            f"SUM(CASE WHEN fm.lateral_cause = '{c}' THEN fm.waste_lateral_kg_min * w.minutes ELSE 0 END) AS {c}_kg"
            for c in ("congestion", "weather", "airspace", "routing"))
        sql = f"""
            WITH r AS (
                SELECT id, ts_utc FROM runs
                WHERE cause_feeds IS NOT NULL AND scoring_version = {SCORING_VERSION} {source_filter}
                  AND ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)),
            w AS (
                SELECT id, MIN(15.0, COALESCE(
                    (julianday(LEAD(ts_utc) OVER (ORDER BY ts_utc)) - julianday(ts_utc)) * 1440, 5.0)) AS minutes
                FROM r)
            SELECT fm.origin, fm.destination, COUNT(*) AS samples, COUNT(DISTINCT fm.callsign) AS flights,
                   SUM(fm.waste_co2_kg_min * w.minutes) AS total_kg, {lateral},
                   SUM(COALESCE(fm.waste_vertical_kg_min, 0) * w.minutes) AS flight_level_kg
            FROM flight_metrics fm JOIN w ON w.id = fm.run_id
            WHERE fm.waste_lateral_kg_min IS NOT NULL AND fm.origin IS NOT NULL AND fm.destination IS NOT NULL
            GROUP BY fm.origin, fm.destination HAVING COUNT(*) >= ?
            ORDER BY total_kg DESC
        """
        with closing(self._connect()) as conn:
            minutes = [row[0] for row in conn.execute(f"""
                WITH r AS (SELECT id, ts_utc FROM runs WHERE cause_feeds IS NOT NULL
                           AND scoring_version = {SCORING_VERSION} {source_filter}
                           AND ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?))
                SELECT MIN(15.0, COALESCE(
                    (julianday(LEAD(ts_utc) OVER (ORDER BY ts_utc)) - julianday(ts_utc)) * 1440, 5.0)) FROM r""",
                (f"-{days} days",))]
            rows = [dict(r) for r in conn.execute(sql, (f"-{days} days", min_samples))]
        covered = sum(minutes)
        scale = (7 * 24 * 60 / covered / 1000) if covered else 0.0  # kg observed -> tonnes per week
        routes = []
        for r in rows:
            by_cause = {c: r.pop(f"{c}_kg") * scale for c in ("congestion", "weather", "airspace", "routing",
                                                              "flight_level")}
            routes.append({**{k: r[k] for k in ("origin", "destination", "samples", "flights")},
                           "t_co2_per_week": r["total_kg"] * scale, "by_cause_t_per_week": by_cause,
                           "main_cause": max(by_cause, key=by_cause.get) if any(by_cause.values()) else None})
        return {"runs": len(minutes), "observed_hours": covered / 60, "routes": routes[:limit],
                "by_cause_t_per_week": {c: sum(rt["by_cause_t_per_week"][c] for rt in routes)
                                        for c in ("congestion", "weather", "airspace", "routing", "flight_level")}}

    def run_count(self, live_only: bool = True) -> int:
        with closing(self._connect()) as conn:
            sql = "SELECT COUNT(*) FROM runs" + (" WHERE source = 'LIVE'" if live_only else "")
            return conn.execute(sql).fetchone()[0]
