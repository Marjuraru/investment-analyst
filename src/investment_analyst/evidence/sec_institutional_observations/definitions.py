"""Closed field and unit policy for institutional 13F observations."""

from datetime import UTC, datetime
from decimal import Decimal

SOURCE_ID = "sec-edgar:institutional-holdings-observations"
TRANSFORMATION_VERSION = "sec-institutional-observation-normalizer-v1"
MONETARY_POLICY_VERSION = "sec-13f-monetary-policy-v1"
USD_FROM = datetime(2023, 1, 3, tzinfo=UTC)


def monetary_value(value: Decimal, *, accepted_at: datetime) -> tuple[Decimal, str]:
    """Return official USD value and its disclosed precision quality."""
    return (value, "valid") if accepted_at >= USD_FROM else (value * Decimal("1000"), "partial")
