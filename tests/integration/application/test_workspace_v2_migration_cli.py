"""Administrative migration CLI integration coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from investment_analyst.workspace.service import WorkspaceService


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
