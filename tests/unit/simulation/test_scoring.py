"""Unit tests for pure Decimal simulation scoring."""

from decimal import Decimal

import pytest

from investment_analyst.core.models import DiagnosticVerdict
from investment_analyst.simulation.scoring import (
    clamp,
    final_score,
    score_return,
    score_volume,
    verdict_for_score,
)


def test_clamp_and_invalid_range() -> None:
    assert clamp(Decimal("5"), Decimal("0"), Decimal("10")) == Decimal("5")
    assert clamp(Decimal("-1"), Decimal("0"), Decimal("10")) == Decimal("0")
    assert clamp(Decimal("11"), Decimal("0"), Decimal("10")) == Decimal("10")
    with pytest.raises(ValueError, match="minimum"):
        clamp(Decimal("5"), Decimal("10"), Decimal("0"))


def test_return_scores_positive_neutral_negative_and_limits() -> None:
    assert score_return(Decimal("0.02")) == Decimal("70")
    assert score_return(Decimal("0")) == Decimal("50")
    assert score_return(Decimal("-0.02")) == Decimal("30")
    assert score_return(Decimal("1")) == Decimal("100")
    assert score_return(Decimal("-1")) == Decimal("0")


def test_volume_scores_and_limits() -> None:
    assert score_volume(Decimal("1.0")) == Decimal("50.0")
    assert score_volume(Decimal("1.5")) == Decimal("62.5")
    assert score_volume(Decimal("2.0")) == Decimal("75.0")
    assert score_volume(Decimal("10")) == Decimal("100")
    assert score_volume(Decimal("-10")) == Decimal("0")


def test_final_score_and_verdicts_use_decimal() -> None:
    result = final_score(Decimal("70"), Decimal("62.5"))

    assert result == Decimal("67.750")
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)
    assert verdict_for_score(Decimal("60")) is DiagnosticVerdict.POSITIVE
    assert verdict_for_score(Decimal("50")) is DiagnosticVerdict.NEUTRAL
    assert verdict_for_score(Decimal("40")) is DiagnosticVerdict.NEGATIVE
