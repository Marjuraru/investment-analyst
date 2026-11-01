"""Strict contracts and deterministic identities for the predictive research dataset."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BeforeValidator, ConfigDict, Field, JsonValue, model_validator

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import AssetClass, DataFrequency, DataQuality


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("predictive financial values must use Decimal")
    return value


PredictiveDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]

EXPERIMENT_SPEC_ID = "crypto-daily-positive-return-5d-v1"
TARGET_VERSION = "crypto-forward-close-return-5d-v1"
FEATURE_SET_VERSION = "crypto-market-baseline-3-v1"
EXPERIMENT_SCHEMA_VERSION = "predictive-experiment-v1"
DATASET_SCHEMA_VERSION = "predictive-dataset-v1"
MANIFEST_SCHEMA_VERSION = "predictive-dataset-manifest-v1"

EXPERIMENT_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/predictive-foundation-v1")

DECISION_HOUR_UTC = 12
MAX_DECISION_DATES = 366
MAX_PAGE_SIZE = 256
CLOSE_FIELD_NAME = "close"
SUPPORTED_FREQUENCY = DataFrequency.DAY_1
SUPPORTED_ASSET_CLASS = AssetClass.CRYPTO
SUPPORTED_QUOTE_CURRENCY = "USD"

CLOSE_RETURN_COLUMN = "close_return_1d"
RSI_COLUMN = "rsi_14_fraction"
MACD_HISTOGRAM_COLUMN = "macd_12_26_9_histogram_close_ratio"
FEATURE_COLUMNS = (CLOSE_RETURN_COLUMN, RSI_COLUMN, MACD_HISTOGRAM_COLUMN)

CLOSE_RETURN_ALGORITHM_VERSION = "predictive-close-ratio-1d-v1"
FEATURE_RATIO_UNIT = "ratio"

RSI_METRIC_KEY = "market.technical.rsi"
RSI_ALGORITHM_VERSION = "market-rsi-incremental-v2-decimal34"
RSI_UNIT = "index"
RSI_PARAMETER_FILTERS = {"window": 14}

MACD_HISTOGRAM_METRIC_KEY = "market.technical.macd.histogram"
MACD_ALGORITHM_VERSION = "market-macd-incremental-v2-decimal34"
MACD_HISTOGRAM_UNIT = "USD"
MACD_PARAMETER_FILTERS = {"fast_window": 12, "slow_window": 26, "signal_window": 9}

FEATURE_DAY_OFFSETS = (-1, -2)
LABEL_ENTRY_OFFSET_DAYS = 0
LABEL_EXIT_OFFSET_DAYS = 5
LABEL_BAR_COUNT = 6
LABEL_HORIZON_INTERVALS = 5

SPLIT_POLICY_VERSION = "predictive-temporal-split-v1"
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


class FeatureState(StrEnum):
    """Per-column and per-sample feature availability state."""

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_YET_AVAILABLE = "not_yet_available"
    INCOMPATIBLE = "incompatible"
    AMBIGUOUS = "ambiguous"


class LabelState(StrEnum):
    """Forward-return label availability state."""

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_YET_AVAILABLE = "not_yet_available"
    INCOMPATIBLE = "incompatible"
    AMBIGUOUS = "ambiguous"


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
    """Predeclared operational admission status for the real dataset."""

    RESEARCH_READY = "research_ready"
    INSUFFICIENT_PIT_SAMPLE = "insufficient_pit_sample"


class ReasonCode(StrEnum):
    """Finite evidence reason codes retained per sample and column."""

    CLOSE_MISSING = "close_missing"
    CLOSE_NOT_YET_AVAILABLE = "close_not_yet_available"
    CLOSE_AMBIGUOUS = "close_ambiguous"
    CLOSE_INVALID = "close_invalid"
    CLOSE_PERIOD_MISMATCH = "close_period_mismatch"
    METRIC_MISSING = "metric_missing"
    METRIC_NOT_YET_AVAILABLE = "metric_not_yet_available"
    METRIC_INCOMPATIBLE = "metric_incompatible"
    METRIC_AMBIGUOUS = "metric_ambiguous"
    LABEL_NOT_MATURED = "label_not_matured"
    LABEL_CLOSE_MISSING = "label_close_missing"
    LABEL_CLOSE_AMBIGUOUS = "label_close_ambiguous"
    LABEL_CLOSE_INVALID = "label_close_invalid"
    LABEL_PERIOD_MISMATCH = "label_period_mismatch"


class EvidenceReason(ContractModel):
    """One finite reason with compact human-readable detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ReasonCode
    detail: NonEmptyStr


