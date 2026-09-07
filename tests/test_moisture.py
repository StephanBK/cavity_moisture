"""
AUDIT SUITE - hourly moisture balance
======================================

  [ANLY-002] the handover document, section cited
  [DERIVED]  arithmetic worked longhand in the docstring
  [PHYSICS]  a bound the answer must satisfy to be physical
  [NUMERICS] a property of the solver rather than the physics

Doc ID: ANLY-002 R1.0, Chunk 3
"""

import math

import pytest

from engine.cavity import f_warm_estimate, t_from_f
from engine.moisture import (
    ACH_PRESETS,
    ACH_PRESETS_ARE_ESTIMATES,
    clamp_to_saturation,
    exchange_analytic,
    exchange_euler,
    drain_excess,
    MAX_SURFACE_FILM_KG_PER_M2,
    run_year,
    step_hour,
    sweep_ach,
)
from engine.psychro import dew_point_from_w, w_from_t_rh, w_saturated

# ---------------------------------------------------------------------------
# Shared fixtures - a short synthetic cold spell, enough to exercise the loop
# ---------------------------------------------------------------------------

F_COLD_VIG = 0.013
F_WARM_VIG = f_warm_estimate(0.013, 0.17, 0.70)
F_COLD_IG = 0.300
F_WARM_IG = f_warm_estimate(0.300, 0.17, 1.70)


def _synthetic_year(mean_c=12.5, swing_c=13.0, n=8760):
    """Sinusoidal annual + daily temperature cycle. Not real weather - only a
    deterministic driver so tests do not depend on a network call."""
    t, rh = [], []
    for h in range(n):
        doy = h / 24.0
        val = (
            mean_c
            - swing_c * math.cos(2 * math.pi * (doy - 15) / 365)
            - 4.0 * math.cos(2 * math.pi * ((h % 24) - 15) / 24)
        )
        t.append(val)
        rh.append(0.65 + 0.15 * math.sin(2 * math.pi * (h % 24) / 24))
    return t, rh


# ===========================================================================
# 1. THE AIR-EXCHANGE STEP - why we deviated from S5.1
# ===========================================================================


def test_analytic_matches_euler_at_low_ach():
    """[NUMERICS] The deviation is documented, not hidden.

    Where Euler is valid the two forms must agree. At ACH = 0.01 over one
    hour, exp(-0.01) = 0.99005 vs Euler's 0.99 - a 0.005% difference.
    """
    w0, w_sup = 0.001, 0.005
    for ach in (0.001, 0.01, 0.05):
        a = exchange_analytic(w0, w_sup, ach, 1.0)
        e = exchange_euler(w0, w_sup, ach, 1.0)
        assert a == pytest.approx(e, rel=0.03)


def test_euler_overshoots_where_analytic_does_not():
    """[NUMERICS] The failure ANLY-002 S5.1's form would have produced.

    Starting dry (W=0.001) with humid supply (W=0.005) at ACH = 5:
        Euler:    0.001 + 5 x (0.005 - 0.001) = 0.021  -> 4x PAST the supply
        Analytic: 0.005 + (0.001-0.005).exp(-5) = 0.00497 -> approaches it

    Euler's answer is not merely imprecise, it is on the wrong side of the
    target and would drive spurious condensation.
    """
    w0, w_sup, ach = 0.001, 0.005, 5.0
    e = exchange_euler(w0, w_sup, ach, 1.0)
    a = exchange_analytic(w0, w_sup, ach, 1.0)
    assert e == pytest.approx(0.021, abs=1e-6)
    assert e > w_sup
    assert w0 < a < w_sup


def test_euler_diverges_at_high_ach():
    """[NUMERICS] At ACH = 50 the Euler iteration blows up. Asserted so the
    reason for the deviation is on the record and reproducible."""
    w, w_sup = 0.001, 0.005
    for _ in range(6):
        w = exchange_euler(w, w_sup, 50.0, 1.0)
    assert abs(w) > 1.0  # physically absurd; W is order 0.005


