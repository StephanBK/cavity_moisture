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
| 2 | Cavity air temperature from f | pending |
| 3 | Hourly moisture balance + NSRDB TMY | pending |
| 4 | ACH bracket sweep + Excel export | pending |
| 5 | Frontend: Explain / Model / Present | pending |
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

`tests/test_psychro.py` is written to be **read**, not just run. Every
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
| `/config` | Public front-end config; reports whether weather is configured |
| `/healthz` | Liveness probe + engine self-check |

## Validation anchors

| Case | Expected | Source |
|---|---|---|
| Dew point, 70 °F / 35% RH | 41.09 °F | Live condensation_calc |
| p_ws at 25 °C | 3169.2 Pa | ASHRAE Fundamentals Ch.1 table |
| Condensation hours, 277 Park, f = 0.300, 70 °F / 35% RH | 676 / 8,760 | Live condensation_calc |

That last one is the regression target for Chunk 4: at a very high air
exchange rate the cavity must track the room, so the new model **must**
reproduce ~676 hours. If it doesn't, the moisture engine is wrong.
