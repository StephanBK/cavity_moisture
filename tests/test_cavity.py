"""
AUDIT SUITE - cavity thermal model
===================================

Same convention as test_psychro.py: every assertion cites its source.

  [ANLY-002] the handover document, section cited
  [DERIVED]  arithmetic worked longhand in the docstring
  [PHYSICS]  a bound or limit the answer must satisfy to be physical

Run:  pytest -v
Doc ID: ANLY-002 R1.0, Chunk 2
"""

import pytest

from engine.cavity import (
    CP_AIR,
    H_CAVITY_DEFAULT,
    NFRC_T_IN_C,
    NFRC_T_OUT_C,
    cavity_air_temperature,
    cavity_dry_air_mass,
    cavity_volume,
    f_from_t,
    f_warm_estimate,
    t_from_f,
)

# ===========================================================================
# 1. f-VALUE <-> TEMPERATURE
# ===========================================================================


def test_nfrc_boundary_conditions():
    """[ANLY-002 S2.1] NFRC 100 winter: -18 degC out, 21 degC in."""
    assert NFRC_T_OUT_C == -18.0
    assert NFRC_T_IN_C == 21.0


def test_277_park_values_on_record():
    """[ANLY-002 S2.3] The two assemblies on record must reproduce."""
    assert t_from_f(0.300) == pytest.approx(-6.3, abs=0.01)
    assert t_from_f(0.013) == pytest.approx(-17.5, abs=0.01)


def test_f_endpoints_are_the_boundary_temperatures():
    """[DERIVED] f=0 means the surface is at outdoor air; f=1 at room air."""
    assert t_from_f(0.0) == pytest.approx(NFRC_T_OUT_C)
    assert t_from_f(1.0) == pytest.approx(NFRC_T_IN_C)


def test_f_and_t_are_exact_inverses():
    """[DERIVED] Round-trip closure."""
    for f in (0.0, 0.013, 0.132, 0.300, 0.589, 1.0):
        assert f_from_t(t_from_f(f)) == pytest.approx(f, rel=1e-12)


def test_f_is_independent_of_boundary_conditions():
    """[ANLY-002 S2.1] The linearity argument, made executable.

    f is the NORMALISED solution of a linear operator, so scaling the boundary
    conditions scales the whole field. A surface with f = 0.300 at NFRC
    conditions must still have f = 0.300 on a mild day in Houston. This is
    what licenses stamping one geometry solution across all 8,760 hours.
    """
    f = 0.300
    for t_out, t_in in ((-18.0, 21.0), (5.0, 21.0), (-30.0, 24.0), (15.0, 20.0)):
        t_surf = t_from_f(f, t_out, t_in)
        assert f_from_t(t_surf, t_out, t_in) == pytest.approx(f, rel=1e-12)


def test_f_undefined_when_boundaries_are_equal():
    """[PHYSICS] With no temperature difference there is no gradient to
    normalise against - division by zero must raise, not return inf."""
    with pytest.raises(ValueError):
        f_from_t(20.0, t_out_c=20.0, t_in_c=20.0)


# ===========================================================================
# 2. WARM-SURFACE ESTIMATOR
# ===========================================================================


def test_f_warm_estimate_swr_vig():
    """[DERIVED] 277 Park SWR-VIG:

        f_warm = f_cold + R_cav . U
               = 0.013 + 0.17 x 0.70
               = 0.013 + 0.119
               = 0.132
        T_warm = -18 + 39 x 0.132 = -12.85 degC
    """
    f_warm = f_warm_estimate(f_cold=0.013, r_cavity=0.17, u_assembly=0.70)
    assert f_warm == pytest.approx(0.132, abs=0.0005)
    assert t_from_f(f_warm) == pytest.approx(-12.85, abs=0.05)


def test_f_warm_estimate_swr_ig():
    """[DERIVED] 277 Park SWR-IG:

        f_warm = 0.300 + 0.17 x 1.70 = 0.300 + 0.289 = 0.589
        T_warm = -18 + 39 x 0.589 = 4.97 degC
    """
    f_warm = f_warm_estimate(f_cold=0.300, r_cavity=0.17, u_assembly=1.70)
    assert f_warm == pytest.approx(0.589, abs=0.0005)
    assert t_from_f(f_warm) == pytest.approx(4.97, abs=0.05)


def test_warm_surface_is_always_warmer_than_cold_surface():
    """[PHYSICS] Heat flows outward in winter, so the interior-side cavity
    surface cannot be colder than the exterior-side one."""
    for f_cold, r_cav, u in ((0.013, 0.17, 0.70), (0.300, 0.17, 1.70)):
        assert f_warm_estimate(f_cold, r_cav, u) > f_cold


