"""Fixed Windows PowerShell bridge for local toast transport."""

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from xml.etree import ElementTree

from investment_analyst.alerts.desktop_notification_models import (
    DesktopNotificationReason,
)

AUMID = "Marjuraru.InvestmentAnalyst"
SHORTCUT_NAME = "Investment Analyst.lnk"
MAX_OUTPUT_BYTES = 64 * 1024
TIMEOUT_SECONDS = 10.0

_POWERSHELL_EXECUTABLE = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
_DEFAULT_WSL_INTEROP = Path("/run/WSL/1_interop")
_SCRIPT_NAME = "windows_notifications.ps1"


def script_path() -> Path:
    """Resolve the fixed release script for a source checkout or installed release."""
    source = Path(__file__).resolve().parents[3] / "scripts" / _SCRIPT_NAME
    if source.is_file():
        return source
    release = Path(sys.prefix).parent / "scripts" / _SCRIPT_NAME
    if release.is_file():
        return release
    return source


_PROBE_REASON_CODES: dict[str, DesktopNotificationReason] = {
    "winrt_unavailable": DesktopNotificationReason.WINRT_UNAVAILABLE,
    "session_unavailable": DesktopNotificationReason.SESSION_UNAVAILABLE,
    "registration_failed": DesktopNotificationReason.REGISTRATION_FAILED,
    "send_error": DesktopNotificationReason.SEND_ERROR,
    "invalid_result": DesktopNotificationReason.INVALID_RESULT,
}


class ToastSendStatus(StrEnum):
    """Transport outcome for one toast in one bounded batch."""

    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToastMessage:
    """One compact toast payload with deterministic Windows identity."""

    tag: str
    group: str
    title: str
    body: str

    def xml(self) -> str:
        """Build escaped ToastGeneric XML through the ElementTree API."""
        toast = ElementTree.Element(
            "toast",
            {
                "activationType": "protocol",
                "launch": "http://127.0.0.1:8765",
                "scenario": "default",
                "duration": "short",
            },
        )
        visual = ElementTree.SubElement(toast, "visual")
        binding = ElementTree.SubElement(visual, "binding", {"template": "ToastGeneric"})
        ElementTree.SubElement(binding, "text").text = self.title
        ElementTree.SubElement(binding, "text").text = self.body
        ElementTree.SubElement(toast, "audio", {"silent": "true"})
        return ElementTree.tostring(toast, encoding="unicode")


@dataclass(frozen=True)
class ToastSendOutcome:
    """Per-toast result that never claims visual observation."""

    tag: str
    status: ToastSendStatus
    reason: DesktopNotificationReason | None


@dataclass(frozen=True)
class WindowsCapability:
    """Transport capability evidence without assuming the OS shows banners."""

    available: bool
    reason: DesktopNotificationReason | None
    powershell_version: str | None
    aumid_registered: bool


@dataclass(frozen=True)
class _Invocation:
    """One bounded subprocess result."""

    parsed: dict[str, object] | None
    failure: DesktopNotificationReason | None
    invoked: bool


