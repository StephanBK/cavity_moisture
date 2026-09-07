# Cavity Moisture Model

Single-node vented-cavity moisture balance for INOVUES secondary window
retrofit (SWR) assemblies.

**Doc ID:** ANLY-002 R1.0 · **Owner:** Stephan Ketterer, VP of Operations

## What this is

The existing [condensation calculator](https://web-production-e8226.up.railway.app)
predicts cavity-side condensation from **temperature alone**, and assumes the
cavity dew point equals the room dew point. That assumption is unverified.

This app replaces it with a defensible cavity dew point, computed from an
hourly moisture balance across the vented cavity, bracketed over three air
exchange rates.

## Status

| Chunk | Scope | State |
|---|---|---|
| 0 | Repo scaffold, Flask, Railway config | done |
| 1 | Psychrometric core + audit test suite | done |
| 2 | Cavity air temperature from f | done |
| 3a | Hourly moisture balance engine | done |
| 3b | NSRDB TMY weather + /calculate endpoint | done |
| 3c | Window geometry, per-window totals | done |
| 4 | ACH sweep + Excel export | done |
| 5 | Frontend: Explain / Model / Present | done |
| 6 | Railway deploy + validation | pending |

## Unit convention

**All physics is SI.** Kelvin, Pascals, kg/kg dry air, metres.
Fahrenheit exists only in `engine/psychro.py`'s conversion helpers and at the
UI boundary. Every ASHRAE constant is published in SI; converting once at the
edge rather than inside a correlation is what keeps the conversions exact.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your tokens
python app.py               # http://localhost:8080
```

## Tests

```bash
pytest
```

`tests/` is written to be **read**, not just run. Every
assertion cites its source: `[ASHRAE]` for a published table value,
`[DERIVED]` for arithmetic worked longhand in the docstring, `[LIVE]` for a
cross-check against the deployed condensation_calc. A reviewer can audit
every physical claim the model makes from that one file without reading the
engine.

## Deployment (Railway)

Railway builds with Nixpacks, detects Python from `requirements.txt`, and
uses the start command in `railway.json`. Health check is `/healthz`, which
smoke-tests the physics engine — a deploy that computes dew point wrong will
fail the health check rather than quietly serve bad numbers.

**Required service variables:**

| Variable | Purpose | Exposed to browser? |
|---|---|---|
| `MAPBOX_TOKEN` | Address autocomplete (publishable token) | Yes, via `/config` |
| `NREL_API_KEY` | NSRDB TMY hourly weather | **No** — server-side only |

Never commit either. `.env` is gitignored.

## Endpoints

| Route | Purpose |
|---|---|
| `/` | Application |
| `/config` | Public front-end config, ACH presets, assumption labels |
| `/healthz` | Liveness probe + engine self-check |
| `/calculate` | Run the model. See below. |
| `/export.xlsx` | Same parameters; returns an Excel workbook |

### `/calculate` parameters

| Parameter | Default | Meaning |
|---|---|---|
| `address` | required | Free text, geocoded via Mapbox |
| `f_cold` | required | Temperature factor, cavity face of the existing pane |
| `f_warm` | estimated | Same for the new IGU. Supply it if WINDOW gives it. |
| `u_assembly` | 1.7 | W/m²K. Only used to estimate `f_warm`. |
| `r_cavity` | 0.17 | m²K/W. Only used to estimate `f_warm`. |
| `t_in` | 70 | °F, interior air |
| `rh_in` | 35 | %, interior |
| `ach` | `moderate` | A preset name **or any number** |
| `vent_interior` | 1.0 | 1 = vents to room, 0 = vents to outdoors |
| `width_in`, `height_in`, `offset_in` | — / — / 0.6024 | Enables per-window litres |
| `sweep` | false | Also run every ACH preset |

### Excel export

`/export.xlsx` takes the identical parameters and returns a three-tab workbook:

| Tab | Contents |
|---|---|
| Summary | Inputs, location, results, assumptions |
| ACH Sweep | One row per air-change rate |
| Hourly Data | 8,760 rows of model output, filterable |

Every figure on the Summary tab is a **live formula** over the Hourly Data tab,
not a value copied out of Python. The workbook recalculates, so a reviewer can
confirm the headline numbers rather than take them on trust. A test recalculates
the file with LibreOffice and asserts every formula lands on the engine's value
to 1e-9.

Example:

```
/calculate?address=277+Park+Avenue,+New+York,+NY&f_cold=0.013
  &u_assembly=0.70&ach=moderate&width_in=60&height_in=96&sweep=true
```

## Validation anchors

| Case | Expected | Source |
|---|---|---|
| Dew point, 70 °F / 35% RH | 41.09 °F | Live condensation_calc |
| p_ws at 25 °C | 3169.2 Pa | ASHRAE Fundamentals Ch.1 table |
| Condensation hours, 277 Park, f = 0.300, 70 °F / 35% RH | 676 / 8,760 | Live condensation_calc |
| Hourly surface temps vs live app, real TMY | < 0.01 °F max error | Live condensation_calc |

**The 676 anchor is met exactly** on real NSRDB TMY data
(`test_room_dew_point_criterion_reproduces_676_hours`). Our engine scores the
room-dew-point criterion as a by-product of every run and lands on the same
integer, not merely close to it.

Note that our own `hours_condensing` is *higher* than 676, and correctly so:
the retained condensate film keeps the cavity saturated after room air alone
would have stopped condensing. The old model has no liquid inventory and
cannot represent that. Do not "fix" this by forcing agreement.

## Unvalidated assumptions

Four numbers do real work and none is measured. All are labelled in code and
surfaced in the `/config` and `/calculate` responses.

| Assumption | Value | How to fix |
|---|---|---|
| ACH presets | 0.1 – 100 | Tracer-gas or pressure-decay test |
| Retained condensate film | 0.1 kg/m² | Lab or literature |
| `f_warm` estimator | `f_cold + R_cav·U` | Ask Arnold — WINDOW reports it directly |
| 277 Park module dimensions | 1.5 × 2.5 m | Real curtain wall dimensions |
