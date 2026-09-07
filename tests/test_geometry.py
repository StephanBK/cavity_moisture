"""
AUDIT SUITE - window and cavity geometry
=========================================

  [ANLY-002] the handover document, section cited
  [DERIVED]  arithmetic worked longhand in the docstring
  [PHYSICS]  a bound or scaling law the answer must satisfy

Doc ID: ANLY-002 R1.0, Chunk 3c
"""

import math

import pytest

from engine.cavity import f_warm_estimate
from engine.geometry import (
    M_PER_INCH,
    REFERENCE_277_PARK,
    CavityGeometry,
)
from engine.moisture import run_year

F_COLD_IG = 0.300
F_WARM_IG = f_warm_estimate(0.300, 0.17, 1.70)


def _synthetic_year(mean_c=12.5, swing_c=13.0, n=8760):
    t, rh = [], []
    for h in range(n):
        doy = h / 24.0
        t.append(
            mean_c
            - swing_c * math.cos(2 * math.pi * (doy - 15) / 365)
            - 4.0 * math.cos(2 * math.pi * ((h % 24) - 15) / 24)
        )
        rh.append(0.65 + 0.15 * math.sin(2 * math.pi * (h % 24) / 24))
    return t, rh


# ===========================================================================
# 1. DIMENSIONS AND DERIVED QUANTITIES
# ===========================================================================


def test_area_volume_perimeter_longhand():
    """[DERIVED] 1.5 m x 2.5 m glazing, 15.3 mm offset:

        area      = 1.5 x 2.5              = 3.75 m2
        perimeter = 2 x (1.5 + 2.5)        = 8.0  m
        volume    = 3.75 x 0.0153          = 0.057375 m3 = 57.375 L
        aspect    = 2.5 / 1.5              = 1.6667
    """
    g = CavityGeometry(width_m=1.5, height_m=2.5, offset_m=0.0153)
    assert g.glazing_area_m2 == pytest.approx(3.75)
    assert g.perimeter_m == pytest.approx(8.0)
    assert g.cavity_volume_m3 == pytest.approx(0.057375)
    assert g.cavity_volume_litres == pytest.approx(57.375)
    assert g.aspect_ratio == pytest.approx(1.66667, abs=1e-4)


def test_inch_conversion_is_exact():
    """[DERIVED] 1 inch is 0.0254 m by definition, not by measurement.
    A 60 in x 96 in window is 40.00 ft2 exactly."""
    g = CavityGeometry.from_inches(60.0, 96.0, 0.6024)
    assert g.width_m == pytest.approx(60 * M_PER_INCH)
    assert g.glazing_area_m2 * 10.7639 == pytest.approx(40.0, abs=0.01)


def test_inch_roundtrip():
    """[DERIVED] Imperial in, imperial out, no drift."""
    g = CavityGeometry.from_inches(48.0, 72.0, 0.75)
    assert g.width_in == pytest.approx(48.0, rel=1e-12)
    assert g.height_in == pytest.approx(72.0, rel=1e-12)
    assert g.offset_in == pytest.approx(0.75, rel=1e-12)


def test_reference_module_carries_the_recorded_offset():
    """[ANLY-002 S2.3] The 15.3 mm gap is on record; the width and height are
    NOT and are flagged provisional in the label."""
    assert REFERENCE_277_PARK.offset_m == pytest.approx(0.0153)
    assert "provisional" in REFERENCE_277_PARK.label


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width_m": 0.0, "height_m": 2.0},
        {"width_m": 1.0, "height_m": -2.0},
        {"width_m": 1.0, "height_m": 2.0, "offset_m": 0.0},
    ],
)
def test_dimension_validation(kwargs):
    with pytest.raises(ValueError):
        CavityGeometry(**kwargs)


def test_offset_in_wrong_units_is_caught():
    """[PHYSICS] Passing 15.3 (millimetres) instead of 0.0153 (metres) would
    silently model a half-metre-deep cavity. Must fail loudly."""
    with pytest.raises(ValueError, match="glazing cavity, not a room"):
        CavityGeometry(width_m=1.5, height_m=2.5, offset_m=15.3)


# ===========================================================================
# 2. THE CANCELLATION RESULT
# ===========================================================================


def test_window_size_does_not_change_the_per_square_metre_answer():
    """[PHYSICS] The result that decides what geometry is actually for.

    Per m2 of glazing, cavity air mass is d.rho and supplied moisture is
    ACH.d.rho.W - area has cancelled from both. So at fixed ACH a small window
    and a large one behave identically per unit area, differing only in TOTAL
    water.

    This is why offset is a design lever and width/height are a reporting
    convenience. It also marks the boundary of the single-node assumption:
    the moment ACH is derived from vent hardware, size stops cancelling.
    """
    t, rh = _synthetic_year()
    results = []
    for w, h in ((0.6, 0.9), (1.5, 2.5), (3.0, 4.0)):
        g = CavityGeometry(width_m=w, height_m=h, offset_m=0.0153)
        results.append(
            run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, geometry=g, keep_hours=False)
        )
    for r in results[1:]:
        assert r.total_condensed_kg_per_m2 == pytest.approx(
            results[0].total_condensed_kg_per_m2, rel=1e-12
        )
        assert r.hours_condensing == results[0].hours_condensing


