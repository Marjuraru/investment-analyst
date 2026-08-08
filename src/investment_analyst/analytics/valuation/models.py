"""Strict public contracts for latest-annual corporate valuation."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial values must use Decimal, not float or bool")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


class ValuationStatus(StrEnum):
    """Whether one descriptive metric is applicable and evaluable."""

    EVALUATED = "evaluated"
    NOT_EVALUABLE = "not_evaluable"
    NOT_APPLICABLE = "not_applicable"


class ValuationSnapshotStatus(StrEnum):
    """Coverage state of a valuation snapshot without an aggregate verdict."""

    EVALUATED = "evaluated"
    PARTIAL = "partial"
    NOT_EVALUABLE = "not_evaluable"
    NOT_APPLICABLE = "not_applicable"


class ValuationReasonCode(StrEnum):
    """Closed, audit-friendly reasons for an unavailable valuation result."""

    ASSET_NOT_APPLICABLE = "asset_not_applicable"
    MARKET_NOT_CONFIGURED = "market_not_configured"
    FUNDAMENTALS_NOT_CONFIGURED = "fundamentals_not_configured"
    SHARE_BASIS_UNAVAILABLE = "share_basis_unavailable"
    SECURITY_UNIT_MISMATCH = "security_unit_mismatch"
    CORPORATE_ACTION_BASIS_UNAVAILABLE = "corporate_action_basis_unavailable"
    PRICE_UNAVAILABLE = "price_unavailable"
    PRICE_AMBIGUOUS = "price_ambiguous"
    FUNDAMENTALS_UNAVAILABLE = "fundamentals_unavailable"
    FUNDAMENTAL_REVISION_AMBIGUOUS = "fundamental_revision_ambiguous"
    PERIOD_MISMATCH = "period_mismatch"
    SOURCE_MISMATCH = "source_mismatch"
    FREQUENCY_MISMATCH = "frequency_mismatch"
    ACCOUNTING_BASIS_MISMATCH = "accounting_basis_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    MISSING_INPUT = "missing_input"
    INVALID_DENOMINATOR = "invalid_denominator"
    EBITDA_UNAVAILABLE = "ebitda_unavailable"


class CorporateValuationRequest(ContractModel):
    """One explicit point-in-time latest-annual valuation request."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["corporate-valuation-request-v1"] = "corporate-valuation-request-v1"
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    valuation_date: date
    basis: Literal["latest_annual"] = "latest_annual"

    @field_validator("valuation_date", mode="before")
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        if isinstance(value, str):
            return date.fromisoformat(value)
        return value


class ValuationSecurityBasis(ContractModel):
    """Catalog-backed conversion between a traded unit and a reported share."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    basis: Literal["reported_common_share", "depositary_receipt"]
    market_units_per_reported_share: FinancialDecimal
    market_adjustment: Literal["all"]
    contract_version: NonEmptyStr

    @field_validator("market_units_per_reported_share")
    @classmethod
    def positive_finite_factor(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("market_units_per_reported_share must be finite and positive")
        return value


class ValuationInput(ContractModel):
    """Exact persisted input used by one valuation snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: NonEmptyStr
    observation_id: UUID
    raw_record_id: UUID
    source_id: NonEmptyStr
    value: FinancialDecimal
    unit: NonEmptyStr
    frequency: DataFrequency
    observed_at: UTCDateTime | None = None
    period_start: UTCDateTime | None = None
    period_end: UTCDateTime | None = None
    available_at: UTCDateTime
    accession_number: NonEmptyStr | None = None
    taxonomy: NonEmptyStr | None = None
    tag: NonEmptyStr | None = None

    @field_validator("value")
    @classmethod
    def finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("valuation inputs must be finite")
        return value

    @model_validator(mode="after")
    def validate_temporal_shape(self) -> "ValuationInput":
        if self.period_start is not None and self.period_end is None:
            raise ValueError("period_start requires period_end")
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ValueError("period_start must not be after period_end")
        return self


class ValuationMetricDefinition(ContractModel):
    """Published formula, exact inputs, unit and limitations for one metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    display_name_es: NonEmptyStr
    formula: NonEmptyStr
    input_roles: tuple[NonEmptyStr, ...]
    unit: Literal["USD", "ratio", "percentage"]
    algorithm_version: NonEmptyStr
    definition_version: NonEmptyStr
    limitations: tuple[NonEmptyStr, ...] = ()


class ValuationMetricValue(ContractModel):
    """An evaluated value or an explicit non-evaluable/not-applicable state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    status: ValuationStatus
    value: FinancialDecimal | None = None
    reason_code: ValuationReasonCode | None = None
    result_id: UUID | None = None
    available_at: UTCDateTime | None = None
    input_observation_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def state_is_exact(self) -> "ValuationMetricValue":
        if self.status is ValuationStatus.EVALUATED:
            if (
                self.value is None
                or self.reason_code is not None
                or self.result_id is None
                or self.available_at is None
                or not self.input_observation_ids
            ):
                raise ValueError("evaluated valuation metrics require value, result and inputs")
            if not self.value.is_finite():
                raise ValueError("valuation values must be finite")
        elif self.value is not None or self.reason_code is None or self.result_id is not None:
            raise ValueError("unavailable valuation metrics require only a reason_code")
        return self


