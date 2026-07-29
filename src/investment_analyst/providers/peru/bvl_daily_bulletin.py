"""Typed, read-only inspection of the official BVL daily equity bulletin."""

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BeforeValidator, ConfigDict, Field, StrictInt, ValidationError, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.http import HttpTransport
from investment_analyst.providers.peru.official_sources import (
    OFFICIAL_SOURCE_DEFINITIONS,
    PeruOfficialSource,
    SourceContractStatus,
)

BVL_DAILY_BULLETIN_SCHEMA_VERSION = "bvl-daily-equity-bulletin-inspection-v1"
MAX_BULLETIN_BYTES = 5_000_000
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_WATCHLIST = (
    "CVERDEC1",
    "BVN",
    "SCCO",
    "VOLCABC1",
    "MINSURI1",
    "POMALCC1",
)
_USER_AGENT = "investment-analyst/0.1.0"
_BULLETIN_DEFINITION = next(
    definition
    for definition in OFFICIAL_SOURCE_DEFINITIONS
    if definition.source is PeruOfficialSource.BVL_DAILY_EQUITY_BULLETIN
)
_UPDATED_AT_PATTERN = re.compile(
    r"Actualizado\s+al\s+(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})",
    flags=re.IGNORECASE,
)
_PLAIN_MNEMONIC_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_BULLETIN_MNEMONIC_PATTERN = re.compile(
    r"^(?P<mnemonic>[A-Z0-9][A-Z0-9.-]{0,31})(?:\s+\((?P<note>[^()]*)\))?$"
)
_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")
_SPAN_PATTERN = re.compile(r"^[1-9]\d*$")
_SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def _reject_float_or_bool(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("financial values must use Decimal, not float or bool")
    return value


BvlDecimal = Annotated[
    Decimal,
    BeforeValidator(_reject_float_or_bool),
    Field(allow_inf_nan=False),
]
NonNegativeBvlDecimal = Annotated[BvlDecimal, Field(ge=Decimal("0"))]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BvlCurrency(StrEnum):
    """Currencies explicitly represented by the BVL equity quotation table."""

    PEN = "PEN"
    USD = "USD"


class BvlDailyQuote(ContractModel):
    """One published BVL quotation row without assigning a premature asset identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mnemonic: NonEmptyStr
    currency: BvlCurrency
    bulletin_note_reference: NonEmptyStr | None = None
    previous_close: NonNegativeBvlDecimal | None = None
    previous_close_date: date | None = None
    open: NonNegativeBvlDecimal | None = None
    close: NonNegativeBvlDecimal | None = None
    high: NonNegativeBvlDecimal | None = None
    low: NonNegativeBvlDecimal | None = None
    day_change_percent: BvlDecimal | None = None
    bid: NonNegativeBvlDecimal | None = None
    ask: NonNegativeBvlDecimal | None = None
    average_price: NonNegativeBvlDecimal | None = None
    traded_shares: NonNegativeBvlDecimal | None = None
    traded_amount: NonNegativeBvlDecimal | None = None
    operation_count: StrictInt | None = Field(default=None, ge=0)
    quote_frequency_percent: BvlDecimal | None = None
    annual_return_percent: BvlDecimal | None = None

    @model_validator(mode="after")
    def validate_financial_relationships(self) -> "BvlDailyQuote":
        if not _PLAIN_MNEMONIC_PATTERN.fullmatch(self.mnemonic):
            raise ValueError("mnemonic does not match the bounded BVL identifier syntax")
        if (self.previous_close is None) != (self.previous_close_date is None):
            raise ValueError("previous close value and date must be present together")
        if self.high is not None and self.low is not None:
            if self.high < self.low:
                raise ValueError("high cannot be lower than low")
            for field_name, value in (("open", self.open), ("close", self.close)):
                if value is not None and not self.low <= value <= self.high:
                    raise ValueError(f"{field_name} must be between low and high")
        return self


class BvlDailyBulletinInspection(ContractModel):
    """Body-free inspection report for explicitly requested BVL mnemonics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bvl-daily-equity-bulletin-inspection-v1"] = (
        BVL_DAILY_BULLETIN_SCHEMA_VERSION
    )
    source: Literal["bvl:daily-equity-bulletin"] = "bvl:daily-equity-bulletin"
    contract_status: Literal[SourceContractStatus.PUBLIC_DOCUMENT_TERMS_REVIEW_REQUIRED] = (
        SourceContractStatus.PUBLIC_DOCUMENT_TERMS_REVIEW_REQUIRED
    )
    requested_url: NonEmptyStr
    final_url: NonEmptyStr
    retrieved_at: UTCDateTime
    bulletin_date: date
    http_status: Literal[200] = 200
    content_type: NonEmptyStr
    source_bytes: int = Field(ge=1, le=MAX_BULLETIN_BYTES)
    document_sha256: Sha256Hex
    detected_quote_rows: int = Field(ge=1)
    requested_mnemonics: tuple[NonEmptyStr, ...] = Field(min_length=1, max_length=100)
    quotes: tuple[BvlDailyQuote, ...]
    missing_mnemonics: tuple[NonEmptyStr, ...]
    persistence_performed: Literal[False] = False

    @model_validator(mode="after")
    def validate_selection(self) -> "BvlDailyBulletinInspection":
        if len(set(self.requested_mnemonics)) != len(self.requested_mnemonics):
            raise ValueError("requested mnemonics must be unique")
        quote_mnemonics = tuple(quote.mnemonic for quote in self.quotes)
        if len(set(quote_mnemonics)) != len(quote_mnemonics):
            raise ValueError("selected quotations must be unique")
        expected_quotes = tuple(
            mnemonic
            for mnemonic in self.requested_mnemonics
            if mnemonic not in self.missing_mnemonics
        )
        if quote_mnemonics != expected_quotes:
            raise ValueError("quotations must follow requested mnemonic order")
        expected_missing = tuple(
            mnemonic for mnemonic in self.requested_mnemonics if mnemonic not in quote_mnemonics
        )
        if self.missing_mnemonics != expected_missing:
            raise ValueError("missing mnemonics must be explicit and follow requested order")
        if any(
            quote.previous_close_date is not None and quote.previous_close_date > self.bulletin_date
            for quote in self.quotes
        ):
            raise ValueError("previous close date cannot be later than bulletin date")
        return self


