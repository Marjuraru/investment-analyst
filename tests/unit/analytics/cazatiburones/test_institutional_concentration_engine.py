from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_concentration_engine import calculate
from investment_analyst.analytics.cazatiburones.institutional_concentration_models import (
    InstitutionalConcentrationInput,
    InstitutionalConcentrationPosition,
)
from investment_analyst.core.models.enums import DataQuality

_KNOWN_AT = datetime(2025, 2, 14, tzinfo=UTC)


def _position(number: int, value: str) -> InstitutionalConcentrationPosition:
    return InstitutionalConcentrationPosition(
        cusip=f"0000000{number:02d}",
        title_of_class="COM",
        put_call=None,
        value_as_reported=Decimal(value),
    )


def _input(**updates: object) -> InstitutionalConcentrationInput:
    positions = tuple(_position(number, "10") for number in range(1, 11))
    values: dict[str, object] = {
        "manager_cik": "0001067983",
        "report_period": date(2024, 12, 31),
        "known_at": _KNOWN_AT,
        "close_status": "original_complete",
        "close_reason": "declared_original",
        "effective_artifact_id": uuid4(),
        "effective_accession": "0000950123-25-000001",
        "accepted_at": _KNOWN_AT,
        "effective_close_total": Decimal("100"),
        "total_quality": DataQuality.VALID,
        "positions": positions,
    }
    values.update(updates)
    return InstitutionalConcentrationInput(**values)


def test_calculates_exact_declared_concentration_with_context_precision() -> None:
    result = calculate(_input())

    assert result.status == "calculated"
    assert result.position_count == 10
    assert result.largest_declared_weight == Decimal("0.1")
    assert result.top_five_declared_weight == Decimal("0.5")
    assert result.top_ten_declared_weight == Decimal("1.0")
    assert result.herfindahl_index == Decimal("0.10")
    assert result.quality is DataQuality.VALID


def test_pre_2023_shared_monetary_policy_preserves_partial_quality_and_ratios() -> None:
    pre = calculate(
        _input(
            accepted_at=datetime(2022, 12, 31, tzinfo=UTC),
            effective_close_total=Decimal("100000"),
            total_quality=DataQuality.PARTIAL,
        )
    )
    post = calculate(_input())

    assert pre.quality is DataQuality.PARTIAL
    assert pre.largest_declared_weight == post.largest_declared_weight
    assert pre.herfindahl_index == post.herfindahl_index


def test_unresolved_close_and_missing_values_are_omitted_not_defaulted() -> None:
    unresolved = calculate(
        _input(close_status="not_evaluable", close_reason="declared_total_missing")
    )
    missing = calculate(_input(effective_close_total=None, accepted_at=None, total_quality=None))
    zero = calculate(_input(effective_close_total=Decimal("0")))
    empty = calculate(_input(positions=()))

    assert [item.reason for item in (unresolved, missing, zero, empty)] == [
        "unresolved_close",
        "missing_total",
        "zero_total",
        "empty_close",
    ]
    assert all(item.position_count is None for item in (unresolved, missing, zero, empty))


def test_duplicate_declared_position_fails_closed_without_aggregation() -> None:
    duplicate = _position(1, "10")
    result = calculate(_input(positions=(duplicate, duplicate)))

    assert result.status == "omitted"
    assert result.reason == "duplicate_declared_position"
    assert result.largest_declared_weight is None


def test_top_n_is_absent_when_the_close_has_fewer_declared_positions() -> None:
    result = calculate(
        _input(
            effective_close_total=Decimal("40"),
            positions=tuple(_position(number, "10") for number in range(1, 5)),
        )
    )

    assert result.position_count == 4
    assert result.top_five_declared_weight is None
    assert result.top_ten_declared_weight is None
