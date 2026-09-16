"""Tests for the deterministic read-only storage observability report."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from investment_analyst.application.storage_observability import (
    StorageObservabilityDailyJobSummary,
    StorageObservabilityDailySnapshot,
)
from investment_analyst.application.storage_observability_report import (
    DEFAULT_BUDGET_BYTES_PER_DAY,
    StorageObservabilityBudgetAlert,
    StorageObservabilityReport,
    StorageObservabilityReportDay,
    StorageObservabilityReportError,
    StorageObservabilityReportService,
    StorageObservabilityReportWindow,
)

ROOT = Path(__file__).resolve().parents[3]
_ARTIFACT_NAME = "storage_observability_v1.jsonl"
_JOB_ID = "equity:us:aapl:market-daily"
_ANCHOR = date(2026, 9, 20)
_MIGRATIONS = ROOT / "src/investment_analyst/storage/migrations"


def _day_snapshot(
    day: date,
    *,
    database_bytes_delta: int,
    wal_bytes_delta: int = 0,
    total_ms: int = 1_000,
    rows_created: int = 0,
    attempts: int = 1,
) -> StorageObservabilityDailySnapshot:
    return StorageObservabilityDailySnapshot(
        utc_date=day,
        record_count=attempts,
        job_summaries=(
            StorageObservabilityDailyJobSummary(
                job_id=_JOB_ID,
                attempt_count=attempts,
                attempts_with_evidence=attempts,
                database_bytes_delta=database_bytes_delta,
                wal_bytes_delta=wal_bytes_delta,
                rows_created=rows_created,
                rows_reused=0,
                total_ms=total_ms,
            ),
        ),
    )


def _line(item: StorageObservabilityDailySnapshot) -> str:
    return json.dumps(
        item.to_json_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_artifact(state_root: Path, items: tuple[StorageObservabilityDailySnapshot, ...]) -> Path:
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / _ARTIFACT_NAME
    path.write_text("".join(f"{_line(item)}\n" for item in items), encoding="utf-8")
    return path


def _populated_state_root(root: Path, days: int, *, wal_bytes_delta: int = 0) -> Path:
    state_root = root / "state"
    _write_artifact(
        state_root,
        tuple(
            _day_snapshot(
                _ANCHOR - timedelta(days=offset),
                database_bytes_delta=1_000_000 + offset,
                wal_bytes_delta=wal_bytes_delta,
                total_ms=7_000 + offset,
                rows_created=offset,
            )
            for offset in reversed(range(days))
        ),
    )
    return state_root


def _render(report: StorageObservabilityReport) -> str:
    return json.dumps(
        report.to_json_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {name for item in value.values() for name in _keys(item)}
    if isinstance(value, list):
        return {name for item in value for name in _keys(item)}
    return set()


def _inventory(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (str(path.relative_to(root)), path.stat().st_size, path.read_bytes().hex())
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )


def test_report_contract_is_frozen_and_versioned(tmp_path: Path) -> None:
    report = StorageObservabilityReportService(state_root=tmp_path / "state").report()

    assert report.schema_version == "storage-observability-report-v1"
    assert report.to_json_dict()["schema_version"] == "storage-observability-report-v1"
    for model in (
        StorageObservabilityBudgetAlert,
        StorageObservabilityReport,
        StorageObservabilityReportDay,
        StorageObservabilityReportWindow,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"
        for field in model.model_fields.values():
            assert "Any" not in str(field.annotation)
    with pytest.raises(ValidationError):
        report.schema_version = "storage-observability-report-v2"
    with pytest.raises(ValidationError):
        StorageObservabilityReport(**{**report.model_dump(), "unexpected_field": 1})
    with pytest.raises(ValidationError):
        StorageObservabilityReport(
            **{**report.model_dump(), "schema_version": "storage-observability-report-v2"}
        )


def test_seven_and_thirty_day_windows_aggregate_daily_snapshots(tmp_path: Path) -> None:
    state_root = _populated_state_root(tmp_path, 10, wal_bytes_delta=100)

    report = StorageObservabilityReportService(state_root=state_root).report()

    assert report.artifact_present is True
    assert report.retained_days == 10
    assert report.unfolded_record_count == 0
    assert report.anchor_date == _ANCHOR
    assert tuple(item.window_days for item in report.windows) == (7, 30)

    week = report.windows[0]
    assert week.start_date == _ANCHOR - timedelta(days=6)
    assert week.end_date == _ANCHOR
    assert week.declared_days == tuple(
        _ANCHOR - timedelta(days=offset) for offset in range(6, -1, -1)
    )
    assert week.missing_days == ()
    assert week.attempts == 7
    assert week.database_bytes_delta == sum(1_000_000 + offset for offset in range(7))
    assert week.wal_bytes_delta == 700
    assert week.rows_created == sum(range(7))
    assert week.rows_reused == 0
    assert week.total_ms == sum(7_000 + offset for offset in range(7))
    assert week.mean_daily_bytes_delta == week.total_bytes_delta // 7
    assert tuple(item.utc_date for item in week.days) == week.declared_days

    month = report.windows[1]
    assert month.start_date == _ANCHOR - timedelta(days=29)
    assert len(month.declared_days) == 10
    assert len(month.missing_days) == 20
    assert month.attempts == 10
    assert month.database_bytes_delta == sum(1_000_000 + offset for offset in range(10))
    assert month.mean_daily_bytes_delta == month.total_bytes_delta // 10
    assert month.total_bytes_delta == sum(item.total_bytes_delta for item in month.days)


def test_window_declares_missing_days_instead_of_interpolating(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    declared = (_ANCHOR - timedelta(days=6), _ANCHOR - timedelta(days=3), _ANCHOR)
    _write_artifact(
        state_root,
        tuple(_day_snapshot(day, database_bytes_delta=1_000_000) for day in declared),
    )

    report = StorageObservabilityReportService(state_root=state_root).report()
    week = report.windows[0]

    assert week.declared_days == declared
    assert week.missing_days == (
        _ANCHOR - timedelta(days=5),
        _ANCHOR - timedelta(days=4),
        _ANCHOR - timedelta(days=2),
        _ANCHOR - timedelta(days=1),
    )
    assert len(week.declared_days) + len(week.missing_days) == 7
    assert tuple(item.utc_date for item in week.days) == declared
    assert week.database_bytes_delta == 3_000_000
    assert week.total_bytes_delta == 3_000_000
    assert week.mean_daily_bytes_delta == 1_000_000
    assert week.attempts == 3
    assert report.retained_days == 3


def test_budget_alert_compares_measured_growth_against_threshold(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _write_artifact(
        state_root,
        (
            _day_snapshot(_ANCHOR - timedelta(days=4), database_bytes_delta=10_000_000),
            _day_snapshot(_ANCHOR - timedelta(days=3), database_bytes_delta=40_000_000),
            _day_snapshot(_ANCHOR - timedelta(days=2), database_bytes_delta=30_000_000),
            _day_snapshot(_ANCHOR - timedelta(days=1), database_bytes_delta=31_000_000),
            _day_snapshot(_ANCHOR, database_bytes_delta=5_000_000, wal_bytes_delta=2_000),
        ),
    )

    exceeded = StorageObservabilityReportService(
        state_root=state_root,
        budget_bytes_per_day=30_000_000,
    ).report()

    assert exceeded.budget is not None
    assert exceeded.budget.threshold_bytes_per_day == 30_000_000
    assert exceeded.budget.evaluated_days == 5
    assert exceeded.budget.exceeded_days == (
        _ANCHOR - timedelta(days=3),
        _ANCHOR - timedelta(days=1),
    )
    assert exceeded.budget.exceeded is True
    assert exceeded.budget.peak_day == _ANCHOR - timedelta(days=3)
    assert exceeded.budget.peak_bytes_per_day == 40_000_000
    assert exceeded.windows[0].days[-1].total_bytes_delta == 5_002_000

    within = StorageObservabilityReportService(
        state_root=state_root,
        budget_bytes_per_day=100_000_000,
    ).report()

    assert within.budget is not None
    assert within.budget.exceeded_days == ()
    assert within.budget.exceeded is False
    assert within.budget.peak_bytes_per_day == 40_000_000
    assert within.budget.evaluated_days == exceeded.budget.evaluated_days

    default = StorageObservabilityReportService(state_root=state_root).report()
    assert default.budget is not None
    assert default.budget.threshold_bytes_per_day == DEFAULT_BUDGET_BYTES_PER_DAY
    with pytest.raises(StorageObservabilityReportError, match="negative"):
        StorageObservabilityReportService(state_root=state_root, budget_bytes_per_day=-1)


def test_budget_alert_declares_an_unevaluated_window(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _write_artifact(
        state_root,
        (_day_snapshot(_ANCHOR - timedelta(days=40), database_bytes_delta=1_000_000),),
    )

    report = StorageObservabilityReportService(state_root=state_root).report(as_of=_ANCHOR)

    assert report.budget is not None
    assert report.budget.evaluated_days == 0
    assert report.budget.exceeded is False
    assert report.budget.exceeded_days == ()
    assert report.budget.peak_day is None
    assert report.budget.peak_bytes_per_day is None
    assert report.windows[0].declared_days == ()
    assert report.windows[0].mean_daily_bytes_delta is None
    assert len(report.windows[0].missing_days) == 7
    assert report.retained_days == 1


def test_report_is_byte_identical_across_repeated_runs(tmp_path: Path) -> None:
    state_root = _populated_state_root(tmp_path, 9)
    service = StorageObservabilityReportService(state_root=state_root)

    first = _render(service.report())
    second = _render(service.report())
    third = _render(StorageObservabilityReportService(state_root=state_root).report())
    anchored = _render(service.report(as_of=_ANCHOR - timedelta(days=1)))

    assert first == second == third
    assert anchored != first
    assert json.loads(first)["anchor_date"] == _ANCHOR.isoformat()


def test_report_runs_against_writer_or_restored_state_root(tmp_path: Path) -> None:
    original = _populated_state_root(tmp_path / "writer", 12, wal_bytes_delta=7)
    restored = tmp_path / "restored"
    shutil.copytree(original, restored / "state")

    from_writer = StorageObservabilityReportService(state_root=original).report()
    from_restored = StorageObservabilityReportService(state_root=restored / "state").report()

    assert _render(from_writer) == _render(from_restored)
    assert from_writer.retained_days == from_restored.retained_days == 12
    assert from_restored.budget is not None and from_restored.budget.exceeded is False
    assert (restored / "state" / _ARTIFACT_NAME).read_bytes() == (
        original / _ARTIFACT_NAME
    ).read_bytes()


def test_report_creates_no_duckdb_object_or_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = _populated_state_root(tmp_path, 8)
    migrations = sorted(item.name for item in _MIGRATIONS.iterdir())

    def refuse_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("the report must not open the engine")

    monkeypatch.setattr(duckdb, "connect", refuse_engine)

    report = StorageObservabilityReportService(state_root=state_root).report()

    assert report.artifact_present is True
    assert report.retained_days == 8
    assert sorted(item.name for item in _MIGRATIONS.iterdir()) == migrations
    assert sorted(item.name for item in state_root.iterdir()) == [_ARTIFACT_NAME]


def test_report_is_operational_and_never_analytical_evidence(tmp_path: Path) -> None:
    state_root = _populated_state_root(tmp_path, 3)
    report = StorageObservabilityReportService(state_root=state_root).report()
    payload = report.to_json_dict()

    fields = {
        name
        for model in (
            StorageObservabilityBudgetAlert,
            StorageObservabilityReport,
            StorageObservabilityReportDay,
            StorageObservabilityReportWindow,
        )
        for name in model.model_fields
    }
    assert fields.isdisjoint(
        {"available_at", "known_at", "computed_at", "value", "verdict", "quality"}
    )
    assert "storage_observability_report" in StorageObservabilityReport.__module__
    assert "available_at" not in json.dumps(payload)
    assert payload["schema_version"] == "storage-observability-report-v1"
    assert set(payload) == {
        "anchor_date",
        "artifact_present",
        "budget",
        "retained_days",
        "schema_version",
        "unfolded_record_count",
        "windows",
    }
    assert state_root.name == "state" and "storage" not in state_root.parts


def test_report_does_not_depend_on_current_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = _populated_state_root(tmp_path, 10)
    service = StorageObservabilityReportService(state_root=state_root)

    monkeypatch.chdir(tmp_path)
    from_root = _render(service.report())
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    from_nested = _render(StorageObservabilityReportService(state_root=state_root).report())
    monkeypatch.chdir(ROOT)
    from_repository = _render(service.report())

    assert from_root == from_nested == from_repository


def test_report_never_writes_to_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    state_root = _populated_state_root(root, 11)
    artifact = state_root / _ARTIFACT_NAME
    before = _inventory(root)

    service = StorageObservabilityReportService(state_root=state_root)
    for as_of in (None, _ANCHOR, _ANCHOR - timedelta(days=45)):
        service.report(as_of=as_of)

    assert _inventory(root) == before
    assert sorted(item.name for item in state_root.iterdir()) == [_ARTIFACT_NAME]
    assert artifact.read_text(encoding="utf-8").endswith("\n")
    assert not any(".tmp" in path for path, _, _ in _inventory(root))


def test_report_rejects_an_unusable_artifact(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)
    (state_root / _ARTIFACT_NAME).write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(StorageObservabilityReportError, match="unusable"):
        StorageObservabilityReportService(state_root=state_root).report()


def test_budget_alert_never_reaches_candidate_outbox(tmp_path: Path) -> None:
    module = (
        ROOT / "src" / "investment_analyst" / "application" / "storage_observability_report.py"
    ).read_text(encoding="utf-8")
    state_root = _populated_state_root(tmp_path, 7)
    probe = (
        "import json, sys;"
        "from pathlib import Path;"
        "from investment_analyst.application.storage_observability_report import"
        " StorageObservabilityReportService;"
        "report = StorageObservabilityReportService(state_root=Path(sys.argv[1])).report();"
        "print(json.dumps(report.to_json_dict(), sort_keys=True));"
        "print([name for name in sys.modules if name.startswith('investment_analyst.alerts')])"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(state_root)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload, imported = completed.stdout.splitlines()
    document = json.loads(payload)

    assert [name for name in imported.strip("[]").split(", ") if name] == []
    assert document["budget"]["exceeded"] is False
    assert _keys(document).isdisjoint({"candidate", "outbox", "signal", "recommendation", "alert"})
    tree = ast.parse(module)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any("alerts" in name or "outbox" in name for name in imports)
