"""Integration coverage for the market write-path adoption of metric identity v2."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investment_analyst.alerts.analytical_backtest import (
    AnalyticalBacktestRequest,
    AnalyticalBacktestService,
)
from investment_analyst.alerts.analytical_engine import AnalyticalScreeningEngine
from investment_analyst.alerts.analytical_models import AnalyticalScreeningRequest
from investment_analyst.alerts.analytical_monitor import AnalyticalScreeningMonitor
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    INITIAL_MARKET_ACTIVITY_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import AnalyticalRuleRegistryStore
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateStatus,
    AnalyticalMonitorReceipt,
    AnalyticalMonitorReceiptStatus,
    AnalyticalScreeningStateStore,
)
from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import COINBASE_SOURCE_ID
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_definitions import (
    BOLLINGER_UPPER_KEY,
    EMA_KEY,
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_identity import (
    metric_result_id,
    semantic_metric_result_id,
)
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsComputation,
    MarketStatisticsRequest,
    MetricCalculation,
)
from investment_analyst.analytics.market.statistics_pipeline import (
    MarketStatisticsPipeline,
    MarketStatisticsPipelineError,
    MetricIdentityConflictError,
)
from investment_analyst.analytics.metric_identity_cut import (
    CutIdentityVersion,
    resolve_cut_identity_version,
)
from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DataQuality,
    DiagnosticVerdict,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.serialization import canonical_json_text
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "crypto:btc-usd"
_FINITE_WINDOW_KEYS = frozenset(
    {
        SIMPLE_RETURN_KEY,
        SMA_KEY,
        VOLATILITY_KEY,
        RELATIVE_VOLUME_KEY,
        BOLLINGER_UPPER_KEY,
    }
)


class _StubEngine:
    """Engine double returning one prepared computation without recomputing bars."""

    def __init__(self, computation: MarketStatisticsComputation) -> None:
        self._computation = computation

    def compute(self, series, request: MarketStatisticsRequest) -> MarketStatisticsComputation:
        return self._computation


def _workspace(tmp_path: Path) -> tuple[WorkspaceService, object]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    paths = service.initialize(tmp_path / "workspace").paths
    return service, paths


def _store_coinbase(storage: LocalStorage, count: int) -> tuple[datetime, datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        retrieved = timestamp + timedelta(hours=1)
        close = Decimal("100") + Decimal(index)
        candle = CoinbaseCandle(
            product_id="BTC-USD",
            start=timestamp,
            low=close - 2,
            high=close + 2,
            open=close - 1,
            close=close,
            volume=Decimal("100") + Decimal(index * 10),
            raw_values=(
                str(int(timestamp.timestamp())),
                str(close - 2),
                str(close + 2),
                str(close - 1),
                str(close),
                str(Decimal("100") + Decimal(index * 10)),
            ),
        )
        raw = candle_to_raw_record(
            candle,
            retrieved_at=retrieved,
            request_url="https://api.exchange.coinbase.com/test",
        )
        storage.raw_records.save(raw)
        for observation in candle_to_observations(
            candle,
            raw,
            normalized_at=retrieved + timedelta(minutes=1),
        ):
            storage.observations.save(observation)
    return start, start + timedelta(days=count)


def _statistics_request(start: datetime, end: datetime, known_at: datetime):
    return MarketStatisticsRequest(
        query=HistoricalBarQuery(
            asset_id=_ASSET_ID,
            source_id=COINBASE_SOURCE_ID,
            start=start,
            end=end,
            known_at=known_at,
        ),
        sma_windows=(2, 3),
        volatility_window=2,
        relative_volume_window=20,
        bollinger_window=2,
        ema_windows=(2,),
    )


def _diagnostic_request(start: datetime, end: datetime, known_at: datetime):
    return MarketDiagnosticRequest(
        query=HistoricalBarQuery(
            asset_id=_ASSET_ID,
            source_id=COINBASE_SOURCE_ID,
            start=start,
            end=end,
            known_at=known_at,
        ),
        short_sma_window=2,
        long_sma_window=3,
        volatility_window=2,
        relative_volume_window=20,
    )


def _pipeline(storage: LocalStorage, history, clock: datetime) -> MarketStatisticsPipeline:
    return MarketStatisticsPipeline(storage, history, MarketStatisticsEngine(), clock=lambda: clock)


def _rows(storage: LocalStorage) -> dict[UUID, MetricResult]:
    return {item.result_id: item for item in storage.metric_results.list(asset_id=_ASSET_ID)}


def _late_observation(storage: LocalStorage, *, known_at: datetime) -> NormalizedObservation:
    available_at = known_at + timedelta(hours=1)
    reference = SourceReference(
        source_id=COINBASE_SOURCE_ID,
        record_key=f"late:{known_at.isoformat()}",
        retrieved_at=available_at,
    )
    raw = RawRecord(
        record_id=uuid4(),
        asset_id=_ASSET_ID,
        source=reference,
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload={"close": "100"},
        schema_version="adoption-v2-test-v1",
    )
    storage.raw_records.save(raw)
    observation = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw.record_id,
        asset_id=_ASSET_ID,
        field_name="close",
        value=Decimal("100"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=available_at,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=reference,
        quality=DataQuality.VALID,
        transformation_version="adoption-v2-test-v1",
    )
    storage.observations.save(observation)
    return observation


def _stored_result(
    calculation: MetricCalculation,
    identifier: UUID,
    *,
    computed_at: datetime,
    parameters: dict[str, object] | None = None,
    available_at: datetime | None = None,
) -> MetricResult:
    return MetricResult(
        result_id=identifier,
        asset_id=calculation.asset_id,
        metric_key=calculation.metric_key,
        value=calculation.value,
        unit=calculation.unit,
        as_of=calculation.as_of,
        available_at=available_at or calculation.available_at,
        computed_at=computed_at,
        parameters=parameters or dict(calculation.parameters),
        input_observation_ids=list(calculation.input_observation_ids),
        input_metric_result_ids=list(calculation.input_metric_result_ids),
        algorithm_version=calculation.algorithm_version,
        quality=calculation.quality,
    )


def test_new_known_at_without_new_bars_creates_zero_finite_window_metrics(tmp_path) -> None:
    """A5: a later cut without new evidence reuses every finite-window metric row."""
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage, count=25)
        history = HistoricalMarketDataService(storage)
        first = _pipeline(storage, history, clock).run(_statistics_request(start, end, clock))
        before = _rows(storage)

        assert first.results_created == first.results_generated > 0
        assert {item.metric_key for item in before.values()} >= _FINITE_WINDOW_KEYS
        assert all(item.result_id.version == 8 for item in before.values())
        assert all(item.parameters.get("known_at") is None for item in before.values())

        later_cut = clock + timedelta(days=1)
        second = _pipeline(storage, history, clock + timedelta(days=2)).run(
            _statistics_request(start, end, later_cut)
        )
        after = _rows(storage)
        created = {
            item.metric_key for identifier, item in after.items() if identifier not in before
        }

        assert created & _FINITE_WINDOW_KEYS == set()
        assert second.results_created == 0
        assert second.results_reused == second.results_generated == first.results_generated
        assert set(after) == set(before)


def test_window_shift_still_creates_recursive_rows_by_declared_limit(tmp_path) -> None:
    """Declared limit: a moved analytics window re-seeds recursive chains but not windows."""
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage, count=25)
        history = HistoricalMarketDataService(storage)
        _pipeline(storage, history, clock).run(_statistics_request(start, end, clock))
        before = _rows(storage)

        shifted = _pipeline(storage, history, clock).run(
            _statistics_request(start + timedelta(days=1), end, clock)
        )
        after = _rows(storage)
        created = {
            item.metric_key for identifier, item in after.items() if identifier not in before
        }

        assert created & _FINITE_WINDOW_KEYS == set()
        assert EMA_KEY in created
        assert shifted.results_created > 0


def test_a_different_value_for_the_same_v2_coordinate_is_a_conflict(tmp_path) -> None:
    """A6: a rewritten value under the same semantic coordinate is a conflict, not a revision."""
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage, count=6)
        history = HistoricalMarketDataService(storage)
        pipeline = _pipeline(storage, history, clock)
        request = _statistics_request(start, end, clock)
        pipeline.run(request)

        target = next(
            item for item in _rows(storage).values() if item.metric_key == SIMPLE_RETURN_KEY
        )
        assert resolve_cut_identity_version(target) is CutIdentityVersion.V2

        corrupted = target.model_copy(update={"value": Decimal("999999.99")})
        storage.metric_results._connection.execute(
            "UPDATE metric_results SET document_json = ? WHERE result_id = ?",
            [canonical_json_text(corrupted), str(corrupted.result_id)],
        )

        with pytest.raises(
            MetricIdentityConflictError,
            match="conflicts with its deterministic identity",
        ):
            pipeline.run(request)


def test_diagnostic_monitor_and_backtest_read_written_v2_rows(tmp_path) -> None:
    """A7: the readers consume the rows the write path now emits."""
    service, paths = _workspace(tmp_path)
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    writer = service.open_storage(paths, WorkspaceAccessMode.READ_WRITE)
    try:
        start, end = _store_coinbase(writer, count=25)
        summary = _pipeline(writer, HistoricalMarketDataService(writer), clock).run(
            _statistics_request(start, end, clock)
        )
        stored = _rows(writer)
    finally:
        writer.close()

    assert summary.results_created == summary.results_generated > 0
    assert all(item.result_id.version == 8 for item in stored.values())
    volume_ids = {
        item.result_id for item in stored.values() if item.metric_key == RELATIVE_VOLUME_KEY
    }
    assert volume_ids

    runtime = ApplicationRuntime.create_default(workspace_service=service)
    diagnostic_writer = service.open_storage(paths, WorkspaceAccessMode.READ_WRITE)
    try:
        diagnostic = MarketDiagnosticPipeline(
            diagnostic_writer,
            MarketDiagnosticMetricSelector(diagnostic_writer),
            MarketDiagnosticEngine(),
            clock=lambda: clock,
        ).run(_diagnostic_request(start, end, clock))
    finally:
        diagnostic_writer.close()

    assert diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA
    assert set(diagnostic.selected_metric_result_ids) <= set(stored)
    assert all(identifier.version == 8 for identifier in diagnostic.selected_metric_result_ids)

    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"confirmations_required": 1})
    AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (rule,),
        clock=lambda: clock + timedelta(minutes=5),
    )(_attempt(attempt_id=uuid4(), known_at=clock))
    state = store.load()

    assert [item.status for item in state.receipts] == [AnalyticalMonitorReceiptStatus.SCREENED]
    assert len(state.results) == 1
    assert state.results[0].conditions[0].metric_result_id in volume_ids

    registry = AnalyticalRuleRegistryStore(
        paths.state_root / "rules.json",
        INITIAL_ANALYTICAL_RULES,
    )
    replayed = AnalyticalBacktestService(runtime, paths.root, registry).run(
        AnalyticalBacktestRequest(
            rule_id=INITIAL_MARKET_ACTIVITY_RULE.rule_id,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert replayed.total_available_cuts > 0
    assert replayed.evaluations
    assert replayed.evaluations[0].result.conditions[0].metric_result_id in volume_ids


def test_identity_switch_opens_no_duplicate_alert_candidate(tmp_path) -> None:
    """A9: the candidate stream deduplicates by rule, asset and source, not by metric identity."""
    store = AnalyticalScreeningStateStore(tmp_path / "state.json")
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"confirmations_required": 1})
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    legacy = _screening_metric(as_of=as_of, known_at=known_at, identifier=1)
    semantic = _screening_metric(as_of=as_of, known_at=known_at, identifier=2, version_two=True)

    assert legacy.result_id.version == 5
    assert semantic.result_id.version == 8
    assert legacy.parameters["known_at"] == known_at.isoformat()
    assert "known_at" not in semantic.parameters

    first = _screen(rule, legacy, known_at)
    second = _screen(rule, semantic, known_at)
    first_outcome = store.record_attempt(_receipt(first, attempt_id=uuid4()), (first,))
    second_outcome = store.record_attempt(_receipt(second, attempt_id=uuid4()), (second,))
    state = store.load()

    assert first_outcome.candidates_created == 1
    assert second_outcome.candidates_created == 0
    assert len(state.candidates) == 1
    assert state.candidates[0].status is not AnalyticalCandidateStatus.RESOLVED


def test_existing_v1_rows_are_never_rewritten_or_reassigned(tmp_path) -> None:
    """N1: the adoption writes UUID8 rows and leaves every UUID5 row byte-identical."""
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage, count=6)
        history = HistoricalMarketDataService(storage)
        request = _statistics_request(start, end, clock)
        computation = MarketStatisticsEngine().compute(history.query(request.query), request)
        for calculation in computation.calculations:
            identifier = metric_result_id(calculation, request.query.known_at)
            storage.metric_results.save(
                _stored_result(
                    calculation,
                    identifier,
                    computed_at=clock,
                    parameters={
                        **calculation.parameters,
                        "known_at": request.query.known_at.isoformat(),
                    },
                )
            )
        before = _rows(storage)
        assert before
        assert all(item.result_id.version == 5 for item in before.values())

        summary = _pipeline(storage, history, clock).run(request)
        after = _rows(storage)
        created = {
            identifier: item for identifier, item in after.items() if identifier not in before
        }

        assert summary.results_created == summary.results_generated == len(created) > 0
        assert set(before) <= set(after)
        assert all(after[identifier] == item for identifier, item in before.items())
        assert all(item.result_id.version == 8 for item in created.values())
        assert set(before) & set(created) == set()
        assert all(item.parameters.get("known_at") is None for item in created.values())
        assert all(
            item.parameters.get("source_id") == COINBASE_SOURCE_ID for item in created.values()
        )


def test_pit_checks_still_reject_inputs_and_dependencies_after_known_at(tmp_path) -> None:
    """N2: no input and no dependency unavailable at known_at can be persisted."""
    clock = datetime(2026, 2, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path / "input")) as storage:
        start, end = _store_coinbase(storage, count=4)
        history = HistoricalMarketDataService(storage)
        request = _statistics_request(start, end, clock)
        computation = MarketStatisticsEngine().compute(history.query(request.query), request)
        template = next(
            item for item in computation.calculations if item.metric_key == SIMPLE_RETURN_KEY
        )
        late = _late_observation(storage, known_at=clock)
        forged = template.model_copy(
            update={
                "input_observation_ids": (late.observation_id,),
                "available_at": late.available_at,
            }
        )
        pipeline = MarketStatisticsPipeline(
            storage,
            history,
            _StubEngine(computation.model_copy(update={"calculations": (forged,)})),
            clock=lambda: clock + timedelta(days=1),
        )

        with pytest.raises(
            MarketStatisticsPipelineError,
            match="result uses information unavailable at known_at",
        ):
            pipeline.run(request)

    with LocalStorage(StoragePaths.from_root(tmp_path / "dependency")) as storage:
        start, end = _store_coinbase(storage, count=5)
        history = HistoricalMarketDataService(storage)
        request = _statistics_request(start, end, clock)
        computation = MarketStatisticsEngine().compute(history.query(request.query), request)
        ema = [item for item in computation.calculations if item.metric_key == EMA_KEY]
        seed, successor = ema[0], ema[1]
        late_available_at = clock + timedelta(hours=1)
        storage.metric_results.save(
            _stored_result(
                seed,
                semantic_metric_result_id(seed),
                computed_at=late_available_at + timedelta(hours=1),
                available_at=late_available_at,
            )
        )
        forged = successor.model_copy(
            update={"input_metric_result_ids": (semantic_metric_result_id(seed),)}
        )
        pipeline = MarketStatisticsPipeline(
            storage,
            history,
            _StubEngine(computation.model_copy(update={"calculations": (forged,)})),
            clock=lambda: clock,
        )

        with pytest.raises(
            MarketStatisticsPipelineError,
            match="result uses derived information unavailable at known_at",
        ):
            pipeline.run(request)


def _screening_metric(
    *,
    as_of: datetime,
    known_at: datetime,
    identifier: int,
    version_two: bool = False,
) -> MetricResult:
    parameters: dict[str, object] = {
        "source_id": "alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        "window": 20,
    }
    if not version_two:
        parameters["known_at"] = known_at.isoformat()
    return MetricResult(
        result_id=UUID(f"90000000-0000-{8000 if version_two else 5000}-8000-{identifier:012d}"),
        asset_id="equity:us:aapl",
        metric_key="market.history.relative_volume",
        value=Decimal("1.8"),
        unit="ratio",
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters=parameters,
        input_observation_ids=[UUID(f"91000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version="market-relative-volume-v1-decimal34",
        quality=DataQuality.PARTIAL,
    )


def _screen(rule, metric: MetricResult, known_at: datetime):
    return AnalyticalScreeningEngine().evaluate(
        AnalyticalScreeningRequest(
            rule=rule,
            asset_id=metric.asset_id,
            asset_class=AssetClass.EQUITY,
            source_id=metric.parameters["source_id"],
            known_at=known_at,
            computed_at=known_at,
            metrics=(metric,),
        )
    )


def _receipt(result, *, attempt_id: UUID) -> AnalyticalMonitorReceipt:
    return AnalyticalMonitorReceipt(
        attempt_id=attempt_id,
        job_id="alpaca:equity:us:aapl:market-daily",
        asset_id=result.asset_id,
        status=AnalyticalMonitorReceiptStatus.SCREENED,
        reason="new_compatible_evidence",
        processed_at=result.computed_at,
        result_ids=(result.result_id,),
    )


def _attempt(*, attempt_id: UUID, known_at: datetime) -> ScheduledJobAttempt:
    definition = ScheduledJobDefinition(
        job_id="coinbase:crypto:btc-usd:daily",
        asset_id=_ASSET_ID,
        provider="coinbase",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
    )
    scheduled_for = datetime(2026, 2, 1, 12, tzinfo=UTC)
    return ScheduledJobAttempt(
        attempt_id=attempt_id,
        definition=definition,
        local_date=date(2026, 2, 1),
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=scheduled_for,
        completed_at=scheduled_for + timedelta(minutes=2),
        execution=ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=known_at,
            evidence_changed=True,
            source_ids=(COINBASE_SOURCE_ID,),
            created_count=1,
            reused_count=0,
        ),
    )
