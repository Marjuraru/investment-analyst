"""Focused host-capacity reader behavior for the migration CLI."""

from __future__ import annotations

import errno
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "migrate_workspace_v2.py"
    spec = importlib.util.spec_from_file_location("migration_capacity_cli", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_capacity_retries_identified_transient_error_then_succeeds(module, monkeypatch) -> None:
    attempts = iter(
        [
            OSError(errno.EAGAIN, "temporary process capacity"),
            subprocess.CompletedProcess([], 0, stdout="7384997888\n", stderr=""),
        ]
    )

    def run(*args, **kwargs):
        value = next(attempts)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(module.subprocess, "run", run)
    evidence = []
    assert module._host_free_bytes(evidence.append) == 7_384_997_888
    assert [item["outcome"] for item in evidence] == ["os_error", "success"]
    assert evidence[0]["diagnostic"] == "transient"


def test_capacity_retries_known_transient_wsl_exit_then_succeeds(module, monkeypatch) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="UtilBindVsockAnyPort: socket failed 1",
            ),
            subprocess.CompletedProcess([], 0, stdout="7384997888\n", stderr=""),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: next(attempts))

    evidence = []
    assert module._host_free_bytes(evidence.append) == 7_384_997_888
    assert [item["outcome"] for item in evidence] == ["nonzero_exit", "success"]
    assert evidence[0]["diagnostic"] == "transient_wsl_vsock"


def test_capacity_retries_transient_wsl_accept_timeout_then_succeeds(module, monkeypatch) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="UtilAcceptVsock: accept4 failed 110",
            ),
            subprocess.CompletedProcess([], 0, stdout="7384997888\n", stderr=""),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: next(attempts))

    evidence = []
    assert module._host_free_bytes(evidence.append) == 7_384_997_888
    assert [item["outcome"] for item in evidence] == ["nonzero_exit", "success"]
    assert evidence[0]["diagnostic"] == "transient_wsl_vsock"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (OSError(errno.ENOENT, "not found"), "invocation failed"),
        (subprocess.TimeoutExpired(["powershell.exe"], 5), "invocation failed"),
        (
            subprocess.CompletedProcess([], 0, stdout="not-a-number", stderr="secret"),
            "invalid value",
        ),
    ],
)
def test_capacity_fails_closed_with_sanitized_attempt_evidence(
    module, monkeypatch, failure, message
) -> None:
    def run(*args, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(module.subprocess, "run", run)
    evidence = []
    with pytest.raises(module.WorkspaceV2MigrationError, match=message):
        module._host_free_bytes(evidence.append)
    assert evidence
    assert all("secret" not in str(item) and "not found" not in str(item) for item in evidence)


def test_capacity_insufficient_remains_fail_closed() -> None:
    from investment_analyst.workspace.workspace_v2_migration import (
        WorkspaceV2MigrationError,
        _require_host_capacity,
    )

    with pytest.raises(WorkspaceV2MigrationError, match="reserve is insufficient"):
        _require_host_capacity(
            lambda: 7_384_997_888,
            reserve_bytes=5_000_000_000,
            expected_write_bytes=2_500_000_000,
        )


def test_smoke_persists_sanitized_capacity_attempts(tmp_path: Path, monkeypatch) -> None:
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    path = scripts / "smoke_workspace_v2_migration.py"
    spec = importlib.util.spec_from_file_location("migration_smoke_cli", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    try:
        spec.loader.exec_module(loaded)
    finally:
        sys.modules.pop(spec.name, None)

    attempts = tmp_path / "smoke.json.capacity-attempts.jsonl"
    observer = loaded._capacity_attempt_log(attempts)
    monkeypatch.setattr(
        loaded,
        "_measured_host_free_bytes",
        lambda attempt_observer: (
            attempt_observer(
                {
                    "schema_version": "workspace-v2-host-capacity-attempt-v1",
                    "ordinal": 1,
                    "duration_ms": 3,
                    "outcome": "success",
                    "returncode": 0,
                    "diagnostic": "measured",
                }
            ),
            7_384_997_888,
        )[1],
    )

    assert loaded._host_free_bytes(observer) == 7_384_997_888
    receipt = attempts.read_text(encoding="utf-8")
    assert '"duration_ms":3' in receipt
    assert '"returncode":0' in receipt
    assert '"diagnostic":"measured"' in receipt
