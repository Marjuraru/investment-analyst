"""Typed HTTPS client for the official SMV Open Data registry portal."""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.http import HttpFormTransport, HttpResponse

SMV_COMPANIES_URL = (
    "https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Empresas_Inscritas.aspx"
)
SMV_SECURITIES_URL = (
    "https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Valores_Inscritos.aspx"
)
SMV_PORTAL_SCHEMA_VERSION = "smv-open-data-portal-snapshot-v1"
MAX_SMV_PORTAL_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "investment-analyst/0.1.0"
_LEGAL_NAME_PATTERN = re.compile(r"^[^\x00-\x1f\x7f<>]{2,200}$")
_MNEMONIC_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_REPORTED_SECURITY_CODE_PATTERN = re.compile(r"^[A-Z0-9]{8}$")
_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_RESULT_TABLE_ID = "body_GridView1"
_STATUS_ID = "body_lblEstado"
_QUERY_INPUT_ID = "body_txtRazonSocial"
_HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
_COMPANY_HEADERS = (
    "Domicilio",
    "FechaInscripcion",
    "GerenteGeneral",
    "PaginaWeb",
    "PresidenteDirectorio",
    "RazonSocial",
    "ResolucionInscripcion",
    "SeccionRegistro",
    "TipoSector",
)
_SECURITY_HEADERS = (
    "CodigoISIN",
    "Cotizacion",
    "DenominacionValor",
    "FechaInscripcion",
    "FechaUltCot",
    "Moneda",
    "MontoInscrito",
    "NemonicoValor",
    "RazonSocial",
    "ResolucionInscripcion",
    "TipoValor",
)
_CURRENCY_CODES = {
    "DOLARES": "USD",
    "DÓLARES": "USD",
    "SOLES": "PEN",
}


