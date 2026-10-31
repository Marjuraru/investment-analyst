"""Focused tests for the fixed Windows toast bridge without implicit commands."""

import socket
from pathlib import Path

import pytest

from investment_analyst.alerts import windows_notifications
from investment_analyst.alerts.desktop_notification_models import (
    DesktopNotificationReason,
)
from investment_analyst.alerts.windows_notifications import (
    ToastMessage,
    ToastSendStatus,
    WindowsNotificationError,
    WindowsToastAdapter,
)

_REAL_POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def test_script_path_resolves_the_fixed_source_and_installed_release(tmp_path, monkeypatch) -> None:
    source = (
        Path(windows_notifications.__file__).resolve().parents[3]
        / "scripts"
        / "windows_notifications.ps1"
    )
    assert windows_notifications.script_path() == source

    fake_release = tmp_path / "release"
    fake_module_dir = (
        fake_release / ".venv" / "lib" / "python3.12" / "site-packages" / "investment_analyst"
    )
    fake_module_dir.mkdir(parents=True)
    fake_module = fake_module_dir / "alerts" / "windows_notifications.py"
    fake_module.parent.mkdir()
    fake_module.write_text("# installed module\n", encoding="utf-8")
    (fake_release / "scripts").mkdir()
    release_script = fake_release / "scripts" / "windows_notifications.ps1"
    release_script.write_text("# fixed release script\n", encoding="utf-8")
    monkeypatch.setattr(windows_notifications, "__file__", str(fake_module))
    monkeypatch.setattr(windows_notifications.sys, "prefix", str(fake_release / ".venv"))
    assert windows_notifications.script_path() == release_script


def _interop_socket(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    socket_path = tmp_path / "interop.sock"
    if socket_path.exists():
        return socket_path
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
    return socket_path


def _fake_powershell(
    tmp_path: Path,
    *,
    output: str,
    exit_code: int = 0,
    sleep_seconds: float = 0,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "fake-powershell"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdin.read()\n"
        f"time.sleep({sleep_seconds})\n"
        f"sys.stdout.write({output!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _adapter(
    tmp_path: Path,
    *,
    executable: Path | None = None,
    max_output_bytes: int = 64 * 1024,
    timeout_seconds: float = 10.0,
) -> WindowsToastAdapter:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "windows_notifications.ps1"
    if not script.exists():
        script.write_text("# fixed script\n", encoding="utf-8")
    return WindowsToastAdapter(
        executable=executable or _fake_powershell(tmp_path, output="{}"),
        script=script,
        environ={"WSL_INTEROP": str(_interop_socket(tmp_path))},
        max_output_bytes=max_output_bytes,
        timeout_seconds=timeout_seconds,
    )


def test_explicit_invalid_interop_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_notifications, "_DEFAULT_WSL_INTEROP", tmp_path / "missing")
    adapter = WindowsToastAdapter(
        environ={"INVESTMENT_ANALYST_WSL_INTEROP": str(tmp_path / "missing")},
    )
    assert adapter.interop_path() is None


