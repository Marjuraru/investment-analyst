"""CLI integration for the deterministic read-only storage observability report."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from investment_analyst.application.storage_observability import (
    StorageObservabilityDailyJobSummary,
    StorageObservabilityDailySnapshot,
)
from investment_analyst.workspace.service import WorkspaceService

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "report_storage_observability.py"
_ARTIFACT_NAME = "storage_observability_v1.jsonl"
_JOB_ID = "equity:us:aapl:market-daily"
_ANCHOR = date(2026, 9, 20)


def _line(snapshot: StorageObservabilityDailySnapshot) -> str:
    return json.dumps(
        snapshot.to_json_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _day_snapshot(day: date, *, database_bytes_delta: int) -> StorageObservabilityDailySnapshot:
    return StorageObservabilityDailySnapshot(
        utc_date=day,
        record_count=1,
        job_summaries=(
            StorageObservabilityDailyJobSummary(
                job_id=_JOB_ID,
                attempt_count=1,
                attempts_with_evidence=1,
                database_bytes_delta=database_bytes_delta,
                wal_bytes_delta=0,
                rows_created=1,
                rows_reused=0,
                total_ms=7_000,
            ),
        ),
    )


def _workspace(tmp_path: Path, *, days: int) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    paths = WorkspaceService(environ={}, home=tmp_path / "home").initialize(root).paths
    artifact = paths.state_root / _ARTIFACT_NAME
    snapshots = (
        _day_snapshot(
            _ANCHOR - timedelta(days=offset),
            database_bytes_delta=1_000_000 + offset,
        )
        for offset in reversed(range(days))
    )
    artifact.write_text("".join(f"{_line(item)}\n" for item in snapshots), encoding="utf-8")
    return root, artifact


def _inventory(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (
            str(path.relative_to(root)),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(root), *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_emits_json_report_and_writes_nothing(tmp_path: Path) -> None:
    root, artifact = _workspace(tmp_path, days=9)
    original = artifact.read_bytes()
    before = _inventory(root)

    first = _run(root)
    between = _inventory(root)
    second = _run(root)
    after = _inventory(root)

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert before == between == after
    assert artifact.read_bytes() == original
    report = json.loads(first.stdout)
    assert report["schema_version"] == "storage-observability-report-v1"
    assert report["artifact_present"] is True
    assert report["retained_days"] == 9
    assert report["anchor_date"] == _ANCHOR.isoformat()
    assert [window["window_days"] for window in report["windows"]] == [7, 30]
    assert report["windows"][0]["missing_days"] == []
    assert len(report["windows"][1]["missing_days"]) == 21
    assert report["windows"][0]["declared_days"][0] == (_ANCHOR - timedelta(days=6)).isoformat()
    assert report["budget"]["threshold_bytes_per_day"] == 30_000_000
    assert report["budget"]["exceeded"] is False

    anchored = _run(root, "--as-of", (_ANCHOR - timedelta(days=3)).isoformat())
    assert anchored.returncode == 0
    assert json.loads(anchored.stdout)["anchor_date"] == (_ANCHOR - timedelta(days=3)).isoformat()
    assert _inventory(root) == before


def test_cli_uses_exit_three_for_a_measured_budget_exceeded(tmp_path: Path) -> None:
    root, _ = _workspace(tmp_path, days=7)

    completed = _run(root, "--budget-bytes-per-day", "1000")

    assert completed.returncode == 3
    assert completed.stderr == ""
    report = json.loads(completed.stdout)
    assert report["budget"]["exceeded"] is True
    assert len(report["budget"]["exceeded_days"]) == 7
    assert report["budget"]["peak_bytes_per_day"] == 1_000_006


def test_cli_uses_exit_two_for_invalid_input_or_unusable_artifact(tmp_path: Path) -> None:
    root, artifact = _workspace(tmp_path, days=3)

    naive = _run(root, "--as-of", "2026-09-20T00:00:00")
    negative = _run(root, "--budget-bytes-per-day", "-1")
    missing = _run(tmp_path / "missing")

    artifact.write_text("{not json}\n", encoding="utf-8")
    corrupt = _run(root)

    assert naive.returncode == negative.returncode == 2
    assert missing.returncode == 2
    assert json.loads(missing.stderr)["error"] == "workspace_path_invalid"
    assert corrupt.returncode == 2
    assert json.loads(corrupt.stderr)["error"].startswith(
        "storage observability artifact is unusable"
    )
    assert corrupt.stdout == ""


def test_cli_reports_an_absent_artifact_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    paths = WorkspaceService(environ={}, home=tmp_path / "home").initialize(root).paths
    before = _inventory(root)

    completed = _run(root)

    assert completed.returncode == 0
    assert not (paths.state_root / _ARTIFACT_NAME).exists()
    assert _inventory(root) == before
    report = json.loads(completed.stdout)
    assert report["artifact_present"] is False
    assert report["anchor_date"] is None
    assert report["windows"] == []
    assert report["budget"] is None
