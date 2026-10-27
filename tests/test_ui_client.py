"""Unit tests for the reviewer interface's typed API client (no network)."""

from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from watchlist.ui.client import (
    DEFAULT_API_URL,
    ApiClientError,
    ClientConfig,
    ReviewForm,
    WatchlistClient,
    new_uuid,
)

KEY = "fictional-test-key-0123456789"
LIMITATION = "fictional limitation notice"
CASE_ID = "0b4f8a3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def mock_client(handler, *, key: str | None = KEY) -> WatchlistClient:
    http = httpx.Client(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler)
    )
    return WatchlistClient(ClientConfig("http://127.0.0.1:8000", key), http=http)


def ok(payload=None):
    def handler(request):
        handler.requests.append(request)
        return httpx.Response(200, json=payload or {"limitation": LIMITATION})

    handler.requests = []
    return handler


def test_from_env_defaults_and_values():
    config = ClientConfig.from_env({})
    assert config.base_url == DEFAULT_API_URL and config.api_key is None
    assert not config.has_key
    config = ClientConfig.from_env(
        {"WATCHLIST_API_URL": "http://localhost:9001/", "WATCHLIST_API_KEY": KEY}
    )
    assert config.base_url == "http://localhost:9001" and config.has_key


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1",
        "127.0.0.1:8000",
        "http://example.org",
        "http://10.0.0.5:8000",
        "http://user:pw@127.0.0.1",
        "http://127.0.0.1/?x=1",
    ],
)
def test_refused_urls(url):
    with pytest.raises(ApiClientError) as info:
        ClientConfig(url, KEY)
    assert info.value.code == "client_config_invalid"


@pytest.mark.parametrize("url", ["http://[::1]:8000", "https://reviews.example.test"])
def test_accepted_urls(url):
    assert ClientConfig(url, KEY).base_url == url


@pytest.mark.parametrize(
    "key", ["short", "x" * 513, "ключ-ключ-ключ-ключ", "a\nb" * 10]
)
def test_invalid_key_is_refused_without_echo(key):
    with pytest.raises(ApiClientError) as info:
        ClientConfig(DEFAULT_API_URL, key)
    assert key not in str(info.value) and key not in info.value.message


def test_key_never_in_repr():
    client = mock_client(ok())
    assert KEY not in repr(client.config) and KEY not in repr(client)
    assert "key_configured=True" in repr(client)


def test_bearer_header_and_json_body():
    handler = ok()
    client = mock_client(handler)
    request_id = new_uuid()
    client.start_case("Luma Vesper", origin="test", request_id=request_id)
    request = handler.requests[0]
    assert request.method == "POST" and request.url.path == "/v2/cases"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "name": "Luma Vesper",
        "origin": "test",
        "request_id": request_id,
    }


def test_health_sends_no_key():
    handler = ok()
    mock_client(handler).health()
    assert "authorization" not in handler.requests[0].headers


def test_missing_key_refuses_before_any_request():
    handler = ok()
    with pytest.raises(ApiClientError) as info:
        mock_client(handler, key=None).list_cases()
    assert info.value.code == "client_config_invalid"
    assert handler.requests == []


def test_review_form_body_and_list_params():
    handler = ok()
    client = mock_client(handler)
    form = ReviewForm(new_uuid(), "a" * 64, "b" * 64, "none_selected", None, "why")
    client.submit_review(CASE_ID, form)
    assert json.loads(handler.requests[0].content) == form.to_json()
    client.list_cases(limit=5, offset=10, origin=None)
    assert dict(handler.requests[1].url.params) == {"limit": "5", "offset": "10"}


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_case("../health"),
        lambda c: c.get_case("0B4F8A3E-1C2D-4E5F-8A9B-0C1D2E3F4A5B"),
        lambda c: c.audit("x/y"),
        lambda c: c.get_entity("syn-001/../../x"),
        lambda c: c.start_case("x", origin="test", request_id="not-a-uuid"),
        lambda c: c.submit_review(
            CASE_ID, ReviewForm("bad", "a" * 64, "b" * 64, "unresolved", None, "r")
        ),
    ],
)
def test_identifiers_validated_before_request(call):
    handler = ok()
    with pytest.raises(ApiClientError) as info:
        call(mock_client(handler))
    assert info.value.code == "invalid_input"
    assert handler.requests == []


def test_structured_api_error_is_preserved():
    body = {
        "code": "stale_submission",
        "message": "reload",
        "retryable": False,
        "details": {"case_id": CASE_ID},
    }
    client = mock_client(lambda request: httpx.Response(409, json=body))
    with pytest.raises(ApiClientError) as info:
        client.get_case(CASE_ID)
    error = info.value
    assert (error.status, error.code, error.message, error.retryable) == (
        409,
        "stale_submission",
        "reload",
        False,
    )
    assert error.details == {"case_id": CASE_ID}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, text="<html>"), "bad_response"),
        (httpx.Response(200, json=[1, 2]), "bad_response"),
        (httpx.Response(200, json={"case": {}}), "bad_response"),
        (httpx.Response(502, text="gateway"), "bad_response"),
        (httpx.Response(500, json={"oops": 1}), "http_error"),
    ],
)
def test_unexpected_responses(response, code):
    client = mock_client(lambda request: response)
    with pytest.raises(ApiClientError) as info:
        client.get_case(CASE_ID)
    assert info.value.code == code


def test_transport_failures_are_retryable_and_never_an_outcome():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ApiClientError) as info:
        mock_client(refuse).get_case(CASE_ID)
    assert info.value.code == "api_unreachable" and info.value.retryable
    assert info.value.status is None
    with pytest.raises(ApiClientError) as info:
        mock_client(slow).get_case(CASE_ID)
    assert info.value.code == "api_timeout" and "reload the case" in info.value.message
    assert KEY not in str(info.value)


def test_default_http_client_ignores_proxies_and_redirects():
    client = WatchlistClient(ClientConfig(DEFAULT_API_URL, KEY))
    try:
        assert client._http.follow_redirects is False
        assert client._http._trust_env is False
        assert str(client._http.base_url).rstrip("/") == DEFAULT_API_URL
    finally:
        client.close()
