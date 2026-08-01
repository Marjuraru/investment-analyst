"""Offline tests for the standard-library HTTPS transport."""

from email.message import Message
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

import investment_analyst.providers.http as http_module
from investment_analyst.providers.http import (
    HttpRequestError,
    HttpRequestFailureKind,
    UrlLibHttpTransport,
)


class FakeResponse:
    """Small context-managed urllib response double."""

    def __init__(self, *, url: str = "https://example.test/data", body: bytes = b"ok") -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._url = url
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, size: int | None = None) -> bytes:
        if size is None:
            return self._body
        return self._body[:size]

    def geturl(self) -> str:
        return self._url


def _http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://example.test/data", status, "failure", headers, None)


def test_rejects_non_https_url() -> None:
    with pytest.raises(HttpRequestError, match="only HTTPS") as error:
        UrlLibHttpTransport().get(
            "http://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )

    assert error.value.failure_kind is HttpRequestFailureKind.CONFIGURATION


def test_returns_successful_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        assert timeout == 2.0
        return FakeResponse(body=b'{"ok":true}')

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    response = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={"Accept": "application/json"},
        timeout_seconds=2.0,
    )

    assert response.status_code == 200
    assert response.body == b'{"ok":true}'
    assert response.headers["Content-Type"] == "application/json"
    assert response.body_truncated is False


def test_posts_encoded_form_over_https_without_putting_fields_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: Request | None = None

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal observed
        observed = request
        assert timeout == 2.0
        return FakeResponse(body=b"<html>ok</html>")

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    response = UrlLibHttpTransport().post_form(
        "https://example.test/query",
        headers={"Accept": "text/html"},
        fields={"issuer": "MINSUR S.A.", "token": "a+b/c="},
        timeout_seconds=2.0,
        max_response_bytes=100,
    )

    assert response.body == b"<html>ok</html>"
    assert observed is not None
    assert observed.get_method() == "POST"
    assert observed.full_url == "https://example.test/query"
    assert observed.data is not None
    assert parse_qs(observed.data.decode("utf-8")) == {
        "issuer": ["MINSUR S.A."],
        "token": ["a+b/c="],
    }
    assert observed.get_header("Content-type") == (
        "application/x-www-form-urlencoded; charset=utf-8"
    )


def test_form_post_rejects_non_https_without_exposing_fields() -> None:
    with pytest.raises(HttpRequestError, match="POST http://example.test/query") as error:
        UrlLibHttpTransport().post_form(
            "http://example.test/query",
            headers={},
            fields={"secret": "must-not-appear"},
            timeout_seconds=1.0,
        )

    assert "must-not-appear" not in str(error.value)


def test_can_bound_response_body_without_reading_the_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        return FakeResponse(body=b"0123456789")

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    response = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=2.0,
        max_response_bytes=4,
    )

    assert response.body == b"0123"
    assert response.body_truncated is True


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_rejects_invalid_response_limit(limit: int | float) -> None:
    with pytest.raises(HttpRequestError, match="max_response_bytes"):
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
            max_response_bytes=limit,
        )


@pytest.mark.parametrize("status", [408, 429, 503])
def test_retries_transient_http_status(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    outcomes: list[BaseException | FakeResponse] = [_http_error(status), FakeResponse()]
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    response = UrlLibHttpTransport(sleep=sleeps.append).get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
    )

    assert response.status_code == 200
    assert len(sleeps) == 1


def test_does_not_retry_permanent_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        raise _http_error(400)

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    with pytest.raises(HttpRequestError) as error:
        UrlLibHttpTransport(sleep=lambda _: None).get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )

    assert attempts == 1
    assert error.value.status_code == 400
    assert error.value.failure_kind is HttpRequestFailureKind.HTTP_STATUS


def test_timeout_becomes_request_error_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    with pytest.raises(HttpRequestError, match="retry limit") as error:
        UrlLibHttpTransport(sleep=lambda _: None).get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )

    assert attempts == 3
    assert error.value.failure_kind is HttpRequestFailureKind.TRANSPORT


def test_invalid_retry_after_uses_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[BaseException | FakeResponse] = [
        _http_error(429, "not-a-number"),
        FakeResponse(),
    ]
    sleeps: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    UrlLibHttpTransport(sleep=sleeps.append).get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
    )

    assert sleeps == [0.1]
