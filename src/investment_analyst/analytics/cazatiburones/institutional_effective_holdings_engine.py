"""Pure, deterministic composition of the public-effective 13F row set."""

from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionCandidate,
)
from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveContributor,
    InstitutionalEffectiveHoldingRow,
    InstitutionalEffectiveHoldingsQuery,
    InstitutionalEffectiveHoldingsResult,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
)


def compose(
    *,
    query: InstitutionalEffectiveHoldingsQuery,
    artifacts: tuple[InstitutionalHoldingsSemantics, ...],
) -> InstitutionalEffectiveHoldingsResult:
    visible = tuple(item for item in artifacts if item.available_at <= query.known_at)
    candidates = tuple(
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
            observed_entry_total=len(item.rows),
            observed_value_total=sum((row.value_as_reported for row in item.rows), start=0),
        )
        for item in visible
    )
    base = resolve(
        manager_cik=query.manager_cik,
        report_period=query.report_period,
        known_at=query.known_at,
        candidates=candidates,
    )
    if base.status != "original_complete" and base.status != "amended":
        reason = {
            "insufficient": "composition_insufficient",
            "ambiguous": "composition_ambiguous",
            "not_evaluable": "composition_not_evaluable",
        }[base.status]
        return _unresolved(
            query, base.status if base.status != "original_complete" else "not_evaluable", reason
        )
    originals = [item for item in visible if not item.is_amendment]
    if len(originals) != 1:
        return _unresolved(query, "ambiguous", "composition_ambiguous")
    ordered = sorted(visible, key=lambda item: (item.available_at, item.accession))
    restatements = [item for item in ordered if item.amendment_type == "RESTATEMENT"]
    base_artifact = restatements[-1] if restatements else originals[0]
    supplements = [
        item
        for item in ordered
        if item.amendment_type == "NEW HOLDINGS ENTRIES"
        and item.available_at > base_artifact.available_at
    ]
    contributors = (base_artifact, *supplements)
    if any(not _complete(item) for item in contributors):
        return _unresolved(query, "not_evaluable", "contributor_incomplete")
    contributor_models = tuple(_contributor(item) for item in contributors)
    all_rows = tuple(
        InstitutionalEffectiveHoldingRow(
            source_artifact_id=item.artifact_id,
            source_accession=item.accession,
            source_row_id=row.row_id,
            source_row_number=row.row_number,
            contributor_index=index,
            row=row,
        )
        for index, item in enumerate(contributors)
        for row in item.rows
    )
    page = all_rows[query.offset : query.offset + query.limit]
    limitations = tuple(
        sorted({limitation for item in contributors for limitation in _limitations(item)})
    )
    return InstitutionalEffectiveHoldingsResult(
        manager_cik=query.manager_cik,
        report_period=query.report_period,
        known_at=query.known_at,
        status="effective",
        reason="effective_rows_empty" if not all_rows else "effective_public_disclosure",
        contributors=contributor_models,
        total_rows=len(all_rows),
        offset=query.offset,
        limit=query.limit,
        truncated=query.offset + len(page) < len(all_rows),
        rows=page,
        disclosure_limitations=limitations,
    )


def _complete(item: InstitutionalHoldingsSemantics) -> bool:
    return (
        item.declared_entry_total is not None
        and item.declared_value_total is not None
        and item.declared_entry_total == len(item.rows)
        and item.declared_value_total == sum((row.value_as_reported for row in item.rows), start=0)
    )


def _contributor(item: InstitutionalHoldingsSemantics) -> InstitutionalEffectiveContributor:
    return InstitutionalEffectiveContributor(
        artifact_id=item.artifact_id,
        accession=item.accession,
        is_amendment=item.is_amendment,
        amendment_number=item.amendment_number,
        amendment_type=item.amendment_type,
        available_at=item.available_at,
        report_type=item.report_type,
        confidential_omitted=item.confidential_omitted,
        other_manager_count=len(item.other_managers_included),
    )


def _limitations(item: InstitutionalHoldingsSemantics) -> tuple[str, ...]:
    values = []
    if item.confidential_omitted:
        values.append("confidential_omitted")
    if item.report_type is not None:
        values.append(f"report_type:{item.report_type}")
    if item.other_managers_included:
        values.append("other_managers_included")
    return tuple(values)


def _unresolved(query, status, reason) -> InstitutionalEffectiveHoldingsResult:
    return InstitutionalEffectiveHoldingsResult(
        manager_cik=query.manager_cik,
        report_period=query.report_period,
        known_at=query.known_at,
        status=status,
        reason=reason,
        total_rows=0,
        offset=query.offset,
        limit=query.limit,
        truncated=False,
    )
