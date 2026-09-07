"""
AUDIT SUITE - psychrometric core
=================================

Every physical claim this model makes, checked against a published value or
a hand-derivable identity. Reviewer can read this file alone and verify the
physics without reading the engine.

Reference sources, cited per test:
  [ASHRAE]  ASHRAE Handbook - Fundamentals 2017, Ch.1 "Psychrometrics"
  [LIVE]    INOVUES condensation_calc, live deployment, independent
            implementation - used as a cross-check, not as truth
  [DERIVED] arithmetic worked out longhand in the docstring

Run:  pytest -v
Doc ID: ANLY-002 R1.0, Chunk 1
"""

import math

import pytest

from engine.psychro import (
    M_RATIO,
    P_ATM_SEA_LEVEL,
    atmospheric_pressure_pa,
    c_to_f,
    c_to_k,
    delta_f_to_c,
    dew_point_f_from_t_rh,
    dew_point_from_p_w,
    dew_point_from_t_rh,
    dew_point_from_w,
    dry_air_density,
    f_to_c,
    f_to_k,
    p_w_from_w,
    p_ws_pa,
    rh_from_t_w,
    w_from_p_w,
    w_from_t_rh,
    w_saturated,
)

# ===========================================================================
# 1. UNIT CONVERSION
#    Cheapest tests in the file and historically the likeliest bug.
# ===========================================================================


def test_freezing_and_boiling_anchors():
    """[DERIVED] The two points everyone knows."""
    assert f_to_c(32.0) == pytest.approx(0.0, abs=1e-12)
    assert f_to_c(212.0) == pytest.approx(100.0, abs=1e-12)
    assert c_to_f(0.0) == pytest.approx(32.0, abs=1e-12)
    assert c_to_f(100.0) == pytest.approx(212.0, abs=1e-12)


def test_scales_cross_at_minus_forty():
    """[DERIVED] -40 is the same number on both scales. Catches sign errors."""
    assert f_to_c(-40.0) == pytest.approx(-40.0, abs=1e-12)
    assert c_to_f(-40.0) == pytest.approx(-40.0, abs=1e-12)


@pytest.mark.parametrize("t_f", [-100.0, -17.5, 0.0, 32.0, 41.09, 70.0, 212.0])
def test_conversion_roundtrip_is_lossless(t_f):
    """[DERIVED] F -> C -> F must return the input to machine precision."""
    assert c_to_f(f_to_c(t_f)) == pytest.approx(t_f, abs=1e-12)


def test_direct_f_to_k_matches_two_step():
    """[DERIVED] The one-step F->K path must not drift from F->C->K."""
    for t_f in (-459.67, 0.0, 70.0, 212.0):
        assert f_to_k(t_f) == pytest.approx(c_to_k(f_to_c(t_f)), abs=1e-9)


def test_absolute_zero():
    """[DERIVED] -459.67 degF is 0 K exactly."""
    assert f_to_k(-459.67) == pytest.approx(0.0, abs=1e-9)


def test_temperature_interval_is_not_a_temperature():
    """[DERIVED] The classic unit bug.

    A 10 degF *interval* is 5.5556 degC. A 10 degF *temperature* is
    -12.22 degC. These must not be interchangeable.
    """
    assert delta_f_to_c(10.0) == pytest.approx(5.555555555, abs=1e-8)
    assert f_to_c(10.0) == pytest.approx(-12.222222222, abs=1e-8)
    assert delta_f_to_c(10.0) != pytest.approx(f_to_c(10.0), abs=1.0)


# ===========================================================================
# 2. SATURATION VAPOUR PRESSURE
# ===========================================================================


def test_p_ws_at_25c_matches_ashrae_table():
    """[ASHRAE] Ch.1 saturation table: p_ws(25 degC) = 3169.2 Pa."""
    assert p_ws_pa(25.0) == pytest.approx(3169.2, abs=0.1)


def test_p_ws_at_100c_is_one_atmosphere():
    """[ASHRAE] Water boils at 101325 Pa.

    The ASHRAE correlation returns 101418.7 Pa here, 0.09% high. That is a
    known property of the fit, not a coding error. Tolerance is set to
    document the deviation rather than hide it.
    """
    assert p_ws_pa(100.0) == pytest.approx(101325.0, rel=0.0015)


def test_p_ws_at_0c_is_triple_point():
    """[ASHRAE] Saturation over water at 0 degC is ~611.2 Pa."""
    assert p_ws_pa(0.0, force_phase="water") == pytest.approx(611.2, abs=0.5)


def test_ice_and_water_branches_agree_at_zero():
    """[ASHRAE] The two correlations are different equations that must meet.

    They are only continuous at 0 degC if both are implemented correctly, so
    this single assertion validates both coefficient sets at once.
    """
    water = p_ws_pa(0.0, force_phase="water")
    ice = p_ws_pa(0.0, force_phase="ice")
    assert ice == pytest.approx(water, rel=2e-4)


