"""Unit contracts for descriptive forward outcomes of screening conditions."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.alerts.analytical_models import AnalyticalConditionState
from investment_analyst.alerts.analytical_rule_catalog import RSI_INCREMENTAL_LOW_REVIEW_RULE
from investment_analyst.alerts.descriptive_outcome_models import (
    ConditionEvidenceReason,
    DescriptiveCloseObservation,
    DescriptiveConditionEvidence,
    DescriptiveConditionOutcomeReport,
    DescriptiveGroupSummary,
    DescriptiveOutcomeEvidence,
    DescriptiveOutcomeGroup,
    DescriptiveOutcomeRequest,
    DescriptiveOutcomeStatus,
    DescriptivePeriodOutcome,
    descriptive_outcome_report_id,
    summarize_descriptive_rows,
)
from investment_analyst.core.models import DataQuality

_RULE = RSI_INCREMENTAL_LOW_REVIEW_RULE
_CONDITION = _RULE.conditions[0]
_BASE = datetime(2026, 10, 1, tzinfo=UTC)
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_WORKSPACE_ID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")


def _request(
    *,
    start: date = date(2026, 10, 1),
    end: date = date(2026, 10, 1),
    known_at: datetime = datetime(2026, 11, 20, tzinfo=UTC),
    fingerprint: str | None = None,
) -> DescriptiveOutcomeRequest:
    return DescriptiveOutcomeRequest(
        asset_id="equity:us:aapl",
        rule_id=_RULE.rule_id,
        expected_rule_fingerprint=fingerprint or _RULE.semantic_fingerprint(),
        start_date=start,
        end_date=end,
        known_at=known_at,
    )


def _close(index: int, value: str = "100") -> DescriptiveCloseObservation:
    start = _BASE + timedelta(days=index)
    return DescriptiveCloseObservation(
        observation_id=uuid5(NAMESPACE_URL, f"close:{index}:{value}"),
        period_start=start,
        period_end=start + timedelta(days=1),
        observed_at=start,
        available_at=start + timedelta(hours=1),
        close=Decimal(value),
        quality=DataQuality.VALID,
    )


def _condition_evidence(
    period: datetime,
    value: str = "25",
) -> DescriptiveConditionEvidence:
    observed = Decimal(value)
    state = (
        AnalyticalConditionState.MET
        if _CONDITION.operator.compare(observed, _CONDITION.threshold)
        else AnalyticalConditionState.NOT_MET
    )
    return DescriptiveConditionEvidence(
        state=state,
        metric_key=_CONDITION.metric_key,
        operator=_CONDITION.operator,
        threshold=_CONDITION.threshold,
        retention_threshold=_CONDITION.retention_threshold,
        unit=_CONDITION.unit,
        metric_result_id=uuid5(NAMESPACE_URL, f"metric:{period.isoformat()}:{value}"),
        as_of=period,
        available_at=period + timedelta(days=1),
        computed_at=period + timedelta(days=1, hours=2),
        algorithm_version=_CONDITION.algorithm_version,
        parameters={"source_id": _SOURCE_ID, "window": 14},
        quality=DataQuality.PARTIAL,
        observed_value=observed,
        screening_result_id=uuid5(NAMESPACE_URL, f"screening:{period.isoformat()}:{value}"),
        explanation_es="La evidencia describe la condición configurada.",
    )


def _available_row(
    period: datetime,
    *,
    value: str = "25",
    last_close: str = "110",
    group: DescriptiveOutcomeGroup = DescriptiveOutcomeGroup.MATCHED,
) -> DescriptivePeriodOutcome:
    closes = tuple(
        _close(index, last_close if index == 6 else str(99 + index)) for index in range(1, 7)
    )
    return DescriptivePeriodOutcome(
        period_as_of=period,
        group=group,
        condition=_condition_evidence(period, value),
        outcome=DescriptiveOutcomeEvidence(
            status=DescriptiveOutcomeStatus.AVAILABLE,
            return_5_intervals=(Decimal(closes[-1].close) / Decimal(closes[0].close))
            - Decimal("1"),
            observed_intervals=5,
            closes=closes,
            elapsed_seconds=5 * 86_400,
            bar_separation_seconds=(86_400, 86_400, 86_400, 86_400, 86_400),
            evidence_available_at=max(item.available_at for item in closes),
        ),
    )


def _not_evaluable_row(period: datetime) -> DescriptivePeriodOutcome:
    return DescriptivePeriodOutcome(
        period_as_of=period,
        group=DescriptiveOutcomeGroup.NOT_EVALUABLE,
        condition=DescriptiveConditionEvidence(
            state=AnalyticalConditionState.NOT_EVALUABLE,
            reason=ConditionEvidenceReason.NO_COMPATIBLE_METRIC,
            metric_key=_CONDITION.metric_key,
            operator=_CONDITION.operator,
            threshold=_CONDITION.threshold,
            retention_threshold=_CONDITION.retention_threshold,
            unit=_CONDITION.unit,
        ),
        outcome=DescriptiveOutcomeEvidence(
            status=DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE,
            reason="no_compatible_metric",
            observed_intervals=0,
        ),
    )


def _report(rows: tuple[DescriptivePeriodOutcome, ...]) -> DescriptiveConditionOutcomeReport:
    request = _request()
    groups = summarize_descriptive_rows(rows)
    latest = max(
        (
            value
            for row in rows
            for value in (row.condition.available_at, row.outcome.evidence_available_at)
            if value is not None
        ),
        default=None,
    )
    limitations = (
        "La revisión describe condiciones y resultados observados; no mide rentabilidad "
        "ejecutable ni valida una predicción.",
    )
    report_id = descriptive_outcome_report_id(
        request=request,
        rule_id=_RULE.rule_id,
        rule_version=_RULE.rule_version,
        rule_fingerprint=request.expected_rule_fingerprint,
        asset_id=request.asset_id,
        asset_class="equity",
        source_id=_SOURCE_ID,
        currency="USD",
        workspace_id=_WORKSPACE_ID,
        requested_calendar_days=request.requested_calendar_days,
        observed_periods=len(rows),
        rows=rows,
        groups=groups,
        latest_evidence_available_at=latest,
        limitations=limitations,
    )
    return DescriptiveConditionOutcomeReport(
        report_id=report_id,
        request=request,
        rule_id=_RULE.rule_id,
        rule_version=_RULE.rule_version,
        rule_fingerprint=request.expected_rule_fingerprint,
        asset_id=request.asset_id,
        asset_class="equity",
        source_id=_SOURCE_ID,
        currency="USD",
        workspace_id=_WORKSPACE_ID,
        requested_calendar_days=request.requested_calendar_days,
        observed_periods=len(rows),
        matched_count=sum(item.group is DescriptiveOutcomeGroup.MATCHED for item in rows),
        not_matched_count=sum(item.group is DescriptiveOutcomeGroup.NOT_MATCHED for item in rows),
        not_evaluable_count=sum(
            item.group is DescriptiveOutcomeGroup.NOT_EVALUABLE for item in rows
        ),
        rows=rows,
        groups=groups,
        latest_evidence_available_at=latest,
        limitations=limitations,
    )


def test_request_accepts_ninety_inclusive_days_and_rejects_outer_bounds() -> None:
    valid = _request(
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        known_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    assert valid.requested_calendar_days == 90

    with pytest.raises(ValidationError, match="90 calendar days"):
        _request(
            start=date(2026, 1, 1),
            end=date(2026, 4, 1),
            known_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="must not predate the requested end_date"):
        _request(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            known_at=datetime(2026, 1, 1, 23, 59, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="too far after"):
        _request(
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            known_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        _request(fingerprint="not-a-fingerprint")
    with pytest.raises(ValidationError):
        DescriptiveOutcomeRequest(
            asset_id="equity:us:aapl",
            rule_id=_RULE.rule_id,
            expected_rule_fingerprint=_RULE.semantic_fingerprint(),
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
            known_at=datetime(2026, 11, 1),
        )


def test_available_outcome_requires_six_closes_and_decimal_return() -> None:
    row = _available_row(_BASE)
    assert row.outcome.return_5_intervals == Decimal("0.1")
    assert row.outcome.observed_intervals == 5
    assert row.outcome.elapsed_seconds == 5 * 86_400

    with pytest.raises(ValidationError, match="six observed closes"):
        DescriptiveOutcomeEvidence(
            status=DescriptiveOutcomeStatus.AVAILABLE,
            return_5_intervals=Decimal("0.1"),
            observed_intervals=4,
            closes=tuple(_close(index) for index in range(1, 6)),
            elapsed_seconds=4 * 86_400,
            bar_separation_seconds=(86_400, 86_400, 86_400, 86_400),
            evidence_available_at=_BASE + timedelta(days=6),
        )
    with pytest.raises(ValidationError, match="five-interval return"):
        DescriptiveOutcomeEvidence(
            status=DescriptiveOutcomeStatus.AVAILABLE,
            observed_intervals=5,
            closes=tuple(_close(index) for index in range(1, 7)),
            elapsed_seconds=5 * 86_400,
            bar_separation_seconds=(86_400, 86_400, 86_400, 86_400, 86_400),
            evidence_available_at=_BASE + timedelta(days=6),
        )
    with pytest.raises(ValidationError, match="must use Decimal"):
        DescriptiveOutcomeEvidence(
            status=DescriptiveOutcomeStatus.AVAILABLE,
            return_5_intervals=0.1,
            observed_intervals=5,
            closes=tuple(_close(index) for index in range(1, 7)),
            elapsed_seconds=5 * 86_400,
            bar_separation_seconds=(86_400, 86_400, 86_400, 86_400, 86_400),
            evidence_available_at=_BASE + timedelta(days=6),
        )


def test_pending_and_insufficient_outcomes_cannot_claim_return() -> None:
    closes = tuple(_close(index) for index in range(1, 4))
    pending = DescriptiveOutcomeEvidence(
        status=DescriptiveOutcomeStatus.PENDING_WINDOW,
        reason="observation_window_open",
        observed_intervals=2,
        closes=closes,
        elapsed_seconds=2 * 86_400,
        bar_separation_seconds=(86_400, 86_400),
        evidence_available_at=closes[-1].available_at,
    )
    assert pending.observed_intervals == 2
    insufficient = pending.model_copy(
        update={"status": DescriptiveOutcomeStatus.INSUFFICIENT_OBSERVED_BARS}
    )
    assert insufficient.status is DescriptiveOutcomeStatus.INSUFFICIENT_OBSERVED_BARS
    with pytest.raises(ValidationError, match="cannot claim a return"):
        DescriptiveOutcomeEvidence.model_validate(
            {**pending.model_dump(mode="python"), "return_5_intervals": Decimal("0")}
        )
    with pytest.raises(ValidationError, match="cannot claim a return"):
        DescriptiveOutcomeEvidence.model_validate(
            {**pending.model_dump(mode="python"), "return_5_intervals": Decimal("0.1")}
        )


def test_group_summary_is_an_exact_partition_with_null_empty_means() -> None:
    matched = _available_row(_BASE, value="25", last_close="110")
    not_matched = _available_row(
        _BASE + timedelta(days=1),
        value="40",
        last_close="95",
        group=DescriptiveOutcomeGroup.NOT_MATCHED,
    )
    not_evaluable = _not_evaluable_row(_BASE + timedelta(days=2))
    summaries = summarize_descriptive_rows((matched, not_matched, not_evaluable))
    by_group = {item.group: item for item in summaries}
    assert by_group[DescriptiveOutcomeGroup.MATCHED].denominator == 1
    assert by_group[DescriptiveOutcomeGroup.MATCHED].positives == 1
    assert by_group[DescriptiveOutcomeGroup.MATCHED].mean_return_5_intervals == Decimal("0.1")
    assert by_group[DescriptiveOutcomeGroup.NOT_MATCHED].negatives == 1
    assert by_group[DescriptiveOutcomeGroup.NOT_EVALUABLE].condition_not_evaluable_count == 1
    assert by_group[DescriptiveOutcomeGroup.NOT_EVALUABLE].mean_return_5_intervals is None

    only_not_evaluable = summarize_descriptive_rows((not_evaluable,))
    empty_matched = only_not_evaluable[0]
    assert empty_matched.denominator == 0
    assert empty_matched.mean_return_5_intervals is None
    assert empty_matched.median_return_5_intervals is None

    with pytest.raises(ValidationError, match="must partition the denominator"):
        DescriptiveGroupSummary(
            group=DescriptiveOutcomeGroup.MATCHED,
            denominator=1,
            available_count=0,
            pending_window_count=0,
            insufficient_observed_bars_count=0,
            ambiguous_close_count=0,
            invalid_close_count=0,
            condition_not_evaluable_count=0,
            positives=0,
            negatives=0,
            zeros=0,
        )


def test_report_identity_is_deterministic_and_changes_with_evidence() -> None:
    rows = (_available_row(_BASE), _not_evaluable_row(_BASE + timedelta(days=1)))
    first = _report(rows)
    repeated = _report(rows)
    assert first.report_id == repeated.report_id
    assert first.to_json_bytes() == repeated.to_json_bytes()

    changed = _report((_available_row(_BASE, last_close="120"), rows[1]))
    assert changed.report_id != first.report_id


def test_report_rejects_more_than_256_kib_without_truncation() -> None:
    rows = tuple(
        _available_row(
            _BASE + timedelta(days=index),
            value="25",
            last_close=str(101 + index),
        ).model_copy(
            update={
                "condition": _condition_evidence(
                    _BASE + timedelta(days=index),
                    "25",
                ).model_copy(
                    update={
                        "parameters": {
                            "source_id": _SOURCE_ID,
                            "payload": "x" * 3_000,
                        }
                    }
                )
            }
        )
        for index in range(90)
    )
    with pytest.raises(ValidationError, match="256 KiB"):
        _report(rows)


def test_not_evaluable_period_never_claims_metric_or_return_evidence() -> None:
    row = _not_evaluable_row(_BASE)
    assert row.condition.reason is ConditionEvidenceReason.NO_COMPATIBLE_METRIC
    assert row.outcome.status is DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE
    with pytest.raises(ValidationError, match="cannot claim a return"):
        DescriptiveOutcomeEvidence.model_validate(
            {**row.outcome.model_dump(mode="python"), "return_5_intervals": Decimal("0")}
        )
    with pytest.raises(ValidationError, match="cannot claim metric evidence"):
        DescriptiveConditionEvidence.model_validate(
            {**row.condition.model_dump(mode="python"), "observed_value": Decimal("25")}
        )
