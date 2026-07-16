"""Crash-safe local lock and atomic state persistence for operational runs."""

import fcntl
import json
import os
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

from investment_analyst.application.operational_models import AaplDailyRunState


class AaplOperationalStateError(RuntimeError):
    """Raised when operational state cannot be read or written safely."""


class AaplDailyRunAlreadyRunningError(AaplOperationalStateError):
    """Raised when another process already holds the workspace run lock."""


class AaplDailyRunLock:
    """Non-blocking advisory process lock retained as a stable workspace file."""

    def __init__(self, path: Path, *, run_id: UUID, started_at: str) -> None:
        self._path = path
        self._run_id = run_id
        self._started_at = started_at
        self._descriptor: int | None = None

    def __enter__(self) -> "AaplDailyRunLock":
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise AaplOperationalStateError("operational lock could not be opened") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise AaplDailyRunAlreadyRunningError(
                "another Apple operational run already holds the workspace lock"
            ) from error
        except OSError as error:
            os.close(descriptor)
            raise AaplOperationalStateError("operational lock could not be acquired") from error
        try:
            metadata = json.dumps(
                {
                    "pid": os.getpid(),
                    "run_id": str(self._run_id),
                    "started_at": self._started_at,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, metadata)
            os.fsync(descriptor)
        except OSError as error:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise AaplOperationalStateError(
                "operational lock metadata could not be written"
            ) from error
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def is_held(path: Path) -> bool:
        """Inspect an existing lock without creating or modifying its file."""
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise AaplOperationalStateError("operational lock could not be inspected") from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        except OSError as error:
            raise AaplOperationalStateError("operational lock could not be inspected") from error
        finally:
            os.close(descriptor)


class AaplDailyRunStateStore:
    """Read and atomically replace the latest versioned operational state."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)

    @property
    def path(self) -> Path:
        """Return the absolute state path used by this store."""
        return self._path

    def load(self) -> AaplDailyRunState | None:
        """Load valid state without creating files when no state exists."""
        if not self._path.exists():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
            return AaplDailyRunState.model_validate_json(text)
        except (OSError, UnicodeError, ValueError) as error:
            raise AaplOperationalStateError(
                "operational state is malformed or unreadable"
            ) from error

    def write(self, state: AaplDailyRunState) -> None:
        """Durably replace state using a private same-directory temporary file."""
        document = (
            json.dumps(
                state.to_json_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        descriptor: int | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise AaplOperationalStateError("operational state could not be written") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
