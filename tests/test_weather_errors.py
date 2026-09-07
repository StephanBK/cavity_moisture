"""Error-path contract for the weather layer.

Every failure reaching the caller must be a WeatherError carrying an actionable
message, and no message anywhere may contain a credential. requests embeds the
full request URL in its exception messages; that URL carries api_key, so an
unhandled exception would print the NREL key into Railway's retained logs.

Sources: [API] observed NLR/api.data.gov behaviour, September 2026
          [SPEC] ANLY-002 R1.0 secrets rule - NREL_API_KEY is server-side only

Doc ID: ANLY-002 R1.0, Chunk 6 hardening
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from engine import weather
from engine.weather import (
    WeatherError,
    describe_upstream_error,
    fetch_tmy,
    geocode,
    scrub_secrets,
)

SECRET = "5sVvd5l8ZLUjF0Vh9gjKSKjHLegzyeoHzhI73fkN"


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        import json as _json

        return _json.loads(self.text)


# ---------------------------------------------------------------------------
# scrub_secrets  [SPEC]
# ---------------------------------------------------------------------------

def test_scrub_removes_an_api_key_from_a_url():
    url = f"https://x/api.csv?wkt=POINT(1 2)&api_key={SECRET}&email=a@b.com"
    out = scrub_secrets(url)
    assert SECRET not in out
    assert "api_key=REDACTED" in out
    # Neighbouring parameters survive - the message stays diagnosable.
    assert "email=a@b.com" in out


def test_scrub_removes_a_mapbox_token():
    assert "pk.secret" not in scrub_secrets("...?q=x&access_token=pk.secret")


def test_scrub_handles_a_key_at_the_end_of_a_string():
    assert scrub_secrets(f"api_key={SECRET}") == "api_key=REDACTED"


def test_scrub_is_a_no_op_when_there_is_no_secret():
    assert scrub_secrets("plain message") == "plain message"


# ---------------------------------------------------------------------------
# describe_upstream_error  [API]
# ---------------------------------------------------------------------------

def test_json_error_body_is_summarised():
    body = '{"errors":["The required \'email\' parameter must be a valid email address"],"status":400}'
    msg = describe_upstream_error(400, body)
    assert "HTTP 400" in msg
    assert "valid email address" in msg


def test_csv_error_body_is_summarised():
    """A .csv request returns a CSV error table, not JSON. [API]"""
    body = 'Error Code,Error Message\n"API_KEY_MISSING","No api_key was supplied."\n'
    msg = describe_upstream_error(403, body)
    assert "HTTP 403" in msg
    assert "API_KEY_MISSING" in msg


def test_unrecognised_body_still_produces_a_message():
    msg = describe_upstream_error(500, "<html>Server Error</html>")
    assert "HTTP 500" in msg


def test_empty_body_does_not_crash():
    assert "HTTP 502" in describe_upstream_error(502, "")


def test_error_body_containing_a_key_is_scrubbed():
    body = f'{{"errors":["bad request for api_key={SECRET}"]}}'
    assert SECRET not in describe_upstream_error(400, body)


# ---------------------------------------------------------------------------
# fetch_tmy failure modes  [API]
# ---------------------------------------------------------------------------

@pytest.fixture
def nocache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


def _raise(exc):
    def _inner(*args, **kwargs):
        raise exc

    return _inner


def test_connection_error_becomes_a_weather_error_without_the_key(monkeypatch, nocache):
    boom = requests.ConnectionError(
        f"HTTPSConnectionPool(host='developer.nlr.gov'): url: /api.csv?api_key={SECRET}"
    )
    monkeypatch.setattr(weather.requests, "get", _raise(boom))
    with pytest.raises(WeatherError) as err:
        fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)
    assert SECRET not in str(err.value)


def test_timeout_becomes_a_weather_error(monkeypatch, nocache):
    monkeypatch.setattr(weather.requests, "get", _raise(requests.Timeout()))
    with pytest.raises(WeatherError, match="did not respond"):
        fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)


def test_a_400_reports_the_service_complaint(monkeypatch, nocache):
    body = '{"errors":["The required \'email\' parameter must be a valid email address"]}'
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: FakeResponse(400, body))
    with pytest.raises(WeatherError, match="valid email address"):
        fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)


def test_a_404_names_the_url_and_suggests_the_cause(monkeypatch, nocache):
    """The psm3-tmy-download path was retired; a 404 must say so. [API]"""
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: FakeResponse(404, "Not Found."))
    with pytest.raises(WeatherError) as err:
        fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)
    assert "404" in str(err.value)
    assert "superseded" in str(err.value)


def test_403_and_429_keep_their_specific_messages(monkeypatch, nocache):
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: FakeResponse(403, ""))
    with pytest.raises(WeatherError, match="NREL_API_KEY"):
        fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: FakeResponse(429, ""))
    with pytest.raises(WeatherError, match="rate limit"):
        fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)


def test_no_failure_mode_leaks_the_key(monkeypatch, nocache):
    """Sweep every status the service can return. [SPEC]"""
    for status in (400, 401, 403, 404, 429, 500, 502, 503):
        monkeypatch.setattr(
            weather.requests,
            "get",
            lambda *a, s=status, **k: FakeResponse(s, f"error with api_key={SECRET}"),
        )
        with pytest.raises(WeatherError) as err:
            fetch_tmy(40.75, -73.97, api_key=SECRET, cache_dir=nocache)
        assert SECRET not in str(err.value), f"key leaked on HTTP {status}"


# ---------------------------------------------------------------------------
# geocode failure modes  [API]
# ---------------------------------------------------------------------------

def test_geocode_connection_error_hides_the_token(monkeypatch):
    boom = requests.ConnectionError("failed for url: /forward?access_token=pk.secret")
    monkeypatch.setattr(weather.requests, "get", _raise(boom))
    with pytest.raises(WeatherError) as err:
        geocode("277 Park Avenue", token="pk.secret")
    assert "pk.secret" not in str(err.value)


def test_geocode_non_ok_status_is_a_weather_error(monkeypatch):
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: FakeResponse(422, "bad query"))
    with pytest.raises(WeatherError, match="422"):
        geocode("277 Park Avenue", token="pk.secret")


def test_geocode_non_json_body_is_a_weather_error(monkeypatch):
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: FakeResponse(200, "<html>"))
    with pytest.raises(WeatherError, match="not JSON"):
        geocode("277 Park Avenue", token="pk.secret")
