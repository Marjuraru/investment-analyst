"""Service-level invariants for local coverage composition."""

from datetime import UTC, date, datetime

import pytest

from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest


def test_request_rejects_more_than_one_year_of_market_days() -> None:
    with pytest.raises(ValueError, match="366"):
        UniverseCoverageRequest(
            known_at=datetime(2026, 8, 29, tzinfo=UTC),
            market_start=date(2025, 8, 1),
            market_end=date(2026, 8, 28),
            fundamental_start=date(2020, 1, 1),
            fundamental_end=date(2026, 8, 28),
        )
