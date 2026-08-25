"""Tests for the generic listed-company report contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.listed_company_report_models import (
    ListedCompanyReportAsset,
    ListedCompanyReportRequest,
)
from investment_analyst.core.models import DataFrequency


def test_generic_request_requires_an_explicit_issuer_and_normalizes_utc() -> None:
    request = ListedCompanyReportRequest(
        asset_id="equity:us:amd",
        known_at=datetime(2026, 8, 1, tzinfo=UTC),
        fundamental_frequency=DataFrequency.QUARTERLY,
    )

    assert request.asset_id == "equity:us:amd"
    assert request.known_at == datetime(2026, 8, 1, tzinfo=UTC)

    with pytest.raises(ValidationError, match="Field required"):
        ListedCompanyReportRequest(
            known_at=datetime(2026, 8, 1, tzinfo=UTC),
            fundamental_frequency=DataFrequency.QUARTERLY,
        )


def test_report_asset_rejects_non_deterministic_source_ids() -> None:
    with pytest.raises(ValidationError, match="unique, and sorted"):
        ListedCompanyReportAsset(
            asset_id="equity:us:amd",
            symbol="AMD",
            name="Advanced Micro Devices",
            source_ids=("sec:companyfacts", "alpaca:amd", "sec:companyfacts"),
        )
