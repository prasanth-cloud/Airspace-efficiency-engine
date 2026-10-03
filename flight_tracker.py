"""
Global Predictive Airspace Efficiency Engine - Phase 1
Live Telemetry Aggregation & Visual Sandbox (East Coast USA)
============================================================

Fetches live aircraft inside the East Coast USA geofence (lat 24.0 to 48.0 N,
lon -85.0 to -65.0) from the OpenSky Network REST API, keeps airborne
commercial flights, and renders them on a dark satellite Folium map saved as
``index.html`` with plane icons pointing along each aircraft's true track.

If the API is rate limited (HTTP 429), unreachable, or returns bad data, the
script falls back to simulated hub-to-hub East Coast traffic so a map is
always produced.

Usage (Windows / VS Code terminal):
    py -m pip install -r requirements.txt
    py flight_tracker.py
    py flight_tracker.py --mock          # force simulated traffic
    py flight_tracker.py --all           # include non-airline callsigns too

Optional: OpenSky issues API clients (OAuth2 client credentials) that get
higher rate limits than anonymous access. Create one on your OpenSky account
page and set these environment variables before running:
    set OPENSKY_CLIENT_ID=your-client-id
    set OPENSKY_CLIENT_SECRET=your-client-secret
"""

from __future__ import annotations

import argparse
import html
import logging
import math
import os
import random
import re
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import folium
import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)

# East Coast USA geofence: Miami - Atlanta - Washington - New York - Boston
BOUNDING_BOX = {
    "lamin": 24.0,   # south
    "lomin": -85.0,  # west
    "lamax": 48.0,   # north
    "lomax": -65.0,  # east
}
REGION_NAME = "East Coast USA"

OUTPUT_FILE = Path(__file__).resolve().parent / "index.html"
REQUEST_TIMEOUT_S = 20
MAX_RETRIES = 3
BACKOFF_BASE_S = 2

METERS_TO_FEET = 3.28084
MS_TO_KNOTS = 1.943844
EARTH_RADIUS_KM = 6371.0

# ICAO airline callsign: 3-letter operator code + flight number (e.g. DAL1234, JBU45A)
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}\d[A-Z0-9]{0,4}$")

# Indices into an OpenSky state vector
IDX_ICAO24, IDX_CALLSIGN, IDX_COUNTRY = 0, 1, 2
IDX_LON, IDX_LAT, IDX_BARO_ALT, IDX_ON_GROUND = 5, 6, 7, 8
IDX_VELOCITY, IDX_TRACK, IDX_VERT_RATE = 9, 10, 11

log = logging.getLogger("flight_tracker")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Flight:
    icao24: str
    callsign: str
    origin_country: str
    latitude: float
    longitude: float
    baro_altitude_m: Optional[float]
    velocity_ms: Optional[float]
    true_track_deg: Optional[float]
    vertical_rate_ms: Optional[float]
    route: Optional[str] = None  # only known for simulated traffic in Phase 1

    @property
    def altitude_ft(self) -> Optional[int]:
        if self.baro_altitude_m is None:
            return None
        return round(self.baro_altitude_m * METERS_TO_FEET)

    @property
    def speed_kt(self) -> Optional[int]:
        if self.velocity_ms is None:
            return None
        return round(self.velocity_ms * MS_TO_KNOTS)


class RateLimitError(Exception):
    """Raised when OpenSky responds with HTTP 429."""


def in_bbox(lat: float, lon: float, bbox: dict) -> bool:
    return bbox["lamin"] <= lat <= bbox["lamax"] and bbox["lomin"] <= lon <= bbox["lomax"]


# --------------------------------------------------------------------------- #
# OpenSky API
# --------------------------------------------------------------------------- #

def get_access_token(session: requests.Session) -> Optional[str]:
    """Return an OAuth2 bearer token if API client credentials are configured."""
    client_id = os.environ.get("OPENSKY_CLIENT_ID")
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.info("No OpenSky credentials set; using anonymous access.")
        return None

    try:
        resp = session.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if token:
            log.info("Authenticated with OpenSky API client credentials.")
        return token
    except (requests.RequestException, ValueError) as exc:
        log.warning("OpenSky authentication failed (%s); continuing anonymously.", exc)
        return None