class BvlDailyBulletinError(RuntimeError):
    """Safe failure raised when the BVL document no longer satisfies its contract."""


@dataclass(frozen=True, slots=True)
class _HtmlCell:
    text: str
    colspan: int
    rowspan: int


@dataclass(slots=True)
class _HtmlTable:
    header_rows: list[tuple[_HtmlCell, ...]] = field(default_factory=list)
    body_rows: list[tuple[_HtmlCell, ...]] = field(default_factory=list)


class _BulletinHtmlParser(HTMLParser):
    """Collect only BVL-styled tables and their header/body cell spans."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_HtmlTable] = []
        self._table: _HtmlTable | None = None
        self._table_depth = 0
        self._section: Literal["header", "body"] | None = None
        self._row: list[_HtmlCell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_colspan = 1
        self._cell_rowspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            classes = frozenset((attributes.get("class") or "").split())
            if self._table is None and "Tablas" in classes:
                self._table = _HtmlTable()
                self._table_depth = 1
            elif self._table is not None:
                self._table_depth += 1
            return
        if self._table is None or self._table_depth != 1:
            return
        if tag == "thead":
            self._section = "header"
        elif tag == "tbody":
            self._section = "body"
        elif tag == "tr" and self._section is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._cell_colspan = _parse_span(attributes.get("colspan"))
            self._cell_rowspan = _parse_span(attributes.get("rowspan"))

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._table is None:
            return
        if tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self.tables.append(self._table)
                self._table = None
                self._section = None
            return
        if self._table_depth != 1:
            return
        if tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            self._row.append(
                _HtmlCell(
                    text=_normalize_text("".join(self._cell_parts)),
                    colspan=self._cell_colspan,
                    rowspan=self._cell_rowspan,
                )
            )
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._section == "header":
                self._table.header_rows.append(tuple(self._row))
            elif self._section == "body":
                self._table.body_rows.append(tuple(self._row))
            self._row = None
        elif tag in {"thead", "tbody"}:
            self._section = None


class BvlDailyBulletinReader:
    """Fetch and inspect the official delayed daily bulletin without persistence."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if timeout_seconds <= 0:
            raise BvlDailyBulletinError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def inspect(
        self,
        mnemonics: Sequence[str] = DEFAULT_WATCHLIST,
    ) -> BvlDailyBulletinInspection:
        """Return selected typed quotations and auditable document metadata."""
        requested_mnemonics = _normalize_requested_mnemonics(mnemonics)
        retrieved_at = self._clock()
        response = self._transport.get(
            _BULLETIN_DEFINITION.url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "User-Agent": _USER_AGENT,
            },
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_BULLETIN_BYTES,
        )
        if response.status_code != 200:
            raise BvlDailyBulletinError(
                f"{PeruOfficialSource.BVL_DAILY_EQUITY_BULLETIN} returned "
                f"HTTP {response.status_code}"
            )
        _validate_final_url(response.url)
        content_type = _header_value(response.headers, "content-type")
        if not content_type or "text/html" not in content_type.lower():
            raise BvlDailyBulletinError("BVL bulletin returned an unexpected content type")
        if response.body_truncated:
            raise BvlDailyBulletinError(
                f"BVL bulletin exceeds the {MAX_BULLETIN_BYTES}-byte safety limit"
            )
        try:
            document = response.body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise BvlDailyBulletinError("BVL bulletin is not valid UTF-8") from error
        bulletin_date = _parse_bulletin_date(document)
        table = _select_equity_quotation_table(document, bulletin_date.year)
        row_index, detected_quote_rows = _index_quote_rows(table.body_rows)
        quotes: list[BvlDailyQuote] = []
        missing_mnemonics: list[str] = []
        for mnemonic in requested_mnemonics:
            rows = row_index.get(mnemonic, ())
            if not rows:
                missing_mnemonics.append(mnemonic)
                continue
            if len(rows) != 1:
                raise BvlDailyBulletinError(
                    f"BVL bulletin contains ambiguous duplicate rows for {mnemonic}"
                )
            quotes.append(_parse_quote(rows[0]))
        try:
            return BvlDailyBulletinInspection(
                requested_url=_BULLETIN_DEFINITION.url,
                final_url=response.url,
                retrieved_at=retrieved_at,
                bulletin_date=bulletin_date,
                content_type=content_type,
                source_bytes=len(response.body),
                document_sha256=sha256(response.body).hexdigest(),
                detected_quote_rows=detected_quote_rows,
                requested_mnemonics=requested_mnemonics,
                quotes=tuple(quotes),
                missing_mnemonics=tuple(missing_mnemonics),
            )
        except ValidationError as error:
            raise BvlDailyBulletinError(
                "BVL bulletin inspection violates report consistency"
            ) from error


