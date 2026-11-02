"""Strict funding-evidence contracts, identities and admission rules.

The funding archive is a research artifact. It is not an operational observation,
metric or diagnostic, and it never becomes point-in-time evidence of first
publication.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.analytics.crypto.derivatives_engine import (
    DECIMAL34,
    FUNDING_WINDOWS,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.historical_models import (
    AvailabilityBasis,
    FeatureState,
    SampleSplit,
    decision_instant,
)

FUNDING_ARCHIVE_SCHEMA_VERSION = "research-funding-archive-v1"
FUNDING_FEATURES_SCHEMA_VERSION = "research-funding-features-v1"
FUNDING_BUNDLE_SCHEMA_VERSION = "research-funding-bundle-v1"

FUNDING_FEATURE_SET_VERSION = "deribit-funding-sums-24h-168h-reconstructed-v1"
FUNDING_ALGORITHM_VERSION = "research-deribit-funding-causal-sums-v1"
FUNDING_AVAILABILITY_POLICY = "historical-funding-end-plus-1h-assumed-v1"
FUNDING_AVAILABILITY_BASIS = "historical_reconstruction_assumed"
FUNDING_DUPLICATE_POLICY = "conflicting-funding-timestamps-rejected-equivalent-collapsed-v1"
FUNDING_METHOD = "public/get_funding_rate_history"

FUNDING_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/research-funding-v1")

FUNDING_COLUMN_24H = "funding_sum_1h_24h"
FUNDING_COLUMN_168H = "funding_sum_1h_168h"
FUNDING_COLUMNS = (FUNDING_COLUMN_24H, FUNDING_COLUMN_168H)
FUNDING_COLUMN_WINDOWS = {
    FUNDING_COLUMN_24H: FUNDING_WINDOWS[0],
    FUNDING_COLUMN_168H: FUNDING_WINDOWS[1],
}
FUNDING_UNIT = "ratio"

DECISION_HOUR_UTC = 12
FUNDING_ANCHOR_HOUR_UTC = 0
ASSUMED_AVAILABILITY_DELAY = timedelta(hours=1)

MAX_ARCHIVE_POINTS = 53_000
MAX_DECISION_DATES = 2_200
MAX_LOGICAL_PAGES = 72
MAX_PAGE_INTERVAL = timedelta(days=31)
MAX_SEALED_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 4 * 1024 * 1024

ARCHIVE_PAGES_FILE_NAME = "pages.jsonl"
MANIFEST_FILE_NAME = "manifest.json"
FUNDING_FEATURES_FILE_NAME = "funding_features.parquet"

MIN_JOINT_COVERAGE = Decimal("0.95")
MIN_TRAIN_JOINT_ELIGIBLE = 150
MIN_VALIDATION_JOINT_ELIGIBLE = 40
MIN_HOLDOUT_JOINT_ELIGIBLE = 40

PROVIDER_DOCUMENTATION_URL = (
    "https://docs.deribit.com/api-reference/market-data/public-get_funding_rate_history"
)
PROVIDER_TERMS_URL = "https://www.deribit.com/legal/terms-of-service/"

FUNDING_LIMITATIONS = (
    "El funding se reconstruye con la recepcion actual del proveedor; una revision"
    " posterior puede diferir de lo publicado en cada timestamp y este artefacto no"
    " demuestra la primera publicacion.",
    "La disponibilidad se asume como timestamp horario mas una hora; no es un SLA"
    " observado ni evidencia point-in-time verificada.",
    "Los huecos horarios se conservan como ausencias y nunca se rellenan con cero,"
    " interpolacion ni la ventana anterior.",
    "El archive es una representacion canonica de filas parseadas, no una copia"
    " byte-exacta de HTTP ni un checksum oficial del proveedor.",
    "El uso previsto es personal y local; no se afirma licencia abierta ni derecho"
    " de redistribucion sobre datos del proveedor.",
    "Las features de funding son descriptivas y no constituyen una senal,"
    " recomendacion, rentabilidad ni permiso para entrenar un modelo.",
)


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("funding financial values must use Decimal")
    return value


FundingDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


class _FundingModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def _milliseconds(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = normalized - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def funding_anchor_instant(day: date) -> datetime:
    """Return the 00:00 UTC funding anchor for one decision date."""
    return datetime.combine(day, time(hour=FUNDING_ANCHOR_HOUR_UTC), tzinfo=UTC)


def assumed_funding_available_instant(funding_timestamp: datetime) -> datetime:
    """Return the declared assumed availability of one hourly funding point."""
    return funding_timestamp.astimezone(UTC) + ASSUMED_AVAILABILITY_DELAY


def funding_window_instants(anchor: datetime, window: int) -> tuple[datetime, datetime]:
    """Return the inclusive first and last hourly timestamp of one funding window."""
    if isinstance(window, bool) or window <= 0:
        raise ValueError("funding window must be a positive integer")
    normalized = anchor.astimezone(UTC)
    if normalized != funding_anchor_instant(normalized.date()):
        raise ValueError("funding window anchor must be 00:00 UTC")
    return normalized - timedelta(hours=window - 1), normalized


class FundingReasonCode(StrEnum):
    """Finite reasons for an unavailable funding feature."""

    POINTS_ABSENT = "funding_points_absent"
    NOT_YET_AVAILABLE = "funding_not_yet_available"
    FEATURE_INCOMPATIBLE = "funding_feature_incompatible"


class FundingAdmissionStatus(StrEnum):
    """Predeclared admission status for the funding complement."""

    FUNDING_DATA_READY = "funding_data_ready"
    INSUFFICIENT_FUNDING_COVERAGE = "insufficient_funding_coverage"


class FundingReason(_FundingModel):
    """One finite funding reason with compact human-readable detail."""

    code: FundingReasonCode
    detail: NonEmptyStr


class FundingAssetIdentity(_FundingModel):
    """Catalog-backed Deribit funding identity for one crypto asset."""

    asset_id: NonEmptyStr
    funding_source_id: NonEmptyStr
    currency: NonEmptyStr
    instrument_name: NonEmptyStr
    funding_venue: Literal["DERIBIT"] = "DERIBIT"
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    crypto_profile: NonEmptyStr

    @model_validator(mode="after")
    def validate_identity(self) -> FundingAssetIdentity:
        if self.asset_class is not AssetClass.CRYPTO:
            raise ValueError("funding evidence supports crypto assets only")
        if self.quote_currency != "USD":
            raise ValueError("funding evidence requires a USD quote")
        if self.instrument_name != f"{self.currency}-PERPETUAL":
            raise ValueError("funding instrument must match CURRENCY-PERPETUAL")
        expected = f"deribit:{self.instrument_name.casefold()}:funding-rate-history"
        if self.funding_source_id != expected:
            raise ValueError("funding_source_id must match the perpetual instrument")
        return self


class FundingArchiveRequest(_FundingModel):
    """Declared funding range derived from the original decision grid."""

    asset_id: NonEmptyStr
    decision_start_date: date
    decision_end_date: date
    decision_count: int = Field(ge=1)
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    expected_points: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_request(self) -> FundingArchiveRequest:
        if self.decision_start_date > self.decision_end_date:
            raise ValueError("decision_start_date must not be later than decision_end_date")
        expected_count = (self.decision_end_date - self.decision_start_date).days + 1
        if self.decision_count != expected_count:
            raise ValueError("decision_count must match the inclusive decision dates")
        if self.decision_count > MAX_DECISION_DATES:
            raise ValueError(f"a funding archive is limited to {MAX_DECISION_DATES} decisions")
        expected_start = funding_anchor_instant(self.decision_start_date) - timedelta(
            hours=FUNDING_COLUMN_WINDOWS[FUNDING_COLUMN_168H] - 1
        )
        expected_end = funding_anchor_instant(self.decision_end_date) + ASSUMED_AVAILABILITY_DELAY
        if self.requested_start != expected_start or self.requested_end != expected_end:
            raise ValueError("requested interval must follow the fixed 168h window plus one hour")
        if self.requested_start >= self.requested_end:
            raise ValueError("requested funding interval must be non-empty")
        seconds = (self.requested_end - self.requested_start).total_seconds()
        if seconds % 3_600:
            raise ValueError("requested funding interval must align to whole hours")
        expected_points = int(seconds // 3_600)
        if self.expected_points != expected_points:
            raise ValueError("expected_points must match the requested hourly interval")
        if self.expected_points > MAX_ARCHIVE_POINTS:
            raise ValueError(f"a funding archive is limited to {MAX_ARCHIVE_POINTS} points")
        return self

    def decision_dates(self) -> tuple[date, ...]:
        """Return the inclusive ordered decision dates."""
        count = (self.decision_end_date - self.decision_start_date).days + 1
        return tuple(self.decision_start_date + timedelta(days=offset) for offset in range(count))


def funding_archive_request(
    *,
    asset_id: str,
    decision_start_date: date,
    decision_end_date: date,
) -> FundingArchiveRequest:
    """Derive the fixed funding acquisition interval from a decision grid."""
    decision_count = (decision_end_date - decision_start_date).days + 1
    requested_start = funding_anchor_instant(decision_start_date) - timedelta(
        hours=FUNDING_COLUMN_WINDOWS[FUNDING_COLUMN_168H] - 1
    )
    requested_end = funding_anchor_instant(decision_end_date) + ASSUMED_AVAILABILITY_DELAY
    expected_points = int((requested_end - requested_start).total_seconds() // 3_600)
    return FundingArchiveRequest(
        asset_id=asset_id,
        decision_start_date=decision_start_date,
        decision_end_date=decision_end_date,
        decision_count=decision_count,
        requested_start=requested_start,
        requested_end=requested_end,
        expected_points=expected_points,
    )


class FundingPoint(_FundingModel):
    """One hourly funding row preserved with its exact provider payload."""

    point_id: UUID
    page_index: int = Field(ge=0)
    timestamp: UTCDateTime
    index_price: FundingDecimal
    prev_index_price: FundingDecimal
    interest_1h: FundingDecimal
    interest_8h: FundingDecimal
    raw_payload: dict[NonEmptyStr, NonEmptyStr]
    assumed_available_at: UTCDateTime

    @model_validator(mode="after")
    def validate_point(self) -> FundingPoint:
        keys = {"timestamp", "index_price", "prev_index_price", "interest_1h", "interest_8h"}
        if set(self.raw_payload) != keys:
            raise ValueError("funding raw_payload must contain the exact provider fields")
        if self.timestamp.minute or self.timestamp.second or self.timestamp.microsecond:
            raise ValueError("funding timestamps must align to whole UTC hours")
        if self.assumed_available_at != assumed_funding_available_instant(self.timestamp):
            raise ValueError("funding availability must follow the end-plus-one-hour policy")
        if self.raw_payload["timestamp"] != str(_milliseconds(self.timestamp)):
            raise ValueError("raw_payload timestamp must match the parsed timestamp")
        values = (self.index_price, self.prev_index_price, self.interest_1h, self.interest_8h)
        if not all(value.is_finite() for value in values):
            raise ValueError("funding values must be finite Decimals")
        if self.index_price <= 0 or self.prev_index_price <= 0:
            raise ValueError("funding index prices must be positive")
        return self


class FundingArchivePage(_FundingModel):
    """One completed logical acquisition page."""

    page_index: int = Field(ge=0)
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    request_url: NonEmptyStr
    started_at: UTCDateTime
    finished_at: UTCDateTime
    elapsed_milliseconds: int = Field(ge=0)
    retrieved_at: UTCDateTime
    point_count: int = Field(ge=0)
    points_sha256: str
    points_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page(self) -> FundingArchivePage:
        if not self.requested_start < self.requested_end:
            raise ValueError("funding page interval must be non-empty")
        if self.requested_end - self.requested_start > MAX_PAGE_INTERVAL:
            raise ValueError("a funding page must not exceed the 31-day provider interval")
        if self.started_at > self.finished_at:
            raise ValueError("funding page start must not be after its finish")
        if not self.started_at <= self.retrieved_at <= self.finished_at:
            raise ValueError("funding page reception must fall inside its measured interval")
        if self.points_bytes > MAX_PAGE_BYTES:
            raise ValueError("funding page exceeds the declared byte budget")
        _validate_sha256(self.points_sha256, "points_sha256")
        return self


class FundingArchiveCounts(_FundingModel):
    """Auditable requested, observed and absent hourly points."""

    requested_points: int = Field(ge=1)
    observed_points: int = Field(ge=0)
    missing_points: int = Field(ge=0)
    page_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_counts(self) -> FundingArchiveCounts:
        if self.observed_points + self.missing_points != self.requested_points:
            raise ValueError("observed plus missing points must add up to requested")
        if self.observed_points > self.requested_points:
            raise ValueError("observed points must not exceed requested points")
        if self.page_count > MAX_LOGICAL_PAGES:
            raise ValueError("funding archive exceeds the bounded logical page count")
        return self


class FundingArchiveManifest(_FundingModel):
    """Sealed acquisition manifest for one canonical funding archive."""

    schema_version: Literal["research-funding-archive-v1"] = FUNDING_ARCHIVE_SCHEMA_VERSION
    archive_id: UUID
    asset: FundingAssetIdentity
    request: FundingArchiveRequest
    method: Literal["public/get_funding_rate_history"] = FUNDING_METHOD
    pages: tuple[FundingArchivePage, ...]
    counts: FundingArchiveCounts
    retrieved_at: UTCDateTime
    availability_policy: Literal["historical-funding-end-plus-1h-assumed-v1"] = (
        FUNDING_AVAILABILITY_POLICY
    )
    availability_basis: AvailabilityBasis = AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED
    strict_pit_verified: Literal[False] = False
    duplicate_policy: Literal["conflicting-funding-timestamps-rejected-equivalent-collapsed-v1"] = (
        FUNDING_DUPLICATE_POLICY
    )
    semantic_sha256: str
    pages_file_name: Literal["pages.jsonl"] = ARCHIVE_PAGES_FILE_NAME
    pages_sha256: str
    pages_bytes: int = Field(ge=0)
    provider_documentation_url: NonEmptyStr = PROVIDER_DOCUMENTATION_URL
    provider_terms_url: NonEmptyStr = PROVIDER_TERMS_URL
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> FundingArchiveManifest:
        if not self.pages:
            raise ValueError("a sealed funding archive requires at least one page")
        if len(self.pages) != self.counts.page_count:
            raise ValueError("manifest page count must match its pages")
        if self.pages[0].requested_start != self.request.requested_start:
            raise ValueError("first page must start at the requested funding interval")
        if self.pages[-1].requested_end != self.request.requested_end:
            raise ValueError("last page must end at the requested funding interval")
        for index, page in enumerate(self.pages):
            if page.page_index != index:
                raise ValueError("funding pages must be ordered and index-contiguous")
            if index and page.requested_start != self.pages[index - 1].requested_end:
                raise ValueError("funding pages must cover the interval without gaps or overlap")
        if sum(page.point_count for page in self.pages) != self.counts.observed_points:
            raise ValueError("manifest observed points must match its pages")
        if self.counts.requested_points != self.request.expected_points:
            raise ValueError("manifest requested points must match the funding request")
        if self.retrieved_at != max(page.retrieved_at for page in self.pages):
            raise ValueError("archive reception must be the latest page reception")
        if self.pages_bytes > MAX_SEALED_ARCHIVE_BYTES:
            raise ValueError("funding archive exceeds the declared byte budget")
        _validate_sha256(self.semantic_sha256, "semantic_sha256")
        _validate_sha256(self.pages_sha256, "pages_sha256")
        return self


class FundingArchive(_FundingModel):
    """One sealed archive: manifest plus its ordered hourly points."""

    archive_id: UUID
    manifest: FundingArchiveManifest
    points: tuple[FundingPoint, ...]

    @model_validator(mode="after")
    def validate_archive(self) -> FundingArchive:
        if self.archive_id != self.manifest.archive_id:
            raise ValueError("archive identity must match its manifest")
        if len(self.points) != self.manifest.counts.observed_points:
            raise ValueError("archive points must match the manifest observed count")
        timestamps = tuple(point.timestamp for point in self.points)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("archive points must be ordered by timestamp")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("archive points must not repeat a timestamp")
        page_indices = tuple(point.page_index for point in self.points)
        if page_indices != tuple(sorted(page_indices)):
            raise ValueError("archive points must follow page order")
        pages = {page.page_index: page for page in self.manifest.pages}
        for page in self.manifest.pages:
            actual_count = sum(1 for point in self.points if point.page_index == page.page_index)
            if actual_count != page.point_count:
                raise ValueError("archive page point count must match its declared count")
        for point in self.points:
            page = pages.get(point.page_index)
            if page is None:
                raise ValueError("archive point references an unknown page")
            if not page.requested_start <= point.timestamp < page.requested_end:
                raise ValueError("archive point is outside its declared page interval")
            if point.point_id != funding_point_id(
                funding_source_id=self.manifest.asset.funding_source_id,
                timestamp=point.timestamp,
                index_price=point.index_price,
                prev_index_price=point.prev_index_price,
                interest_1h=point.interest_1h,
                interest_8h=point.interest_8h,
                raw_payload=point.raw_payload,
            ):
                raise ValueError("archive point identity is not deterministic")
        expected = funding_archive_semantic_hash(
            archive_id=self.archive_id,
            points=self.points,
        )
        if expected != self.manifest.semantic_sha256:
            raise ValueError("funding archive semantic hash does not match its content")
        return self


class FundingArchiveReference(_FundingModel):
    """Content-addressed reference to the conserved funding archive."""

    archive_id: UUID
    semantic_sha256: str
    pages_sha256: str
    pages_bytes: int = Field(ge=0)
    retrieved_at: UTCDateTime
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    expected_points: int = Field(ge=1)
    observed_points: int = Field(ge=0)
    missing_points: int = Field(ge=0)
    page_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_reference(self) -> FundingArchiveReference:
        _validate_sha256(self.semantic_sha256, "semantic_sha256")
        _validate_sha256(self.pages_sha256, "pages_sha256")
        if not self.requested_start < self.requested_end:
            raise ValueError("archive reference requires a non-empty interval")
        if self.observed_points + self.missing_points != self.expected_points:
            raise ValueError("archive reference counts must add up to expected points")
        return self


class FundingBaseDatasetReference(_FundingModel):
    """Identity-only reference to the original reconstructed dataset."""

    dataset_id: UUID
    dataset_content_sha256: str
    feature_set_version: NonEmptyStr
    experiment_spec_id: NonEmptyStr
    target_version: NonEmptyStr
    asset_id: NonEmptyStr
    start_date: date
    end_date: date
    known_at: UTCDateTime
    row_count: int = Field(ge=0)
    base_archive_id: UUID
    base_archive_semantic_sha256: str

    @model_validator(mode="after")
    def validate_reference(self) -> FundingBaseDatasetReference:
        _validate_sha256(self.dataset_content_sha256, "dataset_content_sha256")
        _validate_sha256(self.base_archive_semantic_sha256, "base_archive_semantic_sha256")
        if self.start_date > self.end_date:
            raise ValueError("base dataset start_date must not be later than end_date")
        expected = (self.end_date - self.start_date).days + 1
        if self.row_count != expected:
            raise ValueError("base dataset row_count must match its inclusive decision dates")
        return self


class FundingFeatureRequest(_FundingModel):
    """Decision grid and retrieval cut used by the funding complement."""

    decision_start_date: date
    decision_end_date: date
    decision_count: int = Field(ge=1)
    known_at: UTCDateTime

    @model_validator(mode="after")
    def validate_request(self) -> FundingFeatureRequest:
        if self.decision_start_date > self.decision_end_date:
            raise ValueError("decision_start_date must not be later than decision_end_date")
        expected = (self.decision_end_date - self.decision_start_date).days + 1
        if self.decision_count != expected:
            raise ValueError("decision_count must match the inclusive decision dates")
        if self.decision_count > MAX_DECISION_DATES:
            raise ValueError(f"a funding complement is limited to {MAX_DECISION_DATES} decisions")
        if decision_instant(self.decision_end_date) > self.known_at:
            raise ValueError("known_at must not precede the final decision time")
        return self


class FundingColumnSpec(_FundingModel):
    """One declared Parquet column with exact encoding and nullability."""

    name: NonEmptyStr
    data_type: Literal["text", "integer", "boolean"]
    nullable: bool
    unit: NonEmptyStr | None = None
    description: NonEmptyStr


FUNDING_ROWS_SCHEMA: tuple[FundingColumnSpec, ...] = (
    FundingColumnSpec(
        name="sample_key",
        data_type="text",
        nullable=False,
        description="Original sample identity from the reconstructed dataset.",
    ),
    FundingColumnSpec(
        name="decision_at",
        data_type="text",
        nullable=False,
        unit="iso8601-utc",
        description="Original preregistered 12:00 UTC decision instant.",
    ),
    FundingColumnSpec(
        name=FUNDING_COLUMN_24H,
        data_type="text",
        nullable=True,
        unit=FUNDING_UNIT,
        description="Exact sum of the 24 hourly interest_1h values ending at D00 UTC.",
    ),
    FundingColumnSpec(
        name=FUNDING_COLUMN_168H,
        data_type="text",
        nullable=True,
        unit=FUNDING_UNIT,
        description="Exact sum of the 168 hourly interest_1h values ending at D00 UTC.",
    ),
    FundingColumnSpec(
        name="funding_state",
        data_type="text",
        nullable=False,
        description="Aggregated availability state of both funding features.",
    ),
    FundingColumnSpec(
        name="eligibility_base",
        data_type="boolean",
        nullable=False,
        description="Original reconstructed sample eligibility after split purge.",
    ),
    FundingColumnSpec(
        name="eligibility_joint",
        data_type="boolean",
        nullable=False,
        description="Original eligibility and both funding features complete.",
    ),
    FundingColumnSpec(
        name="split",
        data_type="text",
        nullable=False,
        description="Original temporal split assigned before eligibility filtering.",
    ),
    FundingColumnSpec(
        name="purged",
        data_type="boolean",
        nullable=False,
        description="Original split purge status.",
    ),
    FundingColumnSpec(
        name="feature_evidence_json",
        data_type="text",
        nullable=False,
        description="Canonical JSON evidence for both funding windows.",
    ),
    FundingColumnSpec(
        name="funding_feature_hash",
        data_type="text",
        nullable=False,
        description="Deterministic causal funding feature identity.",
    ),
    FundingColumnSpec(
        name="archive_id",
        data_type="text",
        nullable=False,
        description="Content-addressed reference to the funding archive.",
    ),
    FundingColumnSpec(
        name="archive_semantic_sha256",
        data_type="text",
        nullable=False,
        description="Semantic digest of the funding archive.",
    ),
    FundingColumnSpec(
        name="base_dataset_id",
        data_type="text",
        nullable=False,
        description="Identity of the original reconstructed dataset.",
    ),
    FundingColumnSpec(
        name="base_dataset_content_sha256",
        data_type="text",
        nullable=False,
        description="Semantic digest of the original reconstructed dataset.",
    ),
)


class FundingArtifactSchema(_FundingModel):
    """Published schema, order and encoding contract for the funding rows."""

    schema_version: Literal["research-funding-features-v1"] = FUNDING_FEATURES_SCHEMA_VERSION
    encoding_version: Literal["research-funding-features-encoding-v1"] = (
        "research-funding-features-encoding-v1"
    )
    feature_encoding: Literal["canonical-decimal-text"] = "canonical-decimal-text"
    timestamp_encoding: Literal["iso8601-utc"] = "iso8601-utc"
    id_encoding: Literal["uuid-text"] = "uuid-text"
    order_by: Literal["decision_at,sample_key"] = "decision_at,sample_key"
    funding_columns: tuple[NonEmptyStr, ...] = FUNDING_COLUMNS
    columns: tuple[FundingColumnSpec, ...] = FUNDING_ROWS_SCHEMA

    @model_validator(mode="after")
    def validate_schema(self) -> FundingArtifactSchema:
        if tuple(item.name for item in self.columns) != tuple(
            item.name for item in FUNDING_ROWS_SCHEMA
        ):
            raise ValueError("funding artifact schema must preserve the declared column order")
        if self.funding_columns != FUNDING_COLUMNS:
            raise ValueError("funding artifact schema must declare the two funding columns")
        return self


class FundingFeatureEvidence(_FundingModel):
    """Causal evidence for one funding window."""

    column_name: NonEmptyStr
    state: FeatureState
    value: FundingDecimal | None = None
    unit: Literal["ratio"] = FUNDING_UNIT
    algorithm_version: Literal["research-deribit-funding-causal-sums-v1"] = (
        FUNDING_ALGORITHM_VERSION
    )
    window_start: UTCDateTime
    window_end: UTCDateTime
    expected_points: int = Field(ge=1)
    observed_points: int = Field(ge=0)
    input_digest: str | None = None
    assumed_available_at: UTCDateTime | None = None
    reasons: tuple[FundingReason, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> FundingFeatureEvidence:
        window = FUNDING_COLUMN_WINDOWS.get(self.column_name)
        if window is None:
            raise ValueError("funding evidence requires a declared funding column")
        if self.expected_points != window:
            raise ValueError("funding evidence must declare the fixed window size")
        expected_start, expected_end = funding_window_instants(self.window_end, window)
        if self.window_start != expected_start or self.window_end != expected_end:
            raise ValueError("funding evidence window must end at the anchor hour")
        if self.observed_points > self.expected_points:
            raise ValueError("observed funding points must not exceed expected points")
        if self.input_digest is not None:
            _validate_sha256(self.input_digest, "input_digest")
        if self.state is FeatureState.AVAILABLE:
            if self.value is None or not self.value.is_finite():
                raise ValueError("available funding features require a finite Decimal value")
            if self.observed_points != self.expected_points:
                raise ValueError("available funding features require the complete window")
            if self.input_digest is None or self.assumed_available_at is None:
                raise ValueError("available funding features require lineage and availability")
            if self.reasons:
                raise ValueError("available funding features must not carry failure reasons")
            expected_available = assumed_funding_available_instant(self.window_end)
            if self.assumed_available_at != expected_available:
                raise ValueError("available funding availability must follow the declared policy")
        else:
            if self.value is not None:
                raise ValueError("unavailable funding features must not expose a numeric value")
            if not self.reasons:
                raise ValueError("unavailable funding features require at least one reason")
            if self.state is FeatureState.NOT_YET_AVAILABLE and (
                self.observed_points != self.expected_points or self.assumed_available_at is None
            ):
                raise ValueError("not-yet-available funding requires a complete observed window")
        return self


def aggregate_funding_state(features: tuple[FundingFeatureEvidence, ...]) -> FeatureState:
    """Collapse both funding feature states with contradiction-first precedence."""
    states = {item.state for item in features}
    for state in (
        FeatureState.INCOMPATIBLE,
        FeatureState.NOT_YET_AVAILABLE,
        FeatureState.MISSING,
    ):
        if state in states:
            return state
    return FeatureState.AVAILABLE


class FundingSample(_FundingModel):
    """One original decision with its two causal funding features."""

    sample_key: UUID
    decision_at: UTCDateTime
    features: tuple[FundingFeatureEvidence, ...]
    funding_state: FeatureState
    eligibility_base: bool
    eligibility_joint: bool
    split: SampleSplit
    purged: bool
    funding_feature_hash: UUID

    @model_validator(mode="after")
    def validate_sample(self) -> FundingSample:
        if tuple(item.column_name for item in self.features) != FUNDING_COLUMNS:
            raise ValueError("funding features must preserve the declared column order")
        if self.decision_at != decision_instant(self.decision_at.date()):
            raise ValueError("funding decision_at must be the preregistered 12:00 UTC instant")
        if self.funding_state is not aggregate_funding_state(self.features):
            raise ValueError("funding_state must aggregate its feature states")
        both_available = all(item.state is FeatureState.AVAILABLE for item in self.features)
        if self.eligibility_joint != (self.eligibility_base and both_available):
            raise ValueError("eligibility_joint requires base eligibility and both features")
        if self.purged and self.eligibility_base:
            raise ValueError("a purged sample cannot remain base-eligible")
        expected_hash = funding_feature_hash(
            sample_key=self.sample_key,
            decision_at=self.decision_at,
            features=self.features,
        )
        if self.funding_feature_hash != expected_hash:
            raise ValueError("funding feature hash is not deterministic")
        return self


class FundingDatasetCounts(_FundingModel):
    """Auditable counts for both funding features and joint eligibility."""

    requested: int = Field(ge=0)
    base_eligible: int = Field(ge=0)
    feature_24h_available: int = Field(ge=0)
    feature_24h_missing: int = Field(ge=0)
    feature_24h_not_yet_available: int = Field(ge=0)
    feature_24h_incompatible: int = Field(ge=0)
    feature_168h_available: int = Field(ge=0)
    feature_168h_missing: int = Field(ge=0)
    feature_168h_not_yet_available: int = Field(ge=0)
    feature_168h_incompatible: int = Field(ge=0)
    funding_state_available: int = Field(ge=0)
    funding_state_missing: int = Field(ge=0)
    funding_state_not_yet_available: int = Field(ge=0)
    funding_state_incompatible: int = Field(ge=0)
    joint_available: int = Field(ge=0)
    base_eligible_joint_available: int = Field(ge=0)
    purged: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> FundingDatasetCounts:
        partitions = (
            (
                self.feature_24h_available,
                self.feature_24h_missing,
                self.feature_24h_not_yet_available,
                self.feature_24h_incompatible,
            ),
            (
                self.feature_168h_available,
                self.feature_168h_missing,
                self.feature_168h_not_yet_available,
                self.feature_168h_incompatible,
            ),
            (
                self.funding_state_available,
                self.funding_state_missing,
                self.funding_state_not_yet_available,
                self.funding_state_incompatible,
            ),
        )
        for partition in partitions:
            if sum(partition) != self.requested:
                raise ValueError("funding state partitions must add up to requested")
        if self.base_eligible > self.requested:
            raise ValueError("base_eligible must not exceed requested")
        if self.joint_available > self.requested:
            raise ValueError("joint_available must not exceed requested")
        if self.base_eligible_joint_available > min(self.base_eligible, self.joint_available):
            raise ValueError("base eligible joint availability is inconsistent")
        if self.purged > self.requested:
            raise ValueError("purged must not exceed requested")
        return self


class FundingSplitCoverage(_FundingModel):
    """Coverage counters for one original temporal split."""

    split: SampleSplit
    requested: int = Field(ge=0)
    base_eligible: int = Field(ge=0)
    feature_24h_available: int = Field(ge=0)
    feature_168h_available: int = Field(ge=0)
    joint_available: int = Field(ge=0)
    base_eligible_joint_available: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_split(self) -> FundingSplitCoverage:
        for value in (
            self.base_eligible,
            self.feature_24h_available,
            self.feature_168h_available,
            self.joint_available,
            self.base_eligible_joint_available,
        ):
            if value > self.requested:
                raise ValueError("split coverage counter must not exceed requested")
        if self.base_eligible_joint_available > min(
            self.base_eligible,
            self.joint_available,
        ):
            raise ValueError("split joint availability is inconsistent")
        return self


class FundingAdmission(_FundingModel):
    """Predeclared admission decision without statistical promotion."""

    status: FundingAdmissionStatus
    coverage_numerator: int = Field(ge=0)
    coverage_denominator: int = Field(ge=0)
    coverage_fraction: FundingDecimal | None = None
    train_joint_eligible: int = Field(ge=0)
    validation_joint_eligible: int = Field(ge=0)
    holdout_joint_eligible: int = Field(ge=0)
    unmet_requirements: tuple[NonEmptyStr, ...] = ()

    @staticmethod
    def unmet_requirements_for(
        *,
        numerator: int,
        denominator: int,
        train_joint_eligible: int,
        validation_joint_eligible: int,
        holdout_joint_eligible: int,
    ) -> tuple[str, ...]:
        unmet: list[str] = []
        if denominator == 0:
            unmet.append("coverage_denominator is zero")
        else:
            with localcontext(DECIMAL34):
                coverage = Decimal(numerator) / Decimal(denominator)
            if coverage < MIN_JOINT_COVERAGE:
                unmet.append(f"joint_coverage {numerator}/{denominator} < {MIN_JOINT_COVERAGE}")
        if train_joint_eligible < MIN_TRAIN_JOINT_ELIGIBLE:
            unmet.append(
                f"train_joint_eligible {train_joint_eligible} < {MIN_TRAIN_JOINT_ELIGIBLE}"
            )
        if validation_joint_eligible < MIN_VALIDATION_JOINT_ELIGIBLE:
            unmet.append(
                "validation_joint_eligible"
                f" {validation_joint_eligible} < {MIN_VALIDATION_JOINT_ELIGIBLE}"
            )
        if holdout_joint_eligible < MIN_HOLDOUT_JOINT_ELIGIBLE:
            unmet.append(
                f"holdout_joint_eligible {holdout_joint_eligible} < {MIN_HOLDOUT_JOINT_ELIGIBLE}"
            )
        return tuple(unmet)

    @classmethod
    def from_counts(
        cls,
        *,
        numerator: int,
        denominator: int,
        train_joint_eligible: int,
        validation_joint_eligible: int,
        holdout_joint_eligible: int,
    ) -> FundingAdmission:
        unmet = cls.unmet_requirements_for(
            numerator=numerator,
            denominator=denominator,
            train_joint_eligible=train_joint_eligible,
            validation_joint_eligible=validation_joint_eligible,
            holdout_joint_eligible=holdout_joint_eligible,
        )
        if denominator == 0:
            coverage: Decimal | None = None
        else:
            with localcontext(DECIMAL34):
                coverage = Decimal(numerator) / Decimal(denominator)
        status = (
            FundingAdmissionStatus.FUNDING_DATA_READY
            if not unmet
            else FundingAdmissionStatus.INSUFFICIENT_FUNDING_COVERAGE
        )
        return cls(
            status=status,
            coverage_numerator=numerator,
            coverage_denominator=denominator,
            coverage_fraction=coverage,
            train_joint_eligible=train_joint_eligible,
            validation_joint_eligible=validation_joint_eligible,
            holdout_joint_eligible=holdout_joint_eligible,
            unmet_requirements=unmet,
        )

    @model_validator(mode="after")
    def validate_admission(self) -> FundingAdmission:
        unmet = self.unmet_requirements_for(
            numerator=self.coverage_numerator,
            denominator=self.coverage_denominator,
            train_joint_eligible=self.train_joint_eligible,
            validation_joint_eligible=self.validation_joint_eligible,
            holdout_joint_eligible=self.holdout_joint_eligible,
        )
        expected_status = (
            FundingAdmissionStatus.FUNDING_DATA_READY
            if not unmet
            else FundingAdmissionStatus.INSUFFICIENT_FUNDING_COVERAGE
        )
        if self.status is not expected_status:
            raise ValueError("funding admission status must follow the preregistered floors")
        if unmet != self.unmet_requirements:
            raise ValueError("funding unmet requirements must match observed counts")
        if self.coverage_denominator == 0:
            if self.coverage_fraction is not None:
                raise ValueError("zero denominator requires an absent coverage fraction")
        else:
            if self.coverage_fraction is None:
                raise ValueError("non-zero denominator requires a coverage fraction")
            with localcontext(DECIMAL34):
                expected = Decimal(self.coverage_numerator) / Decimal(self.coverage_denominator)
            if self.coverage_fraction != expected:
                raise ValueError("coverage fraction must equal the exact quotient")
        return self


class FundingGapCount(_FundingModel):
    """One aggregated funding gap by split, feature and primary reason."""

    split: SampleSplit
    column_name: NonEmptyStr
    state: FeatureState
    reason_code: FundingReasonCode | None = None
    count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_gap(self) -> FundingGapCount:
        if self.column_name not in FUNDING_COLUMNS:
            raise ValueError("funding gap must reference a declared funding column")
        if self.state is FeatureState.AVAILABLE:
            raise ValueError("funding gaps must describe an unavailable feature")
        return self


class FundingFeatureManifest(_FundingModel):
    """Deterministic semantic manifest for the funding complement."""

    schema_version: Literal["research-funding-features-v1"] = FUNDING_FEATURES_SCHEMA_VERSION
    bundle_id: UUID
    bundle_content_sha256: str
    feature_set_version: Literal["deribit-funding-sums-24h-168h-reconstructed-v1"] = (
        FUNDING_FEATURE_SET_VERSION
    )
    availability_policy: Literal["historical-funding-end-plus-1h-assumed-v1"] = (
        FUNDING_AVAILABILITY_POLICY
    )
    availability_basis: AvailabilityBasis = AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED
    strict_pit_verified: Literal[False] = False
    base_dataset: FundingBaseDatasetReference
    funding_asset: FundingAssetIdentity
    archive: FundingArchiveReference
    request: FundingFeatureRequest
    artifact_schema: FundingArtifactSchema = FundingArtifactSchema()
    counts: FundingDatasetCounts
    splits: tuple[FundingSplitCoverage, ...]
    admission: FundingAdmission
    gaps: tuple[FundingGapCount, ...]
    rows: tuple[FundingSample, ...]
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> FundingFeatureManifest:
        _validate_sha256(self.bundle_content_sha256, "bundle_content_sha256")
        if self.base_dataset.asset_id != self.funding_asset.asset_id:
            raise ValueError("base dataset and funding asset must identify the same asset")
        if self.base_dataset.start_date != self.request.decision_start_date:
            raise ValueError("base dataset start must match the funding request")
        if self.base_dataset.end_date != self.request.decision_end_date:
            raise ValueError("base dataset end must match the funding request")
        if self.base_dataset.row_count != self.request.decision_count:
            raise ValueError("base dataset row count must match the funding request")
        if self.base_dataset.known_at > self.request.known_at:
            raise ValueError("funding known_at cannot precede the base dataset cut")
        if self.archive.retrieved_at > self.request.known_at:
            raise ValueError("funding known_at cannot precede the archive reception")
        expected_start = funding_anchor_instant(self.request.decision_start_date) - timedelta(
            hours=FUNDING_COLUMN_WINDOWS[FUNDING_COLUMN_168H] - 1
        )
        expected_end = (
            funding_anchor_instant(self.request.decision_end_date) + ASSUMED_AVAILABILITY_DELAY
        )
        if self.archive.requested_start != expected_start:
            raise ValueError("archive start must match the funding request")
        if self.archive.requested_end != expected_end:
            raise ValueError("archive end must match the funding request")
        if (
            self.archive.expected_points
            != (self.request.decision_count - 1) * 24 + FUNDING_COLUMN_WINDOWS[FUNDING_COLUMN_168H]
        ):
            raise ValueError("archive expected points must match the decision grid")
        if tuple(item.split for item in self.splits) != (
            SampleSplit.TRAIN,
            SampleSplit.VALIDATION,
            SampleSplit.HOLDOUT,
        ):
            raise ValueError("funding manifest must publish the three splits in order")
        if len(self.rows) != self.counts.requested:
            raise ValueError("funding rows must match the requested count")
        if self.request.decision_count != len(self.rows):
            raise ValueError("funding decision count must match the published rows")
        sample_keys = tuple(item.sample_key for item in self.rows)
        if len(set(sample_keys)) != len(sample_keys):
            raise ValueError("funding sample identities must be unique")
        decision_times = tuple(item.decision_at for item in self.rows)
        if decision_times != tuple(sorted(decision_times)):
            raise ValueError("funding rows must be ordered by decision_at")
        if any(
            current - previous != timedelta(days=1)
            for previous, current in zip(decision_times, decision_times[1:], strict=False)
        ):
            raise ValueError("funding rows must follow a contiguous daily decision grid")
        if decision_times and (
            decision_times[0].date() != self.request.decision_start_date
            or decision_times[-1].date() != self.request.decision_end_date
        ):
            raise ValueError("funding rows must match the declared decision grid")
        if self.counts != funding_counts(self.rows):
            raise ValueError("funding counts must match the published rows")
        if self.splits != funding_split_coverage(self.rows):
            raise ValueError("funding split coverage must match the published rows")
        if self.gaps != funding_gap_counts(self.rows):
            raise ValueError("funding gaps must match the published rows")
        expected_admission = funding_admission(self.splits)
        if self.admission != expected_admission:
            raise ValueError("funding admission must match the published coverage")
        expected_bundle_id = funding_bundle_id(
            base_dataset=self.base_dataset,
            funding_asset=self.funding_asset,
            archive=self.archive,
            request=self.request,
            rows=self.rows,
        )
        if self.bundle_id != expected_bundle_id:
            raise ValueError("funding bundle identity is not deterministic")
        expected_hash = funding_bundle_content_hash(
            bundle_id=self.bundle_id,
            rows=self.rows,
        )
        if self.bundle_content_sha256 != expected_hash:
            raise ValueError("funding bundle content hash does not match its rows")
        return self


class FundingArtifact(_FundingModel):
    """Physical descriptor of the sealed funding Parquet artifact."""

    rows_file_name: Literal["funding_features.parquet"] = FUNDING_FEATURES_FILE_NAME
    rows_sha256: str
    rows_bytes: int = Field(ge=0)
    exported_at: UTCDateTime
    physical_bytes_note: NonEmptyStr = (
        "Parquet bytes identify this artifact only; the semantic funding hash carries"
        " cross-version equality."
    )

    @model_validator(mode="after")
    def validate_artifact(self) -> FundingArtifact:
        _validate_sha256(self.rows_sha256, "rows_sha256")
        if self.rows_bytes > MAX_BUNDLE_BYTES:
            raise ValueError("funding artifact exceeds the declared byte budget")
        return self


class FundingExportManifest(_FundingModel):
    """Sealed export manifest wrapping the semantic funding complement."""

    schema_version: Literal["research-funding-bundle-v1"] = FUNDING_BUNDLE_SCHEMA_VERSION
    features: FundingFeatureManifest
    artifact: FundingArtifact

    @model_validator(mode="after")
    def validate_export(self) -> FundingExportManifest:
        if self.artifact.exported_at < self.features.archive.retrieved_at:
            raise ValueError("funding export cannot precede the archive reception")
        return self


class FundingBundle(_FundingModel):
    """Complete semantic funding complement with its deterministic rows."""

    manifest: FundingFeatureManifest
    rows: tuple[FundingSample, ...]

    @model_validator(mode="after")
    def validate_bundle(self) -> FundingBundle:
        if self.rows != self.manifest.rows:
            raise ValueError("funding bundle rows must match its manifest")
        return self


class FundingBundleSummary(_FundingModel):
    """CLI-safe aggregate summary without per-sample feature values."""

    bundle_id: UUID
    bundle_content_sha256: str
    feature_set_version: NonEmptyStr
    availability_policy: NonEmptyStr
    availability_basis: AvailabilityBasis
    strict_pit_verified: Literal[False]
    base_dataset: FundingBaseDatasetReference
    funding_asset: FundingAssetIdentity
    archive: FundingArchiveReference
    request: FundingFeatureRequest
    counts: FundingDatasetCounts
    splits: tuple[FundingSplitCoverage, ...]
    admission: FundingAdmission
    gaps: tuple[FundingGapCount, ...]
    limitations: tuple[NonEmptyStr, ...]

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


def funding_point_id(
    *,
    funding_source_id: str,
    timestamp: datetime,
    index_price: Decimal,
    prev_index_price: Decimal,
    interest_1h: Decimal,
    interest_8h: Decimal,
    raw_payload: dict[str, str],
) -> UUID:
    """Derive one funding point identity from its exact provider content."""
    payload = {
        "artifact": "funding_point_id",
        "schema_version": FUNDING_ARCHIVE_SCHEMA_VERSION,
        "funding_source_id": funding_source_id,
        "timestamp": timestamp,
        "index_price": index_price,
        "prev_index_price": prev_index_price,
        "interest_1h": interest_1h,
        "interest_8h": interest_8h,
        "raw_payload": raw_payload,
    }
    return uuid5(FUNDING_NAMESPACE, analytical_content_hash(payload))


def funding_input_digest(
    *,
    funding_source_id: str,
    column_name: str,
    window: int,
    points: tuple[tuple[datetime, Decimal], ...],
) -> str:
    """Return the compact causal digest of one observed funding window."""
    payload = {
        "artifact": "funding_input",
        "schema_version": FUNDING_FEATURES_SCHEMA_VERSION,
        "feature_set_version": FUNDING_FEATURE_SET_VERSION,
        "availability_policy": FUNDING_AVAILABILITY_POLICY,
        "funding_source_id": funding_source_id,
        "column_name": column_name,
        "window": window,
        "points": tuple((timestamp, value) for timestamp, value in points),
    }
    return analytical_content_hash(payload)


def _feature_identity_payload(feature: FundingFeatureEvidence) -> dict[str, object]:
    return {
        "column_name": feature.column_name,
        "state": feature.state,
        "value": feature.value,
        "unit": feature.unit,
        "algorithm_version": feature.algorithm_version,
        "window_start": feature.window_start,
        "window_end": feature.window_end,
        "expected_points": feature.expected_points,
        "observed_points": feature.observed_points,
        "input_digest": feature.input_digest,
        "assumed_available_at": feature.assumed_available_at,
        "reasons": feature.reasons,
    }


def funding_feature_hash(
    *,
    sample_key: UUID,
    decision_at: datetime,
    features: tuple[FundingFeatureEvidence, ...],
) -> UUID:
    """Derive the causal funding feature identity without labels or base rows."""
    payload = {
        "artifact": "funding_feature_hash",
        "schema_version": FUNDING_FEATURES_SCHEMA_VERSION,
        "feature_set_version": FUNDING_FEATURE_SET_VERSION,
        "availability_policy": FUNDING_AVAILABILITY_POLICY,
        "sample_key": sample_key,
        "decision_at": decision_at,
        "features": tuple(_feature_identity_payload(item) for item in features),
    }
    return uuid5(FUNDING_NAMESPACE, analytical_content_hash(payload))


def funding_archive_reference(archive: FundingArchive) -> FundingArchiveReference:
    """Return the content-addressed reference for one verified archive."""
    manifest = archive.manifest
    return FundingArchiveReference(
        archive_id=archive.archive_id,
        semantic_sha256=manifest.semantic_sha256,
        pages_sha256=manifest.pages_sha256,
        pages_bytes=manifest.pages_bytes,
        retrieved_at=manifest.retrieved_at,
        requested_start=manifest.request.requested_start,
        requested_end=manifest.request.requested_end,
        expected_points=manifest.counts.requested_points,
        observed_points=manifest.counts.observed_points,
        missing_points=manifest.counts.missing_points,
        page_count=manifest.counts.page_count,
    )


def funding_archive_semantic_hash(
    *,
    archive_id: UUID,
    points: tuple[FundingPoint, ...],
) -> str:
    """Return the semantic digest of the archive identity and ordered points."""
    payload = {
        "artifact": "funding_archive_content",
        "schema_version": FUNDING_ARCHIVE_SCHEMA_VERSION,
        "archive_id": archive_id,
        "points": tuple(
            {
                "point_id": point.point_id,
                "page_index": point.page_index,
                "timestamp": point.timestamp,
                "index_price": point.index_price,
                "prev_index_price": point.prev_index_price,
                "interest_1h": point.interest_1h,
                "interest_8h": point.interest_8h,
                "raw_payload": point.raw_payload,
            }
            for point in points
        ),
    }
    return analytical_content_hash(payload)


def _archive_request_payload(request: FundingArchiveRequest) -> dict[str, object]:
    return {
        "asset_id": request.asset_id,
        "decision_start_date": request.decision_start_date.isoformat(),
        "decision_end_date": request.decision_end_date.isoformat(),
        "decision_count": request.decision_count,
        "requested_start": request.requested_start,
        "requested_end": request.requested_end,
        "expected_points": request.expected_points,
    }


def _base_dataset_payload(reference: FundingBaseDatasetReference) -> dict[str, object]:
    return {
        "dataset_id": reference.dataset_id,
        "dataset_content_sha256": reference.dataset_content_sha256,
        "feature_set_version": reference.feature_set_version,
        "experiment_spec_id": reference.experiment_spec_id,
        "target_version": reference.target_version,
        "asset_id": reference.asset_id,
        "start_date": reference.start_date.isoformat(),
        "end_date": reference.end_date.isoformat(),
        "known_at": reference.known_at,
        "row_count": reference.row_count,
        "base_archive_id": reference.base_archive_id,
        "base_archive_semantic_sha256": reference.base_archive_semantic_sha256,
    }


def _feature_request_payload(request: FundingFeatureRequest) -> dict[str, object]:
    return {
        "decision_start_date": request.decision_start_date.isoformat(),
        "decision_end_date": request.decision_end_date.isoformat(),
        "decision_count": request.decision_count,
        "known_at": request.known_at,
    }


def funding_archive_id(
    *,
    asset: FundingAssetIdentity,
    request: FundingArchiveRequest,
    pages: tuple[FundingArchivePage, ...],
    retrieved_at: datetime,
    points: tuple[FundingPoint, ...],
) -> UUID:
    """Derive the archive identity from acquisition metadata and ordered content."""
    payload = {
        "artifact": "funding_archive_id",
        "schema_version": FUNDING_ARCHIVE_SCHEMA_VERSION,
        "asset": asset,
        "request": _archive_request_payload(request),
        "pages": tuple(
            {
                "page_index": page.page_index,
                "requested_start": page.requested_start,
                "requested_end": page.requested_end,
                "request_url": page.request_url,
                "retrieved_at": page.retrieved_at,
                "point_count": page.point_count,
                "points_sha256": page.points_sha256,
            }
            for page in pages
        ),
        "retrieved_at": retrieved_at,
        "points": tuple(str(point.point_id) for point in points),
    }
    return uuid5(FUNDING_NAMESPACE, analytical_content_hash(payload))


def funding_sample_payload(sample: FundingSample) -> dict[str, object]:
    """Return the complete deterministic payload of one funding sample."""
    return {
        "sample_key": sample.sample_key,
        "decision_at": sample.decision_at,
        "features": tuple(_feature_identity_payload(item) for item in sample.features),
        "funding_state": sample.funding_state,
        "eligibility_base": sample.eligibility_base,
        "eligibility_joint": sample.eligibility_joint,
        "split": sample.split,
        "purged": sample.purged,
        "funding_feature_hash": sample.funding_feature_hash,
    }


def funding_bundle_id(
    *,
    base_dataset: FundingBaseDatasetReference,
    funding_asset: FundingAssetIdentity,
    archive: FundingArchiveReference,
    request: FundingFeatureRequest,
    rows: tuple[FundingSample, ...],
) -> UUID:
    """Derive the bundle identity from references, request and ordered rows."""
    payload = {
        "artifact": "funding_bundle_id",
        "schema_version": FUNDING_FEATURES_SCHEMA_VERSION,
        "feature_set_version": FUNDING_FEATURE_SET_VERSION,
        "availability_policy": FUNDING_AVAILABILITY_POLICY,
        "base_dataset": _base_dataset_payload(base_dataset),
        "funding_asset": funding_asset,
        "archive": archive,
        "request": _feature_request_payload(request),
        "rows": tuple(
            {
                "sample_key": row.sample_key,
                "decision_at": row.decision_at,
                "funding_feature_hash": row.funding_feature_hash,
                "eligibility_base": row.eligibility_base,
                "eligibility_joint": row.eligibility_joint,
                "split": row.split,
                "purged": row.purged,
            }
            for row in rows
        ),
    }
    return uuid5(FUNDING_NAMESPACE, analytical_content_hash(payload))


def funding_bundle_content_hash(
    *,
    bundle_id: UUID,
    rows: tuple[FundingSample, ...],
) -> str:
    """Return the semantic bundle digest independent from physical Parquet bytes."""
    payload = {
        "artifact": "funding_bundle_content",
        "bundle_id": bundle_id,
        "rows": tuple(funding_sample_payload(row) for row in rows),
    }
    return analytical_content_hash(payload)


def funding_counts(rows: tuple[FundingSample, ...]) -> FundingDatasetCounts:
    """Derive the exact funding counters from published rows."""

    def feature_count(column: str, state: FeatureState) -> int:
        return sum(
            1
            for row in rows
            for feature in row.features
            if feature.column_name == column and feature.state is state
        )

    def state_count(state: FeatureState) -> int:
        return sum(1 for row in rows if row.funding_state is state)

    return FundingDatasetCounts(
        requested=len(rows),
        base_eligible=sum(1 for row in rows if row.eligibility_base),
        feature_24h_available=feature_count(FUNDING_COLUMN_24H, FeatureState.AVAILABLE),
        feature_24h_missing=feature_count(FUNDING_COLUMN_24H, FeatureState.MISSING),
        feature_24h_not_yet_available=feature_count(
            FUNDING_COLUMN_24H, FeatureState.NOT_YET_AVAILABLE
        ),
        feature_24h_incompatible=feature_count(FUNDING_COLUMN_24H, FeatureState.INCOMPATIBLE),
        feature_168h_available=feature_count(FUNDING_COLUMN_168H, FeatureState.AVAILABLE),
        feature_168h_missing=feature_count(FUNDING_COLUMN_168H, FeatureState.MISSING),
        feature_168h_not_yet_available=feature_count(
            FUNDING_COLUMN_168H, FeatureState.NOT_YET_AVAILABLE
        ),
        feature_168h_incompatible=feature_count(FUNDING_COLUMN_168H, FeatureState.INCOMPATIBLE),
        funding_state_available=state_count(FeatureState.AVAILABLE),
        funding_state_missing=state_count(FeatureState.MISSING),
        funding_state_not_yet_available=state_count(FeatureState.NOT_YET_AVAILABLE),
        funding_state_incompatible=state_count(FeatureState.INCOMPATIBLE),
        joint_available=sum(
            1
            for row in rows
            if all(feature.state is FeatureState.AVAILABLE for feature in row.features)
        ),
        base_eligible_joint_available=sum(1 for row in rows if row.eligibility_joint),
        purged=sum(1 for row in rows if row.purged),
    )


def funding_split_coverage(
    rows: tuple[FundingSample, ...],
) -> tuple[FundingSplitCoverage, ...]:
    """Derive exact coverage counters for each original temporal split."""
    coverage: list[FundingSplitCoverage] = []
    for split in (SampleSplit.TRAIN, SampleSplit.VALIDATION, SampleSplit.HOLDOUT):
        assigned = tuple(row for row in rows if row.split is split)
        coverage.append(
            FundingSplitCoverage(
                split=split,
                requested=len(assigned),
                base_eligible=sum(1 for row in assigned if row.eligibility_base),
                feature_24h_available=sum(
                    1 for row in assigned if row.features[0].state is FeatureState.AVAILABLE
                ),
                feature_168h_available=sum(
                    1 for row in assigned if row.features[1].state is FeatureState.AVAILABLE
                ),
                joint_available=sum(
                    1
                    for row in assigned
                    if all(feature.state is FeatureState.AVAILABLE for feature in row.features)
                ),
                base_eligible_joint_available=sum(1 for row in assigned if row.eligibility_joint),
            )
        )
    return tuple(coverage)


def funding_admission(splits: tuple[FundingSplitCoverage, ...]) -> FundingAdmission:
    """Derive admission from the original train/validation/holdout coverage."""
    by_split = {item.split: item for item in splits}
    train = by_split[SampleSplit.TRAIN]
    validation = by_split[SampleSplit.VALIDATION]
    holdout = by_split[SampleSplit.HOLDOUT]
    numerator = train.base_eligible_joint_available + validation.base_eligible_joint_available
    denominator = train.base_eligible + validation.base_eligible
    return FundingAdmission.from_counts(
        numerator=numerator,
        denominator=denominator,
        train_joint_eligible=train.base_eligible_joint_available,
        validation_joint_eligible=validation.base_eligible_joint_available,
        holdout_joint_eligible=holdout.base_eligible_joint_available,
    )


def funding_gap_counts(rows: tuple[FundingSample, ...]) -> tuple[FundingGapCount, ...]:
    """Aggregate unavailable feature evidence by split, column and primary reason."""
    counts: dict[tuple[SampleSplit, str, FeatureState, FundingReasonCode | None], int] = {}
    for row in rows:
        for feature in row.features:
            if feature.state is FeatureState.AVAILABLE:
                continue
            reason = feature.reasons[0].code if feature.reasons else None
            key = (row.split, feature.column_name, feature.state, reason)
            counts[key] = counts.get(key, 0) + 1
    return tuple(
        FundingGapCount(
            split=split,
            column_name=column,
            state=state,
            reason_code=reason,
            count=count,
        )
        for (split, column, state, reason), count in sorted(
            counts.items(),
            key=lambda item: (
                item[0][0].value,
                item[0][1],
                item[0][2].value,
                "" if item[0][3] is None else item[0][3].value,
            ),
        )
    )


def funding_bundle_summary(manifest: FundingFeatureManifest) -> FundingBundleSummary:
    """Return the CLI-safe projection of one funding manifest."""
    return FundingBundleSummary(
        bundle_id=manifest.bundle_id,
        bundle_content_sha256=manifest.bundle_content_sha256,
        feature_set_version=manifest.feature_set_version,
        availability_policy=manifest.availability_policy,
        availability_basis=manifest.availability_basis,
        strict_pit_verified=False,
        base_dataset=manifest.base_dataset,
        funding_asset=manifest.funding_asset,
        archive=manifest.archive,
        request=manifest.request,
        counts=manifest.counts,
        splits=manifest.splits,
        admission=manifest.admission,
        gaps=manifest.gaps,
        limitations=manifest.limitations,
    )


__all__ = [
    "ARCHIVE_PAGES_FILE_NAME",
    "ASSUMED_AVAILABILITY_DELAY",
    "DECIMAL34",
    "DECISION_HOUR_UTC",
    "FUNDING_ALGORITHM_VERSION",
    "FUNDING_ARCHIVE_SCHEMA_VERSION",
    "FUNDING_AVAILABILITY_BASIS",
    "FUNDING_AVAILABILITY_POLICY",
    "FUNDING_BUNDLE_SCHEMA_VERSION",
    "FUNDING_COLUMNS",
    "FUNDING_COLUMN_168H",
    "FUNDING_COLUMN_24H",
    "FUNDING_COLUMN_WINDOWS",
    "FUNDING_DUPLICATE_POLICY",
    "FUNDING_FEATURE_SET_VERSION",
    "FUNDING_FEATURES_FILE_NAME",
    "FUNDING_FEATURES_SCHEMA_VERSION",
    "FUNDING_LIMITATIONS",
    "FUNDING_METHOD",
    "FUNDING_NAMESPACE",
    "FUNDING_ROWS_SCHEMA",
    "FUNDING_UNIT",
    "MANIFEST_FILE_NAME",
    "MAX_ARCHIVE_POINTS",
    "MAX_BUNDLE_BYTES",
    "MAX_DECISION_DATES",
    "MAX_LOGICAL_PAGES",
    "MAX_PAGE_BYTES",
    "MAX_PAGE_INTERVAL",
    "MAX_SEALED_ARCHIVE_BYTES",
    "MIN_HOLDOUT_JOINT_ELIGIBLE",
    "MIN_JOINT_COVERAGE",
    "MIN_TRAIN_JOINT_ELIGIBLE",
    "MIN_VALIDATION_JOINT_ELIGIBLE",
    "PROVIDER_DOCUMENTATION_URL",
    "PROVIDER_TERMS_URL",
    "FundingAdmission",
    "FundingAdmissionStatus",
    "FundingArchive",
    "FundingArchiveCounts",
    "FundingArchiveManifest",
    "FundingArchivePage",
    "FundingArchiveReference",
    "FundingArchiveRequest",
    "FundingArtifact",
    "FundingArtifactSchema",
    "FundingAssetIdentity",
    "FundingBaseDatasetReference",
    "FundingBundle",
    "FundingBundleSummary",
    "FundingColumnSpec",
    "FundingDatasetCounts",
    "FundingExportManifest",
    "FundingFeatureEvidence",
    "FundingFeatureManifest",
    "FundingFeatureRequest",
    "FundingGapCount",
    "FundingPoint",
    "FundingReason",
    "FundingReasonCode",
    "FundingSample",
    "FundingSplitCoverage",
    "aggregate_funding_state",
    "assumed_funding_available_instant",
    "funding_admission",
    "funding_anchor_instant",
    "funding_archive_id",
    "funding_archive_reference",
    "funding_archive_request",
    "funding_archive_semantic_hash",
    "funding_bundle_content_hash",
    "funding_bundle_id",
    "funding_bundle_summary",
    "funding_counts",
    "funding_feature_hash",
    "funding_gap_counts",
    "funding_input_digest",
    "funding_point_id",
    "funding_sample_payload",
    "funding_split_coverage",
    "funding_window_instants",
]
