"""Unit tests for scheduled Form 13F cycle models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.application.sec_institutional_cycle_models import (
    SEC_INSTITUTIONAL_CYCLE_POLICY,
    SEC_INSTITUTIONAL_SCHEDULED_CYCLE_SCHEMA_VERSION,
    SecInstitutionalCycleRequest,
    SecInstitutionalCycleSummary,
)


def test_cycle_request_validation() -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    req = SecInstitutionalCycleRequest(known_at=now)
    assert req.known_at == now
    assert req.accessions_per_manager == 2
    assert not req.force_dataset_refresh

    # Max 2 accessions per manager
    with pytest.raises(ValidationError):
        SecInstitutionalCycleRequest(known_at=now, accessions_per_manager=3)

    with pytest.raises(ValidationError):
        SecInstitutionalCycleRequest(known_at=now, accessions_per_manager=0)


def test_cycle_summary_successful_partition() -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    summary = SecInstitutionalCycleSummary(
        effective_known_at=now,
        status="processed",
        catalog_calls=1,
        zip_calls=0,
        submissions_calls=1,
        archives_calls=2,
        dataset_period_start=date(2026, 4, 1),
        dataset_period_end=date(2026, 6, 30),
        dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01apr2026-30jun2026_form13f.zip",
        dataset_sha256="a" * 64,
        snapshot_id=uuid4(),
        manager_cursor_before=0,
        manager_cursor_after=1,
        total_managers=5,
        coverage_complete=False,
        manager_cik="0001067983",
        manager_name="BERKSHIRE HATHAWAY INC",
        report_period=date(2026, 6, 30),
        created_accessions=("0001067983-26-000001",),
        reused_accessions=(),
        rejected_accessions=(),
        failed_accessions=(),
        backlog_after=0,
        observations_created=1,
        observations_reused=0,
        traceability_verified=True,
    )
    assert summary.schema_version == SEC_INSTITUTIONAL_SCHEDULED_CYCLE_SCHEMA_VERSION
    assert summary.policy_version == SEC_INSTITUTIONAL_CYCLE_POLICY
    assert summary.created_accessions_count == 1
    assert summary.reused_accessions_count == 0


def test_cycle_summary_failures_require_reason_code() -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="failed cycle steps require exactly one reason code"):
        SecInstitutionalCycleSummary(
            effective_known_at=now,
            status="failed",
            reason_code=None,
        )

    with pytest.raises(ValidationError, match="failed cycle steps require exactly one reason code"):
        SecInstitutionalCycleSummary(
            effective_known_at=now,
            status="processed",
            reason_code="unexpected_error",
        )


def test_cycle_summary_cursor_must_not_regress() -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="manager cursor must never regress"):
        SecInstitutionalCycleSummary(
            effective_known_at=now,
            status="processed",
            manager_cursor_before=2,
            manager_cursor_after=1,
        )


def test_cycle_summary_unique_accessions() -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="must contain unique accessions"):
        SecInstitutionalCycleSummary(
            effective_known_at=now,
            status="processed",
            created_accessions=("0001067983-26-000001", "0001067983-26-000001"),
        )
