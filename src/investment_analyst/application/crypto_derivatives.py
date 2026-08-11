"""Application orchestration for ordered Deribit refresh stages."""

from collections.abc import Callable
from datetime import UTC, datetime

from investment_analyst.analytics.crypto.derivatives_engine import CryptoDerivativesMetricEngine
from investment_analyst.analytics.crypto.derivatives_pipeline import (
    CryptoDerivativesMetricPipeline,
)
from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesMetricStageSummary,
    CryptoDerivativesRefreshRequest,
    CryptoDerivativesRefreshSummary,
    public_date_bounds,
)
from investment_analyst.application.crypto_derivatives_planner import (
    CryptoDerivativesPlanner,
)
from investment_analyst.providers.asset_config import DeribitAssetConfiguration
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_pipeline import DeribitEvidencePipeline
from investment_analyst.storage import LocalStorage


class CryptoDerivativesRefreshError(RuntimeError):
    """Failure while executing one ordered derivatives refresh."""


class CryptoDerivativesRefreshService:
    """Run funding, DVOL, summary, then metrics using one open writer."""

    def __init__(
        self,
        storage: LocalStorage,
        client: DeribitClient,
        *,
        configuration: DeribitAssetConfiguration,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._configuration = configuration
        self._clock = clock
        self._evidence = DeribitEvidencePipeline(
            storage,
            client,
            configuration=configuration,
            clock=clock,
        )
        self._planner = CryptoDerivativesPlanner(
            storage,
            configuration=configuration,
        )
        self._metrics = CryptoDerivativesMetricPipeline(
            storage,
            CryptoDerivativesMetricEngine(),
            clock=clock,
        )

    def run(
        self,
        request: CryptoDerivativesRefreshRequest,
    ) -> CryptoDerivativesRefreshSummary:
        """Execute all authorized stages without rolling back completed earlier stages."""
        if request.asset_id != self._configuration.asset_id:
            raise CryptoDerivativesRefreshError(
                "refresh request does not match the resolved Deribit asset"
            )
        start, end = public_date_bounds(request.start_date, request.end_date)
        plan = self._planner.plan(start, end, refresh_mode=request.refresh_mode)
        funding_stages = tuple(
            self._evidence.import_funding(interval.start, interval.end)
            for interval in plan.funding.intervals
        )
        dvol_stages = tuple(
            self._evidence.import_dvol(interval.start, interval.end)
            for interval in plan.dvol.intervals
        )
        summary_stage = self._evidence.capture_summary()
        retrievals = (
            *(stage.retrieved_at for stage in funding_stages),
            *(stage.retrieved_at for stage in dvol_stages),
            summary_stage.retrieved_at,
        )
        requested_known_at = request.known_at or _utc(self._clock())
        effective_known_at = max(requested_known_at, *retrievals)
        metric_result = self._metrics.run(
            asset_id=self._configuration.asset_id,
            funding_source_id=self._configuration.funding_source_id,
            dvol_source_id=self._configuration.dvol_source_id,
            summary_source_id=self._configuration.summary_source_id,
            known_at=effective_known_at,
            as_of_from=start,
            as_of_before=max(end, effective_known_at),
        )
        metric_stage = CryptoDerivativesMetricStageSummary(
            results_created=metric_result.results_created,
            results_reused=metric_result.results_reused,
            missing_requirements=metric_result.missing_requirements,
            traceability_verified=metric_result.traceability_verified,
        )
        return CryptoDerivativesRefreshSummary(
            asset_id=self._configuration.asset_id,
            source_ids=tuple(
                sorted(
                    (
                        self._configuration.funding_source_id,
                        self._configuration.dvol_source_id,
                        self._configuration.summary_source_id,
                    )
                )
            ),
            requested_start=start,
            requested_end=end,
            effective_known_at=effective_known_at,
            plan=plan,
            funding_stages=funding_stages,
            dvol_stages=dvol_stages,
            summary_stage=summary_stage,
            metric_stage=metric_stage,
            traceability_verified=(
                all(stage.traceability_verified for stage in funding_stages)
                and all(stage.traceability_verified for stage in dvol_stages)
                and summary_stage.traceability_verified
                and metric_stage.traceability_verified
            ),
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoDerivativesRefreshError("refresh clock must include timezone information")
    return value.astimezone(UTC)


__all__ = ["CryptoDerivativesRefreshError", "CryptoDerivativesRefreshService"]
