"""Read-only SEC EDGAR client for configurable issuer foundation documents."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import sleep as default_sleep
from urllib.parse import urlsplit

from investment_analyst.providers.http import HttpTransport

OFFICIAL_BASE_URL = "https://data.sec.gov"
APPLE_CIK = "0000320193"
APPLE_TICKER = "AAPL"
APPLE_ENTITY_NAME = "Apple Inc."
SUBMISSIONS_PATH = f"/submissions/CIK{APPLE_CIK}.json"
COMPANY_FACTS_PATH = f"/api/xbrl/companyfacts/CIK{APPLE_CIK}.json"
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024
_REQUEST_DELAY_SECONDS = 0.5
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_ENTITY_LEGAL_TOKEN_ALIASES = {
    "co": "company",
    "corp": "corporation",
    "inc": "incorporated",
    "ltd": "limited",
}
_ENTITY_LEGAL_TOKENS = frozenset(_ENTITY_LEGAL_TOKEN_ALIASES.values())
_ENTITY_JURISDICTION_MARKERS = frozenset({"de"})


type SecJsonValue = dict[str, SecJsonValue] | list[SecJsonValue] | str | None


class SecEdgarError(ValueError):
    """Invalid SEC request configuration or issuer document response."""


class SecDocumentType(StrEnum):
    """SEC issuer documents imported by this project step."""

    SUBMISSIONS = "submissions"
    COMPANY_FACTS = "company_facts"


def _entity_name_key(value: str) -> tuple[str, ...]:
    """Compare SEC labels while normalizing typography and standard legal suffixes."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            token = "".join(current)
            tokens.append(_ENTITY_LEGAL_TOKEN_ALIASES.get(token, token))
            current = []
    if current:
        token = "".join(current)
        tokens.append(_ENTITY_LEGAL_TOKEN_ALIASES.get(token, token))
    if (
        len(tokens) >= 3
        and tokens[-1] in _ENTITY_JURISDICTION_MARKERS
        and tokens[-2] in _ENTITY_LEGAL_TOKENS
    ):
        tokens.pop()
    return tuple(tokens)


@dataclass(frozen=True, slots=True, repr=False)
class SecEdgarIdentity:
    """Declared SEC User-Agent identity without secret semantics."""

    user_agent: str

    def __post_init__(self) -> None:
        value = self.user_agent.strip()
        if not value:
            raise SecEdgarError("SEC User-Agent must not be empty")
        if "@" not in value:
            raise SecEdgarError("SEC User-Agent must include a contact email")
        parts = value.split()
        if len(parts) < 2 or not any("@" not in part for part in parts):
            raise SecEdgarError("SEC User-Agent must include a descriptive application name")
        object.__setattr__(self, "user_agent", value)

    def __repr__(self) -> str:
        return "SecEdgarIdentity(user_agent=<redacted>)"


@dataclass(frozen=True, slots=True)
class SecEdgarDocument:
    """Validated parsed SEC document and exact-response checksum metadata."""

    document_type: SecDocumentType
    cik: str
    entity_name: str
    retrieved_at: datetime
    request_url: str
    body: dict[str, SecJsonValue]
    body_sha256: str
    content_length: int

    def __post_init__(self) -> None:
        cik = normalize_cik(self.cik)
        entity_name = self.entity_name.strip()
        if not entity_name:
            raise SecEdgarError("SEC document entity_name must not be empty")
        retrieved_at = _utc_datetime(self.retrieved_at, field_name="retrieved_at")
        _validate_document_url(self.request_url, self.document_type, cik=cik)
        if not isinstance(self.body, dict):
            raise SecEdgarError("SEC document body must be a JSON object")
        body = deepcopy(self.body)
        _validate_json_value(body)
        if self.document_type is SecDocumentType.SUBMISSIONS:
            body_cik, body_entity_name = _validate_submissions(body)
        else:
            body_cik, body_entity_name = _validate_company_facts(body)
        if body_cik != cik or _entity_name_key(body_entity_name) != _entity_name_key(entity_name):
            raise SecEdgarError("SEC document metadata does not match its JSON body")
        if not _SHA256_PATTERN.fullmatch(self.body_sha256):
            raise SecEdgarError("SEC document body_sha256 must be a lowercase SHA-256 digest")
        if self.content_length <= 0 or self.content_length > _MAX_RESPONSE_BYTES:
            raise SecEdgarError("SEC document content length is outside the accepted range")
        object.__setattr__(self, "cik", cik)
        object.__setattr__(self, "entity_name", entity_name)
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "body", body)