def _reject_float_or_bool(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial registry values must not use float or bool")
    return value


SmvDecimal = Annotated[
    Decimal,
    BeforeValidator(_reject_float_or_bool),
    Field(allow_inf_nan=False),
]
NonNegativeSmvDecimal = Annotated[SmvDecimal, Field(ge=Decimal("0"))]


class SmvOpenDataDataset(StrEnum):
    """Official registry datasets supported by the bounded portal client."""

    REGISTERED_COMPANIES = "registered_companies"
    REGISTERED_SECURITIES = "registered_securities"


class SmvRegisteredCompany(ContractModel):
    """One exact registered-company row returned by SMV."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legal_name: NonEmptyStr
    registration_date: date
    registration_resolution: NonEmptyStr
    registry_section: NonEmptyStr
    sector: NonEmptyStr
    address: NonEmptyStr
    general_manager: NonEmptyStr | None = None
    board_chair: NonEmptyStr | None = None
    website: NonEmptyStr | None = None


class SmvRegisteredSecurity(ContractModel):
    """One SMV security row without misrepresenting its abbreviated code as an ISIN."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legal_name: NonEmptyStr
    security_name: NonEmptyStr
    mnemonic: NonEmptyStr
    reported_security_code: NonEmptyStr
    security_type: NonEmptyStr
    registration_date: date
    registration_resolution: NonEmptyStr
    currency_raw: NonEmptyStr
    currency: NonEmptyStr
    registered_amount: NonNegativeSmvDecimal
    last_quote: NonNegativeSmvDecimal | None = None
    last_quote_date: date | None = None

    @model_validator(mode="after")
    def validate_identity_and_quote(self) -> "SmvRegisteredSecurity":
        """Keep provider identifiers, currency, and optional quotation aligned."""
        if not _MNEMONIC_PATTERN.fullmatch(self.mnemonic):
            raise ValueError("SMV mnemonic is invalid")
        if not _REPORTED_SECURITY_CODE_PATTERN.fullmatch(self.reported_security_code):
            raise ValueError("SMV reported security code must contain eight letters or digits")
        if self.currency not in frozenset(_CURRENCY_CODES.values()):
            raise ValueError("SMV normalized currency is unsupported")
        if (self.last_quote is None) != (self.last_quote_date is None):
            raise ValueError("SMV last quote and date must be present together")
        return self


class SmvOpenDataSnapshot(ContractModel):
    """Canonical parsed result of one exact-name SMV registry query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["smv-open-data-portal-snapshot-v1"] = SMV_PORTAL_SCHEMA_VERSION
    dataset: SmvOpenDataDataset
    query_legal_name: NonEmptyStr
    companies: tuple[SmvRegisteredCompany, ...] = ()
    securities: tuple[SmvRegisteredSecurity, ...] = ()

    @model_validator(mode="after")
    def validate_dataset_payload(self) -> "SmvOpenDataSnapshot":
        """Require the right non-empty, deterministic payload for each dataset."""
        validate_legal_name(self.query_legal_name)
        if self.dataset is SmvOpenDataDataset.REGISTERED_COMPANIES:
            if not self.companies or self.securities:
                raise ValueError("company snapshot must contain only registered companies")
            if any(item.legal_name != self.query_legal_name for item in self.companies):
                raise ValueError("company snapshot contains a different legal name")
            if self.companies != tuple(sorted(self.companies, key=_company_sort_key)):
                raise ValueError("registered companies must be deterministically ordered")
        else:
            if not self.securities or self.companies:
                raise ValueError("security snapshot must contain only registered securities")
            if any(item.legal_name != self.query_legal_name for item in self.securities):
                raise ValueError("security snapshot contains a different legal name")
            if self.securities != tuple(sorted(self.securities, key=_security_sort_key)):
                raise ValueError("registered securities must be deterministically ordered")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives for persistence and CLI output."""
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class SmvOpenDataFetch:
    """One verified portal response and its exact body evidence."""

    snapshot: SmvOpenDataSnapshot
    requested_url: str
    final_url: str
    retrieved_at: datetime
    response_body: bytes
    body_sha256: str
    content_type: str


class SmvOpenDataError(RuntimeError):
    """Safe failure raised when the official portal violates its bounded contract."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class SmvOpenDataNotFoundError(SmvOpenDataError):
    """Raised when an exact legal-name query has no registered result."""


@dataclass(slots=True)
class _PortalTable:
    headers: list[str] = field(default_factory=list)
    rows: list[tuple[str, ...]] = field(default_factory=list)


class _PortalHtmlParser(HTMLParser):
    """Collect only form state, query echo, status, and the exact result table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_fields: dict[str, list[str]] = {}
        self.query_values: list[str] = []
        self.status_parts: list[str] = []
        self.tables: list[_PortalTable] = []
        self._table: _PortalTable | None = None
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_is_header = False
        self._status_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input":
            name = attributes.get("name")
            value = attributes.get("value") or ""
            if name in _HIDDEN_FIELDS:
                self.hidden_fields.setdefault(name, []).append(value)
            if attributes.get("id") == _QUERY_INPUT_ID:
                self.query_values.append(value)
        if attributes.get("id") == _STATUS_ID:
            self._status_depth = 1
        elif self._status_depth:
            self._status_depth += 1
        if tag == "table":
            if self._table is None and attributes.get("id") == _RESULT_TABLE_ID:
                self._table = _PortalTable()
                self._table_depth = 1
            elif self._table is not None:
                self._table_depth += 1
            return
        if self._table is None or self._table_depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._cell_is_header = tag == "th"

    def handle_data(self, data: str) -> None:
        if self._status_depth:
            self.status_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._status_depth:
            self._status_depth -= 1
        if self._table is None:
            return
        if tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self.tables.append(self._table)
                self._table = None
            return
        if self._table_depth != 1:
            return
        if tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            text = _normalize_text("".join(self._cell_parts))
            self._row.append(text)
            if self._cell_is_header:
                self._table.headers.append(text)
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._row and not self._table.headers:
                raise SmvOpenDataError("SMV result table is missing its header row")
            if self._row and tuple(self._row) != tuple(self._table.headers):
                self._table.rows.append(tuple(self._row))
            self._row = None


class SmvOpenDataClient:
    """Query exact registered legal names through the official HTTPS portal."""

    def __init__(
        self,
        transport: HttpFormTransport,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise SmvOpenDataError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def fetch_registered_company(self, legal_name: str) -> SmvOpenDataFetch:
        """Fetch one exact-name company-registry snapshot."""
        return self._fetch(
            dataset=SmvOpenDataDataset.REGISTERED_COMPANIES,
            legal_name=legal_name,
            url=SMV_COMPANIES_URL,
        )

    def fetch_registered_securities(self, legal_name: str) -> SmvOpenDataFetch:
        """Fetch one exact-name registered-securities snapshot."""
        return self._fetch(
            dataset=SmvOpenDataDataset.REGISTERED_SECURITIES,
            legal_name=legal_name,
            url=SMV_SECURITIES_URL,
        )

    def _fetch(
        self,
        *,
        dataset: SmvOpenDataDataset,
        legal_name: str,
        url: str,
    ) -> SmvOpenDataFetch:
        canonical_name = validate_legal_name(legal_name)
        initial = self._transport.get(
            url,
            headers={"Accept": "text/html", "User-Agent": _USER_AGENT},
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_SMV_PORTAL_BYTES,
        )
        initial_text = self._validated_html(initial, expected_url=url)
        state = _parse_form_state(initial_text)
        response = self._transport.post_form(
            url,
            headers={"Accept": "text/html", "User-Agent": _USER_AGENT},
            fields={
                **state,
                "ctl00$body$TipoConsulta": "rbRazSocial",
                "ctl00$body$txtRUC": "",
                "ctl00$body$txtRazonSocial": canonical_name,
                "ctl00$body$Button1": "Ver Resultados",
            },
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_SMV_PORTAL_BYTES,
        )
        response_text = self._validated_html(response, expected_url=url)
        snapshot = parse_smv_portal_snapshot(
            response_text,
            dataset=dataset,
            query_legal_name=canonical_name,
        )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise SmvOpenDataError("SMV retrieval clock must return a timezone-aware datetime")
        retrieved_at = retrieved_at.astimezone(UTC)
        return SmvOpenDataFetch(
            snapshot=snapshot,
            requested_url=url,
            final_url=response.url,
            retrieved_at=retrieved_at,
            response_body=response.body,
            body_sha256=sha256(response.body).hexdigest(),
            content_type=_content_type(response),
        )

    @staticmethod
    def _validated_html(response: HttpResponse, *, expected_url: str) -> str:
        if response.status_code != 200:
            raise SmvOpenDataError(
                f"SMV portal returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if response.body_truncated:
            raise SmvOpenDataError(
                f"SMV portal exceeds the {MAX_SMV_PORTAL_BYTES}-byte safety limit"
            )
        _validate_exact_official_url(response.url, expected_url)
        content_type = _content_type(response)
        if not (content_type == "text/html" or content_type == "application/xhtml+xml"):
            raise SmvOpenDataError("SMV portal returned an unexpected content type")
        try:
            return response.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SmvOpenDataError("SMV portal response is not valid UTF-8") from error


def validate_legal_name(value: str) -> str:
    """Return one exact, bounded legal name without destructive normalization."""
    if not isinstance(value, str):
        raise SmvOpenDataError("SMV legal name must be a string")
    canonical = value.strip()
    if canonical != value or not _LEGAL_NAME_PATTERN.fullmatch(canonical):
        raise SmvOpenDataError("SMV legal name is invalid")
    return canonical


def validate_isin(value: str) -> str:
    """Validate one complete ISO 6166 identifier including its Luhn check digit."""
    canonical = value.strip().upper()
    if canonical != value or not _ISIN_PATTERN.fullmatch(canonical):
        raise SmvOpenDataError("ISIN must contain twelve canonical characters")
    expanded = "".join(
        character if character.isdigit() else str(ord(character) - ord("A") + 10)
        for character in canonical
    )
    total = 0
    for index, character in enumerate(reversed(expanded)):
        digit = int(character)
        if index % 2 == 1:
            digit *= 2
        total += digit // 10 + digit % 10
    if total % 10 != 0:
        raise SmvOpenDataError("ISIN check digit is invalid")
    return canonical


def reported_code_matches_isin(reported_code: str, isin: str) -> bool:
    """Check only explicit substring compatibility; never synthesize a missing ISIN."""
    validated_isin = validate_isin(isin)
    if not _REPORTED_SECURITY_CODE_PATTERN.fullmatch(reported_code):
        raise SmvOpenDataError("SMV reported security code is invalid")
    return reported_code in validated_isin


def parse_smv_portal_snapshot(
    response_text: str,
    *,
    dataset: SmvOpenDataDataset,
    query_legal_name: str,
) -> SmvOpenDataSnapshot:
    """Parse one already-validated portal response into strict registry models."""
    canonical_name = validate_legal_name(query_legal_name)
    parser = _parse_html(response_text)
    if parser.query_values != [canonical_name]:
        raise SmvOpenDataError("SMV portal did not echo the exact legal-name query")
    status = _normalize_text(" ".join(parser.status_parts))
    if not parser.tables:
        if status:
            raise SmvOpenDataNotFoundError(f"SMV registry query returned no result: {status}")
        raise SmvOpenDataError("SMV portal response is missing the result table")
    if len(parser.tables) != 1:
        raise SmvOpenDataError("SMV portal response contains ambiguous result tables")
    table = parser.tables[0]
    if dataset is SmvOpenDataDataset.REGISTERED_COMPANIES:
        if tuple(table.headers) != _COMPANY_HEADERS:
            raise SmvOpenDataError("SMV registered-company headers changed")
        companies = tuple(
            sorted(
                (_parse_company(row, canonical_name) for row in table.rows),
                key=_company_sort_key,
            )
        )
        return SmvOpenDataSnapshot(
            dataset=dataset,
            query_legal_name=canonical_name,
            companies=companies,
        )
    if tuple(table.headers) != _SECURITY_HEADERS:
        raise SmvOpenDataError("SMV registered-security headers changed")
    securities = tuple(
        sorted(
            (_parse_security(row, canonical_name) for row in table.rows),
            key=_security_sort_key,
        )
    )
    return SmvOpenDataSnapshot(
        dataset=dataset,
        query_legal_name=canonical_name,
        securities=securities,
    )


def _parse_company(row: tuple[str, ...], legal_name: str) -> SmvRegisteredCompany:
    if len(row) != len(_COMPANY_HEADERS):
        raise SmvOpenDataError("SMV registered-company row width changed")
    if row[5] != legal_name:
        raise SmvOpenDataError("SMV registered-company row belongs to another legal name")
    return SmvRegisteredCompany(
        address=_required(row[0], "company address"),
        registration_date=_date(row[1], "company registration date"),
        general_manager=_optional(row[2]),
        website=_optional(row[3]),
        board_chair=_optional(row[4]),
        legal_name=row[5],
        registration_resolution=_required(row[6], "company registration resolution"),
        registry_section=_required(row[7], "company registry section"),
        sector=_required(row[8], "company sector"),
    )


def _parse_security(row: tuple[str, ...], legal_name: str) -> SmvRegisteredSecurity:
    if len(row) != len(_SECURITY_HEADERS):
        raise SmvOpenDataError("SMV registered-security row width changed")
    if row[8] != legal_name:
        raise SmvOpenDataError("SMV registered-security row belongs to another legal name")
    currency_raw = _required(row[5], "security currency")
    normalized_currency = _CURRENCY_CODES.get(_normalized_key(currency_raw))
    if normalized_currency is None:
        raise SmvOpenDataError(f"SMV security uses unsupported currency: {currency_raw}")
    last_quote = _optional_decimal(row[1], "last quote")
    last_quote_date = _optional_date(row[4], "last quote date")
    if (last_quote is None) != (last_quote_date is None):
        raise SmvOpenDataError("SMV security last quote and date are incomplete")
    return SmvRegisteredSecurity(
        reported_security_code=_required(row[0], "reported security code").upper(),
        last_quote=last_quote,
        security_name=_required(row[2], "security name"),
        registration_date=_date(row[3], "security registration date"),
        last_quote_date=last_quote_date,
        currency_raw=currency_raw,
        currency=normalized_currency,
        registered_amount=_decimal(row[6], "registered amount"),
        mnemonic=_required(row[7], "security mnemonic").upper(),
        legal_name=row[8],
        registration_resolution=_required(row[9], "security registration resolution"),
        security_type=_required(row[10], "security type"),
    )


def _parse_form_state(response_text: str) -> dict[str, str]:
    parser = _parse_html(response_text)
    state: dict[str, str] = {}
    for field_name in _HIDDEN_FIELDS:
        values = parser.hidden_fields.get(field_name, [])
        if len(values) != 1 or not values[0]:
            raise SmvOpenDataError(f"SMV portal form state is missing {field_name}")
        state[field_name] = values[0]
    return state


def _parse_html(response_text: str) -> _PortalHtmlParser:
    parser = _PortalHtmlParser()
    try:
        parser.feed(response_text)
        parser.close()
    except SmvOpenDataError:
        raise
    except Exception as error:
        raise SmvOpenDataError("SMV portal HTML could not be parsed safely") from error
    return parser


def _company_sort_key(item: SmvRegisteredCompany) -> tuple[str, date, str]:
    return item.legal_name, item.registration_date, item.registration_resolution


def _security_sort_key(
    item: SmvRegisteredSecurity,
) -> tuple[str, str, date, Decimal, str]:
    return (
        item.mnemonic,
        item.reported_security_code,
        item.registration_date,
        item.registered_amount,
        item.security_name,
    )


def _required(value: str, field_name: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        raise SmvOpenDataError(f"SMV {field_name} is empty")
    return normalized


def _optional(value: str) -> str | None:
    normalized = _normalize_text(value)
    return normalized or None


def _date(value: str, field_name: str) -> date:
    parsed = _optional_date(value, field_name)
    if parsed is None:
        raise SmvOpenDataError(f"SMV {field_name} is empty")
    return parsed


def _optional_date(value: str, field_name: str) -> date | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%d/%m/%Y").date()
    except ValueError as error:
        raise SmvOpenDataError(f"SMV {field_name} is invalid") from error


def _decimal(value: str, field_name: str) -> Decimal:
    parsed = _optional_decimal(value, field_name)
    if parsed is None:
        raise SmvOpenDataError(f"SMV {field_name} is empty")
    return parsed


def _optional_decimal(value: str, field_name: str) -> Decimal | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    try:
        parsed = Decimal(normalized.replace(",", ""))
    except InvalidOperation as error:
        raise SmvOpenDataError(f"SMV {field_name} is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise SmvOpenDataError(f"SMV {field_name} is invalid")
    return parsed


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _normalized_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", _normalize_text(value).upper())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _content_type(response: HttpResponse) -> str:
    raw_value = next(
        (value for key, value in response.headers.items() if key.casefold() == "content-type"),
        "",
    )
    return raw_value.split(";", 1)[0].strip().casefold()


def _validate_exact_official_url(final_url: str, expected_url: str) -> None:
    actual = urlsplit(final_url)
    expected = urlsplit(expected_url)
    if (
        actual.scheme.casefold() != "https"
        or actual.hostname != expected.hostname
        or actual.port is not None
        or actual.path.casefold() != expected.path.casefold()
        or actual.query
        or actual.fragment
    ):
        raise SmvOpenDataError("SMV portal redirected outside its exact official HTTPS path")


__all__ = [
    "MAX_SMV_PORTAL_BYTES",
    "SMV_COMPANIES_URL",
    "SMV_PORTAL_SCHEMA_VERSION",
    "SMV_SECURITIES_URL",
    "SmvOpenDataClient",
    "SmvOpenDataDataset",
    "SmvOpenDataError",
    "SmvOpenDataFetch",
    "SmvOpenDataNotFoundError",
    "SmvOpenDataSnapshot",
    "SmvRegisteredCompany",
    "SmvRegisteredSecurity",
    "parse_smv_portal_snapshot",
    "reported_code_matches_isin",
    "validate_isin",
    "validate_legal_name",
]