_EXPECTED_HEADER_PATHS = (
    ("valores",),
    ("moneda",),
    ("anterior", "cotizacionanterior"),
    ("anterior", "fecha"),
    ("negociaciondeldia", "cotizaciones", "apertura"),
    ("negociaciondeldia", "cotizaciones", "cierre"),
    ("negociaciondeldia", "cotizaciones", "maxima"),
    ("negociaciondeldia", "cotizaciones", "minima"),
    ("negociaciondeldia", "variacdia"),
    ("negociaciondeldia", "propuestas", "compra"),
    ("negociaciondeldia", "propuestas", "venta"),
    ("negociaciondeldia", "preciopromedio"),
    ("negociaciondeldia", "ndeaccsnegociadas"),
    ("negociaciondeldia", "montoefectivonegociado"),
    ("negociaciondeldia", "ndeoper"),
    ("frecdecotiz",),
)


def _select_equity_quotation_table(document: str, bulletin_year: int) -> _HtmlTable:
    parser = _BulletinHtmlParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValueError, AssertionError) as error:
        raise BvlDailyBulletinError("BVL bulletin HTML could not be parsed safely") from error
    matches: list[_HtmlTable] = []
    for table in parser.tables:
        paths = _expanded_header_paths(table.header_rows)
        if len(paths) != 17 or paths[:16] != _EXPECTED_HEADER_PATHS:
            continue
        if paths[16] != ("rendimbursatil" + str(bulletin_year),):
            continue
        matches.append(table)
    if len(matches) != 1:
        raise BvlDailyBulletinError(
            "BVL bulletin must contain exactly one recognized equity quotation table"
        )
    return matches[0]