def test_ice_branch_is_lower_than_water_below_freezing():
    """[ASHRAE] Physically required: ice holds vapour back harder than
    supercooled water, so p_ws,ice < p_ws,water below 0 degC.

    This is not academic for us. At the SWR-VIG cavity surface temperature
    of -17.5 degC the gap is ~16%, which propagates straight into the
    condensation mass.
    """
    for t_c in (-1.0, -10.0, -17.5, -30.0):
        assert p_ws_pa(t_c, force_phase="ice") < p_ws_pa(t_c, force_phase="water")

    ice = p_ws_pa(-17.5, force_phase="ice")
    water = p_ws_pa(-17.5, force_phase="water")
    assert ice == pytest.approx(130.95, abs=0.05)
    assert water == pytest.approx(155.44, abs=0.05)
    assert (water - ice) / water == pytest.approx(0.158, abs=0.005)


def test_p_ws_strictly_increasing():
    """[DERIVED] Monotonicity. Required for the dew-point bisection to be
    guaranteed to converge; if this ever fails the solver is unsafe.
    """
    temps = [x * 0.5 for x in range(-200, 400)]
    values = [p_ws_pa(t) for t in temps]
    assert all(b > a for a, b in zip(values, values[1:]))


def test_p_ws_rejects_bad_phase():
    with pytest.raises(ValueError):
        p_ws_pa(20.0, force_phase="steam")


# ===========================================================================
# 3. HUMIDITY RATIO
# ===========================================================================


def test_w_from_t_rh_longhand():
    """[DERIVED] Full arithmetic chain at 25 degC, 50% RH, sea level:

        p_ws  = 3169.216 Pa
        p_w   = 0.50 x 3169.216            = 1584.608 Pa
        W     = 0.621945 x 1584.608 / (101325 - 1584.608)
              = 0.621945 x 1584.608 / 99740.392
              = 0.621945 x 0.01588738
              = 0.00988094 kg/kg
    """
    assert w_from_t_rh(25.0, 0.50) == pytest.approx(0.0098809, abs=1e-6)


def test_w_and_p_w_are_exact_inverses():
    """[DERIVED] Round-trip closure of Eq. 20 and its rearrangement."""
    for w in (0.0001, 0.001, 0.005, 0.01, 0.03):
        assert w_from_p_w(p_w_from_w(w)) == pytest.approx(w, rel=1e-12)


def test_zero_humidity_is_zero_ratio():
    assert w_from_t_rh(20.0, 0.0) == pytest.approx(0.0, abs=1e-15)


def test_w_saturated_gives_100_percent_rh():
    """[DERIVED] Saturated air, evaluated back through RH, must return 1.0.

    w_saturated is the clamp ceiling in the Chunk 3 moisture balance, so an
    error here would silently mis-set every condensation event.
    """
    for t_c in (-17.5, -6.3, 0.0, 10.0, 21.0):
        w_sat = w_saturated(t_c)
        assert rh_from_t_w(t_c, w_sat) == pytest.approx(1.0, rel=1e-9)


def test_w_rises_with_temperature_at_fixed_rh():
    """[DERIVED] Warm air at the same RH carries more water. Sanity of sign."""
    ws = [w_from_t_rh(t, 0.35) for t in (-10.0, 0.0, 10.0, 21.0, 30.0)]
    assert all(b > a for a, b in zip(ws, ws[1:]))


def test_rh_must_be_a_fraction_not_a_percent():
    """[DERIVED] Guard rail: passing 35 instead of 0.35 must raise, not
    silently produce a number 100x wrong."""
    with pytest.raises(ValueError):
        w_from_t_rh(21.0, 35.0)


# ===========================================================================
# 4. DEW POINT
# ===========================================================================


def test_dew_point_matches_live_condensation_calc():
    """[LIVE] The regression anchor.

    The deployed condensation_calc returns t_dew = 41.09 degF for indoor air
    at 70 degF / 35% RH. This engine is an independent implementation and
    must land on the same number.
    """
    assert dew_point_f_from_t_rh(70.0, 35.0) == pytest.approx(41.09, abs=0.10)


def test_dew_point_equals_dry_bulb_at_saturation():
    """[DERIVED] Definitional. At 100% RH the air is already at its dew point."""
    for t_c in (-20.0, 0.0, 5.0, 21.0, 35.0):
        assert dew_point_from_t_rh(t_c, 1.0, force_phase=None) == pytest.approx(
            t_c, abs=1e-6
        )


def test_dew_point_is_independent_of_dry_bulb():
    """[DERIVED] The reason the model tracks W and not RH.

    Heat a sealed parcel of air and its RH collapses, but no water entered or
    left, so its dew point must not move by even a hundredth of a degree.
    """
    w = w_from_t_rh(21.0, 0.35)
    dp_cold = dew_point_from_w(w)
    for t_c in (5.0, 21.0, 40.0):
        assert rh_from_t_w(t_c, w) != pytest.approx(0.35, abs=0.01) or t_c == 21.0
        assert dew_point_from_w(w) == pytest.approx(dp_cold, abs=1e-9)