def test_inherited_interop_socket_is_preferred_over_the_fixed_default(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(windows_notifications, "_DEFAULT_WSL_INTEROP", tmp_path / "missing")
    inherited = _interop_socket(tmp_path)
    adapter = WindowsToastAdapter(environ={"WSL_INTEROP": str(inherited)})
    assert adapter.interop_path() == inherited


def test_missing_executable_script_or_bridge_is_a_proven_retryable_failure(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(windows_notifications, "_DEFAULT_WSL_INTEROP", tmp_path / "missing")
    message = ToastMessage("0123456789abcdef", "fedcba9876543210", "T", "B")
    missing_executable = WindowsToastAdapter(
        executable=tmp_path / "missing",
        script=tmp_path / "missing.ps1",
        environ={"WSL_INTEROP": str(_interop_socket(tmp_path))},
    )
    outcome = missing_executable.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.RETRYABLE
    assert outcome.reason is DesktopNotificationReason.POWERSHELL_MISSING

    missing_script = WindowsToastAdapter(
        executable=_fake_powershell(tmp_path, output="{}"),
        script=tmp_path / "missing.ps1",
        environ={"WSL_INTEROP": str(_interop_socket(tmp_path))},
    )
    outcome = missing_script.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.RETRYABLE
    assert outcome.reason is DesktopNotificationReason.SCRIPT_MISSING

    bridge_script = tmp_path / "script.ps1"
    bridge_script.write_text("# fixed script\n", encoding="utf-8")
    missing_bridge = WindowsToastAdapter(
        executable=_fake_powershell(tmp_path, output="{}"),
        script=bridge_script,
        environ={"WSL_INTEROP": str(tmp_path / "missing")},
    )
    outcome = missing_bridge.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.RETRYABLE
    assert outcome.reason is DesktopNotificationReason.BRIDGE_UNAVAILABLE


def test_timeout_is_unknown_and_never_retried_automatically(tmp_path) -> None:
    adapter = _adapter(
        tmp_path,
        executable=_fake_powershell(tmp_path, output="{}", sleep_seconds=5),
        timeout_seconds=0.05,
    )
    message = ToastMessage("0123456789abcdef", "fedcba9876543210", "T", "B")
    outcome = adapter.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.UNKNOWN
    assert outcome.reason is DesktopNotificationReason.TIMEOUT


def test_nonzero_exit_and_invalid_output_are_unknown(tmp_path) -> None:
    message = ToastMessage("0123456789abcdef", "fedcba9876543210", "T", "B")
    nonzero = _adapter(
        tmp_path / "nonzero",
        executable=_fake_powershell(tmp_path / "nonzero", output="{}", exit_code=2),
    )
    outcome = nonzero.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.UNKNOWN
    assert outcome.reason is DesktopNotificationReason.PROCESS_EXIT

    invalid = _adapter(
        tmp_path / "invalid",
        executable=_fake_powershell(tmp_path / "invalid", output="not-json"),
    )
    outcome = invalid.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.UNKNOWN
    assert outcome.reason is DesktopNotificationReason.INVALID_RESULT


def test_oversized_output_is_unknown(tmp_path) -> None:
    adapter = _adapter(
        tmp_path,
        executable=_fake_powershell(tmp_path, output="x" * 200),
        max_output_bytes=64,
    )
    message = ToastMessage("0123456789abcdef", "fedcba9876543210", "T", "B")
    outcome = adapter.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.UNKNOWN
    assert outcome.reason is DesktopNotificationReason.OUTPUT_LIMIT


def test_send_maps_per_item_acceptance_and_unknown_missing_tags(tmp_path) -> None:
    payload = (
        '{"ok":true,"operation":"send","attempted":1,"results":['
        '{"tag":"0123456789abcdef","accepted":true,"reason":null}]}'
    )
    adapter = _adapter(
        tmp_path,
        executable=_fake_powershell(tmp_path, output=payload),
    )
    accepted = ToastMessage("0123456789abcdef", "fedcba9876543210", "T", "B")
    missing = ToastMessage("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "T", "B")
    outcomes = adapter.send((accepted, missing))
    assert outcomes[accepted.tag].status is ToastSendStatus.ACCEPTED
    assert outcomes[missing.tag].status is ToastSendStatus.UNKNOWN
    assert outcomes[missing.tag].reason is DesktopNotificationReason.INVALID_RESULT


def test_send_reports_proven_retryable_only_before_any_show(tmp_path) -> None:
    payload = (
        '{"ok":false,"operation":"send","attempted":0,"results":[],"reason":"winrt_unavailable"}'
    )
    adapter = _adapter(
        tmp_path,
        executable=_fake_powershell(tmp_path, output=payload),
    )
    message = ToastMessage("0123456789abcdef", "fedcba9876543210", "T", "B")
    outcome = adapter.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.RETRYABLE
    assert outcome.reason is DesktopNotificationReason.WINRT_UNAVAILABLE

    partial = '{"ok":false,"operation":"send","attempted":1,"results":[],"reason":"send_error"}'
    partial_adapter = _adapter(
        tmp_path / "partial",
        executable=_fake_powershell(tmp_path / "partial", output=partial),
    )
    outcome = partial_adapter.send((message,))[message.tag]
    assert outcome.status is ToastSendStatus.UNKNOWN


def test_probe_reports_only_verified_capability(tmp_path) -> None:
    available = (
        '{"ok":true,"operation":"probe","powershell_version":"5.1","interactive":true,'
        '"winrt":true,"aumid_registered":true,"reason":null}'
    )
    adapter = _adapter(
        tmp_path,
        executable=_fake_powershell(tmp_path, output=available),
    )
    capability = adapter.probe()
    assert capability.available is True
    assert capability.powershell_version == "5.1"
    assert capability.aumid_registered is True

    session_missing = (
        '{"ok":true,"operation":"probe","powershell_version":"5.1","interactive":false,'
        '"winrt":true,"aumid_registered":true,"reason":null}'
    )
    adapter = _adapter(
        tmp_path / "session",
        executable=_fake_powershell(tmp_path / "session", output=session_missing),
    )
    capability = adapter.probe()
    assert capability.available is False
    assert capability.reason is DesktopNotificationReason.SESSION_UNAVAILABLE


def test_register_requires_an_explicit_success_and_our_identity(tmp_path) -> None:
    registered = (
        '{"ok":true,"operation":"register","aumid":"Marjuraru.InvestmentAnalyst",'
        '"aumid_registered":true,"reason":null}'
    )
    probe_available = (
        '{"ok":true,"operation":"probe","powershell_version":"5.1","interactive":true,'
        '"winrt":true,"aumid_registered":true,"reason":null}'
    )
    executable = tmp_path / "register-success"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "payload = sys.stdin.read()\n"
        "if 'register' in sys.argv:\n"
        f"    sys.stdout.write({registered!r})\n"
        "else:\n"
        f"    sys.stdout.write({probe_available!r})\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    adapter = _adapter(tmp_path, executable=executable)
    assert adapter.register().available is True

    failing = _adapter(
        tmp_path / "register-failure",
        executable=_fake_powershell(
            tmp_path / "register-failure",
            output='{"ok":false,"operation":"register","aumid_registered":false}',
        ),
    )
    with pytest.raises(WindowsNotificationError):
        failing.register()


@pytest.mark.skipif(
    not _REAL_POWERSHELL.is_file(),
    reason="Windows PowerShell bridge is only available inside WSL",
)
def test_real_powershell_probe_is_bounded_and_structured() -> None:
    adapter = WindowsToastAdapter()
    if adapter.interop_path() is None:
        pytest.skip("no verified WSL interop endpoint is available")
    capability = adapter.probe()
    assert capability.powershell_version is not None
    if capability.available:
        assert capability.reason is None
        assert capability.aumid_registered is True
    else:
        assert capability.reason is not None
