"""Contracts for bounded deterministic coverage requests."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest


def _request(**changes: object) -> UniverseCoverageRequest:
    values: dict[str, object] = {
        "known_at": datetime(2026, 8, 29, tzinfo=UTC),
        "market_start": date(2026, 8, 1),
        "market_end": date(2026, 8, 28),
        "fundamental_start": date(2020, 1, 1),
        "fundamental_end": date(2026, 8, 28),
    }
    values.update(changes)
    return UniverseCoverageRequest(**values)


def test_request_is_bounded_and_deterministic() -> None:
    request = _request(asset_ids=("crypto:sol-usd", "equity:us:msft"))

    assert request.frequency == "annual"
    assert request.asset_ids == ("crypto:sol-usd", "equity:us:msft")


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_ids": ("equity:us:msft", "equity:us:msft")},
        {"asset_ids": ("equity:us:msft", "crypto:sol-usd")},
        {"frequency": "monthly"},
        {"market_end": date(2026, 8, 30)},
        {
            "known_at": datetime(2026, 8, 28, 12, tzinfo=UTC),
            "market_start": date(2026, 8, 28),
            "market_end": date(2026, 8, 28),
        },
    ],
)
def test_request_rejects_invalid_selection_and_ranges(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _request(**changes)
