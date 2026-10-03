"""Validation of the carbon model against published reference figures.

Two kinds of check:

1. **Fuel model (offline).** The engine's own fuel-flow function is flown
   through a simple climb, cruise and descent profile over the equivalent
   still-air distance (ESAD) of published sample flights. The resulting trip
   fuel is compared with the trip fuel those sources report for the same
   aircraft type and distance.

2. **Live plausibility (needs LIVE runs in the database).** Fleet-wide
   averages from recorded live traffic are compared with the FAA/EUROCONTROL
   US benchmark for en-route route extension, and coverage of the aircraft
   type and route lookups is reported.

Run it with ``py run_engine.py --validate``.

Sources
-------
[AC137] Aircraft Commerce No. 137 (2021), single-aisle fuel burn analysis:
        https://www.aircraft-commerce.com/wp-content/uploads/aircraft-commerce-docs/General%20Articles/2021/137_FLTOPS.pdf
[AC121] Aircraft Commerce No. 121 (Dec 2018/Jan 2019), "A350-900/-1000 fuel burn & operating performance":
        https://www.aircraft-commerce.com/sample_article_folder/121_FLTOPS_A.pdf
[FAA17] FAA/EUROCONTROL, "Comparison of Air Traffic Management-Related Operational Performance: U.S./Europe" (2017):
        https://www.faa.gov/air_traffic/publications/media/us_eu_comparison_2017.pdf
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .aircraft import AircraftPerformance, performance_for
from .efficiency import fuel_flow_kg_min
from .geo import METERS_TO_FEET

KG_PER_USG = 3.785411784 * 0.8  # Jet A at 0.8 kg/L
FUEL_TOLERANCE = 0.15            # pass if within +/-15% of the published trip fuel

# Profile used to turn a cruise fuel flow into a trip fuel
CLIMB_MIN, CLIMB_NM = 22, 130
DESCENT_MIN, DESCENT_NM = 25, 120
PROFILE_ALT_FT = 18_000          # representative altitude during climb and descent
CRUISE_ALT_FT = 37_000
VRATE_CLIMB_MS, VRATE_DESCENT_MS = 8.0, -8.0

# FAA17: US horizontal en-route flight inefficiency, flights to and from the main 34 airports (2017)
US_ROUTE_EXTENSION_BENCHMARK = 0.0286
ROUTE_EXTENSION_PLAUSIBLE = (0.005, 0.10)


@dataclass(frozen=True)
class ReferenceTrip:
    icao_type: str
    description: str
    esad_nm: float
    trip_fuel_usg: float
    source: str


REFERENCE_TRIPS = [
    ReferenceTrip("A20N", "A320neo LEAP-1A26, BOS-LAX, 150 pax", 2730, 4541, "AC137"),
    ReferenceTrip("B38M", "737-8 LEAP-1B27, BOS-LAX, 150 pax", 2730, 4578, "AC137"),
    ReferenceTrip("A21N", "A321neo LEAP-1A32, BOS-LAX, 170 pax", 2730, 5012, "AC137"),
    ReferenceTrip("A320", "A320ceo CFM56-5B4/P, BOS-SEA, 150 pax", 2517, 5126, "AC137"),
    ReferenceTrip("B738", "737-800W CFM56-7B26, BOS-SEA, 150 pax", 2517, 4863, "AC137"),
    ReferenceTrip("A332", "A330-200 Trent 772C, LHR-AUS, 248 seats", 4734, 19145, "AC121"),
    ReferenceTrip("B788", "787-8 GEnx-1B67, LHR-AUS, 220 seats", 4734, 15434, "AC121"),
    ReferenceTrip("B789", "787-9 GEnx-1B74/75, LHR-AUS, 266 seats", 4734, 16422, "AC121"),
    ReferenceTrip("B78X", "787-10 GEnx-1B74/75, LHR-AUS, 337 seats", 4734, 17907, "AC121"),
    ReferenceTrip("A35K", "A350-1000, LHR-GRU, 367 seats", 5539, 26147, "AC121"),
    ReferenceTrip("B744", "747-400 3-class, LHR-EZE, 393 seats", 6568, 49763, "AC121"),
]


@dataclass
class FuelCheck:
    ref: ReferenceTrip
    published_kg: float
    estimated_kg: float

    @property
    def error(self) -> float:
        return (self.estimated_kg - self.published_kg) / self.published_kg

    @property
    def passed(self) -> bool:
        return abs(self.error) <= FUEL_TOLERANCE


def cruise_tas_kt(perf: AircraftPerformance) -> float:
    """Typical cruise true airspeed by aircraft class."""
    if perf.wake in ("H", "J"):
        return 485.0
    return 450.0 if perf.cruise_fuel_kg_h >= 2000 else 420.0


def estimate_trip_fuel_kg(perf: AircraftPerformance, esad_nm: float) -> float:
    """Trip fuel from the engine's fuel-flow model over a climb/cruise/descent profile."""
    cruise = perf.cruise_fuel_kg_min
    profile_alt = PROFILE_ALT_FT / METERS_TO_FEET
    climb = CLIMB_MIN * fuel_flow_kg_min(profile_alt, VRATE_CLIMB_MS, cruise)
    descent = DESCENT_MIN * fuel_flow_kg_min(profile_alt, VRATE_DESCENT_MS, cruise)
    cruise_nm = max(0.0, esad_nm - CLIMB_NM - DESCENT_NM)
    cruise_min = cruise_nm / cruise_tas_kt(perf) * 60
    level = fuel_flow_kg_min(CRUISE_ALT_FT / METERS_TO_FEET, 0.0, cruise)
    return climb + descent + cruise_min * level


