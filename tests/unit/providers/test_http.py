"""Offline tests for the standard-library HTTPS transport."""

import gzip
import hashlib
import threading
import time
from email.message import Message
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

import investment_analyst.providers.http as http_module
from investment_analyst.core.operation_control import (
    OperationCancelledError,
    OperationControl,
    operation_control_scope,
)
from investment_analyst.providers.http import (
    HttpRequestError,
    HttpRequestFailureKind,
    UrlLibHttpTransport,
)


class FakeResponse:
    """Small context-managed urllib response double."""

    def __init__(
        self,
        *,
        url: str = "https://example.test/data",
        body: bytes = b"ok",
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}
        self._url = url
        self._body = body
        self._position = 0

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, size: int | None = None) -> bytes:
        if size is None or size < 0:
            data = self._body[self._position :]
            self._position = len(self._body)
            return data
        data = self._body[self._position : self._position + size]
        self._position += len(data)
        return data

    def geturl(self) -> str:
        return self._url


def _http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://example.test/data", status, "failure", headers, None)


def _connection_termination(kind: str) -> BaseException:
    if kind == "incomplete_read":
        return IncompleteRead(b"PARTIAL-SECRET", 99)
    if kind == "remote_disconnected":
        return RemoteDisconnected("RAW-DISCONNECT-SECRET")
    raise AssertionError(f"unsupported connection termination: {kind}")


class _ClosingReadFailureResponse(FakeResponse):
    def __init__(self, failure: BaseException, *, first_chunk: bytes | None = None) -> None:
        super().__init__(
            headers={"Content-Encoding": "gzip" if first_chunk is not None else "identity"}
        )
        self._failure = failure
        self._first_chunk = first_chunk
        self.closed = False
        self._read_calls = 0

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.closed = True

    def read(self, size: int | None = None) -> bytes:
        self._read_calls += 1
        if self._first_chunk is not None and self._read_calls == 1:
            return self._first_chunk
        raise self._failure


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


def test_cancellation_before_retry_does_not_start_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    control = OperationControl()

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        control.cancel()
        raise _http_error(503)

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    with operation_control_scope(control), pytest.raises(OperationCancelledError):
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )

    assert attempts == 1


def test_cancellation_interrupts_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    retry_started = threading.Event()
    control = OperationControl()

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        retry_started.set()
        raise _http_error(503, "5")

    def cancel_after_retry_starts() -> None:
        assert retry_started.wait(timeout=1)
        control.cancel()

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    cancellation_thread = threading.Thread(target=cancel_after_retry_starts)
    cancellation_thread.start()
    started = time.monotonic()
    with operation_control_scope(control), pytest.raises(OperationCancelledError):
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )
    cancellation_thread.join(timeout=1)

    assert attempts == 1
    assert time.monotonic() - started < 1.0


def test_compression_is_negotiated_only_when_the_caller_declared_no_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: Request | None = None

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal observed
        observed = request
        return FakeResponse()

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    transport = UrlLibHttpTransport()

    # 1. No encoding declared -> transport negotiates gzip
    transport.get("https://example.test/data", headers={}, timeout_seconds=1.0)
    assert observed is not None
    assert observed.get_header("Accept-encoding") == "gzip"

    # 2. Explicit Accept-Encoding: identity declared by caller -> preserved
    transport.get(
        "https://example.test/data",
        headers={"Accept-Encoding": "identity"},
        timeout_seconds=1.0,
    )
    assert observed.get_header("Accept-encoding") == "identity"

    # 3. Explicit Accept-Encoding: deflate declared by caller -> preserved
    transport.get(
        "https://example.test/data",
        headers={"Accept-Encoding": "deflate"},
        timeout_seconds=1.0,
    )
    assert observed.get_header("Accept-encoding") == "deflate"

    # 4. Case-insensitive header name (accept-encoding) -> preserved without duplicate
    transport.get(
        "https://example.test/data",
        headers={"accept-encoding": "custom-encoding"},
        timeout_seconds=1.0,
    )
    assert observed.get_header("Accept-encoding") == "custom-encoding"

    # 5. post_form negotiates gzip when caller declared no encoding
    transport.post_form(
        "https://example.test/data",
        headers={},
        fields={"k": "v"},
        timeout_seconds=1.0,
    )
    assert observed.get_header("Accept-encoding") == "gzip"

    # 6. post_form preserves explicit encoding
    transport.post_form(
        "https://example.test/data",
        headers={"Accept-Encoding": "gzip, br"},
        fields={"k": "v"},
        timeout_seconds=1.0,
    )
    assert observed.get_header("Accept-encoding") == "gzip, br"


