"""
Hourly cavity moisture balance - the core of ANLY-002.

THE QUESTION THIS ANSWERS
-------------------------
The existing condensation app assumes the cavity dew point equals the room
dew point. That assumption is unverified (ANLY-002 S4). This module computes
the cavity dew point instead, by tracking how much water is actually in the
cavity air, hour by hour, across a full TMY year.

THE STATE VARIABLE
------------------
W_cav - humidity ratio of the cavity air, kg water per kg DRY air.

W is used rather than RH because W is conserved under temperature change: heat
a sealed parcel and its RH collapses while its W does not move at all. RH is a
ratio against a moving denominator; W is an inventory. You cannot write a
conservation law in RH. (See test_dew_point_is_independent_of_dry_bulb.)

WHAT DRIVES IT
--------------
Two processes, applied each hour:

  1. AIR EXCHANGE - vent air replaces cavity air, carrying its own W with it.
     Pulls W_cav toward W_supply.
  2. CONDENSATION / EVAPORATION - the cavity air cannot hold more water than
     saturation at the COLD surface temperature. Excess leaves the air as
     liquid on the glass. When conditions ease, that liquid evaporates back.

Doc ID: ANLY-002 R1.0, Chunk 3
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from engine.cavity import (
    cavity_air_temperature,
    cavity_dry_air_mass,
    cloud_opacity,
    exterior_film_coefficient,
    radiative_surface_drop,
    solar_surface_boost,
    t_from_f,
    vent_cold_surface_rise,
)
from engine.geometry import CavityGeometry
from engine.psychro import (
    atmospheric_pressure_pa,
    dew_point_from_w,
    rh_from_t_w,
    w_from_t_rh,
    w_saturated,
)

# ---------------------------------------------------------------------------
# ACH presets
# ---------------------------------------------------------------------------

#: Named air-change rates mapped to vent hardware, air changes per hour.
#:
#: ESTIMATES, NOT MEASUREMENTS. ANLY-002 S5.3 identifies ACH as "THE unknown -
#: bound it, don't guess it" and never fixes numeric values. These are
#: physically reasonable order-of-magnitude anchors chosen to span the design
#: space; they have NOT been measured on an INOVUES assembly. Any UI showing
#: them must label them as unvalidated estimates. Replace with tracer-gas or
#: pressure-decay data when available.
ACH_PRESETS: dict[str, float] = {
    "sealed": 0.1,
    "tight_vent": 0.5,
    "moderate": 5.0,
    "open_vent": 20.0,
    "free": 100.0,
}

ACH_PRESET_LABELS: dict[str, str] = {
    "sealed": "Sealed - gasketed perimeter, leakage only",
    "tight_vent": "Tight vent - small weep path, pressure equalisation",
    "moderate": "Moderate - designed baffle",
    "open_vent": "Open vent - deliberate large vent slots",
    "free": "Free - cavity effectively continuous with its vent source",
}

#: Every preset is an estimate. Kept as a constant so the UI cannot forget it.
ACH_PRESETS_ARE_ESTIMATES = True

#: Maximum liquid water the cold surface can hold as a film before it runs
#: off, kg per m2. 0.1 kg/m2 is a 0.1 mm film.
#:
#: ANLY-002 S5 has NO drainage term, and without one the model is unbounded:
#: a free-exchange run accumulated 4.7 kg/m2 - five litres per square metre -
#: standing on vertical glass. That phantom pool then evaporated and
#: re-condensed, inventing 688 spurious condensation hours per year.
#:
#: Real condensate sheets down the glass and leaves through the weep path.
#: Water beyond this film thickness is drained and leaves the system.
#: ESTIMATE, not measured. Like the ACH presets, label it as such in any UI.
MAX_SURFACE_FILM_KG_PER_M2 = 0.1

#: Liquid loading above which condensate is treated as VISIBLE to an
#: occupant looking through the glass. Default 0.0 means "any liquid at
#: all", which introduces NO new assumption - it reports exactly what the
#: model tracks. A non-zero value is an ESTIMATE and must be labelled as
#: one wherever it surfaces: no measurement of the optical threshold for
#: condensate on vertical glass has been made for this project.
#: 1 g/m2 of water is a 1 micron film, so this is 5 microns.
#: ESTIMATE, UNVALIDATED - assumption #5. No optical threshold for condensate
#: on vertical glass has been measured for this project. It is defaulted to a
#: non-zero value because the alternative ("any liquid") counts films of a few
#: NANOMETRES as condensation, which is true thermodynamically and meaningless
#: to an occupant. Sealed cavities peak around 0.2-3 um and vented ones reach
#: the 100 um retained-film cap, so any threshold in the 1-50 um range
#: separates them identically; the exact value is not load-bearing.
VISIBLE_FILM_KG_PER_M2 = 0.005


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class HourResult:
    """One hour of the simulation. 8,760 of these become the Excel raw tab."""

    hour: int
    t_out_c: float
    rh_out: float
    t_room_c: float
    w_room: float
    w_supply: float
    t_cold_c: float
    t_warm_c: float
    t_air_c: float
    w_cav: float
    w_sat_cold: float
    dew_point_cav_c: float
    rh_cav: float
    condensed_kg: float
    evaporated_kg: float
    drained_kg: float
    surface_water_kg: float
    is_condensing: bool
    is_saturated: bool
    vent_rise_k: float = 0.0


@dataclass
class RunSummary:
    """Year totals. This is what goes on the Excel summary page."""

    ach: float
    vent_interior_fraction: float
    hours_total: int
    hours_condensing: int
    hours_saturated: int
    pct_condensing: float
    total_condensed_kg_per_m2: float
    total_drained_kg_per_m2: float
    peak_surface_water_kg_per_m2: float
    mean_cavity_dew_point_c: float
    min_cavity_dew_point_c: float
    max_cavity_dew_point_c: float
    mean_room_dew_point_c: float
    hours_condensing_room_assumption: int
    geometry: CavityGeometry | None = None
    glazing_area_m2: float | None = None
    total_condensed_litres_per_window: float | None = None
    total_drained_litres_per_window: float | None = None
    peak_surface_water_litres_per_window: float | None = None
    hours_water_present: int = 0
    pct_water_present: float = 0.0
    hours_water_visible: int = 0
    pct_water_visible: float = 0.0
    visible_threshold_kg_per_m2: float = 0.0
    mean_vent_rise_k: float = 0.0
    max_vent_rise_k: float = 0.0
    hours: list[HourResult] = field(default_factory=list, repr=False)


# ---------------------------------------------------------------------------
# The air-exchange step
# ---------------------------------------------------------------------------

def exchange_analytic(w_cav: float, w_supply: float, ach: float, dt_hours: float) -> float:
    """Update W after ``dt_hours`` of air exchange at rate ``ach``.

    THE DIFFERENTIAL EQUATION
        dW/dt = ACH . (W_supply - W)

    Vent air arrives at W_supply and cavity air leaves at W, so the driving
    force is the difference between them - and that difference SHRINKS as the
    cavity fills. Its exact solution is exponential decay:

        W(t) = W_supply + (W_0 - W_supply) . exp(-ACH.t)

    WHY NOT THE FORM IN S5.1
    ------------------------
    ANLY-002 S5.1 writes the explicit Euler version:

        W_next = W + ACH.(W_supply - W).dt

    which assumes the driving force stays CONSTANT for the whole hour. That is
    fine when little changes in an hour, and wrong when a lot does. With
    dt = 1 h the step factor is literally the ACH number, so:

        ACH = 0.5  closes half the gap                    fine
        ACH = 1    lands exactly on W_supply              borderline
        ACH = 5    overshoots by 4x, in the wrong direction
        ACH = 50   diverges; W goes negative, then explodes

    Since ACH is a user-set input here and the whole point is sweeping it
    across orders of magnitude, Euler would fail precisely where we care most,
    and would fail SILENTLY - returning plausible-looking wrong numbers rather
    than an error.

    exp(-ACH.dt) is bounded in (0, 1] for any non-negative ACH, so this form
    cannot overshoot at any input. It is also the EXACT solution rather than a
    better approximation, so it is more accurate as well as safer.
    See test_analytic_matches_euler_at_low_ach.
    """
    if ach < 0:
        raise ValueError(f"ach cannot be negative, got {ach}")
    if dt_hours <= 0:
        raise ValueError(f"dt_hours must be positive, got {dt_hours}")
    if ach == 0.0:
        return w_cav
    return w_supply + (w_cav - w_supply) * math.exp(-ach * dt_hours)


def exchange_euler(w_cav: float, w_supply: float, ach: float, dt_hours: float) -> float:
    """The ANLY-002 S5.1 explicit form, kept ONLY so tests can demonstrate
    where it breaks. Never used by the solver."""
    return w_cav + ach * (w_supply - w_cav) * dt_hours


# ---------------------------------------------------------------------------
# The condensation / evaporation step
# ---------------------------------------------------------------------------

def clamp_to_saturation(
    w_cav: float,
    w_sat_cold: float,
    m_cav: float,
    surface_water_kg: float,
) -> tuple[float, float, float, float]:
    """Apply the saturation limit at the cold surface.

    Returns ``(w_cav, condensed_kg, evaporated_kg, surface_water_kg)``.

    CONDENSATION
        Air cannot hold more water than saturation at the coldest surface it
        touches. Excess deposits as liquid:

            condensed = (W_cav - W_sat) . m_cav        kg per m2

    EVAPORATION
        The reverse is equally real and is what lets the cavity DRY. If liquid
        is present and the air is below saturation, water returns to the air
        until either saturation is reached or the liquid is used up.

        This treats evaporation as instantaneous up to the saturation limit,
        which is an UPPER BOUND on drying rate - real evaporation is
        diffusion-limited. So reported surface water is a lower bound and
        reported drying is optimistic. Documented rather than hidden.
    """
    if m_cav <= 0:
        raise ValueError(f"m_cav must be positive, got {m_cav}")
    if surface_water_kg < 0:
        raise ValueError(f"surface_water_kg cannot be negative, got {surface_water_kg}")

    condensed = 0.0
    evaporated = 0.0

    if w_cav > w_sat_cold:
        condensed = (w_cav - w_sat_cold) * m_cav
        w_cav = w_sat_cold
        surface_water_kg += condensed
    elif surface_water_kg > 0.0:
        capacity_kg = (w_sat_cold - w_cav) * m_cav
        evaporated = min(capacity_kg, surface_water_kg)
        w_cav += evaporated / m_cav
        surface_water_kg -= evaporated

    return w_cav, condensed, evaporated, surface_water_kg


def step_hour(
    w_cav: float,
    w_supply: float,
    w_sat_cold: float,
    ach: float,
    m_cav: float,
    surface_water_kg: float,
    dt_hours: float = 1.0,
) -> tuple[float, float, float, float]:
    """Advance one hour with exchange and phase change solved TOGETHER.

    Returns ``(w_cav, condensed_kg, evaporated_kg, surface_water_kg)``.

    WHY NOT JUST ALTERNATE THE TWO STEPS
    ------------------------------------
    Applying exchange and then condensation in sequence (operator splitting)
    undercounts badly, because in reality moisture arrives and condenses
    simultaneously and continuously. Each split slice only condenses what
    arrived in that slice. Measured against the true rate:

        6 sub-steps    67.8% of the correct condensation
        24             90.3%
        96             97.4%
        1536           99.8%

    The error decays as 1/n, so no practical sub-step count fixes it. Solving
    the hour in closed form does.

    THE CLOSED FORM
    ---------------
    When supply air is wetter than the cold surface can hold, the cavity air
    reaches saturation and then PINS there - every further gram arriving is a
    gram deposited. Condensation runs at a constant rate:

        rate = ACH . m_cav . (W_supply - W_sat)      kg/m2 per hour

    If the cavity starts below saturation there is a lead-in first. Setting
    the exchange solution equal to W_sat and solving for time:

        W_supply + (W_0 - W_supply).exp(-ACH.t*) = W_sat
        t* = -ln[ (W_supply - W_sat) / (W_supply - W_0) ] / ACH

    Nothing condenses before t*; the steady rate applies for the remaining
    (dt - t*). If t* exceeds the hour, saturation is never reached and the
    hour is pure exchange.

    Drying is the mirror image. With liquid present and supply air drier than
    saturation, the air pins at W_sat while evaporation feeds it, and the
    outgoing air carries water away at ACH.m_cav.(W_sat - W_supply) - plus a
    one-off (W_sat - W_0).m_cav to fill the air's initial headroom. Both are
    capped by the liquid actually available.

    Evaporation is still treated as fast enough to hold saturation, which is
    an UPPER bound on drying: real evaporation is diffusion-limited. Reported
    surface water is therefore a lower bound.
    """
    if m_cav <= 0:
        raise ValueError(f"m_cav must be positive, got {m_cav}")
    if ach < 0:
        raise ValueError(f"ach cannot be negative, got {ach}")
    if dt_hours <= 0:
        raise ValueError(f"dt_hours must be positive, got {dt_hours}")
    if surface_water_kg < 0:
        raise ValueError(f"surface_water_kg cannot be negative, got {surface_water_kg}")

    condensed = 0.0
    evaporated = 0.0

    # Any supersaturation carried in - e.g. the surface just got colder -
    # deposits immediately.
    if w_cav > w_sat_cold:
        excess = (w_cav - w_sat_cold) * m_cav
        condensed += excess
        surface_water_kg += excess
        w_cav = w_sat_cold

    if ach == 0.0:
        # Sealed. No supply, so only the trapped inventory can move.
        if surface_water_kg > 0.0 and w_cav < w_sat_cold:
            capacity = (w_sat_cold - w_cav) * m_cav
            e = min(capacity, surface_water_kg)
            w_cav += e / m_cav
            surface_water_kg -= e
            evaporated += e
        return w_cav, condensed, evaporated, surface_water_kg

    if w_supply > w_sat_cold:
        # Wetting regime: supply is wetter than the surface can hold.
        if w_cav >= w_sat_cold:
            t_star = 0.0
        else:
            ratio = (w_supply - w_sat_cold) / (w_supply - w_cav)
            t_star = -math.log(ratio) / ach

        if t_star >= dt_hours:
            w_cav = exchange_analytic(w_cav, w_supply, ach, dt_hours)
        else:
            c = ach * m_cav * (w_supply - w_sat_cold) * (dt_hours - t_star)
            condensed += c
            surface_water_kg += c
            w_cav = w_sat_cold
    else:
        # Drying or neutral regime.
        if surface_water_kg > 0.0:
            fill = max(0.0, w_sat_cold - w_cav) * m_cav
            flush = ach * m_cav * (w_sat_cold - w_supply) * dt_hours
            capacity = fill + flush
            e = min(capacity, surface_water_kg)
            evaporated += e
            surface_water_kg -= e
            if e >= capacity:
                w_cav = w_sat_cold
            else:
                w_cav = min(
                    w_sat_cold,
                    exchange_analytic(w_cav, w_supply, ach, dt_hours) + e / m_cav,
                )
        else:
            w_cav = exchange_analytic(w_cav, w_supply, ach, dt_hours)

    return w_cav, condensed, evaporated, surface_water_kg


def drain_excess(
    surface_water_kg: float,
    max_film_kg: float = MAX_SURFACE_FILM_KG_PER_M2,
) -> tuple[float, float]:
    """Run off liquid beyond what the glass can hold. Returns
    ``(retained_kg, drained_kg)``.

    Drained water leaves the cavity through the weep path and is gone - it
    cannot evaporate back. Without this, condensate accumulates without limit
    and the resulting phantom pool drives spurious re-evaporation cycles.
    """
    if max_film_kg <= 0:
        raise ValueError(f"max_film_kg must be positive, got {max_film_kg}")
    if surface_water_kg <= max_film_kg:
        return surface_water_kg, 0.0
    return max_film_kg, surface_water_kg - max_film_kg


# ---------------------------------------------------------------------------
# The hourly loop
# ---------------------------------------------------------------------------

def run_year(
    t_out_c: list[float],
    rh_out: list[float],
    f_cold: float,
    f_warm: float,
    ach: float,
    t_room_c: float = 21.0,
    rh_room: float = 0.35,
    vent_interior_fraction: float = 1.0,
    geometry: CavityGeometry | None = None,
    gap_m: float = 0.0153,
    elevation_m: float = 0.0,
    max_film_kg: float = MAX_SURFACE_FILM_KG_PER_M2,
    visible_film_kg_per_m2: float = VISIBLE_FILM_KG_PER_M2,
    poa_w_m2: list[float] | None = None,
    absorptance: float = 0.0,
    wind_m_s: list[float] | None = None,
    sky_radiation: bool = False,
    cloud_type: list[float] | None = None,
    substeps: int = 1,
    spinup_passes: int = 1,
    keep_hours: bool = True,
    u_assembly: float | None = None,
) -> RunSummary:
    """Run the moisture balance across a full TMY year.

    Parameters
    ----------
    t_out_c, rh_out
        Hourly outdoor dry-bulb (degC) and relative humidity (fraction).
        Typically 8,760 values each from NSRDB TMY.
    f_cold, f_warm
        Temperature factors for the two cavity surfaces. See engine.cavity.
    ach
        Air changes per hour. Free input; see ACH_PRESETS for anchors.
    t_room_c, rh_room
        Interior conditions, held constant.
    vent_interior_fraction
        1.0 = cavity vents entirely to the room (the current design
        assumption). 0.0 = vents entirely to outdoor air. Values between
        model a mixed or leaky path.

        This matters enormously and in a non-obvious direction. In winter,
        outdoor air is COLD but very DRY in absolute terms - far drier than
        room air. So exterior venting can dry a cavity that interior venting
        would wet. Whether that is desirable is exactly what this tool is for.
    substeps
        Sub-divisions per hour. Exchange and condensation happen
        simultaneously in reality but are applied in sequence here (operator
        splitting). Sub-stepping shrinks that error. 6 gives 10-minute steps.
    spinup_passes
        Extra passes over the year, discarded, so results do not depend on the
        arbitrary starting humidity.
    u_assembly
        Assembly U-factor, W/m2K. When given, vented room air is allowed to
        WARM the cold surface (engine.cavity.vent_cold_surface_rise), so the
        pane temperature becomes a function of ACH. Default None reproduces
        the fixed-pane model exactly, which keeps the validation anchors
        live. The app passes it; tests that pin historic numbers do not.
    """
    n = len(t_out_c)
    if n == 0:
        raise ValueError("Weather series is empty")
    if len(rh_out) != n:
        raise ValueError(f"t_out_c has {n} values but rh_out has {len(rh_out)}")
    if not 0.0 <= vent_interior_fraction <= 1.0:
        raise ValueError(
            f"vent_interior_fraction must be in [0, 1], got {vent_interior_fraction}"
        )
    if substeps < 1:
        raise ValueError(f"substeps must be at least 1, got {substeps}")
    if ach < 0:
        raise ValueError(f"ach cannot be negative, got {ach}")

    # Geometry, when supplied, is the authority on cavity depth. Window width
    # and height do NOT change the per-m2 answer at fixed ACH (they cancel);
    # they set the per-window totals. See engine/geometry.py.
    if geometry is not None:
        gap_m = geometry.offset_m

    p_atm = atmospheric_pressure_pa(elevation_m)
    w_room = w_from_t_rh(t_room_c, rh_room, p_atm_pa=p_atm)
    dt = 1.0 / substeps

    # Start at room humidity, then spin up so the answer is independent of it.
    w_cav = w_room
    surface_water = 0.0

    for pass_index in range(spinup_passes + 1):
        recording = pass_index == spinup_passes
        hours: list[HourResult] = []
        total_condensed = 0.0
        peak_water = 0.0
        hours_condensing = 0
        hours_water_present = 0
        hours_water_visible = 0
        hours_condensing_room = 0
        hours_saturated = 0
        total_drained = 0.0
        dew_points: list[float] = []
        vent_rise_sum = 0.0
        vent_rise_max = 0.0

        for i in range(n):
            t_o = t_out_c[i]
            rh_o = min(max(rh_out[i], 0.0), 1.0)

            # Surface temperatures this hour. f is boundary-condition
            # independent (ANLY-002 S2.1), so the same f applies all year.
            t_cold = t_from_f(f_cold, t_out_c=t_o, t_in_c=t_room_c)
            t_warm = t_from_f(f_warm, t_out_c=t_o, t_in_c=t_room_c)

            # Surface energy balance on the outboard pane. Both terms default
            # OFF, so absorptance 0 with sky_radiation False reproduces the
            # air-only model exactly - that is how the 676-hour anchor is kept
            # live while this is developed. Applied to the COLD surface only:
            # for a single-pane retrofit that pane is the absorber, and its
            # cavity face is the surface condensation forms on.
            if poa_w_m2 is not None or sky_radiation:
                wind = wind_m_s[i] if wind_m_s is not None else 2.0
                h_out = exterior_film_coefficient(wind)
                if poa_w_m2 is not None and absorptance > 0.0:
                    t_cold += solar_surface_boost(poa_w_m2[i], absorptance, h_out)
                if sky_radiation:
                    op = cloud_opacity(cloud_type[i]) if cloud_type is not None else 0.0
                    t_cold += radiative_surface_drop(t_cold, t_o, h_out, opacity=op)

            # Vented room air warms the pane. Off unless U is known, because
            # the conductances need an absolute scale that f alone lacks.
            vent_rise = 0.0
            if u_assembly is not None and ach > 0.0:
                vent_rise = vent_cold_surface_rise(
                    t_cold, t_room_c, f_cold, u_assembly, ach, gap_m=gap_m
                )
                t_cold += vent_rise
            t_air = cavity_air_temperature(
                t_cold_c=t_cold,
                t_warm_c=t_warm,
                t_room_c=t_room_c,
                ach=ach,
                gap_m=gap_m,
            )

            w_out = w_from_t_rh(t_o, rh_o, p_atm_pa=p_atm) if rh_o > 0 else 0.0
            w_supply = (
                vent_interior_fraction * w_room
                + (1.0 - vent_interior_fraction) * w_out
            )

            # Saturation ceiling is set by the COLDEST surface, not the air.
            w_sat_cold = w_saturated(t_cold, p_atm_pa=p_atm)
            m_cav = cavity_dry_air_mass(t_air, gap_m=gap_m, w=w_cav, p_atm_pa=p_atm)

            hour_condensed = 0.0
            hour_evaporated = 0.0
            hour_drained = 0.0

            for _ in range(substeps):
                w_cav, cond, evap, surface_water = step_hour(
                    w_cav, w_supply, w_sat_cold, ach, m_cav, surface_water, dt
                )
                hour_condensed += cond
                hour_evaporated += evap
                surface_water, drained = drain_excess(surface_water, max_film_kg)
                hour_drained += drained

            if not recording:
                continue

            total_condensed += hour_condensed
            total_drained += hour_drained
            vent_rise_sum += vent_rise
            vent_rise_max = max(vent_rise_max, vent_rise)
            peak_water = max(peak_water, surface_water)
            condensing = hour_condensed > 0.0
            if condensing:
                hours_condensing += 1

            # Liquid PRESENT, not liquid DEPOSITING. Water laid down at 3am
            # is still on the glass at 9am when someone looks at it, so this
            # count is always >= hours_condensing. It is the occupant-facing
            # number; condensed mass remains the engineering one.
            if surface_water > 0.0:
                hours_water_present += 1
            if surface_water > visible_film_kg_per_m2:
                hours_water_visible += 1

            # The assumption we are replacing, scored side by side.
            # The assumption we are replacing, scored on the same ceiling so
            # the comparison is like for like.
            if w_room > w_sat_cold:
                hours_condensing_room += 1
            if w_cav >= w_sat_cold - 1e-15:
                hours_saturated += 1

            dp_cav = dew_point_from_w(w_cav, p_atm_pa=p_atm) if w_cav > 0 else -100.0
            dew_points.append(dp_cav)

            if keep_hours:
                hours.append(
                    HourResult(
                        hour=i,
                        t_out_c=t_o,
                        rh_out=rh_o,
                        t_room_c=t_room_c,
                        w_room=w_room,
                        w_supply=w_supply,
                        t_cold_c=t_cold,
                        t_warm_c=t_warm,
                        t_air_c=t_air,
                        w_cav=w_cav,
                        w_sat_cold=w_sat_cold,
                        dew_point_cav_c=dp_cav,
                        rh_cav=rh_from_t_w(t_air, w_cav, p_atm_pa=p_atm),
                        condensed_kg=hour_condensed,
                        evaporated_kg=hour_evaporated,
                        drained_kg=hour_drained,
                        surface_water_kg=surface_water,
                        is_condensing=condensing,
                        is_saturated=w_cav >= w_sat_cold - 1e-15,
                        vent_rise_k=vent_rise,
                    )
                )

    return RunSummary(
        ach=ach,
        vent_interior_fraction=vent_interior_fraction,
        hours_total=n,
        hours_condensing=hours_condensing,
        hours_saturated=hours_saturated,
        pct_condensing=100.0 * hours_condensing / n,
        total_condensed_kg_per_m2=total_condensed,
        total_drained_kg_per_m2=total_drained,
        peak_surface_water_kg_per_m2=peak_water,
        mean_cavity_dew_point_c=sum(dew_points) / len(dew_points),
        min_cavity_dew_point_c=min(dew_points),
        max_cavity_dew_point_c=max(dew_points),
        mean_room_dew_point_c=dew_point_from_w(w_room, p_atm_pa=p_atm),
        hours_condensing_room_assumption=hours_condensing_room,
        geometry=geometry,
        glazing_area_m2=geometry.glazing_area_m2 if geometry else None,
        total_condensed_litres_per_window=(
            geometry.litres(total_condensed) if geometry else None
        ),
        total_drained_litres_per_window=(
            geometry.litres(total_drained) if geometry else None
        ),
        peak_surface_water_litres_per_window=(
            geometry.litres(peak_water) if geometry else None
        ),
        hours_water_present=hours_water_present,
        pct_water_present=100.0 * hours_water_present / n,
        hours_water_visible=hours_water_visible,
        pct_water_visible=100.0 * hours_water_visible / n,
        visible_threshold_kg_per_m2=visible_film_kg_per_m2,
        mean_vent_rise_k=vent_rise_sum / n,
        max_vent_rise_k=vent_rise_max,
        hours=hours,
    )


def sweep_ach(
    t_out_c: list[float],
    rh_out: list[float],
    f_cold: float,
    f_warm: float,
    ach_values: list[float],
    **kwargs,
) -> list[RunSummary]:
    """Run the model at several air-change rates.

    The bracket of ANLY-002 S5.5, generalised: any list of ACH values rather
    than three fixed cases. ``keep_hours`` is forced off - a sweep of ten
    values would otherwise hold 87,600 hour records in memory.
    """
    kwargs.pop("keep_hours", None)
    return [
        run_year(t_out_c, rh_out, f_cold, f_warm, ach=a, keep_hours=False, **kwargs)
        for a in ach_values
    ]
