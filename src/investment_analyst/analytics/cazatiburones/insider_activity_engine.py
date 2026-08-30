# ruff: noqa: E501
"""Pure point-in-time calculations over Section 16 statements."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.declared_activity_definitions import (
    CLUSTER_WINDOW_DAYS,
)
from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityFeatureSet,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import DescriptiveMetric
from investment_analyst.evidence.sec_ownership.models import OwnershipEntry, OwnershipStatement


def calculate_insider_features(
    statements: Iterable[OwnershipStatement],
) -> tuple[DeclaredActivityFeatureSet, ...]:
    statement_rows = tuple(statements)
    if len({statement.asset_id for statement in statement_rows}) > 1:
        raise ValueError("insider features require evidence for one asset")
    grouped: dict[tuple[str, str, str], list[tuple[OwnershipStatement, OwnershipEntry]]] = (
        defaultdict(list)
    )
    participant_statements: dict[str, set[str]] = defaultdict(set)
    for statement in statement_rows:
        for entry in statement.entries:
            participant_statements[entry.owner_cik].add(str(statement.statement_id))
            if entry.kind == "transaction":
                grouped[(entry.owner_cik, entry.security_title, entry.table)].append(
                    (statement, entry)
                )
    results: list[DeclaredActivityFeatureSet] = []
    for (participant_cik, security_title, table), rows in sorted(grouped.items()):
        rows.sort(
            key=lambda row: (
                row[1].transaction_date or row[0].period_of_report,
                row[0].available_at,
                str(row[1].entry_id),
            )
        )
        recurrence = Decimal(len(participant_statements[participant_cik]))
        for index, (statement, entry) in enumerate(rows):
            previous = rows[index - 1] if index else None
            previous_entry = previous[1] if previous is not None else None
            event_date = entry.transaction_date
            clustered = _cluster_count(rows, event_date)
            prior = previous_entry.shares_owned_following if previous_entry is not None else None
            post = entry.shares_owned_following
            ratio = _ratio(prior, post, has_history=previous is not None)
            metrics = (
                _metric("transaction_shares", entry.shares),
                _history_metric("prior_holding", prior, has_history=previous is not None),
                _metric("post_holding", post),
                _history_metric("holding_delta_ratio", ratio, has_history=previous is not None),
                _count_metric("acquisition_count", entry.acquired_disposed, "A"),
                _count_metric("disposition_count", entry.acquired_disposed, "D"),
                _metric("clustered_transaction_count", clustered),
                DescriptiveMetric(
                    key="participant_recurrence", status="available", value=recurrence
                ),
                DescriptiveMetric(
                    key="is_amendment", status="available", value=statement.form.endswith("/A")
                ),
                _filing_delay(statement.available_at.date(), event_date),
            )
            results.append(
                DeclaredActivityFeatureSet(
                    asset_id=statement.asset_id,
                    family="insider",
                    participant_cik=participant_cik,
                    form=statement.form,
                    security_title=security_title,
                    table=table,
                    event_date=event_date,
                    available_at=max(previous[0].available_at, statement.available_at)
                    if previous is not None
                    else statement.available_at,
                    revision_ids=(
                        (str(previous[0].document_revision.revision_id),)
                        if previous is not None
                        else ()
                    )
                    + (str(statement.document_revision.revision_id),),
                    comparison_status="available" if previous is not None else "not_evaluable",
                    metrics=metrics,
                )
            )
    return tuple(results)


def _metric(key: str, value: Decimal | None) -> DescriptiveMetric:
    return DescriptiveMetric(
        key=key, status="missing" if value is None else "available", value=value
    )


def _history_metric(key: str, value: Decimal | None, *, has_history: bool) -> DescriptiveMetric:
    if not has_history:
        return DescriptiveMetric(key=key, status="not_evaluable")
    return _metric(key, value)


def _ratio(prior: Decimal | None, post: Decimal | None, *, has_history: bool) -> Decimal | None:
    if not has_history or prior is None or prior == Decimal("0") or post is None:
        return None
    return (post - prior) / prior


def _count_metric(key: str, code: str | None, expected: str) -> DescriptiveMetric:
    if code is None:
        return DescriptiveMetric(key=key, status="missing")
    return DescriptiveMetric(key=key, status="available", value=Decimal(code == expected))


def _cluster_count(
    rows: list[tuple[OwnershipStatement, OwnershipEntry]], event_date: date | None
) -> Decimal | None:
    if event_date is None:
        return None
    return Decimal(
        sum(
            1
            for _, entry in rows
            if entry.transaction_date is not None
            and abs((entry.transaction_date - event_date).days) <= CLUSTER_WINDOW_DAYS
        )
    )


def _filing_delay(available_date: date, event_date: date | None) -> DescriptiveMetric:
    if event_date is None:
        return DescriptiveMetric(key="filing_delay_days", status="missing")
    return DescriptiveMetric(
        key="filing_delay_days",
        status="available",
        value=Decimal((available_date - event_date).days),
    )
