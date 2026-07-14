"""Integration tests for persisted market diagnostics and the CLI script."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import MarketDiagnosticMetricSelector
from investment_analyst.core.models import (
    Asset,
    AssetClass,
    DataFrequency,
    DataQuality,
    DiagnosticVerdict,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_NAMESPACE = UUID("1f187045-8b9e-57ce-8bd0-e7f4d42982aa")
START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 15, tzinfo=UTC)
KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
CLOCK = datetime(2026, 7, 21, tzinfo=UTC)
BTC_ASSET = "crypto:btc-usd"
BTC_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
AAPL_ASSET = "equity:us:aapl"
AAPL_SOURCE = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"


def stable_id(label: str):
    return uuid5(_NAMESPACE, label)


def make_request(asset_id: str, source_id: str, known_at: datetime = KNOWN_AT):
    return MarketDiagnosticRequest(
        query=HistoricalBarQuery(
            asset_id=asset_id,
            source_id=source_id,
            start=START,
            end=END,
            known_at=known_at,
        ),
        short_sma_window=2,
        long_sma_window=3,
        volatility_window=2,
        relative_volume_window=2,
    )


def seed_snapshot(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    quality: DataQuality,
    known_at: datetime = KNOWN_AT,
    suffix: str = "base",
    availability_offset: timedelta = timedelta(0),
) -> tuple[MetricResult, ...]:
    is_equity = asset_id == AAPL_ASSET
    storage.assets.upsert(
        Asset(
            asset_id=asset_id,
            symbol="AAPL" if is_equity else "BTC",
            name="Apple Inc." if is_equity else "Bitcoin",
            asset_class=AssetClass.EQUITY if is_equity else AssetClass.CRYPTO,
            quote_currency="USD",
            exchange="NASDAQ" if is_equity else "COINBASE",
            provider_symbols={"test": "AAPL" if is_equity else "BTC-USD"},
        )
    )
    storage.sources.upsert(
        SourceDefinition(
            source_id=source_id,
            provider_name="Test Provider",
            dataset_name="Test Daily Bars",
            source_type=SourceType.MARKET,
            base_url="https://example.invalid",
            is_official=False,
            coverage_notes="Offline integration fixture.",
        )
    )

    dates = [
        datetime(2026, 7, 8, tzinfo=UTC),
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 7, 10, tzinfo=UTC),
    ]
    closes = (Decimal("100"), Decimal("100"), Decimal("102"))
    volumes = (Decimal("1000"), Decimal("1100"), Decimal("1650"))
    close_observations: list[NormalizedObservation] = []
    volume_observations: list[NormalizedObservation] = []
    for index, timestamp in enumerate(dates):
        available_at = timestamp + timedelta(hours=1) + availability_offset
        reference = SourceReference(
            source_id=source_id,
            record_key=f"{asset_id}:{timestamp.date()}:{suffix}",
            retrieved_at=available_at,
        )
        raw = RawRecord(
            record_id=stable_id(f"raw:{asset_id}:{known_at.isoformat()}:{timestamp}:{suffix}"),
            asset_id=asset_id,
            source=reference,
            event_time=timestamp,
            available_at=available_at,
            received_at=available_at,
            payload={"close": str(closes[index]), "volume": str(volumes[index])},
            schema_version="diagnostic-integration-v1",
        )
        storage.raw_records.save(raw)
        close_observation = NormalizedObservation(
            observation_id=stable_id(f"close:{raw.record_id}"),
            raw_record_id=raw.record_id,
            asset_id=asset_id,
            field_name="close",
            value=closes[index],
            unit="USD",
            frequency=DataFrequency.DAY_1,
            observed_at=timestamp,
            available_at=available_at,
            normalized_at=available_at + timedelta(minutes=1),
            source=reference,
            quality=quality,
            transformation_version="diagnostic-integration-v1",
        )
        volume_observation = NormalizedObservation(
            observation_id=stable_id(f"volume:{raw.record_id}"),
            raw_record_id=raw.record_id,
            asset_id=asset_id,
            field_name="volume",
            value=volumes[index],
            unit="shares" if is_equity else "BTC",
            frequency=DataFrequency.DAY_1,
            observed_at=timestamp,
            available_at=available_at,
            normalized_at=available_at + timedelta(minutes=1),
            source=reference,
            quality=quality,
            transformation_version="diagnostic-integration-v1",
        )
        storage.observations.save(close_observation)
        storage.observations.save(volume_observation)
        close_observations.append(close_observation)
        volume_observations.append(volume_observation)

    as_of = dates[-1]
    available_at = close_observations[-1].available_at
    common = {"source_id": source_id, "known_at": known_at.isoformat()}
    specifications = (
        (
            "market.history.simple_return_1d",
            Decimal("0.02"),
            "ratio",
            {**common, "periods": 1},
            close_observations[1:],
            "market-simple-return-1d-v1-decimal34",
        ),
        (
            "market.history.sma",
            Decimal("101"),
            "USD",
            {**common, "window": 2},
            close_observations[1:],
            "market-sma-v1-decimal34",
        ),
        (
            "market.history.sma",
            Decimal("100"),
            "USD",
            {**common, "window": 3},
            close_observations,
            "market-sma-v1-decimal34",
        ),
        (
            "market.history.rolling_daily_volatility",
            Decimal("0.02"),
            "ratio",
            {**common, "window": 2},
            close_observations,
            "market-rolling-daily-volatility-v1-decimal34",
        ),
        (
            "market.history.relative_volume",
            Decimal("1.5"),
            "ratio",
            {**common, "window": 2},
            volume_observations,
            "market-relative-volume-v1-decimal34",
        ),
    )
    results: list[MetricResult] = []
    for position, (key, value, unit, parameters, observations, algorithm) in enumerate(
        specifications
    ):
        result = MetricResult(
            result_id=stable_id(
                f"metric:{asset_id}:{known_at.isoformat()}:{suffix}:{position}:{key}"
            ),
            asset_id=asset_id,
            metric_key=key,
            value=value,
            unit=unit,
            as_of=as_of,
            available_at=available_at,
            computed_at=known_at,
            parameters=parameters,
            input_observation_ids=[item.observation_id for item in observations],
            algorithm_version=algorithm,
            quality=quality,
        )
        storage.metric_results.save(result)
        results.append(result)
    return tuple(results)


def run_pipeline(storage: LocalStorage, request: MarketDiagnosticRequest, clock: datetime = CLOCK):
    selector = MarketDiagnosticMetricSelector(storage)
    return MarketDiagnosticPipeline(
        storage,
        selector,
        MarketDiagnosticEngine(),
        clock=lambda: clock,
    ).run(request)


def test_btc_diagnostic_is_valid_traceable_and_idempotent(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
        seed_snapshot(
            storage,
            asset_id=BTC_ASSET,
            source_id=BTC_SOURCE,
            quality=DataQuality.VALID,
        )
        counts_before = (
            len(storage.raw_records.list()),
            len(storage.observations.list()),
            len(storage.metric_results.list()),
        )
        first = run_pipeline(storage, make_request(BTC_ASSET, BTC_SOURCE))
        diagnostic = storage.diagnostics.list(asset_id=BTC_ASSET)[0]
        second = run_pipeline(
            storage,
            make_request(BTC_ASSET, BTC_SOURCE),
            clock=CLOCK + timedelta(days=1),
        )

        assert first.quality is DataQuality.VALID
        assert first.verdict is DiagnosticVerdict.POSITIVE
        assert first.diagnostics_created == 1
        assert first.diagnostics_reused == 0
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1
        assert second.computed_at == first.computed_at
        assert len(diagnostic.components) == 2
        assert len(diagnostic.evidence) == 5
        assert diagnostic.confidence <= 1
        assert all(
            storage.metric_results.get(identifier)
            for identifier in first.selected_metric_result_ids
        )
        assert counts_before == (
            len(storage.raw_records.list()),
            len(storage.observations.list()),
            len(storage.metric_results.list()),
        )
        assert all(
            timestamp.tzinfo is UTC
            for timestamp in (diagnostic.as_of, diagnostic.available_at, diagnostic.computed_at)
        )


def test_aapl_partial_quality_caps_confidence_and_assets_do_not_mix(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        seed_snapshot(
            storage,
            asset_id=BTC_ASSET,
            source_id=BTC_SOURCE,
            quality=DataQuality.VALID,
        )
        aapl_metrics = seed_snapshot(
            storage,
            asset_id=AAPL_ASSET,
            source_id=AAPL_SOURCE,
            quality=DataQuality.PARTIAL,
        )
        summary = run_pipeline(storage, make_request(AAPL_ASSET, AAPL_SOURCE))

        assert summary.quality is DataQuality.PARTIAL
        assert summary.confidence <= Decimal("0.75")
        assert set(summary.selected_metric_result_ids) == {item.result_id for item in aapl_metrics}
        assert all(
            storage.metric_results.get(identifier).asset_id == AAPL_ASSET
            for identifier in summary.selected_metric_result_ids
        )


def test_other_known_at_and_metric_revision_create_new_identities(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        seed_snapshot(
            storage,
            asset_id=BTC_ASSET,
            source_id=BTC_SOURCE,
            quality=DataQuality.VALID,
        )
        first = run_pipeline(storage, make_request(BTC_ASSET, BTC_SOURCE))
        other_known_at = KNOWN_AT + timedelta(days=1)
        seed_snapshot(
            storage,
            asset_id=BTC_ASSET,
            source_id=BTC_SOURCE,
            quality=DataQuality.VALID,
            known_at=other_known_at,
            suffix="other-known-at",
        )
        second = run_pipeline(
            storage,
            make_request(BTC_ASSET, BTC_SOURCE, other_known_at),
            clock=other_known_at + timedelta(days=1),
        )

        assert set(first.selected_metric_result_ids) != set(second.selected_metric_result_ids)
        assert len(storage.diagnostics.list(asset_id=BTC_ASSET)) == 2

        revised = seed_snapshot(
            storage,
            asset_id=BTC_ASSET,
            source_id=BTC_SOURCE,
            quality=DataQuality.VALID,
            suffix="revision",
            availability_offset=timedelta(minutes=10),
        )
        third = run_pipeline(storage, make_request(BTC_ASSET, BTC_SOURCE))

        assert third.selected_metric_result_ids != first.selected_metric_result_ids
        assert set(third.selected_metric_result_ids) == {item.result_id for item in revised}
        assert len(storage.diagnostics.list(asset_id=BTC_ASSET)) == 3


def test_insufficient_data_is_persisted_without_creating_inputs(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        summary = run_pipeline(storage, make_request(BTC_ASSET, BTC_SOURCE))
        diagnostic = storage.diagnostics.list(asset_id=BTC_ASSET)[0]

        assert summary.verdict is DiagnosticVerdict.INSUFFICIENT_DATA
        assert summary.final_score == 0
        assert summary.confidence == 0
        assert summary.missing_requirements
        assert diagnostic.components == []
        assert diagnostic.evidence == []
        assert len(storage.raw_records.list()) == 0
        assert len(storage.observations.list()) == 0
        assert len(storage.metric_results.list()) == 0


def test_script_outputs_json_and_reuses_diagnostic(tmp_path) -> None:
    root = tmp_path / "storage"
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        seed_snapshot(
            storage,
            asset_id=BTC_ASSET,
            source_id=BTC_SOURCE,
            quality=DataQuality.VALID,
        )
    script = Path(__file__).parents[4] / "scripts" / "compute_market_diagnostic.py"
    command = [
        sys.executable,
        str(script),
        "--root",
        str(root),
        "--asset-id",
        BTC_ASSET,
        "--source-id",
        BTC_SOURCE,
        "--start",
        "2026-07-01",
        "--end",
        "2026-07-15",
        "--known-at",
        "2026-07-20T00:00:00Z",
        "--short-sma-window",
        "2",
        "--long-sma-window",
        "3",
        "--volatility-window",
        "2",
        "--relative-volume-window",
        "2",
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[4] / "src")

    first = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    second = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["summary"]["diagnostics_created"] == 1
    assert second_payload["summary"]["diagnostics_reused"] == 1
    assert (
        first_payload["diagnostic"]["diagnostic_id"]
        == second_payload["diagnostic"]["diagnostic_id"]
    )
    assert "raw_record" not in first.stdout.lower()
    assert "traceback" not in first.stderr.lower()
    assert "api_key" not in first.stdout.lower()
