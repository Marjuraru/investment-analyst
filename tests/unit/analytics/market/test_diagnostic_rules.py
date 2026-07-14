"""Tests for deterministic Decimal-only market-diagnostic rules."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticRequest,
    MarketMetricSnapshot,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    MarketDiagnosticEngine,
    MarketDiagnosticRuleError,
    clamp,
)
from investment_analyst.core.models import (
    DataQuality,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)

ASSET_ID = "crypto:btc-usd"
SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
AS_OF = datetime(2026, 7, 10, tzinfo=UTC)
KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
COMPUTED_AT = datetime(2026, 7, 21, tzinfo=UTC)


def make_request() -> MarketDiagnosticRequest:
    return MarketDiagnosticRequest(
        query=HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 15, tzinfo=UTC),
            known_at=KNOWN_AT,
        ),
        short_sma_window=2,
        long_sma_window=3,
        volatility_window=2,
        relative_volume_window=2,
    )


def make_metric(
    metric_key: str,
    *,
    value: Decimal,
    unit: str,
    window: int | None = None,
    quality: DataQuality = DataQuality.VALID,
) -> MetricResult:
    algorithms = {
        "market.history.simple_return_1d": "market-simple-return-1d-v1-decimal34",
        "market.history.sma": "market-sma-v1-decimal34",
        "market.history.rolling_daily_volatility": ("market-rolling-daily-volatility-v1-decimal34"),
        "market.history.relative_volume": "market-relative-volume-v1-decimal34",
    }
    parameters: dict[str, object] = {
        "source_id": SOURCE_ID,
        "known_at": KNOWN_AT.isoformat(),
    }
    if window is not None:
        parameters["window"] = window
    return MetricResult(
        result_id=uuid4(),
        asset_id=ASSET_ID,
        metric_key=metric_key,
        value=value,
        unit=unit,
        as_of=AS_OF,
        available_at=AS_OF + timedelta(hours=1),
        computed_at=AS_OF + timedelta(hours=2),
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithms[metric_key],
        quality=quality,
    )


def make_snapshot(
    *,
    simple_return: Decimal = Decimal("0.02"),
    short_sma: Decimal = Decimal("102"),
    long_sma: Decimal = Decimal("100"),
    volatility: Decimal = Decimal("0.03"),
    relative_volume: Decimal = Decimal("1.5"),
    qualities: tuple[DataQuality, ...] | None = None,
) -> MarketMetricSnapshot:
    selected_qualities = qualities or (DataQuality.VALID,) * 5
    return MarketMetricSnapshot(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        known_at=KNOWN_AT,
        as_of=AS_OF,
        simple_return=make_metric(
            "market.history.simple_return_1d",
            value=simple_return,
            unit="ratio",
            quality=selected_qualities[0],
        ),
        short_sma=make_metric(
            "market.history.sma",
            value=short_sma,
            unit="USD",
            window=2,
            quality=selected_qualities[1],
        ),
        long_sma=make_metric(
            "market.history.sma",
            value=long_sma,
            unit="USD",
            window=3,
            quality=selected_qualities[2],
        ),
        rolling_volatility=make_metric(
            "market.history.rolling_daily_volatility",
            value=volatility,
            unit="ratio",
            window=2,
            quality=selected_qualities[3],
        ),
        relative_volume=make_metric(
            "market.history.relative_volume",
            value=relative_volume,
            unit="ratio",
            window=2,
            quality=selected_qualities[4],
        ),
    )


def compute(snapshot: MarketMetricSnapshot):
    return MarketDiagnosticEngine().compute(make_request(), snapshot, computed_at=COMPUTED_AT)


def test_clamp_uses_decimal_and_limits_values() -> None:
    assert clamp(Decimal("5"), Decimal("0"), Decimal("4")) == Decimal("4")
    assert clamp(Decimal("-1"), Decimal("0"), Decimal("4")) == Decimal("0")
    assert clamp(Decimal("2"), Decimal("0"), Decimal("4")) == Decimal("2")
    with pytest.raises(TypeError):
        clamp(1.0, Decimal("0"), Decimal("1"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("short_sma", "long_sma", "simple_return", "verdict", "score"),
    [
        (
            Decimal("102"),
            Decimal("100"),
            Decimal("0.02"),
            DiagnosticVerdict.POSITIVE,
            Decimal("70"),
        ),
        (Decimal("100"), Decimal("100"), Decimal("0"), DiagnosticVerdict.NEUTRAL, Decimal("50")),
        (
            Decimal("98"),
            Decimal("100"),
            Decimal("-0.02"),
            DiagnosticVerdict.NEGATIVE,
            Decimal("30"),
        ),
        (
            Decimal("100"),
            Decimal("100"),
            Decimal("0.025"),
            DiagnosticVerdict.POSITIVE,
            Decimal("60"),
        ),
        (
            Decimal("100"),
            Decimal("100"),
            Decimal("-0.025"),
            DiagnosticVerdict.NEGATIVE,
            Decimal("40"),
        ),
    ],
)
def test_trend_momentum_and_thresholds(
    short_sma: Decimal,
    long_sma: Decimal,
    simple_return: Decimal,
    verdict: DiagnosticVerdict,
    score: Decimal,
) -> None:
    diagnostic = compute(
        make_snapshot(
            short_sma=short_sma,
            long_sma=long_sma,
            simple_return=simple_return,
        )
    )

    assert diagnostic.verdict is verdict
    assert diagnostic.final_score == score
    assert [item.weight for item in diagnostic.components] == [Decimal("0.60"), Decimal("0.40")]


def test_volatility_and_volume_change_confidence_not_score() -> None:
    first = compute(make_snapshot(volatility=Decimal("0"), relative_volume=Decimal("2")))
    second = compute(make_snapshot(volatility=Decimal("0.10"), relative_volume=Decimal("0")))

    assert first.final_score == second.final_score
    assert first.verdict is second.verdict
    assert first.confidence == Decimal("1")
    assert second.confidence == Decimal("0")


def test_confidence_uses_participation_and_stability() -> None:
    diagnostic = compute(make_snapshot(volatility=Decimal("0.05"), relative_volume=Decimal("1")))

    assert diagnostic.confidence == Decimal("0.5")


@pytest.mark.parametrize(
    ("quality", "cap"),
    [
        (DataQuality.VALID, Decimal("1")),
        (DataQuality.DELAYED, Decimal("0.90")),
        (DataQuality.PARTIAL, Decimal("0.75")),
        (DataQuality.SUSPECT, Decimal("0.50")),
    ],
)
def test_confidence_is_capped_by_worst_quality(quality: DataQuality, cap: Decimal) -> None:
    qualities = (
        DataQuality.VALID,
        quality,
        DataQuality.VALID,
        DataQuality.VALID,
        DataQuality.VALID,
    )
    diagnostic = compute(
        make_snapshot(volatility=Decimal("0"), relative_volume=Decimal("2"), qualities=qualities)
    )

    assert diagnostic.quality is quality
    assert diagnostic.confidence == cap


def test_evidence_is_exact_and_context_metrics_are_neutral() -> None:
    snapshot = make_snapshot()
    diagnostic = compute(snapshot)
    evidence = {item.metric_result_id: item for item in diagnostic.evidence}

    assert len(evidence) == 5
    assert evidence[snapshot.simple_return.result_id].direction is EvidenceDirection.SUPPORTS
    assert evidence[snapshot.simple_return.result_id].contribution == Decimal("0.4")
    assert evidence[snapshot.short_sma.result_id].contribution == Decimal("0.2")
    assert evidence[snapshot.long_sma.result_id].contribution == Decimal("0.2")
    assert evidence[snapshot.rolling_volatility.result_id].direction is EvidenceDirection.NEUTRAL
    assert evidence[snapshot.rolling_volatility.result_id].contribution == 0
    assert evidence[snapshot.relative_volume.result_id].direction is EvidenceDirection.NEUTRAL
    assert evidence[snapshot.relative_volume.result_id].contribution == 0


def test_decimal_context_is_not_modified() -> None:
    original_precision = getcontext().prec

    compute(make_snapshot())

    assert getcontext().prec == original_precision


def test_insufficient_data_uses_fallback_context() -> None:
    fallback = make_metric(
        "market.history.simple_return_1d",
        value=Decimal("0.01"),
        unit="ratio",
        quality=DataQuality.DELAYED,
    )
    diagnostic = MarketDiagnosticEngine().compute(
        make_request(),
        None,
        computed_at=COMPUTED_AT,
        fallback_metric_results=(fallback,),
    )

    assert diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA
    assert diagnostic.final_score == 0
    assert diagnostic.confidence == 0
    assert diagnostic.components == []
    assert diagnostic.evidence == []
    assert diagnostic.as_of == fallback.as_of
    assert diagnostic.quality is DataQuality.DELAYED
    assert "sma_short" in diagnostic.summary


def test_computed_at_before_availability_is_rejected() -> None:
    with pytest.raises(MarketDiagnosticRuleError, match="computed_at"):
        MarketDiagnosticEngine().compute(
            make_request(),
            make_snapshot(),
            computed_at=AS_OF,
        )


def test_summary_does_not_contain_order_language() -> None:
    summary = compute(make_snapshot()).summary.lower()

    assert "buy" not in summary
    assert "sell" not in summary
    assert "hold" not in summary
