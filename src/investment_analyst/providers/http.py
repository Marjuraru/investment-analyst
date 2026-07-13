"""Minimal retrying HTTPS transport built on the Python standard library."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import sleep as default_sleep
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 5.0
_DEFAULT_BACKOFF_SECONDS = (0.1, 0.2)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Raw HTTP response returned without interpreting its body."""

    status_code: int
    body: bytes
    headers: Mapping[str, str]
    url: str


class HttpRequestError(RuntimeError):
    """HTTP request failure with safe diagnostic context."""

    def __init__(
        self,
        url: str,
        message: str,
        *,
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.cause = cause
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"GET {url} failed{status}: {message}")


class HttpTransport(Protocol):
    """Small transport protocol used by provider clients."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        """Perform one logical GET request."""
        ...


class UrlLibHttpTransport:
    """HTTPS GET transport with bounded deterministic retries."""

    def __init__(self, *, sleep: Callable[[float], None] = default_sleep) -> None:
        self._sleep = sleep

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        """Fetch bytes over HTTPS, retrying only transient failures."""
        if urlsplit(url).scheme.lower() != "https":
            raise HttpRequestError(url, "only HTTPS URLs are allowed")
        if timeout_seconds <= 0:
            raise HttpRequestError(url, "timeout_seconds must be greater than zero")

        request = Request(url, headers=dict(headers), method="GET")
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urlopen(request, timeout=timeout_seconds) as response:
                    response_headers = MappingProxyType(
                        {str(key): str(value) for key, value in response.headers.items()}
                    )
                    return HttpResponse(
                        status_code=int(response.status),
                        body=response.read(),
                        headers=response_headers,
                        url=str(response.geturl()),
                    )
            except HTTPError as error:
                if error.code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_ATTEMPTS - 1:
                    raise HttpRequestError(
                        url,
                        "the server returned an unsuccessful response",
                        status_code=error.code,
                        cause=error,
                    ) from error
                self._sleep(self._retry_delay(attempt, error.headers.get("Retry-After")))
            except (TimeoutError, URLError) as error:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise HttpRequestError(
                        url,
                        "a temporary network error exhausted the retry limit",
                        cause=error,
                    ) from error
                self._sleep(self._retry_delay(attempt, None))

        raise HttpRequestError(url, "the retry loop ended unexpectedly")

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
