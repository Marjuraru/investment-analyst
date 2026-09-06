"""Versioned vocabulary for the Form 13F composition policy v2.

Distinguishes official SEC 13F amendment type source literals from analytical
operations, rejecting legacy 'NEW HOLDINGS ENTRIES' as an official literal.
"""

from typing import Literal

SEC_13F_COMPOSITION_V2_POLICY_VERSION = "sec-13f-composition-policy-v2"

# Official SEC Form 13F amendment types as extracted from EDGAR XML submissions
SEC_13F_V2_SOURCE_LITERALS = frozenset({"RESTATEMENT", "NEW HOLDINGS"})

Sec13fV2SourceLiteral = Literal["RESTATEMENT", "NEW HOLDINGS"]
Sec13fV2Operation = Literal["replacement", "supplement"]

InstitutionalCompositionV2Status = Literal[
    "original_complete",
    "amended",
    "insufficient",
    "not_evaluable",
    "ambiguous",
]

InstitutionalCompositionV2Reason = Literal[
    "available_at_tie",
    "amendment_chain_incomplete",
    "amendment_number_missing",
    "contradictory_amendment_chain",
    "declared_amendment_new_holdings",
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

SOURCE_LITERAL_TO_OPERATION: dict[Sec13fV2SourceLiteral, Sec13fV2Operation] = {
    "RESTATEMENT": "replacement",
    "NEW HOLDINGS": "supplement",
}
