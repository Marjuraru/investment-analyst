from datetime import UTC, date, datetime
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.institutional_change_engine import (
    compare,
    robust_baseline,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import (
    InstitutionalClose,
    InstitutionalPosition,
)


def _close(period, quantity, value):
    return InstitutionalClose(
        manager_cik="0001067983",
        report_period=period,
        available_at=datetime(2025, 2, 15, tzinfo=UTC),
        declared_value_total=Decimal("100"),
        positions=(
            InstitutionalPosition(
                cusip="037833100", title_of_class="COM", quantity=quantity, value=value
            ),
        ),
    )


def test_decimal_deltas_concentration_and_baseline_are_descriptive() -> None:
    result = compare(
        _close(date(2024, 9, 30), Decimal("10"), Decimal("40")),
        _close(date(2024, 12, 31), Decimal("15"), Decimal("60")),
    )
    values = {item.key: item.value for item in result.metrics}
    assert values["delta_quantity"] == Decimal("5")
    assert values["delta_value"] == Decimal("20")
    assert values["position_concentration"] == Decimal("0.6")
    assert all(
        item.status == "not_evaluable"
        for item in robust_baseline((Decimal("1"), Decimal("2")), Decimal("2"))
    )
