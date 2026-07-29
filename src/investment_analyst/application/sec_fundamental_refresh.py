"""Independent append-only SEC refresh for one catalog-backed issuer."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshStage,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataQuality
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    sec_transformation_version,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticRequest,
    SecFundamentalDiagnosticRunSummary,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricImportSummary,
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecObservationImportSummary,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecIssuerImportSummary,
)
from investment_analyst.storage import LocalStorage

_StageResult = TypeVar("_StageResult")


class _FetchPipeline(Protocol):
    def run(self) -> SecIssuerImportSummary:
        """Persist or reuse both SEC raw snapshots."""
        ...


class _ObservationPipeline(Protocol):
    def run(self) -> SecObservationImportSummary:
        """Normalize the latest compatible SEC snapshots."""
        ...


class _MetricPipeline(Protocol):
    def run(
        self,
        request: SecFundamentalMetricRequest,
    ) -> SecFundamentalMetricImportSummary:
        """Persist deterministic fundamental metrics."""
        ...


class _DiagnosticPipeline(Protocol):
    def run(
        self,
        request: SecFundamentalDiagnosticRequest,
    ) -> SecFundamentalDiagnosticRunSummary:
        """Persist one independent fundamental diagnostic."""
        ...


class SecIssuerFundamentalRefreshError(RuntimeError):
    """Base error for one issuer-only SEC refresh."""


class SecIssuerFundamentalRefreshStageError(SecIssuerFundamentalRefreshError):
    """Identify the failed stage while preserving earlier persisted progress."""

    def __init__(
        self,
        stage: SecIssuerFundamentalRefreshStage,
        cause: Exception,
    ) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage.value} failed: {cause}")


class SecIssuerFundamentalKnownAtTooEarlyError(SecIssuerFundamentalRefreshError):
    """Raised after ingestion when an explicit cut predates issuer evidence."""

    def __init__(
        self,
        *,
        requested_known_at: datetime,
        minimum_known_at: datetime,
    ) -> None:
        self.requested_known_at = requested_known_at
        self.minimum_known_at = minimum_known_at
        super().__init__(
            f"known_at {requested_known_at.isoformat()} predates SEC evidence available at "
            f"{minimum_known_at.isoformat()}"
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SecIssuerFundamentalRefreshPipeline:
    """Fetch, normalize, calculate, and diagnose one SEC issuer in order."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        configuration: SecAssetConfiguration,
        fetch_pipeline: _FetchPipeline,
        observation_pipeline: _ObservationPipeline,
        metric_pipeline: _MetricPipeline,
        diagnostic_pipeline: _DiagnosticPipeline,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._configuration = configuration
        self._fetch_pipeline = fetch_pipeline
        self._observation_pipeline = observation_pipeline
        self._metric_pipeline = metric_pipeline
        self._diagnostic_pipeline = diagnostic_pipeline
        self._clock = clock

    def run(
        self,
        request: SecIssuerFundamentalRefreshRequest,
    ) -> SecIssuerFundamentalRefreshSummary:
        """Execute all stages once without rolling back successful earlier stages."""
        self._storage.require_open()
        if request.asset_id != self._configuration.asset_id:
            raise SecIssuerFundamentalRefreshError(
                "request asset_id does not match the configured SEC issuer"
            )

        fetch = self._run_stage(
            SecIssuerFundamentalRefreshStage.SEC_FETCH,
            self._fetch_pipeline.run,
        )
        if (
            fetch.asset_id != self._configuration.asset_id
            or fetch.cik != self._configuration.cik
            or fetch.documents_received != 2
            or not fetch.traceability_verified
        ):
            raise SecIssuerFundamentalRefreshStageError(
                SecIssuerFundamentalRefreshStage.SEC_FETCH,
                ValueError("SEC fetch returned an invalid issuer context"),
            )

        observations = self._run_stage(
            SecIssuerFundamentalRefreshStage.SEC_NORMALIZATION,
            self._observation_pipeline.run,
        )
        if (
            observations.asset_id != self._configuration.asset_id
            or not observations.traceability_verified
        ):
            raise SecIssuerFundamentalRefreshStageError(
                SecIssuerFundamentalRefreshStage.SEC_NORMALIZATION,
                ValueError("SEC normalization returned an invalid issuer context"),
            )

        effective_known_at = self._resolve_known_at(request)
        metrics = self._run_stage(
            SecIssuerFundamentalRefreshStage.FUNDAMENTAL_METRICS,
            lambda: self._metric_pipeline.run(
                SecFundamentalMetricRequest(
                    asset_id=self._configuration.asset_id,
                    known_at=effective_known_at,
                    frequency=request.frequency,
                )
            ),
        )
        if (
            metrics.asset_id != self._configuration.asset_id
            or metrics.known_at != effective_known_at
            or metrics.frequency is not request.frequency
            or not metrics.traceability_verified
        ):
            raise SecIssuerFundamentalRefreshStageError(
                SecIssuerFundamentalRefreshStage.FUNDAMENTAL_METRICS,
                ValueError("fundamental metrics returned an invalid issuer context"),
            )

        diagnostic = self._run_stage(
            SecIssuerFundamentalRefreshStage.FUNDAMENTAL_DIAGNOSTIC,
            lambda: self._diagnostic_pipeline.run(
                SecFundamentalDiagnosticRequest(
                    asset_id=self._configuration.asset_id,
                    known_at=effective_known_at,
                    frequency=request.frequency,
                )
            ),
        )
        if (
            diagnostic.asset_id != self._configuration.asset_id
            or diagnostic.known_at != effective_known_at
            or diagnostic.frequency is not request.frequency
            or not diagnostic.traceability_verified
        ):
            raise SecIssuerFundamentalRefreshStageError(
                SecIssuerFundamentalRefreshStage.FUNDAMENTAL_DIAGNOSTIC,
                ValueError("fundamental diagnostic returned an invalid issuer context"),
            )

        return SecIssuerFundamentalRefreshSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.companyfacts_source_id,
            request=request,
            effective_known_at=effective_known_at,
            fetched_at=fetch.retrieved_at,
            normalized_at=observations.normalized_at,
            documents_received=fetch.documents_received,
            raw_records_created=fetch.raw_records_created,
            raw_records_reused=fetch.raw_records_reused,
            facts_examined=observations.facts_examined,
            facts_selected=observations.facts_selected,
            observations_generated=observations.observations_generated,
            observations_created=observations.observations_created,
            observations_reused=observations.observations_reused,
            annual_observations=observations.annual_count,
            quarterly_observations=observations.quarterly_count,
            observation_field_counts=dict(observations.field_counts),
            observation_skipped_counts=dict(observations.skipped_counts),
            target_periods=metrics.target_periods,
            metric_results_created=metrics.metrics_created,
            metric_results_reused=metrics.metrics_reused,
            metric_counts=metrics.metric_counts,
            metric_skipped_counts=metrics.skipped_counts,
            diagnostic_target_period_end=diagnostic.target_period_end,
            diagnostic_verdict=diagnostic.verdict,
            diagnostic_coverage=diagnostic.coverage,
            diagnostic_missing_requirements=diagnostic.missing_requirements,
            diagnostics_created=diagnostic.diagnostics_created,
            diagnostics_reused=diagnostic.diagnostics_reused,
            traceability_verified=True,
        )

    def _resolve_known_at(
        self,
        request: SecIssuerFundamentalRefreshRequest,
    ) -> datetime:
        transformation_version = sec_transformation_version(self._configuration)
        available = tuple(
            item.available_at
            for item in self._storage.observations.list(asset_id=self._configuration.asset_id)
            if item.source.source_id == self._configuration.companyfacts_source_id
            and item.frequency is request.frequency
            and item.quality is DataQuality.VALID
            and item.transformation_version == transformation_version
        )
        minimum_known_at = min(available, default=None)
        effective = (
            request.requested_known_at
            if request.requested_known_at is not None
            else self._normalized_now()
        )
        if minimum_known_at is not None and effective < minimum_known_at:
            raise SecIssuerFundamentalKnownAtTooEarlyError(
                requested_known_at=effective,
                minimum_known_at=minimum_known_at,
            )
        return effective

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecIssuerFundamentalRefreshStageError(
                SecIssuerFundamentalRefreshStage.KNOWN_AT_RESOLUTION,
                ValueError("clock must return a timezone-aware datetime"),
            )
        return value.astimezone(UTC)

    @staticmethod
    def _run_stage(
        stage: SecIssuerFundamentalRefreshStage,
        operation: Callable[[], _StageResult],
    ) -> _StageResult:
        try:
            return operation()
        except (RuntimeError, ValueError) as error:
            raise SecIssuerFundamentalRefreshStageError(stage, error) from error


__all__ = [
    "SecIssuerFundamentalKnownAtTooEarlyError",
    "SecIssuerFundamentalRefreshError",
    "SecIssuerFundamentalRefreshPipeline",
    "SecIssuerFundamentalRefreshStageError",
]