def test_vig_strands_the_cavity_near_outdoor_temperature():
    """[ANLY-002 S2.2] The field-evidence lesson, as a regression test.

    A VIG is so thermally effective that everything outboard of it - the old
    pane AND the cavity - sits near outdoor temperature. Both cavity surfaces
    in the SWR-VIG case must therefore stay below -10 degC at NFRC
    conditions, despite a 21 degC room on the other side of the glass.
    """
    f_warm = f_warm_estimate(0.013, 0.17, 0.70)
    assert t_from_f(0.013) < -10.0
    assert t_from_f(f_warm) < -10.0


def test_f_warm_estimate_rejects_impossible_result():
    """[PHYSICS] A cavity surface cannot be warmer than the room air. A
    mismatched R and U (e.g. cavity resistance from one assembly, U from
    another) must fail loudly rather than return f > 1."""
    with pytest.raises(ValueError, match="exceeds 1.0"):
        f_warm_estimate(f_cold=0.60, r_cavity=0.17, u_assembly=5.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"f_cold": -0.1, "r_cavity": 0.17, "u_assembly": 0.7},
        {"f_cold": 1.5, "r_cavity": 0.17, "u_assembly": 0.7},
        {"f_cold": 0.3, "r_cavity": 0.0, "u_assembly": 0.7},
        {"f_cold": 0.3, "r_cavity": 0.17, "u_assembly": -1.0},
    ],
)
def test_f_warm_estimate_input_validation(kwargs):
    with pytest.raises(ValueError):
        f_warm_estimate(**kwargs)


# ===========================================================================
# 3. CAVITY AIR TEMPERATURE
# ===========================================================================


def test_unvented_cavity_is_the_film_weighted_mean():
    """[ANLY-002 S5.3] With ach=0 the balance must collapse exactly to the
    film-weighted mean of the two bounding surfaces."""
    t_air = cavity_air_temperature(t_cold_c=-17.49, t_warm_c=-12.85, ach=0.0)
    expected = (3.0 * -12.85 + 3.0 * -17.49) / 6.0
    assert t_air == pytest.approx(expected, rel=1e-12)


def test_equal_films_give_the_arithmetic_mean():
    """[DERIVED] When h_warm = h_cold the weights cancel and the answer is
    the plain midpoint: (-17.49 + -12.85)/2 = -15.17 degC."""
    assert cavity_air_temperature(-17.49, -12.85, ach=0.0) == pytest.approx(
        -15.17, abs=0.01
    )


def test_cavity_air_lies_between_its_bounding_surfaces():
    """[PHYSICS] Unvented, the air cannot be colder than the cold surface nor
    warmer than the warm one. A weighted average can never leave its range."""
    for t_cold, t_warm in ((-17.49, -12.85), (-6.3, 4.97), (0.0, 0.0)):
        t_air = cavity_air_temperature(t_cold, t_warm, ach=0.0)
        assert t_cold - 1e-9 <= t_air <= t_warm + 1e-9


def test_film_coefficient_weighting_pulls_toward_the_tighter_coupling():
    """[DERIVED] Raising h on the cold side must drag the air colder.

        h_cold = 9, h_warm = 3, surfaces -17.49 / -12.85:
        T_air = (3 x -12.85 + 9 x -17.49) / 12 = -16.33 degC
    """
    t_air = cavity_air_temperature(-17.49, -12.85, ach=0.0, h_cold=9.0, h_warm=3.0)
    assert t_air == pytest.approx(-16.33, abs=0.01)
    assert t_air < cavity_air_temperature(-17.49, -12.85, ach=0.0)


def test_absolute_film_value_barely_matters():
    """[DERIVED] T_air depends only on the RATIO h_warm/h_cold, so scaling
    both by any factor must leave the answer unchanged to machine precision.

    This is why a single default film coefficient is defensible: the quantity
    we cannot pin down cancels out of the quantity we need.
    """
    base = cavity_air_temperature(-17.49, -12.85, ach=0.0, h_cold=3.0, h_warm=3.0)
    for scale in (0.5, 2.0, 10.0):
        scaled = cavity_air_temperature(
            -17.49, -12.85, ach=0.0,
            h_cold=3.0 * scale, h_warm=3.0 * scale,
        )
        assert scaled == pytest.approx(base, rel=1e-12)


# ===========================================================================
# 4. THE VENT TERM - proving it is negligible THERMALLY
# ===========================================================================


def test_ventilation_warms_the_cavity_monotonically():
    """[PHYSICS] More room air in means the cavity moves toward room
    temperature. Never away from it."""
    temps = [
        cavity_air_temperature(-17.49, -12.85, t_room_c=21.0, ach=a)
        for a in (0.0, 1.0, 5.0, 20.0, 50.0)
    ]
    assert all(b > a for a, b in zip(temps, temps[1:]))


