from datetime import UTC, datetime
from decimal import Decimal

import pytest

from investment_analyst.evidence.sec_institutional_observations.definitions import monetary_value


@pytest.mark.parametrize(
    ("accepted_at", "value", "quality"),
    [
        (datetime(2023, 1, 2, 23, 59, tzinfo=UTC), Decimal("1000"), "partial"),
        (datetime(2023, 1, 3, tzinfo=UTC), Decimal("1"), "valid"),
    ],
)
def test_normalizer_monetary_values_are_decimal_exact(
    accepted_at: datetime, value: Decimal, quality: str
) -> None:
    actual, actual_quality = monetary_value(Decimal("1"), accepted_at=accepted_at)

    assert actual == value
    assert actual_quality == quality
