"""Strict contracts and deterministic identities for the reconstructed history dataset."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import AssetClass, DataFrequency


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("historical financial values must use Decimal")
    return value


HistoricalDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]

EXPERIMENT_SPEC_ID = "crypto-daily-positive-return-5d-historical-v1"
TARGET_VERSION = "crypto-forward-close-return-5d-v1"
FEATURE_SET_VERSION = "crypto-market-reconstructed-3-v1"
AVAILABILITY_POLICY = "historical-close-plus-1h-assumed-v1"
AVAILABILITY_BASIS = "historical_reconstruction_assumed"

ARCHIVE_SCHEMA_VERSION = "research-historical-archive-v1"
DATASET_SCHEMA_VERSION = "research-historical-dataset-v1"
MANIFEST_SCHEMA_VERSION = "research-historical-manifest-v1"

HISTORICAL_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/predictive-historical-v1")

DECISION_HOUR_UTC = 12
ASSUMED_AVAILABILITY_DELAY = timedelta(hours=1)
MAX_ARCHIVE_DAYS = 2560
MAX_DECISION_DATES = 2200
MAX_ARCHIVE_REQUESTS = 12
MAX_CANDLES_PER_REQUEST = 300
SUPPORTED_FREQUENCY = DataFrequency.DAY_1
SUPPORTED_ASSET_CLASS = AssetClass.CRYPTO
SUPPORTED_QUOTE_CURRENCY = "USD"
SUPPORTED_GRANULARITY_SECONDS = 86_400

CANDLES_FILE_NAME = "candles.jsonl"
MANIFEST_FILE_NAME = "manifest.json"
ROWS_FILE_NAME = "rows.parquet"

PROVIDER_DOCUMENTATION_URL = (
    "https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles"
)
PROVIDER_TERMS_URL = "https://www.coinbase.com/legal/user_agreement"
DUPLICATE_POLICY = "conflicting-bar-timestamps-rejected-equivalent-collapsed-v1"

CLOSE_RETURN_COLUMN = "close_return_1d"
RSI_COLUMN = "rsi_14_fraction"
MACD_HISTOGRAM_COLUMN = "macd_12_26_9_histogram_close_ratio"
FEATURE_COLUMNS = (CLOSE_RETURN_COLUMN, RSI_COLUMN, MACD_HISTOGRAM_COLUMN)
FEATURE_RATIO_UNIT = "ratio"
CLOSE_UNIT = "USD"

CLOSE_RETURN_ALGORITHM_VERSION = "historical-close-ratio-1d-v1"
RSI_ALGORITHM_VERSION = "historical-rsi-wilder-14-v1"
MACD_ALGORITHM_VERSION = "historical-macd-12-26-9-v1"

RSI_WINDOW = 14
RSI_WARMUP_BARS = RSI_WINDOW + 1
MACD_FAST_WINDOW = 12
MACD_SLOW_WINDOW = 26
MACD_SIGNAL_WINDOW = 9
MACD_WARMUP_BARS = MACD_SLOW_WINDOW + MACD_SIGNAL_WINDOW - 1
FEATURE_WARMUP_BARS = {
    CLOSE_RETURN_COLUMN: 1,
    RSI_COLUMN: RSI_WARMUP_BARS,
    MACD_HISTOGRAM_COLUMN: MACD_WARMUP_BARS,
}

LABEL_ENTRY_OFFSET_DAYS = 0
LABEL_EXIT_OFFSET_DAYS = 5
LABEL_BAR_COUNT = 6

SPLIT_POLICY_VERSION = "historical-temporal-split-v1"
TRAIN_FRACTION = Decimal("0.60")
VALIDATION_FRACTION = Decimal("0.20")
HOLDOUT_FRACTION = Decimal("0.20")
EMBARGO_DAYS = 5
READINESS_MIN_ELIGIBLE = 300
READINESS_MIN_TRAIN = 150
READINESS_MIN_VALIDATION = 40
READINESS_MIN_HOLDOUT = 40
READINESS_MIN_TRAIN_CLASS = 20

DECIMAL34 = Context(prec=34, rounding=ROUND_HALF_EVEN)

DATASET_LIMITATIONS = (
    "Cada fila describe una reconstruccion bajo el supuesto de disponibilidad"
    " posterior al cierre de barra; no es evidencia PIT observada.",
    "El archivo conserva la recepcion actual: un precio historico pudo revisarse"
    " sin que este artefacto pueda demostrar su primera publicacion.",
    "El label es un retorno bruto observado, no una rentabilidad ejecutable.",
    "Un dataset reconstruido no habilita promocion, senal ni recomendacion.",
)


class AvailabilityBasis(StrEnum):
    """Explicit basis for the assumed availability of reconstructed inputs."""

    HISTORICAL_RECONSTRUCTION_ASSUMED = AVAILABILITY_BASIS


class FeatureState(StrEnum):
    """Per-column and per-sample feature availability state."""

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_YET_AVAILABLE = "not_yet_available"
    INCOMPATIBLE = "incompatible"


class LabelState(StrEnum):
    """Forward-return label availability state."""

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_YET_AVAILABLE = "not_yet_available"
    INCOMPATIBLE = "incompatible"


class SampleSplit(StrEnum):
    """Temporal split assigned before eligibility filtering."""

    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class PurgeReason(StrEnum):
    """Finite reasons that exclude a sample from an earlier split."""

    LABEL_INTERVAL_OVERLAP = "label_interval_overlap"
    EMBARGO = "embargo"


class ReadinessStatus(StrEnum):
    """Predeclared admission status for the reconstructed dataset."""

    RECONSTRUCTION_READY = "reconstruction_ready"
    INSUFFICIENT_RECONSTRUCTED_SAMPLE = "insufficient_reconstructed_sample"


class ReasonCode(StrEnum):
    """Finite evidence reason codes retained per sample and column."""

    BAR_ABSENT = "bar_absent"
    BAR_INVALID = "bar_invalid"
    WARMUP_INCOMPLETE = "warmup_incomplete"
    ASSUMED_AVAILABILITY_AFTER_DECISION = "assumed_availability_after_decision"
    LABEL_BAR_ABSENT = "label_bar_absent"
    LABEL_BAR_INVALID = "label_bar_invalid"
    LABEL_NOT_MATURED = "label_not_matured"


class EvidenceReason(ContractModel):
    """One finite reason with compact human-readable detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ReasonCode
    detail: NonEmptyStr


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def decision_instant(day: date) -> datetime:
    """Return the preregistered 12:00 UTC decision instant for one date."""
    return datetime.combine(day, time(hour=DECISION_HOUR_UTC), tzinfo=UTC)