class PredictiveFeatureColumnSpec(ContractModel):
    """One preregistered feature column and its exact upstream contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_name: NonEmptyStr
    transform: Literal["close_ratio_1d", "rsi_fraction", "histogram_over_close"]
    metric_key: NonEmptyStr | None = None
    algorithm_version: NonEmptyStr | None = None
    unit: NonEmptyStr | None = None
    parameter_filters: dict[NonEmptyStr, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_column_contract(self) -> PredictiveFeatureColumnSpec:
        """Keep close-derived and metric-derived columns explicitly separated."""
        metric_fields = (self.metric_key, self.algorithm_version, self.unit)
        if self.transform == "close_ratio_1d":
            if any(value is not None for value in metric_fields) or self.parameter_filters:
                raise ValueError("close_ratio_1d columns must not declare metric fields")
        elif any(value is None for value in metric_fields):
            raise ValueError("metric columns require key, algorithm version and unit")
        return self


PREDICTIVE_FEATURE_COLUMNS: tuple[PredictiveFeatureColumnSpec, ...] = (
    PredictiveFeatureColumnSpec(
        column_name=CLOSE_RETURN_COLUMN,
        transform="close_ratio_1d",
    ),
    PredictiveFeatureColumnSpec(
        column_name=RSI_COLUMN,
        transform="rsi_fraction",
        metric_key=RSI_METRIC_KEY,
        algorithm_version=RSI_ALGORITHM_VERSION,
        unit=RSI_UNIT,
        parameter_filters=dict(RSI_PARAMETER_FILTERS),
    ),
    PredictiveFeatureColumnSpec(
        column_name=MACD_HISTOGRAM_COLUMN,
        transform="histogram_over_close",
        metric_key=MACD_HISTOGRAM_METRIC_KEY,
        algorithm_version=MACD_ALGORITHM_VERSION,
        unit=MACD_HISTOGRAM_UNIT,
        parameter_filters=dict(MACD_PARAMETER_FILTERS),
    ),
)


class PredictiveExperimentSpec(ContractModel):
    """Frozen preregistered question, horizon, units, columns and label rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["predictive-experiment-v1"] = EXPERIMENT_SCHEMA_VERSION
    experiment_spec_id: NonEmptyStr = EXPERIMENT_SPEC_ID
    target_version: NonEmptyStr = TARGET_VERSION
    feature_set_version: NonEmptyStr = FEATURE_SET_VERSION
    target_definition: NonEmptyStr = "positive_forward_close_return_5d"
    question_es: NonEmptyStr = (
        "Con información disponible a las 12:00 UTC del día D, ¿será positivo el cambio "
        "de cierre a cierre durante los cinco intervalos diarios posteriores?"
    )
    decision_hour_utc: int = Field(default=DECISION_HOUR_UTC, ge=0, le=23)
    feature_day_offsets: tuple[int, int] = FEATURE_DAY_OFFSETS
    label_entry_offset_days: int = LABEL_ENTRY_OFFSET_DAYS
    label_exit_offset_days: int = LABEL_EXIT_OFFSET_DAYS
    label_bar_count: int = LABEL_BAR_COUNT
    label_horizon_intervals: int = LABEL_HORIZON_INTERVALS
    close_field_name: NonEmptyStr = CLOSE_FIELD_NAME
    frequency: DataFrequency = SUPPORTED_FREQUENCY
    asset_class: AssetClass = SUPPORTED_ASSET_CLASS
    quote_currency: NonEmptyStr = SUPPORTED_QUOTE_CURRENCY
    feature_columns: tuple[PredictiveFeatureColumnSpec, ...] = PREDICTIVE_FEATURE_COLUMNS
    limitations: tuple[NonEmptyStr, ...] = (
        "El label es un retorno bruto observado, no una rentabilidad ejecutable.",
        "Las features no contienen precios, identificadores ni disponibilidad del label.",
        "La hora de decisión se fija antes de analizar labels y no se ajusta por demoras.",
    )

    @model_validator(mode="after")
    def validate_preregistered_spec(self) -> PredictiveExperimentSpec:
        """Fail closed if any preregistered semantic is changed."""
        if self.schema_version != EXPERIMENT_SCHEMA_VERSION:
            raise ValueError("unsupported experiment schema version")
        if self.experiment_spec_id != EXPERIMENT_SPEC_ID:
            raise ValueError("experiment_spec_id differs from the preregistered identity")
        if self.target_version != TARGET_VERSION:
            raise ValueError("target_version differs from the preregistered value")
        if self.feature_set_version != FEATURE_SET_VERSION:
            raise ValueError("feature_set_version differs from the preregistered value")
        if self.decision_hour_utc != DECISION_HOUR_UTC:
            raise ValueError("decision hour is preregistered at 12:00 UTC")
        if self.feature_day_offsets != FEATURE_DAY_OFFSETS:
            raise ValueError("feature day offsets are preregistered as D-1 and D-2")
        if self.label_entry_offset_days != LABEL_ENTRY_OFFSET_DAYS:
            raise ValueError("label entry offset is preregistered as the D bar")
        if self.label_exit_offset_days != LABEL_EXIT_OFFSET_DAYS:
            raise ValueError("label exit offset is preregistered as D+5")
        if self.label_bar_count != LABEL_BAR_COUNT:
            raise ValueError("label requires six continuous bars")
        if self.label_horizon_intervals != LABEL_HORIZON_INTERVALS:
            raise ValueError("label horizon is preregistered as five intervals")
        if self.close_field_name != CLOSE_FIELD_NAME:
            raise ValueError("predictive features consume the close field only")
        if self.frequency is not SUPPORTED_FREQUENCY:
            raise ValueError("predictive features consume daily frequency only")
        if self.asset_class is not SUPPORTED_ASSET_CLASS:
            raise ValueError("the supported profile is crypto spot")
        if self.quote_currency != SUPPORTED_QUOTE_CURRENCY:
            raise ValueError("the supported quote currency is USD")
        if tuple(item.column_name for item in self.feature_columns) != FEATURE_COLUMNS:
            raise ValueError("feature columns must preserve the preregistered order")
        return self


