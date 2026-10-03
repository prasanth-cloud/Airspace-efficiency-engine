"""Phase 2: Atmospheric fluid layer.

Builds a 3D wind field (u = eastward, v = northward, m/s) over the geofence
from NOAA GFS pressure-level forecasts, served as JSON by the free Open-Meteo
API (no key, no GRIB decoding needed on Windows). The field is interpolated
bilinearly in latitude/longitude and linearly in pressure altitude, which is
exactly what an aircraft's barometric altitude measures.

If the weather API cannot be reached, a clearly labelled SIMULATED field with
a realistic mid-latitude jet stream is used instead.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from .geo import pressure_to_altitude_m, wind_to_uv

log = logging.getLogger(__name__)

OPEN_METEO_GFS_URL = "https://api.open-meteo.com/v1/gfs"
PRESSURE_LEVELS_HPA = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150]
GRID_STEP_DEG = 2.0
CACHE_MAX_AGE_S = 3 * 3600  # GFS runs every 6 h; refresh the cached forecast every 3 h
REQUEST_TIMEOUT_S = 30


@dataclass
class WindField:
    lats: list[float]                # ascending
    lons: list[float]                # ascending
    alts_m: list[float]              # ascending pressure altitudes of the levels
    u: list[list[list[float]]]       # [level][lat][lon]
    v: list[list[list[float]]]
    source: str                      # "GFS" or "SIMULATED"
    valid_time: str = ""
    meta: dict = field(default_factory=dict)

    def uv(self, lat: float, lon: float, alt_m: float) -> tuple[float, float]:
        """Wind (u, v) in m/s at a 3D point, clamped to the grid edges."""
        i, fy = _locate(self.lats, lat)
        j, fx = _locate(self.lons, lon)
        k, fz = _locate(self.alts_m, alt_m)

        def bilinear(grid: list[list[float]]) -> float:
            g00, g01 = grid[i][j], grid[i][j + 1]
            g10, g11 = grid[i + 1][j], grid[i + 1][j + 1]
            return (g00 * (1 - fx) + g01 * fx) * (1 - fy) + (g10 * (1 - fx) + g11 * fx) * fy

        u = bilinear(self.u[k]) * (1 - fz) + bilinear(self.u[k + 1]) * fz
        v = bilinear(self.v[k]) * (1 - fz) + bilinear(self.v[k + 1]) * fz
        return u, v

    def speed_dir(self, lat: float, lon: float, alt_m: float) -> tuple[float, float]:
        """Wind speed (m/s) and the direction it blows FROM (degrees true)."""
        u, v = self.uv(lat, lon, alt_m)
        return math.hypot(u, v), (math.degrees(math.atan2(-u, -v)) + 360) % 360


def _locate(axis: list[float], x: float) -> tuple[int, float]:
    """Index of the lower grid node and the fractional offset, clamped to the axis."""
    if x <= axis[0]:
        return 0, 0.0
    if x >= axis[-1]:
        return len(axis) - 2, 1.0
    lo, hi = 0, len(axis) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if axis[mid] <= x:
            lo = mid
        else:
            hi = mid
    return lo, (x - axis[lo]) / (axis[lo + 1] - axis[lo])


def _grid_axes(bbox: dict, step: float) -> tuple[list[float], list[float]]:
    def axis(a: float, b: float) -> list[float]:
        n = int(round((b - a) / step))
        return [round(a + i * (b - a) / n, 4) for i in range(n + 1)]
    return axis(bbox["lamin"], bbox["lamax"]), axis(bbox["lomin"], bbox["lomax"])


# --------------------------------------------------------------------------- #
# GFS via Open-Meteo
# --------------------------------------------------------------------------- #

def _fetch_gfs_forecast(lats: list[float], lons: list[float]) -> dict:
    """Download the hourly GFS pressure-level forecast for every grid node."""
    points = [(la, lo) for la in lats for lo in lons]
    variables = [f"{name}_{p}hPa" for p in PRESSURE_LEVELS_HPA for name in ("wind_speed", "wind_direction")]
    params = {
        "latitude": ",".join(f"{la:.2f}" for la, _ in points),
        "longitude": ",".join(f"{lo:.2f}" for _, lo in points),
        "hourly": ",".join(variables),
        "wind_speed_unit": "ms",
        "timeformat": "unixtime",
        "forecast_days": 2,
        "timezone": "GMT",
    }
    resp = requests.get(OPEN_METEO_GFS_URL, params=params, timeout=REQUEST_TIMEOUT_S,
                        headers={"User-Agent": "AirspaceEfficiencyEngine/1.0"})
    if resp.status_code == 429:
        raise requests.HTTPError("Open-Meteo rate limit (HTTP 429)", response=resp)
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):  # a single location comes back as an object
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(points):
        raise ValueError(f"Expected {len(points)} grid points from Open-Meteo, got {len(payload)}")
    return {"fetched_at": time.time(), "lats": lats, "lons": lons, "points": payload}


def _field_from_forecast(forecast: dict, now: float) -> WindField:
    lats, lons, points = forecast["lats"], forecast["lons"], forecast["points"]
    times = points[0]["hourly"]["time"]
    t_idx = min(range(len(times)), key=lambda n: abs(times[n] - now))
    if abs(times[t_idx] - now) > 2 * 3600:
        raise ValueError("Cached wind forecast does not cover the current time")

    levels = sorted(PRESSURE_LEVELS_HPA, reverse=True)  # high pressure = low altitude first
    u_grid, v_grid = [], []
    for p in levels:
        u_lvl, v_lvl = [], []
        for i in range(len(lats)):
            u_row, v_row = [], []
            for j in range(len(lons)):
                hourly = points[i * len(lons) + j]["hourly"]
                spd = hourly[f"wind_speed_{p}hPa"][t_idx]
                drn = hourly[f"wind_direction_{p}hPa"][t_idx]
                if spd is None or drn is None:
                    spd, drn = 0.0, 0.0
                u, v = wind_to_uv(float(spd), float(drn))
                u_row.append(u)
                v_row.append(v)
            u_lvl.append(u_row)
            v_lvl.append(v_row)
        u_grid.append(u_lvl)
        v_grid.append(v_lvl)

    valid = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(times[t_idx]))
    return WindField(lats, lons, [pressure_to_altitude_m(p) for p in levels], u_grid, v_grid,
                     source="GFS", valid_time=valid,
                     meta={"provider": "NOAA GFS via Open-Meteo", "levels_hpa": levels})


# --------------------------------------------------------------------------- #
# Simulated fallback
# --------------------------------------------------------------------------- #

def _short_error(exc: Exception) -> str:
    """Exception text without the very long request URL."""
    text = str(exc)
    if isinstance(exc, requests.ConnectionError):
        return f"{exc.__class__.__name__}: could not connect to the weather API"
    return text if len(text) < 200 else text[:200] + "..."


def simulated_field(bbox: dict) -> WindField:
    """Analytic westerly flow with a jet stream core near 38N at about FL340."""
    lats, lons = _grid_axes(bbox, GRID_STEP_DEG)
    levels = sorted(PRESSURE_LEVELS_HPA, reverse=True)
    alts = [pressure_to_altitude_m(p) for p in levels]
    u_grid, v_grid = [], []
    for alt in alts:
        vertical = math.exp(-((alt - 10_400) / 2_200) ** 2)
        u_lvl, v_lvl = [], []
        for la in lats:
            u_row, v_row = [], []
            for lo in lons:
                core = 38 + 3 * math.sin(math.radians((lo + 85) * 9))  # meandering jet axis
                jet = 55 * math.exp(-((la - core) / 6) ** 2) * vertical
                u_row.append(6 + 0.0012 * alt + jet)
                v_row.append(8 * vertical * math.cos(math.radians((lo + 85) * 9)))
            u_lvl.append(u_row)
            v_lvl.append(v_row)
        u_grid.append(u_lvl)
        v_grid.append(v_lvl)
    return WindField(lats, lons, alts, u_grid, v_grid, source="SIMULATED",
                     valid_time=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                     meta={"provider": "Analytic jet-stream model"})


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def load_wind_field(bbox: dict, cache_dir: Path, offline: bool = False) -> WindField:
    """Return the current wind field, using a cached GFS forecast when fresh."""
    if offline:
        log.info("Offline mode: using simulated wind field.")
        return simulated_field(bbox)

    cache_file = cache_dir / "gfs_wind_cache.json"
    lats, lons = _grid_axes(bbox, GRID_STEP_DEG)
    now = time.time()

    cached: Optional[dict] = None
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("lats") != lats or cached.get("lons") != lons:
                cached = None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable wind cache (%s).", exc)
        cached = None

    if cached and now - cached.get("fetched_at", 0) < CACHE_MAX_AGE_S:
        try:
            wf = _field_from_forecast(cached, now)
            log.info("Using cached GFS winds valid %s.", wf.valid_time)
            return wf
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("Cached wind forecast unusable (%s); refreshing.", exc)

    try:
        log.info("Downloading GFS winds for %d grid points x %d levels...",
                 len(lats) * len(lons), len(PRESSURE_LEVELS_HPA))
        forecast = _fetch_gfs_forecast(lats, lons)
        wf = _field_from_forecast(forecast, now)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(forecast), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write wind cache (%s).", exc)
        log.info("GFS winds loaded, valid %s.", wf.valid_time)
        return wf
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("GFS wind download failed (%s).", _short_error(exc))

    if cached:
        try:
            wf = _field_from_forecast(cached, now)
            log.warning("Using stale cached GFS winds valid %s.", wf.valid_time)
            return wf
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    log.warning("Falling back to the simulated wind field.")
    return simulated_field(bbox)
