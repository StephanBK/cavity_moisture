"""
Excel export for the cavity moisture model.

DESIGN PRINCIPLE
----------------
The Summary sheet does NOT contain numbers copied out of Python. Every result
cell is a FORMULA referencing the Hourly Data sheet, so the workbook
recalculates and can be audited: filter or edit the raw tab and the headline
figures move with it. A reviewer can confirm that "396 g/m2" really is the sum
of the condensation column without taking our word for it.

The raw hourly values themselves are model output and cannot be re-derived in
Excel, so those are written as data. Everything computed FROM them is a formula.

SHEETS
------
  Summary      inputs, location, assumptions, headline results (formulas)
  ACH Sweep    one row per air-change rate, each a separate model run
  Hourly Data  8,760 rows of model output

Doc ID: ANLY-002 R1.0, Chunk 4
"""

from __future__ import annotations

import datetime as _dt
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from engine.moisture import ACH_PRESET_LABELS, MAX_SURFACE_FILM_KG_PER_M2
from engine.psychro import c_to_f

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

FONT = "Arial"

TITLE = Font(name=FONT, size=16, bold=True)
HEADING = Font(name=FONT, size=11, bold=True, color="FFFFFF")
SECTION = Font(name=FONT, size=12, bold=True)
BODY = Font(name=FONT, size=10)
BODY_BOLD = Font(name=FONT, size=10, bold=True)

#: Blue text marks a hardcoded input the user supplied, per the modelling
#: convention that inputs and formulas must be visually distinguishable.
INPUT_FONT = Font(name=FONT, size=10, color="0000FF")
NOTE_FONT = Font(name=FONT, size=9, italic=True, color="666666")

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
#: Yellow marks an unvalidated assumption. These must be impossible to miss.
ASSUMPTION_FILL = PatternFill("solid", fgColor="FFF2CC")
BAND_FILL = PatternFill("solid", fgColor="F2F2F2")

THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

RAW = "Hourly Data"
SWEEP = "ACH Sweep"


def _label(ws, row, text, value=None, *, unit="", font=BODY, fill=None, note=""):
    """Write a label / value / unit triple and return the value's cell."""
    ws.cell(row=row, column=1, value=text).font = BODY_BOLD
    cell = ws.cell(row=row, column=2, value=value)
    cell.font = font
    if fill:
        ws.cell(row=row, column=1).fill = fill
        cell.fill = fill
    if unit:
        u = ws.cell(row=row, column=3, value=unit)
        u.font = BODY
    if note:
        n = ws.cell(row=row, column=4, value=note)
        n.font = NOTE_FONT
    return cell


def _section(ws, row, title):
    cell = ws.cell(row=row, column=1, value=title)
    cell.font = SECTION
    return row + 1


# ---------------------------------------------------------------------------
# Hourly Data sheet
# ---------------------------------------------------------------------------

HOURLY_COLUMNS = [
    ("Hour", "hour", "0"),
    ("Month", "month", "0"),
    ("Day", "day", "0"),
    ("Hour of day", "hour_of_day", "0"),
    ("Outdoor air (F)", "t_out_f", "0.00"),
    ("Outdoor RH (%)", "rh_out_pct", "0.0"),
    ("Cold surface (F)", "t_cold_f", "0.00"),
    ("Warm surface (F)", "t_warm_f", "0.00"),
    ("Cavity air (F)", "t_air_f", "0.00"),
    ("Cavity W (kg/kg)", "w_cav", "0.0000000"),
    ("Saturation W at cold surface (kg/kg)", "w_sat_cold", "0.0000000"),
    ("Cavity dew point (F)", "dew_point_cav_f", "0.00"),
    ("Cavity RH (%)", "rh_cav_pct", "0.0"),
    ("Condensed (g/m2)", "condensed_g", "0.0000"),
    ("Evaporated (g/m2)", "evaporated_g", "0.0000"),
    ("Drained (g/m2)", "drained_g", "0.0000"),
    ("Standing water (g/m2)", "surface_water_g", "0.0000"),
    ("Condensing (1/0)", "is_condensing", "0"),
]

#: Column letters, resolved once so formulas and headers cannot drift apart.
COL = {key: get_column_letter(i + 1) for i, (_, key, _) in enumerate(HOURLY_COLUMNS)}


def _hour_to_date(hour_index: int) -> tuple[int, int, int]:
    """TMY hour index to (month, day, hour of day), non-leap year."""
    base = _dt.datetime(2001, 1, 1) + _dt.timedelta(hours=hour_index)
    return base.month, base.day, base.hour


