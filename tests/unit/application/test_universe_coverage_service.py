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


def test_request_accepts_up_to_ten_year_fundamental_range() -> None:
    request = UniverseCoverageRequest(
        known_at=datetime(2026, 8, 29, tzinfo=UTC),
        market_start=date(2025, 8, 28),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2016, 8, 28),
        fundamental_end=date(2026, 8, 28),
    )
    assert (request.fundamental_end - request.fundamental_start).days == 3652
    assert request.frequency == "annual"


def test_request_rejects_more_than_ten_year_fundamental_range() -> None:
    with pytest.raises(ValueError, match="ten years"):
        UniverseCoverageRequest(
            known_at=datetime(2026, 8, 29, tzinfo=UTC),
            market_start=date(2025, 8, 28),
            market_end=date(2026, 8, 28),
            fundamental_start=date(2016, 1, 1),
            fundamental_end=date(2026, 8, 28),
        )


@pytest.mark.parametrize("frequency", ["annual", "quarterly"])
def test_request_accepts_annual_and_quarterly_frequency(frequency: str) -> None:
    request = UniverseCoverageRequest(
        known_at=datetime(2026, 8, 29, tzinfo=UTC),
        market_start=date(2025, 8, 28),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2025, 8, 28),
        fundamental_end=date(2026, 8, 28),
        frequency=frequency,
    )
    assert request.frequency == frequency


def test_request_rejects_unsupported_frequency() -> None:
    with pytest.raises(ValueError, match="frequency must be annual or quarterly"):
        UniverseCoverageRequest(
            known_at=datetime(2026, 8, 29, tzinfo=UTC),
            market_start=date(2025, 8, 28),
            market_end=date(2026, 8, 28),
            fundamental_start=date(2025, 8, 28),
            fundamental_end=date(2026, 8, 28),
            frequency="monthly",
        )
