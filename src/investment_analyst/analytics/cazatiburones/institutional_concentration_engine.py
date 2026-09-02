"""Pure Decimal-exact declared concentration calculation for one 13F close."""

from decimal import Context, Decimal

from investment_analyst.analytics.cazatiburones.institutional_concentration_definitions import (
    InstitutionalConcentrationReason,
)
from investment_analyst.analytics.cazatiburones.institutional_concentration_models import (
    InstitutionalConcentrationInput,
    InstitutionalConcentrationResult,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import monetary_value

_CONTEXT = Context(prec=28)


def calculate(item: InstitutionalConcentrationInput) -> InstitutionalConcentrationResult:
    """Calculate only from one selected as-filed close; never aggregate positions."""
    if item.close_status not in {"original_complete", "amended"}:
        return _omitted(item, "unresolved_close")
    if item.effective_close_total is None or item.accepted_at is None:
        return _omitted(item, "missing_total")
    if item.effective_close_total == 0:
        return _omitted(item, "zero_total")
    if not item.positions:
        return _omitted(item, "empty_close")
    if len({position.declared_position_key for position in item.positions}) != len(item.positions):
        return _omitted(item, "duplicate_declared_position")

    weights = tuple(
        _CONTEXT.divide(
            monetary_value(position.value_as_reported, accepted_at=item.accepted_at)[0],
            item.effective_close_total,
        )
        for position in item.positions
    )
    ordered = tuple(sorted(weights, reverse=True))
    return InstitutionalConcentrationResult(
        manager_cik=item.manager_cik,
        report_period=item.report_period,
        known_at=item.known_at,
        status="calculated",
        reason="calculated",
        close_status=item.close_status,
        close_reason=item.close_reason,
        effective_artifact_id=item.effective_artifact_id,
        effective_accession=item.effective_accession,
        quality=item.total_quality,
        position_count=len(weights),
        largest_declared_weight=ordered[0],
        top_five_declared_weight=_sum(ordered[:5]) if len(ordered) >= 5 else None,
        top_ten_declared_weight=_sum(ordered[:10]) if len(ordered) >= 10 else None,
        herfindahl_index=_sum(tuple(_CONTEXT.multiply(weight, weight) for weight in weights)),
    )


def _sum(values: tuple[Decimal, ...]) -> Decimal:
    total = Decimal("0")
    for value in values:
        total = _CONTEXT.add(total, value)
    return total


def _omitted(
    item: InstitutionalConcentrationInput,
    reason: InstitutionalConcentrationReason,
) -> InstitutionalConcentrationResult:
    return InstitutionalConcentrationResult(
        manager_cik=item.manager_cik,
        report_period=item.report_period,
        known_at=item.known_at,
        status="omitted",
        reason=reason,
        close_status=item.close_status,
        close_reason=item.close_reason,
        effective_artifact_id=item.effective_artifact_id,
        effective_accession=item.effective_accession,
    )