def bar_start_instant(day: date) -> datetime:
    """Return the 00:00 UTC start of one daily bar."""
    return datetime.combine(day, time(0), tzinfo=UTC)


def assumed_available_instant(bar_end: datetime) -> datetime:
    """Return the explicit assumed availability of one completed daily bar."""
    return bar_end + ASSUMED_AVAILABILITY_DELAY


def label_exit_bar_end(day: date) -> datetime:
    """Return the (D+6) 00:00 UTC end of the exit bar for decision date D."""
    return bar_start_instant(day + timedelta(days=LABEL_EXIT_OFFSET_DAYS + 1))


class HistoricalAssetIdentity(ContractModel):
    """Resolved catalog identity and Coinbase daily source used by one archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    product_id: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    crypto_profile: NonEmptyStr
    exchange: NonEmptyStr
    granularity_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> HistoricalAssetIdentity:
        """Keep the archive inside the declared crypto spot USD daily scope."""
        if self.asset_class is not SUPPORTED_ASSET_CLASS:
            raise ValueError("the historical archive supports crypto assets only")
        if self.quote_currency != SUPPORTED_QUOTE_CURRENCY:
            raise ValueError("the historical archive supports USD quotes only")
        if self.granularity_seconds != SUPPORTED_GRANULARITY_SECONDS:
            raise ValueError("the historical archive supports daily candles only")
        if "-" not in self.product_id:
            raise ValueError("the Coinbase product_id must use BASE-QUOTE")
        return self


class HistoricalArchiveRequest(ContractModel):
    """Inclusive public acquisition range without implicit defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_request(self) -> HistoricalArchiveRequest:
        """Reject inverted, oversized or unaligned ranges."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        requested_days = (self.end_date - self.start_date).days + 1
        if requested_days > MAX_ARCHIVE_DAYS:
            raise ValueError(f"a historical archive is limited to {MAX_ARCHIVE_DAYS} days")
        return self

    def requested_days(self) -> int:
        """Return the inclusive number of requested calendar days."""
        return (self.end_date - self.start_date).days + 1

    def interval(self) -> tuple[datetime, datetime]:
        """Return the half-open UTC acquisition interval."""
        start = bar_start_instant(self.start_date)
        end = bar_start_instant(self.end_date + timedelta(days=1))
        return start, end


class HistoricalDecisionRequest(ContractModel):
    """Explicit inclusive decision range and retrieval cut without defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    start_date: date
    end_date: date
    known_at: UTCDateTime

    @model_validator(mode="after")
    def validate_request(self) -> HistoricalDecisionRequest:
        """Reject inverted, oversized or future decision requests."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        decision_count = (self.end_date - self.start_date).days + 1
        if decision_count > MAX_DECISION_DATES:
            raise ValueError(f"a reconstructed request is limited to {MAX_DECISION_DATES} dates")
        if decision_instant(self.end_date) > self.known_at:
            raise ValueError("known_at must not precede the final decision time")
        return self

    def decision_dates(self) -> tuple[date, ...]:
        """Return the inclusive ordered decision dates."""
        count = (self.end_date - self.start_date).days + 1
        return tuple(self.start_date + timedelta(days=offset) for offset in range(count))


class HistoricalCandle(ContractModel):
    """One canonical daily bar preserved from the parsed Coinbase response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bar_content_id: UUID
    bar_start: UTCDateTime
    bar_end: UTCDateTime
    open: HistoricalDecimal
    high: HistoricalDecimal
    low: HistoricalDecimal
    close: HistoricalDecimal
    volume: HistoricalDecimal
    raw_values: tuple[NonEmptyStr, ...]
    assumed_available_at: UTCDateTime
    retrieved_at: UTCDateTime

    @model_validator(mode="after")
    def validate_candle(self) -> HistoricalCandle:
        """Require one finite, internally consistent UTC daily bar."""
        if self.bar_end != self.bar_start + timedelta(days=1):
            raise ValueError("a historical daily bar must span exactly one UTC day")
        if self.bar_start.minute or self.bar_start.second or self.bar_start.microsecond:
            raise ValueError("a historical daily bar must start at 00:00 UTC")
        if self.assumed_available_at != assumed_available_instant(self.bar_end):
            raise ValueError("assumed availability must follow the close-plus-one-hour policy")
        if len(self.raw_values) != 6:
            raise ValueError("raw_values must contain exactly six provider values")
        values = (self.low, self.high, self.open, self.close, self.volume)
        if not all(value.is_finite() for value in values):
            raise ValueError("candle values must be finite Decimals")
        if self.volume < 0:
            raise ValueError("candle volume must not be negative")
        if self.low > self.high:
            raise ValueError("candle low must not exceed high")
        if not self.low <= self.open <= self.high:
            raise ValueError("candle open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("candle close must be within low and high")
        if self.retrieved_at < self.bar_start:
            raise ValueError("retrieval cannot precede the bar it contains")
        return self


class HistoricalArchiveCounts(ContractModel):
    """Auditable counts of requested days, bars and explicit absences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_days: int = Field(ge=1)
    bar_count: int = Field(ge=0)
    absent_days: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> HistoricalArchiveCounts:
        """Require every requested day to be a bar or an explicit absence."""
        if self.bar_count + self.absent_days != self.requested_days:
            raise ValueError("bars plus absences must add up to the requested days")
        return self


class HistoricalArchiveManifest(ContractModel):
    """Sealed acquisition manifest for one canonical historical archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-historical-archive-v1"] = ARCHIVE_SCHEMA_VERSION
    archive_id: UUID
    asset: HistoricalAssetIdentity
    request: HistoricalArchiveRequest
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    request_urls: tuple[NonEmptyStr, ...]
    retrieved_at: UTCDateTime
    frequency: DataFrequency = SUPPORTED_FREQUENCY
    availability_policy: Literal["historical-close-plus-1h-assumed-v1"] = AVAILABILITY_POLICY
    availability_basis: AvailabilityBasis = AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED
    strict_pit_verified: Literal[False] = False
    counts: HistoricalArchiveCounts
    duplicate_policy: Literal["conflicting-bar-timestamps-rejected-equivalent-collapsed-v1"] = (
        DUPLICATE_POLICY
    )
    semantic_sha256: str
    candles_file_name: Literal["candles.jsonl"] = CANDLES_FILE_NAME
    candles_sha256: str
    candles_bytes: int = Field(ge=0)
    provider_documentation_url: NonEmptyStr = PROVIDER_DOCUMENTATION_URL
    provider_terms_url: NonEmptyStr = PROVIDER_TERMS_URL
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> HistoricalArchiveManifest:
        """Require the exact acquisition interval and a bounded request count."""
        start, end = self.request.interval()
        if self.requested_start != start or self.requested_end != end:
            raise ValueError("requested interval must match the inclusive request")
        if self.asset.asset_id != self.request.asset_id:
            raise ValueError("archive asset must match its request")
        if self.frequency is not SUPPORTED_FREQUENCY:
            raise ValueError("historical archives store daily frequency only")
        if not self.request_urls:
            raise ValueError("a sealed archive requires at least one request URL")
        if len(self.request_urls) > MAX_ARCHIVE_REQUESTS:
            raise ValueError("archive acquisition exceeded the bounded request count")
        if len(set(self.request_urls)) != len(self.request_urls):
            raise ValueError("archive request URLs must be unique")
        if self.retrieved_at < self.requested_start:
            raise ValueError("retrieval cannot precede the requested interval")
        if self.counts.requested_days != self.request.requested_days():
            raise ValueError("archive counts must match the requested days")
        _validate_sha256(self.semantic_sha256, "semantic_sha256")
        _validate_sha256(self.candles_sha256, "candles_sha256")
        return self


class HistoricalArchive(ContractModel):
    """One sealed archive: manifest plus its ordered canonical candles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_id: UUID
    manifest: HistoricalArchiveManifest
    candles: tuple[HistoricalCandle, ...]

    @model_validator(mode="after")
    def validate_archive(self) -> HistoricalArchive:
        """Require ordered unique bars inside the requested interval."""
        if self.archive_id != self.manifest.archive_id:
            raise ValueError("archive identity must match its manifest")
        starts = tuple(candle.bar_start for candle in self.candles)
        if starts != tuple(sorted(starts)):
            raise ValueError("archive candles must be ordered by bar_start")
        if len(set(starts)) != len(starts):
            raise ValueError("archive candles must not repeat a bar_start")
        for candle in self.candles:
            if not self.manifest.requested_start <= candle.bar_start < self.manifest.requested_end:
                raise ValueError("archive candle is outside the requested interval")
            if candle.retrieved_at != self.manifest.retrieved_at:
                raise ValueError("archive candles must share the manifest retrieval instant")
        if len(self.candles) != self.manifest.counts.bar_count:
            raise ValueError("archive bar count must match its manifest")
        expected = historical_archive_semantic_hash(
            archive_id=self.archive_id,
            candles=self.candles,
        )
        if expected != self.manifest.semantic_sha256:
            raise ValueError("archive semantic hash does not match its content")
        return self


class HistoricalFeatureEvidence(ContractModel):
    """Per-column evidence for one reconstructed decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_name: NonEmptyStr
    state: FeatureState
    value: HistoricalDecimal | None = None
    unit: NonEmptyStr = FEATURE_RATIO_UNIT
    algorithm_version: NonEmptyStr
    segment_seed_start: date | None = None
    warmup_bars: int = Field(default=0, ge=0)
    assumed_available_at: UTCDateTime | None = None
    reasons: tuple[EvidenceReason, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> HistoricalFeatureEvidence:
        """Require exact values and warm-up for available columns."""
        if self.state is FeatureState.AVAILABLE:
            if self.value is None or not self.value.is_finite():
                raise ValueError("available features require a finite Decimal value")
            if self.assumed_available_at is None or self.segment_seed_start is None:
                raise ValueError("available features require availability and a segment seed")
            if self.reasons:
                raise ValueError("available features must not carry failure reasons")
            required = FEATURE_WARMUP_BARS[self.column_name]
            if self.warmup_bars < required:
                raise ValueError("available features require complete warm-up")
        else:
            if self.value is not None:
                raise ValueError("unavailable features must not expose a numeric value")
            if not self.reasons:
                raise ValueError("unavailable features require at least one reason")
        return self


class HistoricalLabelBarEvidence(ContractModel):
    """One selected daily close used to build the forward-return label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bar_content_id: UUID | None = None
    bar_start: UTCDateTime
    bar_end: UTCDateTime
    state: FeatureState
    value: HistoricalDecimal | None = None
    assumed_available_at: UTCDateTime | None = None
    reasons: tuple[EvidenceReason, ...] = ()

    @model_validator(mode="after")
    def validate_bar(self) -> HistoricalLabelBarEvidence:
        """Keep a label bar available only with exact positive close evidence."""
        if self.bar_end != self.bar_start + timedelta(days=1):
            raise ValueError("a daily label bar must span exactly one UTC day")
        if self.state is FeatureState.AVAILABLE:
            if self.bar_content_id is None or self.assumed_available_at is None:
                raise ValueError("available label bars require identity and availability")
            if self.value is None or not self.value.is_finite() or self.value <= 0:
                raise ValueError("available label bars require positive finite closes")
            if self.reasons:
                raise ValueError("available label bars must not carry failure reasons")
        elif self.value is not None:
            raise ValueError("unavailable label bars must not expose a close value")
        return self


class HistoricalLabelEvidence(ContractModel):
    """Forward five-interval label with six continuous bar evidence rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: LabelState
    decision_at: UTCDateTime
    entry_bar_start: UTCDateTime
    exit_bar_end: UTCDateTime
    entry_close: HistoricalDecimal | None = None
    exit_close: HistoricalDecimal | None = None
    return_5d: HistoricalDecimal | None = None
    label: Literal[0, 1] | None = None
    evidence_available_at: UTCDateTime | None = None
    archive_retrieved_at: UTCDateTime
    bars: tuple[HistoricalLabelBarEvidence, ...]
    reasons: tuple[EvidenceReason, ...] = ()

    @model_validator(mode="after")
    def validate_label(self) -> HistoricalLabelEvidence:
        """Verify the continuous calendar, exact return and binary label rule."""
        if len(self.bars) != LABEL_BAR_COUNT:
            raise ValueError("a historical label requires exactly six bar evidence rows")
        expected_entry = bar_start_instant(self.decision_at.date())
        if self.entry_bar_start != expected_entry:
            raise ValueError("the entry bar must start at the decision date 00:00 UTC")
        if self.exit_bar_end != label_exit_bar_end(self.decision_at.date()):
            raise ValueError("the exit bar must end at (D+6) 00:00 UTC")
        for offset, bar in enumerate(self.bars):
            expected = bar_start_instant(self.decision_at.date() + timedelta(days=offset))
            if bar.bar_start != expected:
                raise ValueError("label bars must follow the continuous D..D+5 calendar")
        if self.state is LabelState.AVAILABLE:
            if (
                self.entry_close is None
                or self.exit_close is None
                or self.return_5d is None
                or self.label is None
                or self.evidence_available_at is None
            ):
                raise ValueError("available labels require closes, return and binary target")
            if any(bar.state is not FeatureState.AVAILABLE for bar in self.bars):
                raise ValueError("available labels require six available bars")
            expected_return = _decimal34_ratio(self.exit_close, self.entry_close)
            if expected_return != self.return_5d:
                raise ValueError("return_5d must equal exit_close / entry_close - 1")
            if self.label != (1 if self.return_5d > 0 else 0):
                raise ValueError("label must be one only for a strictly positive return")
            expected_available = max(self.exit_bar_end, self.archive_retrieved_at)
            if self.evidence_available_at != expected_available:
                raise ValueError("label evidence must include exit end and real reception")
            if self.reasons:
                raise ValueError("available labels must not carry failure reasons")
        else:
            if any(
                value is not None
                for value in (
                    self.entry_close,
                    self.exit_close,
                    self.return_5d,
                    self.label,
                    self.evidence_available_at,
                )
            ):
                raise ValueError("unavailable labels must not expose target values")
            if not self.reasons:
                raise ValueError("unavailable labels require at least one reason")
        return self


def _decimal34_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(DECIMAL34):
        return numerator / denominator - 1


def aggregate_feature_state(features: tuple[HistoricalFeatureEvidence, ...]) -> FeatureState:
    """Collapse column states with contradiction-first precedence."""
    states = {item.state for item in features}
    for state in (
        FeatureState.INCOMPATIBLE,
        FeatureState.NOT_YET_AVAILABLE,
        FeatureState.MISSING,
    ):
        if state in states:
            return state
    return FeatureState.AVAILABLE


class HistoricalSample(ContractModel):
    """One auditable reconstructed decision with separated features and label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_key: UUID
    feature_hash: UUID
    row_id: UUID
    decision_at: UTCDateTime
    features: tuple[HistoricalFeatureEvidence, ...]
    label: HistoricalLabelEvidence
    feature_state: FeatureState
    label_state: LabelState
    split: SampleSplit
    purged: bool = False
    purge_reason: PurgeReason | None = None
    eligible: bool

    @model_validator(mode="after")
    def validate_sample(self) -> HistoricalSample:
        """Keep ordered features, temporal identity and eligibility consistent."""
        if tuple(item.column_name for item in self.features) != FEATURE_COLUMNS:
            raise ValueError("sample features must preserve the declared column order")
        if self.decision_at != decision_instant(self.decision_at.date()):
            raise ValueError("sample decision_at must be the preregistered 12:00 UTC instant")
        if self.label.decision_at != self.decision_at:
            raise ValueError("label decision_at must match its sample")
        if self.feature_state is not aggregate_feature_state(self.features):
            raise ValueError("sample feature_state must aggregate its column states")
        if self.label_state is not self.label.state:
            raise ValueError("sample label_state must match its label evidence")
        if self.purged and self.purge_reason is None:
            raise ValueError("purged samples require a purge reason")
        if not self.purged and self.purge_reason is not None:
            raise ValueError("non-purged samples must not declare a purge reason")
        expected_eligible = (
            self.feature_state is FeatureState.AVAILABLE
            and self.label_state is LabelState.AVAILABLE
            and not self.purged
        )
        if self.eligible is not expected_eligible:
            raise ValueError("eligibility requires available features, label and no purge")
        return self


class HistoricalSplitPolicy(ContractModel):
    """Versioned temporal split, purge and admission policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["historical-temporal-split-v1"] = SPLIT_POLICY_VERSION
    train_fraction: Decimal = TRAIN_FRACTION
    validation_fraction: Decimal = VALIDATION_FRACTION
    holdout_fraction: Decimal = HOLDOUT_FRACTION
    embargo_days: int = Field(default=EMBARGO_DAYS, ge=0)
    min_eligible_before_split: int = Field(default=READINESS_MIN_ELIGIBLE, ge=0)
    min_train: int = Field(default=READINESS_MIN_TRAIN, ge=0)
    min_validation: int = Field(default=READINESS_MIN_VALIDATION, ge=0)
    min_holdout: int = Field(default=READINESS_MIN_HOLDOUT, ge=0)
    min_train_class: int = Field(default=READINESS_MIN_TRAIN_CLASS, ge=0)

    @model_validator(mode="after")
    def validate_policy(self) -> HistoricalSplitPolicy:
        """Require the exact preregistered fractions and floors."""
        if self.policy_version != SPLIT_POLICY_VERSION:
            raise ValueError("unsupported historical split policy version")
        if (self.train_fraction, self.validation_fraction, self.holdout_fraction) != (
            TRAIN_FRACTION,
            VALIDATION_FRACTION,
            HOLDOUT_FRACTION,
        ):
            raise ValueError("split fractions are preregistered as 60/20/20")
        if self.embargo_days != EMBARGO_DAYS:
            raise ValueError("embargo is preregistered at five UTC days")
        if (
            self.min_eligible_before_split,
            self.min_train,
            self.min_validation,
            self.min_holdout,
            self.min_train_class,
        ) != (
            READINESS_MIN_ELIGIBLE,
            READINESS_MIN_TRAIN,
            READINESS_MIN_VALIDATION,
            READINESS_MIN_HOLDOUT,
            READINESS_MIN_TRAIN_CLASS,
        ):
            raise ValueError("readiness floors are preregistered")
        return self


PREREGISTERED_SPLIT_POLICY = HistoricalSplitPolicy()


def split_boundaries(decision_count: int) -> tuple[int, int]:
    """Return train and validation ends using floors over requested dates."""
    if decision_count < 0:
        raise ValueError("decision_count must not be negative")
    train_end = int(
        (Decimal(decision_count) * TRAIN_FRACTION).to_integral_value(rounding=ROUND_FLOOR)
    )
    validation_end = int(
        (Decimal(decision_count) * (TRAIN_FRACTION + VALIDATION_FRACTION)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return train_end, validation_end


class HistoricalDatasetCounts(ContractModel):
    """Auditable counts for every requested decision and evidence state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: int = Field(ge=0)
    feature_available: int = Field(ge=0)
    feature_missing: int = Field(ge=0)
    feature_not_yet_available: int = Field(ge=0)
    feature_incompatible: int = Field(ge=0)
    label_available: int = Field(ge=0)
    label_missing: int = Field(ge=0)
    label_not_yet_available: int = Field(ge=0)
    label_incompatible: int = Field(ge=0)
    eligible_before_split: int = Field(ge=0)
    purged: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> HistoricalDatasetCounts:
        """Require every state partition to add up to the requested total."""
        feature_total = (
            self.feature_available
            + self.feature_missing
            + self.feature_not_yet_available
            + self.feature_incompatible
        )
        label_total = (
            self.label_available
            + self.label_missing
            + self.label_not_yet_available
            + self.label_incompatible
        )
        if feature_total != self.requested:
            raise ValueError("feature state counts must add up to requested")
        if label_total != self.requested:
            raise ValueError("label state counts must add up to requested")
        if self.eligible_before_split > self.requested:
            raise ValueError("eligible_before_split must not exceed requested")
        if self.purged > self.requested:
            raise ValueError("purged must not exceed requested")
        return self


class HistoricalSplitSummary(ContractModel):
    """Proposed, purged and eligible counts for one temporal split."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    split: SampleSplit
    proposed: int = Field(ge=0)
    purged: int = Field(ge=0)
    remaining: int = Field(ge=0)
    eligible_after_purge: int = Field(ge=0)
    positive: int | None = Field(default=None, ge=0)
    negative: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_split(self) -> HistoricalSplitSummary:
        """Keep purge arithmetic and train-only class reporting explicit."""
        if self.proposed != self.purged + self.remaining:
            raise ValueError("proposed must equal purged plus remaining")
        if self.eligible_after_purge > self.remaining:
            raise ValueError("eligible_after_purge must not exceed remaining")
        if self.split is SampleSplit.TRAIN:
            if self.positive is None or self.negative is None:
                raise ValueError("train requires both class counts")
        elif self.positive is not None or self.negative is not None:
            raise ValueError("class counts are only exposed for the train split")
        return self


class HistoricalReadiness(ContractModel):
    """Predeclared admission decision without statistical promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReadinessStatus
    strict_pit_verified: Literal[False] = False
    eligible_before_split: int = Field(ge=0)
    train_eligible: int = Field(ge=0)
    validation_eligible: int = Field(ge=0)
    holdout_eligible: int = Field(ge=0)
    train_positive: int = Field(ge=0)
    train_negative: int = Field(ge=0)
    unmet_requirements: tuple[NonEmptyStr, ...] = ()

    @staticmethod
    def unmet_requirements_for(
        *,
        eligible_before_split: int,
        train_eligible: int,
        validation_eligible: int,
        holdout_eligible: int,
        train_positive: int,
        train_negative: int,
    ) -> tuple[str, ...]:
        """Return the preregistered floors that the observed counts do not meet."""
        unmet: list[str] = []
        if eligible_before_split < READINESS_MIN_ELIGIBLE:
            unmet.append(
                f"eligible_before_split {eligible_before_split} < {READINESS_MIN_ELIGIBLE}"
            )
        if train_eligible < READINESS_MIN_TRAIN:
            unmet.append(f"train_eligible {train_eligible} < {READINESS_MIN_TRAIN}")
        if validation_eligible < READINESS_MIN_VALIDATION:
            unmet.append(f"validation_eligible {validation_eligible} < {READINESS_MIN_VALIDATION}")
        if holdout_eligible < READINESS_MIN_HOLDOUT:
            unmet.append(f"holdout_eligible {holdout_eligible} < {READINESS_MIN_HOLDOUT}")
        if train_positive < READINESS_MIN_TRAIN_CLASS:
            unmet.append(f"train_positive {train_positive} < {READINESS_MIN_TRAIN_CLASS}")
        if train_negative < READINESS_MIN_TRAIN_CLASS:
            unmet.append(f"train_negative {train_negative} < {READINESS_MIN_TRAIN_CLASS}")
        return tuple(unmet)

    @classmethod
    def from_counts(
        cls,
        *,
        eligible_before_split: int,
        train_eligible: int,
        validation_eligible: int,
        holdout_eligible: int,
        train_positive: int,
        train_negative: int,
    ) -> HistoricalReadiness:
        """Build the deterministic readiness artifact from observed counts."""
        unmet = cls.unmet_requirements_for(
            eligible_before_split=eligible_before_split,
            train_eligible=train_eligible,
            validation_eligible=validation_eligible,
            holdout_eligible=holdout_eligible,
            train_positive=train_positive,
            train_negative=train_negative,
        )
        status = (
            ReadinessStatus.RECONSTRUCTION_READY
            if not unmet
            else ReadinessStatus.INSUFFICIENT_RECONSTRUCTED_SAMPLE
        )
        return cls(
            status=status,
            eligible_before_split=eligible_before_split,
            train_eligible=train_eligible,
            validation_eligible=validation_eligible,
            holdout_eligible=holdout_eligible,
            train_positive=train_positive,
            train_negative=train_negative,
            unmet_requirements=unmet,
        )

    @model_validator(mode="after")
    def validate_readiness(self) -> HistoricalReadiness:
        """Derive the exact preregistered admission status."""
        unmet = self.unmet_requirements_for(
            eligible_before_split=self.eligible_before_split,
            train_eligible=self.train_eligible,
            validation_eligible=self.validation_eligible,
            holdout_eligible=self.holdout_eligible,
            train_positive=self.train_positive,
            train_negative=self.train_negative,
        )
        expected = (
            ReadinessStatus.RECONSTRUCTION_READY
            if not unmet
            else ReadinessStatus.INSUFFICIENT_RECONSTRUCTED_SAMPLE
        )
        if self.status is not expected:
            raise ValueError("readiness status must follow the preregistered floors")
        if unmet != self.unmet_requirements:
            raise ValueError("unmet requirements must match the observed counts")
        return self


class HistoricalColumnSpec(ContractModel):
    """One declared Parquet column with exact encoding and nullability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    data_type: Literal["text", "integer", "boolean"]
    nullable: bool
    unit: NonEmptyStr | None = None
    description: NonEmptyStr


HISTORICAL_ROWS_SCHEMA: tuple[HistoricalColumnSpec, ...] = (
    HistoricalColumnSpec(
        name="sample_key",
        data_type="text",
        nullable=False,
        description="Deterministic sample identity.",
    ),
    HistoricalColumnSpec(
        name="row_id",
        data_type="text",
        nullable=False,
        description="Deterministic row identity including feature and label evidence.",
    ),
    HistoricalColumnSpec(
        name="decision_at",
        data_type="text",
        nullable=False,
        unit="iso8601-utc",
        description="Preregistered 12:00 UTC decision instant.",
    ),
    HistoricalColumnSpec(
        name=CLOSE_RETURN_COLUMN,
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="close[D-1] / close[D-2] - 1 as canonical decimal text.",
    ),
    HistoricalColumnSpec(
        name=RSI_COLUMN,
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="Reconstructed RSI[D-1] / 100 as canonical decimal text.",
    ),
    HistoricalColumnSpec(
        name=MACD_HISTOGRAM_COLUMN,
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="Reconstructed histogram[D-1] / close[D-1] as canonical decimal text.",
    ),
    HistoricalColumnSpec(
        name="feature_state",
        data_type="text",
        nullable=False,
        description="Aggregated reconstructed feature availability state.",
    ),
    HistoricalColumnSpec(
        name="label_state",
        data_type="text",
        nullable=False,
        description="Forward-return label availability state.",
    ),
    HistoricalColumnSpec(
        name="label",
        data_type="integer",
        nullable=True,
        description="One when return_5d is strictly positive, zero otherwise.",
    ),
    HistoricalColumnSpec(
        name="return_5d",
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="exit_close / entry_close - 1 as canonical decimal text.",
    ),
    HistoricalColumnSpec(
        name="entry_close",
        data_type="text",
        nullable=True,
        unit=CLOSE_UNIT,
        description="Close of the D bar as canonical decimal text.",
    ),
    HistoricalColumnSpec(
        name="exit_close",
        data_type="text",
        nullable=True,
        unit=CLOSE_UNIT,
        description="Close of the D+5 bar as canonical decimal text.",
    ),
    HistoricalColumnSpec(
        name="label_evidence_available_at",
        data_type="text",
        nullable=True,
        unit="iso8601-utc",
        description="Later of the exit bar end and the real archive reception.",
    ),
    HistoricalColumnSpec(
        name="feature_evidence_json",
        data_type="text",
        nullable=False,
        description="Canonical JSON with prefix digest and per-column evidence.",
    ),
    HistoricalColumnSpec(
        name="label_evidence_json",
        data_type="text",
        nullable=False,
        description="Canonical JSON with the six label bar evidence rows.",
    ),
    HistoricalColumnSpec(
        name="split",
        data_type="text",
        nullable=False,
        description="Temporal split assigned before eligibility filtering.",
    ),
    HistoricalColumnSpec(
        name="purged",
        data_type="boolean",
        nullable=False,
        description="Whether the label interval overlaps or enters the embargo.",
    ),
    HistoricalColumnSpec(
        name="purge_reason",
        data_type="text",
        nullable=True,
        description="Finite purge reason when purged is true.",
    ),
    HistoricalColumnSpec(
        name="eligible",
        data_type="boolean",
        nullable=False,
        description="Mature features and label with no split purge.",
    ),
    HistoricalColumnSpec(
        name="archive_id",
        data_type="text",
        nullable=False,
        description="Content-addressed reference to the single conserved archive.",
    ),
    HistoricalColumnSpec(
        name="archive_semantic_sha256",
        data_type="text",
        nullable=False,
        description="Semantic digest of the conserved archive.",
    ),
    HistoricalColumnSpec(
        name="segment_seed_start",
        data_type="text",
        nullable=True,
        unit="iso8601-date",
        description="First contiguous bar of the reconstructed segment.",
    ),
    HistoricalColumnSpec(
        name="warmup_bars",
        data_type="integer",
        nullable=False,
        description="Bars observed in the segment up to the causal feature bar.",
    ),
)


class HistoricalArtifactSchema(ContractModel):
    """Published schema, order and encoding contract for the exported rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    encoding_version: Literal["research-historical-dataset-encoding-v1"] = (
        "research-historical-dataset-encoding-v1"
    )
    feature_encoding: Literal["canonical-decimal-text"] = "canonical-decimal-text"
    timestamp_encoding: Literal["iso8601-utc"] = "iso8601-utc"
    id_encoding: Literal["uuid-text"] = "uuid-text"
    order_by: Literal["decision_at,sample_key"] = "decision_at,sample_key"
    feature_columns: tuple[NonEmptyStr, ...] = FEATURE_COLUMNS
    columns: tuple[HistoricalColumnSpec, ...] = HISTORICAL_ROWS_SCHEMA

    @model_validator(mode="after")
    def validate_schema(self) -> HistoricalArtifactSchema:
        """Keep the published column order and feature selection exactly as declared."""
        if tuple(item.name for item in self.columns) != tuple(
            item.name for item in HISTORICAL_ROWS_SCHEMA
        ):
            raise ValueError("artifact schema must preserve the declared column order")
        if self.feature_columns != FEATURE_COLUMNS:
            raise ValueError("artifact schema must declare the three feature columns")
        return self


class HistoricalArchiveReference(ContractModel):
    """Content-addressed reference to the single conserved archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    archive_id: UUID
    semantic_sha256: str
    candles_sha256: str
    retrieved_at: UTCDateTime
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    bar_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_reference(self) -> HistoricalArchiveReference:
        """Require complete digests and a coherent requested interval."""
        _validate_sha256(self.semantic_sha256, "semantic_sha256")
        _validate_sha256(self.candles_sha256, "candles_sha256")
        if self.requested_start >= self.requested_end:
            raise ValueError("archive reference requires a non-empty interval")
        return self


class HistoricalDatasetManifest(ContractModel):
    """Deterministic dataset manifest linking every row to its reconstruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-historical-dataset-v1"] = DATASET_SCHEMA_VERSION
    dataset_id: UUID
    dataset_content_sha256: str
    experiment_spec_id: NonEmptyStr = EXPERIMENT_SPEC_ID
    target_version: NonEmptyStr = TARGET_VERSION
    feature_set_version: NonEmptyStr = FEATURE_SET_VERSION
    availability_policy: Literal["historical-close-plus-1h-assumed-v1"] = AVAILABILITY_POLICY
    availability_basis: AvailabilityBasis = AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED
    strict_pit_verified: Literal[False] = False
    request: HistoricalDecisionRequest
    asset: HistoricalAssetIdentity
    archive: HistoricalArchiveReference
    frequency: DataFrequency = SUPPORTED_FREQUENCY
    feature_read_start: UTCDateTime
    feature_read_end: UTCDateTime
    label_read_start: UTCDateTime
    label_read_end: UTCDateTime
    artifact_schema: HistoricalArtifactSchema = HistoricalArtifactSchema()
    counts: HistoricalDatasetCounts
    split_policy: HistoricalSplitPolicy = PREREGISTERED_SPLIT_POLICY
    splits: tuple[HistoricalSplitSummary, ...]
    readiness: HistoricalReadiness
    rows: tuple[HistoricalSample, ...]
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> HistoricalDatasetManifest:
        """Require ordered unique rows, exact versions and the three splits."""
        _validate_sha256(self.dataset_content_sha256, "dataset_content_sha256")
        if (self.experiment_spec_id, self.target_version, self.feature_set_version) != (
            EXPERIMENT_SPEC_ID,
            TARGET_VERSION,
            FEATURE_SET_VERSION,
        ):
            raise ValueError("dataset versions must preserve the preregistered identity")
        if self.asset.asset_id != self.request.asset_id:
            raise ValueError("dataset asset must match its request")
        if tuple(item.split for item in self.splits) != (
            SampleSplit.TRAIN,
            SampleSplit.VALIDATION,
            SampleSplit.HOLDOUT,
        ):
            raise ValueError("manifest must publish train, validation and holdout in order")
        if len(self.rows) != self.counts.requested:
            raise ValueError("manifest rows must match the requested count")
        row_ids = tuple(item.row_id for item in self.rows)
        if len(set(row_ids)) != len(row_ids):
            raise ValueError("manifest row identities must be unique")
        if tuple(item.decision_at for item in self.rows) != tuple(
            sorted(item.decision_at for item in self.rows)
        ):
            raise ValueError("manifest rows must be ordered by decision_at")
        if not self.feature_read_start < self.feature_read_end:
            raise ValueError("feature read window must be non-empty")
        if not self.label_read_start < self.label_read_end:
            raise ValueError("label read window must be non-empty")
        return self


class HistoricalDatasetSummary(ContractModel):
    """CLI-safe aggregate summary without per-sample target values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    dataset_content_sha256: str
    experiment_spec_id: NonEmptyStr
    target_version: NonEmptyStr
    feature_set_version: NonEmptyStr
    availability_policy: Literal["historical-close-plus-1h-assumed-v1"]
    availability_basis: AvailabilityBasis
    strict_pit_verified: Literal[False]
    asset: HistoricalAssetIdentity
    archive: HistoricalArchiveReference
    request: HistoricalDecisionRequest
    counts: HistoricalDatasetCounts
    split_policy: HistoricalSplitPolicy
    splits: tuple[HistoricalSplitSummary, ...]
    readiness: HistoricalReadiness
    limitations: tuple[NonEmptyStr, ...]

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


class HistoricalDataset(ContractModel):
    """Complete in-memory dataset plus its manifest and aggregate summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    dataset_content_sha256: str
    manifest: HistoricalDatasetManifest
    summary: HistoricalDatasetSummary
    samples: tuple[HistoricalSample, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> HistoricalDataset:
        """Keep the manifest, samples and summary on one exact dataset."""
        if self.samples != self.manifest.rows:
            raise ValueError("dataset samples must match the manifest rows")
        if self.dataset_id != self.manifest.dataset_id:
            raise ValueError("dataset identity must match its manifest")
        if self.dataset_content_sha256 != self.manifest.dataset_content_sha256:
            raise ValueError("dataset content hash must match its manifest")
        if self.summary.dataset_id != self.dataset_id:
            raise ValueError("summary identity must match the dataset")
        if self.summary.counts != self.manifest.counts:
            raise ValueError("summary counts must match the manifest")
        if self.summary.readiness != self.manifest.readiness:
            raise ValueError("summary readiness must match the manifest")
        return self


def _feature_payload(feature: HistoricalFeatureEvidence) -> dict[str, object]:
    return {
        "column_name": feature.column_name,
        "state": feature.state,
        "value": feature.value,
        "unit": feature.unit,
        "algorithm_version": feature.algorithm_version,
        "segment_seed_start": (
            None if feature.segment_seed_start is None else feature.segment_seed_start.isoformat()
        ),
        "warmup_bars": feature.warmup_bars,
        "assumed_available_at": feature.assumed_available_at,
        "reasons": feature.reasons,
    }


def _label_payload(label: HistoricalLabelEvidence) -> dict[str, object]:
    return {
        "state": label.state,
        "decision_at": label.decision_at,
        "entry_bar_start": label.entry_bar_start,
        "exit_bar_end": label.exit_bar_end,
        "entry_close": label.entry_close,
        "exit_close": label.exit_close,
        "return_5d": label.return_5d,
        "label": label.label,
        "evidence_available_at": label.evidence_available_at,
        "archive_retrieved_at": label.archive_retrieved_at,
        "bars": tuple(
            {
                "bar_content_id": bar.bar_content_id,
                "bar_start": bar.bar_start,
                "bar_end": bar.bar_end,
                "state": bar.state,
                "value": bar.value,
                "assumed_available_at": bar.assumed_available_at,
                "reasons": bar.reasons,
            }
            for bar in label.bars
        ),
        "reasons": label.reasons,
    }


def _sample_payload(sample: HistoricalSample) -> dict[str, object]:
    return {
        "sample_key": sample.sample_key,
        "feature_hash": sample.feature_hash,
        "row_id": sample.row_id,
        "decision_at": sample.decision_at,
        "features": tuple(_feature_payload(item) for item in sample.features),
        "label": _label_payload(sample.label),
        "feature_state": sample.feature_state,
        "label_state": sample.label_state,
        "split": sample.split,
        "purged": sample.purged,
        "purge_reason": sample.purge_reason,
        "eligible": sample.eligible,
    }


def historical_bar_content_id(
    *,
    asset_id: str,
    source_id: str,
    bar_start: datetime,
    open_value: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    raw_values: tuple[str, ...],
) -> UUID:
    """Derive the content identity of one daily bar without any compute clock."""
    payload = {
        "artifact": "bar_content_id",
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "asset_id": asset_id,
        "source_id": source_id,
        "frequency": SUPPORTED_FREQUENCY,
        "bar_start": bar_start,
        "open": open_value,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "raw_values": raw_values,
    }
    return uuid5(HISTORICAL_NAMESPACE, analytical_content_hash(payload))


def historical_prefix_digest(
    previous: str | None,
    *,
    bar_content_id: UUID,
    close: Decimal,
) -> str:
    """Extend the causal prefix digest with one bar identity and close."""
    payload = {
        "artifact": "historical_prefix",
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "previous": previous,
        "bar_content_id": bar_content_id,
        "close": close,
    }
    return analytical_content_hash(payload)


def historical_archive_semantic_hash(
    *,
    archive_id: UUID,
    candles: tuple[HistoricalCandle, ...],
) -> str:
    """Return the semantic digest of the archive identity and ordered bar content."""
    payload = {
        "artifact": "historical_archive_content",
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "archive_id": archive_id,
        "candles": tuple(
            {
                "bar_content_id": candle.bar_content_id,
                "bar_start": candle.bar_start,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "raw_values": candle.raw_values,
            }
            for candle in candles
        ),
    }
    return analytical_content_hash(payload)


def historical_archive_id(
    *,
    asset: HistoricalAssetIdentity,
    request: HistoricalArchiveRequest,
    request_urls: tuple[str, ...],
    retrieved_at: datetime,
    candles: tuple[HistoricalCandle, ...],
) -> UUID:
    """Derive the archive identity from acquisition parameters and ordered content."""
    payload = {
        "artifact": "archive_id",
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "asset": asset,
        "request": {
            "asset_id": request.asset_id,
            "start_date": request.start_date.isoformat(),
            "end_date": request.end_date.isoformat(),
        },
        "request_urls": request_urls,
        "retrieved_at": retrieved_at,
        "availability_policy": AVAILABILITY_POLICY,
        "candles": tuple(str(candle.bar_content_id) for candle in candles),
    }
    return uuid5(HISTORICAL_NAMESPACE, analytical_content_hash(payload))


def historical_sample_key(
    *,
    asset_id: str,
    source_id: str,
    decision_at: datetime,
) -> UUID:
    """Derive the sample identity from the experiment, scope and decision."""
    payload = {
        "artifact": "sample_key",
        "experiment_spec_id": EXPERIMENT_SPEC_ID,
        "asset_id": asset_id,
        "source_id": source_id,
        "decision_at": decision_at,
    }
    return uuid5(HISTORICAL_NAMESPACE, analytical_content_hash(payload))


def historical_feature_hash(
    *,
    sample_key: UUID,
    decision_at: datetime,
    segment_seed_start: date | None,
    prefix_digest: str,
    features: tuple[HistoricalFeatureEvidence, ...],
) -> UUID:
    """Derive the feature identity without archive, label or future evidence."""
    payload = {
        "artifact": "feature_hash",
        "feature_set_version": FEATURE_SET_VERSION,
        "availability_policy": AVAILABILITY_POLICY,
        "sample_key": sample_key,
        "decision_at": decision_at,
        "segment_seed_start": (
            None if segment_seed_start is None else segment_seed_start.isoformat()
        ),
        "prefix_digest": prefix_digest,
        "features": tuple(
            {
                "column_name": item.column_name,
                "state": item.state,
                "value": item.value,
                "unit": item.unit,
                "algorithm_version": item.algorithm_version,
                "reasons": item.reasons,
            }
            for item in features
        ),
    }
    return uuid5(HISTORICAL_NAMESPACE, analytical_content_hash(payload))


def historical_row_id(
    *,
    sample_key: UUID,
    feature_hash: UUID,
    features: tuple[HistoricalFeatureEvidence, ...],
    label: HistoricalLabelEvidence,
) -> UUID:
    """Derive the row identity from the full feature and label evidence."""
    payload = {
        "artifact": "row_id",
        "sample_key": sample_key,
        "feature_hash": feature_hash,
        "features": tuple(_feature_payload(item) for item in features),
        "label": _label_payload(label),
    }
    return uuid5(HISTORICAL_NAMESPACE, analytical_content_hash(payload))


def _request_payload(request: HistoricalDecisionRequest) -> dict[str, object]:
    return {
        "asset_id": request.asset_id,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "known_at": request.known_at,
    }


def historical_dataset_id(
    *,
    asset_id: str,
    source_id: str,
    archive_id: UUID,
    archive_semantic_sha256: str,
    request: HistoricalDecisionRequest,
    split_policy: HistoricalSplitPolicy,
    rows: tuple[HistoricalSample, ...],
) -> UUID:
    """Derive the dataset identity from the request, policy, archive and ordered rows."""
    payload = {
        "artifact": "dataset_id",
        "experiment_spec_id": EXPERIMENT_SPEC_ID,
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "asset_id": asset_id,
        "source_id": source_id,
        "archive_id": archive_id,
        "archive_semantic_sha256": archive_semantic_sha256,
        "request": _request_payload(request),
        "split_policy": split_policy,
        "rows": tuple(str(item.row_id) for item in rows),
    }
    return uuid5(HISTORICAL_NAMESPACE, analytical_content_hash(payload))


def historical_dataset_content_hash(
    *,
    dataset_id: UUID,
    rows: tuple[HistoricalSample, ...],
) -> str:
    """Return the semantic dataset digest independent from physical Parquet bytes."""
    payload = {
        "artifact": "dataset_content",
        "dataset_id": dataset_id,
        "rows": tuple(_sample_payload(item) for item in rows),
    }
    return analytical_content_hash(payload)


__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "AVAILABILITY_BASIS",
    "AVAILABILITY_POLICY",
    "CANDLES_FILE_NAME",
    "CLOSE_RETURN_COLUMN",
    "CLOSE_RETURN_ALGORITHM_VERSION",
    "DATASET_SCHEMA_VERSION",
    "DECISION_HOUR_UTC",
    "DUPLICATE_POLICY",
    "EMBARGO_DAYS",
    "EXPERIMENT_SPEC_ID",
    "FEATURE_COLUMNS",
    "FEATURE_RATIO_UNIT",
    "FEATURE_SET_VERSION",
    "FEATURE_WARMUP_BARS",
    "HISTORICAL_NAMESPACE",
    "HISTORICAL_ROWS_SCHEMA",
    "LABEL_BAR_COUNT",
    "LABEL_EXIT_OFFSET_DAYS",
    "MACD_HISTOGRAM_COLUMN",
    "MACD_WARMUP_BARS",
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_ARCHIVE_DAYS",
    "MAX_ARCHIVE_REQUESTS",
    "MAX_CANDLES_PER_REQUEST",
    "MAX_DECISION_DATES",
    "PROVIDER_DOCUMENTATION_URL",
    "PROVIDER_TERMS_URL",
    "PREREGISTERED_SPLIT_POLICY",
    "ROWS_FILE_NAME",
    "RSI_COLUMN",
    "RSI_WARMUP_BARS",
    "SUPPORTED_ASSET_CLASS",
    "SUPPORTED_FREQUENCY",
    "SUPPORTED_GRANULARITY_SECONDS",
    "SUPPORTED_QUOTE_CURRENCY",
    "TARGET_VERSION",
    "AvailabilityBasis",
    "EvidenceReason",
    "FeatureState",
    "HistoricalArchive",
    "HistoricalArchiveCounts",
    "HistoricalArchiveManifest",
    "HistoricalArchiveReference",
    "HistoricalArchiveRequest",
    "HistoricalArtifactSchema",
    "HistoricalAssetIdentity",
    "HistoricalCandle",
    "HistoricalColumnSpec",
    "HistoricalDataset",
    "HistoricalDatasetCounts",
    "HistoricalDatasetManifest",
    "HistoricalDatasetSummary",
    "HistoricalDecisionRequest",
    "HistoricalFeatureEvidence",
    "HistoricalLabelBarEvidence",
    "HistoricalLabelEvidence",
    "HistoricalReadiness",
    "EvidenceReason",
    "HistoricalSample",
    "HistoricalSplitPolicy",
    "HistoricalSplitSummary",
    "LabelState",
    "PurgeReason",
    "ReadinessStatus",
    "ReasonCode",
    "SampleSplit",
    "aggregate_feature_state",
    "assumed_available_instant",
    "bar_start_instant",
    "decision_instant",
    "historical_archive_id",
    "historical_archive_semantic_hash",
    "historical_bar_content_id",
    "historical_dataset_content_hash",
    "historical_dataset_id",
    "historical_feature_hash",
    "historical_prefix_digest",
    "historical_row_id",
    "historical_sample_key",
    "label_exit_bar_end",
    "split_boundaries",
]