def test_compressed_and_identity_responses_deliver_the_same_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_payload = b'{"status": "ok", "items": [1, 2, 3], "nested": {"key": "value"}}'
    compressed_payload = gzip.compress(raw_payload)

    # Identity response without Content-Encoding
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=raw_payload,
            headers={"Content-Type": "application/json"},
        ),
    )
    identity_res = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
    )

    # Gzip response with Content-Encoding: gzip
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=compressed_payload,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        ),
    )
    compressed_res = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
    )

    assert identity_res.body == raw_payload
    assert compressed_res.body == raw_payload
    assert (
        hashlib.sha256(identity_res.body).hexdigest()
        == hashlib.sha256(compressed_res.body).hexdigest()
    )
    assert identity_res.body_truncated is False
    assert compressed_res.body_truncated is False


def test_max_response_bytes_bounds_the_decompressed_body_and_marks_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_payload = b"0123456789" * 10
    compressed_payload = gzip.compress(raw_payload)

    # Truncated when uncompressed stream exceeds limit
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=compressed_payload,
            headers={"Content-Encoding": "gzip"},
        ),
    )
    res_truncated = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
        max_response_bytes=40,
    )
    assert res_truncated.body == b"0123456789" * 4
    assert len(res_truncated.body) == 40
    assert res_truncated.body_truncated is True

    # Complete when within limit
    res_within = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
        max_response_bytes=100,
    )
    assert res_within.body == raw_payload
    assert len(res_within.body) == 100
    assert res_within.body_truncated is False

    # Bounds decompression of large stream / zip bomb
    huge_compressed = gzip.compress(b"X" * 100_000)
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=huge_compressed,
            headers={"Content-Encoding": "gzip"},
        ),
    )
    res_bounded = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
        max_response_bytes=50,
    )
    assert res_bounded.body == b"X" * 50
    assert res_bounded.body_truncated is True


def test_response_headers_and_url_remain_exactly_as_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compressed_payload = gzip.compress(b'{"key": "value"}')
    initial_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Encoding": "gzip",
        "X-Custom-Header": "header-value",
        "Server": "TestServer/1.0",
    }
    final_url = "https://example.test/final/redirect/url"

    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            url=final_url,
            body=compressed_payload,
            headers=initial_headers,
            status=200,
        ),
    )
    response = UrlLibHttpTransport().get(
        "https://example.test/initial/url",
        headers={},
        timeout_seconds=1.0,
    )

    assert response.status_code == 200
    assert response.url == final_url
    assert response.body == b'{"key": "value"}'
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["X-Custom-Header"] == "header-value"
    assert response.headers["Server"] == "TestServer/1.0"


def test_identity_responses_keep_the_current_behaviour_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_payload = b"uncompressed plain payload"

    # Explicit Content-Encoding: identity
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=raw_payload,
            headers={"Content-Encoding": "identity", "Content-Type": "text/plain"},
        ),
    )
    res_identity = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
        max_response_bytes=12,
    )
    assert res_identity.body == b"uncompressed"
    assert res_identity.body_truncated is True
    assert res_identity.headers["Content-Encoding"] == "identity"

    # No Content-Encoding header
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=raw_payload,
            headers={"Content-Type": "text/plain"},
        ),
    )
    res_no_encoding = UrlLibHttpTransport().get(
        "https://example.test/data",
        headers={},
        timeout_seconds=1.0,
    )
    assert res_no_encoding.body == raw_payload
    assert res_no_encoding.body_truncated is False


def test_corrupt_compressed_stream_raises_a_typed_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Invalid gzip header / corrupt bytes
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=b"definitely not gzip bytes",
            headers={"Content-Encoding": "gzip"},
        ),
    )
    with pytest.raises(HttpRequestError, match="corrupt or incomplete compressed stream") as err1:
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )
    assert err1.value.failure_kind is HttpRequestFailureKind.TRANSPORT

    # 2. Truncated gzip stream (ended before EOF)
    valid_compressed = gzip.compress(b"valid payload with enough length to test truncation")
    truncated_compressed = valid_compressed[:15]
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=truncated_compressed,
            headers={"Content-Encoding": "gzip"},
        ),
    )
    with pytest.raises(HttpRequestError, match="corrupt or incomplete compressed stream") as err2:
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )
    assert err2.value.failure_kind is HttpRequestFailureKind.TRANSPORT


