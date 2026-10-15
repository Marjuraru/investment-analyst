"""Small, secret-safe rclone transport adapter for sealed workspace artifacts."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_TRANSIENT_EXIT_CODES = frozenset({5, 6, 7, 8, 10})
_SECRET_PATTERN = re.compile(r"(?i)(token|password|secret|client_secret|authorization)=[^\s]+")


class DriveTransportError(RuntimeError):
    """A remote operation has no verified successful result."""

    def __init__(self, message: str, *, attempts: tuple[DriveAttempt, ...] = ()) -> None:
        super().__init__(message)
        self.attempts = attempts


class DriveTransportConfig(ContractModel):
    """Non-secret fixed configuration for an encrypted project namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    rclone_path: Path
    encrypted_remote: NonEmptyStr
    folder_id: NonEmptyStr
    max_attempts: int = Field(default=2, ge=1, le=3)
    timeout_seconds: int = Field(default=600, ge=1, le=3_600)

    @field_validator("rclone_path")
    @classmethod
    def require_absolute_binary(cls, value: Path) -> Path:
        if not value.is_absolute() or value.name != "rclone":
            raise ValueError("rclone path must be an absolute rclone executable")
        return value


class DriveAttempt(ContractModel):
    """Sanitized evidence for exactly one bounded subprocess attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ordinal: int = Field(ge=1)
    duration_seconds: float = Field(ge=0)
    exit_code: int | None
    outcome: str
    diagnostic: str
    at: UTCDateTime


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


class DriveTransport:
    """Run explicit rclone commands without accepting credentials as arguments."""

    def __init__(
        self,
        config: DriveTransportConfig,
        *,
        run: Callable[[Sequence[str], int], _CommandResult] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self._config = config
        self._run = run or _run_command
        self._clock = clock
        self._timer = timer

    def probe(self) -> tuple[DriveAttempt, ...]:
        """Verify the configured binary and exactly one private namespace path."""
        version = self._execute((str(self._config.rclone_path), "version"), retry_transient=False)
        namespace = self._execute(
            (
                str(self._config.rclone_path),
                "lsd",
                self._remote_path(),
                "--max-depth",
                "1",
            ),
            retry_transient=True,
        )
        return version + namespace

    def copy_sealed_object(self, source: Path, remote_object: str) -> tuple[DriveAttempt, ...]:
        """Upload one sealed regular file; a success is never retried."""
        if source.is_symlink() or not source.is_file():
            raise DriveTransportError("sealed source object is missing or unsafe")
        if not _safe_object_name(remote_object):
            raise DriveTransportError("remote object name is unsafe")
        destination = self._remote_path(remote_object)
        return self._execute(
            (str(self._config.rclone_path), "copyto", str(source), destination, "--checksum"),
            retry_transient=True,
        )

    def fetch_sealed_object(
        self, remote_object: str, destination: Path
    ) -> tuple[DriveAttempt, ...]:
        """Download one object through a new local file and atomically publish it.

        The transport command proves only that rclone completed.  Callers must
        verify the content digest before trusting this file because Drive crypt
        may not provide a common checksum with rclone.
        """
        target = destination.expanduser().resolve(strict=False)
        if (
            not _safe_object_name(remote_object)
            or not target.is_absolute()
            or target.exists()
            or target.is_symlink()
            or target.parent.is_symlink()
        ):
            raise DriveTransportError("sealed destination or remote object is unsafe")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.download")
        if temporary.exists() or temporary.is_symlink():
            raise DriveTransportError("sealed download temporary destination is unsafe")
        try:
            attempts = self._execute(
                (
                    str(self._config.rclone_path),
                    "copyto",
                    self._remote_path(remote_object),
                    str(temporary),
                    "--checksum",
                ),
                retry_transient=True,
            )
            if temporary.is_symlink() or not temporary.is_file():
                raise DriveTransportError("sealed download did not create a regular file")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
            return attempts
        finally:
            temporary.unlink(missing_ok=True)

    def _remote_path(self, remote_object: str = "") -> str:
        """Build a path below the root bound into the encrypted rclone remote.

        ``folder_id`` remains an auditable identity binding. It is intentionally
        absent from the command path: the underlying Drive remote owns that root,
        while a ``crypt`` remote would otherwise encrypt the ID as a child name.
        """
        suffix = remote_object.lstrip("/")
        remote = self._config.encrypted_remote.rstrip(":")
        return f"{remote}:{suffix}"

    def _execute(
        self, command: Sequence[str], *, retry_transient: bool
    ) -> tuple[DriveAttempt, ...]:
        attempts: list[DriveAttempt] = []
        for ordinal in range(1, self._config.max_attempts + 1):
            started = self._timer()
            result = self._run(command, self._config.timeout_seconds)
            duration = max(0.0, self._timer() - started)
            diagnostic = _sanitize(result.stderr or result.stdout)
            if result.timed_out:
                outcome = "timeout"
            elif result.returncode == 0:
                outcome = "success"
            elif result.returncode in _TRANSIENT_EXIT_CODES:
                outcome = "transient_failure"
            else:
                outcome = "persistent_failure"
            attempt = DriveAttempt(
                ordinal=ordinal,
                duration_seconds=duration,
                exit_code=result.returncode,
                outcome=outcome,
                diagnostic=diagnostic,
                at=self._now(),
            )
            attempts.append(attempt)
            if outcome == "success":
                return tuple(attempts)
            if not retry_transient or outcome != "transient_failure":
                break
        evidence = tuple(attempts)
        raise DriveTransportError(_failure_message(evidence), attempts=evidence)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise DriveTransportError("transport clock must be timezone-aware")
        return value.astimezone(UTC)


def _run_command(command: Sequence[str], timeout_seconds: int) -> _CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        return _CommandResult(
            returncode=None,
            stdout=error.stdout or "",
            stderr=error.stderr or "",
            timed_out=True,
        )
    return _CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        timed_out=False,
    )


def _safe_object_name(value: str) -> bool:
    return bool(value) and not value.startswith("/") and ".." not in Path(value).parts


def _sanitize(value: str) -> str:
    compact = " ".join(value.split())[:512]
    return _SECRET_PATTERN.sub("<redacted>", compact)


def _failure_message(attempts: Sequence[DriveAttempt]) -> str:
    if not attempts:
        return "remote command produced no attempt"
    last = attempts[-1]
    return (
        f"remote command {last.outcome}; exit_code={last.exit_code}; diagnostic={last.diagnostic}"
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DriveAttempt",
    "DriveTransport",
    "DriveTransportConfig",
    "DriveTransportError",
]