def test_analytic_never_overshoots_at_any_ach():
    """[NUMERICS] exp(-x) lies in (0, 1] for x >= 0, so the result is always a
    convex combination of start and supply. Cannot leave the interval."""
    for ach in (0.0, 0.1, 1.0, 5.0, 50.0, 1000.0, 1e6):
        for w0, w_sup in ((0.001, 0.005), (0.008, 0.002)):
            out = exchange_analytic(w0, w_sup, ach, 1.0)
            assert min(w0, w_sup) - 1e-15 <= out <= max(w0, w_sup) + 1e-15


def test_zero_ach_is_a_sealed_cavity():
    """[PHYSICS] No exchange means no change from exchange."""
    assert exchange_analytic(0.003, 0.009, 0.0, 1.0) == 0.003


def test_infinite_exchange_reaches_supply():
    """[PHYSICS] Very fast exchange makes the cavity indistinguishable from
    its supply air. This is the mechanism behind the regression anchor."""
    assert exchange_analytic(0.001, 0.005, 1000.0, 1.0) == pytest.approx(0.005, rel=1e-9)


def test_exchange_is_time_composable():
    """[NUMERICS] Two half-hour steps must equal one full-hour step exactly.
    True of the exponential, false of Euler. This is what makes sub-stepping
    safe to add for accuracy without changing the exchange answer."""
    once = exchange_analytic(0.001, 0.005, 3.0, 1.0)
    twice = exchange_analytic(exchange_analytic(0.001, 0.005, 3.0, 0.5), 0.005, 3.0, 0.5)
    assert once == pytest.approx(twice, rel=1e-12)


@pytest.mark.parametrize("bad", [{"ach": -1.0}, {"dt_hours": 0.0}, {"dt_hours": -1.0}])
def test_exchange_validation(bad):
    kwargs = {"w_cav": 0.001, "w_supply": 0.005, "ach": 1.0, "dt_hours": 1.0}
    kwargs.update(bad)
    with pytest.raises(ValueError):
        exchange_analytic(**kwargs)


# ===========================================================================
# 2. THE CONDENSATION CLAMP
# ===========================================================================


def test_condensation_mass_longhand():
    """[DERIVED] W_cav = 0.0054, W_sat = 0.0010, m_cav = 0.0198 kg/m2:

        condensed = (0.0054 - 0.0010) x 0.0198
                  = 0.0044 x 0.0198
                  = 8.712e-5 kg/m2   (87 mg per square metre)
    """
    w, cond, evap, water = clamp_to_saturation(0.0054, 0.0010, 0.0198, 0.0)
    assert cond == pytest.approx(8.712e-5, rel=1e-9)
    assert w == pytest.approx(0.0010)
    assert evap == 0.0
    assert water == pytest.approx(8.712e-5, rel=1e-9)


def test_no_condensation_below_saturation():
    """[PHYSICS] Unsaturated air with no liquid present does nothing."""
    w, cond, evap, water = clamp_to_saturation(0.0010, 0.0054, 0.0198, 0.0)
    assert (w, cond, evap, water) == (0.0010, 0.0, 0.0, 0.0)


def test_evaporation_returns_water_to_the_air():
    """[PHYSICS] The drying mechanism. Air at W=0.001 with headroom to
    W_sat=0.005 and 1e-4 kg of liquid on the glass:

        capacity  = (0.005 - 0.001) x 0.0198 = 7.92e-5 kg
        available = 1.0e-4 kg
        evaporated = min(...) = 7.92e-5 kg, air reaches saturation,
        2.08e-5 kg of liquid remains.
    """
    w, cond, evap, water = clamp_to_saturation(0.0010, 0.0050, 0.0198, 1.0e-4)
    assert evap == pytest.approx(7.92e-5, rel=1e-6)
    assert w == pytest.approx(0.0050, rel=1e-9)
    assert water == pytest.approx(2.08e-5, rel=1e-4)
    assert cond == 0.0


