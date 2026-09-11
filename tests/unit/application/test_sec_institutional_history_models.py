"""Unit tests for the two-close institutional 13F history models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.application.sec_institutional_history import plan_history_window
from investment_analyst.application.sec_institutional_history_models import (
    SEC_INSTITUTIONAL_HISTORY_CYCLE_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_HISTORY_POLICY_VERSION,
    SecInstitutionalHistoryPeriodSummary,
    SecInstitutionalHistoryRequest,
    SecInstitutionalHistorySummary,
    SecInstitutionalHistoryTargetSummary,
)
from investment_analyst.evidence.sec_institutional_universe.identity import candidate_id
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)

_OLDER_SHA = "a" * 64
_NEWER_SHA = "b" * 64
_CIK = "0001067983"
_OTHER_CIK = "0000000002"
_CUSIP = "037833100"


def _candidate(
    *,
    dataset_sha256: str,
    manager_cik: str,
    report_period: date,
    accession: str,
    is_selected: bool = True,
    asset_id: str = "equity:us:aapl",
) -> Sec13FManagerCandidate:
    import uuid

    revision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"revision|{dataset_sha256}")
    return Sec13FManagerCandidate(
        candidate_id=candidate_id(
            dataset_sha256=dataset_sha256,
            asset_id=asset_id,
            cusip=_CUSIP,
            manager_cik=manager_cik,
            accession=accession,
            form="13F-HR",
            report_period=report_period,
        ),
        dataset_revision_id=revision_id,
        asset_id=asset_id,
        cusip=_CUSIP,
        manager_cik=manager_cik,
        manager_name=f"MANAGER {manager_cik}",
        accession=accession,
        form="13F-HR",
        filing_date=report_period,
        report_period=report_period,
        value_as_filed=Decimal("1000"),
        is_amendment=False,
        is_selected=is_selected,
        selection_rank=1 if is_selected else None,
    )


def _snapshot(
    *,
    dataset_sha256: str,
    report_period: date,
    candidates: tuple[Sec13FManagerCandidate, ...],
) -> Sec13FManagerUniverseSnapshot:
    import uuid

    revision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"revision|{dataset_sha256}")
    retrieved_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return Sec13FManagerUniverseSnapshot.create(
        dataset_revision_id=revision_id,
        dataset_sha256=dataset_sha256,
        catalog_version=1,
        period_start=report_period,
        period_end=report_period,
        retrieved_at=retrieved_at,
        event_time=retrieved_at,
        eligible_asset_count=1,
        matched_asset_count=1,
        candidate_manager_count=len(candidates),
        selected_manager_count=sum(item.is_selected for item in candidates),
        unselected_manager_count=sum(not item.is_selected for item in candidates),
        covered_cusips=(_CUSIP,),
        candidates=candidates,
    )


def test_history_request_bounds_the_accession_budget() -> None:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    request = SecInstitutionalHistoryRequest(known_at=now)
    assert request.accessions_per_period == 2

    with pytest.raises(ValidationError):
        SecInstitutionalHistoryRequest(known_at=now, accessions_per_period=3)

    with pytest.raises(ValidationError):
        SecInstitutionalHistoryRequest(known_at=now, accessions_per_period=0)


def test_plan_history_window_keeps_exactly_the_common_managers() -> None:
    older_period = date(2025, 12, 31)
    newer_period = date(2026, 3, 31)
    older = _snapshot(
        dataset_sha256=_OLDER_SHA,
        report_period=older_period,
        candidates=(
            _candidate(
                dataset_sha256=_OLDER_SHA,
                manager_cik=_CIK,
                report_period=older_period,
                accession="0001067983-26-000001",
            ),
            _candidate(
                dataset_sha256=_OLDER_SHA,
                manager_cik=_OTHER_CIK,
                report_period=older_period,
                accession="0000000002-26-000001",
            ),
        ),
    )
    newer = _snapshot(
        dataset_sha256=_NEWER_SHA,
        report_period=newer_period,
        candidates=(
            _candidate(
                dataset_sha256=_NEWER_SHA,
                manager_cik=_CIK,
                report_period=newer_period,
                accession="0001067983-26-000010",
            ),
            _candidate(
                dataset_sha256=_NEWER_SHA,
                manager_cik="0000000003",
                report_period=newer_period,
                accession="0000000003-26-000001",
            ),
        ),
    )

    plan = plan_history_window(older, newer)

    assert plan.excluded_manager_count == 2
    assert plan.non_comparable_manager_count == 0
    assert len(plan.targets) == 1
    target = plan.targets[0]
    assert target.manager_cik == _CIK
    assert target.asset_id == "equity:us:aapl"
    assert target.older_report_period == older_period
    assert target.newer_report_period == newer_period
    assert len(target.older_candidates) == 1
    assert len(target.newer_candidates) == 1
    assert target.older_candidates[0].report_period == older_period
    assert target.newer_candidates[0].report_period == newer_period


def test_plan_history_window_counts_non_comparable_periods_separately() -> None:
    period = date(2026, 3, 31)
    older = _snapshot(
        dataset_sha256=_OLDER_SHA,
        report_period=period,
        candidates=(
            _candidate(
                dataset_sha256=_OLDER_SHA,
                manager_cik=_CIK,
                report_period=period,
                accession="0001067983-26-000001",
            ),
        ),
    )
    newer = _snapshot(
        dataset_sha256=_NEWER_SHA,
        report_period=period,
        candidates=(
            _candidate(
                dataset_sha256=_NEWER_SHA,
                manager_cik=_CIK,
                report_period=period,
                accession="0001067983-26-000010",
            ),
        ),
    )

    plan = plan_history_window(older, newer)

    assert plan.targets == ()
    assert plan.excluded_manager_count == 0
    assert plan.non_comparable_manager_count == 1


def test_plan_history_window_ignores_unselected_candidates() -> None:
    older_period = date(2025, 12, 31)
    newer_period = date(2026, 3, 31)
    older = _snapshot(
        dataset_sha256=_OLDER_SHA,
        report_period=older_period,
        candidates=(
            _candidate(
                dataset_sha256=_OLDER_SHA,
                manager_cik=_CIK,
                report_period=older_period,
                accession="0001067983-26-000001",
                is_selected=False,
            ),
        ),
    )
    newer = _snapshot(
        dataset_sha256=_NEWER_SHA,
        report_period=newer_period,
        candidates=(
            _candidate(
                dataset_sha256=_NEWER_SHA,
                manager_cik=_CIK,
                report_period=newer_period,
                accession="0001067983-26-000010",
            ),
        ),
    )

    plan = plan_history_window(older, newer)

    assert plan.targets == ()
    assert plan.excluded_manager_count == 1


def test_history_period_summary_requires_ordered_periods() -> None:
    with pytest.raises(ValidationError, match="dataset period start must precede period end"):
        SecInstitutionalHistoryPeriodSummary(
            role="older",
            period_start=date(2026, 3, 1),
            period_end=date(2025, 12, 31),
            dataset_url="https://www.sec.gov/file.zip",
            dataset_sha256=_OLDER_SHA,
            snapshot_id=uuid4(),
        )


def test_history_summary_lifecycle_and_cursor_rules() -> None:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    summary = SecInstitutionalHistorySummary(
        effective_known_at=now,
        status="processed",
        phase="ready",
        catalog_calls=1,
        submissions_calls=1,
        total_targets=3,
        target_cursor_before=0,
        target_cursor_after=1,
    )
    assert summary.schema_version == SEC_INSTITUTIONAL_HISTORY_CYCLE_SCHEMA_VERSION
    assert summary.policy_version == SEC_INSTITUTIONAL_HISTORY_POLICY_VERSION
    assert summary.to_json_dict()["target_cursor_after"] == 1

    with pytest.raises(ValidationError, match="failed history steps require exactly one reason"):
        SecInstitutionalHistorySummary(
            effective_known_at=now, status="failed", phase="ready", reason_code=None
        )

    with pytest.raises(ValidationError, match="target cursor must never regress"):
        SecInstitutionalHistorySummary(
            effective_known_at=now,
            status="processed",
            phase="ready",
            target_cursor_before=2,
            target_cursor_after=1,
        )

    with pytest.raises(ValidationError, match="a processed step requires a ready window"):
        SecInstitutionalHistorySummary(
            effective_known_at=now, status="processed", phase="preparing"
        )

    with pytest.raises(ValidationError, match="at most two datasets"):
        SecInstitutionalHistorySummary(
            effective_known_at=now,
            status="completed",
            phase="completed",
            periods=(
                SecInstitutionalHistoryPeriodSummary(
                    role="older",
                    period_start=date(2025, 12, 1),
                    period_end=date(2026, 2, 28),
                    dataset_url="https://www.sec.gov/older.zip",
                    dataset_sha256=_OLDER_SHA,
                    snapshot_id=uuid4(),
                ),
                SecInstitutionalHistoryPeriodSummary(
                    role="newer",
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 5, 31),
                    dataset_url="https://www.sec.gov/newer.zip",
                    dataset_sha256=_NEWER_SHA,
                    snapshot_id=uuid4(),
                ),
                SecInstitutionalHistoryPeriodSummary(
                    role="newer",
                    period_start=date(2026, 6, 1),
                    period_end=date(2026, 8, 31),
                    dataset_url="https://www.sec.gov/third.zip",
                    dataset_sha256="c" * 64,
                    snapshot_id=uuid4(),
                ),
            ),
        )


def test_history_summary_window_roles_are_ordered() -> None:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    older = SecInstitutionalHistoryPeriodSummary(
        role="newer",
        period_start=date(2025, 12, 1),
        period_end=date(2026, 2, 28),
        dataset_url="https://www.sec.gov/older.zip",
        dataset_sha256=_OLDER_SHA,
        snapshot_id=uuid4(),
    )
    newer = SecInstitutionalHistoryPeriodSummary(
        role="older",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 5, 31),
        dataset_url="https://www.sec.gov/newer.zip",
        dataset_sha256=_NEWER_SHA,
        snapshot_id=uuid4(),
    )
    with pytest.raises(ValidationError, match="older then newer"):
        SecInstitutionalHistorySummary(
            effective_known_at=now,
            status="processed",
            phase="ready",
            periods=(older, newer),
        )


def test_history_target_summary_rules() -> None:
    older_period = date(2025, 12, 31)
    newer_period = date(2026, 3, 31)
    with pytest.raises(ValidationError, match="older report period must precede"):
        SecInstitutionalHistoryTargetSummary(
            asset_id="equity:us:aapl",
            manager_cik=_CIK,
            manager_name="MANAGER",
            older_report_period=newer_period,
            newer_report_period=older_period,
            state="processed",
        )

    with pytest.raises(ValidationError, match="only a failed target declares a reason code"):
        SecInstitutionalHistoryTargetSummary(
            asset_id="equity:us:aapl",
            manager_cik=_CIK,
            manager_name="MANAGER",
            older_report_period=older_period,
            newer_report_period=newer_period,
            state="failed",
            reason_code=None,
        )

    target = SecInstitutionalHistoryTargetSummary(
        asset_id="equity:us:aapl",
        manager_cik=_CIK,
        manager_name="MANAGER",
        older_report_period=older_period,
        newer_report_period=newer_period,
        state="processed",
        created_accessions=("0001067983-26-000001",),
        non_evaluable={"metric:insufficient_close": 2},
        traceability_verified=True,
    )
    assert target.metrics_created == 0
    assert target.non_evaluable["metric:insufficient_close"] == 2
