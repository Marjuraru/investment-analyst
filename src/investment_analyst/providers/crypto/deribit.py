"""Strict read-only client for the public Deribit derivatives datasets."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import sleep as default_sleep
from urllib.parse import urlencode

from pydantic import ConfigDict, JsonValue, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.http import HttpRequestError, HttpTransport

OFFICIAL_BASE_URL = "https://www.deribit.com/api/v2"
FUNDING_METHOD = "public/get_funding_rate_history"
DVOL_METHOD = "public/get_volatility_index_data"
SUMMARY_METHOD = "public/get_book_summary_by_instrument"
DVOL_RESOLUTION = "1D"
MAX_FUNDING_INTERVAL = timedelta(days=31)
MAX_DVOL_INTERVAL = timedelta(days=366)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_HISTORICAL_ROWS = 1_000
REQUEST_DELAY_SECONDS = 0.25

_CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{2,16}$")
_INSTRUMENT_PATTERN = re.compile(r"^[A-Z0-9]{2,16}-PERPETUAL$")
_MILLISECOND_PATTERN = re.compile(r"^-?\d+$")


class DeribitError(ValueError):
    """Invalid Deribit request, transport outcome, or public response contract."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class _ProviderModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DeribitFundingPoint(_ProviderModel):
    """One validated hourly funding-history row with its canonical raw object."""

    instrument_name: NonEmptyStr
    timestamp: UTCDateTime
    index_price: Decimal
    prev_index_price: Decimal
    interest_1h: Decimal
    interest_8h: Decimal
    raw_payload: dict[str, JsonValue]

    @field_validator("index_price", "prev_index_price", "interest_1h", "interest_8h", mode="before")
    @classmethod
    def reject_non_decimal_input(cls, value: object) -> object:
        if not isinstance(value, Decimal):
            raise ValueError("funding values must be constructed from exact Decimal values")
        return value

    @field_validator("index_price", "prev_index_price", "interest_1h", "interest_8h")
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("funding values must be finite Decimal values")
        return value

    @model_validator(mode="after")
    def validate_row(self) -> "DeribitFundingPoint":
        if not _INSTRUMENT_PATTERN.fullmatch(self.instrument_name):
            raise ValueError("funding instrument_name must identify a perpetual")
        if self.index_price <= 0 or self.prev_index_price <= 0:
            raise ValueError("funding index prices must be positive")
        return self