@pytest.mark.parametrize("encoding", ["deflate", "br", "compress", "zstd", "unknown-encoding"])
def test_unsupported_content_encoding_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    monkeypatch.setattr(
        http_module,
        "urlopen",
        lambda req, timeout: FakeResponse(
            body=b"some bytes",
            headers={"Content-Encoding": encoding},
        ),
    )
    with pytest.raises(HttpRequestError, match="unsupported Content-Encoding") as error:
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )

    assert error.value.failure_kind is HttpRequestFailureKind.TRANSPORT


def test_retry_policy_and_retry_after_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compressed_body = gzip.compress(b'{"retried": true}')
    outcomes: list[BaseException | FakeResponse] = [
        _http_error(503, "2"),
        FakeResponse(
            body=compressed_body,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        ),
    ]
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
    assert response.body == b'{"retried": true}'
    assert sleeps == [2.0]


@pytest.mark.parametrize("error_kind", ["incomplete_read", "remote_disconnected"])
@pytest.mark.parametrize("failure_phase", ["open", "identity_read", "gzip_read"])
def test_get_retries_explicit_response_transport_failures_from_a_clean_response(
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
    failure_phase: str,
) -> None:
    expected_body = b"the complete response body"
    failure = _connection_termination(error_kind)
    partial_gzip = gzip.compress(b"discarded partial response")
    failed_response: _ClosingReadFailureResponse | None = None
    attempts = 0
    sleeps: list[float] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal attempts, failed_response
        assert request.full_url == "https://example.test/data"
        assert request.get_method() == "GET"
        assert timeout == 1.0
        attempts += 1
        if attempts == 1:
            if failure_phase == "open":
                raise failure
            failed_response = _ClosingReadFailureResponse(
                failure,
                first_chunk=partial_gzip if failure_phase == "gzip_read" else None,
            )
            return failed_response
        headers = (
            {"Content-Encoding": "gzip"}
            if failure_phase == "gzip_read"
            else {"Content-Encoding": "identity"}
        )
        response_body = (
            gzip.compress(expected_body) if failure_phase == "gzip_read" else expected_body
        )
        return FakeResponse(body=response_body, headers=headers)

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    response = UrlLibHttpTransport(sleep=sleeps.append).get(
        "https://example.test/data",
        headers={"User-Agent": "PRIVATE-USER-AGENT"},
        timeout_seconds=1.0,
    )

    assert response.body == expected_body
    assert b"discarded partial response" not in response.body
    assert attempts == 2
    assert sleeps == [0.1]
    if failed_response is not None:
        assert failed_response.closed is True


@pytest.mark.parametrize(
    "failures",
    [
        ["incomplete_read", "remote_disconnected", "incomplete_read"],
        ["http_503", "timeout", "remote_disconnected"],
    ],
    ids=["three-response-terminations", "mixed-shared-attempt-budget"],
)
def test_get_exhaustion_uses_three_attempts_two_waits_and_safe_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    failures: list[str],
) -> None:
    outcomes: list[BaseException] = []
    for failure in failures:
        if failure == "http_503":
            outcomes.append(_http_error(503))
        elif failure == "timeout":
            outcomes.append(TimeoutError("RAW-TIMEOUT-SECRET"))
        else:
            outcomes.append(_connection_termination(failure))
    attempts = 0
    sleeps: list[float] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal attempts
        assert request.full_url == "https://example.test/data"
        assert request.get_method() == "GET"
        assert timeout == 1.0
        attempts += 1
        raise outcomes[attempts - 1]

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    with pytest.raises(HttpRequestError) as raised:
        UrlLibHttpTransport(sleep=sleeps.append).get(
            "https://example.test/data",
            headers={"User-Agent": "PRIVATE-USER-AGENT", "X-Private": "PRIVATE-HEADER"},
            timeout_seconds=1.0,
        )

    assert attempts == 3
    assert sleeps == [0.1, 0.2]
    assert raised.value.failure_kind is HttpRequestFailureKind.TRANSPORT
    assert raised.value.__cause__ is outcomes[-1]
    assert "retry limit was exhausted" in str(raised.value)
    for secret in (
        "PARTIAL-SECRET",
        "RAW-DISCONNECT-SECRET",
        "RAW-TIMEOUT-SECRET",
        "PRIVATE-USER-AGENT",
        "PRIVATE-HEADER",
    ):
        assert secret not in str(raised.value)


