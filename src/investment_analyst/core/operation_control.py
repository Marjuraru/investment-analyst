"""Process-local cooperative cancellation for one active operation."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from time import monotonic

_CANCEL_POLL_SECONDS = 0.05


class OperationCancelledError(RuntimeError):
    """Raised only at a safe cooperative operation boundary."""


class OperationControl:
    """Non-persisted cancellation control optionally bound to one stop event."""

    def __init__(self, *, stop_event: Event | None = None) -> None:
        self._stop_event = stop_event
        self._cancel_event = Event()

    @property
    def cancelled(self) -> bool:
        """Return whether this operation has been asked to stop."""
        return self._cancel_event.is_set() or (
            self._stop_event is not None and self._stop_event.is_set()
        )

    def cancel(self) -> None:
        """Cancel this control without persisting or changing any job state."""
        self._cancel_event.set()

    def raise_if_cancelled(self) -> None:
        """Raise at the caller's next explicit safe boundary."""
        if self.cancelled:
            raise OperationCancelledError("operation cancellation requested")

    def wait(self, timeout_seconds: float) -> bool:
        """Wait interruptibly and return whether cancellation was requested."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        deadline = monotonic() + timeout_seconds
        while True:
            if self.cancelled:
                return True
            remaining = deadline - monotonic()
            if remaining <= 0:
                return self.cancelled
            self._cancel_event.wait(min(remaining, _CANCEL_POLL_SECONDS))


_CURRENT_OPERATION_CONTROL: ContextVar[OperationControl | None] = ContextVar(
    "current_operation_control",
    default=None,
)


def current_operation_control() -> OperationControl | None:
    """Return the control bound to the current scheduler execution context."""
    return _CURRENT_OPERATION_CONTROL.get()


@contextmanager
def operation_control_scope(control: OperationControl | None) -> Iterator[None]:
    """Bind one control to the current thread for the duration of a job."""
    token = _CURRENT_OPERATION_CONTROL.set(control)
    try:
        yield
    finally:
        _CURRENT_OPERATION_CONTROL.reset(token)


def check_operation_cancelled() -> None:
    """Raise when the current scheduler-bound operation was cancelled."""
    control = current_operation_control()
    if control is not None:
        control.raise_if_cancelled()


def wait_for_operation_cancelled(timeout_seconds: float) -> bool:
    """Wait on the current control, preserving ordinary callers' sleep behavior."""
    control = current_operation_control()
    return control.wait(timeout_seconds) if control is not None else False
