"""
Global Predictive Airspace Efficiency Engine - command line entry point
=======================================================================

    py run_engine.py                 one full cycle: map, scoreboard, advisories
    py run_engine.py --loop          Phase 4 background engine, every 5 minutes
    py run_engine.py --api           Phase 6 dispatch API on http://127.0.0.1:8000/docs
    py run_engine.py --mock          use simulated traffic (no OpenSky call)
    py run_engine.py --offline       simulated traffic, winds and routes (no network)

Outputs: index.html (efficiency map), scoreboard.html (airline ranking),
data/engine.sqlite3 (history), data/arrival_advisories_latest.json.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import flight_tracker as ft  # noqa: E402
from engine.pipeline import EngineConfig, run_once  # noqa: E402
from engine.store import Store  # noqa: E402

log = logging.getLogger("engine")
MIN_INTERVAL_S = 60


def setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logfile = RotatingFileHandler(data_dir / "engine.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logfile.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, logfile])


def run_loop(cfg: EngineConfig, interval_s: int) -> int:
    store = Store(cfg.db_path)
    log.info("Engine started: polling every %d s. Press Ctrl+C to stop.", interval_s)
    cycle = 0
    try:
        while True:
            cycle += 1
            started = time.monotonic()
            try:
                result = run_once(cfg, store)
                if result.source != "LIVE":
                    log.warning("Cycle %d used simulated traffic; it is stored but excluded from the scoreboard.", cycle)
            except Exception:  # keep the service alive whatever one cycle does
                log.exception("Cycle %d failed; retrying next interval.", cycle)
            if cycle % 288 == 0:
                store.prune(keep_days=30)
            time.sleep(max(5.0, interval_s - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("Engine stopped after %d cycles.", cycle)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Global Predictive Airspace Efficiency Engine")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="run continuously (Phase 4)")
    mode.add_argument("--api", action="store_true", help="serve the dispatch API (Phase 6)")
    parser.add_argument("--interval", type=int, default=300, help="seconds between cycles with --loop (default 300)")
    parser.add_argument("--mock", action="store_true", help="simulated traffic instead of OpenSky")
    parser.add_argument("--offline", action="store_true", help="no network at all: simulated traffic, winds and routes")
    parser.add_argument("--all", action="store_true", help="include non-airline callsigns")
    parser.add_argument("--host", default="127.0.0.1", help="API bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="API port (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the map after a one-shot run")
    args = parser.parse_args()

    cfg = EngineConfig(bbox=ft.BOUNDING_BOX, force_mock=args.mock or args.offline,
                       offline_winds=args.offline, offline_routes=args.offline, airlines_only=not args.all)
    setup_logging(cfg.data_dir)

    if args.api:
        import uvicorn
        from engine.api import create_app
        uvicorn.run(create_app(cfg.db_path), host=args.host, port=args.port)
        return 0
    if args.loop:
        return run_loop(cfg, max(MIN_INTERVAL_S, args.interval))

    result = run_once(cfg)
    scored = sorted((m for m in result.metrics if m.efficiency is not None),
                    key=lambda m: m.waste_co2_kg_min, reverse=True)
    print(f"\n{'CALLSIGN':<10}{'ROUTE':<12}{'EFF %':>7}{'LAT %':>7}{'FL %':>7}{'BEST FL':>9}{'EXCESS CO2 kg/min':>19}")
    print("-" * 71)
    for m in scored[:15]:
        route = f"{m.origin}-{m.destination}" if m.destination else "unknown"
        lat = "n/a" if m.lateral_eff is None else f"{m.lateral_eff * 100:.1f}"
        ver = "n/a" if m.vertical_eff is None else f"{m.vertical_eff * 100:.1f}"
        best = f"FL{m.best_level_fl}" if m.best_level_fl else "n/a"
        print(f"{m.callsign:<10}{route:<12}{m.efficiency * 100:>7.1f}{lat:>7}{ver:>7}{best:>9}{m.waste_co2_kg_min:>19.1f}")
    print(f"\nTotal excess burn: {sum(m.waste_co2_kg_min for m in scored):,.0f} kg CO2/min across {len(scored)} scored flights")
    for q in result.queues:
        if q.advisories:
            print(f"{q.hub}: {q.inbound} inbound, {len(q.advisories)} speed advisories, "
                  f"{q.total_co2_saved_kg:,.0f} kg CO2 saved vs holding")
    print()
    if not args.no_browser:
        webbrowser.open(result.map_path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