def _expanded_header_paths(
    rows: Sequence[tuple[_HtmlCell, ...]],
) -> tuple[tuple[str, ...], ...]:
    if not rows:
        return ()
    grid: dict[tuple[int, int], str] = {}
    width = 0
    for row_index, row in enumerate(rows):
        column_index = 0
        for cell in row:
            while (row_index, column_index) in grid:
                column_index += 1
            normalized_text = _header_key(cell.text)
            for row_offset in range(cell.rowspan):
                for column_offset in range(cell.colspan):
                    position = (row_index + row_offset, column_index + column_offset)
                    if position in grid:
                        return ()
                    grid[position] = normalized_text
            column_index += cell.colspan
        width = max(width, column_index)
    if any(
        (row_index, column_index) not in grid
        for row_index in range(len(rows))
        for column_index in range(width)
    ):
        return ()
    paths: list[tuple[str, ...]] = []
    for column_index in range(width):
        path: list[str] = []
        for row_index in range(len(rows)):
            text = grid[(row_index, column_index)]
            if text and (not path or path[-1] != text):
                path.append(text)
        paths.append(tuple(path))
    return tuple(paths)


def _index_quote_rows(
    rows: Sequence[tuple[_HtmlCell, ...]],
) -> tuple[dict[str, tuple[tuple[str, ...], ...]], int]:
    mutable_index: dict[str, list[tuple[str, ...]]] = {}
    detected_quote_rows = 0
    for row in rows:
        values = tuple(cell.text for cell in row)
        if len(values) != 17:
            continue
        mnemonic, _ = _split_mnemonic(values[0])
        if mnemonic is None or not values[1]:
            continue
        detected_quote_rows += 1
        mutable_index.setdefault(mnemonic, []).append(values)
    if detected_quote_rows == 0:
        raise BvlDailyBulletinError("BVL quotation table contains no recognizable quote rows")
    return (
        {mnemonic: tuple(indexed_rows) for mnemonic, indexed_rows in mutable_index.items()},
        detected_quote_rows,
    )


def _parse_quote(values: tuple[str, ...]) -> BvlDailyQuote:
    mnemonic, note = _split_mnemonic(values[0])
    if mnemonic is None:
        raise BvlDailyBulletinError("BVL quotation row has an invalid mnemonic")
    currency = {"S/": BvlCurrency.PEN, "US$": BvlCurrency.USD}.get(values[1])
    if currency is None:
        raise BvlDailyBulletinError(f"BVL quotation {mnemonic} has an unsupported currency")
    try:
        return BvlDailyQuote(
            mnemonic=mnemonic,
            currency=currency,
            bulletin_note_reference=note,
            previous_close=_parse_optional_decimal(values[2], "previous close", mnemonic),
            previous_close_date=_parse_optional_date(values[3], "previous close date", mnemonic),
            open=_parse_optional_decimal(values[4], "open", mnemonic),
            close=_parse_optional_decimal(values[5], "close", mnemonic),
            high=_parse_optional_decimal(values[6], "high", mnemonic),
            low=_parse_optional_decimal(values[7], "low", mnemonic),
            day_change_percent=_parse_optional_decimal(
                values[8], "day change percent", mnemonic, percentage=True
            ),
            bid=_parse_optional_decimal(values[9], "bid", mnemonic),
            ask=_parse_optional_decimal(values[10], "ask", mnemonic),
            average_price=_parse_optional_decimal(values[11], "average price", mnemonic),
            traded_shares=_parse_optional_decimal(values[12], "traded shares", mnemonic),
            traded_amount=_parse_optional_decimal(values[13], "traded amount", mnemonic),
            operation_count=_parse_optional_integer(values[14], "operation count", mnemonic),
            quote_frequency_percent=_parse_optional_decimal(
                values[15], "quote frequency percent", mnemonic, percentage=True
            ),
            annual_return_percent=_parse_optional_decimal(
                values[16], "annual return percent", mnemonic, percentage=True
            ),
        )
    except ValidationError as error:
        raise BvlDailyBulletinError(
            f"BVL quotation {mnemonic} violates financial consistency"
        ) from error


