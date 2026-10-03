"""Airport and airline reference data for the East Coast scope.

Arrival capacities (aircraft per hour) are approximate good-weather planning
values. Real Airport Arrival Rates change with weather, runway configuration
and time of day, so edit ``arrival_rate`` here as better figures become
available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Airport:
    icao: str
    iata: str
    name: str
    lat: float
    lon: float
    arrival_rate: int  # arrivals per hour


AIRPORTS: dict[str, Airport] = {a.iata: a for a in [
    Airport("KBOS", "BOS", "Boston Logan", 42.3656, -71.0096, 60),
    Airport("KJFK", "JFK", "New York JFK", 40.6413, -73.7781, 44),
    Airport("KLGA", "LGA", "New York LaGuardia", 40.7769, -73.8740, 38),
    Airport("KEWR", "EWR", "Newark Liberty", 40.6895, -74.1745, 40),
    Airport("KPHL", "PHL", "Philadelphia", 39.8744, -75.2424, 52),
    Airport("KIAD", "IAD", "Washington Dulles", 38.9531, -77.4565, 64),
    Airport("KDCA", "DCA", "Washington National", 38.8512, -77.0402, 32),
    Airport("KBWI", "BWI", "Baltimore/Washington", 39.1774, -76.6684, 40),
    Airport("KCLT", "CLT", "Charlotte Douglas", 35.2144, -80.9473, 82),
    Airport("KRDU", "RDU", "Raleigh-Durham", 35.8801, -78.7880, 40),
    Airport("KATL", "ATL", "Atlanta Hartsfield-Jackson", 33.6407, -84.4277, 126),
    Airport("KMCO", "MCO", "Orlando", 28.4312, -81.3081, 64),
    Airport("KTPA", "TPA", "Tampa", 27.9755, -82.5332, 48),
    Airport("KFLL", "FLL", "Fort Lauderdale", 26.0742, -80.1506, 44),
    Airport("KMIA", "MIA", "Miami", 25.7959, -80.2870, 68),
    Airport("KPIT", "PIT", "Pittsburgh", 40.4915, -80.2329, 48),
]}

AIRPORTS_BY_ICAO: dict[str, Airport] = {a.icao: a for a in AIRPORTS.values()}

# Hubs the Phase 5 arrival manager prioritises
PRIORITY_HUBS = ["JFK", "ATL", "BOS", "EWR", "LGA", "CLT", "PHL", "IAD", "DCA", "MIA", "MCO"]

AIRLINES: dict[str, str] = {
    "AAL": "American Airlines", "DAL": "Delta Air Lines", "UAL": "United Airlines",
    "JBU": "JetBlue", "SWA": "Southwest Airlines", "NKS": "Spirit Airlines",
    "FFT": "Frontier Airlines", "ASA": "Alaska Airlines", "AAY": "Allegiant Air",
    "SCX": "Sun Country", "MXY": "Breeze Airways", "RPA": "Republic Airways",
    "EDV": "Endeavor Air", "ENY": "Envoy Air", "SKW": "SkyWest Airlines",
    "PDT": "Piedmont Airlines", "JIA": "PSA Airlines", "ASH": "Mesa Airlines",
    "GJS": "GoJet Airlines", "CPZ": "Compass Airlines", "AWI": "Air Wisconsin",
    "FDX": "FedEx", "UPS": "UPS Airlines", "GTI": "Atlas Air", "ABX": "ABX Air",
    "ACA": "Air Canada", "JZA": "Jazz Aviation", "WJA": "WestJet", "TSC": "Air Transat",
    "BAW": "British Airways", "VIR": "Virgin Atlantic", "DLH": "Lufthansa",
    "AFR": "Air France", "KLM": "KLM", "IBE": "Iberia", "AAR": "Asiana",
    "EIN": "Aer Lingus", "UAE": "Emirates", "QTR": "Qatar Airways",
    "THY": "Turkish Airlines", "AVA": "Avianca", "CMP": "Copa Airlines",
    "AMX": "Aeromexico", "BWA": "Caribbean Airlines", "LAN": "LATAM",
    "TAM": "LATAM Brasil", "ICE": "Icelandair", "SWR": "Swiss", "TAP": "TAP Air Portugal",
    "EJA": "NetJets", "LXJ": "Flexjet",
}


def airline_name(callsign: str) -> str:
    code = callsign[:3].upper()
    return AIRLINES.get(code, code)


def lookup(code: Optional[str]) -> Optional[Airport]:
    if not code:
        return None
    code = code.upper()
    return AIRPORTS.get(code) or AIRPORTS_BY_ICAO.get(code)
