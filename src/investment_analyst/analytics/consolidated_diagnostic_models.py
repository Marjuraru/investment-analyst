"""Strict models for read-only consolidated Apple diagnostic queries."""

from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.core.models import DataFrequency, DiagnosticMode, DiagnosticResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID

_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


class ConsolidatedDiagnosticStatus(StrEnum):
    """Availability state of the two independent diagnostic sections."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ConsolidatedSectionStatus(StrEnum):
    """Availability state of one independent diagnostic section."""

    AVAILABLE = "available"
    NOT_FOUND = "not_found"


class ConsolidatedDiagnosticRequest(ContractModel):
    """Point-in-time request for independent Apple market and fundamental diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr = ASSET_ID
    known_at: UTCDateTime
    fundamental_frequency: DataFrequency
    market_as_of: date | None = None
    fundamental_as_of: date | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "ConsolidatedDiagnosticRequest":
        """Validate the fixed Apple scope and requested reference dates."""
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.fundamental_frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("fundamental_frequency must be annual or quarterly")
        if self.market_as_of is not None and self.market_as_of > self.known_at.date():
            raise ValueError("market_as_of must not be later than known_at")
        if self.fundamental_as_of is not None and self.fundamental_as_of > self.known_at.date():
            raise ValueError("fundamental_as_of must not be later than known_at")
        return self


