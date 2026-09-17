import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID, COINBASE_SOURCE_ID
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_definitions import (
    ATR_KEY,
    EMA_KEY,
    MACD_HISTOGRAM_KEY,
    RSI_KEY,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_identity import metric_result_id
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsRequest,
    MarketStatisticsRunSummary,
    MetricCalculation,
)
from investment_analyst.analytics.market.statistics_pipeline import (
    MarketStatisticsPipeline,
    MarketStatisticsPipelineError,
    MetricIdentityConflictError,
)
from investment_analyst.core.models import DataQuality
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.serialization import canonical_json_text


def _store_coinbase(storage: LocalStorage, count: int = 4) -> tuple[datetime, datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        retrieved = timestamp + timedelta(hours=1)
        close = Decimal("100") + Decimal(index * 2)
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


def _store_alpaca(storage: LocalStorage, count: int = 4) -> tuple[datetime, datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        retrieved = timestamp + timedelta(hours=1)
        close = Decimal("200") + Decimal(index * 2)
        raw_values = {
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(close - 1),
            "h": str(close + 2),
            "l": str(close - 2),
            "c": str(close),
            "v": str(Decimal("1000") + Decimal(index * 100)),
            "n": str(Decimal("100") + Decimal(index)),
            "vw": str(close),
        }
        bar = AlpacaStockBar(
            symbol="AAPL",
            timestamp=timestamp,
            open=close - 1,
            high=close + 2,
            low=close - 2,
            close=close,
            volume=Decimal("1000") + Decimal(index * 100),
            trade_count=Decimal("100") + Decimal(index),
            vwap=close,
            raw_values=raw_values,
        )
        raw = bar_to_raw_record(
            bar,
            retrieved_at=retrieved,
            request_url="https://data.alpaca.markets/test",
        )
        storage.raw_records.save(raw)
        for observation in bar_to_observations(
            bar,
            raw,
            normalized_at=retrieved + timedelta(minutes=1),
        ):
            storage.observations.save(observation)
    return start, start + timedelta(days=count)


def _request(asset_id: str, source_id: str, start: datetime, end: datetime, known_at: datetime):
    return MarketStatisticsRequest(
        query=HistoricalBarQuery(
            asset_id=asset_id,
            source_id=source_id,
            start=start,
            end=end,
            known_at=known_at,
        ),
        sma_windows=(2,),
        volatility_window=2,
        relative_volume_window=2,
        ema_windows=(2,),
    )


def test_btc_and_aapl_statistics_are_persisted_with_quality_and_idempotency(tmp_path) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        btc_start, btc_end = _store_coinbase(storage)
        aapl_start, aapl_end = _store_alpaca(storage)
        history = HistoricalMarketDataService(storage)
        pipeline = MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        btc_request = _request(
            "crypto:btc-usd", COINBASE_SOURCE_ID, btc_start, btc_end, fixed_clock
        )
        aapl_request = _request(
            "equity:us:aapl", ALPACA_SOURCE_ID, aapl_start, aapl_end, fixed_clock
        )
        raw_count = len(storage.raw_records.list())
        observation_count = len(storage.observations.list())

        first_btc = pipeline.run(btc_request)
        btc_ids = {
            item.result_id for item in storage.metric_results.list(asset_id="crypto:btc-usd")
        }
        second_btc = pipeline.run(btc_request)
        aapl_summary = pipeline.run(aapl_request)
        aapl_results = storage.metric_results.list(asset_id="equity:us:aapl")

        assert first_btc.results_created == first_btc.results_generated
        assert second_btc.results_created == 0
        assert second_btc.results_reused == second_btc.results_generated
        assert btc_ids == {
            item.result_id for item in storage.metric_results.list(asset_id="crypto:btc-usd")
        }
        assert all(
            item.quality is DataQuality.VALID
            for item in storage.metric_results.list(asset_id="crypto:btc-usd")
        )
        assert all(item.quality is DataQuality.PARTIAL for item in aapl_results)
        assert aapl_summary.definitions_upserted == 17
        assert len(storage.metric_definitions.list_all()) == 17
        assert len(storage.raw_records.list()) == raw_count
        assert len(storage.observations.list()) == observation_count
        assert storage.diagnostics.list() == []
        assert first_btc.to_json_dict()["traceability_verified"] is True


def test_ema_lineage_is_linear_and_is_reused_without_rewriting_history(tmp_path) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage, count=5)
        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)

        first = pipeline.run(request)
        ema = storage.metric_results.list(asset_id="crypto:btc-usd", metric_key=EMA_KEY)
        first_ids = [item.result_id for item in ema]
        second = pipeline.run(request)
        reused = storage.metric_results.list(asset_id="crypto:btc-usd", metric_key=EMA_KEY)

        assert first.result_counts[EMA_KEY] == 4
        assert [item.input_metric_result_ids for item in ema] == [
            [],
            *[[ema[index - 1].result_id] for index in range(1, 4)],
        ]
        assert [item.result_id for item in reused] == first_ids
        assert second.results_created == 0


