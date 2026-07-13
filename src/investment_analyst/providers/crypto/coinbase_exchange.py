"""Coinbase Exchange client for public daily BTC-USD candles."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import sleep as default_sleep
from urllib.parse import quote, urlencode

from investment_analyst.providers.http import HttpTransport

OFFICIAL_BASE_URL = "https://api.exchange.coinbase.com"
DAILY_GRANULARITY_SECONDS = 86_400
_MAX_INTERVALS_PER_REQUEST = 300
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_ROWS_PER_RESPONSE = 1_000
_REQUEST_DELAY_SECONDS = 0.21
_PRODUCT_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
_INTEGER_PATTERN = re.compile(r"^-?\d+$")


class CoinbaseExchangeError(ValueError):
    """Invalid Coinbase request parameters or response data."""


@dataclass(frozen=True, slots=True)
class CoinbaseCandle:
    """Validated immutable Coinbase OHLCV candle."""

    product_id: str
    start: datetime
    low: Decimal
    high: Decimal
    open: Decimal
    close: Decimal
    volume: Decimal
    raw_values: tuple[str, str, str, str, str, str]

    def __post_init__(self) -> None:
        if not _PRODUCT_ID_PATTERN.fullmatch(self.product_id):
            raise CoinbaseExchangeError("product_id must use the BASE-QUOTE format")
        object.__setattr__(self, "start", _utc_datetime(self.start, field_name="start"))
        if len(self.raw_values) != 6:
            raise CoinbaseExchangeError("raw_values must contain exactly six values")
        raw_time, *raw_financial_values = self.raw_values
        if not _INTEGER_PATTERN.fullmatch(raw_time):
            raise CoinbaseExchangeError("raw candle timestamp must use whole Unix seconds")
        try:
            raw_start = datetime.fromtimestamp(int(raw_time), tz=UTC)
            parsed_financial_values = tuple(Decimal(value) for value in raw_financial_values)
        except (InvalidOperation, OSError, OverflowError, ValueError) as error:
            raise CoinbaseExchangeError("raw_values contain invalid numeric data") from error
        if raw_start != self.start or parsed_financial_values != self._decimal_values():
            raise CoinbaseExchangeError("raw_values do not match the parsed candle fields")
        if not all(value.is_finite() for value in self._decimal_values()):
            raise CoinbaseExchangeError("candle values must be finite")
        if self.volume < 0:
            raise CoinbaseExchangeError("candle volume must not be negative")
        if self.low > self.high:
            raise CoinbaseExchangeError("candle low must not exceed high")
        if not self.low <= self.open <= self.high:
            raise CoinbaseExchangeError("candle open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise CoinbaseExchangeError("candle close must be within low and high")

    def _decimal_values(self) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        return self.low, self.high, self.open, self.close, self.volume


@dataclass(frozen=True, slots=True)
class CoinbaseFetchResult:
    """Candles and request metadata returned by one logical fetch."""

    product_id: str
    requested_start: datetime
    requested_end: datetime
    retrieved_at: datetime
    request_urls: tuple[str, ...]
    candles: tuple[CoinbaseCandle, ...]


class CoinbaseExchangeClient:
    """Read-only client for Coinbase Exchange public market candles."""

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
        if not normalized_base.startswith("https://"):
            raise CoinbaseExchangeError("Coinbase base_url must use HTTPS")
        if timeout_seconds <= 0:
            raise CoinbaseExchangeError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._base_url = normalized_base
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._clock = clock

    def fetch_daily_candles(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
    ) -> CoinbaseFetchResult:
        """Fetch, validate, filter, order, and deduplicate daily candles."""
        if not _PRODUCT_ID_PATTERN.fullmatch(product_id):
            raise CoinbaseExchangeError("product_id must use the BASE-QUOTE format")
        requested_start = _utc_datetime(start, field_name="start")
        requested_end = _utc_datetime(end, field_name="end")
        if requested_start >= requested_end:
            raise CoinbaseExchangeError("start must be earlier than end")
        now = _utc_datetime(self._clock(), field_name="clock result")
        if requested_end > now:
            raise CoinbaseExchangeError("future candle ranges are not allowed")

        request_urls: list[str] = []
        candles_by_start: dict[datetime, CoinbaseCandle] = {}
        cursor = requested_start
        while cursor < requested_end:
            chunk_end = min(
                cursor + timedelta(days=_MAX_INTERVALS_PER_REQUEST),
                requested_end,
            )
            request_url = self._build_request_url(product_id, cursor, chunk_end)
            if request_urls:
                self._sleep(_REQUEST_DELAY_SECONDS)
            response = self._transport.get(
                request_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "investment-analyst/0.1.0",
                },
                timeout_seconds=self._timeout_seconds,
            )
            request_urls.append(request_url)
            if response.status_code != 200:
                raise CoinbaseExchangeError(
                    f"Coinbase returned HTTP {response.status_code} for a candle request"
                )
            for candle in _parse_candles(product_id, response.body):
                if not requested_start <= candle.start < requested_end:
                    continue
                existing = candles_by_start.get(candle.start)
                if existing is None:
                    candles_by_start[candle.start] = candle
                elif existing != candle:
                    raise CoinbaseExchangeError(
                        f"conflicting candles were returned for {candle.start.isoformat()}"
                    )
            cursor = chunk_end

        retrieved_at = _utc_datetime(self._clock(), field_name="clock result")
        candles = tuple(candles_by_start[key] for key in sorted(candles_by_start))
        return CoinbaseFetchResult(
            product_id=product_id,
            requested_start=requested_start,
            requested_end=requested_end,
            retrieved_at=retrieved_at,
            request_urls=tuple(request_urls),
            candles=candles,
        )

    def _build_request_url(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
    ) -> str:
        query = urlencode(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "granularity": str(DAILY_GRANULARITY_SECONDS),
            }
        )
        return f"{self._base_url}/products/{quote(product_id, safe='')}/candles?{query}"


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CoinbaseExchangeError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


def _reject_json_constant(value: str) -> str:
    raise CoinbaseExchangeError(f"non-finite JSON number is not allowed: {value}")


def _parse_candles(product_id: str, body: bytes) -> tuple[CoinbaseCandle, ...]:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CoinbaseExchangeError("Coinbase response body is unexpectedly large")
    try:
        decoded = json.loads(
            body,
            parse_int=str,
            parse_float=str,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoinbaseExchangeError("Coinbase returned invalid JSON") from error
    if not isinstance(decoded, list):
        raise CoinbaseExchangeError("Coinbase candle response must be a list")
    if len(decoded) > _MAX_ROWS_PER_RESPONSE:
        raise CoinbaseExchangeError("Coinbase returned an unjustified number of candle rows")
    return tuple(_parse_candle(product_id, row) for row in decoded)


def _parse_candle(product_id: str, row: object) -> CoinbaseCandle:
    if not isinstance(row, list):
        raise CoinbaseExchangeError("each Coinbase candle must be a list")
    if len(row) != 6:
        raise CoinbaseExchangeError("each Coinbase candle must contain six values")
    raw_values = tuple(_numeric_text(value) for value in row)
    time_text, low_text, high_text, open_text, close_text, volume_text = raw_values
    if not _INTEGER_PATTERN.fullmatch(time_text):
        raise CoinbaseExchangeError("candle timestamps must be whole Unix seconds")
    try:
        timestamp = int(time_text)
        decimals = tuple(
            Decimal(value) for value in (low_text, high_text, open_text, close_text, volume_text)
        )
        start = datetime.fromtimestamp(timestamp, tz=UTC)
    except (InvalidOperation, OSError, OverflowError, ValueError) as error:
        raise CoinbaseExchangeError("candle contains an invalid numeric value") from error
    low, high, open_value, close, volume = decimals
    return CoinbaseCandle(
        product_id=product_id,
        start=start,
        low=low,
        high=high,
        open=open_value,
        close=close,
        volume=volume,
        raw_values=raw_values,
    )


def _numeric_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise CoinbaseExchangeError("candle values must be JSON numbers")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise CoinbaseExchangeError("candle contains a non-numeric value") from error
    if not number.is_finite():
        raise CoinbaseExchangeError("candle values must be finite")
    return value
