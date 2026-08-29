from datetime import UTC, date, datetime
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.institutional_change_engine import compare
from investment_analyst.analytics.cazatiburones.institutional_change_models import (
    InstitutionalClose,
    InstitutionalPosition,
)


def test_read_only_descriptive_change_is_point_in_time() -> None:
    previous = InstitutionalClose(
        manager_cik="0001067983",
        report_period=date(2024, 9, 30),
        available_at=datetime(2024, 11, 14, tzinfo=UTC),
        declared_value_total=Decimal("100"),
        positions=(
            InstitutionalPosition(
                cusip="037833100", title_of_class="COM", quantity=Decimal("1"), value=Decimal("20")
            ),
        ),
    )
    current = previous.model_copy(
        update={
            "report_period": date(2024, 12, 31),
            "available_at": datetime(2025, 2, 14, tzinfo=UTC),
            "positions": (
                InstitutionalPosition(
                    cusip="037833100",
                    title_of_class="COM",
                    quantity=Decimal("2"),
                    value=Decimal("30"),
                ),
            ),
        }
    )
    result = compare(previous, current)

    assert result.available_at == datetime(2025, 2, 14, tzinfo=UTC)
    assert {item.key: item.value for item in result.metrics}["delta_value"] == Decimal("10")
