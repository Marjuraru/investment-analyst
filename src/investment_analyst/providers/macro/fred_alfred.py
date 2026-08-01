"""Read-only client and strict contracts for FRED/ALFRED vintage observations."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal
from urllib.parse import urlencode

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.http import (
    HttpRequestError,
    HttpRequestFailureKind,
    HttpTransport,
)

OFFICIAL_BASE_URL = "https://api.stlouisfed.org"
OBSERVATIONS_PATH = "/fred/series/observations"
VINTAGE_DATES_PATH = "/fred/series/vintagedates"
MAX_OBSERVATIONS = 100_000
MAX_VINTAGE_DATES_PER_PAGE = 10_000
MAX_VINTAGE_DATES_PER_FETCH = 100_000
MAX_RESPONSE_BYTES = 24_000_000
MISSING_VALUE = "."
_API_KEY_PATTERN = re.compile(r"^[a-z0-9]{32}$")
_SERIES_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,79}$")
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class FredAlfredError(ValueError):
    """Invalid FRED/ALFRED input, response, or request result."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        failure_kind: HttpRequestFailureKind | None = None,
    ) -> None:
        self.status_code = status_code
        self.failure_kind = failure_kind
        super().__init__(message)


@dataclass(frozen=True, slots=True, repr=False)
class FredApiKey:
    """Validated FRED API key whose representation never reveals its value."""

    value: str

    def __post_init__(self) -> None:
        if not _API_KEY_PATTERN.fullmatch(self.value):
            raise FredAlfredError(
                "FRED_API_KEY must contain exactly 32 lowercase letters or digits"
            )

    def __repr__(self) -> str:
        return "FredApiKey(value='[REDACTED]')"


class FredApiObservation(ContractModel):
    """One observation exactly as represented by the JSON API."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    realtime_start: date
    realtime_end: date
    observation_date: date = Field(alias="date")
    raw_value: NonEmptyStr = Field(alias="value")

    @model_validator(mode="after")
    def validate_observation(self) -> "FredApiObservation":
        """Validate real-time bounds and Decimal-compatible provider values."""
        if self.realtime_start > self.realtime_end:
            raise ValueError("realtime_start must not be later than realtime_end")
        if self.raw_value != MISSING_VALUE:
            try:
                value = Decimal(self.raw_value)
            except InvalidOperation as error:
                raise ValueError("FRED observation value must be Decimal-compatible") from error
            if not value.is_finite():
                raise ValueError("FRED observation value must be finite")
        return self

    @property
    def value(self) -> Decimal | None:
        """Return the exact value, or None for FRED's documented missing marker."""
        if self.raw_value == MISSING_VALUE:
            return None
        return Decimal(self.raw_value)


class FredObservationsResponse(ContractModel):
    """Strict response envelope for one untransformed vintage snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    realtime_start: date
    realtime_end: date
    observation_start: date
    observation_end: date
    units: Literal["lin"]
    output_type: Literal[1]
    file_type: Literal["json"]
    order_by: Literal["observation_date"]
    sort_order: Literal["asc"]
    count: int = Field(ge=0, le=MAX_OBSERVATIONS)
    offset: Literal[0]
    limit: Literal[MAX_OBSERVATIONS]
    observations: tuple[FredApiObservation, ...]

    @field_validator("count", "offset", "limit", "output_type", mode="before")
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("FRED integer metadata must not be boolean")
        return value

    @model_validator(mode="after")
    def validate_response(self) -> "FredObservationsResponse":
        """Validate bounds, complete first-page coverage, and deterministic ordering."""
        if self.realtime_start > self.realtime_end:
            raise ValueError("response real-time range is invalid")
        if self.observation_start > self.observation_end:
            raise ValueError("response observation range is invalid")
        if self.count != len(self.observations):
            raise ValueError("FRED response count must match the complete observation page")
        observation_dates = tuple(item.observation_date for item in self.observations)
        if observation_dates != tuple(sorted(observation_dates)):
            raise ValueError("FRED observations must be ordered chronologically")
        if len(observation_dates) != len(set(observation_dates)):
            raise ValueError("FRED response contains duplicate observation dates")
        return self


class FredVintageDatesResponse(ContractModel):
    """Strict one-page response from the official vintage-date endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    realtime_start: date
    realtime_end: date
    order_by: Literal["vintage_date"]
    sort_order: Literal["asc", "desc"]
    count: int = Field(ge=0, le=MAX_VINTAGE_DATES_PER_FETCH)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_VINTAGE_DATES_PER_PAGE)
    vintage_dates: tuple[date, ...]

    @field_validator("count", "offset", "limit", mode="before")
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        """Reject booleans accepted as integers by Python."""
        if isinstance(value, bool):
            raise ValueError("FRED vintage pagination metadata must not be boolean")
        return value

    @model_validator(mode="after")
    def validate_page(self) -> "FredVintageDatesResponse":
        """Validate range, page size, uniqueness, and provider ordering."""
        if self.realtime_start > self.realtime_end:
            raise ValueError("FRED vintage response real-time range is invalid")
        if len(self.vintage_dates) > self.limit:
            raise ValueError("FRED vintage response exceeds its page limit")
        if any(
            item < self.realtime_start or item > self.realtime_end for item in self.vintage_dates
        ):
            raise ValueError("FRED vintage response contains an out-of-range date")
        expected = tuple(sorted(self.vintage_dates, reverse=self.sort_order == "desc"))
        if self.vintage_dates != expected or len(expected) != len(set(expected)):
            raise ValueError("FRED vintage dates must be unique and ordered")
        return self


