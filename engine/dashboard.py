"""Phase 4: Public accountability scoreboard (self-contained ``scoreboard.html``).

Airlines are ranked by mean carbon efficiency across every scored LIVE
observation in the last 7 days. If no live data has been collected yet, a
preview built from simulated runs is shown under a prominent SIMULATED banner.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

CSS = """
:root { --bg:#0b0f14; --panel:#131a22; --line:#243040; --text:#e8eef2; --muted:#8a99a6;
        --green:#2ec4b6; --amber:#ffb703; --red:#ff4d6d; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:Segoe UI, Arial, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:17px; margin:32px 0 10px; }
.sub { color:var(--muted); font-size:13px; }
.banner { background:#e76f51; color:#fff; padding:10px 14px; border-radius:8px; margin:16px 0; font-weight:600; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:20px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
.card .v { font-size:24px; font-weight:700; margin-top:4px; } .card .k { color:var(--muted); font-size:12px; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; background:var(--panel); border-radius:10px; overflow:hidden; }
th, td { padding:9px 10px; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; background:#0f151c; } td:nth-child(2), th:nth-child(2) { text-align:left; }
tr:last-child td { border-bottom:none; }
.bar { display:inline-block; height:8px; border-radius:4px; vertical-align:middle; margin-right:6px; }
.pill { padding:1px 8px; border-radius:10px; font-weight:600; font-size:12px; color:#0b0f14; }
.foot { color:var(--muted); font-size:12px; margin-top:28px; line-height:1.6; }
"""


def _colour(eff: float) -> str:
    return "var(--green)" if eff >= 0.97 else "var(--amber)" if eff >= 0.90 else "var(--red)"


def build_scoreboard(store: Store, output: Path, days: int = 7) -> Path:
    live_rows = store.scoreboard(days=days)
    simulated = not live_rows
    rows = live_rows or store.scoreboard(days=days, include_simulated=True, min_samples=1)
    latest = store.latest_run(live_only=not simulated)
    hubs = store.hub_status(latest["id"]) if latest else []

    total_samples = sum(r["samples"] for r in rows)
    fleet_eff = (sum(r["mean_efficiency"] * r["samples"] for r in rows) / total_samples) if total_samples else None

    table_rows = []
    for rank, r in enumerate(rows, 1):
        eff = r["mean_efficiency"]
        table_rows.append(f"""
        <tr><td>{rank}</td>
          <td><strong>{html.escape(r['airline'] or r['code'])}</strong> <span class="sub">{html.escape(r['code'])}</span></td>
          <td><span class="bar" style="width:{max(4, (eff - 0.8) * 400):.0f}px;background:{_colour(eff)}"></span>{eff * 100:.1f}%</td>
          <td>{(r['mean_lateral'] or 1) * 100:.1f}%</td><td>{(r['mean_vertical'] or 1) * 100:.1f}%</td>
          <td>{r['mean_waste_kg_min']:.2f}</td><td>{r['wasteful_share'] * 100:.0f}%</td>
          <td>{r['flights']}</td><td>{r['samples']}</td></tr>""")

    hub_rows = []
    for h in hubs:
        state = ('<span class="pill" style="background:var(--red)">Over capacity</span>' if h["overloaded"]
                 else '<span class="pill" style="background:var(--green)">Normal</span>')
        hub_rows.append(f"""
        <tr><td>{html.escape(h['hub'])}</td><td>{state}</td><td>{h['arrival_rate']}/h</td><td>{h['inbound']}</td>
          <td>{max(map(int, h['demand_bins'].split(','))) if h['demand_bins'] else 0} / {h['capacity_per_bin']}</td>
          <td>{h['total_delay_min']:.0f} min</td><td>{h['total_co2_saved_kg']:,.0f} kg</td></tr>""")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    banner = ('<div class="banner">SIMULATED PREVIEW: no live OpenSky runs recorded yet. '
              'These rankings use simulated traffic and say nothing about real airlines.</div>') if simulated else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Airline Carbon Scoreboard</title><style>{CSS}</style></head>
<body><main>
  <h1>Airline Carbon Efficiency Scoreboard</h1>
  <div class="sub">East Coast USA airspace &middot; last {days} days &middot; updated {now}</div>
  {banner}
  <div class="cards">
    <div class="card"><div class="k">Airlines ranked</div><div class="v">{len(rows)}</div></div>
    <div class="card"><div class="k">Scored observations</div><div class="v">{total_samples:,}</div></div>
    <div class="card"><div class="k">Fleet mean efficiency</div><div class="v">{'n/a' if fleet_eff is None else f'{fleet_eff * 100:.1f}%'}</div></div>
    <div class="card"><div class="k">Excess CO2, latest snapshot</div><div class="v">{(latest or {}).get('total_waste_co2_kg_min', 0):,.0f} kg/min</div></div>
  </div>

  <h2>Airline ranking</h2>
  <div class="table-wrap"><table>
    <tr><th>#</th><th>Airline</th><th>Efficiency</th><th>Lateral</th><th>Flight level</th>
        <th>Excess CO2 kg/min</th><th>Wasteful share</th><th>Flights</th><th>Samples</th></tr>
    {''.join(table_rows) or '<tr><td colspan="9" style="text-align:center">No scored flights yet.</td></tr>'}
  </table></div>

  <h2>Hub arrival pressure (latest snapshot)</h2>
  <div class="table-wrap"><table>
    <tr><th>Hub</th><th>State</th><th>Arrival rate</th><th>Inbound 3 h</th><th>Peak 15 min vs capacity</th>
        <th>Delay to absorb</th><th>CO2 saved by speed control</th></tr>
    {''.join(hub_rows) or '<tr><td colspan="7" style="text-align:center">No hub data yet.</td></tr>'}
  </table></div>

  <div class="foot">
    Efficiency is the share of fuel burn that turns into progress towards the destination, compared with
    flying the great circle through the same winds (lateral) at the best flight level (vertical). Winds come
    from NOAA GFS. Fuel flow comes from each aircraft's type; aircraft whose type cannot be found use a
    single-aisle reference. Flights below 10,000 ft or within 60 km of an airport
    are not scored. Airlines need at least 5 scored observations to be ranked.
  </div>
</main></body></html>"""
    output.write_text(page, encoding="utf-8")
    return output