class DeribitDvolCandle(_ProviderModel):
    """One validated daily DVOL OHLC row with its original five-element array."""

    currency: NonEmptyStr
    start: UTCDateTime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    raw_payload: tuple[str, str, str, str, str]

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def reject_non_decimal_input(cls, value: object) -> object:
        if not isinstance(value, Decimal):
            raise ValueError("DVOL values must be constructed from exact Decimal values")
        return value

    @field_validator("open", "high", "low", "close")
    @classmethod
    def require_finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("DVOL values must be finite Decimal values")
        return value

    @model_validator(mode="after")
    def validate_candle(self) -> "DeribitDvolCandle":
        if not _CURRENCY_PATTERN.fullmatch(self.currency):
            raise ValueError("DVOL currency must use upper-case letters or digits")
        if self.low > self.high:
            raise ValueError("DVOL low must not exceed high")
        if not self.low <= self.open <= self.high:
            raise ValueError("DVOL open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("DVOL close must be within low and high")
        return self


class DeribitPerpetualSummary(_ProviderModel):
    """One validated prospective perpetual snapshot.

    The provider field ``last`` is deliberately promoted as ``last_price``. Funding
    snapshot fields remain distinct from historical ``interest_1h``/``interest_8h``.
    """

    instrument_name: NonEmptyStr
    base_currency: NonEmptyStr
    quote_currency: NonEmptyStr
    creation_timestamp: UTCDateTime
    open_interest: Decimal | None = None
    mark_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    mid_price: Decimal | None = None
    last_price: Decimal | None = None
    current_funding: Decimal | None = None
    funding_8h: Decimal | None = None
    volume_24h: Decimal | None = None
    volume_usd_24h: Decimal | None = None
    price_change_24h: Decimal | None = None
    raw_payload: dict[str, JsonValue]

    @field_validator(
        "open_interest",
        "mark_price",
        "bid_price",
        "ask_price",
        "mid_price",
        "last_price",
        "current_funding",
        "funding_8h",
        "volume_24h",
        "volume_usd_24h",
        "price_change_24h",
        mode="before",
    )
    @classmethod
    def reject_non_decimal_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, Decimal):
            raise ValueError("summary values must be constructed from exact Decimal values")
        return value

    @field_validator(
        "open_interest",
        "mark_price",
        "bid_price",
        "ask_price",
        "mid_price",
        "last_price",
        "current_funding",
        "funding_8h",
        "volume_24h",
        "volume_usd_24h",
        "price_change_24h",
    )
    @classmethod
    def require_finite_optional_decimal(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("summary values must be finite Decimal values")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "DeribitPerpetualSummary":
        if self.instrument_name != f"{self.base_currency}-PERPETUAL":
            raise ValueError("summary instrument and base currency do not match")
        if self.quote_currency != "USD":
            raise ValueError("summary quote_currency must be USD")
        non_negative = (
            self.open_interest,
            self.mark_price,
            self.bid_price,
            self.ask_price,
            self.mid_price,
            self.last_price,
            self.volume_24h,
            self.volume_usd_24h,
        )
        if any(value is not None and value < 0 for value in non_negative):
            raise ValueError("summary prices, volume, and open interest must not be negative")
        if (
            self.ask_price is not None
            and self.bid_price is not None
            and self.ask_price < self.bid_price
        ):
            raise ValueError("summary ask_price must not be below bid_price")
        if self.mid_price is not None and self.mid_price <= 0:
            raise ValueError("summary mid_price must be positive when present")
        return self


@dataclass(frozen=True, slots=True)
class DeribitFundingFetch:
    instrument_name: str
    requested_start: datetime
    requested_end: datetime
    retrieved_at: datetime
    request_urls: tuple[str, ...]
    points: tuple[DeribitFundingPoint, ...]


@dataclass(frozen=True, slots=True)
class DeribitDvolFetch:
    currency: str
    requested_start: datetime
    requested_end: datetime
    retrieved_at: datetime
    request_urls: tuple[str, ...]
    candles: tuple[DeribitDvolCandle, ...]


@dataclass(frozen=True, slots=True)
class DeribitSummaryFetch:
    instrument_name: str
    retrieved_at: datetime
    request_url: str
    summary: DeribitPerpetualSummary


class DeribitClient:
    """Bounded public Deribit client without authentication or trading methods."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        base_url: str = OFFICIAL_BASE_URL,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = default_sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        normalized_base = base_url.rstrip("/")
        if normalized_base != OFFICIAL_BASE_URL:
            raise DeribitError("Deribit v1 base_url must match the official HTTPS endpoint")
        if timeout_seconds <= 0:
            raise DeribitError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._base_url = normalized_base
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._clock = clock
        self._request_count = 0

    def fetch_funding_history(
        self,
        instrument_name: str,
        start: datetime,
        end: datetime,
    ) -> DeribitFundingFetch:
        """Fetch consecutive 31-day chunks and enforce internal half-open semantics."""
        _require_instrument(instrument_name)
        requested_start, requested_end = _range(start, end)
        points_by_time: dict[datetime, DeribitFundingPoint] = {}
        request_urls: list[str] = []
        cursor = requested_start
        while cursor < requested_end:
            chunk_end = min(cursor + MAX_FUNDING_INTERVAL, requested_end)
            result, request_url = self._request_result(
                FUNDING_METHOD,
                {
                    "instrument_name": instrument_name,
                    "start_timestamp": str(_milliseconds(cursor) - 1),
                    "end_timestamp": str(_milliseconds(chunk_end)),
                },
            )
            request_urls.append(request_url)
            if not isinstance(result, list):
                raise DeribitError("Deribit funding result must be a list")
            if len(result) > MAX_HISTORICAL_ROWS:
                raise DeribitError("Deribit funding response exceeds the row limit")
            parsed = tuple(_parse_funding_point(instrument_name, row) for row in result)
            for point in parsed:
                if not cursor <= point.timestamp < chunk_end:
                    continue
                existing = points_by_time.get(point.timestamp)
                if existing is not None:
                    qualifier = "conflicting " if existing != point else "duplicate "
                    raise DeribitError(f"Deribit returned a {qualifier}funding timestamp")
                points_by_time[point.timestamp] = point
            cursor = chunk_end
        retrieved_at = _utc_datetime(self._clock(), field_name="clock result")
        if requested_end > retrieved_at:
            raise DeribitError("Deribit funding history requires a fully closed interval")
        return DeribitFundingFetch(
            instrument_name=instrument_name,
            requested_start=requested_start,
            requested_end=requested_end,
            retrieved_at=retrieved_at,
            request_urls=tuple(request_urls),
            points=tuple(points_by_time[key] for key in sorted(points_by_time)),
        )

    def fetch_dvol_daily(
        self,
        currency: str,
        start: datetime,
        end: datetime,
    ) -> DeribitDvolFetch:
        """Fetch daily DVOL chunks while following backward continuation safely."""
        _require_currency(currency)
        requested_start, requested_end = _range(start, end)
        candles_by_time: dict[datetime, DeribitDvolCandle] = {}
        request_urls: list[str] = []
        chunk_start = requested_start
        while chunk_start < requested_end:
            logical_end = min(chunk_start + MAX_DVOL_INTERVAL, requested_end)
            page_end_ms = _milliseconds(logical_end)
            seen_continuations: set[int] = set()
            while True:
                result, request_url = self._request_result(
                    DVOL_METHOD,
                    {
                        "currency": currency,
                        "start_timestamp": str(_milliseconds(chunk_start)),
                        "end_timestamp": str(page_end_ms),
                        "resolution": DVOL_RESOLUTION,
                    },
                )
                request_urls.append(request_url)
                if not isinstance(result, dict):
                    raise DeribitError("Deribit DVOL result must be an object")
                if set(result) != {"data", "continuation"}:
                    raise DeribitError("Deribit DVOL result has an unexpected shape")
                data = result["data"]
                if not isinstance(data, list):
                    raise DeribitError("Deribit DVOL data must be a list")
                if len(data) > MAX_HISTORICAL_ROWS:
                    raise DeribitError("Deribit DVOL response exceeds the row limit")
                parsed = tuple(_parse_dvol_candle(currency, row) for row in data)
                for candle in parsed:
                    if not chunk_start <= candle.start < logical_end:
                        continue
                    existing = candles_by_time.get(candle.start)
                    if existing is not None and existing != candle:
                        raise DeribitError("Deribit returned conflicting DVOL candles")
                    candles_by_time[candle.start] = candle
                continuation = result["continuation"]
                if continuation is None:
                    break
                next_end_ms = _whole_number(continuation, field_name="DVOL continuation")
                start_ms = _milliseconds(chunk_start)
                if next_end_ms in seen_continuations:
                    raise DeribitError("Deribit DVOL continuation repeated or cycled")
                if not start_ms < next_end_ms < page_end_ms:
                    raise DeribitError(
                        "Deribit DVOL continuation must decrease within the logical interval"
                    )
                seen_continuations.add(next_end_ms)
                page_end_ms = next_end_ms
            chunk_start = logical_end
        retrieved_at = _utc_datetime(self._clock(), field_name="clock result")
        if requested_end > retrieved_at:
            raise DeribitError("Deribit DVOL history requires a fully closed interval")
        return DeribitDvolFetch(
            currency=currency,
            requested_start=requested_start,
            requested_end=requested_end,
            retrieved_at=retrieved_at,
            request_urls=tuple(request_urls),
            candles=tuple(candles_by_time[key] for key in sorted(candles_by_time)),
        )

    def fetch_perpetual_summary(self, instrument_name: str) -> DeribitSummaryFetch:
        """Capture exactly one current public perpetual book summary."""
        currency = _require_instrument(instrument_name)
        result, request_url = self._request_result(
            SUMMARY_METHOD,
            {"instrument_name": instrument_name},
        )
        if not isinstance(result, list) or len(result) != 1:
            raise DeribitError("Deribit summary result must contain exactly one row")
        summary = _parse_summary(instrument_name, currency, result[0])
        retrieved_at = _utc_datetime(self._clock(), field_name="clock result")
        if summary.creation_timestamp > retrieved_at:
            raise DeribitError("Deribit summary timestamp cannot be after local retrieval")
        return DeribitSummaryFetch(
            instrument_name=instrument_name,
            retrieved_at=retrieved_at,
            request_url=request_url,
            summary=summary,
        )

    def _request_result(
        self,
        method: str,
        parameters: Mapping[str, str],
    ) -> tuple[JsonValue, str]:
        if method not in {FUNDING_METHOD, DVOL_METHOD, SUMMARY_METHOD}:
            raise DeribitError("unsupported Deribit public method")
        if self._request_count:
            self._sleep(REQUEST_DELAY_SECONDS)
        request_url = f"{self._base_url}/{method}?{urlencode(tuple(parameters.items()))}"
        try:
            response = self._transport.get(
                request_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "investment-analyst/0.1.0",
                },
                timeout_seconds=self._timeout_seconds,
                max_response_bytes=MAX_RESPONSE_BYTES,
            )
        except HttpRequestError as error:
            raise DeribitError(
                "Deribit public request failed after bounded transport retries",
                status_code=error.status_code,
            ) from error
        self._request_count += 1
        if response.url != request_url:
            raise DeribitError("Deribit public request was redirected outside its exact contract")
        if response.status_code != 200:
            raise DeribitError(
                f"Deribit returned HTTP {response.status_code} for a public request",
                status_code=response.status_code,
            )
        if response.body_truncated or len(response.body) > MAX_RESPONSE_BYTES:
            raise DeribitError("Deribit response exceeded the maximum body size")
        document = _decode_json(response.body)
        if not isinstance(document, dict):
            raise DeribitError("Deribit JSON-RPC response must be an object")
        allowed_envelope = {
            "error",
            "id",
            "jsonrpc",
            "result",
            "testnet",
            "usDiff",
            "usIn",
            "usOut",
        }
        if not set(document).issubset(allowed_envelope):
            raise DeribitError("Deribit JSON-RPC response has an unexpected shape")
        if document.get("jsonrpc") != "2.0":
            raise DeribitError("Deribit JSON-RPC version is unexpected")
        if "id" in document:
            raise DeribitError("Deribit JSON-RPC response contains an unexpected ID")
        if "error" in document:
            raise DeribitError("Deribit returned a JSON-RPC error")
        if "result" not in document:
            raise DeribitError("Deribit JSON-RPC response does not contain result")
        return _json_value(document["result"]), request_url


def _decode_json(body: bytes) -> object:
    try:
        return json.loads(
            body,
            parse_int=str,
            parse_float=str,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeribitError("Deribit returned invalid JSON") from error


def _reject_json_constant(value: str) -> str:
    raise DeribitError(f"non-finite JSON number is not allowed: {value}")


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DeribitError("Deribit JSON object keys must be strings")
            output[key] = _json_value(item)
        return output
    raise DeribitError("Deribit JSON contains an unsupported value")


def _json_object(value: object, *, label: str) -> dict[str, JsonValue]:
    converted = _json_value(value)
    if not isinstance(converted, dict):
        raise DeribitError(f"{label} must be an object")
    return converted


def _parse_funding_point(instrument_name: str, value: object) -> DeribitFundingPoint:
    row = _json_object(value, label="each Deribit funding row")
    expected = {"timestamp", "index_price", "prev_index_price", "interest_1h", "interest_8h"}
    if set(row) != expected:
        raise DeribitError("Deribit funding row has an unexpected shape")
    return DeribitFundingPoint(
        instrument_name=instrument_name,
        timestamp=_datetime_from_milliseconds(row["timestamp"], field_name="funding timestamp"),
        index_price=_decimal(row["index_price"], field_name="funding index_price"),
        prev_index_price=_decimal(row["prev_index_price"], field_name="funding prev_index_price"),
        interest_1h=_decimal(row["interest_1h"], field_name="funding interest_1h"),
        interest_8h=_decimal(row["interest_8h"], field_name="funding interest_8h"),
        raw_payload=row,
    )


def _parse_dvol_candle(currency: str, value: object) -> DeribitDvolCandle:
    converted = _json_value(value)
    if not isinstance(converted, list) or len(converted) != 5:
        raise DeribitError("each Deribit DVOL candle must contain five values")
    raw_values = tuple(_numeric_text(item, field_name="DVOL value") for item in converted)
    timestamp, open_value, high, low, close = raw_values
    return DeribitDvolCandle(
        currency=currency,
        start=_datetime_from_milliseconds(timestamp, field_name="DVOL timestamp"),
        open=_decimal(open_value, field_name="DVOL open"),
        high=_decimal(high, field_name="DVOL high"),
        low=_decimal(low, field_name="DVOL low"),
        close=_decimal(close, field_name="DVOL close"),
        raw_payload=raw_values,
    )


def _parse_summary(
    instrument_name: str,
    currency: str,
    value: object,
) -> DeribitPerpetualSummary:
    row = _json_object(value, label="Deribit perpetual summary row")
    required = {"instrument_name", "base_currency", "quote_currency", "creation_timestamp"}
    if not required.issubset(row):
        raise DeribitError("Deribit perpetual summary is missing identity fields")
    if row["instrument_name"] != instrument_name:
        raise DeribitError("Deribit summary instrument_name does not match the request")
    if row["base_currency"] != currency or row["quote_currency"] != "USD":
        raise DeribitError("Deribit summary currency identity does not match configuration")
    return DeribitPerpetualSummary(
        instrument_name=instrument_name,
        base_currency=currency,
        quote_currency="USD",
        creation_timestamp=_datetime_from_milliseconds(
            row["creation_timestamp"], field_name="summary creation_timestamp"
        ),
        open_interest=_optional_decimal(row, "open_interest"),
        mark_price=_optional_decimal(row, "mark_price"),
        bid_price=_optional_decimal(row, "bid_price"),
        ask_price=_optional_decimal(row, "ask_price"),
        mid_price=_optional_decimal(row, "mid_price"),
        last_price=_optional_decimal(row, "last"),
        current_funding=_optional_decimal(row, "current_funding"),
        funding_8h=_optional_decimal(row, "funding_8h"),
        volume_24h=_optional_decimal(row, "volume"),
        volume_usd_24h=_optional_decimal(row, "volume_usd"),
        price_change_24h=_optional_decimal(row, "price_change"),
        raw_payload=row,
    )


def _optional_decimal(row: Mapping[str, JsonValue], field_name: str) -> Decimal | None:
    if field_name not in row or row[field_name] is None:
        return None
    return _decimal(row[field_name], field_name=f"summary {field_name}")


def _numeric_text(value: JsonValue, *, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise DeribitError(f"{field_name} must be encoded as one JSON number")
    return value


def _decimal(value: JsonValue, *, field_name: str) -> Decimal:
    text = _numeric_text(value, field_name=field_name)
    try:
        parsed = Decimal(text)
    except InvalidOperation as error:
        raise DeribitError(f"{field_name} is not a valid Decimal") from error
    if not parsed.is_finite():
        raise DeribitError(f"{field_name} must be finite")
    return parsed


def _whole_number(value: JsonValue, *, field_name: str) -> int:
    text = _numeric_text(value, field_name=field_name)
    if not _MILLISECOND_PATTERN.fullmatch(text):
        raise DeribitError(f"{field_name} must be a whole millisecond timestamp")
    try:
        return int(text)
    except ValueError as error:
        raise DeribitError(f"{field_name} is outside the integer range") from error


def _datetime_from_milliseconds(value: JsonValue, *, field_name: str) -> datetime:
    milliseconds = _whole_number(value, field_name=field_name)
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
    except (OSError, OverflowError, ValueError) as error:
        raise DeribitError(f"{field_name} is outside the supported datetime range") from error


def _milliseconds(value: datetime) -> int:
    utc_value = _utc_datetime(value, field_name="range bound")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_value - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DeribitError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


def _range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    normalized_start = _utc_datetime(start, field_name="start")
    normalized_end = _utc_datetime(end, field_name="end")
    if normalized_start >= normalized_end:
        raise DeribitError("start must be earlier than end")
    if normalized_start.microsecond % 1_000 or normalized_end.microsecond % 1_000:
        raise DeribitError("Deribit range bounds must align to whole milliseconds")
    return normalized_start, normalized_end


def _require_currency(currency: str) -> str:
    if currency != currency.strip() or not _CURRENCY_PATTERN.fullmatch(currency):
        raise DeribitError("currency must use upper-case letters or digits")
    return currency


def _require_instrument(instrument_name: str) -> str:
    if instrument_name != instrument_name.strip() or not _INSTRUMENT_PATTERN.fullmatch(
        instrument_name
    ):
        raise DeribitError("instrument_name must use CURRENCY-PERPETUAL")
    currency = instrument_name.removesuffix("-PERPETUAL")
    _require_currency(currency)
    return currency


__all__ = [
    "DVOL_METHOD",
    "DVOL_RESOLUTION",
    "DeribitClient",
    "DeribitDvolCandle",
    "DeribitDvolFetch",
    "DeribitError",
    "DeribitFundingFetch",
    "DeribitFundingPoint",
    "DeribitPerpetualSummary",
    "DeribitSummaryFetch",
    "FUNDING_METHOD",
    "OFFICIAL_BASE_URL",
    "SUMMARY_METHOD",
]
