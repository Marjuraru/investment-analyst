"""Unit tests for institutional composition v2 definitions."""

from investment_analyst.analytics.cazatiburones.institutional_composition_v2_definitions import (
    SEC_13F_COMPOSITION_V2_POLICY_VERSION,
    SEC_13F_V2_SOURCE_LITERALS,
    SOURCE_LITERAL_TO_OPERATION,
)


def test_composition_v2_distinguishes_source_literal_and_operation() -> None:
    """Validate that v2 explicitly maps source literals to analytical operations."""
    assert SEC_13F_COMPOSITION_V2_POLICY_VERSION == "sec-13f-composition-policy-v2"
    assert SOURCE_LITERAL_TO_OPERATION["RESTATEMENT"] == "replacement"
    assert SOURCE_LITERAL_TO_OPERATION["NEW HOLDINGS"] == "supplement"


def test_composition_v2_recognizes_official_new_holdings_and_restatement() -> None:
    """Validate that v2 recognizes official SEC Form 13F XML literals."""
    assert frozenset({"RESTATEMENT", "NEW HOLDINGS"}) == SEC_13F_V2_SOURCE_LITERALS


def test_legacy_new_holdings_entries_rejected_in_v2() -> None:
    """Validate that legacy NEW HOLDINGS ENTRIES is not recognized in v2 source literals."""
    assert "NEW HOLDINGS ENTRIES" not in SEC_13F_V2_SOURCE_LITERALS


def test_v2_never_presents_new_holdings_entries_as_sec_literal() -> None:
    """Validate that v2 source literals only include actual SEC XML values."""
    for literal in SEC_13F_V2_SOURCE_LITERALS:
        assert literal in {"RESTATEMENT", "NEW HOLDINGS"}
        assert literal != "NEW HOLDINGS ENTRIES"
