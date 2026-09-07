"""
Weather and location services.

TWO EXTERNAL DEPENDENCIES
-------------------------
  1. Mapbox Geocoding v6   - address text -> (lon, lat)
  2. NREL NSRDB PSM3 TMY    - (lon, lat) -> 8,760 hours of weather

Both mirror the architecture of the existing INOVUES condensation_calc app so
the two tools behave consistently and can share a mental model.

SECRETS
-------
MAPBOX_TOKEN is a *publishable* token and is handed to the browser via
/config. NREL_API_KEY is NEVER exposed - all NSRDB calls happen server-side.
Neither is ever committed; both are Railway service variables.

TESTABILITY
-----------
``parse_nsrdb_csv`` is a pure function taking text and returning a WeatherYear.
It has no network dependency, so the CSV contract is testable in CI without an
API key. The network layer around it is a thin shell.

Doc ID: ANLY-002 R1.0, Chunk 3b
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

MAPBOX_GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/forward"
NSRDB_TMY_URL = "https://developer.nrel.gov/api/nsrdb/v2/solar/psm3-tmy-download.csv"

#: NSRDB attributes we request. Temperature and RH are what the moisture model
#: needs; surface pressure lets us use measured rather than standard-atmosphere
#: pressure in the psychrometrics.
NSRDB_ATTRIBUTES = "air_temperature,relative_humidity,surface_pressure"

DEFAULT_TIMEOUT = 120
CACHE_DIR = Path(os.environ.get("WEATHER_CACHE_DIR", "cache"))

#: NSRDB grid resolution is about 4 km, so coordinates are snapped before
#: caching. Two addresses in the same block must not trigger two downloads.
CACHE_PRECISION = 2


class WeatherError(RuntimeError):
    """Raised when a location or weather lookup fails in a way the user can act on."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Location:
    query: str
    matched_address: str
    lat: float
    lon: float


@dataclass
class WeatherYear:
    """One typical meteorological year of hourly data."""

    t_out_c: list[float]
    rh_out: list[float]
    elevation_m: float = 0.0
    grid_lat: float | None = None
    grid_lon: float | None = None
    time_zone: float | None = None
    station_id: str | None = None
    source: str = "NSRDB PSM3 TMY"
    surface_pressure_pa: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if len(self.t_out_c) != len(self.rh_out):
            raise WeatherError(
                f"Temperature has {len(self.t_out_c)} values but humidity has "
                f"{len(self.rh_out)}"
            )
        if not self.t_out_c:
            raise WeatherError("Weather series is empty")

    @property
    def hours(self) -> int:
        return len(self.t_out_c)

    def describe(self) -> dict:
        return {
            "source": self.source,
            "hours": self.hours,
            "elevation_m": self.elevation_m,
            "grid_lat": self.grid_lat,
            "grid_lon": self.grid_lon,
            "time_zone": self.time_zone,
            "station_id": self.station_id,
            "t_out_c_min": round(min(self.t_out_c), 2),
            "t_out_c_max": round(max(self.t_out_c), 2),
            "t_out_c_mean": round(sum(self.t_out_c) / self.hours, 2),
        }


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(address: str, token: str | None = None, timeout: int = 30) -> Location:
    """Resolve an address to coordinates via Mapbox Geocoding v6."""
    token = token or os.environ.get("MAPBOX_TOKEN", "")
    if not token:
        raise WeatherError(
            "MAPBOX_TOKEN is not set. Address lookup is unavailable; set it as a "
            "service variable."
        )
    if not address or not address.strip():
        raise WeatherError("No address supplied")

    resp = requests.get(
        MAPBOX_GEOCODE_URL,
        params={"q": address.strip(), "limit": 1, "access_token": token},
        timeout=timeout,
    )
    if resp.status_code == 401:
        raise WeatherError("Mapbox rejected the token (401). Check MAPBOX_TOKEN.")
    resp.raise_for_status()

    features = resp.json().get("features") or []
    if not features:
        raise WeatherError(f"No location found for {address!r}")

    first = features[0]
    lon, lat = first["geometry"]["coordinates"]
    matched = (
        first.get("properties", {}).get("full_address")
        or first.get("properties", {}).get("name")
        or address
    )
    return Location(query=address, matched_address=matched, lat=lat, lon=lon)


# ---------------------------------------------------------------------------
# NSRDB parsing - pure, no network
# ---------------------------------------------------------------------------

