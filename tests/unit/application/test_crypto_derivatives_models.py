"""Public inclusive-range and strict request/summary contract tests."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesQueryRequest,
    CryptoDerivativesRefreshRequest,
    public_date_bounds,
)


def test_public_dates_convert_to_half_open_utc_without_losing_final_date() -> None:
    start, end = public_date_bounds(date(2026, 8, 1), date(2026, 8, 7))

    assert start == datetime(2026, 8, 1, tzinfo=UTC)
    assert end == datetime(2026, 8, 8, tzinfo=UTC)


def test_requests_reject_datetime_dates_naive_known_at_and_extra_fields() -> None:
    with pytest.raises(ValidationError, match="must be dates"):
        CryptoDerivativesRefreshRequest(
            asset_id="crypto:btc-usd",
            start_date=datetime(2026, 8, 1, tzinfo=UTC),
            end_date=date(2026, 8, 2),
        )
    with pytest.raises(ValidationError, match="timezone"):
        CryptoDerivativesQueryRequest(
            asset_id="crypto:btc-usd",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            known_at=datetime(2026, 8, 3),
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        CryptoDerivativesRefreshRequest.model_validate(
            {
                "asset_id": "crypto:btc-usd",
                "start_date": "2026-08-01",
                "end_date": "2026-08-02",
                "trading": True,
            }
        )
