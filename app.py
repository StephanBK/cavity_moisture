"""
INOVUES - Cavity Moisture Model
Flask application entry point.

Doc ID: ANLY-002 R1.0
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

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
from engine.weather import WeatherError, get_weather_for_address

APP_VERSION = "0.3.0"
APP_NAME = "INOVUES Cavity Moisture Model"

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------------------
# Request parsing helpers
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
    seal designs that do not match a preset can be tested.
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


def _summary_dict(summary) -> dict:
    """Serialise a RunSummary, omitting the 8,760 hourly records."""
    out = {
        "ach": summary.ach,
        "vent_interior_fraction": summary.vent_interior_fraction,
        "hours_total": summary.hours_total,
        "hours_condensing": summary.hours_condensing,
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


# ---------------------------------------------------------------------------
# Static and config
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
    u_assembly     default 1.7 W/m2K. Only used to estimate f_warm.
    r_cavity       default 0.17 m2K/W. Only used to estimate f_warm.
    t_in           default 70 degF. Interior air temperature.
    rh_in          default 35 %. Interior relative humidity.
    ach            default "moderate". A preset name or any number.
    vent_interior  default 1.0. 1 = vents to room, 0 = vents to outdoors.
    width_in,
    height_in,
    offset_in      optional. Supplying width and height enables per-window
                   totals in litres. Offset defaults to 0.6024 in (15.3 mm).
    sweep          default false. If true, also run every ACH preset.
    """
    try:
        address = request.args.get("address", "").strip()
        if not address:
            raise BadRequest("Missing required parameter 'address'")

        f_cold = _float("f_cold", minimum=0.0, maximum=1.0)
        u_assembly = _float("u_assembly", default=1.7, minimum=0.01)
        r_cavity = _float("r_cavity", default=0.17, minimum=0.001)
        f_warm_raw = request.args.get("f_warm")
        if f_warm_raw:
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

        t_in_f = _float("t_in", default=70.0, minimum=-40.0, maximum=120.0)
        rh_in_pct = _float("rh_in", default=35.0, minimum=0.0, maximum=100.0)
        ach = _ach()
        vent_interior = _float("vent_interior", default=1.0, minimum=0.0, maximum=1.0)
        geometry = _geometry()
        want_sweep = _bool("sweep", False)

        location, weather, cached = get_weather_for_address(address)

        common = dict(
            f_cold=f_cold,
            f_warm=f_warm,
            t_room_c=psychro.f_to_c(t_in_f),
            rh_room=rh_in_pct / 100.0,
            vent_interior_fraction=vent_interior,
            geometry=geometry,
            elevation_m=weather.elevation_m,
        )

        summary = run_year(
            weather.t_out_c, weather.rh_out, ach=ach, keep_hours=False, **common
        )

        payload = {
            "inputs": {
                "address": address,
                "f_cold": f_cold,
                "f_warm": round(f_warm, 4),
                "f_warm_source": f_warm_source,
                "u_assembly": u_assembly,
                "r_cavity": r_cavity,
                "t_in_f": t_in_f,
                "rh_in_pct": rh_in_pct,
                "ach": ach,
                "vent_interior_fraction": vent_interior,
            },
            "location": {
                "matched_address": location.matched_address,
                "lat": round(location.lat, 6),
                "lon": round(location.lon, 6),
                "weather_cached": cached,
            },
            "weather": weather.describe(),
            "surfaces_at_nfrc_winter_f": {
                "t_cold": round(psychro.c_to_f(t_from_f(f_cold)), 2),
                "t_warm": round(psychro.c_to_f(t_from_f(f_warm)), 2),
            },
            "summary": _summary_dict(summary),
            "assumptions": {
                "ach_presets_are_estimates": ACH_PRESETS_ARE_ESTIMATES,
                "max_surface_film_kg_per_m2": MAX_SURFACE_FILM_KG_PER_M2,
                "f_warm_source": f_warm_source,
            },
        }

        if geometry is not None:
            payload["geometry"] = geometry.describe()

        if want_sweep:
            results = sweep_ach(
                weather.t_out_c,
                weather.rh_out,
                f_cold=f_cold,
                f_warm=f_warm,
                ach_values=list(ACH_PRESETS.values()),
                t_room_c=common["t_room_c"],
                rh_room=common["rh_room"],
                vent_interior_fraction=vent_interior,
                geometry=geometry,
                elevation_m=weather.elevation_m,
            )
            payload["sweep"] = [
                {"preset": name, **_summary_dict(r)}
                for name, r in zip(ACH_PRESETS, results)
            ]

        return jsonify(payload)

    except BadRequest as exc:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400
    except WeatherError as exc:
        return jsonify({"error": "weather_unavailable", "message": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": "invalid_input", "message": str(exc)}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
