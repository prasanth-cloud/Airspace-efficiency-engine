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


if __name__ == "__main__":
    unittest.main()