def _write_hourly(wb, summary) -> int:
    """Write the raw tab. Returns the last data row number."""
    ws = wb.create_sheet(RAW)

    for i, (header, _, _) in enumerate(HOURLY_COLUMNS, start=1):
        cell = ws.cell(row=1, column=i, value=header)
        cell.font = HEADING
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HOURLY_COLUMNS))}1"

    for r, h in enumerate(summary.hours, start=2):
        month, day, hour_of_day = _hour_to_date(h.hour)
        values = [
            h.hour, month, day, hour_of_day,
            c_to_f(h.t_out_c), h.rh_out * 100.0,
            c_to_f(h.t_cold_c), c_to_f(h.t_warm_c), c_to_f(h.t_air_c),
            h.w_cav, h.w_sat_cold,
            c_to_f(h.dew_point_cav_c), h.rh_cav * 100.0,
            h.condensed_kg * 1000.0, h.evaporated_kg * 1000.0,
            h.drained_kg * 1000.0, h.surface_water_kg * 1000.0,
            1 if h.is_condensing else 0,
        ]
        for c, (value, (_, _, fmt)) in enumerate(zip(values, HOURLY_COLUMNS), start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = BODY
            cell.number_format = fmt

    for i, (header, _, _) in enumerate(HOURLY_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(header) + 2, 10), 22)

    return len(summary.hours) + 1


# ---------------------------------------------------------------------------
# ACH Sweep sheet
# ---------------------------------------------------------------------------

SWEEP_HEADERS = [
    "Preset", "Description", "ACH", "Wet hours", "Saturated hours",
    "% of year wet", "Condensed (g/m2/yr)", "Drained (g/m2/yr)",
    "Condensed (L/window/yr)", "Mean cavity dew point (F)",
]


