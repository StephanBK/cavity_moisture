"""Occupant-facing metric: hours with liquid PRESENT, not hours depositing.

`hours_condensing` counts hours where water is being laid down.
`hours_water_present` counts hours where water is on the glass. Water deposited
overnight is still there in the morning, so the second is always >= the first.
That gap is the thing the old room-dew-point model could not represent at all,
because it carries no liquid inventory.

Sources: [PHYSICS] liquid persists until it evaporates or drains
          [ANLY-002] real-weather run, 277 Park, GOES TMY v4.0.0
          [SPEC]    thresholds are estimates and must be labelled

Doc ID: ANLY-002 R1.1
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engine.geometry import CavityGeometry
from engine.moisture import VISIBLE_FILM_KG_PER_M2, run_year, sweep_ach

FIXTURE = Path(__file__).parent / "fixtures" / "nsrdb_277park_tmy.json"


@pytest.fixture(scope="module")
def real_year():
    d = json.loads(FIXTURE.read_text())
    t_c = [(f - 32.0) * 5.0 / 9.0 for f in d["t_out_f"]]
    return t_c


@pytest.fixture(scope="module")
def rh_flat(real_year):
    # The fixture stores dry-bulb only; a flat RH is enough for the ordering
    # and monotonicity claims below, which do not depend on the humidity year.
    return [0.6] * len(real_year)


T_ROOM_C = (70.0 - 32.0) * 5.0 / 9.0


def _run(t_c, rh, ach, f_cold=0.300, **kw):
    return run_year(
        t_c, rh, f_cold=f_cold, f_warm=f_cold + 0.17 * 1.7, ach=ach,
        t_room_c=T_ROOM_C, rh_room=0.35, vent_interior_fraction=1.0, **kw
    )


# ---------------------------------------------------------------------------
# The defining relationship  [PHYSICS]
# ---------------------------------------------------------------------------

def test_water_present_is_never_less_than_condensing(real_year, rh_flat):
    for ach in (0.1, 0.5, 5.0, 20.0, 100.0):
        r = _run(real_year, rh_flat, ach)
        assert r.hours_water_present >= r.hours_condensing, f"failed at ACH {ach}"


def test_water_present_exceeds_condensing_on_real_weather(real_year, rh_flat):
    """The gap is the retained film. If these were equal the inventory would
    not be doing anything, and the model would reduce to the old one."""
    r = _run(real_year, rh_flat, 5.0)
    assert r.hours_water_present > r.hours_condensing


def test_water_present_never_exceeds_the_year(real_year, rh_flat):
    r = _run(real_year, rh_flat, 5.0)
    assert 0 <= r.hours_water_present <= r.hours_total


def test_pct_matches_the_count(real_year, rh_flat):
    r = _run(real_year, rh_flat, 5.0)
    assert r.pct_water_present == pytest.approx(
        100.0 * r.hours_water_present / r.hours_total
    )


def test_a_dry_year_reports_no_water(rh_flat):
    """Warm outdoors, no cold surface, nothing deposits and nothing lingers."""
    t_c = [30.0] * 8760
    r = _run(t_c, [0.3] * 8760, 5.0)
    assert r.hours_condensing == 0
    assert r.hours_water_present == 0
    assert r.pct_water_present == 0.0


# ---------------------------------------------------------------------------
# The threshold is an estimate and defaults to "no assumption"  [SPEC]
# ---------------------------------------------------------------------------

def test_default_threshold_introduces_no_assumption():
    """Default 0.0 means 'any liquid', which is exactly what is modelled."""
    assert VISIBLE_FILM_KG_PER_M2 == 0.0


def test_summary_reports_the_threshold_it_used(real_year, rh_flat):
    r = _run(real_year, rh_flat, 5.0, visible_film_kg_per_m2=0.002)
    assert r.visible_threshold_kg_per_m2 == 0.002


def test_a_higher_threshold_never_increases_the_count(real_year, rh_flat):
    loose = _run(real_year, rh_flat, 5.0, visible_film_kg_per_m2=0.0)
    strict = _run(real_year, rh_flat, 5.0, visible_film_kg_per_m2=0.01)
    assert strict.hours_water_present <= loose.hours_water_present


# ---------------------------------------------------------------------------
# The metric trap, one layer deeper  [ANLY-002]
# ---------------------------------------------------------------------------

def test_water_present_is_not_monotonic_in_ach_but_mass_is(real_year, rh_flat):
    """Wet hours barely move across a 1000x ACH range while condensed mass
    swings by orders of magnitude. Anyone ranking vent designs by hours will
    reach the wrong answer; mass is the engineering metric. Sec 5.2."""
    rows = sweep_ach(
        real_year, rh_flat, f_cold=0.300, f_warm=0.300 + 0.17 * 1.7,
        ach_values=[0.1, 0.5, 5.0, 20.0, 100.0],
        t_room_c=T_ROOM_C, rh_room=0.35, vent_interior_fraction=1.0,
    )
    hours = [r.hours_water_present for r in rows]
    mass = [r.total_condensed_kg_per_m2 for r in rows]
    assert mass == sorted(mass), "condensed mass must rise with ACH"
    spread = (max(hours) - min(hours)) / max(hours)
    assert spread < 0.35, f"hours varied {spread:.0%}; expected a flat metric"


# ---------------------------------------------------------------------------
# It reaches the caller  [API]
# ---------------------------------------------------------------------------

def test_calculate_reports_the_metric(monkeypatch):
    os.environ.setdefault("MAPBOX_TOKEN", "pk.test")
    os.environ.setdefault("NREL_API_KEY", "test")
    import app as flask_app
    from engine.weather import Location, WeatherYear

    d = json.loads(FIXTURE.read_text())
    t_c = [(f - 32.0) * 5.0 / 9.0 for f in d["t_out_f"]]
    year = WeatherYear(t_out_c=t_c, rh_out=[0.6] * len(t_c), elevation_m=20.0)
    loc = Location("q", "277 PARK AVE", 40.7568, -73.9742)
    monkeypatch.setattr(
        flask_app, "get_weather_for_address", lambda *a, **k: (loc, year, False)
    )
    c = flask_app.app.test_client()
    s = c.get("/calculate?address=x&f_cold=0.300&t_in=70&rh_in=35&ach=5"
              "&vent_interior=1").get_json()["summary"]
    assert "hours_water_present" in s
    assert "pct_water_present" in s
    assert s["hours_water_present"] >= s["hours_condensing"]


def test_geometry_does_not_change_the_hour_count(real_year, rh_flat):
    """Window size scales totals, never rates. Sec 3c."""
    small = _run(real_year, rh_flat, 5.0,
                 geometry=CavityGeometry(0.5, 0.5, 0.0153))
    large = _run(real_year, rh_flat, 5.0,
                 geometry=CavityGeometry(3.0, 3.0, 0.0153))
    assert small.hours_water_present == large.hours_water_present


# ---------------------------------------------------------------------------
# Excel: the share-of-year regression  [NUMERICS]
# ---------------------------------------------------------------------------

def test_share_of_year_rows_reference_hours_simulated():
    """Regression. total_hours_row was hardcoded to 8 while 'Hours simulated'
    was written to row 9, so every share-of-year cell divided by Elevation (0)
    and IFERROR reported 0%. The row is now captured where it is written."""
    from openpyxl import load_workbook
    import io as _io
    from engine.report import workbook_bytes

    t_c = [-5.0] * 8760
    r = run_year(t_c, [0.8] * 8760, f_cold=0.300, f_warm=0.589, ach=5.0,
                 t_room_c=T_ROOM_C, rh_room=0.35, vent_interior_fraction=1.0)
    ws = load_workbook(_io.BytesIO(
        workbook_bytes(r, {"address": "test"}, [r], ["moderate"]))
    )["Summary"]

    rows = {str(ws.cell(row=i, column=1).value): i
            for i in range(1, 60) if ws.cell(row=i, column=1).value}
    hours_row = rows["Hours simulated"]
    shares = [k for k in rows if k.startswith("Share of year")]
    assert shares, "no share-of-year rows found"
    for label in shares:
        formula = ws.cell(row=rows[label], column=2).value
        assert f"B{hours_row}" in formula, (
            f"{label!r} references the wrong row: {formula}"
        )
