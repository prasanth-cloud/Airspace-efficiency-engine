"""Engine tests. Run from the project folder:  py -m unittest discover tests -v"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flight_tracker as ft  # noqa: E402
from engine import geo  # noqa: E402
from engine.efficiency import analyse_flight, ideal_closure  # noqa: E402
from engine.flightplan import build_flight_plan  # noqa: E402
from engine.pipeline import EngineConfig, run_once  # noqa: E402
from engine.queueing import plan_arrivals  # noqa: E402
from engine.routes import _route_from_text  # noqa: E402
from engine.winds import WindField, simulated_field  # noqa: E402

BBOX = ft.BOUNDING_BOX


def _v1_schema() -> str:
    """The database schema as first released, before later column migrations."""
    from engine.store import SCHEMA
    return (SCHEMA.replace("aircraft_type TEXT, fuel_basis TEXT, cruise_fuel_kg_min REAL,", "")
                  .replace(",\n    scoring_version INTEGER NOT NULL DEFAULT 1", ""))


V1_SCHEMA = _v1_schema()


def uniform_field(u: float, v: float) -> WindField:
    alts = [0.0, 6000.0, 13000.0]
    grid = lambda val: [[[val, val], [val, val]] for _ in alts]
    return WindField([20.0, 50.0], [-90.0, -60.0], alts, grid(u), grid(v), source="TEST")


def make_flight(lat, lon, track, gs=230.0, alt=11000.0, vrate=0.0, route="ATL to JFK", callsign="DAL100"):
    return ft.Flight("a00001", callsign, "United States", lat, lon, alt, gs, track, vrate, route)


class GeoTests(unittest.TestCase):
    def test_isa_levels(self):
        self.assertAlmostEqual(geo.pressure_to_altitude_m(1013.25), 0, delta=1)
        self.assertAlmostEqual(geo.pressure_to_altitude_m(250) * geo.METERS_TO_FEET, 34_000, delta=150)
        self.assertAlmostEqual(geo.pressure_to_altitude_m(200) * geo.METERS_TO_FEET, 38_660, delta=150)

    def test_wind_to_uv(self):
        u, v = geo.wind_to_uv(10, 270)  # from the west blows east
        self.assertAlmostEqual(u, 10, places=6)
        self.assertAlmostEqual(v, 0, places=6)

    def test_bearing_and_distance(self):
        jfk, bos = (40.6413, -73.7781), (42.3656, -71.0096)
        self.assertAlmostEqual(geo.haversine_m(*jfk, *bos) / 1852, 162, delta=3)
        self.assertAlmostEqual(geo.initial_bearing(*jfk, *bos), 49, delta=2)

    def test_icao_latlon(self):
        self.assertEqual(geo.format_icao_latlon(40.6413, -73.7781), "4038N07347W")


class WindTests(unittest.TestCase):
    def test_interpolation_is_exact_on_nodes_and_linear_between(self):
        wf = simulated_field(BBOX)
        lat, lon, alt = wf.lats[3], wf.lons[4], wf.alts_m[6]
        self.assertAlmostEqual(wf.uv(lat, lon, alt)[0], wf.u[6][3][4], places=9)
        mid_alt = (wf.alts_m[6] + wf.alts_m[7]) / 2
        expected = (wf.u[6][3][4] + wf.u[7][3][4]) / 2
        self.assertAlmostEqual(wf.uv(lat, lon, mid_alt)[0], expected, places=9)

    def test_clamps_outside_grid(self):
        wf = simulated_field(BBOX)
        self.assertEqual(wf.uv(80, 0, 50_000), wf.uv(BBOX["lamax"], BBOX["lomax"], wf.alts_m[-1]))


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.route = _route_from_text("ATL to JFK")
        self.start = geo.great_circle_point(33.6407, -84.4277, 40.6413, -73.7781, 0.5)
        self.course = geo.initial_bearing(*self.start, 40.6413, -73.7781)

    def test_direct_flight_in_calm_air_is_fully_efficient_laterally(self):
        m = analyse_flight(make_flight(*self.start, self.course), self.route, uniform_field(0, 0))
        self.assertAlmostEqual(m.lateral_eff, 1.0, places=6)
        self.assertAlmostEqual(m.tas_ms, 230, places=6)

    def test_thirty_degree_dogleg_costs_cosine(self):
        m = analyse_flight(make_flight(*self.start, self.course + 30), self.route, uniform_field(0, 0))
        self.assertAlmostEqual(m.lateral_eff, math.cos(math.radians(30)), places=6)
        self.assertGreater(m.waste_co2_kg_min, 0)
        self.assertEqual(m.rating, "wasteful")

    def test_wind_triangle(self):
        u, v = 30.0, 0.0  # 30 m/s westerly
        m = analyse_flight(make_flight(*self.start, 90.0), self.route, uniform_field(u, v))
        self.assertAlmostEqual(m.tailwind_ms, 30, places=6)
        self.assertAlmostEqual(m.tas_ms, 200, places=6)
        self.assertAlmostEqual(m.friction_delta_ms, 30, places=6)

    def test_ideal_closure_with_crosswind(self):
        self.assertAlmostEqual(ideal_closure(200, 0, 0, 0), 200)
        self.assertAlmostEqual(ideal_closure(200, 60, 0, 0), math.sqrt(200 ** 2 - 60 ** 2))
        self.assertIsNone(ideal_closure(50, 60, 0, 0))

    def test_terminal_area_not_scored(self):
        m = analyse_flight(make_flight(*self.start, self.course, alt=2000), self.route, uniform_field(0, 0))
        self.assertEqual(m.phase, "terminal")
        self.assertIsNone(m.efficiency)

    def test_unknown_route_gets_no_lateral_score(self):
        f = make_flight(*self.start, self.course, route=None)
        m = analyse_flight(f, None, uniform_field(0, 0))
        self.assertIsNone(m.lateral_eff)


class QueueTests(unittest.TestCase):
    def test_bunched_arrivals_get_speed_advisories(self):
        wf = uniform_field(0, 0)
        jfk = (40.6413, -73.7781)
        metrics = []
        for i in range(30):  # 30 aircraft all about 400 NM out at the same moment
            pos = geo.great_circle_point(33.6407, -84.4277, *jfk, 0.4 + i * 0.0005)
            course = geo.initial_bearing(*pos, *jfk)
            metrics.append(analyse_flight(make_flight(*pos, course, callsign=f"DAL{i + 1}"),
                                          _route_from_text("ATL to JFK"), wf))
        q = [x for x in plan_arrivals(metrics, now=datetime(2026, 1, 1, tzinfo=timezone.utc)) if x.hub == "JFK"][0]
        self.assertTrue(q.overloaded)
        self.assertEqual(len(q.advisories), 29)  # everyone but the first needs a slot delay
        last = q.advisories[-1]
        self.assertGreater(last.absorbed_min, 0)
        self.assertLess(last.advised_kt, last.current_kt)
        self.assertAlmostEqual(last.absorbed_min + last.holding_min, last.delay_min, delta=0.15)
        self.assertGreaterEqual(last.advised_kt, last.current_kt * 0.93 - 1)


class FlightPlanTests(unittest.TestCase):
    def test_fpl_format(self):
        row = {"callsign": "DAL100", "origin": "ATL", "destination": "JFK", "orig_lat": 33.6407, "orig_lon": -84.4277,
               "dest_lat": 40.6413, "dest_lon": -73.7781, "lat": 36.0, "lon": -80.0, "best_level_fl": 360, "tas_ms": 235.0}
        fpl = build_flight_plan(row, advised_kt=430, now=datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc))
        self.assertTrue(fpl.startswith("(FPL-DAL100-IS\n-ZZZZ/M-"))
        self.assertIn("-KATL1430\n", fpl)
        self.assertIn("-N0430F360 DCT ", fpl)
        self.assertIn("\n-KJFK", fpl)
        self.assertTrue(fpl.endswith(")"))

    def test_requires_destination(self):
        with self.assertRaises(ValueError):
            build_flight_plan({"callsign": "X", "dest_lat": None, "dest_lon": None, "lat": 0, "lon": 0})


class PipelineAndApiTests(unittest.TestCase):
    def test_offline_cycle_and_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = EngineConfig(bbox=BBOX, data_dir=Path(tmp) / "data", output_dir=Path(tmp),
                               force_mock=True, offline_winds=True, offline_routes=True)
            result = run_once(cfg)
            self.assertTrue(result.map_path.exists())
            self.assertIn("SIMULATED PREVIEW", result.scoreboard_path.read_text(encoding="utf-8"))
            self.assertTrue(any(m.efficiency is not None for m in result.metrics))

            try:
                from fastapi.testclient import TestClient
            except ImportError:
                self.skipTest("fastapi test client not installed")
            from engine.api import create_app
            os.environ["ENGINE_API_KEYS"] = "secret-key"
            try:
                client = TestClient(create_app(cfg.db_path))
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/v1/flights").status_code, 401)
                h = {"X-API-Key": "secret-key"}
                flights = client.get("/v1/flights?rating=wasteful", headers=h).json()
                self.assertEqual(flights["source"], "MOCK")
                cs = next(f["callsign"] for f in flights["flights"] if f["dest_lat"] is not None)
                self.assertEqual(client.get(f"/v1/flights/{cs}", headers=h).status_code, 200)
                plan = client.get(f"/v1/flights/{cs}/flight-plan", headers=h)
                self.assertTrue(plan.text.startswith(f"(FPL-{cs}-IS"))
                matrix = client.get("/v1/hubs/JFK/speed-matrix", headers=h).json()
                self.assertEqual(matrix["hub"], "JFK")
                self.assertEqual(client.get("/v1/scoreboard", headers=h).json()["airlines"], [])
            finally:
                os.environ.pop("ENGINE_API_KEYS", None)


class AircraftTypeTests(unittest.TestCase):
    def setUp(self):
        self.route = _route_from_text("ATL to JFK")
        self.start = geo.great_circle_point(33.6407, -84.4277, 40.6413, -73.7781, 0.5)
        self.course = geo.initial_bearing(*self.start, 40.6413, -73.7781)

    def test_widebody_burns_more_than_default(self):
        f = make_flight(*self.start, self.course + 20)
        default = analyse_flight(f, self.route, uniform_field(0, 0))
        wide = analyse_flight(f, self.route, uniform_field(0, 0), aircraft_type="B77W")
        self.assertEqual(default.fuel_basis, "default")
        self.assertEqual(wide.fuel_basis, "type")
        self.assertAlmostEqual(wide.co2_kg_min / default.co2_kg_min, 7500 / 2400, places=6)
        self.assertAlmostEqual(wide.efficiency, default.efficiency, places=9)

    def test_unknown_type_keeps_code_but_uses_default_fuel(self):
        m = analyse_flight(make_flight(*self.start, self.course), self.route, uniform_field(0, 0), aircraft_type="b712")
        self.assertEqual(m.aircraft_type, "B712")
        self.assertEqual(m.fuel_basis, "default")

    def test_flight_plan_uses_type_and_wake(self):
        row = {"callsign": "UAL1", "origin": "EWR", "destination": "BOS", "orig_lat": 40.6895, "orig_lon": -74.1745,
               "dest_lat": 42.3656, "dest_lon": -71.0096, "lat": 41.0, "lon": -73.0, "aircraft_type": "B789"}
        fpl = build_flight_plan(row)
        self.assertIn("\n-B789/H-", fpl)
        self.assertNotIn("TYP/", fpl)
        row["aircraft_type"] = "B712"
        self.assertIn("TYP/B712", build_flight_plan(row))

    def test_resolver_sources_and_cache(self):
        from unittest import mock
        from engine.aircraft import AircraftResolver

        def fake_get(self_, url, timeout=None):
            r = mock.Mock()
            icao = url.rsplit("/", 1)[1]
            if "adsbdb" in url:
                r.status_code = 200 if icao == "a1" else 404
                r.json.return_value = {"response": {"aircraft": {"icao_type": "A21N"}}}
            else:
                r.status_code = 200 if icao == "a2" else (503 if icao == "a4" else 404)
                r.json.return_value = {"typecode": "E75L"}
            return r

        flights = [make_flight(30, -80, 0, callsign=f"X{i}") for i in range(4)]
        for f, icao in zip(flights, ["a1", "a2", "a3", "a4"]):
            f.icao24 = icao
        with tempfile.TemporaryDirectory() as tmp, mock.patch("requests.Session.get", fake_get):
            resolver = AircraftResolver(Path(tmp), type_index={})
            self.assertEqual(resolver.resolve_many(flights), {"a1": "A21N", "a2": "E75L", "a3": None, "a4": None})
            self.assertIn("a3", resolver.cache)        # unknown everywhere: negative-cached
            self.assertNotIn("a4", resolver.cache)     # transient error: retried next run
            self.assertEqual(AircraftResolver(Path(tmp), type_index={}).cache["a1"]["type"], "A21N")

    def test_store_migrates_old_database(self):
        import sqlite3
        from engine.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite3"
            v1_schema = V1_SCHEMA
            conn = sqlite3.connect(db)
            conn.executescript(v1_schema)
            conn.close()
            Store(db)
            conn = sqlite3.connect(db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(flight_metrics)")}
            conn.close()
            self.assertTrue({"aircraft_type", "fuel_basis", "cruise_fuel_kg_min"} <= cols)
            conn = sqlite3.connect(db)
            run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            conn.close()
            self.assertIn("scoring_version", run_cols)


class ValidationTests(unittest.TestCase):
    def test_fuel_model_within_tolerance_of_published_trip_fuel(self):
        from engine.validation import check_fuel_model
        checks = check_fuel_model()
        self.assertGreaterEqual(len(checks), 10)
        failures = [f"{c.ref.icao_type} {c.error * 100:+.1f}%" for c in checks if not c.passed]
        self.assertEqual(failures, [])

    def test_live_check_migrates_old_database(self):
        import sqlite3
        from engine.validation import check_live_data
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.sqlite3"
            conn = sqlite3.connect(db)
            conn.executescript(V1_SCHEMA)
            conn.close()
            self.assertEqual(check_live_data(db).runs, 0)

    def test_live_check_ignores_runs_from_older_scoring(self):
        import sqlite3
        from engine.store import Store
        from engine.validation import check_live_data
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "engine.sqlite3"
            store = Store(db)
            route = _route_from_text("ATL to JFK")
            pos = geo.great_circle_point(33.6407, -84.4277, 40.6413, -73.7781, 0.5)
            course = geo.initial_bearing(*pos, 40.6413, -73.7781)
            good = analyse_flight(make_flight(*pos, course + 10), route, uniform_field(0, 0))
            store.save_run("2026-10-03T15:00:00Z", "LIVE", "GFS", "", [good], [])
            conn = sqlite3.connect(db)  # a run scored under the old rules, all wrong routes
            conn.execute("INSERT INTO runs (ts_utc, source, wind_source, n_flights, n_scored, "
                         "total_waste_co2_kg_min, scoring_version) VALUES ('2026-10-03T14:00:00Z', 'LIVE', 'GFS', 1, 1, 0, 1)")
            conn.execute("INSERT INTO flight_metrics (run_id, callsign, lateral_eff, route_source) "
                         "VALUES (last_insert_rowid(), 'OLD1', 0.0, 'adsbdb')")
            conn.commit()
            conn.close()
            live = check_live_data(db)
            self.assertEqual(live.runs, 1)
            self.assertAlmostEqual(live.mean_route_extension, 1 - math.cos(math.radians(10)), places=6)

    def test_report_without_live_data(self):
        from engine.validation import run_validation
        with tempfile.TemporaryDirectory() as tmp:
            passed, report = run_validation(Path(tmp) / "none.sqlite3", Path(tmp) / "report.md")
            self.assertTrue(passed)
            self.assertIn("No LIVE runs recorded yet", report)
            self.assertTrue((Path(tmp) / "report.md").exists())


class RouteMismatchTests(unittest.TestCase):
    """Stale callsign routes must not be scored as wasted fuel."""

    def setUp(self):
        from engine.routes import Endpoint, Route
        self.route = Route(Endpoint("KATL", "ATL", "Atlanta", 33.6407, -84.4277),
                           Endpoint("KBOS", "BOS", "Boston", 42.3656, -71.0096), "adsbdb")
        self.mid = geo.great_circle_point(33.6407, -84.4277, 42.3656, -71.0096, 0.5)
        self.course = geo.initial_bearing(*self.mid, 42.3656, -71.0096)

    def test_on_route_with_detour_is_plausible(self):
        from engine.routes import route_mismatch
        self.assertIsNone(route_mismatch(*self.mid, self.course + 35, self.route))
        offset = geo.great_circle_point(*self.mid, 40.0, -72.0, 0.15)  # pushed some way off the line
        self.assertIsNone(route_mismatch(*offset, self.course, self.route))

    def test_reverse_direction_is_rejected(self):
        from engine.routes import route_mismatch
        self.assertIn("heading", route_mismatch(*self.mid, (self.course + 180) % 360, self.route))

    def test_far_off_route_or_beyond_ends_is_rejected(self):
        from engine.routes import route_mismatch
        self.assertIn("off the route", route_mismatch(27.0, -80.0, 45.0, self.route))  # over Florida
        self.assertIn("beyond", route_mismatch(30.0, -88.5, 45.0, self.route))       # behind Atlanta

    def test_mismatched_route_is_not_scored_laterally(self):
        f = make_flight(*self.mid, (self.course + 180) % 360)
        m = analyse_flight(f, self.route, uniform_field(0, 0))
        self.assertEqual(m.route_source, "mismatch")
        self.assertIsNone(m.lateral_eff)
        self.assertIsNone(m.destination)

    def test_terminal_radius_is_40nm(self):
        from engine.efficiency import TERMINAL_RADIUS_M
        self.assertAlmostEqual(TERMINAL_RADIUS_M, 74_080)


class BulkAircraftDatabaseTests(unittest.TestCase):
    def test_index_built_from_opensky_csv_and_used_first(self):
        from engine import aircraft
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / aircraft.AIRCRAFT_DB_CSV).write_text(
                "'icao24','registration','manufacturericao','typecode','model'\n"
                "'a1b2c3','N123','BOEING','B738','737-823'\n"
                "'abcdef','N9','AIRBUS','',''\n"
                "'4ca123','EI-ABC','AIRBUS','A20N','A320-251N'\n", encoding="utf-8")
            index = aircraft.ensure_type_index(d, offline=True)
            self.assertEqual(index, {"a1b2c3": "B738", "4ca123": "A20N"})
            flights = [make_flight(30, -80, 0), make_flight(30, -80, 0)]
            flights[0].icao24, flights[1].icao24 = "A1B2C3", "abcdef"
            resolver = aircraft.AircraftResolver(d, offline=True, type_index=index)
            self.assertEqual(resolver.resolve_many(flights), {"A1B2C3": "B738", "abcdef": None})

    def test_rate_limit_stops_further_lookups(self):
        from unittest import mock
        from engine.aircraft import AircraftResolver
        calls = []

        def fake_get(self_, url, timeout=None):
            calls.append(url)
            r = mock.Mock(status_code=429)
            return r

        flights = [make_flight(30, -80, 0) for _ in range(40)]
        for i, f in enumerate(flights):
            f.icao24 = f"a{i:05d}"
        with tempfile.TemporaryDirectory() as tmp, mock.patch("requests.Session.get", fake_get):
            result = AircraftResolver(Path(tmp), type_index={}).resolve_many(flights)
        self.assertTrue(all(v is None for v in result.values()))
        self.assertLess(len(calls), 40)  # the breaker stopped the batch early


class OpenSkyHistoryTests(unittest.TestCase):
    """Routes from OpenSky flight history replace stale callsign routes."""

    AIRPORTS_CSV = ("id,ident,type,name,latitude_deg,longitude_deg,gps_code,iata_code\n"
                    "1,KMCO,large_airport,Orlando International,28.4294,-81.3090,KMCO,MCO\n"
                    "2,KPVD,medium_airport,Providence,41.7240,-71.4282,KPVD,PVD\n"
                    "3,X01,heliport,Somewhere,30,-80,,\n")

    def make_history(self, tmp, rows, now):
        from unittest import mock
        from engine.history import FlightHistory, build_airport_index
        airports = build_airport_index(self.AIRPORTS_CSV)
        self.assertEqual(set(airports), {"KMCO", "KPVD"})
        history = FlightHistory(Path(tmp), token_fn=lambda s: "token", airports=airports)
        calls = []

        def fake_get(self_, url, params=None, timeout=None):
            calls.append(params)
            start = params["begin"]
            body = [r for r in rows if start <= r["lastSeen"] < start + 7200]
            return mock.Mock(status_code=200 if body else 404, json=lambda: body, raise_for_status=lambda: None)

        with mock.patch("requests.Session.get", fake_get):
            history.refresh(now=now)
        return history, calls

    def test_history_route_replaces_stale_adsbdb_route(self):
        from engine.routes import Endpoint, Route, choose_route
        now = 1_790_000_000
        rows = [
            # yesterday DAL123 flew ATL-BOS, today the aircraft landed at MCO and DAL123 now flies MCO-PVD
            {"callsign": "DAL123 ", "icao24": "a1b2c3", "estDepartureAirport": "KATL",
             "estArrivalAirport": "KBOS", "firstSeen": now - 30 * 3600, "lastSeen": now - 28 * 3600},
            {"callsign": "DAL123 ", "icao24": "a00001", "estDepartureAirport": "KMCO",
             "estArrivalAirport": "KPVD", "firstSeen": now - 26 * 3600, "lastSeen": now - 24 * 3600},
            {"callsign": "DAL900", "icao24": "a1b2c3", "estDepartureAirport": "KJFK",
             "estArrivalAirport": "KMCO", "firstSeen": now - 6 * 3600, "lastSeen": now - 3 * 3600},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            history, calls = self.make_history(tmp, rows, now)
            self.assertEqual(len(calls), 4)  # a few windows per cycle, newest first
            self.assertEqual(history.routes_for("DAL123"), [])  # older windows not fetched yet
            for _ in range(6):
                history, _ = self.make_history(tmp, rows, now)
            self.assertEqual(history.routes_for("DAL123"), [("KMCO", "KPVD"), ("KATL", "KBOS")])
            self.assertEqual(history.departure_hint("A1B2C3", now=now), "KMCO")

            stale = Route(Endpoint("KATL", "ATL", "Atlanta", 33.6407, -84.4277),
                          Endpoint("KBOS", "BOS", "Boston", 42.3656, -71.0096), "adsbdb")
            mid = geo.great_circle_point(28.4294, -81.3090, 41.7240, -71.4282, 0.5)
            f = make_flight(*mid, geo.initial_bearing(*mid, 41.7240, -71.4282))
            f.callsign, f.icao24 = "DAL123", "a1b2c3"
            route = choose_route(f, stale, history)
            self.assertEqual((route.origin.code, route.destination.iata, route.source), ("KMCO", "PVD", "opensky"))
            m = analyse_flight(f, route, uniform_field(0, 0))
            self.assertEqual(m.route_source, "opensky")
            self.assertIsNotNone(m.lateral_eff)

            # an aircraft on the ATL-BOS line keeps that route, now confirmed by OpenSky
            on_atl = geo.great_circle_point(33.6407, -84.4277, 42.3656, -71.0096, 0.5)
            g = make_flight(*on_atl, geo.initial_bearing(*on_atl, 42.3656, -71.0096))
            g.callsign, g.icao24 = "DAL123", "ffffff"
            self.assertEqual(choose_route(g, stale, history).source, "opensky")
            # no history for the callsign: the adsbdb route is left to the mismatch test
            g.callsign = "UAL1"
            self.assertIs(choose_route(g, stale, history), stale)

    def test_no_credentials_means_no_requests(self):
        from unittest import mock
        from engine.history import FlightHistory
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("requests.Session.get", side_effect=AssertionError("no network expected")):
            self.assertEqual(FlightHistory(Path(tmp), token_fn=lambda s: None, airports={}).refresh(), 0)
            self.assertEqual(FlightHistory(Path(tmp), offline=True, token_fn=lambda s: "t", airports={}).refresh(), 0)

    def test_rate_limit_stops_the_backfill(self):
        from unittest import mock
        from engine.history import FlightHistory
        calls = []

        def fake_get(self_, url, params=None, timeout=None):
            calls.append(params)
            return mock.Mock(status_code=429)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("requests.Session.get", fake_get):
            history = FlightHistory(Path(tmp), token_fn=lambda s: "t", airports={})
            self.assertEqual(history.refresh(), 0)
        self.assertEqual(len(calls), 1)


NAS_XML = """<AIRPORT_STATUS_INFORMATION><Update_Time>now</Update_Time>
<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><listLength>1</listLength>
  <Ground_Delay><ARPT>BOS</ARPT><Reason>low ceilings</Reason><Avg>1 hour and 2 minutes</Avg><Max>2 hours</Max></Ground_Delay>