def _write_sweep(wb, sweep_results, preset_names, geometry) -> None:
    ws = wb.create_sheet(SWEEP)

    ws["A1"] = "Air change rate bracket"
    ws["A1"].font = TITLE
    ws["A2"] = (
        "Each row is a separate full-year model run. ACH values are engineering "
        "estimates, not measurements - see Summary."
    )
    ws["A2"].font = NOTE_FONT

    for i, header in enumerate(SWEEP_HEADERS, start=1):
        cell = ws.cell(row=4, column=i, value=header)
        cell.font = HEADING
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")

    for r, (name, s) in enumerate(zip(preset_names, sweep_results), start=5):
        litres = (
            geometry.litres(s.total_condensed_kg_per_m2) if geometry else None
        )
        row = [
            name,
            ACH_PRESET_LABELS.get(name, ""),
            s.ach,
            s.hours_condensing,
            s.hours_saturated,
            s.hours_condensing / s.hours_total,
            s.total_condensed_kg_per_m2 * 1000.0,
            s.total_drained_kg_per_m2 * 1000.0,
            litres,
            c_to_f(s.mean_cavity_dew_point_c),
        ]
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = BODY
            cell.border = BOX
            if r % 2 == 1:
                cell.fill = BAND_FILL
        ws.cell(row=r, column=6).number_format = "0.0%"
        for c in (7, 8):
            ws.cell(row=r, column=c).number_format = "#,##0.0"
        ws.cell(row=r, column=9).number_format = "0.000"
        ws.cell(row=r, column=10).number_format = "0.00"

    last = 4 + len(sweep_results)
    note = ws.cell(
        row=last + 2,
        column=1,
        value=(
            "Note: condensed MASS rises monotonically with air change rate, but "
            "WET HOURS does not. A sealed cavity cycles a small trapped inventory "
            "across many hours - frequent events, negligible water. Mass is the "
            "meaningful metric; hours alone will mislead."
        ),
    )
    note.font = NOTE_FONT
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=last + 2, start_column=1, end_row=last + 4, end_column=10)

    widths = [14, 44, 8, 11, 12, 11, 14, 14, 14, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ---------------------------------------------------------------------------
# Summary sheet
# ---------------------------------------------------------------------------

def _write_summary(wb, summary, meta, last_row: int, has_sweep: bool) -> None:
    ws = wb.create_sheet("Summary", 0)

    ws["A1"] = "INOVUES - Cavity Moisture Analysis"
    ws["A1"].font = TITLE
    ws["A2"] = f"ANLY-002  |  generated {_dt.date.today().isoformat()}"
    ws["A2"].font = NOTE_FONT

    r = 4
    r = _section(ws, r, "LOCATION AND WEATHER")
    _label(ws, r, "Address", meta.get("matched_address", ""), font=INPUT_FONT); r += 1
    _label(ws, r, "Weather source", meta.get("weather_source", "NSRDB PSM3 TMY")); r += 1
    _label(ws, r, "Weather station", meta.get("station_id", "")); r += 1
    _label(ws, r, "Elevation", meta.get("elevation_m", 0.0), unit="m"); r += 1
    total_hours_row = r  # captured, never hardcoded - see below
    _label(ws, r, "Hours simulated", f"=COUNT('{RAW}'!{COL['hour']}2:{COL['hour']}{last_row})"); r += 2

    r = _section(ws, r, "ASSEMBLY")
    _label(ws, r, "f (cold surface)", meta.get("f_cold"), font=INPUT_FONT,
           note="Cavity face of the existing exterior pane, from WINDOW/THERM"); r += 1
    _label(ws, r, "f (warm surface)", meta.get("f_warm"), font=INPUT_FONT,
           note=meta.get("f_warm_source", "")); r += 1
    _label(ws, r, "Cavity offset", meta.get("offset_in"), unit="in",
           font=INPUT_FONT, note="The geometric design lever"); r += 1
    if meta.get("width_in"):
        _label(ws, r, "Window width", meta.get("width_in"), unit="in", font=INPUT_FONT); r += 1
        _label(ws, r, "Window height", meta.get("height_in"), unit="in", font=INPUT_FONT); r += 1
        area_row = r
        # Full precision stored, display rounded. Writing a rounded VALUE here
        # would propagate the rounding into the per-window formulas below.
        area_cell = _label(
            ws, r, "Glazing area",
            summary.geometry.glazing_area_m2 if summary.geometry
            else meta.get("glazing_area_m2"),
            unit="m2",
            note="Window size does not change per-m2 results; it scales totals",
        )
        area_cell.number_format = "0.0000"
        r += 1
    else:
        area_row = None
    r += 1

    r = _section(ws, r, "OPERATING CONDITIONS")
    _label(ws, r, "Interior air temperature", meta.get("t_in_f"), unit="F", font=INPUT_FONT); r += 1
    _label(ws, r, "Interior relative humidity", meta.get("rh_in_pct"), unit="%", font=INPUT_FONT); r += 1
    _label(ws, r, "Air change rate", meta.get("ach"), unit="1/hr", font=INPUT_FONT,
           fill=ASSUMPTION_FILL, note="ESTIMATE - see assumptions below"); r += 1
    _label(ws, r, "Vent source", meta.get("vent_label", ""), font=INPUT_FONT,
           note="1.0 = vents to room, 0.0 = vents to outdoor air"); r += 2

    # -- Results: formulas over the raw tab, never copied numbers -----------
    r = _section(ws, r, "RESULTS")
    ws.cell(row=r, column=1,
            value="Every figure below is a live formula over the Hourly Data "
                  "sheet, not a copied value.").font = NOTE_FONT
    r += 1

    hours_row = r
    _label(ws, r, "Hours with condensation",
           f"=SUMIFS('{RAW}'!{COL['is_condensing']}2:{COL['is_condensing']}{last_row},"
           f"'{RAW}'!{COL['is_condensing']}2:{COL['is_condensing']}{last_row},1)",
           unit="hr"); r += 1
    # total_hours_row was captured where the row was actually written. It
    # used to be hardcoded to 8 while the row was 9, so every share-of-year
    # figure divided by Elevation (0) and IFERROR reported 0%.
    pct = _label(ws, r, "Share of year with condensation",
                 f"=IFERROR(B{hours_row}/B{total_hours_row},0)")
    pct.number_format = "0.0%"; r += 1

    # Liquid PRESENT, not liquid DEPOSITING. Counted with COUNTIF over the
    # standing-water column so the workbook recalculates it like every other
    # summary figure. Always >= "Hours with condensation": water laid down
    # overnight is still on the glass in the morning.
    water_row = r
    _label(ws, r, "Hours with water on the glass",
           f"=COUNTIF('{RAW}'!{COL['surface_water_g']}2:"
           f"{COL['surface_water_g']}{last_row},\">0\")",
           unit="hr"); r += 1
    pctw = _label(ws, r, "Share of year with water on the glass",
                  f"=IFERROR(B{water_row}/B{total_hours_row},0)")
    pctw.number_format = "0.0%"; r += 1
    ws.cell(row=r, column=1, value="Occupant-facing metric. Not monotonic in "
            "ACH - use condensed mass to compare vent designs.").font = NOTE_FONT
    r += 1

    cond_row = r
    c = _label(ws, r, "Condensed water (per m2 of glazing)",
               f"=SUM('{RAW}'!{COL['condensed_g']}2:{COL['condensed_g']}{last_row})",
               unit="g/m2/yr")
    c.number_format = "#,##0.0"; r += 1

    drain_row = r
    c = _label(ws, r, "Drained out the weep path (per m2)",
               f"=SUM('{RAW}'!{COL['drained_g']}2:{COL['drained_g']}{last_row})",
               unit="g/m2/yr")
    c.number_format = "#,##0.0"; r += 1

    c = _label(ws, r, "Peak standing water",
               f"=MAX('{RAW}'!{COL['surface_water_g']}2:{COL['surface_water_g']}{last_row})",
               unit="g/m2")
    c.number_format = "#,##0.00"; r += 1

    c = _label(ws, r, "Lowest cavity dew point",
               f"=MIN('{RAW}'!{COL['dew_point_cav_f']}2:{COL['dew_point_cav_f']}{last_row})",
               unit="F")
    c.number_format = "0.00"; r += 1

    c = _label(ws, r, "Mean cavity dew point",
               f"=AVERAGE('{RAW}'!{COL['dew_point_cav_f']}2:{COL['dew_point_cav_f']}{last_row})",
               unit="F")
    c.number_format = "0.00"; r += 1

    c = _label(ws, r, "Coldest cavity surface",
               f"=MIN('{RAW}'!{COL['t_cold_f']}2:{COL['t_cold_f']}{last_row})", unit="F")
    c.number_format = "0.00"; r += 1

    if area_row is not None:
        r += 1
        r = _section(ws, r, "PER WINDOW")
        c = _label(ws, r, "Condensed water (whole window)",
                   f"=B{cond_row}*B{area_row}/1000",
                   unit="L/yr", note="g/m2 x area / 1000")
        c.number_format = "0.000"; r += 1
        c = _label(ws, r, "Drained (whole window)",
                   f"=B{drain_row}*B{area_row}/1000", unit="L/yr")
        c.number_format = "0.000"; r += 1

    r += 1
    r = _section(ws, r, "COMPARISON WITH THE EXISTING CONDENSATION CALCULATOR")
    _label(ws, r, "Hours flagged by room-dew-point criterion",
           summary.hours_condensing_room_assumption, unit="hr",
           note="The assumption this model replaces"); r += 1
    ws.cell(row=r, column=1, value=(
        "Our figure is higher, and correctly so: the retained condensate film "
        "holds the cavity at saturation after room air alone would have stopped "
        "condensing. The older model has no liquid inventory and cannot "
        "represent that.")).font = NOTE_FONT
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 1, end_column=6)
    r += 3

    # -- Assumptions -------------------------------------------------------
    r = _section(ws, r, "UNVALIDATED ASSUMPTIONS")
    ws.cell(row=r, column=1, value=(
        "The following are engineering estimates, NOT measurements. They "
        "materially affect the results above.")).font = NOTE_FONT
    r += 1
    for text, value, unit, note in (
        ("Air change rate", meta.get("ach"), "1/hr",
         "Bound it, don't guess it - needs tracer-gas or pressure-decay testing"),
        ("Retained condensate film", MAX_SURFACE_FILM_KG_PER_M2 * 1000, "g/m2",
         "Water beyond this drains away. Moves the wet-hour count materially."),
        ("Warm-surface f", meta.get("f_warm"), "-",
         meta.get("f_warm_source", "")),
    ):
        _label(ws, r, text, value, unit=unit, fill=ASSUMPTION_FILL, note=note)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value=(
        "Evaporation is modelled as fast enough to hold saturation, an UPPER "
        "bound on drying. Reported standing water is therefore a lower bound.")
    ).font = NOTE_FONT

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 70
    ws.sheet_view.showGridLines = False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_workbook(summary, meta: dict, sweep_results=None, preset_names=None) -> Workbook:
    """Assemble the workbook. ``summary`` must have been run with keep_hours=True."""
    if not summary.hours:
        raise ValueError(
            "build_workbook needs hourly records; run the model with keep_hours=True"
        )

    wb = Workbook()
    wb.remove(wb.active)

    last_row = _write_hourly(wb, summary)
    if sweep_results:
        _write_sweep(wb, sweep_results, preset_names or [], summary.geometry)
    _write_summary(wb, summary, meta, last_row, bool(sweep_results))

    wb.active = 0
    return wb


def workbook_bytes(summary, meta: dict, sweep_results=None, preset_names=None) -> bytes:
    """Workbook as bytes, for streaming from a Flask response."""
    buffer = BytesIO()
    build_workbook(summary, meta, sweep_results, preset_names).save(buffer)
    return buffer.getvalue()
