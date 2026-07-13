"""Read-only Alpaca Market Data client for historical AAPL daily bars."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from urllib.parse import urlencode, urlsplit

from investment_analyst.providers.http import HttpTransport

OFFICIAL_BASE_URL = "https://data.alpaca.markets"
SUPPORTED_SYMBOL = "AAPL"
FEED = "iex"
ADJUSTMENT = "all"
TIMEFRAME = "1Day"
PAGE_LIMIT = 10_000
_MAX_PAGES = 1_000
_MAX_RESPONSE_BYTES = 10_000_000
_MAX_BARS_PER_RESPONSE = 10_000
_REQUIRED_BAR_FIELDS = frozenset({"t", "o", "h", "l", "c", "v", "n", "vw"})
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")


class AlpacaStockError(ValueError):
    """Invalid Alpaca request parameters, credentials, or response data."""


@dataclass(frozen=True, slots=True, repr=False)
class AlpacaCredentials:
    """Alpaca header credentials that never reveal their values in repr output."""

    api_key: str
    secret_key: str

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        secret_key = self.secret_key.strip()
        if not api_key or not secret_key:
            raise AlpacaStockError("Alpaca API key and secret must not be empty")
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "secret_key", secret_key)

    def __repr__(self) -> str:
        return "AlpacaCredentials(api_key=<redacted>, secret_key=<redacted>)"


@dataclass(frozen=True, slots=True)
class AlpacaStockBar:
    """Validated immutable AAPL bar from the Alpaca IEX market-data feed."""

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: Decimal
    vwap: Decimal
    raw_values: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.symbol != SUPPORTED_SYMBOL or not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise AlpacaStockError("only the AAPL symbol is supported")
        timestamp = _utc_datetime(self.timestamp, field_name="timestamp")
        object.__setattr__(self, "timestamp", timestamp)

        raw_values = dict(self.raw_values)
        if set(raw_values) != _REQUIRED_BAR_FIELDS:
            raise AlpacaStockError("raw_values must contain t, o, h, l, c, v, n, and vw")
        if any(not isinstance(value, str) for value in raw_values.values()):
            raise AlpacaStockError("raw_values must preserve provider values as strings")
        parsed_timestamp = _parse_timestamp(raw_values["t"])
        parsed_decimals = tuple(
            _parse_decimal(raw_values[field], field_name=field)
            for field in ("o", "h", "l", "c", "v", "n", "vw")
        )
        if parsed_timestamp != timestamp or parsed_decimals != self._decimal_values():
            raise AlpacaStockError("raw_values do not match the parsed bar fields")
        object.__setattr__(self, "raw_values", MappingProxyType(raw_values))

        prices = (self.open, self.high, self.low, self.close, self.vwap)
        if not all(value.is_finite() for value in (*prices, self.volume, self.trade_count)):
            raise AlpacaStockError("bar values must be finite")
        if any(value <= 0 for value in prices):
            raise AlpacaStockError("bar prices must be positive")
        if self.volume < 0:
            raise AlpacaStockError("bar volume must not be negative")
        if self.trade_count < 0 or self.trade_count != self.trade_count.to_integral_value():
            raise AlpacaStockError("trade_count must be a non-negative integer")
        if self.low > self.high:
            raise AlpacaStockError("bar low must not exceed high")
        for value, label in (
            (self.open, "open"),
            (self.close, "close"),
            (self.vwap, "vwap"),
        ):
            if not self.low <= value <= self.high:
                raise AlpacaStockError(f"bar {label} must be within low and high")

    def _decimal_values(
        self,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
        return (
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.trade_count,
            self.vwap,
        )


@dataclass(frozen=True, slots=True)
class AlpacaStockFetchResult:
    """Bars and request metadata returned by one logical Alpaca fetch."""

    symbol: str
    requested_start: datetime
    requested_end: datetime
    retrieved_at: datetime
    feed: str
    adjustment: str
    request_urls: tuple[str, ...]
    bars: tuple[AlpacaStockBar, ...]


class AlpacaStockClient:
    """Read-only Alpaca Market Data client with no trading capabilities."""

    def __init__(
        self,
        transport: HttpTransport,
        credentials: AlpacaCredentials,
        *,
        base_url: str = OFFICIAL_BASE_URL,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        normalized_base = base_url.rstrip("/")
        parsed_base = urlsplit(normalized_base)
        if parsed_base.scheme.lower() != "https" or parsed_base.hostname != "data.alpaca.markets":
            raise AlpacaStockError("Alpaca base_url must use https://data.alpaca.markets")
        if parsed_base.path not in ("", "/") or parsed_base.query or parsed_base.fragment:
            raise AlpacaStockError("Alpaca base_url must not contain a path, query, or fragment")
        if timeout_seconds <= 0:
            raise AlpacaStockError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._credentials = credentials
        self._base_url = normalized_base
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def fetch_daily_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> AlpacaStockFetchResult:
        """Fetch all pages, then validate, filter, order, and deduplicate AAPL bars."""
        if symbol != SUPPORTED_SYMBOL:
            raise AlpacaStockError("only AAPL is supported in this project step")
        requested_start = _utc_datetime(start, field_name="start")
        requested_end = _utc_datetime(end, field_name="end")
        if requested_start >= requested_end:
            raise AlpacaStockError("start must be earlier than end")
        now = _utc_datetime(self._clock(), field_name="clock result")
        if requested_end > now:
            raise AlpacaStockError("future bar ranges are not allowed")

        request_urls: list[str] = []
        bars_by_timestamp: dict[datetime, AlpacaStockBar] = {}
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _page_number in range(_MAX_PAGES):
            if page_token is not None:
                if page_token in seen_tokens:
                    raise AlpacaStockError("Alpaca pagination token cycle detected")
                seen_tokens.add(page_token)
            request_url = self._build_request_url(
                symbol, requested_start, requested_end, page_token
            )
            response = self._transport.get(
                request_url,
                headers=self._headers(),
                timeout_seconds=self._timeout_seconds,
            )
            request_urls.append(request_url)
            if response.status_code != 200:
                raise AlpacaStockError(f"Alpaca Market Data returned HTTP {response.status_code}")
            page_bars, next_page_token = _parse_page(symbol, response.body)
            for bar in page_bars:
                if not requested_start <= bar.timestamp < requested_end:
                    continue
                existing = bars_by_timestamp.get(bar.timestamp)
                if existing is None:
                    bars_by_timestamp[bar.timestamp] = bar
                elif existing != bar:
                    raise AlpacaStockError(
                        f"conflicting AAPL bars were returned for {bar.timestamp.isoformat()}"
                    )
            if next_page_token is None:
                break
            if next_page_token == page_token or next_page_token in seen_tokens:
                raise AlpacaStockError("Alpaca pagination token cycle detected")
            page_token = next_page_token
        else:
            raise AlpacaStockError("Alpaca response exceeded the defensive page limit")

        retrieved_at = _utc_datetime(self._clock(), field_name="clock result")
        bars = tuple(bars_by_timestamp[key] for key in sorted(bars_by_timestamp))
        return AlpacaStockFetchResult(
            symbol=symbol,
            requested_start=requested_start,
            requested_end=requested_end,
            retrieved_at=retrieved_at,
            feed=FEED,
            adjustment=ADJUSTMENT,
            request_urls=tuple(request_urls),
            bars=bars,
        )

    def _headers(self) -> Mapping[str, str]:
        return {
            "APCA-API-KEY-ID": self._credentials.api_key,
            "APCA-API-SECRET-KEY": self._credentials.secret_key,
            "Accept": "application/json",
            "User-Agent": "investment-analyst/0.1.0",
        }

    def _build_request_url(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        page_token: str | None,
    ) -> str:
        parameters = {
            "timeframe": TIMEFRAME,
            "feed": FEED,
            "adjustment": ADJUSTMENT,
            "sort": "asc",
            "limit": str(PAGE_LIMIT),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        if page_token is not None:
            parameters["page_token"] = page_token
        return f"{self._base_url}/v2/stocks/{symbol}/bars?{urlencode(parameters)}"


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlpacaStockError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


def _reject_json_constant(value: str) -> str:
    raise AlpacaStockError(f"non-finite JSON number is not allowed: {value}")


def _parse_page(symbol: str, body: bytes) -> tuple[tuple[AlpacaStockBar, ...], str | None]:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise AlpacaStockError("Alpaca response body is unexpectedly large")
    try:
        decoded = json.loads(
            body,
            parse_float=str,
            parse_int=str,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlpacaStockError("Alpaca returned invalid JSON") from error
    if not isinstance(decoded, dict):
        raise AlpacaStockError("Alpaca bar response must be an object")
    response_symbol = decoded.get("symbol")
    if response_symbol != symbol:
        raise AlpacaStockError("Alpaca response symbol does not match AAPL")
    bars = decoded.get("bars")
    if not isinstance(bars, list):
        raise AlpacaStockError("Alpaca response bars must be a list")
    if len(bars) > _MAX_BARS_PER_RESPONSE:
        raise AlpacaStockError("Alpaca returned an unjustified number of bars")
    next_page_token = decoded.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise AlpacaStockError("next_page_token must be a string or null")
    if next_page_token == "":
        raise AlpacaStockError("next_page_token must not be empty")
    return tuple(_parse_bar(symbol, bar) for bar in bars), next_page_token


def _parse_bar(symbol: str, value: object) -> AlpacaStockBar:
    if not isinstance(value, dict):
        raise AlpacaStockError("each Alpaca bar must be an object")
    if not _REQUIRED_BAR_FIELDS.issubset(value):
        missing = sorted(_REQUIRED_BAR_FIELDS.difference(value))
        raise AlpacaStockError(f"Alpaca bar is missing required fields: {', '.join(missing)}")
    raw_values = {
        field: _raw_text(value[field], field_name=field) for field in _REQUIRED_BAR_FIELDS
    }
    timestamp = _parse_timestamp(raw_values["t"])
    return AlpacaStockBar(
        symbol=symbol,
        timestamp=timestamp,
        open=_parse_decimal(raw_values["o"], field_name="o"),
        high=_parse_decimal(raw_values["h"], field_name="h"),
        low=_parse_decimal(raw_values["l"], field_name="l"),
        close=_parse_decimal(raw_values["c"], field_name="c"),
        volume=_parse_decimal(raw_values["v"], field_name="v"),
        trade_count=_parse_decimal(raw_values["n"], field_name="n"),
        vwap=_parse_decimal(raw_values["vw"], field_name="vw"),
        raw_values=raw_values,
    )


def _raw_text(value: object, *, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise AlpacaStockError(f"Alpaca field {field_name} has an invalid JSON type")
    return value


def _parse_decimal(value: str, *, field_name: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise AlpacaStockError(f"Alpaca field {field_name} is not numeric") from error
    if not number.is_finite():
        raise AlpacaStockError(f"Alpaca field {field_name} must be finite")
    return number


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AlpacaStockError("Alpaca bar timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AlpacaStockError("Alpaca bar timestamp must include timezone information")
    return parsed.astimezone(UTC)
