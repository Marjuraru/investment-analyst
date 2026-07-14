"""Tests for published fundamental scoring rules and diagnostic engine."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from uuid import uuid4

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticVerdict,
    EvidenceDirection,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
    fundamental_diagnostic_id,
    verify_fundamental_diagnostic_computation,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticInput,
    SecFundamentalDiagnosticMetric,
    SecFundamentalDiagnosticRequest,
    SecFundamentalDiagnosticSelection,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_rules import (
    BASE_WEIGHTS,
    centered_evidence_contribution,
    confidence_for,
    evidence_direction,
    normalized_weights,
    recency_factor,
    score_liabilities_to_assets,
    score_liabilities_to_equity,
    score_net_income_yoy_change_rate,
    score_net_margin,
    score_revenue_yoy_growth,
    verdict_for,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    get_sec_fundamental_metric_definition,
)

_ROLES = {
    "fundamental.net_margin": ("current_net_income", "current_revenue"),
    "fundamental.liabilities_to_assets": ("current_assets", "current_liabilities"),
    "fundamental.liabilities_to_equity": ("current_equity", "current_liabilities"),
    "fundamental.revenue_yoy_growth": ("current_revenue", "previous_revenue"),
    "fundamental.net_income_yoy_change_rate": (
        "current_net_income",
        "previous_net_income",
    ),
}


def _metric(
    metric_name: str,
    value: str,
    *,
    available_at: datetime | None = None,
    period_end: datetime | None = None,
    frequency: DataFrequency = DataFrequency.ANNUAL,
) -> SecFundamentalDiagnosticMetric:
    definition = get_sec_fundamental_metric_definition(metric_name)
    first = uuid4()
    second = uuid4()
    roles = tuple(
        SecFundamentalDiagnosticInput(role=role, observation_id=identifier)
        for role, identifier in zip(_ROLES[metric_name], (first, second), strict=True)
    )
    return SecFundamentalDiagnosticMetric(
        result_id=uuid4(),
        metric_name=metric_name,
        value=Decimal(value),
        unit="ratio",
        frequency=frequency,
        period_start=None,
        period_end=period_end or datetime(2025, 9, 27, tzinfo=UTC),
        available_at=available_at or datetime(2025, 10, 31, tzinfo=UTC),
        computed_at=datetime(2025, 11, 1, tzinfo=UTC),
        formula=definition.formula,
        algorithm_version=definition.algorithm_version,
        input_observation_ids=(first, second),
        input_roles=roles,
        quality=DataQuality.VALID,
    )


def _selection(
    values: dict[str, str],
    *,
    known_at: datetime | None = None,
    frequency: DataFrequency = DataFrequency.ANNUAL,
) -> SecFundamentalDiagnosticSelection:
    known_at = known_at or datetime(2026, 1, 1, tzinfo=UTC)
    request = SecFundamentalDiagnosticRequest(
        known_at=known_at,
        frequency=frequency,
    )
    metrics = tuple(
        sorted(
            (_metric(name, value, frequency=frequency) for name, value in values.items()),
            key=lambda item: item.metric_name,
        )
    )
    all_names = tuple(BASE_WEIGHTS)
    selected = {item.metric_name for item in metrics}
    return SecFundamentalDiagnosticSelection(
        request=request,
        target_period_end=(metrics[0].period_end if metrics else None),
        selected_metrics=metrics,
        missing_metric_names=tuple(name for name in all_names if name not in selected),
        metrics_examined=len(metrics),
        metrics_eligible=len(metrics),
        revisions_superseded=0,
        traceability_verified=True,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-0.01", "0"),
        ("0", "0"),
        ("0.125", "50.0"),
        ("0.25", "100"),
        ("0.30", "100"),
    ],
)
def test_net_margin_score_boundaries(value: str, expected: str) -> None:
    assert score_net_margin(Decimal(value)) == Decimal(expected)


@pytest.mark.parametrize(
    ("scorer", "value", "expected"),
    [
        (score_liabilities_to_assets, "0.40", "100"),
        (score_liabilities_to_assets, "0.65", "50.0"),
        (score_liabilities_to_assets, "0.90", "0"),
        (score_liabilities_to_equity, "0.50", "100"),
        (score_liabilities_to_equity, "2.75", "50.0"),
        (score_liabilities_to_equity, "5.00", "0"),
        (score_revenue_yoy_growth, "-0.10", "0"),
        (score_revenue_yoy_growth, "0.025", "50.0"),
        (score_revenue_yoy_growth, "0.15", "100"),
        (score_net_income_yoy_change_rate, "-0.50", "0"),
        (score_net_income_yoy_change_rate, "0", "50.00"),
        (score_net_income_yoy_change_rate, "0.50", "100"),
    ],
)
def test_other_score_boundaries(scorer, value: str, expected: str) -> None:
    assert scorer(Decimal(value)) == Decimal(expected)


@pytest.mark.parametrize(
    "scorer",
    [score_liabilities_to_assets, score_liabilities_to_equity],
)
def test_negative_liability_ratios_are_rejected(scorer) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        scorer(Decimal("-0.01"))


def test_weights_coverage_and_renormalization() -> None:
    names = (
        "fundamental.net_margin",
        "fundamental.liabilities_to_assets",
        "fundamental.revenue_yoy_growth",
    )
    weights = normalized_weights(names)

    assert sum(BASE_WEIGHTS.values(), Decimal("0")) == Decimal("1.00")
    assert abs(sum(weights.values(), Decimal("0")) - Decimal("1")) <= Decimal("0.0001")


@pytest.mark.parametrize(
    ("score", "verdict"),
    [
        ("65", DiagnosticVerdict.POSITIVE),
        ("64.999", DiagnosticVerdict.NEUTRAL),
        ("40", DiagnosticVerdict.NEUTRAL),
        ("39.999", DiagnosticVerdict.NEGATIVE),
    ],
)
def test_verdict_thresholds(score: str, verdict: DiagnosticVerdict) -> None:
    assert verdict_for(Decimal(score)) is verdict


def test_evidence_center_and_direction() -> None:
    assert centered_evidence_contribution(Decimal("75")) == Decimal("0.5")
    assert evidence_direction(Decimal("0.5")) is EvidenceDirection.SUPPORTS
    assert evidence_direction(Decimal("-0.5")) is EvidenceDirection.OPPOSES
    assert evidence_direction(Decimal("0")) is EvidenceDirection.NEUTRAL


def test_recency_and_confidence_rules() -> None:
    known_at = datetime(2026, 1, 1, tzinfo=UTC)

    assert recency_factor(known_at, known_at - timedelta(days=150), DataFrequency.QUARTERLY) == 1
    assert recency_factor(
        known_at, known_at - timedelta(days=365), DataFrequency.QUARTERLY
    ) == Decimal("0.50")
    assert recency_factor(known_at, known_at - timedelta(days=400), DataFrequency.ANNUAL) == 1
    assert recency_factor(
        known_at, known_at - timedelta(days=800), DataFrequency.ANNUAL
    ) == Decimal("0.50")
    assert confidence_for(Decimal("0.75"), Decimal("0.80")) == Decimal("0.6000")


def test_engine_builds_sufficient_diagnostic_with_exact_traceability() -> None:
    values = {
        "fundamental.net_margin": "0.20",
        "fundamental.liabilities_to_assets": "0.50",
        "fundamental.liabilities_to_equity": "1.00",
        "fundamental.revenue_yoy_growth": "0.10",
        "fundamental.net_income_yoy_change_rate": "0.20",
    }
    selection = _selection(values)
    global_precision = getcontext().prec

    computation = SecFundamentalDiagnosticEngine().compute(
        selection.request,
        selection,
        computed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    verify_fundamental_diagnostic_computation(computation)

    diagnostic = computation.diagnostic
    assert len(diagnostic.components) == 5
    assert len(diagnostic.evidence) == 5
    assert diagnostic.diagnostic_id == fundamental_diagnostic_id(selection)
    assert diagnostic.confidence <= 1
    assert diagnostic.quality is DataQuality.VALID
    assert "recommendation" in diagnostic.summary
    assert getcontext().prec == global_precision


def test_engine_renormalizes_partial_but_sufficient_coverage() -> None:
    selection = _selection(
        {
            "fundamental.net_margin": "0.20",
            "fundamental.liabilities_to_assets": "0.50",
            "fundamental.revenue_yoy_growth": "0.10",
        }
    )

    computation = SecFundamentalDiagnosticEngine().compute(
        selection.request,
        selection,
        computed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert computation.coverage == Decimal("0.70")
    assert computation.diagnostic.quality is DataQuality.PARTIAL
    assert abs(
        sum((item.weight for item in computation.diagnostic.components), Decimal("0"))
        - Decimal("1")
    ) <= Decimal("0.0001")


def test_engine_returns_insufficient_without_minimum_requirements() -> None:
    selection = _selection({"fundamental.net_margin": "0.20"})

    computation = SecFundamentalDiagnosticEngine().compute(
        selection.request,
        selection,
        computed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert computation.diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA
    assert computation.diagnostic.components == []
    assert computation.diagnostic.evidence == []
    assert computation.diagnostic.final_score == 0
    assert "fundamental.liabilities_to_assets" in computation.missing_requirements