PREREGISTERED_EXPERIMENT = PredictiveExperimentSpec()


class PredictiveDatasetRequest(ContractModel):
    """Explicit inclusive decision range and extraction cut without defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    start_date: date
    end_date: date
    dataset_known_at: UTCDateTime

    @model_validator(mode="after")
    def validate_request(self) -> PredictiveDatasetRequest:
        """Reject inverted, oversized or future decision requests."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        decision_count = (self.end_date - self.start_date).days + 1
        if decision_count > MAX_DECISION_DATES:
            raise ValueError(f"a predictive request is limited to {MAX_DECISION_DATES} dates")
        final_decision = datetime.combine(
            self.end_date,
            time(hour=DECISION_HOUR_UTC),
            tzinfo=UTC,
        )
        if final_decision > self.dataset_known_at:
            raise ValueError("dataset_known_at must not precede the final decision time")
        return self

    def decision_dates(self) -> tuple[date, ...]:
        """Return the inclusive ordered decision dates."""
        count = (self.end_date - self.start_date).days + 1
        return tuple(self.start_date + timedelta(days=offset) for offset in range(count))


def decision_instant(day: date) -> datetime:
    """Return the preregistered 12:00 UTC decision instant for one date."""
    return datetime.combine(day, time(hour=DECISION_HOUR_UTC), tzinfo=UTC)


