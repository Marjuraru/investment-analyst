"""Contract tests for explicit fixed annual horizons."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.fundamentals.annual_horizon_models import (
    ANNUAL_HORIZON_MAX_GAP_DAYS,
    ANNUAL_HORIZON_MIN_GAP_DAYS,
    FundamentalAnnualHorizonPoint,
    FundamentalAnnualHorizonRow,
    FundamentalAnnualHorizonsRequest,
)


def _request(**updates: object) -> FundamentalAnnualHorizonsRequest:
    values: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "known_at": datetime(2026, 10, 29, 19, 47, tzinfo=UTC),
        "end_period_end": date(2025, 12, 31),
        "metric_keys": (
            "fundamental.research.return_on_assets_ending_balance",
            "fundamental.research.free_cash_flow",
        ),
    }
    values.update(updates)
    return FundamentalAnnualHorizonsRequest.model_validate(values)


def test_request_defaults_and_canonical_order() -> None:
    request = _request(
        metric_keys=(
            "fundamental.research.return_on_assets_ending_balance",
            "fundamental.research.free_cash_flow",
            "fundamental.research.diluted_eps",
        )
    )

    assert request.horizons == (3, 5, 10)
    assert request.metric_keys == tuple(sorted(request.metric_keys))
    assert request.schema_version == "fundamental-annual-horizons-request-v1"
    assert request.end_period_end == date(2025, 12, 31)


def test_request_rejects_unknown_metric_keys() -> None:
    with pytest.raises(ValidationError, match="unsupported fundamental research metric"):
        _request(metric_keys=("fundamental.research.not_a_metric",))


def test_request_rejects_more_than_six_metrics() -> None:
    keys = tuple(
        definition
        for definition in (
            "fundamental.research.asset_turnover",
            "fundamental.research.cash_ratio",
            "fundamental.research.current_ratio",
            "fundamental.research.diluted_eps",
            "fundamental.research.free_cash_flow",
            "fundamental.research.gross_margin",
            "fundamental.research.net_margin",
        )
    )

    with pytest.raises(ValidationError):
        _request(metric_keys=keys)


@pytest.mark.parametrize(
    "horizons",
    [(), (4,), (3, 3), (3, 5, 10, 5)],
)
def test_request_rejects_horizons_outside_the_published_subset(
    horizons: tuple[int, ...],
) -> None:
    with pytest.raises(ValidationError):
        _request(horizons=horizons)


def test_request_rejects_an_anchor_after_the_known_at_cut() -> None:
    with pytest.raises(ValidationError, match="anchor must not be later"):
        _request(
            known_at=datetime(2025, 12, 30, 23, 59, tzinfo=UTC),
            end_period_end=date(2025, 12, 31),
        )


def test_request_accepts_the_anchor_at_the_cut_date() -> None:
    request = _request(
        known_at=datetime(2025, 12, 31, 0, 0, tzinfo=UTC),
        end_period_end=date(2025, 12, 31),
    )

    assert request.end_period_end == date(2025, 12, 31)


def test_point_rejects_float_financial_values() -> None:
    with pytest.raises(ValidationError, match="Decimal"):
        FundamentalAnnualHorizonPoint(
            period_end=datetime(2025, 12, 31, tzinfo=UTC),
            value=1.5,  # type: ignore[arg-type]
            unit="USD",
            available_at=datetime(2026, 1, 30, tzinfo=UTC),
            metric_algorithm_version="fixture",
            input_observation_ids=(),
        )


def test_complete_row_requires_statistics() -> None:
    with pytest.raises(ValidationError, match="requires statistics"):
        FundamentalAnnualHorizonRow(
            horizon_years=3,
            status="complete",
            required_points=4,
            observed_points=4,
            period_ends=(
                datetime(2022, 12, 31, tzinfo=UTC),
                datetime(2023, 12, 30, tzinfo=UTC),
                datetime(2024, 12, 28, tzinfo=UTC),
                datetime(2025, 12, 27, tzinfo=UTC),
            ),
            elapsed_days=(date(2025, 12, 27) - date(2022, 12, 31)).days,
        )


def test_incomplete_row_cannot_publish_statistics_or_cagr() -> None:
    with pytest.raises(ValidationError, match="cannot publish statistics"):
        FundamentalAnnualHorizonRow(
            horizon_years=10,
            status="insufficient_history",
            required_points=11,
            observed_points=2,
            period_ends=(
                datetime(2024, 12, 28, tzinfo=UTC),
                datetime(2025, 12, 27, tzinfo=UTC),
            ),
            elapsed_days=364,
            cagr_reason="calculated",
        )


def test_nonconsecutive_row_requires_its_exact_segment() -> None:
    with pytest.raises(ValidationError, match="exact point count"):
        FundamentalAnnualHorizonRow(
            horizon_years=3,
            status="nonconsecutive_periods",
            required_points=4,
            observed_points=3,
            period_ends=(
                datetime(2023, 12, 30, tzinfo=UTC),
                datetime(2024, 12, 28, tzinfo=UTC),
                datetime(2025, 12, 27, tzinfo=UTC),
            ),
            elapsed_days=(date(2025, 12, 27) - date(2023, 12, 30)).days,
        )


def test_gap_bounds_cover_52_53_week_closes() -> None:
    four_week_gap = (date(2024, 12, 28) - date(2023, 12, 30)).days
    assert ANNUAL_HORIZON_MIN_GAP_DAYS <= four_week_gap <= ANNUAL_HORIZON_MAX_GAP_DAYS
    assert ANNUAL_HORIZON_MIN_GAP_DAYS == 330
    assert ANNUAL_HORIZON_MAX_GAP_DAYS == 400


def test_row_rejects_inconsistent_elapsed_days() -> None:
    with pytest.raises(ValidationError, match="elapsed days"):
        FundamentalAnnualHorizonRow(
            horizon_years=3,
            status="anchor_unavailable",
            required_points=4,
            observed_points=2,
            period_ends=(
                datetime(2024, 12, 28, tzinfo=UTC),
                datetime(2025, 12, 27, tzinfo=UTC),
            ),
            elapsed_days=10,
        )


def test_point_rejects_non_finite_and_duplicate_evidence() -> None:
    with pytest.raises(ValidationError, match="finite"):
        FundamentalAnnualHorizonPoint(
            period_end=datetime(2025, 12, 31, tzinfo=UTC),
            value=Decimal("NaN"),
            unit="USD",
            available_at=datetime(2026, 1, 30, tzinfo=UTC),
            metric_algorithm_version="fixture",
            input_observation_ids=(),
        )
