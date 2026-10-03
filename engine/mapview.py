"""Engine map: the Phase 1 dark satellite map extended with winds (Phase 2),
efficiency colouring and ideal great-circle paths (Phase 3) and hub arrival
pressure (Phase 5)."""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from pathlib import Path

import folium

import flight_tracker as ft
from .airports import AIRPORTS
from .efficiency import FlightMetrics
from .geo import METERS_TO_FEET, MS_TO_KNOTS, great_circle_path
from .queueing import HubQueue
from .winds import WindField

RATING_COLOURS = {"efficient": "#2ec4b6", "moderate": "#ffb703", "wasteful": "#ff4d6d", "unscored": "#9aa7b0"}
RATING_LABELS = {"efficient": "Efficient (97% or better)", "moderate": "Moderate (90 to 97%)",
                 "wasteful": "Wasteful (below 90%)", "unscored": "Not scored (terminal area or no route)"}
WIND_DISPLAY_FL = 350


def _fmt(value, spec: str, unit: str = "") -> str:
    return "n/a" if value is None else f"{value:{spec}}{unit}"


def _popup(m: FlightMetrics) -> str:
    def row(k: str, v: str) -> str:
        return f'<tr><td style="color:#666;padding-right:10px;">{k}</td><td>{v}</td></tr>'
    route = f"{m.origin} to {m.destination} ({m.route_source})" if m.destination else "Unknown"
    tail = "n/a" if m.tailwind_ms is None else (
        f"{'Tailwind' if m.tailwind_ms >= 0 else 'Headwind'} {abs(m.tailwind_ms) * MS_TO_KNOTS:.0f} kt")
    rows = [
        row("Airline", html.escape(m.airline)), row("Route", html.escape(route)),
        row("Phase", m.phase),
        row("Altitude", _fmt(m.alt_m and m.alt_m * METERS_TO_FEET, ",.0f", " ft")),
        row("Groundspeed", _fmt(m.gs_ms and m.gs_ms * MS_TO_KNOTS, ".0f", " kt")),
        row("True airspeed", _fmt(m.tas_ms and m.tas_ms * MS_TO_KNOTS, ".0f", " kt")),
        row("Wind effect", tail),
        row("Crosswind", _fmt(m.crosswind_ms and abs(m.crosswind_ms) * MS_TO_KNOTS, ".0f", " kt")),
        row("Lateral efficiency", _fmt(m.lateral_eff and m.lateral_eff * 100, ".1f", "%")),
        row("Flight level efficiency", _fmt(m.vertical_eff and m.vertical_eff * 100, ".1f", "%")),
        row("Best flight level", f"FL{m.best_level_fl}" if m.best_level_fl else "n/a"),
        row("Overall efficiency", f"<b>{_fmt(m.efficiency and m.efficiency * 100, '.1f', '%')}</b>"),
        row("CO2 burn", _fmt(m.co2_kg_min, ".0f", " kg/min")),
        row("Excess CO2", f"<b>{_fmt(m.waste_co2_kg_min, '.1f', ' kg/min')}</b>"),
    ]
    return (f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;min-width:240px;">'
            f'<div style="font-size:16px;font-weight:700;margin-bottom:4px;">{html.escape(m.callsign)}</div>'
            f'<table style="border-collapse:collapse;">{"".join(rows)}</table></div>')


def _hub_popup(q: HubQueue) -> str:
    adv = "".join(
        f"<tr><td>{html.escape(a.callsign)}</td><td>{a.delay_min:.0f}</td><td>{a.absorbed_min:.1f}</td>"
        f"<td>{a.current_kt} to {a.advised_kt}</td><td>{a.co2_saved_kg:.0f}</td></tr>"
        for a in q.advisories[:12])
    table = (f'<table style="border-collapse:collapse;font-size:12px;"><tr><th>Flight</th><th>Delay min</th>'
             f'<th>En route min</th><th>TAS kt</th><th>CO2 saved kg</th></tr>{adv}</table>') if adv else "No speed advisories needed."
    return (f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;min-width:280px;">'
            f'<div style="font-size:15px;font-weight:700;">{q.hub} arrivals</div>'
            f'<div>{q.inbound} inbound in 3 h, capacity {q.arrival_rate}/h, '
            f'peak {max(q.demand_bins) if q.demand_bins else 0} per 15 min vs {q.capacity_per_bin}</div>'
            f'<div style="margin:4px 0;">Delay to absorb {q.total_delay_min:.0f} min, CO2 saved {q.total_co2_saved_kg:,.0f} kg</div>'
            f'{table}</div>')


