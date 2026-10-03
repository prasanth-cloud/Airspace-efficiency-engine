"""Phase 3: Great-circle carbon inefficiency math.

For every aircraft the engine compares what it is doing with what physics
allows at the same moment:

* **Wind triangle.** Ground velocity (OpenSky track + speed) minus the Phase 2
  wind vector gives the air velocity, so true airspeed (TAS), headwind or
  tailwind and crosswind are known.
* **Lateral efficiency.** The ideal closure speed towards the destination is
  what the aircraft would achieve flying the great-circle course with the same
  TAS through the same wind: sqrt(TAS^2 - crosswind^2) + wind-along-course.
  The actual closure speed is groundspeed x cos(track - course). The ratio is
  the share of fuel being turned into real progress.
* **Vertical efficiency.** For cruising aircraft the same closure maths runs at
  every flight level from FL280 to FL410 at constant Mach, weighted by a fuel
  flow penalty away from the optimum level. The ratio of the best level's
  fuel-per-distance to the current level's is the flight level efficiency.

Carbon Waste Metric (kg CO2 per minute) = CO2 burn rate x (1 - lateral x vertical).

Model assumptions (documented so they can be refined):
* Cruise fuel flow comes from the aircraft type (``engine/aircraft.py``),
  scaled for climb, descent and distance from the optimum level. Aircraft
  whose type cannot be found use a single-aisle reference of 40 kg/min.
* Burning 1 kg of jet fuel releases 3.16 kg of CO2.
* Aircraft below 10,000 ft or within 40 NM of their airports are in
  terminal airspace, where vectoring is normal; they are not scored. 40 NM is
  the terminal boundary the FAA/EUROCONTROL en-route efficiency benchmark uses.
* A looked-up route the aircraft is plainly not flying (see
  ``routes.route_mismatch``) is discarded and marked ``mismatch``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional

from .aircraft import performance_for
from .geo import (METERS_TO_FEET, angle_diff, haversine_m, initial_bearing,
                  speed_of_sound_ms)
from .routes import Route, route_mismatch
from .winds import WindField

# Bump when a change makes earlier stored scores incomparable; the scoreboard
# and validation only use runs scored with the current version.
SCORING_VERSION = 2

CO2_PER_KG_FUEL = 3.16
CRUISE_FUEL_KG_MIN = 40.0
CLIMB_FACTOR = 1.8
DESCENT_FACTOR = 0.35
OPTIMUM_FL = 370
LEVEL_PENALTY_PER_FL10_SQ = 0.002   # +0.2% fuel per (10 FL away from optimum)^2
CANDIDATE_LEVELS = list(range(280, 411, 10))
TERMINAL_ALT_M = 10_000 / METERS_TO_FEET
TERMINAL_RADIUS_M = 40 * 1852
CRUISE_MIN_ALT_M = 25_000 / METERS_TO_FEET
LEVEL_VRATE_MS = 2.5

GREEN_THRESHOLD = 0.97
AMBER_THRESHOLD = 0.90


@dataclass
class FlightMetrics:
    icao24: str
    callsign: str
    airline: str
    lat: float
    lon: float
    alt_m: Optional[float]
    gs_ms: Optional[float]
    track_deg: Optional[float]
    vrate_ms: Optional[float]
    origin: Optional[str]
    destination: Optional[str]
    route_source: str
    aircraft_type: Optional[str] = None
    fuel_basis: str = "default"              # "type" when the aircraft type is known
    cruise_fuel_kg_min: float = CRUISE_FUEL_KG_MIN
    dest_lat: Optional[float] = None
    dest_lon: Optional[float] = None
    orig_lat: Optional[float] = None
    orig_lon: Optional[float] = None
    dist_to_dest_m: Optional[float] = None
    wind_u_ms: Optional[float] = None
    wind_v_ms: Optional[float] = None
    tas_ms: Optional[float] = None
    tailwind_ms: Optional[float] = None      # + tailwind / - headwind along track
    crosswind_ms: Optional[float] = None
    friction_delta_ms: Optional[float] = None  # groundspeed - airspeed
    closure_ms: Optional[float] = None
    ideal_closure_ms: Optional[float] = None
    lateral_eff: Optional[float] = None
    vertical_eff: Optional[float] = None
    best_level_fl: Optional[int] = None
    efficiency: Optional[float] = None
    fuel_kg_min: Optional[float] = None
    co2_kg_min: Optional[float] = None
    waste_co2_kg_min: Optional[float] = None
    phase: str = "unknown"                   # cruise, climb, descent, terminal
    rating: str = "unscored"                 # efficient, moderate, wasteful, unscored
    waste_lateral_kg_min: Optional[float] = None   # excess CO2 from not flying the great circle
    waste_vertical_kg_min: Optional[float] = None  # excess CO2 from not flying the best level
    lateral_cause: Optional[str] = None      # causes.CAUSES; set by causes.attribute
    lateral_cause_detail: Optional[str] = None
    cause: Optional[str] = None              # the cause behind the larger share of the waste
    cause_detail: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def fuel_flow_kg_min(alt_m: Optional[float], vrate_ms: Optional[float],
                     cruise_kg_min: float = CRUISE_FUEL_KG_MIN) -> float:
    ff = cruise_kg_min
    if alt_m is not None:
        fl = alt_m * METERS_TO_FEET / 100
        ff *= level_factor(fl) if fl >= 200 else 1.0 + 0.4 * (1 - fl / 200)
    if vrate_ms is not None:
        if vrate_ms > LEVEL_VRATE_MS:
            ff *= CLIMB_FACTOR
        elif vrate_ms < -LEVEL_VRATE_MS:
            ff *= DESCENT_FACTOR
    return ff


def level_factor(fl: float) -> float:
    return 1.0 + LEVEL_PENALTY_PER_FL10_SQ * ((fl - OPTIMUM_FL) / 10) ** 2


def wind_components(u: float, v: float, heading_deg: float) -> tuple[float, float]:
    """(along, across) wind components for a direction; along > 0 is a tailwind."""
    h = math.radians(heading_deg)
    return u * math.sin(h) + v * math.cos(h), u * math.cos(h) - v * math.sin(h)


def ideal_closure(tas: float, u: float, v: float, course_deg: float) -> Optional[float]:
    along, across = wind_components(u, v, course_deg)
    if tas <= abs(across):
        return None
    return math.sqrt(tas ** 2 - across ** 2) + along


def analyse_flight(f, route: Optional[Route], winds: WindField,
                   aircraft_type: Optional[str] = None) -> FlightMetrics:
    from .airports import airline_name  # local import keeps module load light

    route_source = route.source if route else "unknown"
    if route and route.source != "simulated" and route_mismatch(f.latitude, f.longitude, f.true_track_deg, route):
        route, route_source = None, "mismatch"

    type_code = (aircraft_type or getattr(f, "aircraft_type", None) or "").strip().upper() or None
    perf, known = performance_for(type_code)

    m = FlightMetrics(
        icao24=f.icao24, callsign=f.callsign, airline=airline_name(f.callsign),
        lat=f.latitude, lon=f.longitude, alt_m=f.baro_altitude_m, gs_ms=f.velocity_ms,
        track_deg=f.true_track_deg, vrate_ms=f.vertical_rate_ms,
        origin=route.origin.iata or route.origin.code if route else None,
        destination=route.destination.iata or route.destination.code if route else None,
        route_source=route_source,
        aircraft_type=type_code,
        fuel_basis="type" if known else "default",
        cruise_fuel_kg_min=perf.cruise_fuel_kg_min,
    )
    if route:
        m.dest_lat, m.dest_lon = route.destination.lat, route.destination.lon
        m.orig_lat, m.orig_lon = route.origin.lat, route.origin.lon
        m.dist_to_dest_m = haversine_m(f.latitude, f.longitude, m.dest_lat, m.dest_lon)

    if f.baro_altitude_m is None or f.velocity_ms is None or f.true_track_deg is None:
        return m

    vr = f.vertical_rate_ms or 0.0
    m.phase = "climb" if vr > LEVEL_VRATE_MS else "descent" if vr < -LEVEL_VRATE_MS else "cruise"
    m.fuel_kg_min = fuel_flow_kg_min(f.baro_altitude_m, f.vertical_rate_ms, m.cruise_fuel_kg_min)
    m.co2_kg_min = m.fuel_kg_min * CO2_PER_KG_FUEL

    # Wind triangle at the aircraft's exact 3D position
    u, v = winds.uv(f.latitude, f.longitude, f.baro_altitude_m)
    m.wind_u_ms, m.wind_v_ms = u, v
    trk = math.radians(f.true_track_deg)
    gx, gy = f.velocity_ms * math.sin(trk), f.velocity_ms * math.cos(trk)
    m.tas_ms = math.hypot(gx - u, gy - v)
    m.tailwind_ms, m.crosswind_ms = wind_components(u, v, f.true_track_deg)
    m.friction_delta_ms = f.velocity_ms - m.tas_ms

    near_airport = route is not None and (
        m.dist_to_dest_m < TERMINAL_RADIUS_M
        or haversine_m(f.latitude, f.longitude, m.orig_lat, m.orig_lon) < TERMINAL_RADIUS_M
    )
    if f.baro_altitude_m < TERMINAL_ALT_M or near_airport:
        m.phase = "terminal"
        return m

    # Course used for vertical optimisation: towards destination if known
    course = f.true_track_deg
    if route:
        course = initial_bearing(f.latitude, f.longitude, m.dest_lat, m.dest_lon)
        m.closure_ms = f.velocity_ms * math.cos(math.radians(angle_diff(f.true_track_deg, course)))
        m.ideal_closure_ms = ideal_closure(m.tas_ms, u, v, course)
        if m.ideal_closure_ms and m.ideal_closure_ms > 0:
            m.lateral_eff = max(0.0, min(1.0, m.closure_ms / m.ideal_closure_ms))

    if m.phase == "cruise" and f.baro_altitude_m >= CRUISE_MIN_ALT_M:
        mach = m.tas_ms / speed_of_sound_ms(f.baro_altitude_m)
        current_fl = f.baro_altitude_m * METERS_TO_FEET / 100
        current_c = ideal_closure(m.tas_ms, u, v, course)
        if current_c and current_c > 0:
            current_cost = level_factor(current_fl) / current_c
            best_fl, best_cost = round(current_fl / 10) * 10, current_cost
            for fl in CANDIDATE_LEVELS:
                alt = fl * 100 / METERS_TO_FEET
                lu, lv = winds.uv(f.latitude, f.longitude, alt)
                c = ideal_closure(mach * speed_of_sound_ms(alt), lu, lv, course)
                if c and c > 0 and level_factor(fl) / c < best_cost:
                    best_fl, best_cost = fl, level_factor(fl) / c
            m.best_level_fl = best_fl
            m.vertical_eff = max(0.0, min(1.0, best_cost / current_cost))

    if m.lateral_eff is None and m.vertical_eff is None:
        return m
    m.efficiency = (m.lateral_eff if m.lateral_eff is not None else 1.0) * \
                   (m.vertical_eff if m.vertical_eff is not None else 1.0)
    m.waste_co2_kg_min = m.co2_kg_min * (1 - m.efficiency)
    m.rating = ("efficient" if m.efficiency >= GREEN_THRESHOLD
                else "moderate" if m.efficiency >= AMBER_THRESHOLD else "wasteful")
    return m
