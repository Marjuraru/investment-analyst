"""Pure construction of composition candidates from as-filed semantic artifacts."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionCandidate,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
)


def candidates_by_period(
    artifacts: tuple[InstitutionalHoldingsSemantics, ...], *, manager_cik: str
) -> dict[date | None, tuple[InstitutionalCompositionCandidate, ...]]:
    """Build exact as-filed totals without storage, clocks, or implicit defaults."""
    grouped: defaultdict[date | None, list[InstitutionalCompositionCandidate]] = defaultdict(list)
    for item in artifacts:
        if item.manager_cik != manager_cik:
            continue
        grouped[item.report_period].append(
            InstitutionalCompositionCandidate(
                artifact_id=item.artifact_id,
                accession=item.accession,
                manager_cik=item.manager_cik,
                report_period=item.report_period,
                available_at=item.available_at,
                is_amendment=item.is_amendment,
                amendment_number=item.amendment_number,
                amendment_type=item.amendment_type,
                declared_entry_total=item.declared_entry_total,
                declared_value_total=item.declared_value_total,
                observed_entry_total=len(item.rows) if item.rows else None,
                observed_value_total=(
                    sum((row.value_as_reported for row in item.rows), Decimal("0"))
                    if item.rows
                    else None
                ),
            )
        )
    return {period: tuple(values) for period, values in grouped.items()}
