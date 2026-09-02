"""Versioned vocabulary for the 13F as-filed close projection."""

from typing import Literal

SEC_13F_COMPOSITION_POLICY_VERSION = "sec-13f-composition-policy-v1"
SEC_13F_AMENDMENT_TYPES = frozenset({"RESTATEMENT", "NEW HOLDINGS ENTRIES"})

InstitutionalCompositionStatus = Literal[
    "original_complete",
    "amended",
    "insufficient",
    "not_evaluable",
    "ambiguous",
]
InstitutionalCompositionReason = Literal[
    "available_at_tie",
    "amendment_chain_incomplete",
    "amendment_number_missing",
    "contradictory_amendment_chain",
    "declared_amendment_new_holdings_entries",
    "declared_amendment_restatement",
    "declared_original",
    "declared_total_mismatch",
    "declared_total_missing",
    "invalid_amendment_number",
    "missing_or_conflicting_report_period",
    "missing_original_artifact",
    "no_visible_artifact",
    "observed_total_missing",
    "unknown_amendment_type",
]
