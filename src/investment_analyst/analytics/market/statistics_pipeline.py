"""Persistence pipeline for point-in-time historical market statistics."""

import json
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID, uuid5

from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_definitions import (
    get_market_statistics_definitions,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsRequest,
    MarketStatisticsRunSummary,
    MetricCalculation,
)
from investment_analyst.core.models import DataQuality, MetricResult, NormalizedObservation
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError

_RESULT_NAMESPACE = UUID("b5022527-e62b-5cb5-914e-3f67eedeb9be")


class MarketStatisticsPipelineError(RuntimeError):
    """Base error for statistics persistence and traceability checks."""


class MetricIdentityConflictError(MarketStatisticsPipelineError):
    """Raised when a deterministic metric ID maps to different analytical content."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_identity(calculation: MetricCalculation, known_at: datetime) -> str:
    document = {
        "asset_id": calculation.asset_id,
        "source_id": calculation.source_id,
        "metric_key": calculation.metric_key,
        "as_of": calculation.as_of.isoformat(),
        "available_at": calculation.available_at.isoformat(),
        "known_at": known_at.isoformat(),
        "parameters": calculation.parameters,
        "input_observation_ids": [str(item) for item in calculation.input_observation_ids],
        "algorithm_version": calculation.algorithm_version,
        "value": str(calculation.value),
        "unit": calculation.unit,
        "quality": calculation.quality.value,
    }
    return json.dumps(document, allow_nan=False, separators=(",", ":"), sort_keys=True)


def metric_result_id(calculation: MetricCalculation, known_at: datetime) -> UUID:
    """Return the stable UUID5 identity for one point-in-time calculation."""
    return uuid5(_RESULT_NAMESPACE, _canonical_identity(calculation, known_at))


def _quality(observations: tuple[NormalizedObservation, ...]) -> DataQuality:
    precedence = (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    )
    qualities = {item.quality for item in observations}
    for candidate in precedence:
        if candidate in qualities:
            return candidate
    raise MarketStatisticsPipelineError("result has no input observations")


class MarketStatisticsPipeline:
    """Query bars, compute deterministic statistics, and persist auditable results."""

    def __init__(
        self,
        storage: LocalStorage,
        history_service: HistoricalMarketDataService,
        engine: MarketStatisticsEngine,
        *,
        clock=_utc_now,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._history_service = history_service
        self._engine = engine
        self._clock = clock

    def run(self, request: MarketStatisticsRequest) -> MarketStatisticsRunSummary:
        """Execute one idempotent point-in-time statistics run."""
        self._storage.require_open()
        raw_count_before = len(self._storage.raw_records.list())
        observation_count_before = len(self._storage.observations.list())
        diagnostic_count_before = len(self._storage.diagnostics.list())

        series = self._history_service.query(request.query)
        if series.query != request.query:
            raise MarketStatisticsPipelineError("history service returned a different query")

        definitions = get_market_statistics_definitions()
        for definition in definitions:
            self._storage.metric_definitions.upsert(definition)

        computation = self._engine.compute(series, request)
        computed_at = self._clock()
        if computed_at.tzinfo is None or computed_at.utcoffset() is None:
            raise MarketStatisticsPipelineError("clock must return a timezone-aware datetime")
        computed_at = computed_at.astimezone(UTC)

        created = 0
        reused = 0
        stored_results: list[MetricResult] = []
        for calculation in computation.calculations:
            if calculation.available_at > computed_at:
                raise MarketStatisticsPipelineError(
                    "computed_at must not be earlier than result availability"
                )
            identifier = metric_result_id(calculation, request.query.known_at)
            try:
                existing = self._storage.metric_results.get(identifier)
            except RecordNotFoundError:
                result = self._to_result(calculation, identifier, computed_at)
                self._storage.metric_results.save(result)
                stored_results.append(self._storage.metric_results.get(identifier))
                created += 1
            else:
                self._verify_identity(existing, calculation)
                stored_results.append(existing)
                reused += 1

        self._verify_run(
            request,
            stored_results,
            raw_count_before,
            observation_count_before,
            diagnostic_count_before,
        )
        counts = Counter(result.metric_key for result in stored_results)
        as_of_values = [result.as_of for result in stored_results]
        return MarketStatisticsRunSummary(
            asset_id=request.query.asset_id,
            source_id=request.query.source_id,
            requested_start=request.query.start,
            requested_end=request.query.end,
            known_at=request.query.known_at,
            computed_at=computed_at,
            bar_count=computation.bar_count,
            definitions_upserted=len(definitions),
            results_generated=len(stored_results),
            results_created=created,
            results_reused=reused,
            result_counts=dict(sorted(counts.items())),
            warmup_counts=computation.warmup_counts,
            zero_denominator_skips=computation.zero_denominator_skips,
            earliest_as_of=min(as_of_values) if as_of_values else None,
            latest_as_of=max(as_of_values) if as_of_values else None,
            traceability_verified=True,
        )

    @staticmethod
    def _to_result(
        calculation: MetricCalculation,
        identifier: UUID,
        computed_at: datetime,
    ) -> MetricResult:
        return MetricResult(
            result_id=identifier,
            asset_id=calculation.asset_id,
            metric_key=calculation.metric_key,
            value=calculation.value,
            unit=calculation.unit,
            as_of=calculation.as_of,
            available_at=calculation.available_at,
            computed_at=computed_at,
            parameters=dict(calculation.parameters),
            input_observation_ids=list(calculation.input_observation_ids),
            algorithm_version=calculation.algorithm_version,
            quality=calculation.quality,
        )

    @staticmethod
    def _verify_identity(result: MetricResult, calculation: MetricCalculation) -> None:
        expected = {
            "asset_id": calculation.asset_id,
            "metric_key": calculation.metric_key,
            "value": calculation.value,
            "unit": calculation.unit,
            "as_of": calculation.as_of,
            "available_at": calculation.available_at,
            "parameters": calculation.parameters,
            "input_observation_ids": list(calculation.input_observation_ids),
            "algorithm_version": calculation.algorithm_version,
            "quality": calculation.quality,
        }
        actual = {name: getattr(result, name) for name in expected}
        if actual != expected:
            raise MetricIdentityConflictError(
                f"metric result {result.result_id} conflicts with its deterministic identity"
            )

    def _verify_run(
        self,
        request: MarketStatisticsRequest,
        results: list[MetricResult],
        raw_count_before: int,
        observation_count_before: int,
        diagnostic_count_before: int,
    ) -> None:
        definition_keys = {item.metric_key for item in get_market_statistics_definitions()}
        for result in results:
            if result.metric_key not in definition_keys:
                raise MarketStatisticsPipelineError("result has no supported metric definition")
            definition = self._storage.metric_definitions.get(result.metric_key)
            if definition.metric_key != result.metric_key:
                raise MarketStatisticsPipelineError("stored metric definition cannot be verified")
            observations = tuple(
                self._storage.observations.get(identifier)
                for identifier in result.input_observation_ids
            )
            if any(item.asset_id != request.query.asset_id for item in observations):
                raise MarketStatisticsPipelineError("result mixes assets")
            if any(item.source.source_id != request.query.source_id for item in observations):
                raise MarketStatisticsPipelineError("result mixes sources")
            if any(item.available_at > request.query.known_at for item in observations):
                raise MarketStatisticsPipelineError(
                    "result uses information unavailable at known_at"
                )
            if max(item.available_at for item in observations) != result.available_at:
                raise MarketStatisticsPipelineError("result available_at does not match its inputs")
            observed_times = [item.observed_at for item in observations if item.observed_at]
            if not observed_times or max(observed_times) != result.as_of:
                raise MarketStatisticsPipelineError(
                    "result as_of does not match its final input bar"
                )
            if _quality(observations) is not result.quality:
                raise MarketStatisticsPipelineError("result quality does not match its inputs")
            if self._storage.metric_results.get(result.result_id) != result:
                raise MarketStatisticsPipelineError("stored metric result round-trip failed")
            for timestamp in (result.as_of, result.available_at, result.computed_at):
                if timestamp.tzinfo is not UTC:
                    raise MarketStatisticsPipelineError(
                        "result timestamps must be normalized to UTC"
                    )

        try:
            if len(self._storage.raw_records.list()) != raw_count_before:
                raise MarketStatisticsPipelineError("statistics pipeline created raw records")
            if len(self._storage.observations.list()) != observation_count_before:
                raise MarketStatisticsPipelineError("statistics pipeline created observations")
            if len(self._storage.diagnostics.list()) != diagnostic_count_before:
                raise MarketStatisticsPipelineError("statistics pipeline created diagnostics")
        except StorageError as error:
            raise MarketStatisticsPipelineError("storage counts could not be verified") from error