</Ground_Delay_List></Delay_type>
<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List>
  <Delay><ARPT>ATL</ARPT><Reason>VOL:Volume</Reason>
    <Arrival_Departure Type="Arrival"><Min>16 minutes</Min><Max>30 minutes</Max><Trend>Increasing</Trend></Arrival_Departure></Delay>
  <Delay><ARPT>MCO</ARPT><Reason>TM Initiatives</Reason>
    <Arrival_Departure Type="Departure"><Min>31 minutes</Min><Max>45 minutes</Max></Arrival_Departure></Delay>
</Arrival_Departure_Delay_List></Delay_type>
<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List>
  <Program><ARPT>LGA</ARPT><Reason>thunderstorms</Reason><End_Time>18:00</End_Time></Program>
</Ground_Stop_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List>
  <Airport><ARPT>PHL</ARPT><Reason>runway work</Reason></Airport></Airport_Closure_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>"""


class CauseAttributionTests(unittest.TestCase):
    """Each flight's excess CO2 is put down to congestion, weather, airspace, routing or flight level."""

    def scored(self, lat, lon, dest, track_offset=40.0, alt_m=11_000):
        from engine.airports import AIRPORTS
        from engine.routes import Endpoint, Route
        d = AIRPORTS[dest]
        origin = Endpoint("KTST", "TST", "Test origin", *geo.great_circle_point(
            d.lat, d.lon, lat, lon, 1 + 600_000 / geo.haversine_m(d.lat, d.lon, lat, lon)))  # 600 km behind
        route = Route(origin, Endpoint(d.icao, d.iata, d.name, d.lat, d.lon), "opensky")
        f = make_flight(lat, lon, geo.initial_bearing(lat, lon, d.lat, d.lon) + track_offset)
        f.baro_altitude_m = alt_m
        m = analyse_flight(f, route, uniform_field(0, 0))
        self.assertEqual(m.route_source, "opensky")
        self.assertIsNotNone(m.waste_co2_kg_min)
        return m

    def test_nas_status_parsing(self):
        from engine.causes import parse_nas_status
        d = parse_nas_status(NAS_XML)
        self.assertEqual(set(d), {"BOS", "ATL", "LGA"})  # departure delays and closures are ignored
        self.assertEqual((d["BOS"].kind, d["BOS"].minutes), ("Ground Delay Program", 62))
        self.assertEqual((d["ATL"].kind, d["ATL"].minutes, d["ATL"].reason), ("Arrival delay", 30, "VOL:Volume"))
        self.assertEqual(d["LGA"].kind, "Ground Stop")

    def test_congestion_only_near_the_delayed_destination(self):
        from engine.causes import CauseContext, attribute, parse_nas_status
        ctx = CauseContext(hub_delays=parse_nas_status(NAS_XML))
        near = self.scored(*geo.great_circle_point(33.64, -84.43, 42.3656, -71.0096, 0.85), "BOS")
        attribute(near, ctx)
        self.assertEqual(near.lateral_cause, "congestion")
        self.assertIn("ground delay program", near.lateral_cause_detail)
        far = self.scored(*geo.great_circle_point(33.64, -84.43, 42.3656, -71.0096, 0.3), "BOS")
        attribute(far, ctx)
        self.assertNotEqual(far.lateral_cause, "congestion")
        self.assertAlmostEqual(far.waste_lateral_kg_min + far.waste_vertical_kg_min, far.waste_co2_kg_min)

    def test_weather_airspace_and_routing(self):
        from engine.causes import CauseContext, attribute, parse_sigmets
        start = (35.0, -80.0)  # Charlotte area, flying to Boston
        storm = parse_sigmets([{"hazard": "CONVECTIVE", "seriesId": "42E",
                                "coords": [{"lat": 37.5, "lon": -78.5}, {"lat": 37.5, "lon": -76.5},
                                           {"lat": 38.5, "lon": -76.5}, {"lat": 38.5, "lon": -78.5}]},
                               {"hazard": "TURB", "coords": [{"lat": 0, "lon": 0}] * 3}])
        self.assertEqual([n for n, _ in storm], ["Convective SIGMET 42E"])
        m = self.scored(*start, "BOS")
        attribute(m, CauseContext(sigmets=storm))
        self.assertEqual((m.lateral_cause, m.lateral_cause_detail), ("weather", "Convective SIGMET 42E"))

        offshore = self.scored(31.0, -77.0, "BOS")  # direct path runs up the offshore warning areas
        attribute(offshore, CauseContext())
        self.assertEqual(offshore.lateral_cause, "airspace")
        self.assertIn("approx.", offshore.lateral_cause_detail)

        inland = self.scored(40.0, -83.0, "JFK")
        attribute(inland, CauseContext())
        self.assertEqual(inland.lateral_cause, "routing")

    def test_flight_level_is_main_cause_when_vertical_waste_dominates(self):
        from engine.causes import CauseContext, attribute
        m = self.scored(40.0, -83.0, "JFK", track_offset=2.0, alt_m=8_600)  # on course, FL280
        attribute(m, CauseContext())
        self.assertGreater(m.waste_vertical_kg_min, m.waste_lateral_kg_min)
        self.assertEqual(m.cause, "flight_level")

    def test_route_waste_report(self):
        from engine.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            cfg = EngineConfig(bbox=BBOX, data_dir=Path(tmp) / "data", output_dir=Path(tmp),
                               force_mock=True, offline_winds=True, offline_routes=True)
            store = Store(cfg.db_path)
            run_once(cfg, store)
            run_once(cfg, store)
            report = store.route_waste(include_simulated=True, min_samples=1)
            self.assertEqual(report["runs"], 2)
            self.assertTrue(report["routes"])
            top = report["routes"][0]
            self.assertAlmostEqual(sum(top["by_cause_t_per_week"].values()), top["t_co2_per_week"], places=6)
            self.assertEqual(store.route_waste()["routes"], [])  # simulated runs never reach the public view
            page = (Path(tmp) / "scoreboard.html").read_text(encoding="utf-8")
            self.assertIn("Routes wasting the most CO2, and why", page)


if __name__ == "__main__":
    unittest.main()
