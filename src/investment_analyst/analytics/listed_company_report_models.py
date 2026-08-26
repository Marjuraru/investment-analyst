"""Versioned presentation contract for a catalog-selected listed company."""

from typing import Literal

from pydantic import ConfigDict, model_validator

from investment_analyst.analytics.aapl_daily_report_models import (
    AaplDailyDiagnosticSection,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticView,
    ListedCompanyDiagnosticRequest,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr

LISTED_COMPANY_REPORT_SCHEMA_VERSION = "listed-company-diagnostic-report-v1"
LISTED_COMPANY_REPORT_LIMITATIONS = (
    "Market and fundamental diagnostics remain independent; no combined score, verdict, "
    "confidence, quality, recommendation, or ranking is calculated.",
    "Market and fundamental evidence is selected independently at the requested point in time.",
    "This report is descriptive analytical output, not financial advice, and it does not execute "
    "operations.",
)


ListedCompanyReportRequest = ListedCompanyDiagnosticRequest


class ListedCompanyReportAsset(ContractModel):
    """Stable selected-asset identity carried by the generic response."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_sources(self) -> "ListedCompanyReportAsset":
        if not self.source_ids or self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("report source IDs must be non-empty, unique, and sorted")
        return self


class ListedCompanyDiagnosticReport(ContractModel):
    """Read-only report with independent market and fundamental sections."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["listed-company-diagnostic-report-v1"] = (
        LISTED_COMPANY_REPORT_SCHEMA_VERSION
    )
    asset: ListedCompanyReportAsset
    view: ConsolidatedDiagnosticView
    market: AaplDailyDiagnosticSection
    fundamental: AaplDailyDiagnosticSection
    limitations: tuple[NonEmptyStr, ...] = LISTED_COMPANY_REPORT_LIMITATIONS

    @model_validator(mode="after")
    def validate_report(self) -> "ListedCompanyDiagnosticReport":
        if self.asset.asset_id != self.view.request.asset_id:
            raise ValueError("report asset must match request asset_id")
        if (
            self.market.selection != self.view.market
            or self.fundamental.selection != self.view.fundamental
        ):
            raise ValueError("report sections must preserve point-in-time selection")
        if self.limitations != LISTED_COMPANY_REPORT_LIMITATIONS:
            raise ValueError("generic report limitations must preserve the versioned contract")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact HTTP representation without provider access or writes."""
        compact = self.view.to_json_dict()
        return {
            "schema_version": self.schema_version,
            "asset": self.asset.model_dump(mode="json"),
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


def _section_json(compact: object, section: AaplDailyDiagnosticSection) -> dict[str, object]:
    if not isinstance(compact, dict):
        raise TypeError("compact diagnostic section must be a dictionary")
    enriched = dict(compact)
    enriched["metrics"] = [item.model_dump(mode="json") for item in section.metrics]
    enriched["freshness"] = {
        "reference_age_days": section.reference_age_days,
        "availability_age_days": section.availability_age_days,
    }
    return enriched