def bar_start_instant(day: date) -> datetime:
    """Return the 00:00 UTC start of one daily bar."""
    return datetime.combine(day, time(0), tzinfo=UTC)


def label_exit_bar_end(day: date) -> datetime:
    """Return the (D+6) 00:00 UTC end of the exit bar for decision date D."""
    return bar_start_instant(day + timedelta(days=LABEL_EXIT_OFFSET_DAYS + 1))


class PredictiveFeatureEvidence(ContractModel):
    """Per-column evidence for one requested decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_name: NonEmptyStr
    state: FeatureState
    value: PredictiveDecimal | None = None
    metric_result_id: UUID | None = None
    as_of: UTCDateTime | None = None
    available_at: UTCDateTime | None = None
    computed_at: UTCDateTime | None = None
    algorithm_version: NonEmptyStr | None = None
    unit: NonEmptyStr | None = None
    quality: DataQuality | None = None
    reasons: tuple[EvidenceReason, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> PredictiveFeatureEvidence:
        """Require exact values for available columns and reasons otherwise."""
        if self.value is not None and not self.value.is_finite():
            raise ValueError("feature values must be finite Decimals")
        if self.state is FeatureState.AVAILABLE:
            if self.value is None or self.as_of is None or self.available_at is None:
                raise ValueError("available features require value, period and availability")
            if self.reasons:
                raise ValueError("available features must not carry failure reasons")
        elif self.value is not None:
            raise ValueError("unavailable features must not expose a numeric value")
        if self.state is not FeatureState.AVAILABLE and not self.reasons:
            raise ValueError("unavailable features require at least one reason")
        return self


class PredictiveLabelBarEvidence(ContractModel):
    """One selected daily close used to build the forward-return label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bar_start: UTCDateTime
    bar_end: UTCDateTime
    state: FeatureState
    observation_id: UUID | None = None
    available_at: UTCDateTime | None = None
    value: PredictiveDecimal | None = None
    reasons: tuple[EvidenceReason, ...] = ()

    @model_validator(mode="after")
    def validate_bar(self) -> PredictiveLabelBarEvidence:
        """Keep a bar available only with exact positive close evidence."""
        if self.bar_end != self.bar_start + timedelta(days=1):
            raise ValueError("a daily label bar must span exactly one UTC day")
        if self.state is FeatureState.AVAILABLE:
            if (
                self.observation_id is None
                or self.available_at is None
                or self.value is None
                or not self.value.is_finite()
                or self.value <= 0
            ):
                raise ValueError("available label bars require positive finite close evidence")
            if self.reasons:
                raise ValueError("available label bars must not carry failure reasons")
        elif self.value is not None:
            raise ValueError("unavailable label bars must not expose a close value")
        return self