@dataclass(frozen=True, slots=True)
class FredVintageFetch:
    """Validated provider response plus safe request and retrieval metadata."""

    series_id: str
    vintage_date: date
    requested_observation_start: date
    requested_observation_end: date
    retrieved_at: datetime
    public_request_url: str
    body_sha256: str
    response_payload: JsonValue
    response: FredObservationsResponse


@dataclass(frozen=True, slots=True)
class FredVintageDatesFetch:
    """Bounded paginated vintage discovery without secret-bearing metadata."""

    series_id: str
    requested_realtime_start: date
    requested_realtime_end: date
    sort_order: Literal["asc", "desc"]
    retrieved_at: datetime
    public_request_urls: tuple[str, ...]
    total_count: int
    vintage_dates: tuple[date, ...]
    complete: bool


class FredAlfredClient:
    """Official FRED API client using ALFRED real-time vintage semantics."""

    def __init__(
        self,
        transport: HttpTransport,
        api_key: FredApiKey,
        *,
        base_url: str = OFFICIAL_BASE_URL,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        normalized_base = base_url.rstrip("/")
        if not normalized_base.startswith("https://"):
            raise FredAlfredError("FRED base_url must use HTTPS")
        if timeout_seconds <= 0:
            raise FredAlfredError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._api_key = api_key
        self._base_url = normalized_base
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def fetch_vintage_snapshot(
        self,
        series_id: str,
        *,
        vintage_date: date,
        observation_start: date,
        observation_end: date,
    ) -> FredVintageFetch:
        """Fetch one complete, untransformed series snapshot for an explicit vintage."""
        _validate_series_id(series_id)
        if observation_start > observation_end:
            raise FredAlfredError("observation_start must not be later than observation_end")
        request_started_at = _as_utc(self._clock(), field_name="clock result")
        if vintage_date > request_started_at.date():
            raise FredAlfredError("future vintage dates are not allowed")

        public_parameters = (
            ("file_type", "json"),
            ("series_id", series_id),
            ("vintage_dates", vintage_date.isoformat()),
            ("output_type", "1"),
            ("observation_start", observation_start.isoformat()),
            ("observation_end", observation_end.isoformat()),
            ("units", "lin"),
            ("limit", str(MAX_OBSERVATIONS)),
            ("offset", "0"),
            ("sort_order", "asc"),
        )
        public_request_url = f"{self._base_url}{OBSERVATIONS_PATH}?{urlencode(public_parameters)}"
        secret_request_url = (
            f"{self._base_url}{OBSERVATIONS_PATH}?"
            f"{urlencode((('api_key', self._api_key.value), *public_parameters))}"
        )
        try:
            http_response = self._transport.get(
                secret_request_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "investment-analyst/0.1.0",
                },
                timeout_seconds=self._timeout_seconds,
                max_response_bytes=MAX_RESPONSE_BYTES,
            )
        except HttpRequestError as error:
            status = f" (HTTP {error.status_code})" if error.status_code is not None else ""
            raise FredAlfredError(
                f"FRED/ALFRED request failed{status}",
                status_code=error.status_code,
                failure_kind=error.failure_kind,
            ) from None
        if http_response.status_code != 200:
            raise FredAlfredError(
                f"FRED/ALFRED returned HTTP {http_response.status_code}",
                status_code=http_response.status_code,
                failure_kind=HttpRequestFailureKind.HTTP_STATUS,
            )
        if http_response.body_truncated:
            raise FredAlfredError("FRED/ALFRED response exceeded the safe size limit")
        retrieved_at = _as_utc(self._clock(), field_name="clock result")
        if retrieved_at < request_started_at:
            raise FredAlfredError("clock result moved backwards during the FRED request")
        payload = _decode_json(http_response.body)
        response = FredObservationsResponse.model_validate(payload)
        _validate_requested_snapshot(
            response,
            vintage_date=vintage_date,
            observation_start=observation_start,
            observation_end=observation_end,
        )
        return FredVintageFetch(
            series_id=series_id,
            vintage_date=vintage_date,
            requested_observation_start=observation_start,
            requested_observation_end=observation_end,
            retrieved_at=retrieved_at,
            public_request_url=public_request_url,
            body_sha256=sha256(http_response.body).hexdigest(),
            response_payload=payload,
            response=response,
        )

    def fetch_vintage_dates(
        self,
        series_id: str,
        *,
        realtime_start: date,
        realtime_end: date,
        max_dates: int,
        sort_order: Literal["asc", "desc"] = "asc",
    ) -> FredVintageDatesFetch:
        """Enumerate a bounded revision range with explicit pagination and resume order."""
        _validate_series_id(series_id)
        if realtime_start > realtime_end:
            raise FredAlfredError("realtime_start must not be later than realtime_end")
        if isinstance(max_dates, bool) or not 1 <= max_dates <= MAX_VINTAGE_DATES_PER_FETCH:
            raise FredAlfredError(f"max_dates must be between 1 and {MAX_VINTAGE_DATES_PER_FETCH}")
        request_started_at = _as_utc(self._clock(), field_name="clock result")
        if realtime_end > request_started_at.date():
            raise FredAlfredError("future vintage discovery dates are not allowed")
        page_limit = min(MAX_VINTAGE_DATES_PER_PAGE, max_dates)
        offset = 0
        discovered: list[date] = []
        public_urls: list[str] = []
        total_count: int | None = None
        while len(discovered) < max_dates:
            public_parameters = (
                ("file_type", "json"),
                ("series_id", series_id),
                ("realtime_start", realtime_start.isoformat()),
                ("realtime_end", realtime_end.isoformat()),
                ("limit", str(min(page_limit, max_dates - len(discovered)))),
                ("offset", str(offset)),
                ("sort_order", sort_order),
            )
            public_request_url = (
                f"{self._base_url}{VINTAGE_DATES_PATH}?{urlencode(public_parameters)}"
            )
            secret_request_url = (
                f"{self._base_url}{VINTAGE_DATES_PATH}?"
                f"{urlencode((('api_key', self._api_key.value), *public_parameters))}"
            )
            try:
                http_response = self._transport.get(
                    secret_request_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "investment-analyst/0.1.0",
                    },
                    timeout_seconds=self._timeout_seconds,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                )
            except HttpRequestError as error:
                status = f" (HTTP {error.status_code})" if error.status_code is not None else ""
                raise FredAlfredError(
                    f"FRED/ALFRED request failed{status}",
                    status_code=error.status_code,
                    failure_kind=error.failure_kind,
                ) from None
            if http_response.status_code != 200:
                raise FredAlfredError(
                    f"FRED/ALFRED returned HTTP {http_response.status_code}",
                    status_code=http_response.status_code,
                    failure_kind=HttpRequestFailureKind.HTTP_STATUS,
                )
            if http_response.body_truncated:
                raise FredAlfredError("FRED/ALFRED response exceeded the safe size limit")
            response = FredVintageDatesResponse.model_validate(_decode_json(http_response.body))
            if (
                response.realtime_start != realtime_start
                or response.realtime_end != realtime_end
                or response.sort_order != sort_order
                or response.offset != offset
                or response.limit != int(dict(public_parameters)["limit"])
            ):
                raise FredAlfredError("FRED vintage pagination metadata differs from the request")
            if total_count is None:
                total_count = response.count
            elif response.count != total_count:
                raise FredAlfredError("FRED vintage count changed during pagination")
            public_urls.append(public_request_url)
            if not response.vintage_dates:
                if len(discovered) < min(total_count, max_dates):
                    raise FredAlfredError("FRED vintage pagination ended before its count")
                break
            discovered.extend(response.vintage_dates)
            if len(discovered) != len(set(discovered)):
                raise FredAlfredError("FRED vintage pagination returned duplicate dates")
            expected = sorted(discovered, reverse=sort_order == "desc")
            if discovered != expected:
                raise FredAlfredError("FRED vintage pagination order is inconsistent")
            offset += len(response.vintage_dates)
            if offset >= total_count:
                break
        retrieved_at = _as_utc(self._clock(), field_name="clock result")
        if retrieved_at < request_started_at:
            raise FredAlfredError("clock result moved backwards during FRED vintage discovery")
        count = total_count if total_count is not None else 0
        return FredVintageDatesFetch(
            series_id=series_id,
            requested_realtime_start=realtime_start,
            requested_realtime_end=realtime_end,
            sort_order=sort_order,
            retrieved_at=retrieved_at,
            public_request_urls=tuple(public_urls),
            total_count=count,
            vintage_dates=tuple(discovered),
            complete=len(discovered) == count,
        )