def test_evaporation_cannot_exceed_available_water():
    """[PHYSICS] You cannot evaporate water that is not there."""
    w, cond, evap, water = clamp_to_saturation(0.0010, 0.0500, 0.0198, 1.0e-6)
    assert evap == pytest.approx(1.0e-6)
    assert water == pytest.approx(0.0, abs=1e-18)
    assert w < 0.0500


def test_clamp_conserves_mass():
    """[PHYSICS] Water is neither created nor destroyed - it only moves
    between the air and the glass. Total inventory must be unchanged."""
    for w0, w_sat, water0 in ((0.0054, 0.0010, 0.0), (0.0010, 0.0050, 1e-4)):
        m = 0.0198
        before = w0 * m + water0
        w1, cond, evap, water1 = clamp_to_saturation(w0, w_sat, m, water0)
        after = w1 * m + water1
        assert after == pytest.approx(before, rel=1e-12)


def test_surface_water_never_negative():
    """[PHYSICS] A negative puddle is not a thing."""
    _, _, _, water = clamp_to_saturation(0.0001, 0.9, 0.0198, 5e-7)
    assert water >= 0.0


@pytest.mark.parametrize("bad", [{"m_cav": 0.0}, {"m_cav": -1.0}, {"surface_water_kg": -1e-6}])
def test_clamp_validation(bad):
    kwargs = {"w_cav": 0.003, "w_sat_cold": 0.001, "m_cav": 0.0198, "surface_water_kg": 0.0}
    kwargs.update(bad)
    with pytest.raises(ValueError):
        clamp_to_saturation(**kwargs)


# ===========================================================================
# 3. THE REGRESSION ANCHOR - free exchange must reproduce the old model
# ===========================================================================


def test_onset_criterion_matches_the_room_assumption_exactly():
    """[ANLY-002 S4] THE regression anchor, stated where it is exact.

    The existing condensation app says: condensation occurs when room air
    meets a surface below its dew point. Physically that is the ACH -> infinity
    limit with no liquid present. Our engine must agree on that onset
    criterion exactly, in both directions, or the moisture model is wrong.
    """
    m = 0.0198
    for w_sat, should_condense in ((0.0010, True), (0.0100, False)):
        _, cond, _, _ = step_hour(
            w_cav=0.0054, w_supply=0.0054, w_sat_cold=w_sat,
            ach=1000.0, m_cav=m, surface_water_kg=0.0,
        )
        assert (cond > 0.0) is should_condense


def test_saturated_hours_never_fall_below_the_room_assumption():
    """[ANLY-002 S4] The anchor at year scale, stated as a direction.

    Our model must never report FEWER saturated hours than the room-dew-point
    criterion: every hour that criterion flags, room air really is above the
    surface ceiling. It may report MORE, and does - that excess is the drying
    tail, which the old model has no liquid inventory to represent.
    """
    t, rh = _synthetic_year()
    for f_cold, f_warm in ((F_COLD_VIG, F_WARM_VIG), (F_COLD_IG, F_WARM_IG)):
        r = run_year(t, rh, f_cold, f_warm, ach=100.0, keep_hours=False)
        assert r.hours_saturated >= r.hours_condensing_room_assumption


def test_thinner_retained_film_converges_to_the_room_assumption():
    """[ANLY-002 S4] The mechanism behind the excess, demonstrated.

    The gap between our saturated hours and the room criterion is the drying
    tail, and its length is set by how much liquid the glass retains. Shrink
    the retained film and the tail must shrink with it, converging on the old
    model's answer. That both explains the discrepancy and shows it is a
    modelled effect rather than a numerical artefact.

    It also flags where the uncertainty now sits: MAX_SURFACE_FILM_KG_PER_M2
    is an estimate, and it materially moves the wet-hour count.
    """
    t, rh = _synthetic_year()
    gaps = []
    for film in (0.1, 0.01, 0.001, 0.0001):
        r = run_year(
            t, rh, F_COLD_IG, F_WARM_IG, ach=100.0,
            max_film_kg=film, keep_hours=False,
        )
        gaps.append(r.hours_saturated - r.hours_condensing_room_assumption)
    assert all(b <= a for a, b in zip(gaps, gaps[1:]))
    assert gaps[-1] < gaps[0]


