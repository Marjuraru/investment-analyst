"""Contract tests for multi-asset market-comparison requests and results."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.comparison_models import MarketComparisonRequest


def test_request_canonicalizes_benchmark_then_sorted_peers() -> None:
    request = MarketComparisonRequest(
        asset_ids=("equity:us:msft", "equity:us:aapl", "equity:us:spy"),
        benchmark_id="equity:us:spy",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 2, 1),
        known_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    assert request.canonical_asset_ids == (
        "equity:us:spy",
        "equity:us:aapl",
        "equity:us:msft",
    )
    assert request.end_exclusive == datetime(2026, 2, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("asset_ids", "benchmark_id"),
    [
        (("equity:us:aapl",), "equity:us:aapl"),
        (("equity:us:aapl", "equity:us:aapl"), "equity:us:aapl"),
        (("equity:us:aapl", "equity:us:spy"), "equity:us:msft"),
    ],
)
def test_request_rejects_invalid_asset_scope(
    asset_ids: tuple[str, ...],
    benchmark_id: str,
) -> None:
    with pytest.raises(ValidationError):
        MarketComparisonRequest(
            asset_ids=asset_ids,
            benchmark_id=benchmark_id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 2, 1),
            known_at=datetime(2026, 2, 2, tzinfo=UTC),
        )


def test_request_rejects_naive_known_at() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        MarketComparisonRequest(
            asset_ids=("equity:us:aapl", "equity:us:spy"),
            benchmark_id="equity:us:spy",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 2, 1),
            known_at=datetime(2026, 2, 2),
        )
