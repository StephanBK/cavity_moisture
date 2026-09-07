"""
INOVUES - Cavity Moisture Model
Flask application entry point.

Chunk 0/1 scope: scaffold, health checks, and a /config endpoint that hands
public tokens to the browser without ever putting them in the HTML.
The moisture engine and NSRDB weather layer arrive in Chunk 3.

Doc ID: ANLY-002 R1.0
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, send_from_directory

from engine import psychro

APP_VERSION = "0.1.0"
APP_NAME = "INOVUES Cavity Moisture Model"

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# Config - browser fetches this instead of us baking tokens into the HTML
# ---------------------------------------------------------------------------

@app.route("/config")
def config():
    """Public front-end configuration.

    MAPBOX_TOKEN is a *publishable* token, safe to hand to the browser.
    NREL_API_KEY is deliberately NOT exposed here - all NSRDB calls are made
    server-side in Chunk 3. We only report whether it is present, so the UI
    can show a clear error instead of failing mysteriously.
    """
    return jsonify(
        {
            "app": APP_NAME,
            "version": APP_VERSION,
            "mapbox_token": os.environ.get("MAPBOX_TOKEN", ""),
            "weather_configured": bool(os.environ.get("NREL_API_KEY")),
        }
    )


@app.route("/healthz")
def healthz():
    """Liveness probe for Railway. Also smoke-tests the physics engine, so a
    broken deploy fails the health check rather than serving wrong numbers.
    """
    try:
        dew_f = psychro.dew_point_f_from_t_rh(70.0, 35.0)
        engine_ok = abs(dew_f - 41.09) < 0.10
    except Exception:
        engine_ok = False
        dew_f = None

    status = 200 if engine_ok else 503
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
        status,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