def test_free_exchange_cavity_dew_point_equals_room_dew_point():
    """[PHYSICS] With no condensation anywhere in the year, fast exchange must
    put the cavity at exactly the room dew point."""
    t, rh = _synthetic_year(mean_c=25.0, swing_c=2.0)
    r = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=100.0, keep_hours=False)
    assert r.hours_condensing == 0
    assert r.mean_cavity_dew_point_c == pytest.approx(r.mean_room_dew_point_c, abs=0.05)


# ===========================================================================
# 3b. DRAINAGE - the term ANLY-002 S5 omits
# ===========================================================================


def test_drainage_caps_the_film():
    """[PHYSICS] Vertical glass cannot hold an unbounded pool."""
    retained, drained = drain_excess(0.35, max_film_kg=0.1)
    assert retained == pytest.approx(0.1)
    assert drained == pytest.approx(0.25)


def test_drainage_leaves_small_amounts_alone():
    retained, drained = drain_excess(0.02, max_film_kg=0.1)
    assert retained == pytest.approx(0.02)
    assert drained == 0.0


def test_surface_water_stays_bounded_over_a_year():
    """[PHYSICS] The bug this term fixes.

    Without drainage a free-exchange run accumulated 4.7 kg/m2 - five litres
    standing on vertical glass - and the phantom pool then drove 688 spurious
    condensation hours through re-evaporation cycling.
    """
    t, rh = _synthetic_year()
    r = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=100.0, keep_hours=False)
    assert r.peak_surface_water_kg_per_m2 <= MAX_SURFACE_FILM_KG_PER_M2 + 1e-12
    assert r.total_drained_kg_per_m2 > 0.0


def test_condensed_water_is_conserved_across_drainage():
    """[PHYSICS] Every gram condensed must end up evaporated, drained, or
    still on the glass. Nothing may vanish."""
    t, rh = _synthetic_year(n=2000)
    r = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=5.0, spinup_passes=0, keep_hours=True)
    condensed = sum(h.condensed_kg for h in r.hours)
    evaporated = sum(h.evaporated_kg for h in r.hours)
    drained = sum(h.drained_kg for h in r.hours)
    remaining = r.hours[-1].surface_water_kg
    assert condensed == pytest.approx(evaporated + drained + remaining, rel=1e-9)


# ===========================================================================
# 4. DIRECTIONAL BEHAVIOUR
# ===========================================================================


def test_more_interior_venting_means_more_condensed_water():
    """[PHYSICS] A non-obvious and commercially important result.

    Room air is far more humid in ABSOLUTE terms than a sub-freezing cavity
    can hold. So venting the cavity to the interior supplies the very water
    that condenses. More exchange means more water delivered, hence more mass
    condensed. Sealing reduces it.

    This directly challenges the intuition that venting a cavity dries it.
    Venting dries a cavity only when the vent air is drier than the cavity.
    """
    t, rh = _synthetic_year()
    masses = [
        run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=a, keep_hours=False
                 ).total_condensed_kg_per_m2
        for a in (0.1, 0.5, 5.0, 20.0, 100.0)
    ]
    assert all(b > a for a, b in zip(masses, masses[1:]))


