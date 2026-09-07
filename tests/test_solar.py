"""Solar position, plane-of-array irradiance, and the surface energy balance.

THE CONTRACT THAT MATTERS MOST
    absorptance = 0 with sky_radiation = False must reproduce the air-only
    model EXACTLY. That is how the 676-hour validation anchor stays live while
    this is built. It is the first test in the file for that reason.

Sources: [PHYSICS]  Duffie & Beckman ch. 1 (declination, hour angle, incidence)
          [PHYSICS]  Swinbank clear-sky radiant temperature
          [NUMERICS] McAdams h = 5.7 + 3.8v
          [LIVE]     NSRDB GOES TMY v4.0.0 at 40.77, -73.98
          [ANLY-002] anchor preservation
          [SPEC]     estimates are labelled

Doc ID: ANLY-002 R1.3
"""

from __future__ import annotations

import math

import pytest

from engine.cavity import (
    CLOUD_LW_OPACITY,
    cloud_opacity,
    exterior_film_coefficient,
    f_warm_estimate,
    radiative_surface_drop,
    sky_temperature_k,
    solar_surface_boost,
)
from engine.moisture import run_year
from engine.psychro import f_to_c
from engine.solar import (
    ORIENTATIONS,
    cos_incidence,
    declination_deg,
    poa_irradiance,
    solar_position,
)

LAT, LON, TZ = 40.77, -73.98, -5.0
T_ROOM_C = f_to_c(70.0)


# ---------------------------------------------------------------------------
# THE ANCHOR  [ANLY-002]
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def year():
    t_c = [-5.0 + 10.0 * math.sin(2 * math.pi * i / 8760.0) for i in range(8760)]
    return t_c, [0.7] * 8760


def _run(year, **kw):
    t_c, rh = year
    return run_year(t_c, rh, f_cold=0.300, f_warm=f_warm_estimate(0.300, 0.17, 1.7),
                    ach=5.0, t_room_c=T_ROOM_C, rh_room=0.35,
                    vent_interior_fraction=1.0, **kw)


def test_alpha_zero_and_no_sky_reproduces_the_air_only_model(year):
    """The single most important test in this file. If it fails, every number
    downstream of the solar work is untrustworthy and the anchor is gone."""
    base = _run(year)
    with_solar_off = _run(year, poa_w_m2=[800.0] * 8760, absorptance=0.0,
                          wind_m_s=[2.0] * 8760, sky_radiation=False)
    assert with_solar_off.hours_condensing == base.hours_condensing
    assert with_solar_off.hours_water_present == base.hours_water_present
    assert with_solar_off.total_condensed_kg_per_m2 == pytest.approx(
        base.total_condensed_kg_per_m2
    )


def test_defaults_leave_the_model_untouched(year):
    """Passing no solar arguments at all must be identical to alpha = 0."""
    assert _run(year).hours_condensing == _run(year, wind_m_s=None).hours_condensing


def test_sun_can_only_reduce_condensation(year):
    """Warming the cold surface cannot create water."""
    base = _run(year)
    sunny = _run(year, poa_w_m2=[400.0] * 8760, absorptance=0.20,
                 wind_m_s=[2.0] * 8760)
    assert sunny.hours_condensing <= base.hours_condensing


def test_sky_radiation_can_only_increase_condensation(year):
    """Cooling the surface cannot dry it."""
    base = _run(year)
    cold = _run(year, wind_m_s=[2.0] * 8760, sky_radiation=True)
    assert cold.hours_condensing >= base.hours_condensing