def parse_nsrdb_csv(text: str) -> WeatherYear:
    """Parse an NSRDB PSM3 TMY CSV into a WeatherYear.

    FORMAT
        row 1  metadata field names   (Source, Location ID, Latitude, ...)
        row 2  metadata values
        row 3  data column headers    (Year, Month, Day, Hour, Minute,
                                       Temperature, Relative Humidity, ...)
        row 4+ hourly data

    Columns are located BY NAME rather than by position, because NSRDB orders
    them according to the attributes requested. Indexing by position would
    silently mis-read the file the first time anyone edits NSRDB_ATTRIBUTES.
    """
    if not text or not text.strip():
        raise WeatherError("NSRDB returned an empty response")

    # An API-key or quota error comes back as JSON, not CSV.
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        errors = payload.get("errors") or [payload.get("error", {}).get("message")]
        raise WeatherError(f"NSRDB error: {errors}")

    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 4:
        raise WeatherError(
            f"NSRDB response has only {len(rows)} rows; expected metadata plus "
            "8,760 hours"
        )

    meta = dict(zip(rows[0], rows[1]))
    header = rows[2]

    def column(*names: str) -> int:
        for name in names:
            if name in header:
                return header.index(name)
        raise WeatherError(
            f"NSRDB response is missing column {names[0]!r}. Columns present: "
            f"{header}"
        )

    i_t = column("Temperature", "Air Temperature")
    i_rh = column("Relative Humidity")
    try:
        i_p = column("Pressure", "Surface Pressure")
    except WeatherError:
        i_p = None

    t_out_c: list[float] = []
    rh_out: list[float] = []
    pressures: list[float] = []

    for row in rows[3:]:
        if not row or not row[i_t].strip():
            continue
        t_out_c.append(float(row[i_t]))
        # NSRDB reports RH in percent; the engine expects a fraction.
        rh_out.append(min(max(float(row[i_rh]) / 100.0, 0.0), 1.0))
        if i_p is not None:
            # NSRDB reports pressure in millibar (hPa); 1 mbar = 100 Pa.
            pressures.append(float(row[i_p]) * 100.0)

    if len(t_out_c) not in (8760, 8784):
        raise WeatherError(
            f"NSRDB returned {len(t_out_c)} hours; expected 8760 (or 8784 in a "
            "leap year)"
        )

    def meta_float(key: str) -> float | None:
        try:
            return float(meta[key])
        except (KeyError, TypeError, ValueError):
            return None

    return WeatherYear(
        t_out_c=t_out_c,
        rh_out=rh_out,
        elevation_m=meta_float("Elevation") or 0.0,
        grid_lat=meta_float("Latitude"),
        grid_lon=meta_float("Longitude"),
        time_zone=meta_float("Time Zone"),
        station_id=meta.get("Location ID"),
        surface_pressure_pa=pressures,
    )


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def _cache_key(lat: float, lon: float) -> str:
    snapped = f"{round(lat, CACHE_PRECISION)},{round(lon, CACHE_PRECISION)}"
    digest = hashlib.sha256(snapped.encode()).hexdigest()[:16]
    return f"tmy_{digest}.json"


def _cache_read(path: Path) -> WeatherYear | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        return WeatherYear(**payload)
    except (json.JSONDecodeError, TypeError, WeatherError, OSError):
        return None


def _cache_write(path: Path, year: WeatherYear) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(year.__dict__))
    except OSError:
        pass  # Cache is an optimisation; never let it break a request.


# ---------------------------------------------------------------------------
# NSRDB fetch
# ---------------------------------------------------------------------------

def fetch_tmy(
    lat: float,
    lon: float,
    api_key: str | None = None,
    email: str | None = None,
    cache_dir: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[WeatherYear, bool]:
    """Download a TMY year for a point. Returns ``(year, was_cached)``.

    NSRDB snaps the request to its own ~4 km grid, so the returned latitude and
    longitude will differ slightly from the ones sent. That snapped point is
    reported back to the user rather than hidden.
    """
    api_key = api_key or os.environ.get("NREL_API_KEY", "")
    if not api_key:
        raise WeatherError(
            "NREL_API_KEY is not set. Weather data is unavailable; set it as a "
            "service variable."
        )
    email = email or os.environ.get("NREL_EMAIL", "info@inovues.com")

    cache_path = (cache_dir or CACHE_DIR) / _cache_key(lat, lon)
    cached = _cache_read(cache_path)
    if cached is not None:
        return cached, True

    resp = requests.get(
        NSRDB_TMY_URL,
        params={
            "wkt": f"POINT({lon} {lat})",
            "names": "tmy",
            "interval": "60",
            "utc": "false",
            "leap_day": "false",
            "attributes": NSRDB_ATTRIBUTES,
            "api_key": api_key,
            "email": email,
        },
        timeout=timeout,
    )
    if resp.status_code == 403:
        raise WeatherError("NSRDB rejected the API key (403). Check NREL_API_KEY.")
    if resp.status_code == 429:
        raise WeatherError("NSRDB rate limit reached (429). Try again shortly.")
    resp.raise_for_status()

    year = parse_nsrdb_csv(resp.text)
    _cache_write(cache_path, year)
    return year, False


def get_weather_for_address(
    address: str,
    mapbox_token: str | None = None,
    nrel_api_key: str | None = None,
) -> tuple[Location, WeatherYear, bool]:
    """Address text to weather, in one call. The path the API endpoint uses."""
    location = geocode(address, token=mapbox_token)
    year, cached = fetch_tmy(location.lat, location.lon, api_key=nrel_api_key)
    return location, year, cached