def fetch_states(bbox: dict) -> list[list]:
    """Fetch raw state vectors for the bounding box, retrying transient errors.

    Raises RateLimitError on HTTP 429 and requests.RequestException / ValueError
    on any other unrecoverable failure.
    """
    with requests.Session() as session:
        session.headers.update({"User-Agent": "AirspaceEfficiencyEngine/1.0"})
        token = get_access_token(session)
        if token:
            session.headers["Authorization"] = f"Bearer {token}"

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info("Requesting OpenSky states (attempt %d/%d)...", attempt, MAX_RETRIES)
                resp = session.get(OPENSKY_STATES_URL, params=bbox, timeout=REQUEST_TIMEOUT_S)

                if resp.status_code == 429:
                    retry_after = resp.headers.get("X-Rate-Limit-Retry-After-Seconds")
                    raise RateLimitError(
                        "OpenSky rate limit hit (HTTP 429)"
                        + (f"; retry after {retry_after}s" if retry_after else "")
                    )

                # Retry server-side errors, fail fast on other client errors
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"Server error HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()

                payload = resp.json()
                if not isinstance(payload, dict):
                    raise ValueError("Unexpected response format from OpenSky")
                return payload.get("states") or []

            except RateLimitError:
                raise  # retrying immediately would only burn more quota
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
            except requests.HTTPError as exc:
                last_error = exc
                if exc.response is not None and exc.response.status_code < 500:
                    raise

            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_S ** attempt
                log.warning("Request failed (%s); retrying in %ds.", last_error, wait)
                time.sleep(wait)

        assert last_error is not None
        raise last_error