def test_overcast_sky_removes_the_radiative_penalty(year):
    """Cloud type 2 (fog) is long-wave opaque, so the sky radiates at air
    temperature and the surface stops cooling."""
    clear = _run(year, wind_m_s=[2.0] * 8760, sky_radiation=True,
                 cloud_type=[0.0] * 8760)
    fog = _run(year, wind_m_s=[2.0] * 8760, sky_radiation=True,
               cloud_type=[2.0] * 8760)
    base = _run(year)
    assert fog.hours_condensing < clear.hours_condensing
    # Fog does NOT restore the air-only answer, and should not. The sky then
    # radiates at AIR temperature, but a surface held above air temperature by
    # indoor heat still loses net long-wave to it. Only a surface already at
    # air temperature would see zero drop.
    assert fog.hours_condensing > base.hours_condensing
    # It should nonetheless land far closer to the air-only case than clear
    # sky does - that is the whole point of modelling cloud.
    assert (fog.hours_condensing - base.hours_condensing) < 0.5 * (
        clear.hours_condensing - base.hours_condensing
    )


# ---------------------------------------------------------------------------
# Solar position  [PHYSICS]
# ---------------------------------------------------------------------------

def test_declination_stays_within_the_tropics():
    vals = [declination_deg(d) for d in range(1, 366)]
    assert max(vals) == pytest.approx(23.45, abs=0.1)
    assert min(vals) == pytest.approx(-23.45, abs=0.1)


@pytest.mark.parametrize("day,expected", [(80, 49.2), (172, 72.7), (355, 25.8)])
def test_noon_altitude_matches_astronomy(day, expected):
    """Equinox noon altitude is 90 - latitude; solstices shift by +/-23.45."""
    peak = max(
        solar_position(day, h / 4.0, LAT, LON, TZ) for h in range(0, 96)
    ).altitude_deg if False else max(
        solar_position(day, h / 4.0, LAT, LON, TZ).altitude_deg for h in range(0, 96)
    )
    assert peak == pytest.approx(expected, abs=0.6)


def test_the_sun_is_due_south_at_local_noon():
    peak = max(
        (solar_position(172, h / 4.0, LAT, LON, TZ) for h in range(0, 96)),
        key=lambda s: s.altitude_deg,
    )
    assert peak.azimuth_deg == pytest.approx(180.0, abs=5.0)


def test_the_sun_is_down_at_midnight():
    assert not solar_position(355, 0.5, LAT, LON, TZ).is_up


# ---------------------------------------------------------------------------
# Plane of array  [PHYSICS] [LIVE]
# ---------------------------------------------------------------------------

def test_a_surface_facing_away_receives_no_beam():
    noon = max(
        (solar_position(172, h / 4.0, LAT, LON, TZ) for h in range(0, 96)),
        key=lambda s: s.altitude_deg,
    )
    assert cos_incidence(noon, ORIENTATIONS["north"]) == 0.0
    assert cos_incidence(noon, ORIENTATIONS["south"]) > 0.0


def test_beam_is_never_negative():
    for day in (1, 100, 200, 300):
        for h in range(24):
            sun = solar_position(day, h + 0.5, LAT, LON, TZ)
            for az in ORIENTATIONS.values():
                assert cos_incidence(sun, az) >= 0.0


def test_a_shaded_vertical_wall_still_sees_half_the_sky():
    """Isotropic diffuse: a vertical surface sees half the sky dome, so it
    receives DHI/2 even with the sun behind it."""
    noon = max(
        (solar_position(172, h / 4.0, LAT, LON, TZ) for h in range(0, 96)),
        key=lambda s: s.altitude_deg,
    )
    poa = poa_irradiance(800.0, 700.0, 150.0, noon, ORIENTATIONS["north"])
    assert poa == pytest.approx(150.0 / 2 + 800.0 * 0.2 / 2, rel=1e-6)


def test_night_gives_zero_on_every_orientation():
    midnight = solar_position(355, 0.5, LAT, LON, TZ)
    for az in ORIENTATIONS.values():
        assert poa_irradiance(0.0, 0.0, 0.0, midnight, az) == 0.0


def test_south_beats_north_in_winter_by_a_wide_margin():
    """The commercially interesting fact: low winter sun strikes vertical
    south glass near normal, in exactly the season condensation peaks. [LIVE]"""
    day = 15  # mid-January
    south = north = 0.0
    for h in range(24):
        sun = solar_position(day, h + 0.5, LAT, LON, TZ)
        south += cos_incidence(sun, ORIENTATIONS["south"])
        north += cos_incidence(sun, ORIENTATIONS["north"])
    assert south > 3.0 * max(north, 1e-9)


