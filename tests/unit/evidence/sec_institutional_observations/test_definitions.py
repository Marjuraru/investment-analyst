from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_analyst.evidence.sec_institutional_observations.definitions import (
    USD_FROM,
    monetary_value,
)


def test_monetary_policy_uses_filing_acceptance_boundary_only() -> None:
    before = USD_FROM - timedelta(microseconds=1)

    assert monetary_value(Decimal("7"), accepted_at=before) == (Decimal("7000"), "partial")
    assert monetary_value(Decimal("7"), accepted_at=USD_FROM) == (Decimal("7"), "valid")
    assert monetary_value(Decimal("7"), accepted_at=datetime(2028, 1, 1, tzinfo=UTC)) == (
        Decimal("7"),
        "valid",
    )