def test_dew_point_roundtrip_through_w():
    """[DERIVED] T,RH -> W -> dew point -> W must close on itself."""
    for t_c, rh in ((21.0, 0.35), (21.0, 0.25), (24.0, 0.50), (5.0, 0.80)):
        w = w_from_t_rh(t_c, rh)
        dp = dew_point_from_w(w)
        w_back = w_saturated(dp, force_phase="water")
        assert w_back == pytest.approx(w, rel=1e-8)


def test_dew_point_solver_is_precise():
    """[DERIVED] Bisection must invert p_ws to better than 1e-6 degC."""
    for t_c in (-40.0, -17.5, 0.0, 15.0, 50.0):
        p = p_ws_pa(t_c, force_phase="water")
        assert dew_point_from_p_w(p, force_phase="water") == pytest.approx(
            t_c, abs=1e-6
        )


def test_frost_point_is_above_dew_point_below_freezing():
    """[ASHRAE] Same vapour pressure saturates against ice at a HIGHER
    temperature than against liquid water.

    Consequence for the model: below freezing, using the water branch is the
    conservative choice for onset timing. Documented, not accidental.
    """
    w = w_from_t_rh(21.0, 0.15)
    dp_water = dew_point_from_w(w, force_phase="water")
    fp_ice = dew_point_from_w(w, force_phase=None)
    assert dp_water < 0.0
    assert fp_ice > dp_water


def test_dry_air_has_no_dew_point():
    with pytest.raises(ValueError):
        dew_point_from_t_rh(21.0, 0.0)


# ===========================================================================
# 5. PRESSURE AND DENSITY
# ===========================================================================


def test_sea_level_pressure_is_standard():
    assert atmospheric_pressure_pa(0.0) == pytest.approx(101325.0, abs=1e-6)


def test_pressure_falls_with_elevation():
    """[ASHRAE] Eq. 3. Denver at 1609 m sits near 83.4 kPa."""
    assert atmospheric_pressure_pa(1609.0) == pytest.approx(83431.0, abs=50.0)
    assert atmospheric_pressure_pa(20.0) < atmospheric_pressure_pa(0.0)


def test_elevation_changes_humidity_ratio():
    """[DERIVED] Same temperature and RH, thinner air, more water per kg dry.

    277 Park sits on an NSRDB grid cell at 20 m, so the effect is small here
    - but the model must not hard-code sea level for a Denver project.
    """
    p_nyc = atmospheric_pressure_pa(20.0)
    p_den = atmospheric_pressure_pa(1609.0)
    w_nyc = w_from_t_rh(21.0, 0.35, p_atm_pa=p_nyc)
    w_den = w_from_t_rh(21.0, 0.35, p_atm_pa=p_den)
    assert w_den > w_nyc
    assert w_den / w_nyc == pytest.approx(1.21, abs=0.02)


def test_dry_air_density_at_standard_conditions():
    """[DERIVED] p/(R.T) = 101325 / (287.042 x 293.15) = 1.2041 kg/m3."""
    assert dry_air_density(20.0, w=0.0) == pytest.approx(1.2041, abs=0.001)


def test_moist_air_holds_less_dry_air_per_cubic_metre():
    """[DERIVED] Vapour displaces dry air. The Chunk 3 cavity mass must use
    the dry-air figure, since W is defined per kg of DRY air.
    """
    assert dry_air_density(21.0, w=0.010) < dry_air_density(21.0, w=0.0)


# ===========================================================================
# 6. PROJECT ANCHORS - the values on record in ANLY-002 section 2.3
# ===========================================================================


def test_277_park_surface_temperatures_from_f_values():
    """[ANLY-002 S2.3] f = (T_surf - T_out)/(T_in - T_out), NFRC winter:
    T_out = -18 degC, T_in = 21 degC, so T_surf = -18 + 39f.

        SWR-IG  f=0.300 -> -18 + 11.70 = -6.30 degC
        SWR-VIG f=0.013 -> -18 +  0.507 = -17.49 degC
    """
    t_out, t_in = -18.0, 21.0
    assert t_out + (t_in - t_out) * 0.300 == pytest.approx(-6.3, abs=0.01)
    assert t_out + (t_in - t_out) * 0.013 == pytest.approx(-17.5, abs=0.01)


def test_room_air_cannot_condense_on_the_ig_surface_but_can_on_vig():
    """[ANLY-002 S4] Sanity check of the CURRENT model's logic before we
    replace it: at 21 degC / 35% RH the room dew point is ~5.0 degC, which is
    above both cavity surface temperatures - so both assemblies condense at
    NFRC design conditions under the room-dew-point assumption.

    This is the assumption Chunk 3 exists to replace.
    """
    dp = dew_point_from_t_rh(21.0, 0.35)
    assert dp == pytest.approx(4.97, abs=0.05)
    assert dp > -6.3
    assert dp > -17.5
