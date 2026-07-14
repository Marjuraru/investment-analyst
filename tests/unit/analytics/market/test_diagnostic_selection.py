"""Tests for point-in-time selection of persisted diagnostic metrics."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_selection import (
    AmbiguousMetricRevisionError,
    MarketDiagnosticMetricSelector,
    describe_missing_requirements,
)
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths

ASSET_ID = "crypto:btc-usd"
SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
AS_OF = datetime(2026, 7, 10, tzinfo=UTC)


def make_request(**overrides: object) -> MarketDiagnosticRequest:
    values: dict[str, object] = {
        "query": HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 15, tzinfo=UTC),
            known_at=KNOWN_AT,
        ),
        "short_sma_window": 2,
        "long_sma_window": 3,
        "volatility_window": 2,
        "relative_volume_window": 2,
    }
    values.update(overrides)
    return MarketDiagnosticRequest.model_validate(values)


def make_metric(
    metric_key: str,
    *,
    window: int | None = None,
    as_of: datetime = AS_OF,
    available_at: datetime | None = None,
    source_id: str = SOURCE_ID,
    known_at: datetime = KNOWN_AT,
    algorithm_version: str | None = None,
    value: Decimal = Decimal("1"),
) -> MetricResult:
    algorithms = {
        "market.history.simple_return_1d": "market-simple-return-1d-v1-decimal34",
        "market.history.sma": "market-sma-v1-decimal34",
        "market.history.rolling_daily_volatility": ("market-rolling-daily-volatility-v1-decimal34"),
        "market.history.relative_volume": "market-relative-volume-v1-decimal34",
    }
    parameters: dict[str, object] = {
        "source_id": source_id,
        "known_at": known_at.isoformat(),
    }
    if window is not None:
        parameters["window"] = window
    available = available_at or as_of + timedelta(hours=1)
    return MetricResult(
        result_id=uuid4(),
        asset_id=ASSET_ID,
        metric_key=metric_key,
        value=value,
        unit="USD" if metric_key == "market.history.sma" else "ratio",
        as_of=as_of,
        available_at=available,
        computed_at=max(available, KNOWN_AT),
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithm_version or algorithms[metric_key],
        quality=DataQuality.VALID,
    )


def complete_metrics(*, as_of: datetime = AS_OF) -> tuple[MetricResult, ...]:
    return (
        make_metric("market.history.simple_return_1d", as_of=as_of),
        make_metric("market.history.sma", window=2, as_of=as_of, value=Decimal("102")),
        make_metric("market.history.sma", window=3, as_of=as_of, value=Decimal("100")),
        make_metric("market.history.rolling_daily_volatility", window=2, as_of=as_of),
        make_metric("market.history.relative_volume", window=2, as_of=as_of),
    )


def save_all(storage: LocalStorage, results: tuple[MetricResult, ...]) -> None:
    for result in results:
        storage.metric_results.save(result)


def test_selector_returns_none_without_metrics(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        selector = MarketDiagnosticMetricSelector(storage)

        assert selector.select(make_request()) is None
        assert describe_missing_requirements(make_request(), ()) == (
            "simple_return_1d",
            "sma_short",
            "sma_long",
            "rolling_daily_volatility",
            "relative_volume",
        )


def test_selector_builds_complete_snapshot(tmp_path) -> None:
    metrics = complete_metrics()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        save_all(storage, metrics)
        snapshot = MarketDiagnosticMetricSelector(storage).select(make_request())

    assert snapshot is not None
    assert snapshot.as_of == AS_OF
    assert snapshot.metric_result_ids() == tuple(item.result_id for item in metrics)


def test_selector_uses_latest_complete_as_of_without_mixing(tmp_path) -> None:
    older = complete_metrics(as_of=AS_OF - timedelta(days=1))
    latest_incomplete = complete_metrics(as_of=AS_OF)[:-1]
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        save_all(storage, older + latest_incomplete)
        snapshot = MarketDiagnosticMetricSelector(storage).select(make_request())

    assert snapshot is not None
    assert snapshot.as_of == AS_OF - timedelta(days=1)


def test_selector_filters_source_known_at_windows_and_future_metrics(tmp_path) -> None:
    valid = complete_metrics()
    ignored = (
        make_metric("market.history.sma", window=9),
        make_metric("market.history.relative_volume", window=2, source_id="other:source"),
        make_metric(
            "market.history.rolling_daily_volatility",
            window=2,
            known_at=KNOWN_AT - timedelta(days=1),
        ),
        make_metric(
            "market.history.simple_return_1d",
            available_at=KNOWN_AT + timedelta(seconds=1),
        ),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        save_all(storage, valid + ignored)
        selector = MarketDiagnosticMetricSelector(storage)
        candidates = selector.candidates(make_request())
        snapshot = selector.select_from_results(make_request(), candidates)

    assert snapshot is not None
    assert set(snapshot.metric_result_ids()) == {item.result_id for item in valid}


def test_selector_selects_latest_available_revision(tmp_path) -> None:
    base = list(complete_metrics())
    old_return = base[0]
    new_return = make_metric(
        "market.history.simple_return_1d",
        available_at=old_return.available_at + timedelta(minutes=1),
        value=Decimal("0.02"),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        save_all(storage, tuple(base) + (new_return,))
        snapshot = MarketDiagnosticMetricSelector(storage).select(make_request())

    assert snapshot is not None
    assert snapshot.simple_return.result_id == new_return.result_id


def test_selector_rejects_equal_availability_revisions(tmp_path) -> None:
    base = list(complete_metrics())
    duplicate = make_metric(
        "market.history.simple_return_1d",
        available_at=base[0].available_at,
        value=Decimal("0.02"),
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        save_all(storage, tuple(base) + (duplicate,))
        selector = MarketDiagnosticMetricSelector(storage)

        with pytest.raises(AmbiguousMetricRevisionError):
            selector.select(make_request())


def test_selector_ignores_demonstrative_and_wrong_algorithm_metrics(tmp_path) -> None:
    valid = complete_metrics()
    demo = MetricResult(
        result_id=uuid4(),
        asset_id=ASSET_ID,
        metric_key="market.simple_return_1d",
        value=Decimal("9"),
        unit="ratio",
        as_of=AS_OF,
        available_at=AS_OF + timedelta(hours=1),
        computed_at=KNOWN_AT,
        parameters={"source_id": SOURCE_ID, "known_at": KNOWN_AT.isoformat()},
        input_observation_ids=[uuid4()],
        algorithm_version="simulated-metrics-v1",
        quality=DataQuality.VALID,
    )
    wrong_algorithm = make_metric(
        "market.history.simple_return_1d",
        algorithm_version="other-algorithm",
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        save_all(storage, valid + (demo, wrong_algorithm))
        selector = MarketDiagnosticMetricSelector(storage)
        candidates = selector.candidates(make_request())
        repeated = selector.candidates(make_request())

    assert len(candidates) == 5
    assert all(
        item.result_id not in {demo.result_id, wrong_algorithm.result_id} for item in candidates
    )
    assert candidates == repeated