class ValuationCoverage(ContractModel):
    """Deterministic metric coverage counts for compact HTTP clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=1)
    evaluated: int = Field(ge=0)
    not_evaluable: int = Field(ge=0)
    not_applicable: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_partition_total(self) -> "ValuationCoverage":
        if self.total != self.evaluated + self.not_evaluable + self.not_applicable:
            raise ValueError("valuation coverage counts must partition total")
        return self


class CorporateValuationSnapshot(ContractModel):
    """Compact read-only result, deliberately independent from diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["corporate-valuation-snapshot-v1"] = "corporate-valuation-snapshot-v1"
    asset_id: NonEmptyStr
    request: CorporateValuationRequest
    status: ValuationSnapshotStatus
    valuation_as_of: UTCDateTime | None = None
    known_at: UTCDateTime
    computed_at: UTCDateTime
    available_at: UTCDateTime | None = None
    price_age_days: int | None = Field(default=None, ge=0)
    annual_period_start: UTCDateTime | None = None
    annual_period_end: UTCDateTime | None = None
    filing_date: date | None = None
    filing_accepted_at: UTCDateTime | None = None
    filing_accession_number: NonEmptyStr | None = None
    filing_form: NonEmptyStr | None = None
    fiscal_year: NonEmptyStr | None = None
    fiscal_period: NonEmptyStr | None = None
    price_currency: NonEmptyStr | None = None
    report_currency: NonEmptyStr | None = None
    price_source_id: NonEmptyStr | None = None
    fundamental_source_id: NonEmptyStr | None = None
    security_basis: ValuationSecurityBasis | None = None
    inputs: tuple[ValuationInput, ...] = ()
    definitions: tuple[ValuationMetricDefinition, ...]
    metrics: tuple[ValuationMetricValue, ...]
    coverage: ValuationCoverage
    limitations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> "CorporateValuationSnapshot":
        if self.request.asset_id != self.asset_id or self.request.known_at != self.known_at:
            raise ValueError("valuation snapshot must preserve the request identity and cut")
        metric_keys = tuple(item.metric_key for item in self.metrics)
        if metric_keys != tuple(sorted(metric_keys)) or len(metric_keys) != len(set(metric_keys)):
            raise ValueError("valuation metrics must be ordered and unique")
        definition_keys = tuple(item.metric_key for item in self.definitions)
        if definition_keys != metric_keys:
            raise ValueError("valuation definitions must match metric values")
        if self.coverage.total != len(self.metrics):
            raise ValueError("valuation coverage total must match metrics")
        if self.available_at is not None and self.available_at > self.computed_at:
            raise ValueError("available_at must not be after computed_at")
        expected_status = _snapshot_status(self.metrics)
        if self.status is not expected_status:
            raise ValueError("valuation snapshot status must match metric coverage")
        if (
            self.valuation_as_of is not None
            and self.valuation_as_of.date() > self.request.valuation_date
        ):
            raise ValueError("valuation price must not be after valuation_date")
        if self.filing_accepted_at is not None and self.filing_accepted_at > self.known_at:
            raise ValueError("filing acceptance must not be after known_at")
        input_ids = {item.observation_id for item in self.inputs}
        if any(not set(metric.input_observation_ids) <= input_ids for metric in self.metrics):
            raise ValueError("metric input IDs must belong to snapshot inputs")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-safe projection preserving Decimal as text."""
        return self.model_dump(mode="json")


def _snapshot_status(metrics: tuple[ValuationMetricValue, ...]) -> ValuationSnapshotStatus:
    statuses = {item.status for item in metrics}
    if statuses == {ValuationStatus.EVALUATED}:
        return ValuationSnapshotStatus.EVALUATED
    if ValuationStatus.EVALUATED in statuses:
        return ValuationSnapshotStatus.PARTIAL
    if statuses == {ValuationStatus.NOT_APPLICABLE}:
        return ValuationSnapshotStatus.NOT_APPLICABLE
    return ValuationSnapshotStatus.NOT_EVALUABLE


__all__ = [
    "CorporateValuationRequest",
    "CorporateValuationSnapshot",
    "FinancialDecimal",
    "ValuationCoverage",
    "ValuationInput",
    "ValuationMetricDefinition",
    "ValuationMetricValue",
    "ValuationReasonCode",
    "ValuationSecurityBasis",
    "ValuationSnapshotStatus",
    "ValuationStatus",
]
