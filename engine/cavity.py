"""
Cavity thermal model for the INOVUES vented SWR cavity.

WHAT THIS MODULE ANSWERS
------------------------
The condensation app knows one number: the temperature of the COLD surface
(inner face of the existing exterior pane). The moisture balance in Chunk 3
needs three more:

  1. T_warm  - the cavity-facing surface of the new interior IGU
  2. T_air   - the temperature of the air IN the cavity, which sets both the
               cavity's dry-air mass and its relative humidity
  3. m_cav   - kg of DRY air in the cavity, per square metre of glazing

DESIGN RULE
-----------
SI throughout. See engine/psychro.py.

Doc ID: ANLY-002 R1.0, Chunk 2
"""

from __future__ import annotations

from engine.psychro import dry_air_density

# ---------------------------------------------------------------------------
# NFRC 100 winter boundary conditions - ANLY-002 S2.1
# ---------------------------------------------------------------------------

#: NFRC 100 winter exterior air temperature, degC.
NFRC_T_OUT_C = -18.0

#: NFRC 100 winter interior air temperature, degC.
NFRC_T_IN_C = 21.0

#: Specific heat of dry air at constant pressure, J/(kg.K).
CP_AIR = 1006.0

#: Default convective film coefficient on a cavity-facing glass surface,
#: W/(m2.K). Narrow vertical air cavity, natural convection.
#:
#: Both cavity surfaces are glass bounding the same gap with the same
#: geometry, so their convective coefficients are near-identical. Since
#: T_air depends only on the RATIO of the two, the absolute value largely
#: cancels - which is why a single default is defensible here and why
#: overriding it barely moves the answer. See test_cavity.py.
H_CAVITY_DEFAULT = 3.0


# ---------------------------------------------------------------------------
# f-value <-> temperature
# ---------------------------------------------------------------------------

def t_from_f(
    f: float,
    t_out_c: float = NFRC_T_OUT_C,
    t_in_c: float = NFRC_T_IN_C,
) -> float:
    """Surface temperature from a temperature factor. ANLY-002 S2.1.

        f = (T_surf - T_out) / (T_in - T_out)   =>   T_surf = T_out + f.dT

    Because the steady-state conduction operator is linear, f depends only on
    geometry and materials - so the same f computed at NFRC conditions applies
    at every hour of the TMY file. That is what makes the quasi-steady-state
    approach legitimate.
    """
    return t_out_c + f * (t_in_c - t_out_c)


def f_from_t(
    t_surf_c: float,
    t_out_c: float = NFRC_T_OUT_C,
    t_in_c: float = NFRC_T_IN_C,
) -> float:
    """Temperature factor from a measured or simulated surface temperature."""
    if t_in_c == t_out_c:
        raise ValueError("t_in_c and t_out_c must differ to define f")
    return (t_surf_c - t_out_c) / (t_in_c - t_out_c)


def f_warm_estimate(
    f_cold: float,
    r_cavity: float,
    u_assembly: float,
) -> float:
    """Estimate the f-value of the cavity-facing surface of the interior IGU.

    PREFER A MEASURED VALUE. WINDOW reports every surface temperature in the
    stack (ANLY-002 S3.1), so Arnold can read T_warm directly and you should
    pass it via ``f_from_t`` instead. This function exists for screening when
    that number is not to hand.

    DERIVATION
    ----------
    Treat the assembly as a series resistance chain from outdoor to indoor.
    In a series chain the temperature drop across each element is proportional
    to its resistance, so the f-value at any node is just the fraction of
    total resistance accumulated up to that node:

        f_node = R_outboard_of_node / R_total

    The cold surface and the warm surface are separated by exactly one
    element - the cavity itself. So:

        f_warm = (R_outboard + R_cav) / R_total
               = f_cold + R_cav / R_total
               = f_cold + R_cav . U_assembly          since U = 1/R_total

    Every term is a number INOVUES already has: f_cold from WINDOW, U from the
    assembly spec sheet, R_cav from the gap width.

    WORKED EXAMPLE - 277 Park SWR-VIG
        f_cold = 0.013, R_cav = 0.17 m2K/W, U = 0.70 W/m2K
        f_warm = 0.013 + 0.17 x 0.70 = 0.132
        T_warm = -18 + 39 x 0.132 = -12.8 degC

    Note how little the cavity warms even though the VIG behind it is highly
    insulating - the VIG strands everything outboard of it near outdoor
    temperature (ANLY-002 S2.2).
    """
    if not 0.0 <= f_cold <= 1.0:
        raise ValueError(f"f_cold must be in [0, 1], got {f_cold}")
    if r_cavity <= 0:
        raise ValueError(f"r_cavity must be positive, got {r_cavity}")
    if u_assembly <= 0:
        raise ValueError(f"u_assembly must be positive, got {u_assembly}")

    f_warm = f_cold + r_cavity * u_assembly

    if f_warm > 1.0:
        raise ValueError(
            f"Estimated f_warm = {f_warm:.3f} exceeds 1.0, which is physically "
            "impossible (the cavity surface cannot be warmer than the room). "
            "Check that r_cavity and u_assembly belong to the same assembly."
        )
    return f_warm


