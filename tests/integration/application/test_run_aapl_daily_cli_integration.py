"""Subprocess tests for the stateful Apple operational CLI."""

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from investment_analyst.application.operational_state import AaplDailyRunLock
from investment_analyst.workspace.service import WorkspaceService


def _environment(workspace: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["INVESTMENT_ANALYST_WORKSPACE"] = str(workspace)
    environment.pop("ALPACA_API_KEY", None)
    environment.pop("ALPACA_API_SECRET", None)
    environment.pop("SEC_USER_AGENT", None)
    return environment


def _credential_environment(workspace: Path) -> dict[str, str]:
    environment = _environment(workspace)
    environment["ALPACA_API_KEY"] = "test-key"
    environment["ALPACA_API_SECRET"] = "test-secret"
    environment["SEC_USER_AGENT"] = "Investment Analyst tests@example.com"
    return environment


def test_health_reports_ready_workspace_without_creating_run_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialization = WorkspaceService(environ={}, home=tmp_path).initialize(workspace)

    result = subprocess.run(
        [sys.executable, "scripts/run_aapl_daily.py", "health", "--workspace", str(workspace)],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(workspace),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["latest_run"] is None
    assert payload["issues"] == ["no operational run has been recorded"]
    assert not (initialization.paths.state_root / "aapl_daily_run_state.json").exists()


def test_run_requires_credentials_before_creating_workspace_or_state(tmp_path: Path) -> None:
    workspace = tmp_path / "missing"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_aapl_daily.py",
            "run",
            "--workspace",
            str(workspace),
            "--market-start",
            "2025-01-01",
            "--market-end",
            "2026-07-15",
            "--fundamental-frequency",
            "quarterly",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(workspace),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "required" in result.stderr
    assert "Traceback" not in result.stderr
    assert not workspace.exists()


def test_run_returns_concurrency_exit_without_calling_providers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    lock_path = workspace / "state" / "aapl_daily_run.lock"
    lock = AaplDailyRunLock(
        lock_path,
        run_id=uuid4(),
        started_at="2026-07-16T15:00:00+00:00",
    )

    with lock:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/run_aapl_daily.py",
                "run",
                "--workspace",
                str(workspace),
                "--market-start",
                "2025-01-01",
                "--market-end",
                "2026-07-15",
                "--fundamental-frequency",
                "quarterly",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=_credential_environment(workspace),
        )

    assert result.returncode == 4
    assert result.stdout == ""
    assert "already active" in result.stderr
    assert "Traceback" not in result.stderr


def test_local_service_requires_credentials_before_inspecting_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "missing"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/serve_investment_analyst.py",
            "--workspace",
            str(workspace),
            "--no-scheduler",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(workspace),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "required" in result.stderr
    assert "Traceback" not in result.stderr
    assert not workspace.exists()


def test_service_installer_generates_private_unit_without_starting_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)
    output = tmp_path / "units" / "investment-analyst.service"
    with TemporaryDirectory(prefix="investment-analyst-env-", dir="/tmp") as private_root:
        environment_file = Path(private_root) / "service.env"
        environment_file.write_text(
            "ALPACA_API_KEY=test-key\n"
            "ALPACA_API_SECRET=test-secret\n"
            "SEC_USER_AGENT=Investment Analyst tests@example.com\n",
            encoding="utf-8",
        )
        environment_file.chmod(0o600)

        result = subprocess.run(
            [
                sys.executable,
                "scripts/install_local_service.py",
                "--workspace",
                str(workspace),
                "--env-file",
                str(environment_file),
                "--output",
                str(output),
                "--no-scheduler",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=_environment(workspace),
        )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert '"--no-scheduler"' in output.read_text(encoding="utf-8")
    assert "systemctl --user enable --now" in result.stdout
    assert "test-secret" not in output.read_text(encoding="utf-8")
    assert "test-secret" not in result.stdout


def test_service_installer_rejects_readable_environment_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)
    environment_file = tmp_path / "service.env"
    environment_file.write_text(
        "ALPACA_API_KEY=test-key\n"
        "ALPACA_API_SECRET=test-secret\n"
        "SEC_USER_AGENT=Investment Analyst tests@example.com\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o644)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/install_local_service.py",
            "--workspace",
            str(workspace),
            "--env-file",
            str(environment_file),
            "--output",
            str(tmp_path / "unit.service"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_environment(workspace),
    )

    assert result.returncode == 2
    assert "permissions" in result.stderr
    assert "test-secret" not in result.stderr


def test_local_service_stops_cleanly_on_sigint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)
    environment = _credential_environment(workspace)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/serve_investment_analyst.py",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--no-scheduler",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout is not None
        assert "available" in process.stdout.readline()
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    assert "Traceback" not in stderr
    assert "test-secret" not in stdout
    assert "test-secret" not in stderr
