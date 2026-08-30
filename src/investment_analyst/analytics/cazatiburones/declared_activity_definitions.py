# ruff: noqa: E501
"""Versioned definitions for descriptive SEC declared-activity features."""

from decimal import Decimal

from pydantic import ConfigDict

from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class DeclaredActivityFeatureDefinition(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: NonEmptyStr
    category: NonEmptyStr
    formula: NonEmptyStr
    unit: NonEmptyStr
    parameters: tuple[NonEmptyStr, ...]
    limitations: NonEmptyStr
    definition_version: NonEmptyStr


DEFINITION_VERSION = "declared-activity-features-v1"
CLUSTER_WINDOW_DAYS = 30
ZERO = Decimal("0")
FEATURE_DEFINITIONS = tuple(
    DeclaredActivityFeatureDefinition(
        key=key,
        category=category,
        formula=formula,
        unit=unit,
        parameters=parameters,
        limitations=limitations,
        definition_version=DEFINITION_VERSION,
    )
    for key, category, formula, unit, parameters, limitations in (
        ("transaction_shares", "insider", "declared shares", "shares", (), "missing when absent"),
        (
            "prior_holding",
            "insider",
            "previous declared holding",
            "shares",
            (),
            "missing when absent",
        ),
        (
            "post_holding",
            "insider",
            "declared post-transaction holding",
            "shares",
            (),
            "missing when absent",
        ),
        (
            "holding_delta_ratio",
            "insider",
            "(post - prior) / prior",
            "ratio",
            (),
            "missing for zero or absent prior",
        ),
        (
            "acquisition_count",
            "insider",
            "count(acquired_disposed == A)",
            "count",
            (),
            "declared code only",
        ),
        (
            "disposition_count",
            "insider",
            "count(acquired_disposed == D)",
            "count",
            (),
            "declared code only",
        ),
        (
            "clustered_transaction_count",
            "insider",
            "count within declared day window",
            "count",
            ("window_days=30",),
            "same participant/security/table only",
        ),
        (
            "participant_recurrence",
            "insider",
            "distinct statements for participant",
            "count",
            (),
            "history available at cut",
        ),
        (
            "delta_percent_of_class",
            "beneficial",
            "current percent - previous percent",
            "percentage_points",
            (),
            "consecutive declarations only",
        ),
        (
            "delta_shares_beneficially_owned",
            "beneficial",
            "current shares - previous shares",
            "shares",
            (),
            "consecutive declarations only",
        ),
        (
            "threshold_appearance",
            "beneficial",
            "present(current) and absent(previous)",
            "boolean",
            (),
            "declared presence only",
        ),
        (
            "threshold_exit",
            "beneficial",
            "absent(current) and present(previous)",
            "boolean",
            (),
            "declared presence only",
        ),
        ("is_amendment", "filing", "form ends with /A", "boolean", (), "literal form only"),
        (
            "filing_delay_days",
            "filing",
            "available_at.date - declared date",
            "days",
            (),
            "missing without declared date",
        ),
    )
)