@pytest.mark.parametrize("error_kind", ["incomplete_read", "remote_disconnected"])
def test_explicit_response_terminations_are_inferred_as_transport(error_kind: str) -> None:
    error = HttpRequestError(
        "https://example.test/data",
        "response failed",
        cause=_connection_termination(error_kind),
    )

    assert error.failure_kind is HttpRequestFailureKind.TRANSPORT


@pytest.mark.parametrize("error_kind", ["incomplete_read", "remote_disconnected"])
def test_post_wraps_explicit_response_termination_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal attempts
        assert request.full_url == "https://example.test/query"
        assert request.get_method() == "POST"
        assert timeout == 1.0
        attempts += 1
        raise _connection_termination(error_kind)

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    with pytest.raises(HttpRequestError) as raised:
        UrlLibHttpTransport(sleep=sleeps.append).post_form(
            "https://example.test/query",
            headers={"User-Agent": "PRIVATE-USER-AGENT"},
            fields={"secret": "PRIVATE-POST-FIELD"},
            timeout_seconds=1.0,
        )

    assert attempts == 1
    assert sleeps == []
    assert raised.value.method == "POST"
    assert raised.value.failure_kind is HttpRequestFailureKind.TRANSPORT
    assert "PARTIAL-SECRET" not in str(raised.value)
    assert "RAW-DISCONNECT-SECRET" not in str(raised.value)
    assert "PRIVATE-USER-AGENT" not in str(raised.value)
    assert "PRIVATE-POST-FIELD" not in str(raised.value)


@pytest.mark.parametrize(
    ("error_kind", "expected_sleep"),
    [("http_503", 2.0), ("timeout", 0.1), ("url_error", 0.1)],
)
def test_post_keeps_existing_retry_policy_for_other_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
    expected_sleep: float,
) -> None:
    if error_kind == "http_503":
        first_failure: BaseException = _http_error(503, "2")
    elif error_kind == "timeout":
        first_failure = TimeoutError("simulated timeout")
    else:
        first_failure = URLError("simulated URL failure")
    attempts = 0
    sleeps: list[float] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        nonlocal attempts
        assert request.full_url == "https://example.test/query"
        assert request.get_method() == "POST"
        assert timeout == 1.0
        attempts += 1
        if attempts == 1:
            raise first_failure
        return FakeResponse(body=b"accepted")

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    response = UrlLibHttpTransport(sleep=sleeps.append).post_form(
        "https://example.test/query",
        headers={},
        fields={"form": "stable"},
        timeout_seconds=1.0,
    )

    assert response.body == b"accepted"
    assert attempts == 2
    assert sleeps == [expected_sleep]


def test_cancellation_after_response_termination_prevents_get_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    control = OperationControl()

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        control.cancel()
        raise RemoteDisconnected("RAW-DISCONNECT-SECRET")

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    with operation_control_scope(control), pytest.raises(OperationCancelledError):
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )

    assert attempts == 1


def test_cancellation_during_response_termination_backoff_interrupts_get_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    backoff_started = threading.Event()
    control = OperationControl()
    original_wait = OperationControl.wait

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        raise IncompleteRead(b"PARTIAL-SECRET", 99)

    def tracked_wait(waiting_control: OperationControl, timeout_seconds: float) -> bool:
        backoff_started.set()
        return original_wait(waiting_control, timeout_seconds)

    def cancel_during_backoff() -> None:
        assert backoff_started.wait(timeout=1)
        control.cancel()

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(OperationControl, "wait", tracked_wait)
    cancellation_thread = threading.Thread(target=cancel_during_backoff)
    cancellation_thread.start()
    with operation_control_scope(control), pytest.raises(OperationCancelledError):
        UrlLibHttpTransport().get(
            "https://example.test/data",
            headers={},
            timeout_seconds=1.0,
        )
    cancellation_thread.join(timeout=1)

    assert attempts == 1
    assert backoff_started.is_set()
    assert not cancellation_thread.is_alive()
