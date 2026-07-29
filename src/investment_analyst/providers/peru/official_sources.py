"""Bounded, read-only probes for official SMV and BVL public sources."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.http import HttpTransport

PROBE_SCHEMA_VERSION = "peru-official-source-probe-v1"
MAX_INSPECTED_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 15.0
_USER_AGENT = "investment-analyst/0.1.0"

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PeruOfficialSource(StrEnum):
    """Stable identifiers for the official endpoints checked in phase zero."""

    SMV_REGISTERED_SECURITIES = "smv:registered-securities"
    BVL_DAILY_EQUITY_BULLETIN = "bvl:daily-equity-bulletin"
    BVL_DAILY_BULLETIN_NOTES = "bvl:daily-bulletin-notes"


class SourceContractStatus(StrEnum):
    """Current contractual maturity; availability does not imply permission."""

    OPEN_DATA_ODBL = "open_data_odbl"
    PUBLIC_DOCUMENT_TERMS_REVIEW_REQUIRED = "public_document_terms_review_required"


@dataclass(frozen=True, slots=True)
class OfficialSourceDefinition:
    """Immutable definition used to keep every probe explicit and deterministic."""

    source: PeruOfficialSource
    url: str
    purpose: str
    contract_status: SourceContractStatus
    required_markers: tuple[bytes, ...]


OFFICIAL_SOURCE_DEFINITIONS = (
    OfficialSourceDefinition(
        source=PeruOfficialSource.SMV_REGISTERED_SECURITIES,
        url=("https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Valores_Inscritos.aspx"),
        purpose="Catálogo oficial de valores inscritos",
        contract_status=SourceContractStatus.OPEN_DATA_ODBL,
        required_markers=(b"Valores Inscritos", b"ISIN del valor"),
    ),
    OfficialSourceDefinition(
        source=PeruOfficialSource.BVL_DAILY_EQUITY_BULLETIN,
        url="https://documents.bvl.com.pe/pubdif/boldia/stockq.htm",
        purpose="Boletín diario oficial de renta variable",
        contract_status=SourceContractStatus.PUBLIC_DOCUMENT_TERMS_REVIEW_REQUIRED,
        required_markers=(b"Mercado de Renta Variable", b"Cotizaciones"),
    ),
    OfficialSourceDefinition(
        source=PeruOfficialSource.BVL_DAILY_BULLETIN_NOTES,
        url="https://documents.bvl.com.pe/pubdif/boldia/bolnota.htm",
        purpose="Notas y reglas del boletín diario oficial",
        contract_status=SourceContractStatus.PUBLIC_DOCUMENT_TERMS_REVIEW_REQUIRED,
        required_markers=(b"CALENDARIO DE FERIADOS", b"FUENTES DE INFORMACION"),
    ),
)


class OfficialSourceProbeResult(ContractModel):
    """Auditable metadata for one successful bounded source inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: PeruOfficialSource
    purpose: NonEmptyStr
    contract_status: SourceContractStatus
    requested_url: NonEmptyStr
    final_url: NonEmptyStr
    checked_at: UTCDateTime
    http_status: Literal[200] = 200
    content_type: NonEmptyStr
    inspected_bytes: int = Field(ge=1, le=MAX_INSPECTED_BYTES)
    body_truncated: bool
    inspected_prefix_sha256: Sha256Hex
    verified_markers: tuple[NonEmptyStr, ...] = Field(min_length=1)


class PeruOfficialSourceProbeReport(ContractModel):
    """Versioned phase-zero report that never contains source document bodies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["peru-official-source-probe-v1"] = PROBE_SCHEMA_VERSION
    checked_at: UTCDateTime
    max_inspected_bytes_per_source: Literal[65_536] = MAX_INSPECTED_BYTES
    results: tuple[OfficialSourceProbeResult, ...]
    all_sources_available: Literal[True] = True
    persistence_performed: Literal[False] = False

    @model_validator(mode="after")
    def validate_complete_matrix(self) -> "PeruOfficialSourceProbeReport":
        expected_sources = tuple(definition.source for definition in OFFICIAL_SOURCE_DEFINITIONS)
        actual_sources = tuple(result.source for result in self.results)
        if actual_sources != expected_sources:
            raise ValueError("probe results must cover every official source in canonical order")
        if any(result.checked_at != self.checked_at for result in self.results):
            raise ValueError("all probe results must share the report timestamp")
        return self


class PeruOfficialSourceProbeError(RuntimeError):
    """Safe failure raised when an official source no longer matches its contract."""


class PeruOfficialSourceProbe:
    """Inspect small prefixes of official sources without writing to local storage."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if timeout_seconds <= 0:
            raise PeruOfficialSourceProbeError("timeout_seconds must be greater than zero")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def run(self) -> PeruOfficialSourceProbeReport:
        """Check the complete phase-zero matrix in deterministic source order."""
        checked_at = self._clock()
        results = tuple(
            self._inspect(definition, checked_at) for definition in OFFICIAL_SOURCE_DEFINITIONS
        )
        return PeruOfficialSourceProbeReport(checked_at=checked_at, results=results)

    def _inspect(
        self,
        definition: OfficialSourceDefinition,
        checked_at: datetime,
    ) -> OfficialSourceProbeResult:
        response = self._transport.get(
            definition.url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "User-Agent": _USER_AGENT,
            },
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=MAX_INSPECTED_BYTES,
        )
        if response.status_code != 200:
            raise PeruOfficialSourceProbeError(
                f"{definition.source} returned HTTP {response.status_code}"
            )
        self._validate_final_url(definition, response.url)
        missing_markers = tuple(
            marker.decode("ascii")
            for marker in definition.required_markers
            if marker not in response.body
        )
        if missing_markers:
            raise PeruOfficialSourceProbeError(
                f"{definition.source} response is missing expected contract markers"
            )
        content_type = _header_value(response.headers, "content-type")
        if not content_type or "text/html" not in content_type.lower():
            raise PeruOfficialSourceProbeError(
                f"{definition.source} returned an unexpected content type"
            )
        return OfficialSourceProbeResult(
            source=definition.source,
            purpose=definition.purpose,
            contract_status=definition.contract_status,
            requested_url=definition.url,
            final_url=response.url,
            checked_at=checked_at,
            content_type=content_type,
            inspected_bytes=len(response.body),
            body_truncated=response.body_truncated,
            inspected_prefix_sha256=sha256(response.body).hexdigest(),
            verified_markers=tuple(
                marker.decode("ascii") for marker in definition.required_markers
            ),
        )

    @staticmethod
    def _validate_final_url(definition: OfficialSourceDefinition, final_url: str) -> None:
        requested = urlsplit(definition.url)
        final = urlsplit(final_url)
        if final.scheme.lower() != "https" or final.hostname != requested.hostname:
            raise PeruOfficialSourceProbeError(
                f"{definition.source} redirected outside its official HTTPS host"
            )


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    normalized_name = name.lower()
    return next(
        (value for key, value in headers.items() if key.lower() == normalized_name),
        None,
    )
