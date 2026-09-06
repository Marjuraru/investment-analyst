"""Pure, deterministic, fail-closed selection of as-filed 13F closes v2."""

from datetime import date, datetime
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_composition_v2_definitions import (
    SEC_13F_V2_SOURCE_LITERALS,
    SOURCE_LITERAL_TO_OPERATION,
    InstitutionalCompositionV2Reason,
    InstitutionalCompositionV2Status,
    Sec13fV2Operation,
    Sec13fV2SourceLiteral,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_v2_models import (
    InstitutionalCompositionV2Candidate,
    InstitutionalCompositionV2Result,
)


def resolve_v2(
    *,
    manager_cik: str,
    report_period: date | None,
    known_at: datetime,
    candidates: tuple[InstitutionalCompositionV2Candidate, ...],
) -> InstitutionalCompositionV2Result:
    visible = tuple(item for item in candidates if item.available_at <= known_at)
    if not visible:
        return _result(manager_cik, report_period, known_at, "insufficient", "no_visible_artifact")
    if report_period is None or any(
        item.report_period != report_period or item.manager_cik != manager_cik for item in visible
    ):
        return _result(
            manager_cik,
            report_period,
            known_at,
            "ambiguous",
            "missing_or_conflicting_report_period",
        )
    if any(
        item.is_amendment and item.amendment_type not in SEC_13F_V2_SOURCE_LITERALS
        for item in visible
    ):
        return _result(manager_cik, report_period, known_at, "ambiguous", "unknown_amendment_type")
    if _has_available_at_tie(visible):
        return _result(manager_cik, report_period, known_at, "ambiguous", "available_at_tie")
    amendments = tuple(item for item in visible if item.is_amendment)
    originals = tuple(item for item in visible if not item.is_amendment)
    if len(originals) > 1:
        return _result(
            manager_cik, report_period, known_at, "ambiguous", "contradictory_amendment_chain"
        )
    if any(item.amendment_number is None for item in amendments):
        return _result(
            manager_cik, report_period, known_at, "ambiguous", "amendment_number_missing"
        )
    amendment_numbers = tuple(_amendment_number(item) for item in amendments)
    if any(value is None for value in amendment_numbers):
        return _result(
            manager_cik, report_period, known_at, "ambiguous", "invalid_amendment_number"
        )
    numbered_amendments = tuple(value for value in amendment_numbers if value is not None)
    if len(set(numbered_amendments)) != len(numbered_amendments):
        return _result(
            manager_cik, report_period, known_at, "ambiguous", "contradictory_amendment_chain"
        )
    if amendments and not originals:
        return _result(
            manager_cik, report_period, known_at, "insufficient", "missing_original_artifact"
        )
    if amendments and set(numbered_amendments) != set(range(1, max(numbered_amendments) + 1)):
        return _result(
            manager_cik, report_period, known_at, "insufficient", "amendment_chain_incomplete"
        )
    if amendments and _chain_is_contradictory(amendments):
        return _result(
            manager_cik, report_period, known_at, "ambiguous", "contradictory_amendment_chain"
        )
    latest = max(item.available_at for item in visible)
    newest = tuple(item for item in visible if item.available_at == latest)
    if len(newest) != 1:
        return _result(manager_cik, report_period, known_at, "ambiguous", "available_at_tie")
    selected = newest[0]
    source_literal: Sec13fV2SourceLiteral | None = None
    operation: Sec13fV2Operation | None = None
    if selected.is_amendment:
        status = "amended"
        reason = _amendment_reason(selected)
        source_literal = selected.amendment_type  # type: ignore[assignment]
        if source_literal in SOURCE_LITERAL_TO_OPERATION:
            operation = SOURCE_LITERAL_TO_OPERATION[source_literal]
    else:
        status = "original_complete"
        reason = "declared_original"
    entry_match = (
        None
        if selected.declared_entry_total is None or selected.observed_entry_total is None
        else selected.declared_entry_total == selected.observed_entry_total
    )
    value_match = (
        None
        if selected.declared_value_total is None or selected.observed_value_total is None
        else selected.declared_value_total == selected.observed_value_total
    )
    if selected.declared_entry_total is None or selected.declared_value_total is None:
        status, reason = "not_evaluable", "declared_total_missing"
    elif selected.observed_entry_total is None or selected.observed_value_total is None:
        status, reason = "not_evaluable", "observed_total_missing"
    elif not entry_match or not value_match:
        status, reason = "not_evaluable", "declared_total_mismatch"
    return InstitutionalCompositionV2Result(
        manager_cik=manager_cik,
        report_period=report_period,
        known_at=known_at,
        status=status,
        reason=reason,
        effective_artifact_id=selected.artifact_id,
        effective_accession=selected.accession,
        source_literal=source_literal,
        operation=operation,
        declared_entry_total=selected.declared_entry_total,
        observed_entry_total=selected.observed_entry_total,
        declared_value_total=selected.declared_value_total,
        observed_value_total=selected.observed_value_total,
        entry_total_matches=entry_match,
        value_total_matches=value_match,
    )


def _result(
    manager_cik: str,
    report_period: date | None,
    known_at: datetime,
    status: InstitutionalCompositionV2Status,
    reason: InstitutionalCompositionV2Reason,
) -> InstitutionalCompositionV2Result:
    return InstitutionalCompositionV2Result(
        manager_cik=manager_cik,
        report_period=report_period,
        known_at=known_at,
        status=status,
        reason=reason,
    )


def _has_available_at_tie(candidates: tuple[InstitutionalCompositionV2Candidate, ...]) -> bool:
    by_available_at: dict[datetime, set[UUID]] = {}
    for item in candidates:
        by_available_at.setdefault(item.available_at, set()).add(item.artifact_id)
    return any(len(artifact_ids) > 1 for artifact_ids in by_available_at.values())


def _amendment_number(item: InstitutionalCompositionV2Candidate) -> int | None:
    if item.amendment_number is None or not item.amendment_number.isdecimal():
        return None
    number = int(item.amendment_number)
    return number if number >= 1 else None


def _chain_is_contradictory(amendments: tuple[InstitutionalCompositionV2Candidate, ...]) -> bool:
    ordered = tuple(sorted(amendments, key=lambda item: item.available_at))
    numbers = tuple(_amendment_number(item) for item in ordered)
    if any(number is None for number in numbers):
        return True
    resolved_numbers = tuple(number for number in numbers if number is not None)
    return resolved_numbers != tuple(sorted(resolved_numbers))


def _amendment_reason(
    item: InstitutionalCompositionV2Candidate,
) -> InstitutionalCompositionV2Reason:
    if item.amendment_type == "RESTATEMENT":
        return "declared_amendment_restatement"
    return "declared_amendment_new_holdings"