class PredictiveLabelEvidence(ContractModel):
    """Forward five-interval label with six continuous bar evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: LabelState
    decision_at: UTCDateTime
    entry_bar_start: UTCDateTime
    exit_bar_end: UTCDateTime
    entry_close: PredictiveDecimal | None = None
    exit_close: PredictiveDecimal | None = None
    return_5d: PredictiveDecimal | None = None
    label: Literal[0, 1] | None = None
    label_available_at: UTCDateTime | None = None
    bars: tuple[PredictiveLabelBarEvidence, ...]
    reasons: tuple[EvidenceReason, ...] = ()

    @model_validator(mode="after")
    def validate_label(self) -> PredictiveLabelEvidence:
        """Verify the continuous calendar, exact return and binary label rule."""
        if len(self.bars) != LABEL_BAR_COUNT:
            raise ValueError("a predictive label requires exactly six bar evidence rows")
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
                or self.label_available_at is None
            ):
                raise ValueError("available labels require closes, return and binary target")
            if any(bar.state is not FeatureState.AVAILABLE for bar in self.bars):
                raise ValueError("available labels require six available bars")
            with localcontext(DECIMAL34):
                expected_return = self.exit_close / self.entry_close - 1
            if expected_return != self.return_5d:
                raise ValueError("return_5d must equal exit_close / entry_close - 1")
            expected_label = 1 if self.return_5d > 0 else 0
            if self.label != expected_label:
                raise ValueError("label must be one only for a strictly positive return")
            expected_available = max(
                [bar.available_at for bar in self.bars if bar.available_at is not None]
                + [self.exit_bar_end]
            )
            if self.label_available_at != expected_available:
                raise ValueError("label availability must include every input and exit bar end")
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
                    self.label_available_at,
                )
            ):
                raise ValueError("unavailable labels must not expose target values")
            if not self.reasons:
                raise ValueError("unavailable labels require at least one reason")
        return self


def aggregate_feature_state(features: tuple[PredictiveFeatureEvidence, ...]) -> FeatureState:
    """Collapse column states with contradiction-first precedence."""
    states = {item.state for item in features}
    for state in (
        FeatureState.AMBIGUOUS,
        FeatureState.INCOMPATIBLE,
        FeatureState.NOT_YET_AVAILABLE,
        FeatureState.MISSING,
    ):
        if state in states:
            return state
    return FeatureState.AVAILABLE


class PredictiveSample(ContractModel):
    """One auditable requested decision with separated feature and label states."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_key: UUID
    feature_hash: UUID
    row_id: UUID
    decision_at: UTCDateTime
    features: tuple[PredictiveFeatureEvidence, ...]
    label: PredictiveLabelEvidence
    feature_state: FeatureState
    label_state: LabelState
    split: SampleSplit
    purged: bool = False
    purge_reason: PurgeReason | None = None
    eligible: bool

    @model_validator(mode="after")
    def validate_sample(self) -> PredictiveSample:
        """Keep ordered features, temporal identity and eligibility consistent."""
        if tuple(item.column_name for item in self.features) != FEATURE_COLUMNS:
            raise ValueError("sample features must preserve the preregistered column order")
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


