"""Focused transport behavior without a provider account or credential."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.workspace.drive_transport import (
    DriveTransport,
    DriveTransportConfig,
    DriveTransportError,
    _CommandResult,
)


def _config() -> DriveTransportConfig:
    return DriveTransportConfig(
        rclone_path=Path("/opt/rclone/rclone"),
        encrypted_remote="workspace_crypt",
        folder_id="folder-id",
        max_attempts=2,
        timeout_seconds=30,
    )


def _transport(results: list[_CommandResult]) -> DriveTransport:
    def run(command: Sequence[str], timeout_seconds: int) -> _CommandResult:
        assert command[0] == "/opt/rclone/rclone"
        assert timeout_seconds == 30
        return results.pop(0)

    return DriveTransport(
        _config(),
        run=run,
        clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        timer=iter((1.0, 1.5, 2.0, 2.25)).__next__,
    )


def test_transient_failure_retries_once_then_records_success() -> None:
    transport = _transport(
        [
            _CommandResult(
                returncode=5, stdout="", stderr="token=secret transient", timed_out=False
            ),
            _CommandResult(returncode=0, stdout="ok", stderr="", timed_out=False),
        ]
    )
    attempts = transport.copy_sealed_object(Path(__file__), "objects/one.gz")
    assert [item.outcome for item in attempts] == ["transient_failure", "success"]
    assert attempts[0].diagnostic == "<redacted> transient"
    assert attempts[1].duration_seconds == 0.25


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            _CommandResult(returncode=1, stdout="", stderr="permission denied", timed_out=False),
            "persistent_failure",
        ),
        (_CommandResult(returncode=None, stdout="", stderr="", timed_out=True), "timeout"),
        (
            _CommandResult(returncode=99, stdout="", stderr="bad result", timed_out=False),
            "persistent_failure",
        ),
    ],
)
def test_non_transient_outcomes_fail_closed_with_sanitized_attempt(
    result: _CommandResult, expected: str
) -> None:
    transport = _transport([result])
    with pytest.raises(DriveTransportError) as raised:
        transport.copy_sealed_object(Path(__file__), "objects/two.gz")
    assert len(raised.value.attempts) == 1
    assert raised.value.attempts[0].outcome == expected


@pytest.mark.parametrize("configured_remote", ("workspace_crypt", "workspace_crypt:"))
def test_probe_uses_the_root_prebound_in_the_crypt_remote(
    configured_remote: str,
) -> None:
    commands: list[Sequence[str]] = []

    def run(command: Sequence[str], timeout_seconds: int) -> _CommandResult:
        commands.append(command)
        return _CommandResult(returncode=0, stdout="ok", stderr="", timed_out=False)

    transport = DriveTransport(
        _config().model_copy(update={"encrypted_remote": configured_remote}), run=run
    )
    attempts = transport.probe()
    assert len(attempts) == 2
    assert commands[1][2] == "workspace_crypt:"
    assert "folder-id" not in " ".join(commands[1])


def test_unsafe_source_or_remote_object_is_rejected_before_subprocess() -> None:
    transport = _transport([])
    with pytest.raises(DriveTransportError, match="unsafe"):
        transport.copy_sealed_object(Path(__file__), "../outside")
    with pytest.raises(DriveTransportError, match="missing"):
        transport.copy_sealed_object(Path("/missing"), "objects/three.gz")
