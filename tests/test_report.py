"""
AUDIT SUITE - Excel export
===========================

  [SPEC]     the workbook contract Stephan specified: summary page plus raw
             data on a separate tab
  [DERIVED]  arithmetic worked longhand in the docstring
  [PHYSICS]  a bound the answer must satisfy
  [API]      the HTTP contract

The central claim tested here: the Summary sheet contains FORMULAS over the
raw tab, not numbers copied out of Python. A reviewer must be able to confirm
the headline figures by recalculating the workbook.

Doc ID: ANLY-002 R1.0, Chunk 4
"""

import json
import pathlib
import re
from io import BytesIO
from unittest.mock import patch

import pytest
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

import app as flask_app
from engine.cavity import f_warm_estimate
from engine.geometry import CavityGeometry
from engine.moisture import ACH_PRESETS, run_year, sweep_ach
from engine.psychro import c_to_f, f_to_c
from engine.report import HOURLY_COLUMNS, build_workbook, workbook_bytes
from engine.weather import Location, WeatherYear

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

F_COLD = 0.013
F_WARM = f_warm_estimate(0.013, 0.17, 0.70)


@pytest.fixture(scope="module")
def real_year():
    path = FIXTURES / "nsrdb_277park_tmy.json"
    if not path.exists():
        pytest.skip("NSRDB fixture not present")
    fx = json.loads(path.read_text())
    return [f_to_c(t) for t in fx["t_out_f"]], [0.7] * 8760


@pytest.fixture(scope="module")
def geometry():
    return CavityGeometry.from_inches(60, 96, 0.6024, "Type A")


@pytest.fixture(scope="module")
def summary(real_year, geometry):
    t_out, rh_out = real_year
    return run_year(
        t_out, rh_out, F_COLD, F_WARM, ach=5.0,
        t_room_c=f_to_c(70.0), rh_room=0.35, geometry=geometry, keep_hours=True,
    )


@pytest.fixture(scope="module")
def meta(geometry):
    return {
        "matched_address": "277 PARK AVE, NEW YORK, NY, 10022",
        "weather_source": "NSRDB PSM3 TMY",
        "station_id": "149629",
        "elevation_m": 20.0,
        "f_cold": F_COLD,
        "f_warm": round(F_WARM, 4),
        "f_warm_source": "estimated as f_cold + r_cavity x u_assembly",
        "offset_in": 0.6024,
        "width_in": 60.0,
        "height_in": 96.0,
        "glazing_area_m2": geometry.glazing_area_m2,
        "t_in_f": 70.0,
        "rh_in_pct": 35.0,
        "ach": 5.0,
        "vent_label": "1.00 (interior)",
    }


@pytest.fixture(scope="module")
def workbook(summary, meta, real_year, geometry):
    t_out, rh_out = real_year
    sweep = sweep_ach(
        t_out, rh_out, f_cold=F_COLD, f_warm=F_WARM,
        ach_values=list(ACH_PRESETS.values()),
        t_room_c=f_to_c(70.0), rh_room=0.35, geometry=geometry,
    )
    return build_workbook(summary, meta, sweep, list(ACH_PRESETS))


def _summary_labels(ws) -> dict:
    return {
        row[0].value: row[1].value
        for row in ws.iter_rows(min_col=1, max_col=2)
        if isinstance(row[0].value, str)
    }


# ===========================================================================
# 1. STRUCTURE
# ===========================================================================


def test_workbook_has_the_specified_tabs(workbook):
    """[SPEC] Summary page, with raw data on its own tab."""
    assert workbook.sheetnames == ["Summary", "Hourly Data", "ACH Sweep"]


def test_summary_is_the_active_sheet(workbook):
    """[SPEC] The reader must land on the summary, not 8,760 rows of data."""
    assert workbook.active.title == "Summary"


def test_raw_tab_holds_every_hour(workbook):
    """[SPEC] All 8,760 hours, one per row, plus a header."""
    ws = workbook["Hourly Data"]
    assert ws.max_row == 8761
    assert ws.max_column == len(HOURLY_COLUMNS)


def test_raw_tab_is_filterable_and_frozen(workbook):
    """[SPEC] A 8,760-row tab is unusable without a frozen header and filters."""
    ws = workbook["Hourly Data"]
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref is not None