def build_engine_map(metrics: list[FlightMetrics], winds: WindField, queues: list[HubQueue],
                     bbox: dict, source: str, output: Path) -> Path:
    fmap = folium.Map(location=[(bbox["lamin"] + bbox["lamax"]) / 2, (bbox["lomin"] + bbox["lomax"]) / 2],
                      zoom_start=5, tiles=None, control_scale=True, prefer_canvas=True)
    folium.TileLayer(tiles=ft.ESRI_IMAGERY, attr=ft.ESRI_ATTR, name="Dark satellite", className="dark-satellite").add_to(fmap)
    folium.TileLayer(tiles=ft.CARTO_DARK, attr=ft.CARTO_ATTR, name="Dark map", show=False).add_to(fmap)
    folium.TileLayer(tiles=ft.CARTO_LABELS, attr=ft.CARTO_ATTR, name="Place labels", overlay=True).add_to(fmap)
    fmap.get_root().header.add_child(folium.Element(ft.DARK_SATELLITE_CSS))
    folium.Rectangle(bounds=[[bbox["lamin"], bbox["lomin"]], [bbox["lamax"], bbox["lomax"]]],
                     color="#4cc9f0", weight=1.5, dash_array="6 6", fill=False, tooltip="Geofence").add_to(fmap)

    # Phase 2: wind arrows at a representative cruise level
    wind_layer = folium.FeatureGroup(name=f"Winds FL{WIND_DISPLAY_FL} ({winds.source})", show=False).add_to(fmap)
    alt = WIND_DISPLAY_FL * 100 / METERS_TO_FEET
    for la in winds.lats:
        for lo in winds.lons:
            u, v = winds.uv(la, lo, alt)
            spd = math.hypot(u, v)
            scale = 0.012  # degrees per m/s
            end = [la + v * scale, lo + u * scale / max(math.cos(math.radians(la)), 0.2)]
            colour = "#80ffdb" if spd < 25 else "#ffb703" if spd < 45 else "#ff4d6d"
            folium.PolyLine([[la, lo], end], color=colour, weight=2, opacity=0.8,
                            tooltip=f"{spd * MS_TO_KNOTS:.0f} kt").add_to(wind_layer)
            folium.CircleMarker([la, lo], radius=1.5, color=colour, fill=True, opacity=0.8).add_to(wind_layer)

    # Phase 3: ideal great-circle paths, coloured by efficiency
    path_layer = folium.FeatureGroup(name="Ideal great-circle paths").add_to(fmap)
    for m in metrics:
        if m.dest_lat is None or m.rating == "unscored":
            continue
        path = great_circle_path(m.lat, m.lon, m.dest_lat, m.dest_lon, step_m=40_000)
        folium.PolyLine(path, color=RATING_COLOURS[m.rating], weight=1.6, opacity=0.55, dash_array="4 6",
                        tooltip=f"{m.callsign} ideal path to {m.destination}").add_to(path_layer)

    # Phase 5: hubs, red when over capacity
    hub_layer = folium.FeatureGroup(name="Hub arrival pressure").add_to(fmap)
    queue_by_hub = {q.hub: q for q in queues}
    for code, a in AIRPORTS.items():
        q = queue_by_hub.get(code)
        colour = "#ff4d6d" if q and q.overloaded else "#ffffff"
        marker = folium.CircleMarker([a.lat, a.lon], radius=7 if q else 4, color=colour, weight=2,
                                     fill=True, fill_color=colour, fill_opacity=0.35 if q else 0.8,
                                     tooltip=f"{code}" + (f" | {q.inbound} inbound" if q else ""))
        if q:
            marker.add_child(folium.Popup(_hub_popup(q), max_width=420))
        marker.add_to(hub_layer)

    aircraft = folium.FeatureGroup(name=f"Aircraft ({len(metrics)})").add_to(fmap)
    for m in metrics:
        svg = ft.PLANE_SVG.format(rotation=round(m.track_deg or 0), colour=RATING_COLOURS[m.rating])
        eff = "" if m.efficiency is None else f" | {m.efficiency * 100:.1f}%"
        folium.Marker([m.lat, m.lon],
                      icon=folium.DivIcon(html=svg, icon_size=(22, 22), icon_anchor=(11, 11), class_name="plane"),
                      tooltip=f"{html.escape(m.callsign)}{eff}",
                      popup=folium.Popup(_popup(m), max_width=320)).add_to(aircraft)

    folium.LayerControl(collapsed=True).add_to(fmap)
    fmap.fit_bounds([[bbox["lamin"], bbox["lomin"]], [bbox["lamax"], bbox["lomax"]]])

    scored = [m for m in metrics if m.efficiency is not None]
    total_waste = sum(m.waste_co2_kg_min for m in scored)
    worst = sorted(scored, key=lambda m: m.waste_co2_kg_min, reverse=True)[:5]
    worst_html = "".join(f"<div>{html.escape(m.callsign)} &middot; {m.waste_co2_kg_min:.1f} kg/min</div>" for m in worst)
    overloaded = [q.hub for q in queues if q.overloaded]
    saved = sum(q.total_co2_saved_kg for q in queues)
    legend = "".join(
        f'<div><span style="display:inline-block;width:12px;height:12px;background:{RATING_COLOURS[k]};'
        f'border-radius:2px;margin-right:6px;vertical-align:middle;"></span>{v}</div>' for k, v in RATING_LABELS.items())
    panel = ("position:fixed;z-index:9999;background:rgba(11,15,20,0.9);color:#e8eef2;"
             "border:1px solid rgba(255,255,255,0.12);border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.5);"
             "font-family:Segoe UI,Arial,sans-serif;")
    badge = lambda ok, txt: (f'<span style="background:{"#2a9d8f" if ok else "#e76f51"};color:#fff;padding:1px 8px;'
                             f'border-radius:10px;font-weight:600;font-size:12px;margin-right:4px;">{txt}</span>')
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    overlay = f"""
    <div style="{panel} top:12px; left:56px; padding:10px 14px; font-size:13px; max-width:330px;">
      <div style="font-size:15px;font-weight:700;">Airspace Efficiency Engine</div>
      <div>East Coast USA &middot; {len(metrics)} aircraft &middot; {len(scored)} scored</div>
      <div style="margin:5px 0;">{badge(source == 'LIVE', 'LIVE TRAFFIC' if source == 'LIVE' else 'SIMULATED TRAFFIC')}{badge(winds.source == 'GFS', 'GFS WINDS' if winds.source == 'GFS' else 'SIMULATED WINDS')}</div>
      <div style="font-size:22px;font-weight:700;color:#ff4d6d;">{total_waste:,.0f} kg CO2/min</div>
      <div style="color:#9aa7b0;">excess burn across scored flights</div>
      <div style="margin-top:6px;">Hubs over capacity: <b>{', '.join(overloaded) or 'none'}</b></div>
      <div>Speed control would save <b>{saved:,.0f} kg CO2</b></div>
      <div style="margin-top:6px;font-weight:600;">Highest excess burn</div>{worst_html or '<div>none</div>'}
      <div style="color:#9aa7b0;margin-top:6px;">{ts} &middot; winds valid {html.escape(winds.valid_time)}</div>
    </div>
    <div style="{panel} bottom:28px; right:12px; padding:8px 12px; font-size:12px; line-height:1.7;">
      <div style="font-weight:700;">Carbon efficiency</div>{legend}
      <div style="color:#9aa7b0;">Dashed lines: ideal great-circle path to destination</div>
    </div>"""
    fmap.get_root().html.add_child(folium.Element(overlay))
    fmap.save(str(output))
    return output