@dataclass(frozen=True, slots=True)
class SecIssuerFetchResult:
    """Exactly one issuer's submissions and company-facts documents from one fetch."""

    cik: str
    ticker: str
    entity_name: str
    retrieved_at: datetime
    documents: tuple[SecEdgarDocument, ...]

    def __post_init__(self) -> None:
        cik = normalize_cik(self.cik)
        ticker = _validate_ticker(self.ticker)
        entity_name = self.entity_name.strip()
        if not entity_name:
            raise SecEdgarError("SEC fetch result entity_name must not be empty")
        retrieved_at = _utc_datetime(self.retrieved_at, field_name="retrieved_at")
        document_types = [document.document_type for document in self.documents]
        if document_types.count(SecDocumentType.SUBMISSIONS) != 1:
            raise SecEdgarError("SEC fetch result requires exactly one submissions document")
        if document_types.count(SecDocumentType.COMPANY_FACTS) != 1:
            raise SecEdgarError("SEC fetch result requires exactly one company-facts document")
        if len(self.documents) != 2:
            raise SecEdgarError("SEC fetch result must contain exactly two documents")
        for document in self.documents:
            if document.cik != cik:
                raise SecEdgarError("SEC fetch result documents have an inconsistent CIK")
            if _entity_name_key(document.entity_name) != _entity_name_key(entity_name):
                raise SecEdgarError("SEC fetch result documents have inconsistent entity names")
            if document.retrieved_at != retrieved_at:
                raise SecEdgarError("SEC fetch result documents must share one retrieval timestamp")
        submissions = next(
            document
            for document in self.documents
            if document.document_type is SecDocumentType.SUBMISSIONS
        )
        if ticker not in _submission_tickers(submissions.body):
            raise SecEdgarError(
                f"SEC submissions response does not include configured ticker {ticker}"
            )
        object.__setattr__(self, "cik", cik)
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "entity_name", entity_name)
        object.__setattr__(self, "retrieved_at", retrieved_at)


SecAaplFetchResult = SecIssuerFetchResult


