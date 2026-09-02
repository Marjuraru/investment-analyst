"""Versioned policy definitions for declared 13F close concentration."""

from typing import Literal

SEC_13F_CONCENTRATION_POLICY_VERSION = "sec-13f-concentration-policy-v1"

InstitutionalConcentrationStatus = Literal["calculated", "omitted"]
InstitutionalConcentrationReason = Literal[
    "calculated",
    "unresolved_close",
    "missing_total",
    "zero_total",
    "empty_close",
    "duplicate_declared_position",
]

DECLARED_CONCENTRATION_FORMULAS = {
    "position_count": "count(as_filed_rows)",
    "largest_declared_weight": "max(row_value / effective_close_value_total)",
    "top_five_declared_weight": "sum(five_largest(row_value / effective_close_value_total))",
    "top_ten_declared_weight": "sum(ten_largest(row_value / effective_close_value_total))",
    "herfindahl_index": "sum((row_value / effective_close_value_total) ** 2)",
}
