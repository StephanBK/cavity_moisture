"""
Psychrometric core for the INOVUES cavity moisture model.

DESIGN RULE
-----------
Everything in this module is SI. Kelvin, Pascals, kg/kg, metres.
Fahrenheit exists ONLY in the two conversion helpers at the top and
is never used in a physical correlation. Every ASHRAE constant below
is published in SI; converting at the boundary rather than inside the
physics is what keeps the conversions exact.

SOURCE
------
ASHRAE Handbook - Fundamentals, Chapter 1 "Psychrometrics".
Equation numbers below refer to the 2017 edition.

Doc ID: ANLY-002 R1.0, Chunk 1
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Physical constants
# ---------------------------------------------------------------------------

#: Standard atmospheric pressure at sea level, Pa.
P_ATM_SEA_LEVEL = 101325.0

#: Ratio of the molecular mass of water vapour to that of dry air.
#: ASHRAE Ch.1 Eq. 20. Dimensionless. This is the 0.621945 that turns a
#: vapour *pressure* into a humidity *ratio*.
M_RATIO = 0.621945

#: Absolute zero in degrees Celsius.
ZERO_C_IN_K = 273.15

# ASHRAE Ch.1 Eq. 5 - saturation pressure over ICE, valid -100..0 degC.
_ICE = (
    -5.6745359e3,   # C1
    6.3925247,      # C2
    -9.677843e-3,   # C3
    6.2215701e-7,   # C4
    2.0747825e-9,   # C5
    -9.484024e-13,  # C6
    4.1635019,      # C7
)

# ASHRAE Ch.1 Eq. 6 - saturation pressure over liquid WATER, valid 0..200 degC.
_WATER = (
    -5.8002206e3,   # C8
    1.3914993,      # C9
    -4.8640239e-2,  # C10
    4.1764768e-5,   # C11
    -1.4452093e-8,  # C12
    6.5459673,      # C13
)


# ---------------------------------------------------------------------------
# Unit conversion - the ONLY place Fahrenheit is allowed
# ---------------------------------------------------------------------------

#: 1 Btu/(hr*ft^2*degF) = 5.678263... W/(m^2*K). Exact by definition of the
#: international Btu, the foot and the Fahrenheit degree.
W_M2K_PER_BTU_HR_FT2_F = 5.678263341113646


def u_ip_to_si(u_ip: float) -> float:
    """U-factor, Btu/(hr*ft^2*degF) -> W/(m^2*K)."""
    return u_ip * W_M2K_PER_BTU_HR_FT2_F


def u_si_to_ip(u_si: float) -> float:
    """U-value, W/(m^2*K) -> Btu/(hr*ft^2*degF)."""
    return u_si / W_M2K_PER_BTU_HR_FT2_F


def r_ip_to_si(r_ip: float) -> float:
    """R-value, hr*ft^2*degF/Btu -> m^2*K/W. R and U convert by the same
    factor in OPPOSITE directions, which is why the dimensionless product
    r_cavity * u_assembly is invariant across the two systems."""
    return r_ip / W_M2K_PER_BTU_HR_FT2_F


def r_si_to_ip(r_si: float) -> float:
    """R-value, m^2*K/W -> hr*ft^2*degF/Btu."""
    return r_si * W_M2K_PER_BTU_HR_FT2_F


def f_to_c(t_f: float) -> float:
    """Fahrenheit -> Celsius. Exact rational form, no rounded 0.5556."""
    return (t_f - 32.0) * 5.0 / 9.0


def c_to_f(t_c: float) -> float:
    """Celsius -> Fahrenheit. Exact inverse of :func:`f_to_c`."""
    return t_c * 9.0 / 5.0 + 32.0


def c_to_k(t_c: float) -> float:
    """Celsius -> Kelvin."""
    return t_c + ZERO_C_IN_K


def k_to_c(t_k: float) -> float:
    """Kelvin -> Celsius."""
    return t_k - ZERO_C_IN_K


def f_to_k(t_f: float) -> float:
    """Fahrenheit -> Kelvin, in one step to avoid a double rounding."""
    return (t_f + 459.67) * 5.0 / 9.0


def delta_f_to_c(dt_f: float) -> float:
    """Convert a temperature *difference*, not a temperature.

    A 10 degF interval is 5.556 degC, not -12.2 degC. Mixing these two up
    is the classic unit bug, so they get separate functions.
    """
    return dt_f * 5.0 / 9.0


# ---------------------------------------------------------------------------
# Atmospheric pressure
# ---------------------------------------------------------------------------

def atmospheric_pressure_pa(elevation_m: float = 0.0) -> float:
    """Standard atmosphere pressure at a given elevation.

    ASHRAE Ch.1 Eq. 3. Matters because the humidity ratio depends on total
    pressure: the same vapour pressure carries more water per kg of dry air
    when the surrounding air is thinner.
    """
    return P_ATM_SEA_LEVEL * (1.0 - 2.25577e-5 * elevation_m) ** 5.2559


# ---------------------------------------------------------------------------
# Saturation vapour pressure
# ---------------------------------------------------------------------------

def p_ws_pa(t_c: float, force_phase: str | None = None) -> float:
    """Saturation vapour pressure of water in air, Pa.

    Below 0 degC the stable condensed phase is ice, and ASHRAE gives a
    *separate* correlation for it (Eq. 5) with a measurably lower saturation
    pressure than supercooled water. This matters here: the cavity-side
    surface at 277 Park runs to -17.5 degC, deep in the ice branch.

    Parameters
    ----------
    t_c
        Temperature, degrees Celsius.
    force_phase
        ``None``  - pick the branch by temperature (physical default).
        ``"water"`` - always use the liquid-water correlation, extrapolating
        below 0 degC. Use this when you need a conventional "dew point"
        rather than a frost point.
        ``"ice"``  - always use the ice correlation.
    """
    if force_phase not in (None, "water", "ice"):
        raise ValueError(f"force_phase must be None, 'water' or 'ice', got {force_phase!r}")

    use_ice = (force_phase == "ice") or (force_phase is None and t_c < 0.0)
    t_k = c_to_k(t_c)

    if t_k <= 0:
        raise ValueError(f"Temperature below absolute zero: {t_c} degC")

    if use_ice:
        c1, c2, c3, c4, c5, c6, c7 = _ICE
        ln_p = (
            c1 / t_k
            + c2
            + c3 * t_k
            + c4 * t_k**2
            + c5 * t_k**3
            + c6 * t_k**4
            + c7 * math.log(t_k)
        )
    else:
        c8, c9, c10, c11, c12, c13 = _WATER
        ln_p = (
            c8 / t_k
            + c9
            + c10 * t_k
            + c11 * t_k**2
            + c12 * t_k**3
            + c13 * math.log(t_k)
        )

    return math.exp(ln_p)


# ---------------------------------------------------------------------------
# Humidity ratio  W  -  the conserved coordinate
# ---------------------------------------------------------------------------

def w_from_p_w(p_w_pa: float, p_atm_pa: float = P_ATM_SEA_LEVEL) -> float:
    """Humidity ratio from vapour partial pressure. ASHRAE Ch.1 Eq. 20."""
    if p_w_pa >= p_atm_pa:
        raise ValueError(
            f"Vapour pressure {p_w_pa:.1f} Pa >= total pressure {p_atm_pa:.1f} Pa"
        )
    return M_RATIO * p_w_pa / (p_atm_pa - p_w_pa)


def p_w_from_w(w: float, p_atm_pa: float = P_ATM_SEA_LEVEL) -> float:
    """Vapour partial pressure from humidity ratio. Exact inverse of the above."""
    if w < 0:
        raise ValueError(f"Humidity ratio cannot be negative: {w}")
    return p_atm_pa * w / (M_RATIO + w)


def w_from_t_rh(
    t_c: float,
    rh: float,
    p_atm_pa: float = P_ATM_SEA_LEVEL,
    force_phase: str | None = None,
) -> float:
    """Humidity ratio from dry-bulb temperature and relative humidity.

    ``rh`` is a fraction in [0, 1], not a percentage. This is the entry point
    the model uses on room air and on TMY outdoor air.
    """
    if not 0.0 <= rh <= 1.0:
        raise ValueError(f"rh must be a fraction in [0, 1], got {rh}")
    p_w = rh * p_ws_pa(t_c, force_phase=force_phase)
    return w_from_p_w(p_w, p_atm_pa)


def w_saturated(
    t_c: float,
    p_atm_pa: float = P_ATM_SEA_LEVEL,
    force_phase: str | None = None,
) -> float:
    """Humidity ratio of saturated air at ``t_c``.

    This is the clamp ceiling in the moisture balance: when cavity W exceeds
    ``w_saturated(T_surface)``, the excess condenses out onto the glass.
    """
    return w_from_t_rh(t_c, 1.0, p_atm_pa, force_phase=force_phase)


def rh_from_t_w(
    t_c: float,
    w: float,
    p_atm_pa: float = P_ATM_SEA_LEVEL,
    force_phase: str | None = None,
) -> float:
    """Relative humidity (fraction) from dry-bulb temperature and W."""
    p_w = p_w_from_w(w, p_atm_pa)
    return p_w / p_ws_pa(t_c, force_phase=force_phase)


# ---------------------------------------------------------------------------
# Dew point  -  solved, not fitted
# ---------------------------------------------------------------------------

_DEWPOINT_LO_C = -100.0
_DEWPOINT_HI_C = 200.0


def dew_point_from_p_w(
    p_w_pa: float,
    force_phase: str | None = "water",
    tol_c: float = 1e-10,
) -> float:
    """Dew-point temperature (degC) at which ``p_w_pa`` becomes saturation.

    ASHRAE publishes polynomial *fits* for this inversion. We instead invert
    the saturation correlation itself by bisection, which is exact to machine
    tolerance and stays valid outside the fit range. p_ws is strictly
    increasing in temperature, so bisection is guaranteed to converge.

    ``force_phase`` defaults to ``"water"`` because "dew point" conventionally
    means the liquid-water value even below freezing. Pass ``None`` to get the
    physical frost point instead.
    """
    if p_w_pa <= 0:
        raise ValueError(f"Vapour pressure must be positive, got {p_w_pa}")

    lo, hi = _DEWPOINT_LO_C, _DEWPOINT_HI_C
    if p_w_pa < p_ws_pa(lo, force_phase=force_phase):
        raise ValueError(f"Vapour pressure {p_w_pa} Pa below solvable range")
    if p_w_pa > p_ws_pa(hi, force_phase=force_phase):
        raise ValueError(f"Vapour pressure {p_w_pa} Pa above solvable range")

    while hi - lo > tol_c:
        mid = 0.5 * (lo + hi)
        if p_ws_pa(mid, force_phase=force_phase) < p_w_pa:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def dew_point_from_w(
    w: float,
    p_atm_pa: float = P_ATM_SEA_LEVEL,
    force_phase: str | None = "water",
) -> float:
    """Dew point (degC) of air with humidity ratio ``w``.

    Note that dew point depends on W and total pressure ONLY - not on the
    air's own temperature. That independence is exactly why the model tracks
    W rather than RH: W and its dew point travel with the air parcel, RH does
    not.
    """
    return dew_point_from_p_w(p_w_from_w(w, p_atm_pa), force_phase=force_phase)


def dew_point_from_t_rh(
    t_c: float,
    rh: float,
    force_phase: str | None = "water",
) -> float:
    """Dew point (degC) from dry-bulb temperature and relative humidity."""
    if not 0.0 <= rh <= 1.0:
        raise ValueError(f"rh must be a fraction in [0, 1], got {rh}")
    if rh == 0.0:
        raise ValueError("Dry air (rh=0) has no dew point")
    p_w = rh * p_ws_pa(t_c, force_phase=force_phase)
    return dew_point_from_p_w(p_w, force_phase=force_phase)


# ---------------------------------------------------------------------------
# Air density  -  needed to turn a cavity volume into a cavity air mass
# ---------------------------------------------------------------------------

#: Specific gas constant for dry air, J/(kg.K).
R_DRY_AIR = 287.042


def dry_air_density(
    t_c: float,
    w: float = 0.0,
    p_atm_pa: float = P_ATM_SEA_LEVEL,
) -> float:
    """Mass of DRY air per cubic metre of moist air, kg/m3.

    Chunk 3 needs ``m_cav`` in kg of dry air, because W is per kg of dry air.
    Using total moist-air mass here would introduce a ~1% bias in the
    moisture balance.
    """
    t_k = c_to_k(t_c)
    p_w = p_w_from_w(w, p_atm_pa)
    p_dry = p_atm_pa - p_w
    return p_dry / (R_DRY_AIR * t_k)


# ---------------------------------------------------------------------------
# Fahrenheit-facing convenience wrappers (UI edge only)
# ---------------------------------------------------------------------------

def dew_point_f_from_t_rh(t_f: float, rh_pct: float) -> float:
    """Dew point in degF from dry-bulb degF and RH in percent.

    Thin wrapper for the UI and for comparison against the existing
    condensation_calc app. No physics lives here.
    """
    return c_to_f(dew_point_from_t_rh(f_to_c(t_f), rh_pct / 100.0))