def test_raw_tab_headers_match_the_column_spec(workbook):
    ws = workbook["Hourly Data"]
    headers = [ws.cell(row=1, column=i + 1).value for i in range(len(HOURLY_COLUMNS))]
    assert headers == [h for h, _, _ in HOURLY_COLUMNS]


def test_every_sheet_uses_a_professional_font(workbook):
    """[SPEC] Arial throughout."""
    for name in workbook.sheetnames:
        ws = workbook[name]
        for row in ws.iter_rows(min_row=1, max_row=6):
            for cell in row:
                if cell.value is not None and cell.font and cell.font.name:
                    assert cell.font.name == "Arial", f"{name}!{cell.coordinate}"


# ===========================================================================
# 2. THE CENTRAL CLAIM - the summary computes, it does not copy
# ===========================================================================


def test_summary_results_are_formulas_not_copied_values(workbook):
    """[SPEC] The point of the whole design.

    Every headline result must be a live formula over the raw tab. If these
    were Python-computed constants the workbook could not be audited and would
    not recalculate when the raw data is filtered or edited.
    """
    labels = _summary_labels(workbook["Summary"])
    for label in (
        "Hours simulated",
        "Hours with condensation",
        "Condensed water (per m2 of glazing)",
        "Drained out the weep path (per m2)",
        "Peak standing water",
        "Lowest cavity dew point",
        "Mean cavity dew point",
        "Coldest cavity surface",
    ):
        value = labels[label]
        assert isinstance(value, str) and value.startswith("="), (
            f"{label!r} is a hardcoded {value!r}, not a formula"
        )


def test_result_formulas_reference_the_raw_tab(workbook):
    """[SPEC] A formula that references nothing is just a constant in disguise."""
    labels = _summary_labels(workbook["Summary"])
    assert "'Hourly Data'!" in labels["Condensed water (per m2 of glazing)"]
    assert "'Hourly Data'!" in labels["Hours with condensation"]


def test_per_window_formulas_reference_the_per_m2_cells(workbook):
    """[DERIVED] litres = g/m2 x area / 1000, computed in the sheet so a reader
    can see where the number came from."""
    labels = _summary_labels(workbook["Summary"])
    formula = labels["Condensed water (whole window)"]
    assert formula.startswith("=B") and "/1000" in formula


def test_glazing_area_is_stored_at_full_precision(workbook, geometry):
    """[DERIVED] The per-window formulas multiply by this cell, so storing a
    rounded value would propagate the rounding into the results. Round the
    DISPLAY, never the stored value."""
    labels = _summary_labels(workbook["Summary"])
    assert labels["Glazing area"] == pytest.approx(
        geometry.glazing_area_m2, rel=1e-12
    )


def test_summary_formulas_evaluate_to_the_python_results(workbook, summary, tmp_path):
    """[SPEC] THE test that matters: recalculate the workbook with LibreOffice
    and confirm every formula lands on the value the engine computed.

    A green recalculation proves formulas EVALUATE. This proves they are RIGHT
    - an off-by-one range would recalculate cleanly and still be wrong.

    Skipped when LibreOffice is unavailable, since the assertion cannot be made
    without it.
    """
    import shutil
    import subprocess

    if shutil.which("soffice") is None:
        pytest.skip("LibreOffice not available")

    path = tmp_path / "report.xlsx"
    workbook.save(path)
    script = pathlib.Path("/mnt/skills/public/xlsx/scripts/recalc.py")
    if not script.exists():
        pytest.skip("recalc script not available")

    result = subprocess.run(
        ["python3", str(script), str(path), "180"],
        capture_output=True, text=True, timeout=300,
    )
    report = json.loads(result.stdout)
    assert report.get("status") == "success", report
    assert report.get("total_errors") == 0, report

    labels = _summary_labels(load_workbook(path, data_only=True)["Summary"])
    assert labels["Hours simulated"] == summary.hours_total
    assert labels["Hours with condensation"] == summary.hours_condensing
    assert labels["Condensed water (per m2 of glazing)"] == pytest.approx(
        summary.total_condensed_kg_per_m2 * 1000, rel=1e-9
    )
    assert labels["Drained out the weep path (per m2)"] == pytest.approx(
        summary.total_drained_kg_per_m2 * 1000, rel=1e-9
    )
    assert labels["Peak standing water"] == pytest.approx(
        summary.peak_surface_water_kg_per_m2 * 1000, rel=1e-9
    )
    assert labels["Condensed water (whole window)"] == pytest.approx(
        summary.total_condensed_litres_per_window, rel=1e-9
    )
    assert labels["Mean cavity dew point"] == pytest.approx(
        c_to_f(summary.mean_cavity_dew_point_c), rel=1e-9
    )


