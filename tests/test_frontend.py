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
    for name in ("address", "f_cold", "u_ip", "t_in", "rh_in", "ach",
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


# ===========================================================================
# 4. THE SCRIPT MUST PARSE
# ===========================================================================
#
# Build session 4 found the live page blank: commit b0a2ff6 left one ternary
# parenthesis unclosed in ModelView, and a syntax error anywhere in a <script>
# block stops the whole block, so React never mounted. 342 tests passed, /calculate
# was validated live, and nobody opened the page. A page with no build step
# needs a parse check, and that is what these two tests are.


def _inline_script(html: str) -> str:
    lines = html.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == "<script>")
    end = max(i for i, l in enumerate(lines) if l.strip() == "</script>")
    return "\n".join(lines[start + 1:end])


def _js_bracket_imbalance(src: str):
    """Walk the script, ignoring strings, template literals and comments, and
    return the first unmatched bracket as (line, char) or None if balanced.

    This is not a parser. It is the narrowest check that would have caught
    b0a2ff6, written without a JavaScript dependency so it always runs. The
    node-based test below is the real thing where node is installed.
    """
    pairs = {")": "(", "}": "{", "]": "["}
    stack = []          # (bracket, line)
    mode = []           # nested template-literal state: 'tpl' or 'expr'
    i, line, n = 0, 1, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
        if mode and mode[-1] == "tpl":
            if c == "\\":
                i += 2; continue
            if c == "`":
                mode.pop()
            elif src.startswith("${", i):
                mode.append("expr"); stack.append(("${", line)); i += 2; continue
            i += 1; continue
        if src.startswith("//", i):
            j = src.find("\n", i); i = n if j < 0 else j; continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            line += src.count("\n", i, j); i = j + 2; continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1; continue
        if c == "`":
            mode.append("tpl"); i += 1; continue
        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if c == "}" and stack and stack[-1][0] == "${":
                stack.pop(); mode.pop()
            elif not stack or stack[-1][0] != pairs[c]:
                return (line, c)
            else:
                stack.pop()
        i += 1
    return (stack[-1][1], stack[-1][0]) if stack else None


def test_page_script_brackets_balance(html):
    """[SPEC] Every bracket in the inline script closes. b0a2ff6 would fail
    here at the orientations panel in ModelView."""
    bad = _js_bracket_imbalance(_inline_script(html))
    assert bad is None, f"unmatched bracket near script line {bad[0]}: {bad[1]!r}"


def test_page_script_parses_with_node(html, tmp_path):
    """[SPEC] The real parse, where node exists. Skipped elsewhere, like the
    LibreOffice recalculation test; the bracket check above always runs."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    js = tmp_path / "page.js"
    js.write_text(_inline_script(html))
    proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ===========================================================================
# 5. EXPLAIN MODE COVERS THE WHOLE MODEL
# ===========================================================================


def test_explain_mode_covers_the_surface_energy_balance(html):
    """[SPEC] Build session 3 added sky radiation, solar gain, wind and
    orientation to the engine. The explanation must describe the model that
    runs, not the air-only model it replaced."""
    assert "eight steps" in html
    assert "Swinbank" in html
    assert "5.7 + 3.8" in html
    assert "α × I_poa / h_out" in html
    assert "Cars frost on clear nights" in html


def test_explain_mode_names_the_three_hour_counts(html):
    """[SPEC] Condensing, present and visible answer different questions.
    A reader must be told which one is theirs."""
    for label in ("Condensing", "Water present", "Water visible"):
        assert label in html
    assert "what an occupant sees" in html
    assert "use mass, not hours, to rank vent designs" in html


def test_explain_mode_math_is_optional(html):
    """[SPEC] Customers read prose; engineers flip a switch. The formulas
    are behind a toggle and the plain-words paragraph is never hidden."""
    assert "Show the math" in html and "Hide the math" in html
    assert "showMath ? h('div',{className:'math'}" in html


# ===========================================================================
# 6. THE TWO EXPLAIN-MODE GRAPHICS
# ===========================================================================


def test_energy_flow_diagram_shows_every_term_including_the_ones_switched_off(html):
    """[SPEC] A term that is off is drawn grey and labelled off, never
    omitted, so the reader sees what the model could do as well as what it
    did. Both directions of the surface energy balance must be present."""
    assert "function EnergyFlow" in html
    assert "'sun  off (α = 0)'" in html
    assert "'sky radiation  off'" in html
    assert "h_out = 5.7 + 3.8 v" in html


def test_energy_flow_reference_conditions_match_the_handover(html):
    """[SPEC] Session-3 handover §6.2 quotes +19 °F at α 0.20 and −5.5 °F on a
    clear night, both at the engine's 2 m/s default wind and 700 W/m². The page
    must reproduce those magnitudes, so it uses the same reference conditions."""
    assert "hCalm=5.7+3.8*2" in html
    assert "hWind=5.7+3.8*6" in html
    assert "I=700" in html


def test_orientation_chart_is_suppressed_without_an_orientations_block(html):
    """[SPEC] Same rule as the results-page panel: four identical bars would
    say 'orientation does not matter' when the truth is 'not modelled'."""
    assert "function OrientationChart" in html
    assert "if(!rows||!rows.length) return null;" in html


def test_orientation_chart_reports_the_north_south_spread(html):
    """[SPEC] The north/south spread is the most robust result in the model
    because absorptance cancels out of it. It is stated, not left to the eye."""
    assert "visible condensation than south" in html
    assert "Trust the comparison more than any single bar" in html


# ===========================================================================
# 7. THE ODOO IFRAME MUST SEE REDEPLOYS
# ===========================================================================


def test_index_revalidates_on_every_visit(client):
    """[SPEC] The page is served inside an Odoo iframe. Chrome caches that
    copy under Odoo's site, separately from the direct URL, so a stale page
    can persist there long after a redeploy. no-cache forces an ETag check
    each visit; the API responses are not affected."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-cache"
    assert r.headers.get("ETag"), "no ETag, so no-cache would refetch the whole page every time"
