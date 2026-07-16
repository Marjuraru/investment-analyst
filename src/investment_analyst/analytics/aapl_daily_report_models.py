"""Versioned contracts for the read-only Apple daily diagnostic report."""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, JsonValue, field_validator, model_validator

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticSection,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
)
from investment_analyst.core.models import DataQuality, DiagnosticMode
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

AAPL_DAILY_REPORT_SCHEMA_VERSION = "aapl-daily-diagnostic-report-v1"
AAPL_DAILY_REPORT_LIMITATIONS = (
    "Market and fundamental diagnostics remain independent; no combined score, verdict, "
    "confidence, quality, recommendation, or ranking is calculated.",
    "Apple market data uses Alpaca Market Data IEX daily bars with adjustment all; IEX is "
    "single-exchange coverage and is not consolidated SIP coverage.",
    "Apple fundamental data comes from official SEC EDGAR submissions and company facts.",
    "Diagnostic confidence describes evidence coverage under deterministic rules; it is not a "
    "calibrated probability.",
    "This report is descriptive analytical output, not financial advice, and it does not execute "
    "operations.",
)


def _reject_financial_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial values must use Decimal, not float or bool")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_financial_float)]


class AaplDailyMetric(ContractModel):
    """One selected metric resolved for human-readable diagnostic evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    result_id: UUID
    metric_key: NonEmptyStr
    display_name: NonEmptyStr
    formula: NonEmptyStr
    value: FinancialDecimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    computed_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue]
    input_observation_ids: tuple[UUID, ...]
    algorithm_version: NonEmptyStr
    quality: DataQuality

    @model_validator(mode="after")
    def validate_metric(self) -> "AaplDailyMetric":
        """Require finite values, valid timing, and deterministic traceability IDs."""
        if not self.value.is_finite():
            raise ValueError("metric value must be finite")
        if self.available_at > self.computed_at:
            raise ValueError("metric available_at must not be later than computed_at")
        if not self.input_observation_ids:
            raise ValueError("metric must reference at least one observation")
        if len(set(self.input_observation_ids)) != len(self.input_observation_ids):
            raise ValueError("metric observation IDs must be unique")
        expected = tuple(sorted(self.input_observation_ids, key=str))
        if self.input_observation_ids != expected:
            raise ValueError("metric observation IDs must be deterministically ordered")
        return self


class AaplDailyDiagnosticSection(ContractModel):
    """One independent diagnostic section enriched with its resolved metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    selection: ConsolidatedDiagnosticSection
    metrics: tuple[AaplDailyMetric, ...] = ()
    reference_age_days: int | None = None
    availability_age_days: int | None = None

    @field_validator("reference_age_days", "availability_age_days", mode="before")
    @classmethod
    def reject_boolean_ages(cls, value: object) -> object:
        """Reject booleans masquerading as calendar-day ages."""
        if isinstance(value, bool):
            raise ValueError("freshness ages must be integers")
        return value

    @model_validator(mode="after")
    def validate_section(self) -> "AaplDailyDiagnosticSection":
        """Keep selection, resolved metrics, and freshness metadata aligned."""
        metric_ids = tuple(item.result_id for item in self.metrics)
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("resolved metric IDs must be unique")
        expected_order = tuple(
            sorted(
                self.metrics,
                key=lambda item: (
                    item.metric_key,
                    item.as_of,
                    item.available_at,
                    str(item.result_id),
                ),
            )
        )
        if self.metrics != expected_order:
            raise ValueError("resolved metrics must be deterministically ordered")

        if self.selection.status is ConsolidatedSectionStatus.NOT_FOUND:
            if self.metrics:
                raise ValueError("not-found section cannot include resolved metrics")
            if self.reference_age_days is not None or self.availability_age_days is not None:
                raise ValueError("not-found section cannot include freshness ages")
            return self

        if set(metric_ids) != set(self.selection.selected_metric_result_ids):
            raise ValueError("resolved metrics must match selected metric result IDs")
        if self.reference_age_days is None or self.availability_age_days is None:
            raise ValueError("available section requires freshness ages")
        if min(self.reference_age_days, self.availability_age_days) < 0:
            raise ValueError("freshness ages must be non-negative")
        expected_prefix = {
            DiagnosticMode.MARKET: "market.",
            DiagnosticMode.FUNDAMENTAL: "fundamental.",
        }.get(self.selection.mode)
        if expected_prefix is None:
            raise ValueError("daily report supports only market and fundamental modes")
        if any(not item.metric_key.startswith(expected_prefix) for item in self.metrics):
            raise ValueError("resolved metric mode does not match its diagnostic section")
        return self


class AaplDailyDiagnosticReport(ContractModel):
    """Versioned daily presentation of two independent Apple diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["aapl-daily-diagnostic-report-v1"] = AAPL_DAILY_REPORT_SCHEMA_VERSION
    view: ConsolidatedDiagnosticView
    market: AaplDailyDiagnosticSection
    fundamental: AaplDailyDiagnosticSection
    limitations: tuple[NonEmptyStr, ...] = AAPL_DAILY_REPORT_LIMITATIONS

    @model_validator(mode="after")
    def validate_report(self) -> "AaplDailyDiagnosticReport":
        """Preserve the consolidated selection and strict analytical separation."""
        if self.market.selection != self.view.market:
            raise ValueError("market report section must preserve the selected market diagnostic")
        if self.fundamental.selection != self.view.fundamental:
            raise ValueError(
                "fundamental report section must preserve the selected fundamental diagnostic"
            )
        if self.market.selection.mode is not DiagnosticMode.MARKET:
            raise ValueError("market report section must use MARKET mode")
        if self.fundamental.selection.mode is not DiagnosticMode.FUNDAMENTAL:
            raise ValueError("fundamental report section must use FUNDAMENTAL mode")
        if self.limitations != AAPL_DAILY_REPORT_LIMITATIONS:
            raise ValueError("daily report limitations must preserve the versioned contract")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic enriched JSON representation for schema v1."""
        compact = self.view.to_json_dict()
        return {
            "schema_version": self.schema_version,
            "query": compact["request"],
            "status": compact["status"],
            "market": _section_json(compact["market"], self.market),
            "fundamental": _section_json(compact["fundamental"], self.fundamental),
            "temporal_context": compact["temporal_context"],
            "traceability": {
                "diagnostics_examined": compact["diagnostics_examined"],
                "metric_results_examined": compact["metric_results_examined"],
                "ignored_algorithm_versions": compact["ignored_algorithm_versions"],
                "verified": compact["traceability_verified"],
            },
            "limitations": list(self.limitations),
        }


def _section_json(
    compact_section: object,
    section: AaplDailyDiagnosticSection,
) -> dict[str, object]:
    """Add resolved metrics and freshness to one compact diagnostic section."""
    if not isinstance(compact_section, dict):
        raise TypeError("compact diagnostic section must be a dictionary")
    enriched: dict[str, object] = dict(compact_section)
    enriched["metrics"] = [item.model_dump(mode="json") for item in section.metrics]
    enriched["freshness"] = {
        "reference_age_days": section.reference_age_days,
        "availability_age_days": section.availability_age_days,
    }
    return enriched


__all__ = [
    "AAPL_DAILY_REPORT_LIMITATIONS",
    "AAPL_DAILY_REPORT_SCHEMA_VERSION",
    "AaplDailyDiagnosticReport",
    "AaplDailyDiagnosticSection",
    "AaplDailyMetric",
]
