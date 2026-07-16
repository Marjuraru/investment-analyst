"""Read-only assembly of the versioned Apple daily diagnostic report."""

from dataclasses import dataclass
from uuid import UUID

from investment_analyst.analytics.aapl_daily_report_models import (
    AaplDailyDiagnosticReport,
    AaplDailyDiagnosticSection,
    AaplDailyMetric,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticSection,
    ConsolidatedSectionStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.statistics_definitions import (
    get_market_statistics_definitions,
)
from investment_analyst.core.models import DiagnosticMode, MetricResult
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError


@dataclass(frozen=True, slots=True)
class _MetricMetadata:
    display_name: str
    formula: str
    unit: str


_FUNDAMENTAL_DISPLAY_NAMES = {
    "fundamental.net_margin": "Net Margin",
    "fundamental.liabilities_to_assets": "Liabilities to Assets",
    "fundamental.liabilities_to_equity": "Liabilities to Equity",
    "fundamental.revenue_yoy_growth": "Revenue Year-over-Year Growth",
    "fundamental.net_income_yoy_change_rate": "Net Income Year-over-Year Change Rate",
}
_MARKET_METADATA = {
    item.metric_key: _MetricMetadata(
        display_name=item.display_name,
        formula=item.formula,
        unit=item.unit,
    )
    for item in get_market_statistics_definitions()
}
_FUNDAMENTAL_METADATA = {
    item.metric_name: _MetricMetadata(
        display_name=_FUNDAMENTAL_DISPLAY_NAMES[item.metric_name],
        formula=item.formula,
        unit=item.unit,
    )
    for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
}


class AaplDailyReportError(RuntimeError):
    """Base error for Apple daily report presentation."""


class AaplDailyReportTraceabilityError(AaplDailyReportError):
    """Raised when selected persisted evidence cannot be presented safely."""


class AaplDailyReportService:
    """Resolve selected metrics without providers, recomputation, or persistence."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def query(self, request: ConsolidatedDiagnosticRequest) -> AaplDailyDiagnosticReport:
        """Build one deterministic report from the point-in-time consolidated view."""
        self._storage.require_open()
        view = AaplConsolidatedDiagnosticService(self._storage).query(request)
        return AaplDailyDiagnosticReport(
            view=view,
            market=self._section(view.market, request),
            fundamental=self._section(view.fundamental, request),
        )

    def _section(
        self,
        selection: ConsolidatedDiagnosticSection,
        request: ConsolidatedDiagnosticRequest,
    ) -> AaplDailyDiagnosticSection:
        if selection.status is ConsolidatedSectionStatus.NOT_FOUND:
            return AaplDailyDiagnosticSection(selection=selection)
        diagnostic = selection.diagnostic
        if diagnostic is None:
            raise AaplDailyReportTraceabilityError("available diagnostic section has no diagnostic")

        metrics = tuple(
            sorted(
                (
                    self._metric(identifier, selection.mode, request)
                    for identifier in selection.selected_metric_result_ids
                ),
                key=lambda item: (
                    item.metric_key,
                    item.as_of,
                    item.available_at,
                    str(item.result_id),
                ),
            )
        )
        return AaplDailyDiagnosticSection(
            selection=selection,
            metrics=metrics,
            reference_age_days=(request.known_at.date() - diagnostic.as_of.date()).days,
            availability_age_days=(request.known_at.date() - diagnostic.available_at.date()).days,
        )

    def _metric(
        self,
        identifier: UUID,
        mode: DiagnosticMode,
        request: ConsolidatedDiagnosticRequest,
    ) -> AaplDailyMetric:
        try:
            result = self._storage.metric_results.get(identifier)
        except RecordNotFoundError as error:
            raise AaplDailyReportTraceabilityError(
                f"selected metric result {identifier} is missing"
            ) from error
        if result.result_id != identifier:
            raise AaplDailyReportTraceabilityError(
                f"selected metric result {identifier} has a mismatched persisted identity"
            )
        self._validate_metric(result, mode, request)
        metadata = self._metadata(result.metric_key, mode)
        return AaplDailyMetric(
            result_id=result.result_id,
            metric_key=result.metric_key,
            display_name=metadata.display_name,
            formula=metadata.formula,
            value=result.value,
            unit=result.unit,
            as_of=result.as_of,
            available_at=result.available_at,
            computed_at=result.computed_at,
            parameters=dict(result.parameters),
            input_observation_ids=tuple(sorted(result.input_observation_ids, key=str)),
            algorithm_version=result.algorithm_version,
            quality=result.quality,
        )

    @staticmethod
    def _validate_metric(
        result: MetricResult,
        mode: DiagnosticMode,
        request: ConsolidatedDiagnosticRequest,
    ) -> None:
        if result.asset_id != request.asset_id:
            raise AaplDailyReportTraceabilityError(
                f"selected metric result {result.result_id} belongs to another asset"
            )
        if result.available_at > request.known_at:
            raise AaplDailyReportTraceabilityError(
                f"selected metric result {result.result_id} was unavailable at known_at"
            )
        if result.as_of > request.known_at:
            raise AaplDailyReportTraceabilityError(
                f"selected metric result {result.result_id} is later than known_at"
            )
        metadata = AaplDailyReportService._metadata(result.metric_key, mode)
        if result.unit != metadata.unit:
            raise AaplDailyReportTraceabilityError(
                f"selected metric result {result.result_id} has an unexpected unit"
            )

    @staticmethod
    def _metadata(metric_key: str, mode: DiagnosticMode) -> _MetricMetadata:
        metadata_by_key = {
            DiagnosticMode.MARKET: _MARKET_METADATA,
            DiagnosticMode.FUNDAMENTAL: _FUNDAMENTAL_METADATA,
        }.get(mode)
        if metadata_by_key is None:
            raise AaplDailyReportTraceabilityError(
                "daily report supports only market and fundamental modes"
            )
        try:
            return metadata_by_key[metric_key]
        except KeyError as error:
            raise AaplDailyReportTraceabilityError(
                f"selected {mode.value} metric {metric_key!r} is unsupported"
            ) from error


__all__ = [
    "AaplDailyReportError",
    "AaplDailyReportService",
    "AaplDailyReportTraceabilityError",
]
