"""Unit tests for strict SEC issuer refresh contracts."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
    SecIssuerFundamentalRefreshSummary,
)
from investment_analyst.core.models import DataFrequency, DiagnosticVerdict

_KNOWN_AT = datetime(2026, 7, 28, 15, tzinfo=UTC)
_FETCHED_AT = datetime(2026, 7, 28, 14, 55, tzinfo=UTC)
_NORMALIZED_AT = datetime(2026, 7, 28, 14, 56, tzinfo=UTC)
_PERIOD_END = datetime(2025, 12, 27, tzinfo=UTC)


def _request() -> SecIssuerFundamentalRefreshRequest:
    return SecIssuerFundamentalRefreshRequest(
        asset_id="equity:us:amd",
        frequency=DataFrequency.ANNUAL,
        requested_known_at=_KNOWN_AT,
    )


def _summary(**updates: object) -> SecIssuerFundamentalRefreshSummary:
    values: dict[str, object] = {
        "asset_id": "equity:us:amd",
        "source_id": "sec-edgar:amd:companyfacts",
        "request": _request(),
        "effective_known_at": _KNOWN_AT,
        "fetched_at": _FETCHED_AT,
        "normalized_at": _NORMALIZED_AT,
        "documents_received": 2,
        "raw_records_created": 1,
        "raw_records_reused": 1,
        "facts_examined": 5,
        "facts_selected": 2,
        "observations_generated": 2,
        "observations_created": 1,
        "observations_reused": 1,
        "annual_observations": 2,
        "quarterly_observations": 0,
        "observation_field_counts": {
            "fundamental.net_income": 1,
            "fundamental.revenue": 1,
        },
        "observation_skipped_counts": {"missing_tag": 3},
        "target_periods": 1,
        "metric_results_created": 0,
        "metric_results_reused": 1,
        "metric_counts": {"fundamental.net_margin": 1},
        "metric_skipped_counts": {},
        "diagnostic_target_period_end": _PERIOD_END,
        "diagnostic_verdict": DiagnosticVerdict.INSUFFICIENT_DATA,
        "diagnostic_coverage": Decimal("0.3"),
        "diagnostic_missing_requirements": ("fundamental.liabilities_to_assets",),
        "diagnostics_created": 0,
        "diagnostics_reused": 1,
        "traceability_verified": True,
    }
    values.update(updates)
    return SecIssuerFundamentalRefreshSummary(**values)


def test_summary_preserves_exact_decimal_and_explicit_context() -> None:
    summary = _summary()
    payload = summary.to_json_dict()

    assert payload["schema_version"] == "sec-issuer-fundamental-refresh-v1"
    assert payload["asset_id"] == "equity:us:amd"
    assert payload["diagnostic_coverage"] == "0.3"
    assert payload["request"]["requested_known_at"] == "2026-07-28T15:00:00Z"


def test_request_rejects_nonfundamental_frequency() -> None:
    with pytest.raises(ValidationError, match="annual or quarterly"):
        SecIssuerFundamentalRefreshRequest(
            asset_id="equity:us:amd",
            frequency=DataFrequency.DAY_1,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"asset_id": "equity:us:intc"},
        {"raw_records_reused": 0},
        {"observations_reused": 0},
        {"annual_observations": 1},
        {"metric_results_reused": 0},
        {"diagnostics_reused": 0},
        {"traceability_verified": False},
    ],
)
def test_summary_rejects_inconsistent_stage_counts(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _summary(**updates)


def test_summary_rejects_binary_float_coverage() -> None:
    with pytest.raises(ValidationError, match="must not use float"):
        _summary(diagnostic_coverage=0.3)