def test_exterior_venting_dries_a_winter_cavity():
    """[PHYSICS] The design lever this tool exists to expose.

    Winter outdoor air is cold but very DRY in absolute terms - far drier than
    heated interior air. So venting to outdoors supplies less water than
    venting to the room, and condenses less.
    """
    t, rh = _synthetic_year(mean_c=0.0, swing_c=8.0)
    interior = run_year(
        t, rh, F_COLD_IG, F_WARM_IG, ach=5.0,
        vent_interior_fraction=1.0, keep_hours=False,
    )
    exterior = run_year(
        t, rh, F_COLD_IG, F_WARM_IG, ach=5.0,
        vent_interior_fraction=0.0, keep_hours=False,
    )
    assert exterior.total_condensed_kg_per_m2 < interior.total_condensed_kg_per_m2


def test_colder_surface_condenses_more():
    """[PHYSICS] Lower f means a colder surface means a lower saturation
    ceiling means more condensation. The SWR-VIG problem in one assertion."""
    t, rh = _synthetic_year()
    vig = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=5.0, keep_hours=False)
    ig = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, keep_hours=False)
    assert vig.hours_condensing > ig.hours_condensing
    assert vig.total_condensed_kg_per_m2 > ig.total_condensed_kg_per_m2


def test_drier_room_condenses_less():
    """[PHYSICS] The operating lever available to a building manager. This is
    what an RH envelope recommendation ultimately rests on."""
    t, rh = _synthetic_year()
    wet = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, rh_room=0.50, keep_hours=False)
    dry = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, rh_room=0.20, keep_hours=False)
    assert dry.total_condensed_kg_per_m2 < wet.total_condensed_kg_per_m2


def test_warm_climate_never_condenses():
    """[PHYSICS] No cold surface, no condensation. Guards against a model that
    manufactures water out of nothing."""
    t, rh = _synthetic_year(mean_c=28.0, swing_c=1.0)
    r = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, keep_hours=False)
    assert r.hours_condensing == 0
    assert r.total_condensed_kg_per_m2 == pytest.approx(0.0, abs=1e-15)


# ===========================================================================
# 5. NUMERICAL SOUNDNESS
# ===========================================================================


def test_result_is_exactly_invariant_to_substep_count():
    """[NUMERICS] The hour is solved in closed form, so sub-dividing it must
    change NOTHING - not approximately, exactly.

    This started as a convergence test and failed: operator splitting captured
    only 68% of the true condensation at 6 sub-steps and 90% at 24, decaying
    as 1/n. See step_hour's docstring. The closed form removed the error
    rather than shrinking it, which is why this assertion can be exact.
    """
    t, rh = _synthetic_year()
    results = [
        run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, substeps=n, keep_hours=False)
        for n in (1, 2, 6, 24, 96)
    ]
    for r in results[1:]:
        assert r.total_condensed_kg_per_m2 == pytest.approx(
            results[0].total_condensed_kg_per_m2, rel=1e-9
        )
        assert r.hours_condensing == results[0].hours_condensing


def test_pinned_condensation_rate_longhand():
    """[DERIVED] Once the cavity is saturated it pins there and condensation
    runs at a constant rate. Starting already at saturation, t* = 0:

        rate = ACH . m_cav . (W_supply - W_sat)
             = 5.0 x 0.0198 x (0.0054 - 0.0010)
             = 5.0 x 0.0198 x 0.0044
             = 4.356e-4 kg/m2 per hour
    """
    w, cond, evap, water = step_hour(
        w_cav=0.0010, w_supply=0.0054, w_sat_cold=0.0010,
        ach=5.0, m_cav=0.0198, surface_water_kg=0.0, dt_hours=1.0,
    )
    assert cond == pytest.approx(4.356e-4, rel=1e-9)
    assert w == pytest.approx(0.0010)
    assert evap == 0.0


