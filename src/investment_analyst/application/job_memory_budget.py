"""Cooperative process RSS budgets for one scheduled job."""

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Event, Lock, Thread

from investment_analyst.core.operation_control import OperationControl

_PROC_STATUS = Path("/proc/self/status")
_BYTES_PER_KILOBYTE = 1024
_DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.05


def read_process_rss_kb() -> int | None:
    """Read this process's resident set size from Linux's own status file."""
    try:
        lines = _PROC_STATUS.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        if not line.startswith("VmRSS:"):
            continue
        fields = line.split()
        if len(fields) < 3 or fields[2] != "kB":
            return None
        try:
            value = int(fields[1])
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


@dataclass(frozen=True, slots=True)
class JobMemoryBudget:
    """Immutable memory-budget configuration for one job execution."""

    ceiling_bytes: int | None = None
    sample_interval_seconds: float = _DEFAULT_SAMPLE_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.ceiling_bytes is not None and (
            isinstance(self.ceiling_bytes, bool)
            or not isinstance(self.ceiling_bytes, int)
            or self.ceiling_bytes <= 0
        ):
            raise ValueError("ceiling_bytes must be a positive integer or None")
        if (
            isinstance(self.sample_interval_seconds, bool)
            or not isinstance(self.sample_interval_seconds, (int, float))
            or not isfinite(float(self.sample_interval_seconds))
            or self.sample_interval_seconds <= 0
        ):
            raise ValueError("sample_interval_seconds must be positive and finite")

    def watch(self, operation_control: OperationControl | None) -> "JobMemoryWatchdog":
        """Create one non-persisted watchdog for this execution."""
        return JobMemoryWatchdog(self, operation_control)


class JobMemoryWatchdog:
    """Sample one process and cooperatively cancel it after a breach."""

    def __init__(
        self,
        budget: JobMemoryBudget,
        operation_control: OperationControl | None,
    ) -> None:
        self._budget = budget
        self._operation_control = operation_control
        self._stop_event = Event()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._peak_rss_kb: int | None = None
        self._breached = False
        self._cancel_requested = False

    @property
    def peak_rss_kb(self) -> int | None:
        """Return the largest observed RSS, or None when the source was unavailable."""
        with self._state_lock:
            return self._peak_rss_kb

    @property
    def breached(self) -> bool:
        """Return whether an observed RSS sample exceeded the configured ceiling."""
        with self._state_lock:
            return self._breached

    @property
    def thread_alive(self) -> bool:
        """Return whether the watchdog worker is still running."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def __enter__(self) -> "JobMemoryWatchdog":
        if self._thread is not None:
            raise RuntimeError("job memory watchdog cannot be entered twice")
        self._sample()
        self._thread = Thread(
            target=self._run,
            name="job-memory-budget",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        del exc_type, exc_value, traceback
        thread = self._thread
        if thread is None:
            raise RuntimeError("job memory watchdog was not entered")
        self._stop_event.set()
        thread.join()
        return False

    def _run(self) -> None:
        while True:
            self._sample()
            if self._stop_event.wait(float(self._budget.sample_interval_seconds)):
                break
        self._sample()

    def _sample(self) -> None:
        rss_kb = read_process_rss_kb()
        if rss_kb is None:
            return
        should_cancel = False
        with self._state_lock:
            if self._peak_rss_kb is None or rss_kb > self._peak_rss_kb:
                self._peak_rss_kb = rss_kb
            ceiling_bytes = self._budget.ceiling_bytes
            if ceiling_bytes is None or rss_kb * _BYTES_PER_KILOBYTE <= ceiling_bytes:
                return
            self._breached = True
            if self._operation_control is not None and not self._cancel_requested:
                self._cancel_requested = True
                should_cancel = True
        if should_cancel:
            self._operation_control.cancel()


__all__ = ["JobMemoryBudget", "JobMemoryWatchdog", "read_process_rss_kb"]