def test_per_window_totals_scale_with_area():
    """[PHYSICS] The corollary: a window of twice the area collects twice the
    water, even though the per-m2 figure is unchanged."""
    t, rh = _synthetic_year()
    small = CavityGeometry(width_m=1.5, height_m=2.0, offset_m=0.0153)
    big = CavityGeometry(width_m=3.0, height_m=2.0, offset_m=0.0153)
    r_small = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, geometry=small, keep_hours=False)
    r_big = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, geometry=big, keep_hours=False)
    assert r_big.total_condensed_litres_per_window == pytest.approx(
        2.0 * r_small.total_condensed_litres_per_window, rel=1e-9
    )


def test_offset_does_change_the_answer():
    """[PHYSICS] Offset is the geometric dimension that survives the
    cancellation. A deeper cavity holds more air, so at fixed ACH it is
    flushed with proportionally more moisture-bearing supply air.
    """
    t, rh = _synthetic_year()
    masses = []
    for offset in (0.006, 0.0153, 0.050):
        g = CavityGeometry(width_m=1.5, height_m=2.5, offset_m=offset)
        masses.append(
            run_year(
                t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, geometry=g, keep_hours=False
            ).total_condensed_kg_per_m2
        )
    assert all(b > a for a, b in zip(masses, masses[1:]))


def test_geometry_overrides_gap_m():
    """[DERIVED] When both are supplied, geometry is the authority on cavity
    depth. Two ways of saying the same thing must not silently disagree."""
    t, rh = _synthetic_year(n=720)
    g = CavityGeometry(width_m=1.5, height_m=2.5, offset_m=0.030)
    with_geom = run_year(
        t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, geometry=g, gap_m=0.0153, keep_hours=False
    )
    direct = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, gap_m=0.030, keep_hours=False)
    assert with_geom.total_condensed_kg_per_m2 == pytest.approx(
        direct.total_condensed_kg_per_m2, rel=1e-12
    )


# ===========================================================================
# 3. VENT GEOMETRY - the factor that will drive a future ACH model
# ===========================================================================


def test_vent_geometry_ratio_longhand():
    """[DERIVED] P/(A.d) for a 1 m square window with a 20 mm cavity:

        P = 4.0 m,  A = 1.0 m2,  d = 0.02 m
        P/(A.d) = 4.0 / 0.02 = 200 per m2

    For a square of side L this is 4/(L.d), so it falls as either the window
    or the cavity gets bigger.
    """
    g = CavityGeometry(width_m=1.0, height_m=1.0, offset_m=0.020)
    assert g.vent_geometry_ratio == pytest.approx(200.0)


def test_bigger_windows_vent_more_slowly_for_the_same_slots():
    """[PHYSICS] Vent area lives on the perimeter; the volume to be flushed
    scales with area times offset. So P/(A.d) falls as the window grows -
    a large window is harder to ventilate than a small one."""
    small = CavityGeometry(width_m=0.5, height_m=0.5, offset_m=0.0153)
    large = CavityGeometry(width_m=3.0, height_m=3.0, offset_m=0.0153)
    assert large.vent_geometry_ratio < small.vent_geometry_ratio


def test_deeper_cavities_vent_more_slowly_too():
    """[PHYSICS] The second half of the same scaling."""
    shallow = CavityGeometry(width_m=1.5, height_m=2.5, offset_m=0.006)
    deep = CavityGeometry(width_m=1.5, height_m=2.5, offset_m=0.050)
    assert deep.vent_geometry_ratio < shallow.vent_geometry_ratio


# ===========================================================================
# 4. REPORTING
# ===========================================================================


def test_litres_conversion_longhand():
    """[DERIVED] 0.32 kg/m2 on a 3.75 m2 window:

        total = 0.32 x 3.75 = 1.20 kg
        1 kg of water is 1 litre, so 1.20 L.
    """
    g = CavityGeometry(width_m=1.5, height_m=2.5, offset_m=0.0153)
    assert g.per_window(0.32) == pytest.approx(1.20)
    assert g.litres(0.32) == pytest.approx(1.20)


def test_describe_is_complete_and_json_safe():
    """[DERIVED] The API response and Excel summary read this dictionary."""
    g = CavityGeometry.from_inches(60, 96, 0.6024, label="Type A")
    d = g.describe()
    expected = {
        "label", "width_m", "height_m", "offset_m",
        "width_in", "height_in", "offset_in",
        "glazing_area_m2", "glazing_area_ft2", "perimeter_m",
        "cavity_volume_litres", "aspect_ratio", "vent_geometry_ratio",
    }
    assert set(d) == expected
    assert all(isinstance(v, (int, float, str)) for v in d.values())
    assert d["label"] == "Type A"


def test_summary_omits_per_window_fields_without_geometry():
    """[DERIVED] Per-window numbers require dimensions. Reporting a fabricated
    default would be worse than reporting nothing."""
    t, rh = _synthetic_year(n=240)
    r = run_year(t, rh, F_COLD_IG, F_WARM_IG, ach=5.0, keep_hours=False)
    assert r.geometry is None
    assert r.glazing_area_m2 is None
    assert r.total_condensed_litres_per_window is None


def test_geometry_is_immutable():
    """[DERIVED] Dimensions are validated at construction. Allowing mutation
    afterwards would let an unvalidated offset slip past __post_init__."""
    g = CavityGeometry(width_m=1.5, height_m=2.5)
    with pytest.raises(Exception):
        g.offset_m = 99.0