def test_lead_in_time_before_saturation():
    """[DERIVED] Starting dry, nothing condenses until the air reaches
    saturation. With W_0 = 0.0010, W_supply = 0.0054, W_sat = 0.0050:

        t* = -ln[(0.0054-0.0050)/(0.0054-0.0010)] / ACH
           = -ln(0.0004/0.0044) / 5
           = -ln(0.090909) / 5 = 2.3979 / 5 = 0.4796 h

        condensed = 5 x 0.0198 x 0.0004 x (1 - 0.4796) = 2.061e-5 kg/m2

    A naive scheme that condensed for the whole hour would overstate this by
    roughly a factor of two.
    """
    w, cond, evap, water = step_hour(
        w_cav=0.0010, w_supply=0.0054, w_sat_cold=0.0050,
        ach=5.0, m_cav=0.0198, surface_water_kg=0.0, dt_hours=1.0,
    )
    assert cond == pytest.approx(2.061e-5, rel=1e-3)
    assert w == pytest.approx(0.0050)


def test_no_condensation_when_saturation_is_never_reached():
    """[PHYSICS] Slow exchange into a nearly-saturated ceiling: if t* exceeds
    the hour, the hour is pure exchange and nothing deposits."""
    w, cond, evap, water = step_hour(
        w_cav=0.0010, w_supply=0.0054, w_sat_cold=0.0053,
        ach=0.05, m_cav=0.0198, surface_water_kg=0.0, dt_hours=1.0,
    )
    assert cond == 0.0
    assert 0.0010 < w < 0.0053


def test_step_hour_bookkeeping_is_closed():
    """[PHYSICS] The liquid inventory must change by exactly what was
    deposited minus what was evaporated. No water may appear or vanish inside
    the step."""
    cases = [
        (0.0010, 0.0054, 0.0010, 5.0, 0.0),
        (0.0050, 0.0005, 0.0050, 5.0, 1e-4),
        (0.0060, 0.0054, 0.0010, 0.0, 0.0),
        (0.0010, 0.0054, 0.0050, 0.05, 0.0),
    ]
    for w0, w_sup, w_sat, ach, water0 in cases:
        _, cond, evap, water1 = step_hour(w0, w_sup, w_sat, ach, 0.0198, water0)
        assert water1 - water0 == pytest.approx(cond - evap, abs=1e-15)
        assert cond >= 0.0 and evap >= 0.0
        assert not (cond > 0 and evap > 0)  # a step cannot do both


def test_supersaturation_carried_in_deposits_immediately():
    """[PHYSICS] If the surface cools between hours, the air is instantly
    above its new ceiling and the excess must deposit at once."""
    w, cond, evap, water = step_hour(
        w_cav=0.0060, w_supply=0.0005, w_sat_cold=0.0010,
        ach=0.0, m_cav=0.0198, surface_water_kg=0.0,
    )
    assert cond == pytest.approx((0.0060 - 0.0010) * 0.0198, rel=1e-9)
    assert w == pytest.approx(0.0010)


def test_sealed_cavity_cycles_its_trapped_inventory():
    """[PHYSICS] With ach=0 nothing enters or leaves, so condensation and
    evaporation must exactly trade one inventory back and forth."""
    m, water = 0.0198, 0.0
    w = 0.0054
    w, c1, _, water = step_hour(w, 0.0, 0.0010, 0.0, m, water)   # cold: deposits
    w, _, e1, water = step_hour(w, 0.0, 0.0080, 0.0, m, water)   # warm: re-evaporates
    assert c1 > 0 and e1 > 0
    assert e1 == pytest.approx(c1, rel=1e-9)
    assert water == pytest.approx(0.0, abs=1e-18)


def test_result_is_insensitive_to_spinup():
    """[NUMERICS] The cavity starts at room humidity, which is arbitrary. One
    discarded pass must be enough that the answer no longer depends on it."""
    t, rh = _synthetic_year()
    one = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=0.1, spinup_passes=1, keep_hours=False)
    three = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=0.1, spinup_passes=3, keep_hours=False)
    assert three.hours_condensing == pytest.approx(one.hours_condensing, rel=0.02)