def test_vent_conductance_is_under_one_percent_at_ach_5():
    """[DERIVED] The magnitude argument, computed rather than asserted.

        m_dot    = ACH x V x rho / 3600 = 5 x 0.0153 x ~1.3 / 3600
                 = 2.8e-5 kg/s per m2
        m_dot.cp = 2.8e-5 x 1006  = 0.028 W/K
        h_w + h_c                  = 6.0   W/K
        ratio                      = 0.5%
    """
    rho = 1.3
    m_dot = 5.0 * 0.0153 * rho / 3600.0
    c_vent = m_dot * CP_AIR
    c_surfaces = 2 * H_CAVITY_DEFAULT
    assert c_vent / c_surfaces < 0.01


def test_vent_shifts_cavity_temperature_only_slightly_even_at_ach_50():
    """[DERIVED] The payoff. Air is a poor carrier of heat but the only
    carrier of moisture.

    At an implausibly high ACH of 50, the SWR-VIG cavity moves from -15.17 to
    about -13.49 degC: 1.7 K, against a 36 K room-to-cavity gap. Under 5% of
    the way to room temperature.

    So S5.3's film-weighted mean is justified in the thermal half - now shown
    rather than assumed. The SAME ventilation will dominate the moisture
    balance in Chunk 3, and the contrast is the whole reason moisture beats
    temperature as a modelling priority (S4).
    """
    sealed = cavity_air_temperature(-17.49, -12.85, t_room_c=21.0, ach=0.0)
    free = cavity_air_temperature(-17.49, -12.85, t_room_c=21.0, ach=50.0)
    fraction_closed = (free - sealed) / (21.0 - sealed)
    assert sealed == pytest.approx(-15.17, abs=0.01)
    assert free == pytest.approx(-13.49, abs=0.05)
    assert fraction_closed < 0.05


def test_vent_requires_a_room_temperature():
    """[PHYSICS] Ventilating with air of unspecified temperature is not a
    defined problem. Must raise rather than silently assume."""
    with pytest.raises(ValueError, match="t_room_c is required"):
        cavity_air_temperature(-17.49, -12.85, ach=5.0)


def test_zero_ach_does_not_require_a_room_temperature():
    """[DERIVED] The unvented case is well defined without it."""
    assert cavity_air_temperature(-17.49, -12.85, ach=0.0) == pytest.approx(
        -15.17, abs=0.01
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ach": -1.0, "t_room_c": 21.0},
        {"gap_m": 0.0},
        {"h_cold": 0.0},
        {"h_warm": -3.0},
    ],
)
def test_cavity_air_input_validation(kwargs):
    with pytest.raises(ValueError):
        cavity_air_temperature(-17.49, -12.85, **kwargs)


# ===========================================================================
# 5. CAVITY MASS AND VOLUME
# ===========================================================================


def test_cavity_volume_is_gap_times_one_square_metre():
    """[ANLY-002 S5.3] 15.3 mm gap -> 0.0153 m3 per m2."""
    assert cavity_volume(0.0153) == pytest.approx(0.0153)


def test_cavity_dry_air_mass_at_freezing():
    """[DERIVED] m_cav = gap x rho_dry(0 degC) = 0.0153 x 1.2922
                       = 0.01977 kg dry air per m2.

    A very small reservoir - roughly 20 grams of air per square metre - which
    is why the cavity responds fast to both ventilation and condensation.
    """
    assert cavity_dry_air_mass(0.0, gap_m=0.0153) == pytest.approx(0.01977, abs=1e-4)


def test_cavity_mass_scales_linearly_with_gap():
    """[DERIVED] Doubling the gap doubles the air, at fixed temperature."""
    single = cavity_dry_air_mass(0.0, gap_m=0.0153)
    double = cavity_dry_air_mass(0.0, gap_m=0.0306)
    assert double == pytest.approx(2.0 * single, rel=1e-12)


def test_colder_cavity_holds_more_dry_air():
    """[PHYSICS] Cold air is denser, so a cold cavity holds MORE mass in the
    same volume. Relevant because it means the cold VIG cavity has a slightly
    larger moisture reservoir than a warm one of the same width."""
    assert cavity_dry_air_mass(-17.5) > cavity_dry_air_mass(0.0) > cavity_dry_air_mass(21.0)


def test_moisture_reduces_dry_air_mass():
    """[PHYSICS] Water vapour displaces dry air. W is per kg DRY air, so this
    correction has to be present or the Chunk 3 balance carries a ~1% bias."""
    assert cavity_dry_air_mass(0.0, w=0.004) < cavity_dry_air_mass(0.0, w=0.0)


@pytest.mark.parametrize("bad_gap", [0.0, -0.01])
def test_gap_validation(bad_gap):
    with pytest.raises(ValueError):
        cavity_volume(bad_gap)
    with pytest.raises(ValueError):
        cavity_dry_air_mass(0.0, gap_m=bad_gap)
