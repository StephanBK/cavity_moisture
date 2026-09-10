"""
Vented room air warming the cold pane - engine.cavity.vent_cold_surface_rise
and its wiring into run_year.

Background: the ANLY-002 companion note (Sep 8, 2026) holds the pane
temperature fixed across the ACH sweep. The Sep 10 review pointed out that
venting also warms the glass (NRC CBD-4). These tests pin the size of that
effect so the argument is numeric rather than rhetorical.
"""

from __future__ import annotations

import gzip
import math
from pathlib import Path

import pytest

from engine.cavity import CP_AIR, H_CAVITY_DEFAULT, vent_cold_surface_rise
from engine.moisture import ACH_PRESETS, run_year
from engine.psychro import dry_air_density

FIXTURE = Path(__file__).parent / "fixtures" / "nsrdb_277park_tmy_v4.csv.gz"

# Companion-note inputs: f_cold 0.300, U 0.30 IP (1.703 SI), 70F / 35 % RH.
F_COLD = 0.300
U_SI = 1.703
T_ROOM = 21.111
RH_ROOM = 0.35


# ---------------------------------------------------------------------------
# The function itself
# ---------------------------------------------------------------------------

def test_hand_calculation_matches_docstring():
    """Worked example in the docstring, redone here so it cannot drift."""
    t_out, t_room = -5.0, 21.0
    t_cold = t_out + F_COLD * (t_room - t_out)          # 2.8 degC
    ach, gap = 20.0, 0.0153

    g_out = U_SI / F_COLD
    g_in = U_SI / (1.0 - F_COLD)
    m_dot = ach * gap * dry_air_density(t_cold) / 3600.0
    g_vent = 1.0 / (1.0 / (m_dot * CP_AIR) + 1.0 / H_CAVITY_DEFAULT)
    expected = g_vent * (t_room - t_cold) / (g_out + g_in + g_vent)

    got = vent_cold_surface_rise(t_cold, t_room, F_COLD, U_SI, ach, gap_m=gap)
    assert got == pytest.approx(expected, rel=1e-12)
    # Order of magnitude the note and the review both need to agree on.
    assert 0.2 < got < 0.3


def test_free_exchange_is_under_one_degree_on_a_cold_day():
    t_cold = -5.0 + F_COLD * 26.0
    got = vent_cold_surface_rise(t_cold, 21.0, F_COLD, U_SI, 100.0)
    assert 0.8 < got < 1.1


def test_zero_at_sealed_and_monotone_in_ach():
    t_cold = 2.8
    assert vent_cold_surface_rise(t_cold, 21.0, F_COLD, U_SI, 0.0) == 0.0
    rises = [vent_cold_surface_rise(t_cold, 21.0, F_COLD, U_SI, a)
             for a in (0.1, 0.5, 5.0, 20.0, 100.0)]
    assert all(b > a for a, b in zip(rises, rises[1:]))


def test_sign_follows_room_minus_pane():
    """Summer: a pane hotter than the room is COOLED by venting, which is
    the direction the Eurac CFD shows (closed 79.2 -> open 76.1 degC)."""
    assert vent_cold_surface_rise(40.0, 24.0, F_COLD, U_SI, 20.0) < 0.0
    assert vent_cold_surface_rise(24.0, 24.0, F_COLD, U_SI, 20.0) == 0.0


def test_bounded_by_the_vent_conductance():
    """dT can never exceed what a conductance G_vent to the room could do
    on its own: G_vent/(G_out+G_in+G_vent) < 1 times the gap."""
    t_cold, t_room = -10.0, 21.0
    got = vent_cold_surface_rise(t_cold, t_room, F_COLD, U_SI, 1000.0)
    assert 0.0 < got < (t_room - t_cold)


def test_pane_pinned_to_one_air_stream_cannot_move():
    assert vent_cold_surface_rise(0.0, 21.0, 0.0, U_SI, 50.0) == 0.0
    assert vent_cold_surface_rise(21.0, 21.0, 1.0, U_SI, 50.0) == 0.0


@pytest.mark.parametrize("bad", [
    dict(ach=-1.0), dict(u_assembly=0.0), dict(gap_m=0.0), dict(h_cold=0.0),
])
def test_rejects_nonphysical_inputs(bad):
    kw = dict(t_cold_c=0.0, t_room_c=21.0, f_cold=F_COLD, u_assembly=U_SI,
              ach=5.0, gap_m=0.0153, h_cold=H_CAVITY_DEFAULT)
    kw.update(bad)
    with pytest.raises(ValueError):
        vent_cold_surface_rise(**kw)


# ---------------------------------------------------------------------------
# Wiring into run_year
# ---------------------------------------------------------------------------

def _cold_spell(n: int = 240):
    """Ten days at -5 degC and 80 % RH: every hour is a condensing hour."""
    return [-5.0] * n, [0.8] * n


