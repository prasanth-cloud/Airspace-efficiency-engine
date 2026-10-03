"""Phase 5: Predictive arrival queueing.

For each priority hub the engine projects every inbound aircraft's arrival
time from its remaining great-circle distance and current groundspeed, then
runs a first-come-first-served runway slot model at the hub's arrival rate.

When demand outruns capacity, each aircraft's required delay is absorbed as far
as possible by slowing down at cruise while still far out (up to 7% of airspeed
when more than 150 NM from the hub), and only the remainder is left to
low-altitude holding. Holding at around FL100 burns roughly 45 kg of fuel per
minute in a single-aisle jet, while slowing in cruise costs almost nothing
extra, which is where the CO2 saving comes from.

Limitation: only aircraft inside the geofence are visible, so traffic still
outside it within the 3-hour horizon is not yet counted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .airports import AIRPORTS, PRIORITY_HUBS
from .efficiency import CO2_PER_KG_FUEL, CRUISE_FUEL_KG_MIN, FlightMetrics
from .geo import MS_TO_KNOTS, M_TO_NM, speed_of_sound_ms

HORIZON_S = 3 * 3600
BIN_S = 15 * 60
MAX_SPEED_REDUCTION = 0.07
MIN_ABSORB_DISTANCE_NM = 150
MIN_ADVISORY_DELAY_MIN = 1.0
HOLDING_FUEL_KG_MIN = 45.0
SPEED_CONTROL_EXTRA_FRACTION = 0.15  # net extra burn per minute of added cruise time
APPROACH_SPEED_FACTOR = 0.85         # aircraft slow down on approach


@dataclass
class Advisory:
    hub: str
    callsign: str
    eta_utc: str
    slot_utc: str
    distance_nm: float
    delay_min: float
    absorbed_min: float
    holding_min: float
    current_kt: int
    advised_kt: int
    advised_mach: float | None
    co2_saved_kg: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HubQueue:
    hub: str
    arrival_rate: int
    inbound: int
    demand_bins: list[int]          # arrivals per 15-minute bin over the next 3 h
    capacity_per_bin: float
    overloaded: bool
    total_delay_min: float
    total_co2_saved_kg: float
    advisories: list[Advisory] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["advisories"] = [a.to_dict() for a in self.advisories]
        return d


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_arrivals(metrics: list[FlightMetrics], now: datetime | None = None,
                  hubs: list[str] = PRIORITY_HUBS) -> list[HubQueue]:
    now = now or datetime.now(timezone.utc)
    queues: list[HubQueue] = []
    for hub in hubs:
        airport = AIRPORTS[hub]
        inbound = []
        for m in metrics:
            if m.destination not in (hub, airport.icao) or not m.dist_to_dest_m or not m.gs_ms:
                continue
            eta_s = m.dist_to_dest_m / max(m.gs_ms * APPROACH_SPEED_FACTOR, 60.0)
            if eta_s <= HORIZON_S:
                inbound.append((eta_s, m))
        inbound.sort(key=lambda x: x[0])

        spacing = 3600 / airport.arrival_rate
        bins = [0] * (HORIZON_S // BIN_S)
        next_free = 0.0
        q = HubQueue(hub, airport.arrival_rate, len(inbound), bins,
                     round(airport.arrival_rate * BIN_S / 3600, 1), False, 0.0, 0.0)
        for eta_s, m in inbound:
            bins[min(int(eta_s // BIN_S), len(bins) - 1)] += 1
            slot_s = max(eta_s, next_free)
            next_free = slot_s + spacing
            delay_min = (slot_s - eta_s) / 60
            if delay_min < MIN_ADVISORY_DELAY_MIN:
                continue

            dist_nm = m.dist_to_dest_m * M_TO_NM
            remaining_min = eta_s / 60
            absorbable = 0.0
            if m.phase == "cruise" and dist_nm >= MIN_ABSORB_DISTANCE_NM:
                absorbable = remaining_min * (1 / (1 - MAX_SPEED_REDUCTION) - 1)
            absorbed = min(delay_min, absorbable)
            holding = delay_min - absorbed

            tas = m.tas_ms or m.gs_ms
            ratio = remaining_min / (remaining_min + absorbed) if absorbed else 1.0
            advised_tas = tas * ratio
            mach = None
            if m.alt_m and m.alt_m > 7_000:
                mach = round(advised_tas / speed_of_sound_ms(m.alt_m), 2)
            saved = absorbed * (HOLDING_FUEL_KG_MIN - SPEED_CONTROL_EXTRA_FRACTION * CRUISE_FUEL_KG_MIN) * CO2_PER_KG_FUEL

            q.advisories.append(Advisory(
                hub=hub, callsign=m.callsign,
                eta_utc=_iso(now + timedelta(seconds=eta_s)),
                slot_utc=_iso(now + timedelta(seconds=slot_s)),
                distance_nm=round(dist_nm, 1), delay_min=round(delay_min, 1),
                absorbed_min=round(absorbed, 1), holding_min=round(holding, 1),
                current_kt=round(tas * MS_TO_KNOTS), advised_kt=round(advised_tas * MS_TO_KNOTS),
                advised_mach=mach, co2_saved_kg=round(saved, 1),
            ))
        q.overloaded = any(b > q.capacity_per_bin for b in bins)
        q.total_delay_min = round(sum(a.delay_min for a in q.advisories), 1)
        q.total_co2_saved_kg = round(sum(a.co2_saved_kg for a in q.advisories), 1)
        queues.append(q)
    return queues
