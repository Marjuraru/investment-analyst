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


def test_delta_batch_is_selected_bounded_and_immutable_only_on_upload(tmp_path: Path) -> None:
    keys = ("physical-delta/objects/" + "a" * 64 + ".bin",)
    source = tmp_path / "staging"
    (source / keys[0]).parent.mkdir(parents=True)
    (source / keys[0]).write_bytes(b"sealed")
    commands = []

    def run(command: Sequence[str], timeout_seconds: int) -> _CommandResult:
        assert timeout_seconds == 30
        selected = Path(command[command.index("--files-from-raw") + 1]).read_text().splitlines()
        assert selected == list(keys)
        commands.append(command)
        return _CommandResult(returncode=0, stdout="", stderr="", timed_out=False)

    transport = DriveTransport(_config(), run=run)
    transport.copy_immutable_batch(source, keys)
    transport.fetch_sealed_batch(keys, tmp_path / "independent")
    assert commands[0][1:4] == ("copy", str(source), "workspace_crypt:")
    assert commands[1][1:4] == ("copy", "workspace_crypt:", str(tmp_path / "independent"))
    assert "--immutable" in commands[0] and "--immutable" not in commands[1]
    for command in commands:
        assert "--transfers=4" in command and "--buffer-size=256K" in command
        assert "--multi-thread-streams=0" in command
        assert "--ignore-existing" not in command
    for invalid in ((), keys * 33, ("../other",), ("manifests/parent.json",)):
        with pytest.raises(DriveTransportError, match="batch keys"):
            transport.copy_immutable_batch(source, invalid)
    with pytest.raises(DriveTransportError, match="destination"):
        transport.fetch_sealed_batch(keys, source)
    assert len(commands) == 2


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


def test_immutable_publish_never_requests_overwrite_or_size_equality() -> None:
    commands: list[Sequence[str]] = []

    def run(command: Sequence[str], timeout_seconds: int) -> _CommandResult:
        commands.append(command)
        return _CommandResult(returncode=0, stdout="", stderr="", timed_out=False)

    DriveTransport(_config(), run=run).copy_immutable_object(Path(__file__), "objects/sealed")
    assert commands[0][-1] == "--immutable"
    assert "--checksum" not in commands[0]
    assert "--ignore-existing" not in commands[0]


@pytest.mark.parametrize("reported", [None, 0, 12])
def test_transfer_measurement_uses_rclone_cumulative_counter_and_preserves_unknown(
    reported: int | None,
) -> None:
    import json

    observed: list[tuple[str, str, int | None]] = []

    def run(command: Sequence[str], timeout_seconds: int) -> _CommandResult:
        assert "--immutable" in command
        assert "--use-json-log" in command and "--stats=1h" in command
        stderr = (
            "unstructured diagnostic\n"
            if reported is None
            else json.dumps({"stats": {"bytes": 0}})
            + "\n"
            + json.dumps({"stats": {"bytes": reported}})
        )
        return _CommandResult(returncode=0, stdout="", stderr=stderr, timed_out=False)

    transport = DriveTransport(
        _config(),
        run=run,
        transfer_observer=lambda direction, key, value: observed.append((direction, key, value)),
    )
    transport.copy_immutable_object(Path(__file__), "physical-delta/objects/sealed.bin")
    assert observed == [("upload", "physical-delta/objects/sealed.bin", reported)]


def test_transfer_measurement_counts_each_retry_without_summing_periodic_snapshots() -> None:
    results = [
        _CommandResult(returncode=5, stdout="", stderr='{"stats":{"bytes":4}}', timed_out=False),
        _CommandResult(
            returncode=0,
            stdout="",
            stderr='{"stats":{"bytes":4}}\n{"stats":{"bytes":12}}',
            timed_out=False,
        ),
    ]
    observed: list[int | None] = []
    transport = DriveTransport(
        _config(),
        run=lambda command, timeout: results.pop(0),
        transfer_observer=lambda direction, key, value: observed.append(value),
    )
    transport.copy_immutable_object(Path(__file__), "objects/sealed")
    assert observed == [4, 12]


def test_download_measurement_is_separate_from_upload(tmp_path: Path) -> None:
    observed: list[tuple[str, str, int | None]] = []

    def run(command: Sequence[str], timeout: int) -> _CommandResult:
        Path(command[3]).write_bytes(b"data")
        return _CommandResult(
            returncode=0, stdout="", stderr='{"stats":{"bytes":4}}', timed_out=False
        )

    target = tmp_path / "downloaded"
    transport = DriveTransport(
        _config(),
        run=run,
        transfer_observer=lambda direction, key, value: observed.append((direction, key, value)),
    )
    transport.fetch_sealed_object("physical-delta/controls/sealed.json", target)
    assert target.read_bytes() == b"data"
    assert observed == [("download", "physical-delta/controls/sealed.json", 4)]


def test_timeout_bytes_are_decoded_before_sanitization(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30, output=b"partial", stderr=b"token=secret\xff")

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(DriveTransportError) as raised:
        DriveTransport(_config()).copy_immutable_object(Path(__file__), "objects/sealed")
    assert raised.value.attempts[0].outcome == "timeout"
    assert "secret" not in raised.value.attempts[0].diagnostic
