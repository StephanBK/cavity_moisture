"""Solar position and plane-of-array irradiance.

WHY THIS EXISTS
    Until now every surface temperature followed outdoor AIR temperature via
    the f-value alone. Sunlight absorbed in the glass raises the surface above
    air temperature, which dries a cavity that the air-only model says is still
    wetting. A south-facing vertical pane in a New York January receives more
    irradiance than the same pane in June - the low sun strikes it near
    normal - and that is exactly the season the model reports as worst.

    Roughly a third of all wet hours fall between 09:00 and 16:00, so this is
    not a rounding effect.

SCOPE AND HONESTY
    Solar position uses the standard declination / hour-angle construction
    (Duffie & Beckman ch. 1). Diffuse sky is ISOTROPIC (Liu-Jordan), not Perez:
    isotropic underestimates POA near the sun's position and is the
    conservative choice for a drying calculation. Ground reflectance defaults
    to 0.2 and is an ESTIMATE.

    Nothing here converts irradiance into a temperature. That happens in
    engine.cavity, where absorptance enters as a user input.

UNITS
    SI throughout. Angles in degrees at the boundary, radians internally.

Doc ID: ANLY-002 R1.3
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Ground reflectance (albedo). ESTIMATE. 0.2 is the conventional value for
#: an unspecified urban surface; snow would be 0.6-0.8 and would raise the
#: ground-reflected term on every orientation equally.
GROUND_ALBEDO = 0.2

#: Surface azimuths, degrees clockwise from north. A vertical window faces
#: OUTWARD, so a "south" window has its normal pointing at 180.
ORIENTATIONS = {"north": 0.0, "east": 90.0, "south": 180.0, "west": 270.0}


@dataclass(frozen=True)
class SolarPosition:
    """Where the sun is. Altitude <= 0 means below the horizon."""

    altitude_deg: float
    azimuth_deg: float  # clockwise from north

    @property
    def is_up(self) -> bool:
        return self.altitude_deg > 0.0


def declination_deg(day_of_year: int) -> float:
    """Solar declination, Cooper's equation. Range +/-23.45 deg."""
    return 23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0))


def equation_of_time_min(day_of_year: int) -> float:
    """Difference between apparent and mean solar time, minutes."""
    b = math.radians(360.0 * (day_of_year - 81) / 364.0)
    return 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)


def solar_position(
    day_of_year: int,
    hour_local: float,
    latitude_deg: float,
    longitude_deg: float,
    time_zone: float,
) -> SolarPosition:
    """Sun altitude and azimuth for a local standard time.

    ``hour_local`` is decimal hours in LOCAL STANDARD time. NSRDB is fetched
    with ``utc=false`` and stamps each row at minute 30, so the caller should
    pass ``hour + 0.5`` to land mid-interval rather than on the boundary.

    ``time_zone`` is the UTC offset in hours (New York standard time = -5).
    Daylight saving is deliberately ignored: NSRDB reports standard time all
    year, and shifting it would misplace the sun by an hour for half the year.
    """
    lat = math.radians(latitude_deg)
    dec = math.radians(declination_deg(day_of_year))

    # Longitude correction: 4 minutes per degree from the zone meridian.
    standard_meridian = 15.0 * time_zone
    solar_time = (
        hour_local
        + (4.0 * (longitude_deg - standard_meridian) + equation_of_time_min(day_of_year))
        / 60.0
    )
    hour_angle = math.radians(15.0 * (solar_time - 12.0))

    sin_alt = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(
        hour_angle
    )
    sin_alt = min(max(sin_alt, -1.0), 1.0)
    altitude = math.asin(sin_alt)

    # Azimuth measured clockwise from north, via atan2 so quadrants are right.
    y = math.sin(hour_angle)
    x = math.cos(hour_angle) * math.sin(lat) - math.tan(dec) * math.cos(lat)
    azimuth = math.degrees(math.atan2(y, x)) + 180.0
    return SolarPosition(math.degrees(altitude), azimuth % 360.0)


def cos_incidence(
    sun: SolarPosition, surface_azimuth_deg: float, tilt_deg: float = 90.0
) -> float:
    """Cosine of the angle between the beam and the surface normal.

    Clamped at zero: a surface facing away from the sun receives no beam, it
    does not receive negative beam.
    """
    if not sun.is_up:
        return 0.0
    alt = math.radians(sun.altitude_deg)
    tilt = math.radians(tilt_deg)
    delta_az = math.radians(sun.azimuth_deg - surface_azimuth_deg)
    cos_theta = math.sin(alt) * math.cos(tilt) + math.cos(alt) * math.sin(tilt) * math.cos(
        delta_az
    )
    return max(0.0, cos_theta)


def poa_irradiance(
    ghi: float,
    dni: float,
    dhi: float,
    sun: SolarPosition,
    surface_azimuth_deg: float,
    tilt_deg: float = 90.0,
    albedo: float = GROUND_ALBEDO,
) -> float:
    """Total irradiance on a tilted surface, W/m2. Isotropic sky.

    Three components:
      beam    DNI * cos(incidence)
      sky     DHI * (1 + cos tilt) / 2   -> half the sky for a vertical wall
      ground  GHI * albedo * (1 - cos tilt) / 2

    Isotropic diffuse is a documented approximation. It underestimates POA
    around the solar disc, so a drying result computed from it is conservative.
    """
    if ghi <= 0.0 and dni <= 0.0 and dhi <= 0.0:
        return 0.0
    tilt = math.radians(tilt_deg)
    beam = max(0.0, dni) * cos_incidence(sun, surface_azimuth_deg, tilt_deg)
    sky = max(0.0, dhi) * (1.0 + math.cos(tilt)) / 2.0
    ground = max(0.0, ghi) * albedo * (1.0 - math.cos(tilt)) / 2.0
    return beam + sky + ground


def poa_series(
    ghi: list[float],
    dni: list[float],
    dhi: list[float],
    latitude_deg: float,
    longitude_deg: float,
    time_zone: float,
    surface_azimuth_deg: float,
    tilt_deg: float = 90.0,
    albedo: float = GROUND_ALBEDO,
) -> list[float]:
    """POA for a full year, hour by hour. Index 0 is 1 January, hour 0."""
    n = len(ghi)
    if not (len(dni) == len(dhi) == n):
        raise ValueError(
            f"Irradiance series lengths differ: ghi {n}, dni {len(dni)}, dhi {len(dhi)}"
        )
    out: list[float] = []
    for i in range(n):
        day = i // 24 + 1
        sun = solar_position(day, i % 24 + 0.5, latitude_deg, longitude_deg, time_zone)
        out.append(
            poa_irradiance(
                ghi[i], dni[i], dhi[i], sun, surface_azimuth_deg, tilt_deg, albedo
            )
        )
    return out