class WindowsNotificationError(RuntimeError):
    """Raised when the fixed bridge cannot be used before any send attempt."""

    def __init__(self, reason: DesktopNotificationReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class WindowsToastAdapter:
    """Invoke only the fixed release script with a verified WSL interop context."""

    def __init__(
        self,
        *,
        executable: Path | None = None,
        script: Path | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        self._executable = (executable or _POWERSHELL_EXECUTABLE).expanduser()
        self._script = (script or script_path()).expanduser()
        self._environ = dict(os.environ if environ is None else environ)
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def interop_path(self) -> Path | None:
        """Resolve one verified interop socket without scanning foreign processes."""
        explicit = self._environ.get("INVESTMENT_ANALYST_WSL_INTEROP", "").strip()
        if explicit:
            candidate = Path(explicit)
            return candidate if self._is_socket(candidate) else None
        inherited = self._environ.get("WSL_INTEROP", "").strip()
        if inherited:
            candidate = Path(inherited)
            if self._is_socket(candidate):
                return candidate
        return _DEFAULT_WSL_INTEROP if self._is_socket(_DEFAULT_WSL_INTEROP) else None

    @staticmethod
    def _is_socket(path: Path) -> bool:
        try:
            return path.is_socket()
        except OSError:
            return False

    def _preflight(self) -> DesktopNotificationReason | None:
        if not self._executable.is_file():
            return DesktopNotificationReason.POWERSHELL_MISSING
        if not self._script.is_file():
            return DesktopNotificationReason.SCRIPT_MISSING
        if self.interop_path() is None:
            return DesktopNotificationReason.BRIDGE_UNAVAILABLE
        return None

    def _invoke(self, operation: str, payload: dict[str, object] | None) -> _Invocation:
        preflight_failure = self._preflight()
        if preflight_failure is not None:
            return _Invocation(parsed=None, failure=preflight_failure, invoked=False)
        interop = self.interop_path()
        if interop is None:
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.BRIDGE_UNAVAILABLE,
                invoked=False,
            )
        environment = dict(self._environ)
        environment["WSL_INTEROP"] = str(interop)
        # The release lives on a WSL UNC path, which PowerShell classifies as
        # remote. The bypass is scoped to this child process only; the machine
        # and user execution policies are never read or modified.
        command = (
            str(self._executable),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._script),
            "-Operation",
            operation,
        )
        try:
            completed = subprocess.run(
                command,
                input=(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    if payload is not None
                    else b""
                ),
                capture_output=True,
                timeout=self._timeout_seconds,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.TIMEOUT,
                invoked=True,
            )
        except OSError:
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.POWERSHELL_MISSING,
                invoked=False,
            )
        if len(completed.stdout) > self._max_output_bytes:
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.OUTPUT_LIMIT,
                invoked=True,
            )
        if completed.returncode != 0:
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.PROCESS_EXIT,
                invoked=True,
            )
        try:
            parsed = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, ValueError):
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.INVALID_RESULT,
                invoked=True,
            )
        if not isinstance(parsed, dict):
            return _Invocation(
                parsed=None,
                failure=DesktopNotificationReason.INVALID_RESULT,
                invoked=True,
            )
        return _Invocation(parsed=parsed, failure=None, invoked=True)

    def probe(self) -> WindowsCapability:
        """Check WinRT, session, and our own AUMID shortcut without sending."""
        invocation = self._invoke("probe", None)
        if invocation.failure is not None or invocation.parsed is None:
            return WindowsCapability(
                available=False,
                reason=invocation.failure or DesktopNotificationReason.PROBE_FAILED,
                powershell_version=None,
                aumid_registered=False,
            )
        parsed = invocation.parsed
        winrt = parsed.get("winrt") is True
        interactive = parsed.get("interactive") is True
        registered = parsed.get("aumid_registered") is True
        reason: DesktopNotificationReason | None = None
        if not winrt:
            reason = DesktopNotificationReason.WINRT_UNAVAILABLE
        elif not interactive:
            reason = DesktopNotificationReason.SESSION_UNAVAILABLE
        elif not registered:
            reason = DesktopNotificationReason.REGISTRATION_FAILED
        version = parsed.get("powershell_version")
        return WindowsCapability(
            available=reason is None,
            reason=reason,
            powershell_version=version if isinstance(version, str) else None,
            aumid_registered=registered,
        )

    def register(self) -> WindowsCapability:
        """Create or verify only our own user Start Menu identity."""
        invocation = self._invoke("register", None)
        if invocation.failure is not None or invocation.parsed is None:
            raise WindowsNotificationError(
                invocation.failure or DesktopNotificationReason.REGISTRATION_FAILED
            )
        if invocation.parsed.get("ok") is not True:
            raise WindowsNotificationError(DesktopNotificationReason.REGISTRATION_FAILED)
        return self.probe()

    def send(self, messages: Sequence[ToastMessage]) -> dict[str, ToastSendOutcome]:
        """Send at most one bounded batch and never equate acceptance with viewing."""
        if not messages:
            return {}
        payload = {
            "operation": "send",
            "aumid": AUMID,
            "items": [
                {
                    "tag": message.tag,
                    "group": message.group,
                    "xml": message.xml(),
                }
                for message in messages
            ],
        }
        invocation = self._invoke("send", payload)
        tags = tuple(message.tag for message in messages)
        if invocation.failure is not None or invocation.parsed is None:
            status = (
                ToastSendStatus.RETRYABLE if not invocation.invoked else ToastSendStatus.UNKNOWN
            )
            reason = invocation.failure or DesktopNotificationReason.INVALID_RESULT
            return {tag: ToastSendOutcome(tag, status, reason) for tag in tags}
        parsed = invocation.parsed
        if parsed.get("ok") is not True:
            reason = _reason(parsed.get("reason"), DesktopNotificationReason.SEND_ERROR)
            attempted = parsed.get("attempted")
            accepted_before_failure = isinstance(attempted, int) and attempted > 0
            status = (
                ToastSendStatus.UNKNOWN if accepted_before_failure else ToastSendStatus.RETRYABLE
            )
            return {tag: ToastSendOutcome(tag, status, reason) for tag in tags}
        raw_results = parsed.get("results")
        outcomes: dict[str, ToastSendOutcome] = {}
        if isinstance(raw_results, list):
            for raw in raw_results:
                if not isinstance(raw, dict):
                    continue
                tag = raw.get("tag")
                if not isinstance(tag, str) or tag not in tags:
                    continue
                if raw.get("accepted") is True:
                    outcomes[tag] = ToastSendOutcome(tag, ToastSendStatus.ACCEPTED, None)
                else:
                    outcomes[tag] = ToastSendOutcome(
                        tag,
                        ToastSendStatus.RETRYABLE,
                        _reason(raw.get("reason"), DesktopNotificationReason.SEND_ERROR),
                    )
        return {
            tag: outcomes.get(
                tag,
                ToastSendOutcome(
                    tag,
                    ToastSendStatus.UNKNOWN,
                    DesktopNotificationReason.INVALID_RESULT,
                ),
            )
            for tag in tags
        }


def _reason(value: object, default: DesktopNotificationReason) -> DesktopNotificationReason:
    if isinstance(value, str):
        return _PROBE_REASON_CODES.get(value, default)
    return default


__all__ = [
    "AUMID",
    "MAX_OUTPUT_BYTES",
    "SHORTCUT_NAME",
    "TIMEOUT_SECONDS",
    "ToastMessage",
    "ToastSendOutcome",
    "ToastSendStatus",
    "WindowsCapability",
    "WindowsNotificationError",
    "WindowsToastAdapter",
    "script_path",
]