def _parse_optional_decimal(
    raw_value: str,
    field_name: str,
    mnemonic: str,
    *,
    percentage: bool = False,
) -> Decimal | None:
    value = raw_value.strip()
    if _is_missing(value):
        return None
    if percentage:
        if not value.endswith("%"):
            raise BvlDailyBulletinError(f"BVL quotation {mnemonic} has an invalid {field_name}")
        value = value[:-1]
    value = value.replace(",", "").replace(" ", "")
    if not _NUMBER_PATTERN.fullmatch(value):
        raise BvlDailyBulletinError(f"BVL quotation {mnemonic} has an invalid {field_name}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise BvlDailyBulletinError(
            f"BVL quotation {mnemonic} has an invalid {field_name}"
        ) from error
    if not parsed.is_finite():
        raise BvlDailyBulletinError(f"BVL quotation {mnemonic} has a non-finite {field_name}")
    return parsed


def _parse_optional_integer(raw_value: str, field_name: str, mnemonic: str) -> int | None:
    parsed = _parse_optional_decimal(raw_value, field_name, mnemonic)
    if parsed is None:
        return None
    if parsed != parsed.to_integral_value():
        raise BvlDailyBulletinError(f"BVL quotation {mnemonic} has a fractional {field_name}")
    return int(parsed)


def _parse_optional_date(raw_value: str, field_name: str, mnemonic: str) -> date | None:
    value = raw_value.strip()
    if _is_missing(value):
        return None
    for date_format in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            pass
    raise BvlDailyBulletinError(f"BVL quotation {mnemonic} has an invalid {field_name}")


def _parse_bulletin_date(document: str) -> date:
    matches = _UPDATED_AT_PATTERN.findall(document)
    if len(matches) != 1:
        raise BvlDailyBulletinError("BVL bulletin must contain one unambiguous publication date")
    day_text, month_text, year_text = matches[0]
    month = _SPANISH_MONTHS.get(_strip_accents(month_text.lower()))
    if month is None:
        raise BvlDailyBulletinError("BVL bulletin uses an unsupported publication month")
    try:
        return date(int(year_text), month, int(day_text))
    except ValueError as error:
        raise BvlDailyBulletinError("BVL bulletin publication date is invalid") from error


def _normalize_requested_mnemonics(mnemonics: Sequence[str]) -> tuple[str, ...]:
    if isinstance(mnemonics, (str, bytes)):
        raise BvlDailyBulletinError("mnemonics must be a sequence of identifiers")
    normalized = tuple(mnemonic.strip().upper() for mnemonic in mnemonics)
    if not normalized:
        raise BvlDailyBulletinError("at least one BVL mnemonic is required")
    if len(normalized) > 100:
        raise BvlDailyBulletinError("at most 100 BVL mnemonics may be inspected at once")
    if len(set(normalized)) != len(normalized):
        raise BvlDailyBulletinError("requested BVL mnemonics must be unique")
    if any(_PLAIN_MNEMONIC_PATTERN.fullmatch(mnemonic) is None for mnemonic in normalized):
        raise BvlDailyBulletinError("a requested BVL mnemonic is invalid")
    return normalized


def _split_mnemonic(raw_value: str) -> tuple[str | None, str | None]:
    match = _BULLETIN_MNEMONIC_PATTERN.fullmatch(raw_value.strip())
    if match is None:
        return None, None
    mnemonic = match.group("mnemonic")
    note = match.group("note")
    return mnemonic, note.strip() if note else None


def _validate_final_url(final_url: str) -> None:
    requested = urlsplit(_BULLETIN_DEFINITION.url)
    final = urlsplit(final_url)
    if (
        final.scheme.lower() != "https"
        or final.hostname != requested.hostname
        or final.path != requested.path
    ):
        raise BvlDailyBulletinError(
            "BVL daily bulletin redirected outside its exact official HTTPS path"
        )


def _parse_span(value: str | None) -> int:
    if value is None:
        return 1
    if _SPAN_PATTERN.fullmatch(value) is None:
        raise ValueError("HTML table span must be a positive integer")
    return int(value)


def _is_missing(value: str) -> bool:
    return not value or set(value) == {"."}


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _header_key(value: str) -> str:
    normalized = _strip_accents(value).lower()
    return "".join(character for character in normalized if character.isalnum())


def _strip_accents(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    normalized_name = name.lower()
    return next(
        (value for key, value in headers.items() if key.lower() == normalized_name),
        None,
    )
