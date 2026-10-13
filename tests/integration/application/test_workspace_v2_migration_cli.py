"""Administrative migration CLI integration coverage."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import _require_host_capacity


@pytest.fixture
def migration_module():
    repository = Path(__file__).resolve().parents[3]
    path = repository / "scripts" / "migrate_workspace_v2.py"
    spec = importlib.util.spec_from_file_location("migration_capacity_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_budget_measures_roots_without_a_manual_byte_argument(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    destination = tmp_path / "target-v2"
    snapshot = tmp_path / "source-copy-b"
    destination.mkdir()
    snapshot.mkdir()
    (destination / "partial.bin").write_bytes(b"partial-target")
    (snapshot / "source.bin").write_bytes(b"published-source")
    report = tmp_path / "budget.json"
    environment = {**os.environ, "PYTHONPATH": str(repository / "src")}

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "migrate_workspace_v2.py"),
            "budget",
            "--destination",
            str(destination),
            "--source-snapshot",
            str(snapshot),
            "--report",
            str(report),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "workspace-v2-migration-capacity-budget-v2"
    assert payload["measurements"]["destination"]["allocated_bytes"] >= 0
    assert payload["measurements"]["source_snapshot"]["apparent_bytes"] == len(b"published-source")
    assert payload["historical_intermediate_status"] == "NOT_PLANNED_DIRECT_TRANSPORT"
    assert payload["phases"]["backup"]["new_write_bytes"] == "UNKNOWN"


def test_inventory_writes_streaming_entries_and_trailer_from_another_cwd(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    source = tmp_path / "source-copy"
    source.mkdir()
    (source / "raw.bin").write_bytes(b"frozen")
    inventory = tmp_path / "inventory.jsonl"
    environment = {**os.environ, "PYTHONPATH": str(repository / "src")}

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "migrate_workspace_v2.py"),
            "inventory",
            "--source",
            str(source),
            "--report",
            str(inventory),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in inventory.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["path"] == "raw.bin"
    assert lines[-1]["schema_version"] == "workspace-v2-backup-inventory-trailer-v1"


def test_cli_reports_a_compact_safe_failure_detail(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    report = tmp_path / "budget.json"
    environment = {**os.environ, "PYTHONPATH": str(repository / "src")}

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "migrate_workspace_v2.py"),
            "budget",
            "--destination",
            str(tmp_path / "missing-target"),
            "--source-snapshot",
            str(tmp_path),
            "--report",
            str(report),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "WorkspaceV2MigrationError: capacity root is unavailable or unsafe" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_status_reports_checkpoint_completion_without_claiming_equivalence(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    destination = tmp_path / "target-v2"
    WorkspaceService(environ={}, home=tmp_path / "home").initialize(destination, format_version=2)
    (destination / "state" / "direct-historical-import-state.json").write_text(
        json.dumps({"phase": "complete"}), encoding="utf-8"
    )
    report = tmp_path / "status.json"
    environment = {**os.environ, "PYTHONPATH": str(repository / "src")}

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "migrate_workspace_v2.py"),
            "status",
            "--destination",
            str(destination),
            "--report",
            str(report),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert payload["completion_status"] == "CHECKPOINT_COMPLETE"


def test_supervisor_records_a_durable_start_and_error_receipt(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    report = tmp_path / "attempt.json"
    environment = {**os.environ, "PYTHONPATH": str(repository / "src")}

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "migrate_workspace_v2.py"),
            "migrate",
            "--source",
            str(tmp_path / "missing-source"),
            "--destination",
            str(tmp_path / "missing-target"),
            "--inventory",
            str(tmp_path / "inventory.jsonl"),
            "--report",
            str(report),
            "--page-limit",
            "256",
            "--reserve-bytes",
            "5000000000",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    supervisor = json.loads(
        report.with_suffix(report.suffix + ".supervisor.json").read_text(encoding="utf-8")
    )
    assert supervisor["status"] == "error"
    assert supervisor["returncode"] == 2
    assert supervisor["process"]["pid"] > 0
    assert "start_ticks" in supervisor["process"]
    assert supervisor["safe_error"].startswith("workspace-v2-migration:")


@pytest.mark.parametrize(
    "first_failure",
    [
        OSError(errno.EAGAIN, "temporary capacity process"),
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="UtilBindVsockAnyPort: socket failed 1"
        ),
    ],
)
def test_capacity_retries_only_identified_transient_failures(
    migration_module, monkeypatch, first_failure
) -> None:
    attempts = iter(
        [first_failure, subprocess.CompletedProcess([], 0, stdout="7384997888\n", stderr="")]
    )

    def run(*args, **kwargs):
        outcome = next(attempts)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(migration_module.subprocess, "run", run)
    evidence: list[dict[str, object]] = []

    expected_outcome = "os_error" if isinstance(first_failure, OSError) else "nonzero_exit"
    assert migration_module._host_free_bytes(evidence.append) == 7_384_997_888
    assert [entry["outcome"] for entry in evidence] == [expected_outcome, "success"]
    assert evidence[0]["diagnostic"] in {"transient", "transient_wsl_vsock"}


@pytest.mark.parametrize(
    ("failure", "message", "outcome"),
    [
        (
            subprocess.CompletedProcess([], 1, stdout="", stderr="permanent"),
            "nonzero exit status",
            "nonzero_exit",
        ),
        (subprocess.TimeoutExpired(["powershell.exe"], 5), "invocation failed", "timeout"),
        (
            subprocess.CompletedProcess([], 0, stdout="not-a-number", stderr="secret"),
            "invalid value",
            "invalid_output",
        ),
    ],
)
def test_capacity_fails_closed_with_sanitized_attempt_evidence(
    migration_module, monkeypatch, failure, message: str, outcome: str
) -> None:
    def run(*args, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(migration_module.subprocess, "run", run)
    evidence: list[dict[str, object]] = []

    with pytest.raises(migration_module.WorkspaceV2MigrationError, match=message):
        migration_module._host_free_bytes(evidence.append)
    assert evidence[0]["outcome"] == outcome
    assert "secret" not in str(evidence)


def test_capacity_reserve_remains_fail_closed(migration_module) -> None:
    with pytest.raises(migration_module.WorkspaceV2MigrationError, match="reserve is insufficient"):
        _require_host_capacity(
            lambda: 7_384_997_888,
            reserve_bytes=5_000_000_000,
            expected_write_bytes=2_500_000_000,
        )
