"""
INOVUES - Cavity Moisture Model
Flask application entry point.

Doc ID: ANLY-002 R1.0
"""

from __future__ import annotations

import os

from flask import Flask, Response, jsonify, request, send_from_directory

from engine import psychro
from engine.cavity import f_warm_estimate, t_from_f
from engine.geometry import CavityGeometry
from engine.moisture import (
    ACH_PRESET_LABELS,
    ACH_PRESETS,
    ACH_PRESETS_ARE_ESTIMATES,
    MAX_SURFACE_FILM_KG_PER_M2,
    run_year,
    sweep_ach,
)
from engine.report import workbook_bytes
from engine.solar import ORIENTATIONS, poa_series
from engine.weather import WeatherError, get_weather_for_address

APP_VERSION = "0.4.0"
APP_NAME = "INOVUES Cavity Moisture Model"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

class BadRequest(ValueError):
    """A user-correctable problem with the query string."""


def _float(name, default=None, minimum=None, maximum=None) -> float:
    raw = request.args.get(name)
    if raw is None or raw == "":
        if default is None:
            raise BadRequest(f"Missing required parameter {name!r}")
        return default
    try:
        value = float(raw)
    except ValueError:
        raise BadRequest(f"Parameter {name!r} must be a number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise BadRequest(f"Parameter {name!r} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise BadRequest(f"Parameter {name!r} must be at most {maximum}, got {value}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _ach() -> float:
    """Air change rate: a preset name or any positive number.

    ACH is deliberately a free input rather than a fixed bracket, so vent and
    seal designs that match no preset can be tested.
    """
    raw = request.args.get("ach", "moderate").strip().lower()
    if raw in ACH_PRESETS:
        return ACH_PRESETS[raw]
    try:
        value = float(raw)
    except ValueError:
        raise BadRequest(
            f"Parameter 'ach' must be a number or one of "
            f"{sorted(ACH_PRESETS)}, got {raw!r}"
        )
    if value < 0:
        raise BadRequest(f"Parameter 'ach' cannot be negative, got {value}")
    return value


def _geometry():
    """Optional window geometry, supplied in inches.

    Width and height do not change the per-m2 answer at fixed ACH - they set
    the per-window totals. Offset does change it, and is the geometric design
    lever. See engine/geometry.py.
    """
    if request.args.get("width_in") is None and request.args.get("height_in") is None:
        return None
    try:
        return CavityGeometry.from_inches(
            width_in=_float("width_in", minimum=0.1),
            height_in=_float("height_in", minimum=0.1),
            offset_in=_float("offset_in", default=0.6024, minimum=0.01),
            label=request.args.get("label", ""),
        )
    except BadRequest:
        raise
    except ValueError as exc:
        raise BadRequest(str(exc))


def _parse_params() -> dict:
    """Parse and validate every model input.

    Shared by /calculate and /export.xlsx so the two endpoints cannot drift
    apart in validation or defaults.
    """
    address = request.args.get("address", "").strip()
    if not address:
        raise BadRequest("Missing required parameter 'address'")

    f_cold = _float("f_cold", minimum=0.0, maximum=1.0)
    # US practice quotes U-factor in Btu/(hr*ft^2*degF). The engine is SI
    # throughout (design rule 1), so IP is converted here at the boundary,
    # exactly as t_in and width_in are. The SI parameters remain accepted so
    # existing URLs and saved reports cannot be silently reinterpreted.
    if request.args.get("u_ip"):
        u_assembly = psychro.u_ip_to_si(_float("u_ip", minimum=0.001, maximum=2.0))
    else:
        u_assembly = _float("u_assembly", default=1.7, minimum=0.01)

    # Visible-film threshold in MICRONS at the boundary (1 g/m2 = 1 um).
    # ESTIMATE - see VISIBLE_FILM_KG_PER_M2. Labelled wherever it surfaces.
    visible_um = _float("visible_um", default=5.0, minimum=0.0, maximum=1000.0)

    # Surface energy balance. Both default OFF so existing URLs and the
    # validation anchor keep their meaning; the UI opts in explicitly. Same
    # precedent as u_ip vs u_assembly: never silently reinterpret a request.
    absorptance = _float("absorptance", default=0.0, minimum=0.0, maximum=1.0)
    sky_radiation = _bool("sky_radiation", False)
    # Vented room air warming the pane. OFF by default, like solar and
    # sky, so the 676/860-hour anchors stay live. See
    # engine.cavity.vent_cold_surface_rise for why it is small.
    pane_coupling = _bool("pane_coupling", False)
    orientation = (request.args.get("orientation") or "south").strip().lower()
    if orientation not in ORIENTATIONS:
        raise BadRequest(
            f"orientation must be one of {', '.join(ORIENTATIONS)}, "
            f"got {orientation!r}"
        )

    if request.args.get("r_ip"):
        r_cavity = psychro.r_ip_to_si(_float("r_ip", minimum=0.001, maximum=100.0))
    else:
        r_cavity = _float("r_cavity", default=0.17, minimum=0.001)

    if request.args.get("f_warm"):
        f_warm = _float("f_warm", minimum=0.0, maximum=1.0)
        f_warm_source = "supplied"
    else:
        f_warm = f_warm_estimate(f_cold, r_cavity, u_assembly)
        f_warm_source = "estimated as f_cold + r_cavity x u_assembly"

    if f_warm < f_cold:
        raise BadRequest(
            f"f_warm ({f_warm:.3f}) is below f_cold ({f_cold:.3f}). The "
            "interior-side cavity surface cannot be colder than the "
            "exterior-side one."
        )

    return {
        "address": address,
        "f_cold": f_cold,
        "u_assembly": u_assembly,
        "r_cavity": r_cavity,
        "visible_um": visible_um,
        "absorptance": absorptance,
        "sky_radiation": sky_radiation,
        "pane_coupling": pane_coupling,
        "orientation": orientation,
        "f_warm": f_warm,
        "f_warm_source": f_warm_source,
        "t_in_f": _float("t_in", default=70.0, minimum=-40.0, maximum=120.0),
        "rh_in_pct": _float("rh_in", default=35.0, minimum=0.0, maximum=100.0),
        "ach": _ach(),
        "vent_interior": _float("vent_interior", default=1.0, minimum=0.0, maximum=1.0),
        "geometry": _geometry(),
        "sweep": _bool("sweep", False),
        "orientations": _bool("orientations", False),
    }


def _run_kwargs(params: dict, weather) -> dict:
    return dict(
        f_cold=params["f_cold"],
        f_warm=params["f_warm"],
        t_room_c=psychro.f_to_c(params["t_in_f"]),
        rh_room=params["rh_in_pct"] / 100.0,
        vent_interior_fraction=params["vent_interior"],
        geometry=params["geometry"],
        elevation_m=weather.elevation_m,
        # microns -> kg/m2. 1 g/m2 is a 1 um film.
        visible_film_kg_per_m2=params["visible_um"] / 1000.0,
        u_assembly=params["u_assembly"] if params["pane_coupling"] else None,
        **_surface_balance_kwargs(params, weather, params["orientation"]),
    )


def _surface_balance_kwargs(params: dict, weather, orientation: str) -> dict:
    """Solar and sky arguments for run_year, or nothing at all.

    Returns an empty dict when the weather year carries no irradiance, so an
    older cached TMY silently falls back to the air-only model instead of
    half-applying sun.
    """
    if not weather.has_solar:
        return {}
    out: dict = {
        "wind_m_s": weather.wind_m_s or None,
        "sky_radiation": params["sky_radiation"],
        "cloud_type": weather.cloud_type or None,
    }
    if params["absorptance"] > 0.0:
        out["poa_w_m2"] = poa_series(
            weather.ghi_w_m2, weather.dni_w_m2, weather.dhi_w_m2,
            weather.grid_lat, weather.grid_lon, weather.time_zone or 0.0,
            ORIENTATIONS[orientation],
        )
        out["absorptance"] = params["absorptance"]
    return out


def _summary_dict(summary) -> dict:
    """Serialise a RunSummary, omitting the 8,760 hourly records."""
    out = {
        "ach": summary.ach,
        "vent_interior_fraction": summary.vent_interior_fraction,
        "hours_total": summary.hours_total,
        "hours_condensing": summary.hours_condensing,
        "hours_water_present": summary.hours_water_present,
        "pct_water_present": round(summary.pct_water_present, 3),
        "hours_water_visible": summary.hours_water_visible,
        "pct_water_visible": round(summary.pct_water_visible, 3),
        "visible_threshold_um": round(summary.visible_threshold_kg_per_m2 * 1000.0, 3),
        # Pane warming from vented room air. 0 at sealed by definition.
        "mean_vent_rise_k": round(summary.mean_vent_rise_k, 3),
        "max_vent_rise_k": round(summary.max_vent_rise_k, 3),
        "max_vent_rise_f": round(summary.max_vent_rise_k * 1.8, 3),
        "hours_saturated": summary.hours_saturated,
        "pct_condensing": round(summary.pct_condensing, 3),
        "condensed_g_per_m2": round(summary.total_condensed_kg_per_m2 * 1000, 3),
        "drained_g_per_m2": round(summary.total_drained_kg_per_m2 * 1000, 3),
        "peak_surface_water_g_per_m2": round(
            summary.peak_surface_water_kg_per_m2 * 1000, 4
        ),
        "cavity_dew_point_f": {
            "mean": round(psychro.c_to_f(summary.mean_cavity_dew_point_c), 2),
            "min": round(psychro.c_to_f(summary.min_cavity_dew_point_c), 2),
            "max": round(psychro.c_to_f(summary.max_cavity_dew_point_c), 2),
        },
        "room_dew_point_f": round(psychro.c_to_f(summary.mean_room_dew_point_c), 2),
        "hours_condensing_room_assumption": summary.hours_condensing_room_assumption,
    }
    if summary.geometry is not None:
        out["per_window"] = {
            "glazing_area_m2": round(summary.glazing_area_m2, 4),
            "condensed_litres_per_year": round(
                summary.total_condensed_litres_per_window, 4
            ),
            "drained_litres_per_year": round(
                summary.total_drained_litres_per_window, 4
            ),
            "peak_standing_litres": round(
                summary.peak_surface_water_litres_per_window, 5
            ),
        }
    return out


def _error_response(exc):
    if isinstance(exc, BadRequest):
        return jsonify({"error": "bad_request", "message": str(exc)}), 400
    if isinstance(exc, WeatherError):
        return jsonify({"error": "weather_unavailable", "message": str(exc)}), 502
    return jsonify({"error": "invalid_input", "message": str(exc)}), 400


# ---------------------------------------------------------------------------
# Static and config
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # no-cache does not mean "never cache": it means revalidate before reuse.
    # Flask sends an ETag, so the browser re-checks every visit and a redeploy
    # shows up immediately. Without this, condensation_calc produced two
    # "I still see the old version" incidents, one of them because Chrome
    # partitions its cache per top-level site: the copy shown inside the Odoo
    # iframe is cached separately from the copy at the direct URL.
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/config")
def config():
    """Public front-end configuration.

    MAPBOX_TOKEN is publishable and safe to hand to the browser.
    NREL_API_KEY is deliberately NOT exposed - all NSRDB calls are server-side.
    We report only whether it is present, so the UI can show a clear error.
    """
    return jsonify(
        {
            "app": APP_NAME,
            "version": APP_VERSION,
            "mapbox_token": os.environ.get("MAPBOX_TOKEN", ""),
            "weather_configured": bool(os.environ.get("NREL_API_KEY")),
            "ach_presets": ACH_PRESETS,
            "ach_preset_labels": ACH_PRESET_LABELS,
            "ach_presets_are_estimates": ACH_PRESETS_ARE_ESTIMATES,
            "max_surface_film_kg_per_m2": MAX_SURFACE_FILM_KG_PER_M2,
            "assumptions": [
                "ACH presets are engineering estimates, not measurements.",
                "Retained condensate film (0.1 kg/m2) is an estimate.",
                "Warm-surface f is estimated as f_cold + R_cav x U unless supplied.",
                "Visible-film threshold (5 um default) is an estimate; no optical "
                "threshold for condensate on vertical glass has been measured.",
                "Solar gain is NOT modelled. Surface temperatures follow outdoor "
                "AIR temperature only, so sunlit drying is absent and night-sky "
                "radiative cooling is absent. Orientation has no effect.",
            ],
        }
    )


@app.route("/healthz")
def healthz():
    """Liveness probe. Also smoke-tests the physics engine, so a broken deploy
    fails the health check rather than quietly serving wrong numbers."""
    try:
        dew_f = psychro.dew_point_f_from_t_rh(70.0, 35.0)
        engine_ok = abs(dew_f - 41.09) < 0.10
    except Exception:
        engine_ok = False
        dew_f = None

    return (
        jsonify(
            {
                "status": "ok" if engine_ok else "engine_check_failed",
                "version": APP_VERSION,
                "engine_check": {
                    "case": "dew point of 70 degF / 35% RH air",
                    "expected_f": 41.09,
                    "actual_f": round(dew_f, 4) if dew_f is not None else None,
                },
            }
        ),
        200 if engine_ok else 503,
    )


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

@app.route("/calculate")
def calculate():
    """Run the moisture balance for one address and one assembly.

    Query parameters
    ----------------
    address        required. Free text, geocoded via Mapbox.
    f_cold         required. Temperature factor of the cavity-side face of the
                   existing exterior pane, from WINDOW/THERM.
    f_warm         optional. Same for the cavity-facing face of the new IGU.
                   If omitted, estimated from u_assembly and r_cavity.
    u_ip           U-factor in Btu/(hr*ft^2*degF). Preferred. 0.30 = 1.7 W/m2K.
    u_assembly     same in W/m2K, default 1.7. Ignored when u_ip is given.
    r_ip           cavity R in hr*ft^2*degF/Btu. 0.97 = 0.17 m2K/W.
    r_cavity       same in m2K/W, default 0.17. Ignored when r_ip is given.
    t_in           default 70 degF. Interior air temperature.
    rh_in          default 35 %. Interior relative humidity.
    ach            default "moderate". A preset name or any number.
    vent_interior  default 1.0. 1 = vents to room, 0 = vents to outdoors.
    pane_coupling  default 0. 1 lets vented room air warm the cold pane
                   (pane temperature then depends on ACH). Small effect;
                   see engine.cavity.vent_cold_surface_rise.
    width_in,
    height_in,
    offset_in      optional. Supplying width and height enables per-window
                   totals in litres. Offset defaults to 0.6024 in (15.3 mm).
    sweep          default false. If true, also run every ACH preset.
    """
    try:
        params = _parse_params()
        location, weather, cached = get_weather_for_address(params["address"])
        common = _run_kwargs(params, weather)

        summary = run_year(
            weather.t_out_c, weather.rh_out, ach=params["ach"],
            keep_hours=False, **common
        )

        payload = {
            "inputs": {
                "address": params["address"],
                "f_cold": params["f_cold"],
                "f_warm": round(params["f_warm"], 4),
                "f_warm_source": params["f_warm_source"],
                "u_assembly": params["u_assembly"],
                "u_assembly_ip": round(psychro.u_si_to_ip(params["u_assembly"]), 4),
                "r_cavity": params["r_cavity"],
                "r_cavity_ip": round(psychro.r_si_to_ip(params["r_cavity"]), 4),
                "absorptance": params["absorptance"],
                "sky_radiation": params["sky_radiation"],
                "pane_coupling": params["pane_coupling"],
                "orientation": params["orientation"],
                "visible_um": params["visible_um"],
                "t_in_f": params["t_in_f"],
                "rh_in_pct": params["rh_in_pct"],
                "ach": params["ach"],
                "vent_interior_fraction": params["vent_interior"],
            },
            "location": {
                "matched_address": location.matched_address,
                "lat": round(location.lat, 6),
                "lon": round(location.lon, 6),
                "weather_cached": cached,
            },
            "weather": weather.describe(),
            "surfaces_at_nfrc_winter_f": {
                "t_cold": round(psychro.c_to_f(t_from_f(params["f_cold"])), 2),
                "t_warm": round(psychro.c_to_f(t_from_f(params["f_warm"])), 2),
            },
            "summary": _summary_dict(summary),
            "assumptions": {
                "ach_presets_are_estimates": ACH_PRESETS_ARE_ESTIMATES,
                "max_surface_film_kg_per_m2": MAX_SURFACE_FILM_KG_PER_M2,
                "f_warm_source": params["f_warm_source"],
            },
        }

        if params["geometry"] is not None:
            payload["geometry"] = params["geometry"].describe()

        if params["sweep"]:
            results = sweep_ach(
                weather.t_out_c, weather.rh_out,
                f_cold=params["f_cold"], f_warm=params["f_warm"],
                ach_values=list(ACH_PRESETS.values()),
                t_room_c=common["t_room_c"], rh_room=common["rh_room"],
                vent_interior_fraction=params["vent_interior"],
                geometry=params["geometry"], elevation_m=weather.elevation_m,
                visible_film_kg_per_m2=params["visible_um"] / 1000.0,
                u_assembly=params["u_assembly"] if params["pane_coupling"] else None,
                **_surface_balance_kwargs(params, weather, params["orientation"]),
            )
            payload["sweep"] = [
                {"preset": name, **_summary_dict(r)}
                for name, r in zip(ACH_PRESETS, results)
            ]

        # Four facades at the chosen vent design. Only meaningful when the sun
        # is actually switched on - with absorptance 0 every orientation
        # returns the same numbers, which would imply orientation does not
        # matter rather than that it was not modelled.
        if params["orientations"] and weather.has_solar and params["absorptance"] > 0:
            payload["orientations"] = [
                {
                    "orientation": name,
                    "azimuth_deg": ORIENTATIONS[name],
                    **_summary_dict(
                        run_year(
                            weather.t_out_c, weather.rh_out, ach=params["ach"],
                            keep_hours=False,
                            **{**common, **_surface_balance_kwargs(params, weather, name)},
                        )
                    ),
                }
                for name in ORIENTATIONS
            ]

        return jsonify(payload)

    except (BadRequest, WeatherError, ValueError) as exc:
        return _error_response(exc)


@app.route("/export.xlsx")
def export_xlsx():
    """Excel workbook: Summary, ACH Sweep, and 8,760 hours of raw data.

    Takes the same query parameters as /calculate. The Summary sheet computes
    its figures with live formulas over the raw tab rather than copying values
    out of Python, so the workbook recalculates and can be audited.
    """
    try:
        params = _parse_params()
        location, weather, _ = get_weather_for_address(params["address"])
        common = _run_kwargs(params, weather)

        summary = run_year(
            weather.t_out_c, weather.rh_out, ach=params["ach"],
            keep_hours=True, **common
        )
        sweep = sweep_ach(
            weather.t_out_c, weather.rh_out,
            f_cold=params["f_cold"], f_warm=params["f_warm"],
            ach_values=list(ACH_PRESETS.values()),
            t_room_c=common["t_room_c"], rh_room=common["rh_room"],
            vent_interior_fraction=params["vent_interior"],
            geometry=params["geometry"], elevation_m=weather.elevation_m,
            u_assembly=params["u_assembly"] if params["pane_coupling"] else None,
        )

        geometry = params["geometry"]
        vent = params["vent_interior"]
        meta = {
            "matched_address": location.matched_address,
            "weather_source": weather.source,
            "station_id": weather.station_id,
            "elevation_m": weather.elevation_m,
            "f_cold": params["f_cold"],
            "f_warm": round(params["f_warm"], 4),
            "f_warm_source": params["f_warm_source"],
            "offset_in": round(geometry.offset_in, 4) if geometry else None,
            "width_in": round(geometry.width_in, 2) if geometry else None,
            "height_in": round(geometry.height_in, 2) if geometry else None,
            "glazing_area_m2": geometry.glazing_area_m2 if geometry else None,
            "t_in_f": params["t_in_f"],
            "rh_in_pct": params["rh_in_pct"],
            "ach": params["ach"],
            "vent_label": f"{vent:.2f} ({'interior' if vent >= 0.5 else 'exterior'})",
        }

        orientation_rows = None
        if params["orientations"] and weather.has_solar and params["absorptance"] > 0:
            orientation_rows = [
                {"orientation": name, "azimuth_deg": ORIENTATIONS[name],
                 **_summary_dict(run_year(
                     weather.t_out_c, weather.rh_out, ach=params["ach"],
                     keep_hours=False,
                     **{**common, **_surface_balance_kwargs(params, weather, name)},
                 ))}
                for name in ORIENTATIONS
            ]
        data = workbook_bytes(summary, meta, sweep, list(ACH_PRESETS),
                              orientation_rows)
        stem = "".join(
            ch if ch.isalnum() else "_" for ch in params["address"]
        )[:40].strip("_") or "cavity"
        return Response(
            data,
            mimetype=XLSX_MIME,
            headers={
                "Content-Disposition":
                    f'attachment; filename="{stem}_cavity_moisture.xlsx"'
            },
        )

    except (BadRequest, WeatherError, ValueError) as exc:
        return _error_response(exc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