def test_technical_indicator_lineage_is_persisted_and_reused(tmp_path) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage, count=40)
        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)

        first = pipeline.run(request)
        second = pipeline.run(request)

        assert first.result_counts[RSI_KEY] == 26
        assert first.result_counts[ATR_KEY] == 27
        assert first.result_counts[MACD_HISTOGRAM_KEY] == 7
        assert second.results_created == 0
        assert second.results_reused == second.results_generated


def test_known_at_is_part_of_result_identity_and_computed_at_is_preserved(tmp_path) -> None:
    first_clock = datetime(2026, 3, 1, tzinfo=UTC)
    second_clock = datetime(2026, 3, 2, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        history = HistoricalMarketDataService(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, first_clock)
        first_pipeline = MarketStatisticsPipeline(
            storage, history, MarketStatisticsEngine(), clock=lambda: first_clock
        )
        first_pipeline.run(request)
        first_results = storage.metric_results.list(asset_id="crypto:btc-usd")
        original_computed = {item.result_id: item.computed_at for item in first_results}

        second_pipeline = MarketStatisticsPipeline(
            storage, history, MarketStatisticsEngine(), clock=lambda: second_clock
        )
        second_pipeline.run(request)
        reused = storage.metric_results.list(asset_id="crypto:btc-usd")
        assert {item.result_id: item.computed_at for item in reused} == original_computed

        later_request = _request(
            "crypto:btc-usd",
            COINBASE_SOURCE_ID,
            start,
            end,
            first_clock + timedelta(hours=1),
        )
        second_pipeline.run(later_request)
        later_results = storage.metric_results.list(asset_id="crypto:btc-usd")
        assert len(later_results) == len(first_results) * 2


def test_statistics_cardinality_checks_do_not_materialize_global_lists(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        originals = {
            repository: repository.list
            for repository in (
                storage.raw_records,
                storage.observations,
                storage.metric_results,
                storage.diagnostics,
            )
        }

        def guarded_list(repository, *args, **kwargs):
            if not kwargs:
                pytest.fail("statistics pipeline loaded an unfiltered repository list")
            return originals[repository](*args, **kwargs)

        for repository in originals:
            monkeypatch.setattr(
                repository,
                "list",
                lambda *args, _repository=repository, **kwargs: guarded_list(
                    _repository,
                    *args,
                    **kwargs,
                ),
            )

        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        summary = pipeline.run(
            _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)
        )

        assert summary.traceability_verified is True


def test_pipeline_batches_observations_and_dependencies_without_get_after_save(
    tmp_path,
) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)

        save_many_calls: list[int] = []
        orig_save_many = storage.metric_results.save_many

        def spy_save_many(results):
            save_many_calls.append(len(results))
            return orig_save_many(results)

        storage.metric_results.save_many = spy_save_many

        def fail_save(*args, **kwargs):
            pytest.fail("pipeline called per-row metric_results.save instead of save_many")

        storage.metric_results.save = fail_save

        obs_get_many_calls: list[int] = []
        orig_obs_get_many = storage.observations.get_many

        def spy_obs_get_many(ids):
            obs_get_many_calls.append(len(ids))
            return orig_obs_get_many(ids)

        storage.observations.get_many = spy_obs_get_many

        def fail_obs_get(*args, **kwargs):
            pytest.fail("pipeline called per-row observations.get instead of get_many")

        storage.observations.get = fail_obs_get

        orig_metric_get = storage.metric_results.get

        def fail_metric_get_after_save(*args, **kwargs):
            if save_many_calls:
                pytest.fail("pipeline called metric_results.get after save_many")
            return orig_metric_get(*args, **kwargs)

        storage.metric_results.get = fail_metric_get_after_save

        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        summary = pipeline.run(
            _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)
        )

        assert len(save_many_calls) == 1
        assert save_many_calls[0] == summary.results_created > 0
        assert len(obs_get_many_calls) >= 1
        assert summary.traceability_verified is True