# ===========================================================================
# 2b. FORMULA VERIFICATION WITHOUT LIBREOFFICE
# ===========================================================================
#
# The LibreOffice recalculation above is the gold standard, but it skips
# wherever soffice is absent - which includes most laptops and most CI. A
# verification that silently skips is not a verification.
#
# So we also evaluate the formulas directly: parse each one, read the cells it
# references straight out of the workbook, compute the result in Python, and
# compare against the engine. Same claim, no external dependency, always runs.

_RANGE = re.compile(r"'([^']+)'!([A-Z]+)(\d+):([A-Z]+)(\d+)")


def _range_values(wb, formula: str):
    """Pull the values a formula's range refers to, out of the workbook."""
    match = _RANGE.search(formula)
    if match is None:
        raise AssertionError(f"No sheet range found in {formula!r}")
    sheet, col1, row1, col2, row2 = match.groups()
    ws = wb[sheet]
    assert col1 == col2, f"Multi-column range in {formula!r}"
    return [
        ws.cell(row=r, column=column_index_from_string(col1)).value
        for r in range(int(row1), int(row2) + 1)
    ]


def _evaluate(wb, formula: str, labels: dict):
    """Evaluate the small subset of Excel this workbook uses."""
    body = formula.lstrip("=")

    if body.startswith("SUMIFS("):
        values = _range_values(wb, body)
        # Every SUMIFS here sums a column against itself with criteria 1,
        # i.e. counts the flagged hours.
        return sum(v for v in values if v == 1)
    for name, func in (
        ("SUM(", sum),
        ("MAX(", max),
        ("MIN(", min),
        ("AVERAGE(", lambda v: sum(v) / len(v)),
        ("COUNT(", len),
    ):
        if body.startswith(name):
            return func(_range_values(wb, body))

    # Arithmetic over Summary cells, e.g. =B29*B14/1000
    if re.fullmatch(r"[B\d*/+\-.() ]+", body):
        ws = wb["Summary"]

        def cell(m):
            value = ws[m.group(0)].value
            if isinstance(value, str) and value.startswith("="):
                value = _evaluate(wb, value, labels)
            return repr(float(value))

        return eval(re.sub(r"B\d+", cell, body))  # noqa: S307 - fixed grammar

    raise AssertionError(f"Unsupported formula {formula!r}")


def test_summary_formulas_are_correct_without_libreoffice(workbook, summary):
    """[SPEC] The formulas compute the RIGHT thing, verified with no external
    dependency so this can never silently skip.

    An off-by-one range or a reference to the wrong column would recalculate
    cleanly in Excel and still be wrong. This catches that.
    """
    wb = workbook
    labels = _summary_labels(wb["Summary"])

    expected = {
        "Hours simulated": summary.hours_total,
        "Hours with condensation": summary.hours_condensing,
        "Condensed water (per m2 of glazing)":
            summary.total_condensed_kg_per_m2 * 1000,
        "Drained out the weep path (per m2)":
            summary.total_drained_kg_per_m2 * 1000,
        "Peak standing water": summary.peak_surface_water_kg_per_m2 * 1000,
        "Lowest cavity dew point": c_to_f(summary.min_cavity_dew_point_c),
        "Mean cavity dew point": c_to_f(summary.mean_cavity_dew_point_c),
        "Condensed water (whole window)":
            summary.total_condensed_litres_per_window,
        "Drained (whole window)": summary.total_drained_litres_per_window,
    }

    for label, want in expected.items():
        got = _evaluate(wb, labels[label], labels)
        assert got == pytest.approx(want, rel=1e-9), (
            f"{label}: formula {labels[label]!r} gives {got}, engine says {want}"
        )


