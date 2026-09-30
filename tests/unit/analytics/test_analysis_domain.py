"""Unit tests for pure analytical domain membership policy."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.analysis_domain import (
    DOMAIN_MEMBERSHIP_POLICY_VERSION,
    AnalysisDomain,
    DomainMembershipError,
    domain_for_metric_key,
    is_authorized_domain,
    metric_prefix_for_domain,
    require_authorized_domain,
    validate_diagnostic_internal_consistency,
    validate_diagnostic_mode_for_domain,
    validate_metric_key_for_domain,
    validate_metric_keys_for_domain,
)
from investment_analyst.core.models.diagnostic import (
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticResult,
)
from investment_analyst.core.models.enums import (
    DataQuality,
    DiagnosticMode,
    DiagnosticVerdict,
    EvidenceDirection,
)


def test_domain_membership_policy_version() -> None:
    assert DOMAIN_MEMBERSHIP_POLICY_VERSION == "analytical-domain-membership-v1"


def test_authorized_domains_and_prefixes() -> None:
    assert is_authorized_domain("market")
    assert is_authorized_domain("fundamental")
    assert is_authorized_domain("valuation")
    assert is_authorized_domain("derivatives")
    assert is_authorized_domain("events")

    assert not is_authorized_domain("funding")
    assert not is_authorized_domain("macro")
    assert not is_authorized_domain("")
    assert not is_authorized_domain("unknown")

    assert metric_prefix_for_domain("market") == "market."
    assert metric_prefix_for_domain("fundamental") == "fundamental."
    assert metric_prefix_for_domain("valuation") == "valuation.corporate."
    assert metric_prefix_for_domain("derivatives") == "crypto.derivatives."
    assert metric_prefix_for_domain("events") == "cazatiburones."


def test_require_authorized_domain_rejects_invalid() -> None:
    with pytest.raises(DomainMembershipError, match="unknown analytical domain"):
        require_authorized_domain("funding")

    with pytest.raises(DomainMembershipError, match="non-empty"):
        require_authorized_domain("")


def test_domain_for_metric_key() -> None:
    assert domain_for_metric_key("market.close_price") == AnalysisDomain.MARKET
    assert domain_for_metric_key("fundamental.revenue") == AnalysisDomain.FUNDAMENTAL
    assert domain_for_metric_key("valuation.corporate.pe_ratio") == AnalysisDomain.VALUATION
    assert domain_for_metric_key("crypto.derivatives.funding_rate") == AnalysisDomain.DERIVATIVES
    assert domain_for_metric_key("cazatiburones.insider_signal") == AnalysisDomain.EVENTS

    with pytest.raises(DomainMembershipError, match="does not belong"):
        domain_for_metric_key("funding.mean_1h")

    with pytest.raises(DomainMembershipError, match="non-empty"):
        domain_for_metric_key("")


def test_validate_metric_key_for_domain() -> None:
    validate_metric_key_for_domain("market.sma_20", "market")
    validate_metric_key_for_domain("fundamental.net_income", AnalysisDomain.FUNDAMENTAL)
    validate_metric_key_for_domain("crypto.derivatives.oi", "derivatives")

    with pytest.raises(DomainMembershipError, match="must start with"):
        validate_metric_key_for_domain("market.sma_20", "fundamental")

    with pytest.raises(DomainMembershipError, match="must start with"):
        validate_metric_key_for_domain("funding.sum_1h", "derivatives")

    validate_metric_keys_for_domain(["market.close", "market.open", "market.volume"], "market")
    with pytest.raises(DomainMembershipError):
        validate_metric_keys_for_domain(["market.close", "fundamental.eps"], "market")


def test_validate_diagnostic_mode_for_domain() -> None:
    validate_diagnostic_mode_for_domain(DiagnosticMode.MARKET, "market")
    validate_diagnostic_mode_for_domain(DiagnosticMode.MARKET, "derivatives")
    validate_diagnostic_mode_for_domain(DiagnosticMode.FUNDAMENTAL, "fundamental")

    with pytest.raises(DomainMembershipError, match="FUNDAMENTAL mode cannot belong"):
        validate_diagnostic_mode_for_domain(DiagnosticMode.FUNDAMENTAL, "market")

    with pytest.raises(DomainMembershipError, match="MARKET mode cannot belong"):
        validate_diagnostic_mode_for_domain(DiagnosticMode.MARKET, "fundamental")


def test_validate_diagnostic_internal_consistency() -> None:
    m1 = uuid4()
    m2 = uuid4()
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    diag = DiagnosticResult(
        diagnostic_id=uuid4(),
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.NEUTRAL,
        final_score=Decimal("50.0"),
        confidence=Decimal("0.8"),
        as_of=now,
        available_at=now,
        computed_at=now,
        components=[
            DiagnosticComponent(
                component_key="trend",
                score=Decimal("50.0"),
                weight=Decimal("1.0"),
                weighted_contribution=Decimal("50.0"),
                metric_result_ids=[m1],
                explanation="Neutral trend",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=m2,
                direction=EvidenceDirection.NEUTRAL,
                contribution=Decimal("0.0"),
                reason="Neutral evidence",
            )
        ],
        algorithm_version="v1",
        summary="summary",
        quality=DataQuality.VALID,
    )

    # Consistent: both market metrics
    keys_market = {m1: "market.sma_20", m2: "market.close"}
    domain = validate_diagnostic_internal_consistency(diag, keys_market)
    assert domain == AnalysisDomain.MARKET

    # Inconsistent / mixed domains
    keys_mixed = {m1: "market.sma_20", m2: "fundamental.eps"}
    with pytest.raises(DomainMembershipError, match="cites mixed metric domains"):
        validate_diagnostic_internal_consistency(diag, keys_mixed)

    # Incompatible mode: MARKET mode with fundamental metric
    keys_fund = {m1: "fundamental.eps", m2: "fundamental.revenue"}
    with pytest.raises(DomainMembershipError, match="MARKET mode cannot belong"):
        validate_diagnostic_internal_consistency(diag, keys_fund)
