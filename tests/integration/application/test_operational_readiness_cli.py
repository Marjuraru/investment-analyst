"""CLI integration for deterministic zero-write operational readiness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

from investment_analyst.alerts.analytical_state import (
    AnalyticalMonitorReceipt,
    AnalyticalMonitorReceiptStatus,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduleStateStore,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
)
from investment_analyst.application.operational_alerts import (
    OperationalAlertMonitor,
    OperationalAlertStateStore,
)
from investment_analyst.workspace.service import WorkspaceService

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_operational_readiness.py"
_SCHEDULE = "multi_asset_schedule_state_v1.json"
_OPERATIONAL = "operational_alert_state_v1.json"
_ANALYTICAL = "analytical_screening_state_v1.json"


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    paths = WorkspaceService(environ={}, home=tmp_path / "home").initialize(root).paths
    schedule = MultiAssetScheduleStateStore(paths.state_root / _SCHEDULE)
    operational = OperationalAlertStateStore(paths.state_root / _OPERATIONAL)
    analytical = AnalyticalScreeningStateStore(paths.state_root / _ANALYTICAL)
    monitor = OperationalAlertMonitor(
        operational,
        clock=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    definition = ScheduledJobDefinition(
        job_id="alpaca:equity:us:aapl:market-daily",
        asset_id="equity:us:aapl",
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        retry_backoff_seconds=60,
    )
    for index, day in enumerate((1, 2, 3), start=1):
        local_date = date(2026, 8, day)
        scheduled_for = definition.scheduled_for(local_date)
        started_at = scheduled_for + timedelta(minutes=1)
        completed_at = started_at + timedelta(seconds=30)
        attempt = ScheduledJobAttempt(
            attempt_id=UUID(f"00000000-0000-4000-8000-{index:012d}"),
            definition=definition,
            local_date=local_date,
            scheduled_for=scheduled_for,
            attempt_number=1,
            status=ScheduledJobAttemptStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=completed_at,
            execution=ScheduledJobExecution(
                job_id=definition.job_id,
                effective_known_at=completed_at,
                evidence_changed=False,
                source_ids=("alpaca-market-data:iex:aapl:daily-bars:adjustment-all",),
                created_count=0,
                reused_count=1,
                coverage_complete=True,
            ),
        )
        schedule.write_attempt(attempt)
        monitor(attempt)
        analytical.record_attempt(
            AnalyticalMonitorReceipt(
                attempt_id=attempt.attempt_id,
                job_id=definition.job_id,
                asset_id=definition.asset_id,
                status=AnalyticalMonitorReceiptStatus.SKIPPED,
                reason="unchanged_evidence",
                processed_at=completed_at,
            ),
            (),
        )
    return root


def _inventory(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (
            str(path.relative_to(root)),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )


def _run(root: Path, *, minimum: str = "3", since: str = "2026-08-01T00:00:00Z"):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace",
            str(root),
            "--since",
            since,
            "--min-local-dates",
            minimum,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_is_byte_exact_zero_write_and_semantically_repeatable(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = _inventory(root)

    first = _run(root)
    between = _inventory(root)
    second = _run(root)
    after = _inventory(root)

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert before == between == after
    assert not any(".tmp" in path or "lock" in path for path, _, _ in after)
    report = json.loads(first.stdout)
    assert report["schema_version"] == "operational-readiness-report-v1"
    assert report["decision"] == "PASS"
    assert report["reason_codes"] == []
    assert report["summary"]["terminal_attempt_count"] == 3
    assert report["summary"]["operational_screening_count"] == 12
    assert report["summary"]["analytical_receipt_count"] == 3


def test_cli_uses_exit_three_for_valid_not_ready(tmp_path: Path) -> None:
    completed = _run(_workspace(tmp_path), minimum="4")

    assert completed.returncode == 3
    assert completed.stderr == ""
    report = json.loads(completed.stdout)
    assert report["decision"] == "NOT_READY"
    assert report["reason_codes"] == ["insufficient_local_dates"]


def test_cli_uses_exit_two_for_invalid_path_or_timestamp(tmp_path: Path) -> None:
    missing = _run(tmp_path / "missing")
    naive = _run(_workspace(tmp_path), since="2026-08-01T00:00:00")

    assert missing.returncode == 2
    assert json.loads(missing.stderr)["error"] == "workspace_path_invalid"
    assert naive.returncode == 2
    assert "since must include an explicit UTC offset" in naive.stderr
