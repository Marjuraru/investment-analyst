"""Tests for strict point-in-time market-diagnostic models."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticRequest,
    MarketDiagnosticRunSummary,
    MarketMetricSnapshot,
)
from investment_analyst.core.models import DataQuality, DiagnosticVerdict, MetricResult

KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
AS_OF = datetime(2026, 7, 10, tzinfo=UTC)
SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
ASSET_ID = "crypto:btc-usd"


def make_query() -> HistoricalBarQuery:
    return HistoricalBarQuery(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 7, 15, tzinfo=UTC),
        known_at=KNOWN_AT,
    )


def make_request(**overrides: object) -> MarketDiagnosticRequest:
    values: dict[str, object] = {
        "query": make_query(),
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
    value: Decimal,
    unit: str,
    window: int | None = None,
    asset_id: str = ASSET_ID,
    source_id: str = SOURCE_ID,
    known_at: datetime = KNOWN_AT,
    as_of: datetime = AS_OF,
    result_id: UUID | None = None,
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
    return MetricResult(
        result_id=result_id or uuid4(),
        asset_id=asset_id,
        metric_key=metric_key,
        value=value,
        unit=unit,
        as_of=as_of,
        available_at=as_of + timedelta(hours=1),
        computed_at=as_of + timedelta(hours=2),
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithms[metric_key],
        quality=DataQuality.VALID,
    )


def make_snapshot(**overrides: object) -> MarketMetricSnapshot:
    values: dict[str, object] = {
        "asset_id": ASSET_ID,
        "source_id": SOURCE_ID,
        "known_at": KNOWN_AT,
        "as_of": AS_OF,
        "simple_return": make_metric(
            "market.history.simple_return_1d", value=Decimal("0.02"), unit="ratio"
        ),
        "short_sma": make_metric("market.history.sma", value=Decimal("102"), unit="USD", window=2),
        "long_sma": make_metric("market.history.sma", value=Decimal("100"), unit="USD", window=3),
        "rolling_volatility": make_metric(
            "market.history.rolling_daily_volatility",
            value=Decimal("0.03"),
            unit="ratio",
            window=2,
        ),
        "relative_volume": make_metric(
            "market.history.relative_volume",
            value=Decimal("1.5"),
            unit="ratio",
            window=2,
        ),
    }
    values.update(overrides)
    return MarketMetricSnapshot.model_validate(values)


def test_request_defaults() -> None:
    request = MarketDiagnosticRequest(query=make_query())

    assert request.short_sma_window == 5
    assert request.long_sma_window == 20
    assert request.volatility_window == 20
    assert request.relative_volume_window == 20


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("short_sma_window", True),
        ("long_sma_window", 0),
        ("volatility_window", 1),
        ("relative_volume_window", -1),
        ("long_sma_window", 10_001),
    ],
)
def test_request_rejects_invalid_windows(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_request(**{field: value})


def test_request_requires_short_window_below_long_window() -> None:
    with pytest.raises(ValidationError, match="short_sma_window"):
        make_request(short_sma_window=3, long_sma_window=3)


def test_snapshot_is_valid_and_has_deterministic_id_order() -> None:
    snapshot = make_snapshot()

    assert snapshot.metric_result_ids() == tuple(
        result.result_id for result in snapshot.metric_results()
    )
    assert all(isinstance(result.value, Decimal) for result in snapshot.metric_results())


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "simple_return",
            make_metric(
                "market.history.simple_return_1d",
                value=Decimal("0.02"),
                unit="ratio",
                asset_id="equity:us:aapl",
            ),
            "same asset",
        ),
        (
            "short_sma",
            make_metric(
                "market.history.sma",
                value=Decimal("102"),
                unit="USD",
                window=2,
                source_id="other:source",
            ),
            "source_id",
        ),
        (
            "long_sma",
            make_metric(
                "market.history.sma",
                value=Decimal("100"),
                unit="USD",
                window=3,
                known_at=KNOWN_AT + timedelta(days=1),
            ),
            "known_at",
        ),
        (
            "rolling_volatility",
            make_metric(
                "market.history.rolling_daily_volatility",
                value=Decimal("0.03"),
                unit="ratio",
                window=2,
                as_of=AS_OF - timedelta(days=1),
            ),
            "same as_of",
        ),
    ],
)
def test_snapshot_rejects_inconsistent_context(
    field: str,
    replacement: MetricResult,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_snapshot(**{field: replacement})


def test_snapshot_rejects_wrong_key_and_unit() -> None:
    wrong_key = make_metric(
        "market.history.relative_volume",
        value=Decimal("1.5"),
        unit="ratio",
        window=2,
    )
    with pytest.raises(ValidationError, match="metric key"):
        make_snapshot(simple_return=wrong_key)

    wrong_unit = make_metric(
        "market.history.simple_return_1d",
        value=Decimal("0.02"),
        unit="USD",
    )
    with pytest.raises(ValidationError, match="unit"):
        make_snapshot(simple_return=wrong_unit)


def test_snapshot_rejects_duplicate_result_ids() -> None:
    shared_id = uuid4()
    simple_return = make_metric(
        "market.history.simple_return_1d",
        value=Decimal("0.02"),
        unit="ratio",
        result_id=shared_id,
    )
    relative_volume = make_metric(
        "market.history.relative_volume",
        value=Decimal("1.5"),
        unit="ratio",
        window=2,
        result_id=shared_id,
    )
    with pytest.raises(ValidationError, match="distinct"):
        make_snapshot(simple_return=simple_return, relative_volume=relative_volume)


def test_run_summary_serializes_to_json() -> None:
    snapshot = make_snapshot()
    summary = MarketDiagnosticRunSummary(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        requested_start=make_query().start,
        requested_end=make_query().end,
        known_at=KNOWN_AT,
        as_of=AS_OF,
        computed_at=KNOWN_AT,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("70"),
        confidence=Decimal("0.75"),
        quality=DataQuality.VALID,
        selected_metric_result_ids=snapshot.metric_result_ids(),
        missing_requirements=(),
        diagnostics_generated=1,
        diagnostics_created=1,
        diagnostics_reused=0,
        traceability_verified=True,
    )

    document = summary.to_json_dict()

    assert document["final_score"] == "70"
    assert document["known_at"].endswith("Z")