def test_hourly_records_are_complete_and_consistent():
    """[NUMERICS] The Excel raw tab depends on these. Every hour recorded,
    every condensing flag matching its mass, no humidity ratio negative."""
    t, rh = _synthetic_year(n=720)
    r = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=5.0, keep_hours=True)
    assert len(r.hours) == 720
    assert r.hours_total == 720
    for h in r.hours:
        assert h.w_cav >= 0.0
        assert h.surface_water_kg >= 0.0
        assert h.is_condensing == (h.condensed_kg > 0.0)
        assert h.drained_kg >= 0.0
        assert h.surface_water_kg <= MAX_SURFACE_FILM_KG_PER_M2 + 1e-12
        assert h.w_cav <= h.w_sat_cold + 1e-12
        assert h.t_cold_c <= h.t_air_c <= h.t_warm_c + 1e-9


def test_summary_arithmetic_is_self_consistent():
    """[DERIVED] Percentages and extremes must match the underlying series."""
    t, rh = _synthetic_year(n=2000)
    r = run_year(t, rh, F_COLD_VIG, F_WARM_VIG, ach=5.0, keep_hours=True)
    assert r.pct_condensing == pytest.approx(100.0 * r.hours_condensing / 2000)
    assert r.hours_condensing == sum(1 for h in r.hours if h.is_condensing)
    dps = [h.dew_point_cav_c for h in r.hours]
    assert r.min_cavity_dew_point_c == pytest.approx(min(dps))
    assert r.max_cavity_dew_point_c == pytest.approx(max(dps))


# ===========================================================================
# 6. ACH PRESETS AND SWEEP
# ===========================================================================


def test_presets_are_flagged_as_estimates():
    """[ANLY-002 S5.3] The document says ACH is THE unknown and never fixes
    values. Ours are estimates and the code must say so, because they surface
    in a customer-facing UI."""
    assert ACH_PRESETS_ARE_ESTIMATES is True


def test_presets_span_three_orders_of_magnitude():
    """[ANLY-002 S5.3] "Bound it, don't guess it" - the bracket is only
    meaningful if it is wide."""
    values = sorted(ACH_PRESETS.values())
    assert values[0] <= 0.1
    assert values[-1] >= 100.0
    assert values[-1] / values[0] >= 1000.0


def test_sweep_returns_one_summary_per_value():
    t, rh = _synthetic_year(n=1000)
    ach_values = [0.1, 1.0, 10.0]
    results = sweep_ach(t, rh, F_COLD_IG, F_WARM_IG, ach_values)
    assert [r.ach for r in results] == ach_values
    assert all(r.hours == [] for r in results)  # memory guard


def test_sweep_accepts_arbitrary_user_values():
    """[REQUIREMENT] ACH is a free input, not a fixed three-case bracket, so
    the tool can test vent and seal designs that do not match a preset."""
    t, rh = _synthetic_year(n=500)
    results = sweep_ach(t, rh, F_COLD_IG, F_WARM_IG, [0.37, 12.5, 88.0])
    assert len(results) == 3


# ===========================================================================
# 7. INPUT VALIDATION
# ===========================================================================


@pytest.mark.parametrize(
    "bad",
    [
        {"ach": -1.0},
        {"substeps": 0},
        {"vent_interior_fraction": 1.5},
        {"vent_interior_fraction": -0.1},
    ],
)
def test_run_year_validation(bad):
    t, rh = _synthetic_year(n=48)
    kwargs = {"ach": 5.0}
    kwargs.update(bad)
    with pytest.raises(ValueError):
        run_year(t, rh, F_COLD_IG, F_WARM_IG, **kwargs)


def test_mismatched_weather_series_rejected():
    with pytest.raises(ValueError, match="rh_out"):
        run_year([0.0, 1.0, 2.0], [0.5, 0.5], F_COLD_IG, F_WARM_IG, ach=5.0)


def test_empty_weather_rejected():
    with pytest.raises(ValueError, match="empty"):
        run_year([], [], F_COLD_IG, F_WARM_IG, ach=5.0)
