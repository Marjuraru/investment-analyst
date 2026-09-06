"""Vocabulary for the ephemeral public-effective Form 13F projection."""

from typing import Literal

SEC_13F_EFFECTIVE_PUBLIC_HOLDINGS_POLICY_VERSION = "sec-13f-effective-public-holdings-policy-v1"

InstitutionalEffectiveHoldingsStatus = Literal[
    "effective", "insufficient", "not_evaluable", "ambiguous"
]
InstitutionalEffectiveHoldingsReason = Literal[
    "effective_public_disclosure",
    "composition_insufficient",
    "composition_ambiguous",
    "composition_not_evaluable",
    "contributor_incomplete",
    "no_visible_artifact",
    "effective_rows_empty",
]
