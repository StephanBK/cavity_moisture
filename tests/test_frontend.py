"""
AUDIT SUITE - frontend
=======================

The page is plain HTML + React from a CDN with no build step, so there is no
compiler to catch a broken reference. These tests stand in for one: they check
the page against the API it calls and against the quality floor we committed
to, so the two cannot drift apart silently.

  [API]     the page must only call endpoints that exist
  [A11Y]    accessibility floor
  [SPEC]    behaviour we decided on deliberately

Doc ID: ANLY-002 R1.0, Chunk 5
"""

import pathlib
import re

import pytest

import app as flask_app
from engine.moisture import ACH_PRESETS

PAGE = pathlib.Path(__file__).parent.parent / "static" / "index.html"


@pytest.fixture(scope="module")
def html():
    return PAGE.read_text()


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


# ===========================================================================
# 1. THE PAGE AND THE API MUST AGREE
# ===========================================================================


def test_page_is_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Cavity Moisture" in resp.data


def test_every_endpoint_the_page_calls_exists(html, client):
    """[API] There is no build step, so a renamed route would fail only at
    runtime, in front of whoever is using it."""
    referenced = set(re.findall(r"""fetch\(['"](/[a-z.]+)""", html))
    referenced |= set(re.findall(r"""href:['"](/[a-z.]+)""", html))
    assert referenced, "page appears to call no endpoints"

    routes = {r.rule.split("?")[0] for r in flask_app.app.url_map.iter_rules()}
    for path in referenced:
        assert path in routes, f"page calls {path}, which the app does not serve"


def test_page_sends_every_parameter_the_api_needs(html):
    """[API] A missing address or f_cold returns 400, so the page must send
    both. Checked here rather than discovered in the browser."""
    for name in ("address", "f_cold", "u_assembly", "t_in", "rh_in", "ach",
                 "vent_interior", "width_in", "height_in", "offset_in"):
        assert name in html, f"page never sends {name!r}"


def test_preset_values_come_from_config_not_the_page(html):
    """[SPEC] ACH presets are estimates that will change once measured. Their
    VALUES must come from /config so there is one source of truth."""
    assert "cfg.ach_presets" in html
    for name, value in ACH_PRESETS.items():
        assert f"{name}:{value}" not in html.replace(" ", ""), (
            f"preset value for {name!r} appears hardcoded in the page"
        )


def test_preset_names_the_page_references_actually_exist(html):
    """[SPEC] The Present view names two presets directly to compute the
    sealed-versus-open ratio. Renaming a preset in the engine would break that
    silently - the ratio would just stop appearing, with no error."""
    referenced = set(re.findall(r"preset===['\"]([a-z_]+)['\"]", html))
    assert referenced, "expected the page to reference preset names"
    unknown = referenced - set(ACH_PRESETS)
    assert not unknown, f"page references presets that do not exist: {unknown}"


# ===========================================================================
# 2. THINGS WE DECIDED DELIBERATELY
# ===========================================================================


def test_ach_slider_is_logarithmic(html):
    """[SPEC] The useful range spans 0.05 to 200. On a linear slider, 95% of
    the travel would sit above ACH 20, making the sealed end unusable - and the
    sealed end is where the interesting answer is."""
    assert "posToAch" in html and "Math.pow" in html


def test_page_leads_with_mass_not_hours(html):
    """[SPEC] The finding from Chunk 3b: condensed mass rises monotonically
    with air change rate but wet HOURS does not. A UI that headlines hours
    makes a sealed cavity look worse than one delivering far more water."""
    assert "litres" in html
    assert "Read the mass column, not the hours" in html


def test_page_surfaces_the_unvalidated_assumptions(html):
    """[SPEC] Three estimates do real work. They travel with the numbers, in
    the interface, not only in a docstring."""
    assert "estimates" in html or "estimate" in html
    assert "Retained condensate film" in html
    assert "Tracer-gas" in html


def test_page_explains_the_vent_direction_result(html):
    """[SPEC] Venting to dry outdoor air can dry a cavity that room air would
    wet. It is counterintuitive enough that the control needs to say so."""
    assert "cold but very dry" in html


def test_page_offers_the_excel_export(html):
    assert "/export.xlsx" in html
    assert "Download Excel report" in html


def test_three_modes_exist(html):
    for mode in ("Explain", "Model", "Present"):
        assert f"'{mode}'" in html or f'"{mode}"' in html or f">{mode}<" in html


# ===========================================================================
# 3. QUALITY FLOOR
# ===========================================================================


def test_no_browser_storage(html):
    """[SPEC] Browser storage is unavailable in some embedding contexts, and
    the Odoo iframe is one. State lives in React."""
    assert "localStorage" not in html
    assert "sessionStorage" not in html


def test_reduced_motion_is_respected(html):
    """[A11Y] The condensation beads animate. Anyone who has asked their system
    for less motion must not get it."""
    assert "prefers-reduced-motion" in html


def test_focus_is_visible(html):
    """[A11Y] Keyboard focus must be visible; the page overrides default
    outlines, so it has to restore one."""
    assert "focus-visible" in html
    assert "outline" in html


def test_inputs_are_labelled(html):
    """[A11Y] Every control the user types into needs a label or an aria-label,
    or a screen reader announces nothing useful."""
    ids = set(re.findall(r"h\('input',\{id:'([a-z_]+)'", html))
    labelled = set(re.findall(r"htmlFor:'([a-z_]+)'", html))
    assert ids, "no identified inputs found"
    assert ids <= labelled, f"inputs without labels: {ids - labelled}"


def test_range_and_svg_carry_accessible_names(html):
    """[A11Y] A slider and an SVG are meaningless to a screen reader without
    an explicit name."""
    assert "'aria-label':'Air change rate'" in html
    assert "role:'img'" in html and "'aria-label'" in html


def test_layout_is_responsive(html):
    """[A11Y] The two-column shell must collapse rather than overflow."""
    assert "@media(max-width:820px)" in html or "@media (max-width:820px)" in html


def test_page_uses_the_project_typeface(html):
    """[SPEC] IBM Plex, matching the sibling condensation_calc app - the two
    tools sit side by side in Odoo."""
    assert "IBM+Plex+Sans" in html


def test_errors_say_what_to_do(html):
    """[SPEC] A failed run must explain itself. The API returns a message
    field; the page must render it rather than a generic failure."""
    assert "e.message" in html or "d.message" in html
    assert "className:'err'" in html


def test_empty_state_directs_the_user(html):
    """[SPEC] The first thing anyone sees is an empty stage. It should say what
    to do, not sit blank."""
    assert "Set up a window, then run the year" in html
