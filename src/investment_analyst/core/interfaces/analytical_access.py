"""Provider-independent read port for persisted analytical artifacts."""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol
from uuid import UUID

from investment_analyst.analytics.analysis_snapshot import AnalysisSnapshot
from investment_analyst.analytics.analytical_access_models import (
    MetricIndexEntry,
    MetricSeriesQuery,
)
from investment_analyst.core.models import DiagnosticResult, MetricDefinition, MetricResult


class AnalyticalAccessPort(Protocol):
    """Read-only port; implementations may use independent storage backends."""

    def validate_snapshot(self, snapshot: AnalysisSnapshot) -> None: ...

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]: ...

    def get_metric_results(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...

    def get_diagnostic_results(
        self, diagnostic_ids: Collection[UUID]
    ) -> dict[UUID, DiagnosticResult]: ...

    def get_metric_definition(self, metric_key: str) -> MetricDefinition: ...


__all__ = ["AnalyticalAccessPort"]