class SecEdgarClient:
    """Fetch two issuer documents from official SEC EDGAR data."""

    def __init__(
        self,
        transport: HttpTransport,
        identity: SecEdgarIdentity,
        *,
        cik: str = APPLE_CIK,
        ticker: str = APPLE_TICKER,
        base_url: str = OFFICIAL_BASE_URL,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = default_sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        normalized_base = base_url.rstrip("/")
        parsed = urlsplit(normalized_base)
        if parsed.scheme.lower() != "https" or parsed.hostname != "data.sec.gov":
            raise SecEdgarError("SEC base_url must use https://data.sec.gov")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise SecEdgarError("SEC base_url must not contain credentials or a custom port")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise SecEdgarError("SEC base_url must not contain a path, query, or fragment")
        if timeout_seconds <= 0:
            raise SecEdgarError("timeout_seconds must be greater than zero")
        normalized_cik = normalize_cik(cik)
        normalized_ticker = _validate_ticker(ticker)
        self._cik = normalized_cik
        self._ticker = normalized_ticker
        self._transport = transport
        self._identity = identity
        self._base_url = normalized_base
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._clock = clock

    def fetch_issuer_documents(self) -> SecIssuerFetchResult:
        """Fetch, parse, validate, and checksum both configured issuer documents."""
        submissions_path = f"/submissions/CIK{self._cik}.json"
        company_facts_path = f"/api/xbrl/companyfacts/CIK{self._cik}.json"
        submissions_url = f"{self._base_url}{submissions_path}"
        company_facts_url = f"{self._base_url}{company_facts_path}"
        submissions_response = self._transport.get(
            submissions_url,
            headers=self._headers(),
            timeout_seconds=self._timeout_seconds,
        )
        self._sleep(_REQUEST_DELAY_SECONDS)
        company_facts_response = self._transport.get(
            company_facts_url,
            headers=self._headers(),
            timeout_seconds=self._timeout_seconds,
        )
        retrieved_at = _utc_datetime(self._clock(), field_name="clock result")
        submissions = _parse_document(
            SecDocumentType.SUBMISSIONS,
            submissions_url,
            submissions_response.status_code,
            submissions_response.url,
            submissions_response.body,
            retrieved_at,
            expected_cik=self._cik,
        )
        company_facts = _parse_document(
            SecDocumentType.COMPANY_FACTS,
            company_facts_url,
            company_facts_response.status_code,
            company_facts_response.url,
            company_facts_response.body,
            retrieved_at,
            expected_cik=self._cik,
        )
        return SecIssuerFetchResult(
            cik=self._cik,
            ticker=self._ticker,
            entity_name=submissions.entity_name,
            retrieved_at=retrieved_at,
            documents=(submissions, company_facts),
        )

    def fetch_aapl_issuer_documents(self) -> SecAaplFetchResult:
        """Compatibility wrapper for the historical fixed-AAPL call."""
        if self._cik != APPLE_CIK or self._ticker != APPLE_TICKER:
            raise SecEdgarError("Apple fetch requires the configured Apple AAPL issuer")
        return self.fetch_issuer_documents()

    def _headers(self) -> Mapping[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": self._identity.user_agent,
        }


def normalize_cik(value: object) -> str:
    """Normalize a numeric or textual CIK to ten decimal digits."""
    if isinstance(value, bool):
        raise SecEdgarError("CIK must be numeric text or an integer")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise SecEdgarError("CIK must be numeric text or an integer")
    if not text.isdecimal() or len(text) > 10:
        raise SecEdgarError("CIK must contain at most ten decimal digits")
    return text.zfill(10)


def _validate_ticker(value: str) -> str:
    if value != value.strip() or not _TICKER_PATTERN.fullmatch(value):
        raise SecEdgarError(
            "SEC ticker must use upper-case letters, digits, dot, or dash without whitespace"
        )
    return value


def _parse_document(
    document_type: SecDocumentType,
    request_url: str,
    status_code: int,
    response_url: str,
    body_bytes: bytes,
    retrieved_at: datetime,
    *,
    expected_cik: str,
) -> SecEdgarDocument:
    if status_code != 200:
        raise SecEdgarError(f"SEC returned HTTP {status_code} for {document_type.value}")
    if response_url != request_url:
        raise SecEdgarError("SEC response redirected away from the requested official document")
    if len(body_bytes) > _MAX_RESPONSE_BYTES:
        raise SecEdgarError("SEC response body exceeds the 50 MiB safety limit")
    try:
        decoded = json.loads(
            body_bytes,
            parse_int=str,
            parse_float=str,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecEdgarError("SEC returned invalid JSON") from error
    if not isinstance(decoded, dict):
        raise SecEdgarError("SEC document response must be a JSON object")
    _validate_json_value(decoded)
    if document_type is SecDocumentType.SUBMISSIONS:
        cik, entity_name = _validate_submissions(decoded)
    else:
        cik, entity_name = _validate_company_facts(decoded)
    if cik != expected_cik:
        raise SecEdgarError("SEC response CIK does not match the configured issuer")
    return SecEdgarDocument(
        document_type=document_type,
        cik=cik,
        entity_name=entity_name,
        retrieved_at=retrieved_at,
        request_url=request_url,
        body=decoded,
        body_sha256=hashlib.sha256(body_bytes).hexdigest(),
        content_length=len(body_bytes),
    )


def _validate_submissions(body: dict[str, SecJsonValue]) -> tuple[str, str]:
    required = ("cik", "name", "tickers", "exchanges", "filings")
    if any(field not in body for field in required):
        raise SecEdgarError("SEC submissions response is missing required fields")
    cik = normalize_cik(body["cik"])
    entity_name = _required_string(body["name"], field_name="submissions name")
    tickers = body["tickers"]
    exchanges = body["exchanges"]
    filings = body["filings"]
    if (
        not isinstance(tickers, list)
        or not tickers
        or not all(isinstance(value, str) and value for value in tickers)
    ):
        raise SecEdgarError("SEC submissions tickers must be a list of strings")
    if not isinstance(exchanges, list) or not all(isinstance(value, str) for value in exchanges):
        raise SecEdgarError("SEC submissions exchanges must be a list of strings")
    if not isinstance(filings, dict):
        raise SecEdgarError("SEC submissions filings must be an object")
    return cik, entity_name


def _validate_company_facts(body: dict[str, SecJsonValue]) -> tuple[str, str]:
    required = ("cik", "entityName", "facts")
    if any(field not in body for field in required):
        raise SecEdgarError("SEC company facts response is missing required fields")
    cik = normalize_cik(body["cik"])
    entity_name = _required_string(body["entityName"], field_name="company facts entityName")
    if not isinstance(body["facts"], dict):
        raise SecEdgarError("SEC company facts facts field must be an object")
    return cik, entity_name


def _submission_tickers(body: dict[str, SecJsonValue]) -> tuple[str, ...]:
    tickers = body.get("tickers")
    if not isinstance(tickers, list) or not all(isinstance(value, str) for value in tickers):
        raise SecEdgarError("SEC submissions tickers must be a list of strings")
    return tuple(tickers)


def _required_string(value: SecJsonValue, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecEdgarError(f"SEC {field_name} must be a non-empty string")
    return value.strip()


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SecEdgarError("SEC JSON object keys must be strings")
            _validate_json_value(item)
        return
    raise SecEdgarError("SEC JSON must contain only objects, arrays, strings, and null")


def _validate_document_url(
    url: str,
    document_type: SecDocumentType,
    *,
    cik: str,
) -> None:
    expected_path = (
        f"/submissions/CIK{cik}.json"
        if document_type is SecDocumentType.SUBMISSIONS
        else f"/api/xbrl/companyfacts/CIK{cik}.json"
    )
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.hostname != "data.sec.gov":
        raise SecEdgarError("SEC document URL must use the official HTTPS domain")
    if parsed.path != expected_path or parsed.query or parsed.fragment:
        raise SecEdgarError("SEC document URL does not match the expected issuer endpoint")


def _reject_json_constant(value: str) -> str:
    raise SecEdgarError(f"non-standard JSON constant is not allowed: {value}")


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecEdgarError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)