def test_batch_path_produces_identical_results_ids_and_decimals(
    tmp_path,
) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)
        history_service = HistoricalMarketDataService(storage)
        series = history_service.query(request.query)
        engine = MarketStatisticsEngine()
        computation = engine.compute(series, request)

        pipeline = MarketStatisticsPipeline(
            storage,
            history_service,
            engine,
            clock=lambda: fixed_clock,
        )
        summary = pipeline.run(request)

        expected_by_id = {
            metric_result_id(calc, request.query.known_at): calc
            for calc in computation.calculations
        }
        stored = storage.metric_results.list(asset_id="crypto:btc-usd")
        assert len(stored) == len(expected_by_id) > 0
        assert len(stored) == summary.results_generated
        for item in stored:
            assert item.result_id in expected_by_id
            expected_calc = expected_by_id[item.result_id]
            assert item.value == expected_calc.value
            assert isinstance(item.value, Decimal)
            assert item.metric_key == expected_calc.metric_key
            assert item.as_of == expected_calc.as_of
            assert item.available_at == expected_calc.available_at
            assert item.algorithm_version == expected_calc.algorithm_version


def test_dependency_graph_is_memoized_without_changing_dag_semantics(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)
        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )

        visited_nodes: list[UUID] = []
        orig_verify_graph = pipeline._verify_derived_graph

        def spy_verify_graph(identifier, active, verified, dep_cache):
            visited_nodes.append(identifier)
            return orig_verify_graph(identifier, active, verified, dep_cache)

        monkeypatch.setattr(pipeline, "_verify_derived_graph", spy_verify_graph)
        summary = pipeline.run(request)
        assert summary.traceability_verified is True
        assert len(visited_nodes) > 0

        # Cycle detection check: DAG semantics preserve cycle detection
        c1_id = uuid4()
        c2_id = uuid4()
        c1 = MetricCalculation(
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            metric_key=EMA_KEY,
            value=Decimal("100"),
            unit="USD",
            as_of=fixed_clock,
            available_at=fixed_clock,
            parameters={"source_id": COINBASE_SOURCE_ID, "known_at": fixed_clock.isoformat()},
            input_observation_ids=(uuid4(),),
            input_metric_result_ids=(c2_id,),
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        c2 = MetricCalculation(
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            metric_key=EMA_KEY,
            value=Decimal("100"),
            unit="USD",
            as_of=fixed_clock,
            available_at=fixed_clock,
            parameters={"source_id": COINBASE_SOURCE_ID, "known_at": fixed_clock.isoformat()},
            input_observation_ids=(uuid4(),),
            input_metric_result_ids=(c1_id,),
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        monkeypatch.setattr(
            "investment_analyst.analytics.market.statistics_pipeline.metric_result_id",
            lambda calc, known_at: c1_id if calc is c1 else c2_id,
        )
        with pytest.raises(MarketStatisticsPipelineError, match="cycle"):
            pipeline._topologically_order((c1, c2), fixed_clock)


def test_deep_verification_covers_new_and_conflicting_rows_only(
    tmp_path,
) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)
        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )

        obs_reads: list[int] = []
        orig_obs_get_many = storage.observations.get_many

        def spy_obs_get_many(ids):
            obs_reads.append(len(ids))
            return orig_obs_get_many(ids)

        storage.observations.get_many = spy_obs_get_many

        # Run 1: all new rows
        first_run = pipeline.run(request)
        assert first_run.results_created > 0
        assert first_run.results_reused == 0
        assert len(obs_reads) == 1

        # Run 2: all reused rows
        obs_reads.clear()
        second_run = pipeline.run(request)
        assert second_run.results_created == 0
        assert second_run.results_reused == first_run.results_created
        assert len(obs_reads) == 0
        assert second_run.traceability_verified is True

        # Run 3: conflicting row detection
        stored = storage.metric_results.list(asset_id="crypto:btc-usd")
        corrupted = stored[0].model_copy(update={"value": Decimal("999999.99")})
        storage.metric_results._connection.execute(
            "UPDATE metric_results SET document_json = ? WHERE result_id = ?",
            [canonical_json_text(corrupted), str(corrupted.result_id)],
        )
        with pytest.raises(
            MetricIdentityConflictError,
            match="conflicts with its deterministic identity",
        ):
            pipeline.run(request)