# ---------------------------------------------------------------------------
# Cavity air temperature
# ---------------------------------------------------------------------------

def cavity_air_temperature(
    t_cold_c: float,
    t_warm_c: float,
    t_room_c: float | None = None,
    ach: float = 0.0,
    gap_m: float = 0.0153,
    h_cold: float = H_CAVITY_DEFAULT,
    h_warm: float = H_CAVITY_DEFAULT,
) -> float:
    """Well-mixed cavity air temperature, degC, per m2 of glazing.

    STEADY-STATE ENERGY BALANCE
    ---------------------------
    Three ways heat reaches the cavity air, summing to zero at steady state:

        h_warm.A.(T_warm - T_air)          from the interior IGU surface
      + h_cold.A.(T_cold - T_air)          from the existing pane surface
      + m_dot.cp.(T_room - T_air)          carried in by vented room air
      = 0

    Rearranged, it is a weighted average of the three driving temperatures,
    each weighted by its conductance to the air:

        T_air = (h_w.T_warm + h_c.T_cold + m_dot.cp.T_room)
                / (h_w + h_c + m_dot.cp)

    With ``ach = 0`` this collapses exactly to the film-weighted mean of the
    two bounding surfaces that ANLY-002 S5.3 proposes.

    IS THE VENT TERM WORTH CARRYING?
    --------------------------------
    Barely, and that is a useful result rather than a wasted term. At
    ACH = 5, gap = 15.3 mm:

        m_dot = 5 x 0.0153 x 1.3 / 3600 = 2.8e-5 kg/s
        m_dot.cp = 2.8e-5 x 1006        = 0.028 W/K
        h_w + h_c                        = 6.0   W/K

    The vent contributes under half a percent of the conductance. Air is a
    poor carrier of heat but the ONLY carrier of moisture, which is precisely
    why ventilation dominates the moisture balance while being negligible
    here. Carrying the term costs nothing and lets us prove that claim in a
    test rather than assert it.

    Parameters
    ----------
    t_cold_c, t_warm_c
        Bounding surface temperatures, degC.
    t_room_c
        Interior air temperature, degC. Required when ``ach > 0``.
    ach
        Cavity air changes per hour with room air. THE unknown parameter -
        bracketed rather than guessed in Chunk 4.
    gap_m
        Cavity width, m. 15.3 mm for the 277 Park SWR-VIG stack.
    h_cold, h_warm
        Convective film coefficients, W/(m2.K).
    """
    if h_cold <= 0 or h_warm <= 0:
        raise ValueError("Film coefficients must be positive")
    if ach < 0:
        raise ValueError(f"ach cannot be negative, got {ach}")
    if gap_m <= 0:
        raise ValueError(f"gap_m must be positive, got {gap_m}")

    numerator = h_warm * t_warm_c + h_cold * t_cold_c
    denominator = h_warm + h_cold

    if ach > 0:
        if t_room_c is None:
            raise ValueError("t_room_c is required when ach > 0")
        # First pass without the vent, purely to get a density temperature.
        t_seed = numerator / denominator
        rho = dry_air_density(t_seed)
        m_dot = ach * gap_m * rho / 3600.0  # kg dry air per second, per m2
        c_vent = m_dot * CP_AIR             # W/K
        numerator += c_vent * t_room_c
        denominator += c_vent

    return numerator / denominator


def cavity_dry_air_mass(
    t_air_c: float,
    gap_m: float = 0.0153,
    w: float = 0.0,
    p_atm_pa: float | None = None,
) -> float:
    """Mass of DRY air in the cavity, kg per m2 of glazing.

    The humidity ratio W is defined per kg of dry air, so the moisture balance
    must divide by dry-air mass. Using moist-air mass instead would bias every
    condensation figure by roughly 1%.

        m_cav = V_cav x rho_dry = (gap x 1 m2) x rho_dry

    For the 277 Park 15.3 mm cavity at 0 degC this is about 0.0153 x 1.29
    = 0.0197 kg of dry air per square metre - a very small reservoir, which is
    why it responds quickly to both ventilation and condensation.
    """
    if gap_m <= 0:
        raise ValueError(f"gap_m must be positive, got {gap_m}")
    kwargs = {} if p_atm_pa is None else {"p_atm_pa": p_atm_pa}
    return gap_m * dry_air_density(t_air_c, w=w, **kwargs)


def cavity_volume(gap_m: float = 0.0153) -> float:
    """Cavity volume, m3 per m2 of glazing. ANLY-002 S5.3."""
    if gap_m <= 0:
        raise ValueError(f"gap_m must be positive, got {gap_m}")
    return gap_m
