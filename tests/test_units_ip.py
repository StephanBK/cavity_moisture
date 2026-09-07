"""U-factor in US customary units at the API boundary.

US practice quotes U-factor in Btu/(hr*ft^2*degF), not W/(m^2*K). The engine
stays SI throughout (design rule 1); IP is converted at the boundary exactly as
t_in and width_in already are.

THE TRAP THIS GUARDS
    f_warm = f_cold + r_cavity * u_assembly is DIMENSIONLESS. R and U convert
    by the same factor in opposite directions, so the product is invariant -
    but converting only one of them yields an f_warm wrong by 5.68x with no
    error raised. These tests pin both the factor and the invariance.

Sources: [SPEC]     ASHRAE / NFRC quote U-factor in IP
          [NUMERICS] 1 Btu/(hr*ft^2*degF) = 5.678263... W/(m^2*K)
          [API]      u_ip is additive; u_assembly keeps working

Doc ID: ANLY-002 R1.1
"""

from __future__ import annotations

import os

import pytest

from engine.cavity import f_warm_estimate
from engine.psychro import (
    W_M2K_PER_BTU_HR_FT2_F,
    r_ip_to_si,
    r_si_to_ip,
    u_ip_to_si,
    u_si_to_ip,
)


# ---------------------------------------------------------------------------
# The factor  [NUMERICS]
# ---------------------------------------------------------------------------

def test_the_conversion_factor_is_correct():
    assert W_M2K_PER_BTU_HR_FT2_F == pytest.approx(5.678263, abs=1e-6)


@pytest.mark.parametrize("u_si,u_ip", [(1.7, 0.2994), (0.7, 0.1233), (5.678263, 1.0)])
def test_known_u_pairs(u_si, u_ip):
    assert u_si_to_ip(u_si) == pytest.approx(u_ip, abs=1e-4)
    assert u_ip_to_si(u_ip) == pytest.approx(u_si, rel=1e-3)


def test_u_round_trips():
    for u in (0.05, 0.12, 0.30, 1.0):
        assert u_si_to_ip(u_ip_to_si(u)) == pytest.approx(u)


def test_r_converts_in_the_opposite_direction():
    """R rises going to IP where U falls. 0.17 m2K/W = 0.9653 hr*ft2*degF/Btu."""
    assert r_si_to_ip(0.17) == pytest.approx(0.9653, abs=1e-4)
    assert r_ip_to_si(0.9653) == pytest.approx(0.17, rel=1e-3)


# ---------------------------------------------------------------------------
# The invariance that makes this safe  [NUMERICS]
# ---------------------------------------------------------------------------

def test_r_times_u_is_invariant_across_unit_systems():
    """The whole reason a half-converted pair fails silently."""
    for r_si, u_si in ((0.17, 1.7), (0.17, 0.7), (0.05, 3.0)):
        assert r_si_to_ip(r_si) * u_si_to_ip(u_si) == pytest.approx(r_si * u_si)


def test_f_warm_is_unchanged_when_both_are_converted():
    si = f_warm_estimate(0.300, 0.17, 1.7)
    ip_pair = f_warm_estimate(0.300, r_ip_to_si(r_si_to_ip(0.17)),
                              u_ip_to_si(u_si_to_ip(1.7)))
    assert ip_pair == pytest.approx(si)


def test_converting_only_u_is_caught_by_the_engine():
    """Half-converting the pair inflates f_warm by 5.68x. Here that pushes it
    past 1.0 and f_warm_estimate refuses, which is the desired behaviour: a
    loud failure rather than a plausible wrong number. The guard is not
    universal - it only fires when the inflated value exceeds 1.0 - which is
    why the conversion happens once, at the boundary, and is pinned above."""
    correct = f_warm_estimate(0.300, 0.17, 1.7)
    assert correct < 1.0
    with pytest.raises(ValueError, match="same assembly"):
        f_warm_estimate(0.300, 0.17, u_ip_to_si(1.7))


def test_a_half_conversion_that_stays_below_one_is_still_wrong():
    """Small U values slip under the guard, so the boundary conversion is the
    real defence. 0.05 -> 0.284 W/m2K inflates f_warm without tripping it."""
    correct = f_warm_estimate(0.300, 0.17, 0.05)
    half_done = f_warm_estimate(0.300, 0.17, u_ip_to_si(0.05))
    assert half_done < 1.0
    assert (half_done - 0.300) / (correct - 0.300) == pytest.approx(
        W_M2K_PER_BTU_HR_FT2_F, rel=1e-6
    )


# ---------------------------------------------------------------------------
# The API boundary  [API]
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    os.environ.setdefault("MAPBOX_TOKEN", "pk.test")
    os.environ.setdefault("NREL_API_KEY", "test")
    import app as flask_app
    from engine.weather import Location, WeatherYear

    year = WeatherYear(t_out_c=[-5.0] * 8760, rh_out=[0.8] * 8760, elevation_m=20.0)
    loc = Location("q", "277 PARK AVE", 40.7568, -73.9742)
    monkeypatch.setattr(
        flask_app, "get_weather_for_address", lambda *a, **k: (loc, year, False)
    )
    return flask_app.app.test_client()


def _inputs(client, query):
    return client.get("/calculate?address=x&f_cold=0.300&" + query).get_json()["inputs"]


def test_u_ip_is_converted_to_si(client):
    got = _inputs(client, "u_ip=0.30")
    # 0.30 Btu/hr*ft2*degF is 1.7035 W/m2K; 1.7 SI is 0.2994 IP.
    assert got["u_assembly"] == pytest.approx(1.7035, abs=1e-3)
    assert got["u_assembly_ip"] == pytest.approx(0.30, abs=1e-3)


def test_u_ip_and_equivalent_u_assembly_agree(client):
    a = _inputs(client, "u_ip=0.2994")
    b = _inputs(client, "u_assembly=1.7")
    assert a["f_warm"] == pytest.approx(b["f_warm"], abs=1e-4)


def test_the_si_parameter_still_works(client):
    """Old URLs and saved reports must not be silently reinterpreted. [API]"""
    got = _inputs(client, "u_assembly=1.7")
    assert got["u_assembly"] == pytest.approx(1.7)


def test_u_ip_wins_when_both_are_supplied(client):
    got = _inputs(client, "u_ip=0.12&u_assembly=1.7")
    assert got["u_assembly"] == pytest.approx(0.6814, rel=1e-3)


def test_both_unit_systems_are_echoed(client):
    got = _inputs(client, "u_ip=0.12")
    for key in ("u_assembly", "u_assembly_ip", "r_cavity", "r_cavity_ip"):
        assert key in got, key


def test_r_ip_is_converted_too(client):
    got = _inputs(client, "u_ip=0.30&r_ip=0.9653")
    assert got["r_cavity"] == pytest.approx(0.17, rel=1e-3)


def test_an_si_value_pasted_into_the_ip_field_is_rejected(client):
    """1.7 Btu/hr*ft2*degF is 9.65 W/m2K - not a window. The cap catches the
    most likely paste error rather than returning a plausible wrong answer."""
    r = client.get("/calculate?address=x&f_cold=0.300&u_ip=1.7")
    assert r.status_code == 400


def test_the_frontend_sends_ip():
    """[A11Y] The field label and the parameter must not disagree."""
    from pathlib import Path

    html = Path(__file__).resolve().parents[1].joinpath("static/index.html").read_text()
    assert "u_ip:inp.u_ip" in html
    assert "Btu/hr" in html
    assert "W/m²K)'" not in html, "an SI label survived in the rail"
