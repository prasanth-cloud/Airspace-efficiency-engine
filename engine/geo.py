"""Geodesy and International Standard Atmosphere helpers (SI units)."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0
METERS_TO_FEET = 3.28084
MS_TO_KNOTS = 1.943844
M_TO_NM = 1 / 1852.0

# ISA constants
P0_HPA = 1013.25
T0_K = 288.15
LAPSE_K_PER_M = 0.0065
TROPOPAUSE_M = 11_000.0
P_TROPOPAUSE_HPA = 226.32
T_TROPOPAUSE_K = 216.65
GAMMA_R = 1.4 * 287.053


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle initial bearing in degrees true from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def great_circle_point(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> tuple[float, float]:
    """Point at fraction f (0..1) along the great circle from point 1 to point 2."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = haversine_m(lat1, lon1, lat2, lon2) / EARTH_RADIUS_M
    if d < 1e-12:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def great_circle_path(lat1: float, lon1: float, lat2: float, lon2: float, step_m: float = 50_000) -> list[tuple[float, float]]:
    """Points along the great circle, roughly every ``step_m`` metres, endpoints included."""
    n = max(1, int(haversine_m(lat1, lon1, lat2, lon2) // step_m))
    return [great_circle_point(lat1, lon1, lat2, lon2, i / n) for i in range(n + 1)]


def cross_track_m(lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Signed distance of a point from the great circle through points 1 and 2 (right of track positive)."""
    d13 = haversine_m(lat1, lon1, lat, lon) / EARTH_RADIUS_M
    t13 = math.radians(initial_bearing(lat1, lon1, lat, lon))
    t12 = math.radians(initial_bearing(lat1, lon1, lat2, lon2))
    return math.asin(math.sin(d13) * math.sin(t13 - t12)) * EARTH_RADIUS_M


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference a - b in degrees, in (-180, 180]."""
    d = (a - b + 180) % 360 - 180
    return 180.0 if d == -180 else d


# --------------------------------------------------------------------------- #
# International Standard Atmosphere (pressure altitude == barometric altitude)
# --------------------------------------------------------------------------- #

def pressure_to_altitude_m(p_hpa: float) -> float:
    if p_hpa >= P_TROPOPAUSE_HPA:
        return (T0_K / LAPSE_K_PER_M) * (1 - (p_hpa / P0_HPA) ** 0.190263)
    return TROPOPAUSE_M + 6341.62 * math.log(P_TROPOPAUSE_HPA / p_hpa)


def isa_temperature_k(alt_m: float) -> float:
    return T0_K - LAPSE_K_PER_M * min(alt_m, TROPOPAUSE_M)


def speed_of_sound_ms(alt_m: float) -> float:
    return math.sqrt(GAMMA_R * isa_temperature_k(alt_m))


def wind_to_uv(speed_ms: float, direction_from_deg: float) -> tuple[float, float]:
    """Meteorological wind (speed, direction it blows FROM) to u (east) and v (north)."""
    r = math.radians(direction_from_deg)
    return -speed_ms * math.sin(r), -speed_ms * math.cos(r)


def format_icao_latlon(lat: float, lon: float) -> str:
    """ICAO flight-plan coordinate, e.g. 4038N07347W (degrees and minutes)."""
    def dm(value: float, width: int) -> str:
        value = abs(value)
        deg = int(value)
        minutes = int(round((value - deg) * 60))
        if minutes == 60:
            deg, minutes = deg + 1, 0
        return f"{deg:0{width}d}{minutes:02d}"
    return f"{dm(lat, 2)}{'N' if lat >= 0 else 'S'}{dm(lon, 3)}{'E' if lon >= 0 else 'W'}"