def parse_states(states: list[list], bbox: dict, airlines_only: bool = True) -> list[Flight]:
    """Convert raw state vectors to Flight objects, keeping only airborne
    aircraft strictly inside the geofence."""
    flights: list[Flight] = []
    for row in states:
        try:
            if row[IDX_ON_GROUND]:
                continue
            lat, lon = row[IDX_LAT], row[IDX_LON]
            if lat is None or lon is None or not in_bbox(lat, lon, bbox):
                continue
            callsign = (row[IDX_CALLSIGN] or "").strip().upper()
            if not callsign:
                continue
            if airlines_only and not AIRLINE_CALLSIGN.match(callsign):
                continue

            flights.append(
                Flight(
                    icao24=row[IDX_ICAO24],
                    callsign=callsign,
                    origin_country=row[IDX_COUNTRY] or "Unknown",
                    latitude=float(lat),
                    longitude=float(lon),
                    baro_altitude_m=_to_float(row[IDX_BARO_ALT]),
                    velocity_ms=_to_float(row[IDX_VELOCITY]),
                    true_track_deg=_to_float(row[IDX_TRACK]),
                    vertical_rate_ms=_to_float(row[IDX_VERT_RATE]),
                )
            )
        except (IndexError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed state vector %r: %s", row, exc)
    return flights


def _to_float(value) -> Optional[float]:
    return None if value is None else float(value)


# --------------------------------------------------------------------------- #
# Simulated fallback traffic
# --------------------------------------------------------------------------- #

# Major hubs inside the geofence: (lat, lon)
HUBS = {
    "BOS": (42.3656, -71.0096), "JFK": (40.6413, -73.7781), "LGA": (40.7769, -73.8740),
    "EWR": (40.6895, -74.1745), "PHL": (39.8744, -75.2424), "IAD": (38.9531, -77.4565),
    "DCA": (38.8512, -77.0402), "BWI": (39.1774, -76.6684), "CLT": (35.2144, -80.9473),
    "RDU": (35.8801, -78.7880), "ATL": (33.6407, -84.4277), "MCO": (28.4312, -81.3081),
    "TPA": (27.9755, -82.5332), "FLL": (26.0742, -80.1506), "MIA": (25.7959, -80.2870),
    "PIT": (40.4915, -80.2329),
}

# (ICAO operator code, hubs it mostly serves)
US_OPERATORS = [
    ("AAL", ["CLT", "MIA", "PHL", "DCA", "JFK", "LGA", "BOS"]),
    ("DAL", ["ATL", "JFK", "LGA", "BOS", "MCO", "TPA", "RDU"]),
    ("UAL", ["EWR", "IAD", "BOS", "MCO", "FLL"]),
    ("JBU", ["JFK", "BOS", "FLL", "MCO", "TPA"]),
    ("SWA", ["BWI", "ATL", "MCO", "TPA", "FLL", "PIT"]),
    ("NKS", ["FLL", "MCO", "ATL", "EWR", "LGA"]),
    ("RPA", ["LGA", "DCA", "PHL", "BOS", "IAD"]),
    ("EDV", ["JFK", "LGA", "ATL", "RDU", "BOS"]),
    ("FFT", ["MCO", "PHL", "TPA", "ATL"]),
]


ARRIVAL_BANK_SHARE = 0.25
ARRIVAL_BANK_HUBS = ["JFK", "ATL", "BOS"]


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle initial bearing in degrees from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def great_circle_point(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> tuple[float, float]:
    """Point at fraction f (0..1) along the great circle from point 1 to point 2."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2
    ))
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def generate_mock_flights(bbox: dict, count: int = 250, seed: Optional[int] = None) -> list[Flight]:
    """Simulate airborne hub-to-hub traffic along great-circle tracks.

    Each aircraft is placed part-way along a route between two East Coast hubs,
    pointing along the local great-circle track, with altitude, speed and
    vertical rate following a simple climb / cruise / descent profile.
    """
    rng = random.Random(seed)
    flights: list[Flight] = []
    while len(flights) < count:
        code, hubs = rng.choice(US_OPERATORS)
        origin, dest = rng.sample(hubs, 2)
        frac = rng.uniform(0.04, 0.96)
        if rng.random() < ARRIVAL_BANK_SHARE:
            # Arrival bank: a wave of traffic converging on one of the big hubs
            dest = rng.choice(ARRIVAL_BANK_HUBS)
            origin = rng.choice([h for h in HUBS if h != dest])
            frac = rng.uniform(0.45, 0.93)
        (lat1, lon1), (lat2, lon2) = HUBS[origin], HUBS[dest]

        lat, lon = great_circle_point(lat1, lon1, lat2, lon2, frac)
        if not in_bbox(lat, lon, bbox):
            continue
        # Track at the aircraft's current position, towards the destination.
        # Most flights hold the great circle within a few degrees; about a
        # quarter are on airway doglegs or ATC vectors well off the direct line.
        deviation = rng.gauss(0, 2)
        if rng.random() < 0.25:
            deviation = rng.choice([-1, 1]) * rng.uniform(8, 35)
        track = (initial_bearing(lat, lon, lat2, lon2) + deviation) % 360

        if frac < 0.15:  # climb
            alt_m = 900 + (frac / 0.15) * 9000 + rng.uniform(-300, 300)
            speed_ms = rng.uniform(130, 210)
            vrate = rng.uniform(6, 14)
        elif frac > 0.82:  # descent
            alt_m = 900 + ((1 - frac) / 0.18) * 9000 + rng.uniform(-300, 300)
            speed_ms = rng.uniform(120, 200)
            vrate = rng.uniform(-14, -5)
        else:  # cruise, flight levels in 1,000 ft steps
            alt_m = rng.choice(range(30000, 40001, 1000)) / METERS_TO_FEET
            speed_ms = rng.uniform(215, 255)
            vrate = rng.uniform(-0.5, 0.5)

        flights.append(
            Flight(
                icao24=f"{rng.randrange(0xA00000, 0xADFFFF):06x}",  # US ICAO24 block
                callsign=f"{code}{rng.randint(1, 2999)}",
                origin_country="United States",
                latitude=lat,
                longitude=lon,
                baro_altitude_m=max(alt_m, 300.0),
                velocity_ms=speed_ms,
                true_track_deg=track,
                vertical_rate_ms=vrate,
                route=f"{origin} to {dest}",
            )
        )
    return flights


# --------------------------------------------------------------------------- #
# Map rendering
# --------------------------------------------------------------------------- #

ALTITUDE_BANDS = [  # (upper bound ft, colour, label)
    (10000, "#ff4d6d", "Below 10,000 ft"),
    (20000, "#ffb703", "10,000 to 20,000 ft"),
    (30000, "#4cc9f0", "20,000 to 30,000 ft"),
    (float("inf"), "#80ffdb", "Above 30,000 ft"),
]
UNKNOWN_ALT_COLOUR = "#bbbbbb"

ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTR = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"
CARTO_DARK = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
CARTO_LABELS = "https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png"
CARTO_ATTR = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors '
    '&copy; <a href="https://carto.com/attributions">CARTO</a>'
)

# Plane silhouette pointing north (0 deg); rotated by true track.
PLANE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="22" height="22" '
    'style="transform: rotate({rotation}deg); filter: drop-shadow(0 0 2px rgba(0,0,0,0.9));">'
    '<path fill="{colour}" stroke="#0b0f14" stroke-width="0.7" '
    'd="M12 2c.8 0 1.4.7 1.4 1.6v5.6l7.6 4.5v2l-7.6-2.4v4.8l2.2 1.7v1.6L12 '
    '20.4l-3.6 1v-1.6l2.2-1.7v-4.8L3 15.7v-2l7.6-4.5V3.6C10.6 2.7 11.2 2 12 2z"/>'
    "</svg>"
)

# Darkens the satellite imagery so markers stand out ("dark satellite" style)
DARK_SATELLITE_CSS = """
<style>
  .dark-satellite { filter: brightness(0.55) contrast(1.1) saturate(0.7); }
  .leaflet-container { background: #0b0f14 !important; }
</style>
"""


def altitude_colour(alt_ft: Optional[int]) -> str:
    if alt_ft is None:
        return UNKNOWN_ALT_COLOUR
    for upper, colour, _ in ALTITUDE_BANDS:
        if alt_ft < upper:
            return colour
    return UNKNOWN_ALT_COLOUR


def popup_html(f: Flight) -> str:
    def fmt(value, unit):
        return "n/a" if value is None else f"{value:,} {unit}"

    if f.vertical_rate_ms is None:
        trend = "n/a"
    elif f.vertical_rate_ms > 1:
        trend = f"Climbing {round(f.vertical_rate_ms * METERS_TO_FEET * 60):,} ft/min"
    elif f.vertical_rate_ms < -1:
        trend = f"Descending {round(abs(f.vertical_rate_ms) * METERS_TO_FEET * 60):,} ft/min"
    else:
        trend = "Level"

    track = "n/a" if f.true_track_deg is None else f"{round(f.true_track_deg) % 360:03d}&deg;"
    alt_m = "n/a" if f.baro_altitude_m is None else f"{round(f.baro_altitude_m):,} m"
    vel_ms = "n/a" if f.velocity_ms is None else f"{f.velocity_ms:.0f} m/s"
    route_row = (
        f'<tr><td style="color:#666;padding-right:10px;">Route</td><td>{html.escape(f.route)} (simulated)</td></tr>'
        if f.route else ""
    )
    return f"""
    <div style="font-family: Segoe UI, Arial, sans-serif; font-size: 13px; min-width: 210px;">
      <div style="font-size: 16px; font-weight: 700; margin-bottom: 4px;">{html.escape(f.callsign)}</div>
      <table style="border-collapse: collapse;">
        {route_row}
        <tr><td style="color:#666;padding-right:10px;">Baro altitude</td><td>{fmt(f.altitude_ft, "ft")} ({alt_m})</td></tr>
        <tr><td style="color:#666;padding-right:10px;">Velocity</td><td>{fmt(f.speed_kt, "kt")} ({vel_ms})</td></tr>
        <tr><td style="color:#666;padding-right:10px;">True track</td><td>{track}</td></tr>
        <tr><td style="color:#666;padding-right:10px;">Vertical</td><td>{trend}</td></tr>
        <tr><td style="color:#666;padding-right:10px;">Position</td><td>{f.latitude:.4f}, {f.longitude:.4f}</td></tr>
        <tr><td style="color:#666;padding-right:10px;">Country</td><td>{html.escape(f.origin_country)}</td></tr>
        <tr><td style="color:#666;padding-right:10px;">ICAO24</td><td>{html.escape(f.icao24)}</td></tr>
      </table>
    </div>
    """


def build_map(flights: list[Flight], bbox: dict, source: str, output: Path) -> Path:
    centre = [(bbox["lamin"] + bbox["lamax"]) / 2, (bbox["lomin"] + bbox["lomax"]) / 2]
    fmap = folium.Map(location=centre, zoom_start=5, tiles=None, control_scale=True, prefer_canvas=True)
    folium.TileLayer(
        tiles=ESRI_IMAGERY, attr=ESRI_ATTR, name="Dark satellite", className="dark-satellite",
    ).add_to(fmap)
    folium.TileLayer(tiles=CARTO_DARK, attr=CARTO_ATTR, name="Dark map", show=False).add_to(fmap)
    folium.TileLayer(
        tiles=CARTO_LABELS, attr=CARTO_ATTR, name="Place labels", overlay=True, control=True,
    ).add_to(fmap)
    fmap.get_root().header.add_child(folium.Element(DARK_SATELLITE_CSS))

    folium.Rectangle(
        bounds=[[bbox["lamin"], bbox["lomin"]], [bbox["lamax"], bbox["lomax"]]],
        color="#4cc9f0", weight=1.5, dash_array="6 6", fill=False,
        tooltip=f"{REGION_NAME} geofence",
    ).add_to(fmap)

    hub_layer = folium.FeatureGroup(name="Major hubs").add_to(fmap)
    for code, (lat, lon) in HUBS.items():
        folium.CircleMarker(
            location=[lat, lon], radius=4, color="#ffffff", weight=1,
            fill=True, fill_color="#ffffff", fill_opacity=0.8, tooltip=code,
        ).add_to(hub_layer)

    layer = folium.FeatureGroup(name=f"Aircraft ({len(flights)})").add_to(fmap)
    for f in flights:
        svg = PLANE_SVG.format(
            rotation=round(f.true_track_deg or 0),
            colour=altitude_colour(f.altitude_ft),
        )
        alt_label = "n/a" if f.altitude_ft is None else f"{f.altitude_ft:,} ft"
        folium.Marker(
            location=[f.latitude, f.longitude],
            icon=folium.DivIcon(html=svg, icon_size=(22, 22), icon_anchor=(11, 11), class_name="plane"),
            tooltip=f"{html.escape(f.callsign)} | {alt_label}",
            popup=folium.Popup(popup_html(f), max_width=300),
        ).add_to(layer)

    folium.LayerControl(collapsed=True).add_to(fmap)
    fmap.fit_bounds([[bbox["lamin"], bbox["lomin"]], [bbox["lamax"], bbox["lomax"]]])

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    badge_colour = "#2a9d8f" if source == "LIVE" else "#e76f51"
    legend_rows = "".join(
        f'<div><span style="display:inline-block;width:12px;height:12px;background:{c};'
        f'border-radius:2px;margin-right:6px;vertical-align:middle;"></span>{label}</div>'
        for _, c, label in ALTITUDE_BANDS
    )
    panel = ("position: fixed; z-index: 9999; background: rgba(11,15,20,0.88); color: #e8eef2; "
             "border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; "
             "box-shadow: 0 2px 10px rgba(0,0,0,0.5); font-family: Segoe UI, Arial, sans-serif;")
    overlay = f"""
    <div style="{panel} top: 12px; left: 56px; padding: 10px 14px; font-size: 13px;">
      <div style="font-size: 15px; font-weight: 700;">Airspace Efficiency Engine &middot; Phase 1</div>
      <div>{REGION_NAME} &middot; {len(flights)} airborne aircraft</div>
      <div style="margin-top: 4px;">
        <span style="background:{badge_colour};color:#fff;padding:1px 8px;border-radius:10px;font-weight:600;">
          {"LIVE" if source == "LIVE" else "SIMULATED"} DATA</span>
        <span style="color:#9aa7b0;margin-left:6px;">{timestamp}</span>
      </div>
    </div>
    <div style="{panel} bottom: 28px; left: 12px; padding: 8px 12px; font-size: 12px; line-height: 1.7;">
      <div style="font-weight: 700;">Barometric altitude</div>
      {legend_rows}
      <div style="color:#9aa7b0;">Icons point along true track</div>
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(overlay))

    fmap.save(str(output))
    return output


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def get_flights(bbox: dict, force_mock: bool, airlines_only: bool) -> tuple[list[Flight], str]:
    """Return (flights, source) where source is 'LIVE' or 'MOCK'."""
    if force_mock:
        log.info("Simulated traffic requested.")
        return generate_mock_flights(bbox), "MOCK"

    try:
        states = fetch_states(bbox)
        flights = parse_states(states, bbox, airlines_only=airlines_only)
        log.info("Received %d state vectors, %d airborne commercial flights in the geofence.",
                 len(states), len(flights))
        if not flights:
            log.warning("API returned no usable flights; using simulated traffic instead.")
            return generate_mock_flights(bbox), "MOCK"
        return flights, "LIVE"
    except RateLimitError as exc:
        log.warning("%s. Falling back to simulated traffic.", exc)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log.error("OpenSky returned HTTP %s. Falling back to simulated traffic.", status)
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.error("Could not reach OpenSky (%s). Falling back to simulated traffic.", exc.__class__.__name__)
    except (requests.RequestException, ValueError) as exc:
        log.error("Unexpected API failure (%s). Falling back to simulated traffic.", exc)
    return generate_mock_flights(bbox), "MOCK"


def print_table(flights: list[Flight]) -> None:
    print(f"\n{'CALLSIGN':<10}{'LAT':>10}{'LON':>11}{'ALT (m)':>10}{'VEL (m/s)':>11}{'TRACK':>8}")
    print("-" * 60)
    for f in sorted(flights, key=lambda x: x.callsign):
        alt = "n/a" if f.baro_altitude_m is None else f"{f.baro_altitude_m:,.0f}"
        vel = "n/a" if f.velocity_ms is None else f"{f.velocity_ms:.0f}"
        trk = "n/a" if f.true_track_deg is None else f"{f.true_track_deg:.0f}"
        print(f"{f.callsign:<10}{f.latitude:>10.4f}{f.longitude:>11.4f}{alt:>10}{vel:>11}{trk:>8}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Plot live aircraft over the {REGION_NAME} on an interactive map.")
    parser.add_argument("--mock", action="store_true", help="skip the API and use simulated traffic")
    parser.add_argument("--all", action="store_true", help="include non-airline callsigns (GA, military, etc.)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the map automatically")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE, help="output HTML path (default: index.html)")
    args = parser.parse_args()

    # Windows consoles may default to a legacy code page; avoid UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")

    flights, source = get_flights(BOUNDING_BOX, force_mock=args.mock, airlines_only=not args.all)
    print_table(flights)

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        path = build_map(flights, BOUNDING_BOX, source, args.output.resolve())
    except OSError as exc:
        log.error("Could not write map file: %s", exc)
        return 1

    log.info("Map saved to %s (%s data, %d aircraft).", path, source, len(flights))
    if not args.no_browser:
        webbrowser.open(path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