def check_fuel_model() -> list[FuelCheck]:
    checks = []
    for ref in REFERENCE_TRIPS:
        perf, known = performance_for(ref.icao_type)
        if not known:
            raise ValueError(f"Reference type {ref.icao_type} missing from the performance table")
        checks.append(FuelCheck(ref, ref.trip_fuel_usg * KG_PER_USG, estimate_trip_fuel_kg(perf, ref.esad_nm)))
    return checks


@dataclass
class LiveCheck:
    runs: int
    scored_samples: int
    mean_route_extension: Optional[float]
    type_coverage: Optional[float]
    route_coverage: Optional[float]

    @property
    def route_extension_plausible(self) -> Optional[bool]:
        if self.mean_route_extension is None:
            return None
        lo, hi = ROUTE_EXTENSION_PLAUSIBLE
        return lo <= self.mean_route_extension <= hi


def check_live_data(db_path: Path) -> LiveCheck:
    """Fleet-wide plausibility from LIVE runs only.

    Lateral inefficiency (1 - lateral efficiency) on en-route aircraft is the
    engine's equivalent of route extension; FAA17 puts the US average at 2.86%.
    """
    if not db_path.exists():
        return LiveCheck(0, 0, None, None, None)
    with closing(sqlite3.connect(db_path)) as conn:
        runs = conn.execute("SELECT COUNT(*) FROM runs WHERE source = 'LIVE'").fetchone()[0]
        row = conn.execute("""
            SELECT COUNT(fm.lateral_eff), AVG(1 - fm.lateral_eff),
                   AVG(CASE WHEN fm.fuel_basis = 'type' THEN 1.0 ELSE 0.0 END),
                   AVG(CASE WHEN fm.route_source != 'unknown' THEN 1.0 ELSE 0.0 END)
            FROM flight_metrics fm JOIN runs r ON r.id = fm.run_id
            WHERE r.source = 'LIVE'
        """).fetchone()
    samples, ext, type_cov, route_cov = row
    return LiveCheck(runs, samples or 0, ext, type_cov, route_cov)


def render_report(fuel: list[FuelCheck], live: LiveCheck) -> str:
    lines = ["# Carbon model validation", "", "## 1. Fuel model vs published trip fuel", "",
             "| Aircraft and sample flight | ESAD nm | Published kg | Engine kg | Error | Result |",
             "|---|---:|---:|---:|---:|---|"]
    for c in fuel:
        lines.append(f"| {c.ref.description} [{c.ref.source}] | {c.ref.esad_nm:,.0f} | {c.published_kg:,.0f} | "
                     f"{c.estimated_kg:,.0f} | {c.error * 100:+.1f}% | {'PASS' if c.passed else 'FAIL'} |")
    mean_abs = sum(abs(c.error) for c in fuel) / len(fuel)
    lines += ["", f"Mean absolute error {mean_abs * 100:.1f}%, tolerance {FUEL_TOLERANCE * 100:.0f}% per flight. "
              "Published figures are for specific payloads and engines; the engine uses one mid-weight value per type.",
              "", "## 2. Live traffic plausibility", ""]
    if live.runs == 0:
        lines.append("No LIVE runs recorded yet. Run `py run_engine.py` with network access, then validate again.")
    else:
        verdict = {True: "PLAUSIBLE", False: "OUT OF RANGE", None: "no data"}[live.route_extension_plausible]
        ext = "n/a" if live.mean_route_extension is None else f"{live.mean_route_extension * 100:.2f}%"
        lines += [
            f"- LIVE runs: {live.runs}, scored lateral samples: {live.scored_samples:,}",
            f"- Mean lateral inefficiency: {ext} vs US benchmark {US_ROUTE_EXTENSION_BENCHMARK * 100:.2f}% [FAA17]: {verdict}",
            f"- Aircraft type known: {live.type_coverage * 100:.0f}% of observations" if live.type_coverage is not None else "- Aircraft type coverage: n/a",
            f"- Route known: {live.route_coverage * 100:.0f}% of observations" if live.route_coverage is not None else "- Route coverage: n/a",
            "",
            "The engine measures inefficiency at each moment against a wind-aware great circle, while the benchmark "
            "measures flown distance against the great circle for whole flights, so agreement within a few points is "
            "the expectation, not an exact match.",
        ]
    lines += ["", "## Sources", "",
              "- [AC137] Aircraft Commerce No. 137 (2021): single-aisle fuel burn analysis",
              "- [AC121] Aircraft Commerce No. 121 (2018/19): A350-900/-1000 fuel burn & operating performance",
              "- [FAA17] FAA/EUROCONTROL U.S./Europe ATM operational performance comparison (2017)"]
    return "\n".join(lines) + "\n"


def run_validation(db_path: Path, report_path: Path) -> tuple[bool, str]:
    """Run every check, write the Markdown report, and return (fuel_checks_passed, report)."""
    fuel = check_fuel_model()
    report = render_report(fuel, check_live_data(db_path))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return all(c.passed for c in fuel), report
