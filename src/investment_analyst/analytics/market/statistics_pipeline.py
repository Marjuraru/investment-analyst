"""Persistence pipeline for point-in-time historical market statistics."""

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_definitions import (
    ATR_KEY,
    EMA_KEY,
    MACD_HISTOGRAM_KEY,
    MACD_LINE_KEY,
    MACD_SIGNAL_KEY,
    RSI_AVERAGE_GAIN_KEY,
    RSI_AVERAGE_LOSS_KEY,
    RSI_KEY,
    get_market_statistics_definitions,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_identity import semantic_metric_result_id
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsRequest,
    MarketStatisticsRunSummary,
    MetricCalculation,
)
from investment_analyst.analytics.metric_identity_cut import (
    CutIdentityVersion,
    resolve_cut_identity_version,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.core.operation_control import check_operation_cancelled
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError


class MarketStatisticsPipelineError(RuntimeError):
    """Base error for statistics persistence and traceability checks."""


class MetricIdentityConflictError(MarketStatisticsPipelineError):
    """Raised when a deterministic metric ID maps to different analytical content."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quality(values: tuple[DataQuality, ...]) -> DataQuality:
    precedence = (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    )
    for candidate in precedence:
        if candidate in values:
            return candidate
    raise MarketStatisticsPipelineError("result has no input quality")


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
        check_operation_cancelled()

        series = self._history_service.query(request.query)
        check_operation_cancelled()
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

        ordered_calculations = self._topologically_order(computation.calculations)
        for calculation in ordered_calculations:
            if calculation.available_at > computed_at:
                raise MarketStatisticsPipelineError(
                    "computed_at must not be earlier than result availability"
                )

        calc_entries = [
            (calculation, semantic_metric_result_id(calculation))
            for calculation in ordered_calculations
        ]
        identifiers = {identifier for _, identifier in calc_entries}
        existing_map = self._storage.metric_results.get_existing(identifiers)

        created = 0
        reused = 0
        to_save: list[MetricResult] = []
        stored_results: list[MetricResult] = []

        for calculation, identifier in calc_entries:
            check_operation_cancelled()
            existing = existing_map.get(identifier)
            if existing is not None:
                self._verify_identity(existing, calculation)
                stored_results.append(existing)
                reused += 1
            else:
                result = self._to_result(calculation, identifier, computed_at)
                to_save.append(result)
                stored_results.append(result)
                created += 1

        if to_save:
            receipt = self._storage.metric_results.save_many(to_save)
        else:
            receipt = BatchWriteReceipt()

        check_operation_cancelled()
        self._verify_run(
            request,
            stored_results,
            receipt,
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
            input_metric_result_ids=list(calculation.input_metric_result_ids),
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
            "input_metric_result_ids": list(calculation.input_metric_result_ids),
            "algorithm_version": calculation.algorithm_version,
            "quality": calculation.quality,
        }
        actual = {name: getattr(result, name) for name in expected}
        if actual != expected:
            raise MetricIdentityConflictError(
                f"metric result {result.result_id} conflicts with its deterministic identity"
            )

    def _topologically_order(
        self,
        calculations: tuple[MetricCalculation, ...],
    ) -> tuple[MetricCalculation, ...]:
        """Order generated derived metrics after their same-run dependencies."""
        entries = tuple(
            (calculation, semantic_metric_result_id(calculation)) for calculation in calculations
        )
        identifiers = [identifier for _, identifier in entries]
        if len(set(identifiers)) != len(identifiers):
            raise MarketStatisticsPipelineError("calculation identities must be unique")
        generated_ids = set(identifiers)
        for calculation, identifier in entries:
            if identifier in calculation.input_metric_result_ids:
                raise MarketStatisticsPipelineError("metric result cannot depend on itself")
        external_dep_ids = {
            dependency_id
            for calculation, _ in entries
            for dependency_id in calculation.input_metric_result_ids
            if dependency_id not in generated_ids
        }
        if external_dep_ids:
            try:
                self._storage.metric_results.get_many(external_dep_ids)
            except RecordNotFoundError as error:
                raise MarketStatisticsPipelineError(
                    "derived metric dependency is missing"
                ) from error

        pending = list(entries)
        established: set[UUID] = set()
        ordered: list[MetricCalculation] = []
        while pending:
            ready = [
                entry
                for entry in pending
                if all(
                    dependency_id not in generated_ids or dependency_id in established
                    for dependency_id in entry[0].input_metric_result_ids
                )
            ]
            if not ready:
                raise MarketStatisticsPipelineError("derived metric dependencies contain a cycle")
            for calculation, identifier in ready:
                ordered.append(calculation)
                established.add(identifier)
            ready_ids = {identifier for _, identifier in ready}
            pending = [entry for entry in pending if entry[1] not in ready_ids]
        return tuple(ordered)

    def _verify_run(
        self,
        request: MarketStatisticsRequest,
        results: list[MetricResult],
        receipt: BatchWriteReceipt,
    ) -> None:
        new_ids = set(receipt.created_ids)
        if not new_ids:
            return

        new_results = [result for result in results if result.result_id in new_ids]
        definition_keys = {item.metric_key for item in get_market_statistics_definitions()}
        for result in new_results:
            if result.metric_key not in definition_keys:
                raise MarketStatisticsPipelineError("result has no supported metric definition")
            definition = self._storage.metric_definitions.get(result.metric_key)
            if definition.metric_key != result.metric_key:
                raise MarketStatisticsPipelineError("stored metric definition cannot be verified")
            if result.parameters.get("source_id") != request.query.source_id:
                raise MarketStatisticsPipelineError(
                    "result source parameter does not match request"
                )
            if resolve_cut_identity_version(result) is not CutIdentityVersion.V2:
                raise MarketStatisticsPipelineError(
                    "new market result does not carry a semantic v2 identity"
                )
            if "known_at" in result.parameters:
                raise MarketStatisticsPipelineError(
                    "v2 market result must not carry the legacy known_at parameter"
                )

        needed_observation_ids = {
            obs_id for result in new_results for obs_id in result.input_observation_ids
        }
        observations_map = (
            self._storage.observations.get_many(needed_observation_ids)
            if needed_observation_ids
            else {}
        )

        needed_dependency_ids = {
            dep_id for result in new_results for dep_id in result.input_metric_result_ids
        }
        stored_map = {result.result_id: result for result in results}
        external_dep_ids = needed_dependency_ids - set(stored_map.keys())
        external_deps = (
            self._storage.metric_results.get_many(external_dep_ids) if external_dep_ids else {}
        )
        dependencies_map = {**external_deps, **stored_map}

        memoized_verified: set[UUID] = set()

        for result in new_results:
            observations = tuple(
                observations_map[identifier] for identifier in result.input_observation_ids
            )
            dependencies = tuple(
                dependencies_map[identifier] for identifier in result.input_metric_result_ids
            )
            self._verify_derived_dependencies(
                request,
                result,
                dependencies,
                memoized_verified,
                dependencies_map,
            )
            if any(item.asset_id != request.query.asset_id for item in observations):
                raise MarketStatisticsPipelineError("result mixes assets")
            if any(item.source.source_id != request.query.source_id for item in observations):
                raise MarketStatisticsPipelineError("result mixes sources")
            if any(item.available_at > request.query.known_at for item in observations):
                raise MarketStatisticsPipelineError(
                    "result uses information unavailable at known_at"
                )
            if any(item.available_at > request.query.known_at for item in dependencies):
                raise MarketStatisticsPipelineError(
                    "result uses derived information unavailable at known_at"
                )
            availability_inputs = tuple(item.available_at for item in observations) + tuple(
                item.available_at for item in dependencies
            )
            if max(availability_inputs) != result.available_at:
                raise MarketStatisticsPipelineError("result available_at does not match its inputs")
            observed_times = [item.observed_at for item in observations if item.observed_at]
            if not observed_times or max(observed_times) != result.as_of:
                raise MarketStatisticsPipelineError(
                    "result as_of does not match its final input bar"
                )
            qualities = tuple(item.quality for item in observations) + tuple(
                item.quality for item in dependencies
            )
            if _quality(qualities) is not result.quality:
                raise MarketStatisticsPipelineError("result quality does not match its inputs")
            for timestamp in (result.as_of, result.available_at, result.computed_at):
                if timestamp.tzinfo is not UTC:
                    raise MarketStatisticsPipelineError(
                        "result timestamps must be normalized to UTC"
                    )

    def _verify_derived_dependencies(
        self,
        request: MarketStatisticsRequest,
        result: MetricResult,
        dependencies: tuple[MetricResult, ...],
        memoized_verified: set[UUID],
        dependencies_map: dict[UUID, MetricResult],
    ) -> None:
        """Prove derived lineage is a compatible, strictly prior EMA chain."""
        if result.result_id in result.input_metric_result_ids:
            raise MarketStatisticsPipelineError("metric result cannot depend on itself")
        if not dependencies:
            return
        allowed = {
            EMA_KEY,
            RSI_AVERAGE_GAIN_KEY,
            RSI_AVERAGE_LOSS_KEY,
            RSI_KEY,
            MACD_LINE_KEY,
            MACD_SIGNAL_KEY,
            MACD_HISTOGRAM_KEY,
            ATR_KEY,
        }
        if result.metric_key not in allowed:
            raise MarketStatisticsPipelineError("metric does not support derived dependencies")
        if result.metric_key == EMA_KEY and len(dependencies) != 1:
            raise MarketStatisticsPipelineError("EMA requires one prior dependency")
        dependency = dependencies[0]
        if dependency.asset_id != result.asset_id or dependency.asset_id != request.query.asset_id:
            raise MarketStatisticsPipelineError("derived metric dependency mixes assets")
        if result.metric_key == EMA_KEY:
            if (
                dependency.metric_key != EMA_KEY
                or dependency.algorithm_version != result.algorithm_version
            ):
                raise MarketStatisticsPipelineError("EMA derived dependency is incompatible")
            for parameter in (
                "source_id",
                "window",
                "alpha",
                "seed_method",
                "seed_start",
            ):
                if dependency.parameters.get(parameter) != result.parameters.get(parameter):
                    raise MarketStatisticsPipelineError(
                        "derived metric dependency has incompatible parameters"
                    )
        if dependency.parameters.get("source_id") != request.query.source_id:
            raise MarketStatisticsPipelineError("derived metric dependency mixes sources")
        if resolve_cut_identity_version(dependency) is not CutIdentityVersion.V2:
            raise MarketStatisticsPipelineError(
                "derived metric dependency does not carry a semantic v2 identity"
            )
        same_time = {
            ATR_KEY,
            RSI_KEY,
            MACD_LINE_KEY,
            MACD_SIGNAL_KEY,
            MACD_HISTOGRAM_KEY,
        }
        if result.metric_key not in same_time and dependency.as_of >= result.as_of:
            raise MarketStatisticsPipelineError("derived metric dependency is not strictly prior")
        self._verify_derived_graph(
            result.result_id,
            set(),
            memoized_verified,
            dependencies_map,
        )

    def _verify_derived_graph(
        self,
        identifier: UUID,
        active: set[UUID],
        verified: set[UUID],
        dependencies_map: dict[UUID, MetricResult],
    ) -> None:
        if identifier in active:
            raise MarketStatisticsPipelineError("derived metric dependencies contain a cycle")
        if identifier in verified:
            return
        active.add(identifier)
        if identifier in dependencies_map:
            metric = dependencies_map[identifier]
        else:
            metric = self._storage.metric_results.get(identifier)
            dependencies_map[identifier] = metric
        for dependency_id in metric.input_metric_result_ids:
            self._verify_derived_graph(dependency_id, active, verified, dependencies_map)
        active.remove(identifier)
        verified.add(identifier)