def test_formula_ranges_cover_every_data_row_and_no_header(workbook):
    """[SPEC] The classic silent error: a range starting at row 1 would pull
    the text header into a SUM, and one ending at 8760 would drop the last
    hour of the year. Both recalculate without error."""
    labels = _summary_labels(workbook["Summary"])
    for label in (
        "Condensed water (per m2 of glazing)",
        "Drained out the weep path (per m2)",
        "Peak standing water",
        "Mean cavity dew point",
    ):
        match = _RANGE.search(labels[label])
        assert match, label
        _, _, row1, _, row2 = match.groups()
        assert int(row1) == 2, f"{label} includes the header row"
        assert int(row2) == 8761, f"{label} stops at row {row2}, not 8761"


# ===========================================================================
# 3. RAW DATA FIDELITY
# ===========================================================================


def test_raw_rows_match_the_model_output(workbook, summary):
    """[DERIVED] Spot-check three rows end to end, in Fahrenheit and grams."""
    ws = workbook["Hourly Data"]
    for index in (0, 4000, 8759):
        row = index + 2
        h = summary.hours[index]
        assert ws.cell(row=row, column=1).value == h.hour
        assert ws.cell(row=row, column=5).value == pytest.approx(c_to_f(h.t_out_c))
        assert ws.cell(row=row, column=7).value == pytest.approx(c_to_f(h.t_cold_c))
        assert ws.cell(row=row, column=14).value == pytest.approx(
            h.condensed_kg * 1000
        )
        assert ws.cell(row=row, column=18).value == (1 if h.is_condensing else 0)


def test_raw_dates_start_on_january_first(workbook):
    """[DERIVED] TMY hour 0 is 1 January, 00:00."""
    ws = workbook["Hourly Data"]
    # Row 2 is hour 0, so row 25 is hour 23 and row 26 is hour 24.
    assert (ws["B2"].value, ws["C2"].value, ws["D2"].value) == (1, 1, 0)
    assert (ws["B25"].value, ws["C25"].value, ws["D25"].value) == (1, 1, 23)
    assert (ws["B26"].value, ws["C26"].value, ws["D26"].value) == (1, 2, 0)
    assert (ws["B8761"].value, ws["C8761"].value, ws["D8761"].value) == (12, 31, 23)


def test_raw_standing_water_never_exceeds_the_drain_cap(workbook):
    """[PHYSICS] The drainage cap must hold in the exported data too."""
    ws = workbook["Hourly Data"]
    for row in ws.iter_rows(min_row=2, min_col=17, max_col=17):
        assert row[0].value <= 100.0 + 1e-9


# ===========================================================================
# 4. SWEEP TAB
# ===========================================================================


def test_sweep_tab_has_one_row_per_preset(workbook):
    ws = workbook["ACH Sweep"]
    names = [ws.cell(row=5 + i, column=1).value for i in range(len(ACH_PRESETS))]
    assert names == list(ACH_PRESETS)


def test_sweep_mass_rises_with_ach(workbook):
    """[PHYSICS] Interior venting supplies the water that condenses, so mass
    must rise monotonically with air change rate."""
    ws = workbook["ACH Sweep"]
    masses = [ws.cell(row=5 + i, column=7).value for i in range(len(ACH_PRESETS))]
    assert all(b > a for a, b in zip(masses, masses[1:]))


def test_sweep_carries_the_hours_versus_mass_warning(workbook):
    """[SPEC] Wet HOURS is not monotonic in ACH while mass is. Anyone reading
    the hours column without that warning will conclude a sealed cavity is
    worse than a freely vented one, which is backwards."""
    ws = workbook["ACH Sweep"]
    text = " ".join(
        str(c.value) for col in ws.iter_cols() for c in col if c.value
    ).lower()
    assert "mislead" in text or "mass is the meaningful metric" in text


# ===========================================================================
# 5. ASSUMPTIONS MUST TRAVEL WITH THE NUMBERS
# ===========================================================================


