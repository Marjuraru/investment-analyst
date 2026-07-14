"""Subprocess tests for workspace initialization and inspection commands."""

import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parents[3]


def _environment(*, workspace: Path | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    source = str(_PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = source if not existing else f"{source}{os.pathsep}{existing}"
    environment.pop("INVESTMENT_ANALYST_WORKSPACE", None)
    environment.pop("XDG_DATA_HOME", None)
    environment["UNRELATED_SECRET"] = "must-not-be-printed"
    if workspace is not None:
        environment["INVESTMENT_ANALYST_WORKSPACE"] = str(workspace)
    return environment


def _run(
    script: str,
    *arguments: str,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PROJECT_ROOT / "scripts" / script), *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_init_and_inspect_scripts_are_idempotent_and_cwd_independent(tmp_path) -> None:
    workspace = (tmp_path / "persistent" / "workspace").resolve()
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    environment = _environment()

    first = _run(
        "init_workspace.py",
        "--workspace",
        str(workspace),
        cwd=cwd,
        environment=environment,
    )
    second = _run(
        "init_workspace.py",
        "--workspace",
        str(workspace),
        cwd=cwd,
        environment=environment,
    )
    inspected = _run(
        "inspect_workspace.py",
        "--workspace",
        str(workspace),
        cwd=cwd,
        environment=environment,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert inspected.returncode == 0, inspected.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    inspection = json.loads(inspected.stdout)
    assert first_payload["workspace_id"] == second_payload["workspace_id"]
    assert first_payload["reused"] is False
    assert second_payload["reused"] is True
    assert inspection["status"] == "ready"
    assert inspection["counts"] == {
        "diagnostic_results": 0,
        "metric_results": 0,
        "observations": 0,
        "raw_records": 0,
    }
    combined = first.stdout + first.stderr + second.stdout + second.stderr + inspected.stdout
    assert "must-not-be-printed" not in combined
    assert "Traceback" not in combined


def test_scripts_use_environment_and_missing_inspection_fails_cleanly(tmp_path) -> None:
    workspace = (tmp_path / "environment-workspace").resolve()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    environment = _environment(workspace=workspace)

    initialized = _run(
        "init_workspace.py",
        cwd=cwd,
        environment=environment,
    )
    inspected = _run(
        "inspect_workspace.py",
        cwd=cwd,
        environment=environment,
    )
    missing = _run(
        "inspect_workspace.py",
        "--workspace",
        str((tmp_path / "missing").resolve()),
        cwd=cwd,
        environment=environment,
    )

    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["workspace"] == str(workspace)
    assert inspected.returncode == 0, inspected.stderr
    assert missing.returncode != 0
    assert "manifest was not found" in missing.stderr
    assert "Traceback" not in missing.stderr
    assert not (tmp_path / "missing").exists()