class ConsolidatedDiagnosticSection(ContractModel):
    """One independently selected diagnostic mode in a consolidated view."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    status: ConsolidatedSectionStatus
    mode: DiagnosticMode
    diagnostic: DiagnosticResult | None = None
    selected_metric_result_ids: tuple[UUID, ...] = ()
    computed_after_known_at: bool = False
    not_found_reason: NonEmptyStr | None = None
    revisions_superseded: int = 0
    candidates_examined: int = 0
    candidates_eligible: int = 0

    @field_validator(
        "revisions_superseded",
        "candidates_examined",
        "candidates_eligible",
        mode="before",
    )
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans masquerading as integer counters."""
        if isinstance(value, bool):
            raise ValueError("section counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_section(self) -> "ConsolidatedDiagnosticSection":
        """Keep availability state, diagnostic payload, and metric IDs consistent."""
        if (
            min(
                self.revisions_superseded,
                self.candidates_examined,
                self.candidates_eligible,
            )
            < 0
        ):
            raise ValueError("section counters must be non-negative")
        if self.candidates_eligible > self.candidates_examined:
            raise ValueError("eligible candidates cannot exceed examined candidates")
        if len(set(self.selected_metric_result_ids)) != len(self.selected_metric_result_ids):
            raise ValueError("selected metric result IDs must be unique")
        expected_order = tuple(sorted(self.selected_metric_result_ids, key=str))
        if self.selected_metric_result_ids != expected_order:
            raise ValueError("selected metric result IDs must be deterministically ordered")

        if self.status is ConsolidatedSectionStatus.AVAILABLE:
            if self.diagnostic is None:
                raise ValueError("available section requires a diagnostic")
            if self.not_found_reason is not None:
                raise ValueError("available section cannot include a not-found reason")
            if self.diagnostic.mode is not self.mode:
                raise ValueError("section mode must match diagnostic mode")
        else:
            if self.diagnostic is not None:
                raise ValueError("not-found section cannot include a diagnostic")
            if self.not_found_reason is None:
                raise ValueError("not-found section requires a reason")
            if self.selected_metric_result_ids:
                raise ValueError("not-found section cannot include metric result IDs")
            if self.computed_after_known_at:
                raise ValueError("not-found section cannot be computed after known_at")
        return self


class ConsolidatedTemporalContext(ContractModel):
    """Independent reference dates and publication times for both diagnostic modes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_as_of: UTCDateTime | None = None
    fundamental_as_of: UTCDateTime | None = None
    reference_gap_days: int | None = None
    market_available_at: UTCDateTime | None = None
    fundamental_available_at: UTCDateTime | None = None

    @field_validator("reference_gap_days", mode="before")
    @classmethod
    def reject_boolean_gap(cls, value: object) -> object:
        """Reject booleans masquerading as a day count."""
        if isinstance(value, bool):
            raise ValueError("reference_gap_days must be an integer")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> "ConsolidatedTemporalContext":
        """Validate optional timestamps and the exact absolute reference gap."""
        market_pair = (self.market_as_of, self.market_available_at)
        fundamental_pair = (self.fundamental_as_of, self.fundamental_available_at)
        if (market_pair[0] is None) != (market_pair[1] is None):
            raise ValueError("market reference and availability must appear together")
        if (fundamental_pair[0] is None) != (fundamental_pair[1] is None):
            raise ValueError("fundamental reference and availability must appear together")
        if self.market_as_of is None or self.fundamental_as_of is None:
            if self.reference_gap_days is not None:
                raise ValueError("reference_gap_days requires both diagnostics")
        else:
            expected = abs((self.market_as_of.date() - self.fundamental_as_of.date()).days)
            if self.reference_gap_days != expected:
                raise ValueError("reference_gap_days does not match diagnostic dates")
        if self.reference_gap_days is not None and self.reference_gap_days < 0:
            raise ValueError("reference_gap_days must be non-negative")
        return self


class ConsolidatedDiagnosticView(ContractModel):
    """Compact, traceable, and strictly non-combined diagnostic query result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: ConsolidatedDiagnosticRequest
    status: ConsolidatedDiagnosticStatus
    market: ConsolidatedDiagnosticSection
    fundamental: ConsolidatedDiagnosticSection
    temporal_context: ConsolidatedTemporalContext
    diagnostics_examined: int
    metric_results_examined: int
    ignored_algorithm_versions: int
    traceability_verified: bool

    @field_validator(
        "diagnostics_examined",
        "metric_results_examined",
        "ignored_algorithm_versions",
        mode="before",
    )
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans masquerading as integer counters."""
        if isinstance(value, bool):
            raise ValueError("view counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_view(self) -> "ConsolidatedDiagnosticView":
        """Validate status derivation, mode separation, counters, and traceability."""
        if (
            min(
                self.diagnostics_examined,
                self.metric_results_examined,
                self.ignored_algorithm_versions,
            )
            < 0
        ):
            raise ValueError("view counters must be non-negative")
        if self.market.mode is not DiagnosticMode.MARKET:
            raise ValueError("market section must use MARKET mode")
        if self.fundamental.mode is not DiagnosticMode.FUNDAMENTAL:
            raise ValueError("fundamental section must use FUNDAMENTAL mode")
        available = sum(
            section.status is ConsolidatedSectionStatus.AVAILABLE
            for section in (self.market, self.fundamental)
        )
        expected = {
            0: ConsolidatedDiagnosticStatus.UNAVAILABLE,
            1: ConsolidatedDiagnosticStatus.PARTIAL,
            2: ConsolidatedDiagnosticStatus.COMPLETE,
        }[available]
        if self.status is not expected:
            raise ValueError("consolidated status does not match section availability")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact deterministic JSON-compatible representation."""
        return {
            "request": self.request.model_dump(mode="json"),
            "status": self.status.value,
            "market": _section_json(self.market),
            "fundamental": _section_json(self.fundamental),
            "temporal_context": self.temporal_context.model_dump(mode="json"),
            "diagnostics_examined": self.diagnostics_examined,
            "metric_results_examined": self.metric_results_examined,
            "ignored_algorithm_versions": self.ignored_algorithm_versions,
            "traceability_verified": self.traceability_verified,
        }


def _section_json(section: ConsolidatedDiagnosticSection) -> dict[str, object]:
    """Serialize one section without embedding referenced MetricResult documents."""
    diagnostic = section.diagnostic
    diagnostic_json: dict[str, object] | None = None
    if diagnostic is not None:
        diagnostic_json = {
            "diagnostic_id": str(diagnostic.diagnostic_id),
            "mode": diagnostic.mode.value,
            "verdict": diagnostic.verdict.value,
            "final_score": str(diagnostic.final_score),
            "confidence": str(diagnostic.confidence),
            "quality": diagnostic.quality.value,
            "as_of": diagnostic.as_of.isoformat(),
            "available_at": diagnostic.available_at.isoformat(),
            "computed_at": diagnostic.computed_at.isoformat(),
            "algorithm_version": diagnostic.algorithm_version,
            "summary": diagnostic.summary,
            "components": [item.model_dump(mode="json") for item in diagnostic.components],
            "evidence": [item.model_dump(mode="json") for item in diagnostic.evidence],
        }
    return {
        "status": section.status.value,
        "mode": section.mode.value,
        "diagnostic": diagnostic_json,
        "selected_metric_result_ids": [str(item) for item in section.selected_metric_result_ids],
        "computed_after_known_at": section.computed_after_known_at,
        "not_found_reason": section.not_found_reason,
        "revisions_superseded": section.revisions_superseded,
        "candidates_examined": section.candidates_examined,
        "candidates_eligible": section.candidates_eligible,
    }
