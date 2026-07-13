"""Tests for diagnostic validation and aggregation."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import (
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
)


def make_component(
    *,
    score: Decimal = Decimal("80"),
    weight: Decimal = Decimal("1"),
    contribution: Decimal = Decimal("80"),
) -> DiagnosticComponent:
    return DiagnosticComponent(
        component_key="trend",
        score=score,
        weight=weight,
        weighted_contribution=contribution,
        metric_result_ids=[uuid4()],
        explanation="Trend component.",
    )


def make_evidence() -> DiagnosticEvidence:
    return DiagnosticEvidence(
        metric_result_id=uuid4(),
        direction=EvidenceDirection.SUPPORTS,
        contribution=Decimal("0.8"),
        reason="The metric supports the component score.",
    )


def make_diagnostic(**overrides: object) -> DiagnosticResult:
    values: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "mode": DiagnosticMode.MARKET,
        "verdict": DiagnosticVerdict.POSITIVE,
        "final_score": Decimal("80"),
        "confidence": Decimal("0.75"),
        "as_of": datetime(2026, 7, 10, tzinfo=UTC),
        "available_at": datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        "computed_at": datetime(2026, 7, 10, 16, 2, tzinfo=UTC),
        "components": [make_component()],
        "evidence": [make_evidence()],
        "algorithm_version": "1.0.0",
        "summary": "Positive market diagnostic.",
        "quality": DataQuality.VALID,
    }
    values.update(overrides)
    return DiagnosticResult.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("final_score", Decimal("100.01")),
        ("final_score", Decimal("-0.01")),
        ("confidence", Decimal("1.01")),
        ("confidence", Decimal("-0.01")),
    ],
)
def test_diagnostic_validates_score_and_confidence_bounds(field: str, value: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_diagnostic(**{field: value})


def test_diagnostic_validates_component_weight_sum() -> None:
    components = [
        make_component(score=Decimal("80"), weight=Decimal("0.6"), contribution=Decimal("48")),
        DiagnosticComponent(
            component_key="momentum",
            score=Decimal("60"),
            weight=Decimal("0.3"),
            weighted_contribution=Decimal("18"),
            metric_result_ids=[uuid4()],
            explanation="Momentum component.",
        ),
    ]

    with pytest.raises(ValidationError, match="weights must sum to 1"):
        make_diagnostic(final_score=Decimal("66"), components=components)


def test_component_validates_explicit_weighted_contribution() -> None:
    with pytest.raises(ValidationError, match="weighted_contribution"):
        make_component(
            score=Decimal("80"),
            weight=Decimal("0.5"),
            contribution=Decimal("41"),
        )


def test_diagnostic_validates_final_score_from_contributions() -> None:
    with pytest.raises(ValidationError, match="final_score"):
        make_diagnostic(final_score=Decimal("79"))


def test_insufficient_data_may_omit_components_and_evidence() -> None:
    result = make_diagnostic(
        verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
        final_score=Decimal("0"),
        confidence=Decimal("0"),
        components=[],
        evidence=[],
        summary="Not enough observations are available.",
        quality=DataQuality.PARTIAL,
    )

    assert result.verdict is DiagnosticVerdict.INSUFFICIENT_DATA
