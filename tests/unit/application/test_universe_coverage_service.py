"""Service-level invariants for local coverage composition."""

from datetime import UTC, date, datetime

import pytest

from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest
from investment_analyst.application.universe_coverage_service import (
    fundamental_frequency_supported,
)
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import (
    SecAccountingStandard,
    SecAssetConfiguration,
    sec_source_ids,
)


def _sec_configuration(
    ticker: str,
    cik: str,
    accounting_standard: SecAccountingStandard,
) -> SecAssetConfiguration:
    submissions, companyfacts = sec_source_ids(ticker)
    return SecAssetConfiguration(
        asset_id=f"equity:us:{ticker.lower()}",
        cik=cik,
        ticker=ticker,
        submissions_source_id=submissions,
        companyfacts_source_id=companyfacts,
        name=ticker,
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NYSE",
        accounting_standard=accounting_standard,
    )


def test_ifrs_issuer_declares_only_annual_fundamental_frequency() -> None:
    configuration = _sec_configuration(
        "TSM",
        "0001046179",
        SecAccountingStandard.IFRS,
    )

    assert fundamental_frequency_supported(configuration, "annual") is True
    assert fundamental_frequency_supported(configuration, "quarterly") is False


def test_us_gaap_issuer_declares_annual_and_quarterly_fundamental_frequency() -> None:
    configuration = _sec_configuration(
        "AMD",
        "0000002488",
        SecAccountingStandard.US_GAAP,
    )

    assert fundamental_frequency_supported(configuration, "annual") is True
    assert fundamental_frequency_supported(configuration, "quarterly") is True


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
