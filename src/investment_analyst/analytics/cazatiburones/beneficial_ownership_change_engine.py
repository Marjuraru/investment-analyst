# ruff: noqa: E501
"""Pure point-in-time calculations over Schedule 13D/13G statements."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityFeatureSet,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import DescriptiveMetric
from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipStatement


def calculate_beneficial_features(
    statements: Iterable[BeneficialOwnershipStatement],
) -> tuple[DeclaredActivityFeatureSet, ...]:
    statement_rows = tuple(statements)
    if len({statement.asset_id for statement in statement_rows}) > 1:
        raise ValueError("beneficial features require evidence for one asset")
    grouped: dict[tuple[str, str], list[BeneficialOwnershipStatement]] = defaultdict(list)
    for statement in statement_rows:
        if statement.reporting_person_cik is not None:
            grouped[(statement.subject_cik, statement.reporting_person_cik)].append(statement)
    results: list[DeclaredActivityFeatureSet] = []
    for (_, participant_cik), rows in sorted(grouped.items()):
        rows.sort(
            key=lambda item: (
                item.event_date or item.available_at.date(),
                item.available_at,
                str(item.statement_id),
            )
        )
        for index, current in enumerate(rows):
            previous = rows[index - 1] if index else None
            percent = (
                _delta(current.percent_of_class, previous.percent_of_class)
                if previous is not None
                else None
            )
            shares = (
                _delta(current.shares_beneficially_owned, previous.shares_beneficially_owned)
                if previous is not None
                else None
            )
            event = current.event_date
            metrics = (
                _history_metric(
                    "delta_percent_of_class", percent, has_history=previous is not None
                ),
                _history_metric(
                    "delta_shares_beneficially_owned", shares, has_history=previous is not None
                ),
                _threshold_metric(
                    "threshold_appearance",
                    previous.percent_of_class if previous is not None else None,
                    current.percent_of_class,
                    has_history=previous is not None,
                ),
                _threshold_metric(
                    "threshold_exit",
                    current.percent_of_class,
                    previous.percent_of_class if previous is not None else None,
                    has_history=previous is not None,
                ),
                DescriptiveMetric(
                    key="is_amendment", status="available", value=current.form.endswith("/A")
                ),
                _filing_delay(current.available_at.date(), event),
            )
            results.append(
                DeclaredActivityFeatureSet(
                    asset_id=current.asset_id,
                    family="beneficial_ownership",
                    participant_cik=participant_cik,
                    form=current.form,
                    declared_nature=current.form,
                    event_date=event,
                    available_at=max(previous.available_at, current.available_at)
                    if previous is not None
                    else current.available_at,
                    revision_ids=(
                        (str(previous.document_revision.revision_id),)
                        if previous is not None
                        else ()
                    )
                    + (str(current.document_revision.revision_id),),
                    comparison_status="available" if previous is not None else "not_evaluable",
                    metrics=metrics,
                )
            )
    return tuple(results)


def _delta(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    return None if current is None or previous is None else current - previous


def _metric(key: str, value: Decimal | None) -> DescriptiveMetric:
    return DescriptiveMetric(
        key=key, status="missing" if value is None else "available", value=value
    )


def _history_metric(key: str, value: Decimal | None, *, has_history: bool) -> DescriptiveMetric:
    if not has_history:
        return DescriptiveMetric(key=key, status="not_evaluable")
    return _metric(key, value)


def _threshold_metric(
    key: str,
    absent_value: Decimal | None,
    present_value: Decimal | None,
    *,
    has_history: bool,
) -> DescriptiveMetric:
    if not has_history:
        return DescriptiveMetric(key=key, status="not_evaluable")
    return DescriptiveMetric(
        key=key,
        status="available",
        value=absent_value is None and present_value is not None,
    )


def _filing_delay(available_date: date, event_date: date | None) -> DescriptiveMetric:
    if event_date is None:
        return DescriptiveMetric(key="filing_delay_days", status="missing")
    return DescriptiveMetric(
        key="filing_delay_days",
        status="available",
        value=Decimal((available_date - event_date).days),
    )
