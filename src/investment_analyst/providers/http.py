"""Minimal retrying HTTPS transport built on the Python standard library."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import sleep as default_sleep
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 5.0
_DEFAULT_BACKOFF_SECONDS = (0.1, 0.2)


class HttpRequestFailureKind(StrEnum):
    """Structured reason why one bounded HTTP request failed."""

    CONFIGURATION = "configuration"
    HTTP_STATUS = "http_status"
    TRANSPORT = "transport"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Raw HTTP response returned without interpreting its body."""

    status_code: int
    body: bytes
    headers: Mapping[str, str]
    url: str
    body_truncated: bool = False


class HttpRequestError(RuntimeError):
    """HTTP request failure with safe diagnostic context."""

    def __init__(
        self,
        url: str,
        message: str,
        *,
        method: str = "GET",
        status_code: int | None = None,
        cause: BaseException | None = None,
        failure_kind: HttpRequestFailureKind | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.status_code = status_code
        self.cause = cause
        self.failure_kind = failure_kind or self._infer_failure_kind(
            status_code=status_code,
            cause=cause,
        )
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{method} {url} failed{status}: {message}")

    @staticmethod
    def _infer_failure_kind(
        *,
        status_code: int | None,
        cause: BaseException | None,
    ) -> HttpRequestFailureKind:
        if status_code is not None:
            return HttpRequestFailureKind.HTTP_STATUS
        if isinstance(cause, (ConnectionError, TimeoutError, URLError)):
            return HttpRequestFailureKind.TRANSPORT
        return HttpRequestFailureKind.UNEXPECTED


class HttpTransport(Protocol):
    """Small transport protocol used by provider clients."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        """Perform one logical GET request."""
        ...


class HttpFormTransport(HttpTransport, Protocol):
    """HTTPS transport that also supports bounded form submissions."""

    def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        """Perform one logical application/x-www-form-urlencoded POST request."""
        ...


class UrlLibHttpTransport:
    """HTTPS GET/form-POST transport with bounded deterministic retries."""

    def __init__(self, *, sleep: Callable[[float], None] = default_sleep) -> None:
        self._sleep = sleep

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        """Fetch bytes over HTTPS, retrying only transient failures."""
        return self._request(
            "GET",
            url,
            headers=headers,
            body=None,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        """Submit one HTTPS form without logging or reflecting its field values."""
        request_headers = dict(headers)
        request_headers.setdefault(
            "Content-Type",
            "application/x-www-form-urlencoded; charset=utf-8",
        )
        body = urlencode(tuple(fields.items())).encode("utf-8")
        return self._request(
            "POST",
            url,
            headers=request_headers,
            body=body,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int | None,
    ) -> HttpResponse:
        """Execute one already-encoded request under the shared safety policy."""
        if urlsplit(url).scheme.lower() != "https":
            raise HttpRequestError(
                url,
                "only HTTPS URLs are allowed",
                method=method,
                failure_kind=HttpRequestFailureKind.CONFIGURATION,
            )
        if timeout_seconds <= 0:
            raise HttpRequestError(
                url,
                "timeout_seconds must be greater than zero",
                method=method,
                failure_kind=HttpRequestFailureKind.CONFIGURATION,
            )
        if max_response_bytes is not None and (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise HttpRequestError(
                url,
                "max_response_bytes must be a positive integer",
                method=method,
                failure_kind=HttpRequestFailureKind.CONFIGURATION,
            )

        request = Request(url, data=body, headers=dict(headers), method=method)
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urlopen(request, timeout=timeout_seconds) as response:
                    response_headers = MappingProxyType(
                        {str(key): str(value) for key, value in response.headers.items()}
                    )
                    if max_response_bytes is None:
                        body = response.read()
                        body_truncated = False
                    else:
                        candidate = response.read(max_response_bytes + 1)
                        body = candidate[:max_response_bytes]
                        body_truncated = len(candidate) > max_response_bytes
                    return HttpResponse(
                        status_code=int(response.status),
                        body=body,
                        headers=response_headers,
                        url=str(response.geturl()),
                        body_truncated=body_truncated,
                    )
            except HTTPError as error:
                if error.code not in RETRYABLE_HTTP_STATUS_CODES or attempt == _MAX_ATTEMPTS - 1:
                    raise HttpRequestError(
                        url,
                        "the server returned an unsuccessful response",
                        method=method,
                        status_code=error.code,
                        cause=error,
                    ) from error
                self._sleep(self._retry_delay(attempt, error.headers.get("Retry-After")))
            except (TimeoutError, URLError) as error:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise HttpRequestError(
                        url,
                        "a temporary network error exhausted the retry limit",
                        method=method,
                        cause=error,
                    ) from error
                self._sleep(self._retry_delay(attempt, None))

        raise HttpRequestError(
            url,
            "the retry loop ended unexpectedly",
            method=method,
            failure_kind=HttpRequestFailureKind.UNEXPECTED,
        )

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                seconds = float(retry_after)
            except ValueError:
                seconds = -1.0
            if 0 <= seconds <= _MAX_RETRY_AFTER_SECONDS:
                return seconds
        return _DEFAULT_BACKOFF_SECONDS[min(attempt, len(_DEFAULT_BACKOFF_SECONDS) - 1)]
