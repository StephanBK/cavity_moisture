"""
AUDIT SUITE - weather layer and HTTP API
=========================================

  [NSRDB]    the documented NSRDB PSM3 CSV contract
  [LIVE]     real NSRDB data pulled through the deployed condensation_calc
  [DERIVED]  arithmetic worked longhand in the docstring
  [PHYSICS]  a bound the answer must satisfy
  [API]      the HTTP contract this service exposes

No test here touches the network. The NSRDB parser is a pure function tested
against a synthetic CSV built to the documented format, and the end-to-end
anchor uses a stored fixture of real NSRDB data.

Doc ID: ANLY-002 R1.0, Chunk 3b
"""

import json
import pathlib

import pytest

import app as flask_app
from engine.cavity import f_warm_estimate
from engine.geometry import CavityGeometry
from engine.moisture import ACH_PRESETS, run_year
from engine.psychro import c_to_f, f_to_c
from engine.weather import (
    WeatherError,
    WeatherYear,
    _cache_key,
    parse_nsrdb_csv,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _nsrdb_csv(hours=8760, t_start=0.0, rh=50.0, pressure=1013.0, elevation=20):
    """Build a synthetic NSRDB PSM3 TMY CSV to the documented format.

    Row 1 metadata names, row 2 metadata values, row 3 column headers,
    rows 4+ hourly data.
    """
    meta_names = (
        "Source,Location ID,City,State,Country,Latitude,Longitude,Time Zone,"
        "Elevation,Local Time Zone"
    )
    meta_values = f"NSRDB,149629,New York,New York,United States,40.77,-73.98,-5,{elevation},-5"
    header = "Year,Month,Day,Hour,Minute,Temperature,Relative Humidity,Pressure"
    lines = [meta_names, meta_values, header]
    for i in range(hours):
        t = t_start + 10.0 * ((i % 24) / 24.0)
        lines.append(f"2020,1,1,{i % 24},0,{t:.2f},{rh:.1f},{pressure:.1f}")
    return "\n".join(lines) + "\n"


# ===========================================================================
# 1. NSRDB PARSING
# ===========================================================================


def test_parses_a_full_year():
    """[NSRDB] The happy path: 8,760 hours, metadata extracted."""
    year = parse_nsrdb_csv(_nsrdb_csv())
    assert year.hours == 8760
    assert year.elevation_m == pytest.approx(20.0)
    assert year.grid_lat == pytest.approx(40.77)
    assert year.grid_lon == pytest.approx(-73.98)
    assert year.time_zone == pytest.approx(-5.0)
    assert year.station_id == "149629"


def test_relative_humidity_is_converted_from_percent_to_fraction():
    """[NSRDB] NSRDB reports RH in percent; the engine requires a fraction.

    Getting this wrong by a factor of 100 would not raise - w_from_t_rh would,
    but only after the value had already propagated. Convert at the boundary.
    """
    year = parse_nsrdb_csv(_nsrdb_csv(rh=65.0))
    assert year.rh_out[0] == pytest.approx(0.65)
    assert all(0.0 <= r <= 1.0 for r in year.rh_out)


def test_pressure_is_converted_from_millibar_to_pascal():
    """[DERIVED] NSRDB reports pressure in mbar. 1013 mbar = 101300 Pa."""
    year = parse_nsrdb_csv(_nsrdb_csv(pressure=1013.0))
    assert year.surface_pressure_pa[0] == pytest.approx(101300.0)


def test_columns_are_located_by_name_not_position():
    """[NSRDB] NSRDB orders columns according to the attributes requested, so
    positional indexing would silently mis-read the file the first time anyone
    edits NSRDB_ATTRIBUTES. Reordering the header must not change the result.
    """
    csv_text = _nsrdb_csv()
    lines = csv_text.split("\n")
    lines[2] = "Year,Month,Day,Hour,Minute,Relative Humidity,Temperature,Pressure"
    rows = []
    for line in lines[3:]:
        if not line:
            continue
        f = line.split(",")
        rows.append(",".join([f[0], f[1], f[2], f[3], f[4], f[6], f[5], f[7]]))
    reordered = "\n".join(lines[:3] + rows) + "\n"

    original = parse_nsrdb_csv(csv_text)
    swapped = parse_nsrdb_csv(reordered)
    assert swapped.t_out_c == original.t_out_c
    assert swapped.rh_out == original.rh_out


def test_json_error_response_is_surfaced_not_parsed_as_csv():
    """[NSRDB] A bad API key returns JSON, not CSV. Reporting 'expected 8760
    hours' would send the user chasing the wrong problem."""
    body = json.dumps({"errors": ["API_KEY_INVALID"]})
    with pytest.raises(WeatherError, match="API_KEY_INVALID"):
        parse_nsrdb_csv(body)


def test_short_response_is_rejected():
    """[NSRDB] A truncated download must fail loudly rather than run the model
    on a partial year."""
    with pytest.raises(WeatherError, match="returned 100 hours"):
        parse_nsrdb_csv(_nsrdb_csv(hours=100))


def test_empty_response_is_rejected():
    with pytest.raises(WeatherError, match="empty"):
        parse_nsrdb_csv("")


def test_missing_required_column_is_named_in_the_error():
    """[NSRDB] The error must say WHICH column is missing."""
    lines = _nsrdb_csv().split("\n")
    lines[2] = "Year,Month,Day,Hour,Minute,Pressure"
    with pytest.raises(WeatherError, match="Temperature"):
        parse_nsrdb_csv("\n".join(lines))


def test_leap_year_length_is_accepted():
    """[NSRDB] 8,784 hours is a valid leap year, not a corrupt file."""
    assert parse_nsrdb_csv(_nsrdb_csv(hours=8784)).hours == 8784


# ===========================================================================
# 2. WeatherYear
# ===========================================================================


def test_mismatched_series_lengths_rejected():
    with pytest.raises(WeatherError, match="humidity"):
        WeatherYear(t_out_c=[1.0, 2.0, 3.0], rh_out=[0.5, 0.5])


def test_empty_series_rejected():
    with pytest.raises(WeatherError, match="empty"):
        WeatherYear(t_out_c=[], rh_out=[])


def test_describe_is_json_safe():
    year = parse_nsrdb_csv(_nsrdb_csv())
    d = year.describe()
    assert d["hours"] == 8760
    assert all(isinstance(v, (int, float, str, type(None))) for v in d.values())


# ===========================================================================
# 3. CACHING
# ===========================================================================


def test_nearby_coordinates_share_a_cache_entry():
    """[DERIVED] The NSRDB grid is about 4 km, so two addresses on the same
    block must not trigger two downloads. Rounding to 2 decimal places is
    about 1 km."""
    assert _cache_key(40.7568, -73.9742) == _cache_key(40.7571, -73.9739)


def test_distant_coordinates_do_not_share_a_cache_entry():
    """[PHYSICS] New York must not be served Boston's weather."""
    assert _cache_key(40.75, -73.97) != _cache_key(42.36, -71.06)


# ===========================================================================
# 4. THE REAL-DATA ANCHOR
# ===========================================================================


@pytest.fixture(scope="module")
def real_tmy():
    """Real NSRDB TMY for 277 Park Avenue, pulled through the deployed
    condensation_calc app. Stored so this anchor runs offline and in CI."""
    path = FIXTURES / "nsrdb_277park_tmy.json"
    if not path.exists():
        pytest.skip("NSRDB fixture not present")
    return json.loads(path.read_text())


def test_fixture_is_a_complete_year(real_tmy):
    assert len(real_tmy["t_out_f"]) == 8760
    assert real_tmy["live_summary"]["hours_all"] == 676


def test_surface_temperatures_match_the_live_app_on_real_weather(real_tmy):
    """[LIVE] Our f-value chain against an independent implementation, hour by
    hour on real data. Agreement is limited only by the live app's JSON
    rounding to 0.01 degF.
    """
    from engine.cavity import t_from_f

    t_in_c = f_to_c(70.0)
    errors = [
        abs(c_to_f(t_from_f(0.300, t_out_c=f_to_c(t_out), t_in_c=t_in_c)) - t_surf)
        for t_out, t_surf in zip(real_tmy["t_out_f"], real_tmy["t_surf_f_at_f0300"])
    ]
    assert max(errors) < 0.01


def test_room_dew_point_criterion_reproduces_676_hours(real_tmy):
    """[LIVE] THE regression anchor, on real NSRDB weather.

    The deployed condensation_calc reports 676 condensing hours for 277 Park at
    f = 0.300, 70 degF / 35% RH. Our engine scores the same criterion as a
    by-product of every run, and must land on the same integer. Not close - the
    same.
    """
    t_out_c = [f_to_c(t) for t in real_tmy["t_out_f"]]
    rh_out = [0.7] * len(t_out_c)  # unused when venting to the interior
    summary = run_year(
        t_out_c,
        rh_out,
        f_cold=0.300,
        f_warm=f_warm_estimate(0.300, 0.17, 1.70),
        ach=5.0,
        t_room_c=f_to_c(70.0),
        rh_room=0.35,
        keep_hours=False,
    )
    assert summary.hours_condensing_room_assumption == 676


def test_vig_is_materially_worse_than_ig_on_real_weather(real_tmy):
    """[PHYSICS] The commercial finding, on real data rather than synthetic.

    At f = 0.013 the existing pane is stranded near outdoor temperature, so the
    room-dew-point criterion rises from 676 to about 2,639 hours - roughly four
    times worse.
    """
    t_out_c = [f_to_c(t) for t in real_tmy["t_out_f"]]
    rh_out = [0.7] * len(t_out_c)
    common = dict(
        ach=5.0, t_room_c=f_to_c(70.0), rh_room=0.35, keep_hours=False
    )
    ig = run_year(
        t_out_c, rh_out, f_cold=0.300, f_warm=f_warm_estimate(0.300, 0.17, 1.70), **common
    )
    vig = run_year(
        t_out_c, rh_out, f_cold=0.013, f_warm=f_warm_estimate(0.013, 0.17, 0.70), **common
    )
    assert vig.hours_condensing_room_assumption > 3 * ig.hours_condensing_room_assumption
    assert vig.total_condensed_kg_per_m2 > ig.total_condensed_kg_per_m2


def test_sealing_beats_interior_venting_on_real_weather(real_tmy):
    """[PHYSICS] The headline result, confirmed against real NSRDB data.

    Room air is far wetter in absolute terms than a sub-freezing surface can
    hold, so venting to the interior supplies the very water that condenses.
    Condensed mass must rise monotonically with air change rate.
    """
    t_out_c = [f_to_c(t) for t in real_tmy["t_out_f"]]
    rh_out = [0.7] * len(t_out_c)
    masses = [
        run_year(
            t_out_c, rh_out, f_cold=0.013,
            f_warm=f_warm_estimate(0.013, 0.17, 0.70),
            ach=a, t_room_c=f_to_c(70.0), rh_room=0.35, keep_hours=False,
        ).total_condensed_kg_per_m2
        for a in (0.1, 0.5, 5.0, 20.0, 100.0)
    ]
    assert all(b > a for a, b in zip(masses, masses[1:]))
    assert masses[-1] / masses[0] > 100


def test_per_window_litres_are_reportable_on_real_weather(real_tmy):
    """[DERIVED] The number a building operator can actually act on."""
    t_out_c = [f_to_c(t) for t in real_tmy["t_out_f"]]
    rh_out = [0.7] * len(t_out_c)
    g = CavityGeometry.from_inches(60, 96, 0.6024, "Type A")
    r = run_year(
        t_out_c, rh_out, f_cold=0.013, f_warm=f_warm_estimate(0.013, 0.17, 0.70),
        ach=5.0, t_room_c=f_to_c(70.0), rh_room=0.35, geometry=g, keep_hours=False,
    )
    assert r.total_condensed_litres_per_window > 0.0
    assert r.total_condensed_litres_per_window == pytest.approx(
        r.total_condensed_kg_per_m2 * g.glazing_area_m2, rel=1e-9
    )


# ===========================================================================
# 5. HTTP API
# ===========================================================================


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def test_healthz_smoke_tests_the_engine(client):
    """[API] A deploy that computes dew point wrong must fail the health check
    rather than quietly serve bad numbers."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["engine_check"]["actual_f"] == pytest.approx(41.09, abs=0.05)


def test_config_never_exposes_the_nrel_key(client, monkeypatch):
    """[API] MAPBOX_TOKEN is publishable. NREL_API_KEY is not, and must never
    appear in a browser-facing payload - only a boolean saying it is present."""
    monkeypatch.setenv("NREL_API_KEY", "secret-value-do-not-leak")
    monkeypatch.setenv("MAPBOX_TOKEN", "pk.public")
    body = client.get("/config").get_json()
    assert "secret-value-do-not-leak" not in json.dumps(body)
    assert body["weather_configured"] is True
    assert body["mapbox_token"] == "pk.public"


def test_config_publishes_the_presets_and_flags_them_as_estimates(client):
    """[API] The presets reach a customer-facing UI, so the caveat must travel
    with them rather than living only in a docstring."""
    body = client.get("/config").get_json()
    assert body["ach_presets"]["moderate"] == 5.0
    assert body["ach_presets_are_estimates"] is True
    assert any("estimate" in a.lower() for a in body["assumptions"])


def test_calculate_requires_an_address(client):
    resp = client.get("/calculate?f_cold=0.3")
    assert resp.status_code == 400
    assert "address" in resp.get_json()["message"]


def test_calculate_requires_f_cold(client):
    resp = client.get("/calculate?address=277+Park+Ave")
    assert resp.status_code == 400
    assert "f_cold" in resp.get_json()["message"]


@pytest.mark.parametrize(
    "query,fragment",
    [
        ("address=x&f_cold=1.5", "at most"),
        ("address=x&f_cold=-0.1", "at least"),
        ("address=x&f_cold=abc", "must be a number"),
        ("address=x&f_cold=0.3&ach=nonsense", "must be a number or one of"),
        ("address=x&f_cold=0.3&ach=-5", "cannot be negative"),
        ("address=x&f_cold=0.3&vent_interior=2", "at most"),
        ("address=x&f_cold=0.3&rh_in=150", "at most"),
        ("address=x&f_cold=0.5&f_warm=0.2", "cannot be colder"),
    ],
)
def test_calculate_validates_inputs_before_calling_the_network(client, query, fragment):
    """[API] Every one of these must fail on validation, not on a failed
    weather lookup - no NREL_API_KEY is set in the test environment, so a 502
    would mean validation ran too late."""
    resp = client.get(f"/calculate?{query}")
    assert resp.status_code == 400
    assert fragment in resp.get_json()["message"]


def test_calculate_reports_missing_credentials_as_a_gateway_error(client, monkeypatch):
    """[API] A missing API key is a server configuration problem (502), not a
    user input problem (400). The message must say which variable is missing."""
    monkeypatch.delenv("MAPBOX_TOKEN", raising=False)
    monkeypatch.delenv("NREL_API_KEY", raising=False)
    resp = client.get("/calculate?address=277+Park+Ave&f_cold=0.3")
    assert resp.status_code == 502
    assert "MAPBOX_TOKEN" in resp.get_json()["message"]


def test_ach_accepts_both_presets_and_free_values(client):
    """[API] ACH is a free input so vent designs that match no preset can be
    tested. Verified through the parser rather than a full request."""
    with flask_app.app.test_request_context("/calculate?ach=moderate"):
        assert flask_app._ach() == 5.0
    with flask_app.app.test_request_context("/calculate?ach=12.5"):
        assert flask_app._ach() == 12.5
    with flask_app.app.test_request_context("/calculate"):
        assert flask_app._ach() == 5.0  # default


def test_geometry_is_optional_and_parsed_from_inches(client):
    """[API] Windows are specified in inches. Omitting them must yield None
    rather than a fabricated default."""
    with flask_app.app.test_request_context("/calculate"):
        assert flask_app._geometry() is None
    with flask_app.app.test_request_context(
        "/calculate?width_in=60&height_in=96&offset_in=0.6024"
    ):
        g = flask_app._geometry()
        assert g.glazing_area_m2 * 10.7639 == pytest.approx(40.0, abs=0.01)
        assert g.offset_m == pytest.approx(0.0153, abs=0.0001)


# ===========================================================================
# 6. END TO END - full payload against real 277 Park weather
# ===========================================================================


@pytest.fixture
def stubbed_weather(real_tmy):
    """Patch the network layer with the stored real NSRDB year, so the whole
    request path is exercised without an API key."""
    from unittest.mock import patch

    from engine.weather import Location

    year = WeatherYear(
        t_out_c=[f_to_c(t) for t in real_tmy["t_out_f"]],
        rh_out=[0.7] * 8760,
        elevation_m=20.0,
        grid_lat=40.77,
        grid_lon=-73.98,
        time_zone=-5.0,
        station_id="149629",
    )
    location = Location(
        query="277 Park Ave",
        matched_address="277 PARK AVE, NEW YORK, NY, 10022",
        lat=40.756824,
        lon=-73.974155,
    )
    with patch("app.get_weather_for_address", return_value=(location, year, True)):
        yield


def test_calculate_returns_a_complete_payload(client, stubbed_weather):
    """[API] The response contract the frontend and Excel export depend on."""
    resp = client.get(
        "/calculate?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70"
        "&ach=moderate&width_in=60&height_in=96"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) >= {
        "inputs", "location", "weather", "surfaces_at_nfrc_winter_f",
        "summary", "geometry", "assumptions",
    }
    assert body["weather"]["hours"] == 8760
    assert body["inputs"]["f_warm"] == pytest.approx(0.132, abs=0.001)
    assert "estimated" in body["inputs"]["f_warm_source"]
    assert body["geometry"]["glazing_area_ft2"] == pytest.approx(40.0, abs=0.01)


def test_calculate_reports_per_window_litres(client, stubbed_weather):
    """[API] The number a building operator can act on, not g/m2."""
    resp = client.get(
        "/calculate?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70"
        "&ach=moderate&width_in=60&height_in=96"
    )
    per_window = resp.get_json()["summary"]["per_window"]
    assert per_window["condensed_litres_per_year"] > 0.0
    assert per_window["glazing_area_m2"] == pytest.approx(3.7161, abs=0.001)


def test_calculate_omits_per_window_without_dimensions(client, stubbed_weather):
    """[API] No dimensions means no per-window totals. Fabricating a default
    window would be worse than omitting the field."""
    resp = client.get("/calculate?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70")
    assert "per_window" not in resp.get_json()["summary"]
    assert "geometry" not in resp.get_json()


def test_sweep_covers_every_preset_and_mass_rises_with_ach(client, stubbed_weather):
    """[API] The bracket of ANLY-002 S5.5, delivered in one request.

    Condensed MASS must rise monotonically with air change rate. Note that
    hours_condensing does NOT - a sealed cavity cycles a small trapped
    inventory across many hours. Mass is the honest headline metric; hours
    alone would mislead.
    """
    resp = client.get(
        "/calculate?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70&sweep=true"
    )
    sweep = resp.get_json()["sweep"]
    assert [s["preset"] for s in sweep] == list(ACH_PRESETS)
    masses = [s["condensed_g_per_m2"] for s in sweep]
    assert all(b > a for a, b in zip(masses, masses[1:]))


def test_supplied_f_warm_overrides_the_estimator(client, stubbed_weather):
    """[API] When Arnold provides a real WINDOW surface temperature it must
    take precedence, and the response must say so."""
    resp = client.get(
        "/calculate?address=277+Park+Ave&f_cold=0.013&f_warm=0.250"
    )
    body = resp.get_json()
    assert body["inputs"]["f_warm"] == pytest.approx(0.250)
    assert body["inputs"]["f_warm_source"] == "supplied"


def test_exterior_venting_changes_the_answer(client, stubbed_weather):
    """[PHYSICS] The design lever, exposed through the API. Venting to dry
    winter outdoor air must not produce the same result as venting to a heated
    room."""
    base = "/calculate?address=277+Park+Ave&f_cold=0.013&u_assembly=0.70&ach=5"
    interior = client.get(base + "&vent_interior=1.0").get_json()
    exterior = client.get(base + "&vent_interior=0.0").get_json()
    assert (
        exterior["summary"]["condensed_g_per_m2"]
        < interior["summary"]["condensed_g_per_m2"]
    )