def test_default_is_the_fixed_pane_model():
    """u_assembly=None must reproduce the historic model to the last bit."""
    t, rh = _cold_spell()
    a = run_year(t, rh, F_COLD, 0.59, ach=20.0, t_room_c=T_ROOM, rh_room=RH_ROOM)
    b = run_year(t, rh, F_COLD, 0.59, ach=20.0, t_room_c=T_ROOM, rh_room=RH_ROOM,
                 u_assembly=None)
    assert a.total_condensed_kg_per_m2 == b.total_condensed_kg_per_m2
    assert a.mean_vent_rise_k == 0.0 and a.max_vent_rise_k == 0.0
    assert all(h.vent_rise_k == 0.0 for h in a.hours)


def test_coupling_warms_pane_and_reduces_condensation():
    t, rh = _cold_spell()
    off = run_year(t, rh, F_COLD, 0.59, ach=20.0, t_room_c=T_ROOM, rh_room=RH_ROOM)
    on = run_year(t, rh, F_COLD, 0.59, ach=20.0, t_room_c=T_ROOM, rh_room=RH_ROOM,
                  u_assembly=U_SI)
    assert on.max_vent_rise_k > 0.0
    assert on.hours[10].t_cold_c > off.hours[10].t_cold_c
    assert on.total_condensed_kg_per_m2 < off.total_condensed_kg_per_m2
    # ...but only slightly. Under 0.4 K at 20 ACH, under 10 % on mass.
    assert on.max_vent_rise_k < 0.4
    assert on.total_condensed_kg_per_m2 > 0.9 * off.total_condensed_kg_per_m2


def test_sealed_run_is_unaffected_by_coupling():
    t, rh = _cold_spell()
    off = run_year(t, rh, F_COLD, 0.59, ach=0.0, t_room_c=T_ROOM, rh_room=RH_ROOM)
    on = run_year(t, rh, F_COLD, 0.59, ach=0.0, t_room_c=T_ROOM, rh_room=RH_ROOM,
                  u_assembly=U_SI)
    assert on.total_condensed_kg_per_m2 == off.total_condensed_kg_per_m2
    assert on.max_vent_rise_k == 0.0


# ---------------------------------------------------------------------------
# The 277 Park sweep, air-only (no solar, no sky) so it runs fast and does
# not depend on the solar module. Pins the SHAPE of the result: coupling
# trims free exchange by a third and leaves sealed alone.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not FIXTURE.exists(), reason="277 Park TMY fixture absent")
def test_277park_sweep_shape():
    from engine.weather import parse_nsrdb_csv

    w = parse_nsrdb_csv(gzip.open(FIXTURE, "rt").read())
    kw = dict(t_room_c=T_ROOM, rh_room=RH_ROOM, elevation_m=w.elevation_m,
              keep_hours=False)
    out = {}
    for name in ("sealed", "open_vent", "free"):
        a = ACH_PRESETS[name]
        off = run_year(w.t_out_c, w.rh_out, F_COLD, 0.59, ach=a, **kw)
        on = run_year(w.t_out_c, w.rh_out, F_COLD, 0.59, ach=a, u_assembly=U_SI, **kw)
        out[name] = (off.total_condensed_kg_per_m2, on.total_condensed_kg_per_m2,
                     on.max_vent_rise_k)

    s_off, s_on, s_k = out["sealed"]
    o_off, o_on, o_k = out["open_vent"]
    f_off, f_on, f_k = out["free"]

    assert s_on == pytest.approx(s_off, rel=0.01)      # sealed: unchanged
    assert 0.85 < o_on / o_off < 0.97                   # open: single-digit %
    assert 0.55 < f_on / f_off < 0.85                   # free: roughly a third
    assert s_k < 0.01 and o_k < 0.5 and f_k < 2.0
    # The note's headline survives: hundreds of times, not tens.
    assert f_on / s_on > 100.0


# ---------------------------------------------------------------------------
# API flag  [wiring]
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """Serve the 277 Park fixture in place of the live NSRDB fetch, the same
    way tests/test_solar_wiring.py does."""
    import os
    os.environ.setdefault("MAPBOX_TOKEN", "pk.test")
    os.environ.setdefault("NREL_API_KEY", "test")
    import app as flask_app
    from engine.weather import Location, parse_nsrdb_csv

    year = parse_nsrdb_csv(gzip.open(FIXTURE, "rt").read())
    loc = Location("x", "277 PARK AVE, NEW YORK, NY 10172", 40.7568, -73.9742)
    monkeypatch.setattr(
        flask_app, "get_weather_for_address", lambda *a, **k: (loc, year, False)
    )
    return flask_app.app.test_client()


BASE_Q = "address=x&f_cold=0.300&u_ip=0.30&t_in=70&rh_in=35&ach=20&vent_interior=1"


@pytest.mark.skipif(not FIXTURE.exists(), reason="277 Park TMY fixture absent")
def test_api_flag_defaults_off_and_switches_on(client):
    off = client.get("/calculate?" + BASE_Q).get_json()
    on = client.get("/calculate?" + BASE_Q + "&pane_coupling=1").get_json()
    assert off["inputs"]["pane_coupling"] is False
    assert on["inputs"]["pane_coupling"] is True
    assert off["summary"]["max_vent_rise_k"] == 0.0
    assert on["summary"]["max_vent_rise_k"] > 0.0
    assert on["summary"]["condensed_g_per_m2"] < off["summary"]["condensed_g_per_m2"]