class PredictiveSplitPolicy(ContractModel):
    """Versioned temporal split, purge and admission policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["predictive-temporal-split-v1"] = SPLIT_POLICY_VERSION
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
    def validate_policy(self) -> PredictiveSplitPolicy:
        """Require the exact preregistered fractions and floors."""
        if self.policy_version != SPLIT_POLICY_VERSION:
            raise ValueError("unsupported split policy version")
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


PREREGISTERED_SPLIT_POLICY = PredictiveSplitPolicy()


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


class PredictiveDatasetCounts(ContractModel):
    """Auditable counts for every requested decision and evidence state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: int = Field(ge=0)
    feature_available: int = Field(ge=0)
    feature_missing: int = Field(ge=0)
    feature_not_yet_available: int = Field(ge=0)
    feature_incompatible: int = Field(ge=0)
    feature_ambiguous: int = Field(ge=0)
    label_available: int = Field(ge=0)
    label_missing: int = Field(ge=0)
    label_not_yet_available: int = Field(ge=0)
    label_incompatible: int = Field(ge=0)
    label_ambiguous: int = Field(ge=0)
    eligible_before_split: int = Field(ge=0)
    purged: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> PredictiveDatasetCounts:
        """Require every state partition to add up to the requested total."""
        feature_total = (
            self.feature_available
            + self.feature_missing
            + self.feature_not_yet_available
            + self.feature_incompatible
            + self.feature_ambiguous
        )
        label_total = (
            self.label_available
            + self.label_missing
            + self.label_not_yet_available
            + self.label_incompatible
            + self.label_ambiguous
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


class PredictiveSplitSummary(ContractModel):
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
    def validate_split(self) -> PredictiveSplitSummary:
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


class PredictiveReadiness(ContractModel):
    """Predeclared admission decision without statistical promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReadinessStatus
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
    ) -> PredictiveReadiness:
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
            ReadinessStatus.RESEARCH_READY if not unmet else ReadinessStatus.INSUFFICIENT_PIT_SAMPLE
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
    def validate_readiness(self) -> PredictiveReadiness:
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
            ReadinessStatus.RESEARCH_READY if not unmet else ReadinessStatus.INSUFFICIENT_PIT_SAMPLE
        )
        if self.status is not expected:
            raise ValueError("readiness status must follow the preregistered floors")
        if unmet != self.unmet_requirements:
            raise ValueError("unmet requirements must match the observed counts")
        return self


class PredictiveAssetIdentity(ContractModel):
    """Resolved catalog identity and source used by one dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    crypto_profile: NonEmptyStr
    exchange: NonEmptyStr


class PredictiveColumnSpec(ContractModel):
    """One declared parquet column with exact encoding and nullability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    data_type: Literal["text", "integer", "boolean"]
    nullable: bool
    unit: NonEmptyStr | None = None
    description: NonEmptyStr


PREDICTIVE_ROWS_SCHEMA: tuple[PredictiveColumnSpec, ...] = (
    PredictiveColumnSpec(
        name="sample_key",
        data_type="text",
        nullable=False,
        description="Deterministic sample identity.",
    ),
    PredictiveColumnSpec(
        name="row_id",
        data_type="text",
        nullable=False,
        description="Deterministic row identity including feature and label evidence.",
    ),
    PredictiveColumnSpec(
        name="decision_at",
        data_type="text",
        nullable=False,
        unit="iso8601-utc",
        description="Preregistered 12:00 UTC decision instant.",
    ),
    PredictiveColumnSpec(
        name=CLOSE_RETURN_COLUMN,
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="close[D-1] / close[D-2] - 1 as canonical decimal text.",
    ),
    PredictiveColumnSpec(
        name=RSI_COLUMN,
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="RSI[D-1] / 100 as canonical decimal text.",
    ),
    PredictiveColumnSpec(
        name=MACD_HISTOGRAM_COLUMN,
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="MACD histogram[D-1] / close[D-1] as canonical decimal text.",
    ),
    PredictiveColumnSpec(
        name="feature_state",
        data_type="text",
        nullable=False,
        description="Aggregated feature availability state.",
    ),
    PredictiveColumnSpec(
        name="label_state",
        data_type="text",
        nullable=False,
        description="Forward-return label availability state.",
    ),
    PredictiveColumnSpec(
        name="label",
        data_type="integer",
        nullable=True,
        description="One when return_5d is strictly positive, zero otherwise.",
    ),
    PredictiveColumnSpec(
        name="return_5d",
        data_type="text",
        nullable=True,
        unit=FEATURE_RATIO_UNIT,
        description="exit_close / entry_close - 1 as canonical decimal text.",
    ),
    PredictiveColumnSpec(
        name="entry_close",
        data_type="text",
        nullable=True,
        unit="USD",
        description="Close of the D bar as canonical decimal text.",
    ),
    PredictiveColumnSpec(
        name="exit_close",
        data_type="text",
        nullable=True,
        unit="USD",
        description="Close of the D+5 bar as canonical decimal text.",
    ),
    PredictiveColumnSpec(
        name="entry_close_observation_id",
        data_type="text",
        nullable=True,
        description="Persisted observation identity of the entry close.",
    ),
    PredictiveColumnSpec(
        name="exit_close_observation_id",
        data_type="text",
        nullable=True,
        description="Persisted observation identity of the exit close.",
    ),
    PredictiveColumnSpec(
        name="label_available_at",
        data_type="text",
        nullable=True,
        unit="iso8601-utc",
        description="Latest availability of the six closes and the exit bar end.",
    ),
    PredictiveColumnSpec(
        name="feature_evidence_json",
        data_type="text",
        nullable=False,
        description="Canonical JSON with per-column metric ids and availability.",
    ),
    PredictiveColumnSpec(
        name="label_bars_json",
        data_type="text",
        nullable=False,
        description="Canonical JSON with the six label bar evidence rows.",
    ),
    PredictiveColumnSpec(
        name="split",
        data_type="text",
        nullable=False,
        description="Temporal split assigned before eligibility filtering.",
    ),
    PredictiveColumnSpec(
        name="purged",
        data_type="boolean",
        nullable=False,
        description="Whether the label interval overlaps or enters the embargo.",
    ),
    PredictiveColumnSpec(
        name="purge_reason",
        data_type="text",
        nullable=True,
        description="Finite purge reason when purged is true.",
    ),
    PredictiveColumnSpec(
        name="eligible",
        data_type="boolean",
        nullable=False,
        description="Mature features and label with no split purge.",
    ),
)


class PredictiveArtifactSchema(ContractModel):
    """Published schema, order and encoding contract for the exported rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    encoding_version: Literal["predictive-dataset-encoding-v1"] = "predictive-dataset-encoding-v1"
    feature_encoding: Literal["canonical-decimal-text"] = "canonical-decimal-text"
    timestamp_encoding: Literal["iso8601-utc"] = "iso8601-utc"
    id_encoding: Literal["uuid-text"] = "uuid-text"
    order_by: Literal["decision_at,sample_key"] = "decision_at,sample_key"
    columns: tuple[PredictiveColumnSpec, ...] = PREDICTIVE_ROWS_SCHEMA

    @model_validator(mode="after")
    def validate_schema(self) -> PredictiveArtifactSchema:
        """Keep the published column order exactly as declared."""
        if tuple(item.name for item in self.columns) != tuple(
            item.name for item in PREDICTIVE_ROWS_SCHEMA
        ):
            raise ValueError("artifact schema must preserve the declared column order")
        return self


class PredictiveDatasetManifest(ContractModel):
    """Deterministic dataset manifest linking every row to its inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["predictive-dataset-v1"] = DATASET_SCHEMA_VERSION
    dataset_id: UUID
    dataset_content_sha256: str
    experiment: PredictiveExperimentSpec
    request: PredictiveDatasetRequest
    asset: PredictiveAssetIdentity
    workspace_uuid: UUID
    frequency: DataFrequency
    close_field_name: NonEmptyStr
    feature_read_start: UTCDateTime
    feature_read_end: UTCDateTime
    label_read_start: UTCDateTime
    label_read_end: UTCDateTime
    artifact_schema: PredictiveArtifactSchema
    counts: PredictiveDatasetCounts
    split_policy: PredictiveSplitPolicy
    splits: tuple[PredictiveSplitSummary, ...]
    readiness: PredictiveReadiness
    rows: tuple[PredictiveSample, ...]
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> PredictiveDatasetManifest:
        """Require ordered unique rows and the exact three temporal splits."""
        if len(self.dataset_content_sha256) != 64:
            raise ValueError("dataset content hash must be a SHA-256 digest")
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
        return self


class PredictiveDatasetSummary(ContractModel):
    """CLI-safe aggregate summary without per-sample target values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    dataset_content_sha256: str
    experiment_spec_id: NonEmptyStr
    target_version: NonEmptyStr
    feature_set_version: NonEmptyStr
    asset: PredictiveAssetIdentity
    request: PredictiveDatasetRequest
    counts: PredictiveDatasetCounts
    split_policy: PredictiveSplitPolicy
    splits: tuple[PredictiveSplitSummary, ...]
    readiness: PredictiveReadiness
    limitations: tuple[NonEmptyStr, ...]

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


class PredictiveDataset(ContractModel):
    """Complete in-memory dataset plus its manifest and aggregate summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    dataset_content_sha256: str
    manifest: PredictiveDatasetManifest
    summary: PredictiveDatasetSummary
    samples: tuple[PredictiveSample, ...]

    @model_validator(mode="after")
    def validate_dataset(self) -> PredictiveDataset:
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


def _feature_payload(feature: PredictiveFeatureEvidence) -> dict[str, object]:
    return {
        "column_name": feature.column_name,
        "state": feature.state,
        "value": feature.value,
        "metric_result_id": feature.metric_result_id,
        "as_of": feature.as_of,
        "available_at": feature.available_at,
        "algorithm_version": feature.algorithm_version,
        "unit": feature.unit,
        "quality": feature.quality,
        "reasons": feature.reasons,
    }


def _label_payload(label: PredictiveLabelEvidence) -> dict[str, object]:
    return {
        "state": label.state,
        "decision_at": label.decision_at,
        "entry_bar_start": label.entry_bar_start,
        "exit_bar_end": label.exit_bar_end,
        "entry_close": label.entry_close,
        "exit_close": label.exit_close,
        "return_5d": label.return_5d,
        "label": label.label,
        "label_available_at": label.label_available_at,
        "bars": label.bars,
        "reasons": label.reasons,
    }


def predictive_sample_key(
    *,
    experiment_spec_id: str,
    asset_id: str,
    source_id: str,
    decision_at: datetime,
) -> UUID:
    """Derive the sample identity from the experiment, scope and decision."""
    payload = {
        "artifact": "sample_key",
        "experiment_spec_id": experiment_spec_id,
        "asset_id": asset_id,
        "source_id": source_id,
        "decision_at": decision_at,
    }
    return uuid5(EXPERIMENT_NAMESPACE, analytical_content_hash(payload))


def predictive_feature_hash(
    *,
    sample_key: UUID,
    asset_id: str,
    source_id: str,
    decision_at: datetime,
    features: tuple[PredictiveFeatureEvidence, ...],
) -> UUID:
    """Derive the feature identity without any label or future evidence."""
    payload = {
        "artifact": "feature_hash",
        "feature_set_version": FEATURE_SET_VERSION,
        "sample_key": sample_key,
        "asset_id": asset_id,
        "source_id": source_id,
        "decision_at": decision_at,
        "features": tuple(_feature_payload(item) for item in features),
    }
    return uuid5(EXPERIMENT_NAMESPACE, analytical_content_hash(payload))


def predictive_row_id(
    *,
    sample_key: UUID,
    feature_hash: UUID,
    features: tuple[PredictiveFeatureEvidence, ...],
    label: PredictiveLabelEvidence,
) -> UUID:
    """Derive the row identity from the full feature and label evidence."""
    payload = {
        "artifact": "row_id",
        "sample_key": sample_key,
        "feature_hash": feature_hash,
        "features": tuple(_feature_payload(item) for item in features),
        "label": _label_payload(label),
    }
    return uuid5(EXPERIMENT_NAMESPACE, analytical_content_hash(payload))


def _request_payload(request: PredictiveDatasetRequest) -> dict[str, object]:
    return {
        "asset_id": request.asset_id,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "dataset_known_at": request.dataset_known_at,
    }


def predictive_dataset_id(
    *,
    experiment_spec_id: str,
    workspace_uuid: UUID,
    asset_id: str,
    source_id: str,
    request: PredictiveDatasetRequest,
    split_policy: PredictiveSplitPolicy,
    rows: tuple[PredictiveSample, ...],
) -> UUID:
    """Derive the dataset identity from the request, policy and ordered rows."""
    payload = {
        "artifact": "dataset_id",
        "experiment_spec_id": experiment_spec_id,
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "workspace_uuid": workspace_uuid,
        "asset_id": asset_id,
        "source_id": source_id,
        "request": _request_payload(request),
        "split_policy": split_policy,
        "rows": tuple(str(item.row_id) for item in rows),
    }
    return uuid5(EXPERIMENT_NAMESPACE, analytical_content_hash(payload))


def _sample_payload(sample: PredictiveSample) -> dict[str, object]:
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


def predictive_dataset_content_hash(
    *,
    dataset_id: UUID,
    rows: tuple[PredictiveSample, ...],
) -> str:
    """Return the semantic dataset digest independent from physical Parquet bytes."""
    payload = {
        "artifact": "dataset_content",
        "dataset_id": dataset_id,
        "rows": tuple(_sample_payload(item) for item in rows),
    }
    return analytical_content_hash(payload)
