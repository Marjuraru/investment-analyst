"""Strict no-score contracts for derivatives diagnostics."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.crypto.derivatives_models import (
    CryptoDerivativesDiagnostic,
    CryptoDerivativesDiagnosticStatus,
    DvolDirection,
    FundingDirection,
)


def _payload() -> dict[str, object]:
    return {
        "diagnostic_id": UUID("11111111-1111-5111-8111-111111111111"),
        "asset_id": "crypto:btc-usd",
        "source_ids": (
            "deribit:btc-perpetual:book-summary",
            "deribit:btc-perpetual:funding-rate-history",
            "deribit:btc:dvol:daily",
            "deribit:eth-perpetual:book-summary",
            "deribit:eth-perpetual:funding-rate-history",
            "deribit:eth:dvol:daily",
        ),
        "known_at": datetime(2026, 8, 2, tzinfo=UTC),
        "status": CryptoDerivativesDiagnosticStatus.INSUFFICIENT_DATA,
        "funding_direction": FundingDirection.UNAVAILABLE,
        "dvol_direction": DvolDirection.UNAVAILABLE,
        "observation_ids": (),
        "metric_result_ids": (),
        "missing_requirements": ("diagnostic:funding_sum_168h",),
        "limitations": ("Descriptive only.",),
    }


def test_diagnostic_is_multidimensional_without_score_verdict_or_confidence() -> None:
    diagnostic = CryptoDerivativesDiagnostic.model_validate(_payload())

    document = diagnostic.model_dump(mode="json")
    assert document["status"] == "insufficient_data"
    assert document["funding_direction"] == "unavailable"
    assert document["dvol_direction"] == "unavailable"
    assert not {"score", "confidence", "verdict", "ranking"}.intersection(document)


@pytest.mark.parametrize("forbidden", ["score", "confidence", "verdict", "ranking"])
def test_diagnostic_rejects_aggregate_fields(forbidden: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        CryptoDerivativesDiagnostic.model_validate({**_payload(), forbidden: "synthetic"})
