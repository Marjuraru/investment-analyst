"""Integration coverage for bounded descriptive condition outcomes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.alerts.analytical_rule_catalog import (
    MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE,
    RSI_INCREMENTAL_LOW_REVIEW_RULE,
    RSI_LOW_REVIEW_RULE,
)
from investment_analyst.alerts.descriptive_outcome_models import (
    DescriptiveOutcomeRequest,
    DescriptiveOutcomeStatus,
)
from investment_analyst.alerts.descriptive_outcomes import (
    _MAX_METRIC_ENTRIES,
    _MAX_OBSERVATIONS,
    DescriptiveConditionOutcomeService,
    DescriptiveOutcomeBudgetExceededError,
    DescriptiveOutcomeFingerprintChangedError,
    DescriptiveOutcomeUnsupportedError,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.repositories import (
    DuckDBMetricResultRepository,
    DuckDBObservationRepository,
)
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspacePaths
from investment_analyst.workspace.service import WorkspaceService

_RSI = RSI_INCREMENTAL_LOW_REVIEW_RULE
_MACD = MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE
_AAPL = "equity:us:aapl"
_AAPL_SOURCE = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_AMD = "equity:us:amd"
_AMD_SOURCE = "alpaca-market-data:iex:amd:daily-bars:adjustment-all"
_BTC = "crypto:btc-usd"
_BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_QQQ = "etf:us:qqq"
_QQQ_SOURCE = "alpaca-market-data:iex:qqq:daily-bars:adjustment-all"
_BASE = datetime(2026, 10, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 10, 20, tzinfo=UTC)


def _raw(
    asset_id: str,
    source_id: str,
    day: datetime,
    value: str,
    *,
    revision: str = "base",
    available_at: datetime | None = None,
) -> RawRecord:
    available = available_at or day + timedelta(days=1, hours=1)
    return RawRecord(
        record_id=uuid5(
            NAMESPACE_URL,
            f"descriptive-raw:{asset_id}:{source_id}:{day.isoformat()}:{revision}:{value}",
        ),
        asset_id=asset_id,
        source=SourceReference(
            source_id=source_id,
            record_key=f"{day.isoformat()}:{revision}:{value}",
            retrieved_at=available,
        ),
        event_time=day,
        available_at=available,
        received_at=available,
        payload={"close": value},
        schema_version="descriptive-outcome-integration-v1",
    )


def _close(
    asset_id: str,
    source_id: str,
    day: datetime,
    value: str,
    *,
    revision: str = "base",
    available_at: datetime | None = None,
    native_period: bool = False,
) -> tuple[RawRecord, NormalizedObservation]:
    available = available_at or day + timedelta(days=1, hours=1)
    raw = _raw(asset_id, source_id, day, value, revision=revision, available_at=available)
    observation = NormalizedObservation(
        observation_id=uuid5(
            NAMESPACE_URL,
            f"descriptive-close:{raw.record_id}:{asset_id}:{source_id}",
        ),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name="close",
        value=Decimal(value),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=day,
        period_start=day if native_period else None,
        period_end=day + timedelta(days=1) if native_period else None,
        available_at=available,
        normalized_at=available + timedelta(minutes=1),
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="descriptive-outcome-integration-v1",
    )
    return raw, observation


def _rsi_metric(
    period: datetime,
    value: str,
    available_at: datetime,
    *,
    asset_id: str = _AAPL,
    source_id: str = _AAPL_SOURCE,
    quality: DataQuality = DataQuality.PARTIAL,
) -> MetricResult:
    parameters = {"source_id": source_id, "window": 14}
    inputs = [uuid5(NAMESPACE_URL, f"rsi-input:{asset_id}:{period.isoformat()}:{value}")]
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=asset_id,
            metric_key="market.technical.rsi",
            input_observation_ids=inputs,
            algorithm_version="market-rsi-incremental-v2-decimal34",
            as_of=period,
            available_at=available_at,
            unit="index",
            quality=quality,
            parameters=parameters,
        ),
        asset_id=asset_id,
        metric_key="market.technical.rsi",
        value=Decimal(value),
        unit="index",
        as_of=period,
        available_at=available_at,
        computed_at=available_at + timedelta(hours=2),
        parameters=parameters,
        input_observation_ids=inputs,
        algorithm_version="market-rsi-incremental-v2-decimal34",
        quality=quality,
    )


def _macd_metric(
    period: datetime,
    value: str,
    available_at: datetime,
    *,
    asset_id: str = _BTC,
    source_id: str = _BTC_SOURCE,
) -> MetricResult:
    parameters = {
        "source_id": source_id,
        "fast_window": 12,
        "slow_window": 26,
        "signal_window": 9,
    }
    inputs = [uuid5(NAMESPACE_URL, f"macd-input:{asset_id}:{period.isoformat()}:{value}")]
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=asset_id,
            metric_key="market.technical.macd.histogram",
            input_observation_ids=inputs,
            algorithm_version="market-macd-incremental-v2-decimal34",
            as_of=period,
            available_at=available_at,
            unit="USD",
            quality=DataQuality.PARTIAL,
            parameters=parameters,
        ),
        asset_id=asset_id,
        metric_key="market.technical.macd.histogram",
        value=Decimal(value),
        unit="USD",
        as_of=period,
        available_at=available_at,
        computed_at=available_at + timedelta(hours=2),
        parameters=parameters,
        input_observation_ids=inputs,
        algorithm_version="market-macd-incremental-v2-decimal34",
        quality=DataQuality.PARTIAL,
    )


def _seed(
    root: Path,
    *,
    closes: tuple[tuple[RawRecord, NormalizedObservation], ...],
    metrics: tuple[MetricResult, ...],
) -> tuple[WorkspaceService, WorkspacePaths, ApplicationRuntime]:
    service = WorkspaceService(environ={}, home=root / "home")
    paths = service.initialize(root / "workspace").paths
    storage = service.open_storage(paths, WorkspaceAccessMode.READ_WRITE)
    try:
        for raw, observation in closes:
            storage.raw_records.save(raw)
            storage.observations.save(observation)
        for metric in metrics:
            storage.metric_results.save(metric)
    finally:
        storage.close()
    return service, paths, ApplicationRuntime.create_default(workspace_service=service)


def _request(
    rule,
    *,
    asset_id: str = _AAPL,
    start: datetime = _BASE,
    end: datetime = _BASE,
    known_at: datetime = _KNOWN_AT,
    fingerprint: str | None = None,
) -> DescriptiveOutcomeRequest:
    return DescriptiveOutcomeRequest(
        asset_id=asset_id,
        rule_id=rule.rule_id,
        expected_rule_fingerprint=fingerprint or rule.semantic_fingerprint(),
        start_date=start.date(),
        end_date=end.date(),
        known_at=known_at,
    )


def _service(
    runtime: ApplicationRuntime,
    paths: WorkspacePaths,
    rule,
) -> DescriptiveConditionOutcomeService:
    return DescriptiveConditionOutcomeService(runtime, paths.root, rule)


def test_equity_report_partitions_matched_not_matched_and_zero_returns(
    tmp_path: Path,
) -> None:
    periods = (_BASE, _BASE + timedelta(days=1), _BASE + timedelta(days=2))
    metrics = (
        _rsi_metric(periods[0], "25", periods[0] + timedelta(days=1)),
        _rsi_metric(periods[1], "40", periods[1] + timedelta(days=1)),
        _rsi_metric(periods[2], "20", periods[2] + timedelta(days=1)),
    )
    values = {
        1: "100",
        2: "110",
        3: "100",
        4: "100",
        5: "100",
        6: "100",
        7: "100",
        8: "100",
    }
    closes = tuple(
        _close(_AAPL, _AAPL_SOURCE, _BASE + timedelta(days=day), value)
        for day, value in values.items()
    )
    service, paths, runtime = _seed(tmp_path, closes=closes, metrics=metrics)

    request = _request(_RSI, start=periods[0], end=periods[2])
    report = _service(runtime, paths, _RSI).run(request)

    assert report.observed_periods == 3
    assert (report.matched_count, report.not_matched_count, report.not_evaluable_count) == (
        2,
        1,
        0,
    )
    assert [row.group.value for row in report.rows] == ["matched", "not_matched", "matched"]
    matched = report.groups[0]
    not_matched = report.groups[1]
    assert matched.zeros == 2
    assert matched.mean_return_5_intervals == Decimal("0")
    assert matched.median_return_5_intervals == Decimal("0")
    assert not_matched.negatives == 1
    assert not_matched.mean_return_5_intervals is not None
    assert not_matched.mean_return_5_intervals < 0
    assert report.rows[1].outcome.return_5_intervals == not_matched.mean_return_5_intervals
    assert report.rows[0].outcome.status is DescriptiveOutcomeStatus.AVAILABLE
    assert report.rows[0].outcome.observed_intervals == 5
    assert report.rows[0].outcome.elapsed_seconds == 5 * 86_400
    assert report.rows[0].condition.computed_at == periods[0] + timedelta(days=1, hours=2)
    assert report.rows[0].outcome.closes[0].observation_id in {
        observation.observation_id for _, observation in closes
    }
    assert report.latest_evidence_available_at is not None
    assert report.latest_evidence_available_at <= request.known_at

    repeated = _service(runtime, paths, _RSI).run(request)
    assert repeated == report
    assert repeated.to_json_bytes() == report.to_json_bytes()


def test_pending_insufficient_and_thirty_day_boundary(
    tmp_path: Path,
) -> None:
    period = _BASE
    metric = _macd_metric(period, "1", period + timedelta(days=1))
    five = tuple(
        _close(
            _BTC,
            _BTC_SOURCE,
            period + timedelta(days=day),
            str(99 + day),
            native_period=True,
        )
        for day in range(1, 6)
    )
    service, paths, runtime = _seed(tmp_path, closes=five, metrics=(metric,))
    engine = _service(runtime, paths, _MACD)

    pending = engine.run(
        _request(
            _MACD,
            asset_id=_BTC,
            start=period,
            end=period,
            known_at=period + timedelta(days=10),
        )
    )
    assert pending.rows[0].outcome.status is DescriptiveOutcomeStatus.PENDING_WINDOW
    assert pending.rows[0].outcome.return_5_intervals is None

    insufficient = engine.run(
        _request(
            _MACD,
            asset_id=_BTC,
            start=period,
            end=period,
            known_at=period + timedelta(days=32),
        )
    )
    assert (
        insufficient.rows[0].outcome.status is DescriptiveOutcomeStatus.INSUFFICIENT_OBSERVED_BARS
    )

    boundary_sixth = _close(
        _BTC,
        _BTC_SOURCE,
        period + timedelta(days=30),
        "106",
        native_period=True,
        available_at=period + timedelta(days=31),
    )
    boundary_service, boundary_paths, boundary_runtime = _seed(
        tmp_path / "boundary",
        closes=(*five, boundary_sixth),
        metrics=(metric,),
    )
    boundary = _service(boundary_runtime, boundary_paths, _MACD).run(
        _request(
            _MACD,
            asset_id=_BTC,
            start=period,
            end=period,
            known_at=period + timedelta(days=31),
        )
    )
    assert boundary.rows[0].outcome.status is DescriptiveOutcomeStatus.AVAILABLE
    assert len(boundary.rows[0].outcome.closes) == 6
    assert boundary.rows[0].outcome.closes[-1].period_end == period + timedelta(days=31)
    assert boundary.rows[0].outcome.return_5_intervals == Decimal("0.06")


def test_first_revision_is_frozen_and_close_ambiguity_is_not_skipped(
    tmp_path: Path,
) -> None:
    period = _BASE
    first = _rsi_metric(period, "25", period + timedelta(days=1))
    later = _rsi_metric(period, "40", period + timedelta(days=2))
    closes = []
    for day in range(1, 8):
        revision = "base"
        value = str(100 + day)
        if day == 2:
            revision = "base"
        closes.append(
            _close(
                _AAPL,
                _AAPL_SOURCE,
                period + timedelta(days=day),
                value,
                revision=revision,
            )
        )
    ambiguous_raw, ambiguous_observation = _close(
        _AAPL,
        _AAPL_SOURCE,
        period + timedelta(days=2),
        "999",
        revision="conflict",
        available_at=period + timedelta(days=3, hours=1),
    )
    service, paths, runtime = _seed(
        tmp_path,
        closes=(*closes, (ambiguous_raw, ambiguous_observation)),
        metrics=(first, later),
    )

    report = _service(runtime, paths, _RSI).run(_request(_RSI, start=period, end=period))
    assert report.rows[0].group.value == "matched"
    assert report.rows[0].condition.observed_value == Decimal("25")
    assert report.rows[0].condition.metric_result_id == first.result_id
    assert report.rows[0].outcome.status is DescriptiveOutcomeStatus.AMBIGUOUS_CLOSE
    assert "ambiguous_close_at" in (report.rows[0].outcome.reason or "")
    assert len(report.rows[0].outcome.closes) < 6

    mutated_later = _rsi_metric(period, "45", period + timedelta(days=2))
    mutating_service, mutating_paths, mutating_runtime = _seed(
        tmp_path / "mutated",
        closes=closes,
        metrics=(first, mutated_later),
    )
    mutated = _service(mutating_runtime, mutating_paths, _RSI).run(
        _request(_RSI, start=period, end=period)
    )
    assert mutated.rows[0].condition.observed_value == Decimal("25")
    assert mutated.rows[0].condition.metric_result_id == first.result_id


def test_ambiguous_metric_is_not_evaluable_without_return(
    tmp_path: Path,
) -> None:
    period = _BASE
    left = _rsi_metric(period, "25", period + timedelta(days=1))
    right = _rsi_metric(period, "20", period + timedelta(days=1))
    closes = tuple(
        _close(
            _AAPL,
            _AAPL_SOURCE,
            period + timedelta(days=day),
            str(100 + day),
        )
        for day in range(1, 7)
    )
    service, paths, runtime = _seed(tmp_path, closes=closes, metrics=(left, right))

    report = _service(runtime, paths, _RSI).run(_request(_RSI, start=period, end=period))
    assert report.not_evaluable_count == 1
    row = report.rows[0]
    assert row.condition.reason.value == "ambiguous_metric"
    assert row.outcome.status is DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE
    assert row.outcome.return_5_intervals is None
    assert report.groups[0].mean_return_5_intervals is None
    assert report.groups[0].median_return_5_intervals is None


def test_fingerprint_and_unsupported_inputs_fail_closed(tmp_path: Path) -> None:
    period = _BASE
    metric = _rsi_metric(period, "25", period + timedelta(days=1))
    closes = tuple(
        _close(_AAPL, _AAPL_SOURCE, period + timedelta(days=day), str(100 + day))
        for day in range(1, 7)
    )
    service, paths, runtime = _seed(tmp_path, closes=closes, metrics=(metric,))

    with pytest.raises(DescriptiveOutcomeFingerprintChangedError):
        _service(runtime, paths, _RSI).run(
            _request(_RSI, start=period, end=period, fingerprint="0" * 64)
        )
    with pytest.raises(DescriptiveOutcomeUnsupportedError):
        DescriptiveConditionOutcomeService(runtime, paths.root, RSI_LOW_REVIEW_RULE)
    with pytest.raises(DescriptiveOutcomeUnsupportedError):
        _service(runtime, paths, _RSI).run(
            _request(_RSI, asset_id="equity:us:unknown", start=period, end=period)
        )


def test_read_path_uses_one_close_batch_and_hydrates_only_scoped_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = _BASE
    target_metric = _rsi_metric(period, "25", period + timedelta(days=1))
    target_closes = tuple(
        _close(_AAPL, _AAPL_SOURCE, period + timedelta(days=day), str(100 + day))
        for day in range(1, 7)
    )
    foreign_metrics = tuple(
        _rsi_metric(
            period + timedelta(days=index),
            "40",
            period + timedelta(days=index + 1),
            asset_id=_AMD,
            source_id=_AMD_SOURCE,
        )
        for index in range(20)
    )
    foreign_closes = tuple(
        _close(_AMD, _AMD_SOURCE, period + timedelta(days=index), "50") for index in range(20)
    )
    service, paths, runtime = _seed(
        tmp_path,
        closes=(*target_closes, *foreign_closes),
        metrics=(target_metric, *foreign_metrics),
    )

    list_calls: list[dict[str, object]] = []
    count_calls: list[dict[str, object]] = []
    hydration_batches: list[tuple[UUID, ...]] = []
    index_pages: list[int] = []
    original_list = DuckDBObservationRepository.list
    original_count = DuckDBObservationRepository.count
    original_get_many = DuckDBMetricResultRepository.get_many
    original_index = DuckDBMetricResultRepository.list_metric_index_page

    def tracked_list(repository, **kwargs):
        list_calls.append(dict(kwargs))
        return original_list(repository, **kwargs)

    def tracked_count(repository, **kwargs):
        count_calls.append(dict(kwargs))
        return original_count(repository, **kwargs)

    def tracked_get_many(repository, result_ids):
        hydration_batches.append(tuple(result_ids))
        return original_get_many(repository, result_ids)

    def tracked_index(repository, query):
        index_pages.append(query.limit)
        return original_index(repository, query)

    with monkeypatch.context() as patch:
        patch.setattr(DuckDBObservationRepository, "list", tracked_list)
        patch.setattr(DuckDBObservationRepository, "count", tracked_count)
        patch.setattr(DuckDBMetricResultRepository, "get_many", tracked_get_many)
        patch.setattr(DuckDBMetricResultRepository, "list_metric_index_page", tracked_index)
        report = _service(runtime, paths, _RSI).run(_request(_RSI, start=period, end=period))

    assert report.observed_periods == 1
    close_counts = [call for call in count_calls if call.get("field_name") == "close"]
    close_lists = [call for call in list_calls if call.get("field_name") == "close"]
    assert len(close_counts) == 1
    assert len(close_lists) == 1
    assert index_pages and all(limit <= 256 for limit in index_pages)
    hydrated = {identifier for batch in hydration_batches for identifier in batch}
    assert hydrated == {target_metric.result_id}
    assert all(call.get("asset_id") == _AAPL for call in close_counts + close_lists)
    assert all(call.get("source_id") == _AAPL_SOURCE for call in close_counts + close_lists)


def test_declared_budgets_fail_closed_without_partial_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = _BASE
    metrics = tuple(
        _rsi_metric(period + timedelta(days=index), "25", period + timedelta(days=index + 1))
        for index in range(5)
    )
    closes = tuple(
        _close(_AAPL, _AAPL_SOURCE, period + timedelta(days=day), str(100 + day))
        for day in range(1, 8)
    )
    service, paths, runtime = _seed(tmp_path, closes=closes, metrics=metrics)

    assert _MAX_METRIC_ENTRIES == 4096
    assert _MAX_OBSERVATIONS == 4096
    with monkeypatch.context() as patch:
        patch.setattr(
            "investment_analyst.alerts.descriptive_outcomes._MAX_METRIC_ENTRIES",
            4,
        )
        with pytest.raises(DescriptiveOutcomeBudgetExceededError):
            _service(runtime, paths, _RSI).run(
                _request(
                    _RSI,
                    start=period,
                    end=period + timedelta(days=4),
                )
            )
    with monkeypatch.context() as patch:
        patch.setattr(
            "investment_analyst.alerts.descriptive_outcomes._MAX_OBSERVATIONS",
            4,
        )
        with pytest.raises(DescriptiveOutcomeBudgetExceededError):
            _service(runtime, paths, _RSI).run(_request(_RSI, start=period, end=period))


@pytest.mark.parametrize(
    ("asset_id", "source_id", "asset_class"),
    [
        (_AAPL, _AAPL_SOURCE, "equity"),
        (_BTC, _BTC_SOURCE, "crypto"),
        (_QQQ, _QQQ_SOURCE, "etf"),
    ],
)
def test_same_capability_covers_equity_crypto_and_etf(
    tmp_path: Path,
    asset_id: str,
    source_id: str,
    asset_class: str,
) -> None:
    period = _BASE
    metric = _rsi_metric(
        period,
        "25",
        period + timedelta(days=1),
        asset_id=asset_id,
        source_id=source_id,
    )
    closes = tuple(
        _close(
            asset_id,
            source_id,
            period + timedelta(days=day),
            str(100 + day),
            native_period=asset_id == _BTC,
        )
        for day in range(1, 7)
    )
    service, paths, runtime = _seed(tmp_path, closes=closes, metrics=(metric,))

    report = _service(runtime, paths, _RSI).run(
        _request(_RSI, asset_id=asset_id, start=period, end=period)
    )
    assert report.asset_class.value == asset_class
    assert report.source_id == source_id
    assert report.matched_count == 1
    assert report.rows[0].outcome.status is DescriptiveOutcomeStatus.AVAILABLE


def test_report_is_read_only_at_the_repository_boundary(tmp_path: Path) -> None:
    period = _BASE
    metric = _rsi_metric(period, "25", period + timedelta(days=1))
    closes = tuple(
        _close(_AAPL, _AAPL_SOURCE, period + timedelta(days=day), str(100 + day))
        for day in range(1, 7)
    )
    service, paths, runtime = _seed(tmp_path, closes=closes, metrics=(metric,))

    def forbidden(*args, **kwargs):
        raise AssertionError("descriptive outcomes must not write domain data")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(DuckDBObservationRepository, "save", forbidden)
        patch.setattr(DuckDBObservationRepository, "save_many", forbidden)
        patch.setattr(DuckDBMetricResultRepository, "save", forbidden)
        patch.setattr(DuckDBMetricResultRepository, "save_many", forbidden)
        report = _service(runtime, paths, _RSI).run(_request(_RSI, start=period, end=period))
    assert report.observed_periods == 1