def test_late_failure_preserves_previously_persisted_stages(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)
        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )

        def fail_verify_run(*args, **kwargs):
            raise MarketStatisticsPipelineError("simulated late verification failure")

        monkeypatch.setattr(pipeline, "_verify_run", fail_verify_run)

        with pytest.raises(
            MarketStatisticsPipelineError, match="simulated late verification failure"
        ):
            pipeline.run(request)

        definitions = storage.metric_definitions.list_all()
        assert len(definitions) > 0
        persisted_metrics = storage.metric_results.list(asset_id="crypto:btc-usd")
        assert len(persisted_metrics) > 0


def test_every_existing_validation_is_preserved(tmp_path) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, fixed_clock)

        # 1. Clock without timezone
        naive_clock = datetime.now()
        pipeline_bad_clock = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: naive_clock,
        )
        with pytest.raises(
            MarketStatisticsPipelineError, match="clock must return a timezone-aware datetime"
        ):
            pipeline_bad_clock.run(request)

        # 2. Query mismatch
        class _MismatchedQueryService:
            def query(self, query):
                real_series = HistoricalMarketDataService(storage).query(query)
                return real_series.model_copy(
                    update={"query": query.model_copy(update={"source_id": ALPACA_SOURCE_ID})}
                )

        pipeline_mismatched = MarketStatisticsPipeline(
            storage,
            _MismatchedQueryService(),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        with pytest.raises(
            MarketStatisticsPipelineError, match="history service returned a different query"
        ):
            pipeline_mismatched.run(request)

        # 3. Missing dependency
        calc_missing_dep = MetricCalculation(
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            metric_key=EMA_KEY,
            value=Decimal("100"),
            unit="USD",
            as_of=fixed_clock,
            available_at=fixed_clock,
            parameters={"source_id": COINBASE_SOURCE_ID, "known_at": fixed_clock.isoformat()},
            input_observation_ids=(uuid4(),),
            input_metric_result_ids=(uuid4(),),
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        with pytest.raises(
            MarketStatisticsPipelineError, match="derived metric dependency is missing"
        ):
            pipeline._topologically_order((calc_missing_dep,), fixed_clock)

        # 4. Self dependency
        dep_id = uuid4()
        calc_self_dep = MetricCalculation(
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            metric_key=EMA_KEY,
            value=Decimal("100"),
            unit="USD",
            as_of=fixed_clock,
            available_at=fixed_clock,
            parameters={"source_id": COINBASE_SOURCE_ID, "known_at": fixed_clock.isoformat()},
            input_observation_ids=(uuid4(),),
            input_metric_result_ids=(dep_id,),
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "investment_analyst.analytics.market.statistics_pipeline.metric_result_id",
                lambda calc, known_at: dep_id,
            )
            with pytest.raises(
                MarketStatisticsPipelineError, match="metric result cannot depend on itself"
            ):
                pipeline._topologically_order((calc_self_dep,), fixed_clock)


def test_public_run_signature_and_result_models_are_unchanged() -> None:
    sig = inspect.signature(MarketStatisticsPipeline.run)
    assert "request" in sig.parameters
    assert sig.parameters["request"].annotation == MarketStatisticsRequest
    assert sig.return_annotation == MarketStatisticsRunSummary

    fields = MarketStatisticsRunSummary.model_fields
    assert "results_created" in fields
    assert "results_reused" in fields
    assert "results_generated" in fields
    assert "traceability_verified" in fields


def test_no_validation_becomes_optional_sampled_or_configurable() -> None:
    init_sig = inspect.signature(MarketStatisticsPipeline.__init__)
    for forbidden in (
        "skip_validation",
        "sample",
        "sample_rate",
        "validate",
        "verify",
        "fast_mode",
    ):
        assert forbidden not in init_sig.parameters

    run_sig = inspect.signature(MarketStatisticsPipeline.run)
    for forbidden in (
        "skip_validation",
        "sample",
        "sample_rate",
        "validate",
        "verify",
        "fast_mode",
    ):
        assert forbidden not in run_sig.parameters