def test_summary_names_every_unvalidated_assumption(workbook):
    """[SPEC] This workbook goes to customers. The four estimates doing real
    work must be visible in it, not buried in a docstring."""
    ws = workbook["Summary"]
    text = " ".join(
        str(c.value) for col in ws.iter_cols() for c in col if c.value
    ).lower()
    assert "unvalidated assumptions" in text
    assert "estimate" in text
    assert "air change rate" in text
    assert "retained condensate film" in text
    assert "upper" in text and "drying" in text  # the evaporation caveat


def test_assumption_cells_are_highlighted(workbook):
    """[SPEC] Yellow fill marks an unvalidated number. It must be impossible to
    read the workbook and miss them."""
    ws = workbook["Summary"]
    filled = [
        c for col in ws.iter_cols() for c in col
        if c.fill and c.fill.fgColor and c.fill.fgColor.rgb
        and "FFF2CC" in str(c.fill.fgColor.rgb)
    ]
    assert len(filled) >= 6


def test_summary_explains_the_difference_from_the_old_model(workbook):
    """[SPEC] Our hours exceed the room-dew-point criterion, and the workbook
    must say why rather than leave a reader to assume it is an error."""
    ws = workbook["Summary"]
    text = " ".join(
        str(c.value) for col in ws.iter_cols() for c in col if c.value
    ).lower()
    assert "room-dew-point" in text or "room dew point" in text
    assert "liquid inventory" in text


# ===========================================================================
# 6. GUARDS AND THE HTTP ENDPOINT
# ===========================================================================


def test_build_requires_hourly_records(meta):
    """[SPEC] Without keep_hours the raw tab would be empty and every summary
    formula would read zero. Fail loudly instead."""
    from engine.moisture import run_year as run

    s = run([0.0] * 48, [0.5] * 48, F_COLD, F_WARM, ach=5.0, keep_hours=False)
    with pytest.raises(ValueError, match="keep_hours"):
        build_workbook(s, meta)


def test_workbook_bytes_is_a_valid_xlsx(summary, meta):
    """[API] What the endpoint streams must open as a workbook."""
    data = workbook_bytes(summary, meta)
    assert data[:2] == b"PK"  # xlsx is a zip
    assert load_workbook(BytesIO(data)).sheetnames[0] == "Summary"


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture
def stubbed_weather(real_year):
    t_out, rh_out = real_year
    year = WeatherYear(
        t_out_c=t_out, rh_out=rh_out, elevation_m=20.0,
        grid_lat=40.77, grid_lon=-73.98, time_zone=-5.0, station_id="149629",
    )
    location = Location(
        query="277 Park Ave",
        matched_address="277 PARK AVE, NEW YORK, NY, 10022",
        lat=40.756824, lon=-73.974155,
    )
    with patch("app.get_weather_for_address", return_value=(location, year, True)):
        yield


def test_export_returns_a_downloadable_workbook(client, stubbed_weather):
    """[API] Correct MIME type and a filename the browser will save."""
    resp = client.get(
        "/export.xlsx?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70"
        "&ach=moderate&width_in=60&height_in=96"
    )
    assert resp.status_code == 200
    assert resp.mimetype == flask_app.XLSX_MIME
    assert "attachment" in resp.headers["Content-Disposition"]
    assert ".xlsx" in resp.headers["Content-Disposition"]
    wb = load_workbook(BytesIO(resp.data))
    assert wb.sheetnames == ["Summary", "Hourly Data", "ACH Sweep"]
    assert wb["Hourly Data"].max_row == 8761


def test_export_validates_inputs_like_calculate(client):
    """[API] Both endpoints share one parser, so validation cannot drift."""
    for query in ("f_cold=0.3", "address=x&f_cold=2.0", "address=x&f_cold=0.3&ach=xyz"):
        resp = client.get(f"/export.xlsx?{query}")
        assert resp.status_code == 400, query


def test_export_works_without_geometry(client, stubbed_weather):
    """[API] Per-window totals need dimensions; the rest of the workbook does
    not, and must still build."""
    resp = client.get("/export.xlsx?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70")
    assert resp.status_code == 200
    labels = _summary_labels(load_workbook(BytesIO(resp.data))["Summary"])
    assert "Condensed water (per m2 of glazing)" in labels
    assert "Condensed water (whole window)" not in labels