# ---------------------------------------------------------------------------
# Surface energy balance  [NUMERICS]
# ---------------------------------------------------------------------------

def test_film_coefficient_rises_with_wind():
    assert exterior_film_coefficient(0.0) == pytest.approx(5.7)
    assert exterior_film_coefficient(2.0) == pytest.approx(13.3)
    assert exterior_film_coefficient(6.0) > exterior_film_coefficient(2.0)


def test_wind_suppresses_solar_warming():
    """Faster air strips absorbed heat away, so the same sun warms less."""
    calm = solar_surface_boost(700.0, 0.20, exterior_film_coefficient(1.0))
    windy = solar_surface_boost(700.0, 0.20, exterior_film_coefficient(8.0))
    assert windy < calm / 2.0


def test_solar_boost_is_linear_in_absorptance():
    """Why the orientation COMPARISON survives a wrong alpha: it is a common
    factor across all four, so the ranking and the spread are preserved."""
    h = exterior_film_coefficient(2.0)
    assert solar_surface_boost(500.0, 0.30, h) == pytest.approx(
        3.0 * solar_surface_boost(500.0, 0.10, h)
    )


def test_no_sun_no_boost():
    assert solar_surface_boost(0.0, 0.5, 13.3) == 0.0


@pytest.mark.parametrize("alpha", [-0.1, 1.1])
def test_absorptance_outside_zero_to_one_is_rejected(alpha):
    with pytest.raises(ValueError, match="absorptance"):
        solar_surface_boost(500.0, alpha, 13.3)


def test_clear_sky_is_colder_than_the_air():
    assert sky_temperature_k(-5.0, 0.0) < -5.0 + 273.15


def test_opaque_sky_equals_air_temperature():
    assert sky_temperature_k(-5.0, 1.0) == pytest.approx(-5.0 + 273.15)


def test_radiative_drop_is_never_positive():
    h = exterior_film_coefficient(2.0)
    for t in (-20.0, -5.0, 0.0, 15.0, 30.0):
        assert radiative_surface_drop(t, t, h) <= 0.0


def test_clear_night_drop_is_a_few_degrees():
    """Literature range for vertical glass is roughly 2-4 K. [PHYSICS]"""
    drop = radiative_surface_drop(-5.0, -5.0, exterior_film_coefficient(2.0))
    assert -4.5 < drop < -2.0


def test_cloud_monotonically_reduces_cooling():
    h = exterior_film_coefficient(2.0)
    drops = [radiative_surface_drop(-5.0, -5.0, h, opacity=o)
             for o in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert drops == sorted(drops)
    assert drops[-1] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Cloud mapping  [SPEC]
# ---------------------------------------------------------------------------

def test_clear_codes_are_transparent_and_thick_cloud_is_not():
    assert cloud_opacity(0) == 0.0
    assert cloud_opacity(1) < 0.2
    assert cloud_opacity(3) > 0.8
    assert cloud_opacity(8) > 0.8


def test_fog_is_opaque_despite_transmitting_sunlight():
    """Fog passes 82% of shortwave in the 277 Park TMY yet radiates at
    essentially air temperature. Shortwave transmittance is NOT a valid
    long-wave proxy, which is why this table is judgement, not a fit. [SPEC]"""
    assert cloud_opacity(2) == 1.0


def test_cirrus_stays_semi_transparent():
    assert 0.2 < cloud_opacity(7) < 0.6


def test_every_code_is_a_valid_opacity():
    for code, value in CLOUD_LW_OPACITY.items():
        assert 0.0 <= value <= 1.0, code


def test_unknown_codes_fall_back_to_clear_sky():
    """Clear sky maximises cooling, so an unrecognised code never hides
    condensation."""
    assert cloud_opacity(99) == 0.0
    assert cloud_opacity(None) == 0.0