def validate_fred_series_id(series_id: str) -> str:
    """Return one canonical valid series identifier."""
    _validate_series_id(series_id)
    return series_id


def parse_stored_fred_response(
    payload: JsonValue,
    *,
    vintage_date: date,
    observation_start: date,
    observation_end: date,
) -> FredObservationsResponse:
    """Validate a stored raw payload against its immutable request metadata."""
    response = FredObservationsResponse.model_validate(payload)
    _validate_requested_snapshot(
        response,
        vintage_date=vintage_date,
        observation_start=observation_start,
        observation_end=observation_end,
    )
    return response


def _decode_json(body: bytes) -> JsonValue:
    if len(body) > MAX_RESPONSE_BYTES:
        raise FredAlfredError("FRED/ALFRED response body is unexpectedly large")
    try:
        decoded = json.loads(
            body,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FredAlfredError("FRED/ALFRED returned invalid JSON") from error
    if not isinstance(decoded, dict):
        raise FredAlfredError("FRED/ALFRED response must be a JSON object")
    return _JSON_VALUE_ADAPTER.validate_python(decoded)


def _reject_json_constant(value: str) -> None:
    raise FredAlfredError(f"non-standard JSON constant is not allowed: {value}")


def _reject_json_float(value: str) -> None:
    raise FredAlfredError(f"unexpected binary floating-point JSON number is not allowed: {value}")


def _validate_series_id(series_id: str) -> None:
    if not isinstance(series_id, str) or not _SERIES_ID_PATTERN.fullmatch(series_id):
        raise FredAlfredError(
            "series_id must use 1-80 uppercase letters, digits, dots, underscores, or hyphens"
        )


def _validate_requested_snapshot(
    response: FredObservationsResponse,
    *,
    vintage_date: date,
    observation_start: date,
    observation_end: date,
) -> None:
    if not response.realtime_start <= vintage_date <= response.realtime_end:
        raise FredAlfredError("FRED response does not cover the requested vintage date")
    if (
        response.observation_start != observation_start
        or response.observation_end != observation_end
    ):
        raise FredAlfredError("FRED response observation bounds differ from the request")
    for observation in response.observations:
        if not observation_start <= observation.observation_date <= observation_end:
            raise FredAlfredError("FRED returned an observation outside the requested range")
        if not observation.realtime_start <= vintage_date <= observation.realtime_end:
            raise FredAlfredError(
                "FRED observation real-time bounds do not cover the requested vintage"
            )


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FredAlfredError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)
