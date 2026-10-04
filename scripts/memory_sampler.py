#!/usr/bin/env python3
"""Bounded read-only sampler for one systemd service and its cgroup v2 counters."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from investment_analyst.application.release_acceptance import (
    SERVICE_NAME_PATTERN,
    _extract_release_sha,
)

SCHEMA_VERSION = "operational-memory-sample-v2"
DEFAULT_SERVICE = "investment-analyst"
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_RETENTION_DAYS = 14
DEFAULT_OPS_ROOT = Path("/home/marjuraru/.local/share/investment-analyst/ops")
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_PROC_ROOT = Path("/proc")
_LIMA = ZoneInfo("America/Lima")
_SAMPLE_NAME = re.compile(r"^mem-(\d{4}-\d{2}-\d{2})\.jsonl$")
_BYTE_TEXT = re.compile(r"^([0-9]+) kB$")
_CGROUP_COMPONENT = re.compile(r"^[A-Za-z0-9_.@:+\\-]+$")
_SYSTEMD_PROPERTIES = (
    "MainPID",
    "ControlGroup",
    "InvocationID",
    "WorkingDirectory",
    "ExecStart",
    "MemoryCurrent",
    "MemoryPeak",
    "MemoryHigh",
    "MemoryMax",
    "MemorySwapMax",
)


class SystemctlReader(Protocol):
    """Small injectable reader for one fixed systemd property allowlist."""

    def show(self, service: str) -> Mapping[str, str]:
        """Return the allowlisted properties for ``service``."""
        ...


class RealSystemctlReader:
    """Read the current user's systemd unit without exposing command output on errors."""

    def show(self, service: str) -> Mapping[str, str]:
        if SERVICE_NAME_PATTERN.fullmatch(service) is None:
            raise ValueError("invalid service name")
        command = ["systemctl", "--user", "show", service]
        for property_name in _SYSTEMD_PROPERTIES:
            command.extend(("--property", property_name))
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        properties: dict[str, str] = {}
        for line in result.stdout.splitlines():
            name, separator, value = line.partition("=")
            if separator and name in _SYSTEMD_PROPERTIES:
                properties[name] = value.strip()
        return properties


@dataclass(frozen=True, slots=True)
class OperationalMemorySampleV2:
    """Typed, bounded sample with legacy aliases for the existing cycle report."""

    at: str
    service: str
    pid: int | None
    process_starttime_ticks: int | None
    boot_id: str | None
    cgroup_path: str | None
    cgroup_generation: str | None
    service_invocation_id: str | None
    release_sha: str | None
    release_sha_state: Literal["known", "unknown", "incoherent"]
    VmRSS: int | None
    VmHWM: int | None
    VmSwap: int | None
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    memory_stat_anon_bytes: int | None
    memory_stat_file_bytes: int | None
    memory_stat_kernel_bytes: int | None
    swap_current_bytes: int | None
    memory_high_limit_bytes: int | None
    memory_max_limit_bytes: int | None
    memory_swap_max_limit_bytes: int | None
    high_events: int | None
    max_events: int | None
    oom_events: int | None
    oom_kill_events: int | None
    psi_some_avg10_pct: float | None
    psi_some_avg60_pct: float | None
    psi_some_avg300_pct: float | None
    psi_some_total_us: int | None
    psi_full_avg10_pct: float | None
    psi_full_avg60_pct: float | None
    psi_full_avg300_pct: float | None
    psi_full_total_us: int | None
    sample_interval_seconds: int
    missing_reasons: Mapping[str, str]

    @property
    def memory_events(self) -> Mapping[str, int | None]:
        """Return cgroup event counters under their documented names."""
        return {
            "high": self.high_events,
            "max": self.max_events,
            "oom": self.oom_events,
            "oom_kill": self.oom_kill_events,
        }

    def to_json_dict(self) -> dict[str, object]:
        """Serialize explicit fields without environment data or exception text."""
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["memory_events"] = dict(self.memory_events)
        payload["missing_reasons"] = dict(self.missing_reasons)
        return payload


def capture_sample(
    *,
    service: str = DEFAULT_SERVICE,
    systemctl: SystemctlReader | None = None,
    proc_root: Path = DEFAULT_PROC_ROOT,
    cgroup_root: Path = DEFAULT_CGROUP_ROOT,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sample_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> OperationalMemorySampleV2:
    """Capture one service/process/cgroup sample; missing inputs remain explicit."""
    if SERVICE_NAME_PATTERN.fullmatch(service) is None:
        raise ValueError("invalid service name")
    if (
        isinstance(sample_interval_seconds, bool)
        or not isinstance(sample_interval_seconds, int)
        or sample_interval_seconds <= 0
    ):
        raise ValueError("sample interval must be a positive integer")
    captured_at = clock()
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("sample clock must be timezone-aware")
    captured_at = captured_at.astimezone(UTC)
    missing: dict[str, str] = {}
    reader = systemctl or RealSystemctlReader()
    try:
        properties = reader.show(service)
    except Exception:  # noqa: BLE001 - preserve a safe partial sample on host absence
        properties = {}
        missing["systemd"] = "systemctl_unavailable"

    pid = _parse_positive_int(properties.get("MainPID"))
    if pid is None:
        missing["pid"] = "main_pid_missing"

    boot_id = _read_text(proc_root / "sys/kernel/random/boot_id", missing, "boot_id")
    process_starttime_ticks: int | None = None
    rss_bytes: int | None = None
    hwm_bytes: int | None = None
    process_swap_bytes: int | None = None
    release_sha: str | None = None
    release_sha_state: Literal["known", "unknown", "incoherent"] = "unknown"
    if pid is not None:
        process_dir = proc_root / str(pid)
        process_starttime_ticks = _read_process_starttime(process_dir / "stat", missing)
        status = _parse_status(process_dir / "status", missing)
        rss_bytes = _read_kilobytes(status, "VmRSS", missing)
        hwm_bytes = _read_kilobytes(status, "VmHWM", missing)
        process_swap_bytes = _read_kilobytes(status, "VmSwap", missing)
        try:
            executable = os.readlink(process_dir / "exe")
        except OSError:
            executable = ""
            missing["release_sha"] = "process_executable_unavailable"
        else:
            unit_exec = properties.get("ExecStart", "")
            working_directory = properties.get("WorkingDirectory", "")
            release_sha, paths_consistent = _extract_release_sha(
                {
                    "WorkingDirectory": working_directory,
                    "ExecStart": f"{unit_exec} {executable}",
                }
            )
            if not paths_consistent:
                release_sha_state = "incoherent"
                release_sha = None
                missing["release_sha"] = "release_paths_incoherent"
            elif release_sha is None:
                release_sha_state = "unknown"
                missing["release_sha"] = "release_path_has_no_sha"
            else:
                release_sha_state = "known"
    else:
        missing.setdefault("process_starttime_ticks", "main_pid_missing")
        missing.setdefault("VmRSS", "main_pid_missing")
        missing.setdefault("VmHWM", "main_pid_missing")
        missing.setdefault("VmSwap", "main_pid_missing")
        missing.setdefault("release_sha", "main_pid_missing")

    cgroup_path = properties.get("ControlGroup") or None
    cgroup_dir = _resolve_cgroup_dir(cgroup_root, cgroup_path, missing)
    cgroup_generation: str | None = None
    invocation_id = properties.get("InvocationID") or ""
    if cgroup_path is not None and cgroup_dir is not None and boot_id is not None and invocation_id:
        try:
            identity = cgroup_dir.stat()
        except OSError:
            missing["cgroup_generation"] = "cgroup_stat_unavailable"
        else:
            cgroup_generation = f"{boot_id}:{invocation_id}:{identity.st_dev}:{identity.st_ino}"
    else:
        missing.setdefault("cgroup_generation", "cgroup_identity_incomplete")

    memory_current = _read_integer_file(cgroup_dir, "memory.current", missing)
    memory_peak = _read_integer_file(cgroup_dir, "memory.peak", missing)
    memory_stat = _read_key_value_file(cgroup_dir, "memory.stat", missing)
    swap_current = _read_integer_file(cgroup_dir, "memory.swap.current", missing)
    memory_events = _read_key_value_file(cgroup_dir, "memory.events", missing)
    psi = _read_psi(cgroup_dir, missing)
    high_limit = _parse_byte_limit(properties, "MemoryHigh", missing)
    max_limit = _parse_byte_limit(properties, "MemoryMax", missing)
    swap_max_limit = _parse_byte_limit(properties, "MemorySwapMax", missing)

    return OperationalMemorySampleV2(
        at=captured_at.isoformat().replace("+00:00", "Z"),
        service=service,
        pid=pid,
        process_starttime_ticks=process_starttime_ticks,
        boot_id=boot_id,
        cgroup_path=cgroup_path,
        cgroup_generation=cgroup_generation,
        service_invocation_id=properties.get("InvocationID") or None,
        release_sha=release_sha,
        release_sha_state=release_sha_state,
        VmRSS=rss_bytes,
        VmHWM=hwm_bytes,
        VmSwap=process_swap_bytes,
        memory_current_bytes=memory_current,
        memory_peak_bytes=memory_peak,
        memory_stat_anon_bytes=_value_from_map(memory_stat, "anon", missing),
        memory_stat_file_bytes=_value_from_map(memory_stat, "file", missing),
        memory_stat_kernel_bytes=_value_from_map(memory_stat, "kernel", missing),
        swap_current_bytes=swap_current,
        memory_high_limit_bytes=high_limit,
        memory_max_limit_bytes=max_limit,
        memory_swap_max_limit_bytes=swap_max_limit,
        high_events=_value_from_map(memory_events, "high", missing),
        max_events=_value_from_map(memory_events, "max", missing),
        oom_events=_value_from_map(memory_events, "oom", missing),
        oom_kill_events=_value_from_map(memory_events, "oom_kill", missing),
        psi_some_avg10_pct=psi.get("some_avg10"),
        psi_some_avg60_pct=psi.get("some_avg60"),
        psi_some_avg300_pct=psi.get("some_avg300"),
        psi_some_total_us=psi.get("some_total"),
        psi_full_avg10_pct=psi.get("full_avg10"),
        psi_full_avg60_pct=psi.get("full_avg60"),
        psi_full_avg300_pct=psi.get("full_avg300"),
        psi_full_total_us=psi.get("full_total"),
        sample_interval_seconds=sample_interval_seconds,
        missing_reasons=missing,
    )


def append_sample(
    sample: OperationalMemorySampleV2,
    *,
    samples_dir: Path = DEFAULT_OPS_ROOT / "samples",
    retention_days: int = DEFAULT_RETENTION_DAYS,
    today: date | None = None,
) -> Path:
    """Append one sample and prune only dated ``mem-*.jsonl`` sampler files."""
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < 1
    ):
        raise ValueError("sample retention must be a positive number of days")
    sample_day = datetime.fromisoformat(sample.at.replace("Z", "+00:00")).astimezone(_LIMA).date()
    current_day = today or datetime.now(UTC).astimezone(_LIMA).date()
    target_dir = Path(samples_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"mem-{sample_day.isoformat()}.jsonl"
    if target.is_symlink():
        raise OSError("sample target must not be a symbolic link")
    with target.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                sample.to_json_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    prune_samples(target_dir, before=current_day - timedelta(days=retention_days - 1))
    return target


def prune_samples(samples_dir: Path, *, before: date) -> tuple[str, ...]:
    """Delete only valid sampler sample files older than ``before``."""
    root = Path(samples_dir)
    if root.is_symlink() or not root.is_dir():
        return ()
    removed: list[str] = []
    for path in root.iterdir():
        match = _SAMPLE_NAME.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        try:
            file_day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if file_day < before:
            path.unlink()
            removed.append(path.name)
    return tuple(sorted(removed))


def runtime_identity_summary(sample: OperationalMemorySampleV2) -> dict[str, object]:
    """Return safe live-probe metadata without workspace or database access."""
    return {
        "observed_at": sample.at,
        "service": sample.service,
        "pid": sample.pid,
        "process_starttime_ticks": sample.process_starttime_ticks,
        "boot_id": sample.boot_id,
        "cgroup_path": sample.cgroup_path,
        "cgroup_generation": sample.cgroup_generation,
        "service_invocation_id": sample.service_invocation_id,
        "release_sha": sample.release_sha,
        "release_sha_state": sample.release_sha_state,
        "memory_current_bytes": sample.memory_current_bytes,
        "memory_peak_bytes": sample.memory_peak_bytes,
        "memory_high_limit_bytes": sample.memory_high_limit_bytes,
        "memory_max_limit_bytes": sample.memory_max_limit_bytes,
        "memory_swap_max_limit_bytes": sample.memory_swap_max_limit_bytes,
        "missing_reasons": dict(sample.missing_reasons),
    }


def _parse_positive_int(value: str | None) -> int | None:
    if value is None or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _read_text(path: Path, missing: dict[str, str], field_name: str) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        missing[field_name] = "proc_file_unavailable"
        return None
    if not value:
        missing[field_name] = "proc_file_empty"
        return None
    return value


def _parse_status(path: Path, missing: dict[str, str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        missing["process_status"] = "proc_file_unavailable"
        return {}
    return {
        key: value.strip()
        for line in lines
        for key, separator, value in (line.partition(":"),)
        if separator and key in {"VmRSS", "VmHWM", "VmSwap"}
    }


def _read_kilobytes(
    status: Mapping[str, str],
    field_name: str,
    missing: dict[str, str],
) -> int | None:
    value = status.get(field_name)
    if value is None:
        missing[field_name] = "proc_status_field_missing"
        return None
    match = _BYTE_TEXT.fullmatch(value)
    if match is None:
        missing[field_name] = "proc_status_value_invalid"
        return None
    return int(match.group(1)) * 1024


def _read_process_starttime(path: Path, missing: dict[str, str]) -> int | None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        missing["process_starttime_ticks"] = "proc_stat_unavailable"
        return None
    close = content.rfind(")")
    if close < 0:
        missing["process_starttime_ticks"] = "proc_stat_invalid"
        return None
    fields = content[close + 1 :].split()
    if len(fields) <= 19 or not fields[19].isdecimal():
        missing["process_starttime_ticks"] = "proc_stat_invalid"
        return None
    return int(fields[19])


def _resolve_cgroup_dir(
    root: Path,
    control_group: str | None,
    missing: dict[str, str],
) -> Path | None:
    if control_group is None:
        missing["cgroup_path"] = "systemd_control_group_missing"
        return None
    group = PurePosixPath(control_group)
    if not group.is_absolute() or any(
        part in {"", ".", ".."} or _CGROUP_COMPONENT.fullmatch(part) is None
        for part in group.parts[1:]
    ):
        missing["cgroup_path"] = "systemd_control_group_invalid"
        return None
    resolved_root = Path(root).resolve(strict=False)
    candidate = resolved_root.joinpath(*group.parts[1:]).resolve(strict=False)
    if not candidate.is_relative_to(resolved_root):
        missing["cgroup_path"] = "systemd_control_group_invalid"
        return None
    if not candidate.is_dir():
        missing["cgroup_path"] = "cgroup_directory_unavailable"
        return None
    return candidate


def _read_integer_file(
    directory: Path | None,
    name: str,
    missing: dict[str, str],
) -> int | None:
    key = name.replace(".", "_")
    if directory is None:
        missing[key] = "cgroup_unavailable"
        return None
    try:
        raw = (directory / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        missing[key] = "cgroup_file_unavailable"
        return None
    if not raw.isdecimal():
        missing[key] = "cgroup_value_invalid"
        return None
    return int(raw)


def _read_key_value_file(
    directory: Path | None,
    name: str,
    missing: dict[str, str],
) -> dict[str, int]:
    if directory is None:
        missing[name.replace(".", "_")] = "cgroup_unavailable"
        return {}
    try:
        lines = (directory / name).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        missing[name.replace(".", "_")] = "cgroup_file_unavailable"
        return {}
    values: dict[str, int] = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].isdecimal():
            values[parts[0]] = int(parts[1])
    return values


def _value_from_map(
    values: Mapping[str, int],
    key: str,
    missing: dict[str, str],
) -> int | None:
    value = values.get(key)
    if value is None:
        missing[key] = "cgroup_metric_missing"
    return value


def _parse_byte_limit(
    properties: Mapping[str, str],
    name: str,
    missing: dict[str, str],
) -> int | None:
    value = properties.get(name)
    output_name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() + "_limit_bytes"
    if value is None:
        missing[output_name] = "systemd_limit_unavailable"
        return None
    if value.casefold() == "infinity":
        missing[output_name] = "limit_unbounded"
        return None
    if not value.isdecimal():
        missing[output_name] = "systemd_limit_invalid"
        return None
    return int(value)


def _read_psi(directory: Path | None, missing: dict[str, str]) -> dict[str, float | int | None]:
    names = (
        "some_avg10",
        "some_avg60",
        "some_avg300",
        "some_total",
        "full_avg10",
        "full_avg60",
        "full_avg300",
        "full_total",
    )
    if directory is None:
        for name in names:
            missing[f"psi_{name}"] = "cgroup_unavailable"
        return {name: None for name in names}
    try:
        lines = (directory / "memory.pressure").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        for name in names:
            missing[f"psi_{name}"] = "psi_unavailable"
        return {name: None for name in names}
    parsed: dict[str, float | int | None] = {name: None for name in names}
    for line in lines:
        parts = line.split()
        if not parts or parts[0] not in {"some", "full"}:
            continue
        values = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
        for suffix in ("avg10", "avg60", "avg300"):
            key = f"{parts[0]}_{suffix}"
            raw = values.get(suffix)
            if raw is None:
                missing[f"psi_{key}"] = "psi_field_missing"
                continue
            try:
                number = float(raw)
            except ValueError:
                missing[f"psi_{key}"] = "psi_value_invalid"
            else:
                if math.isfinite(number):
                    parsed[key] = number
                else:
                    missing[f"psi_{key}"] = "psi_value_invalid"
        key = f"{parts[0]}_total"
        raw_total = values.get("total")
        if raw_total is None or not raw_total.isdecimal():
            missing[f"psi_{key}"] = "psi_field_missing"
        else:
            parsed[key] = int(raw_total)
    for name in names:
        if parsed[name] is None:
            missing.setdefault(f"psi_{name}", "psi_field_missing")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--samples-dir", type=Path, default=DEFAULT_OPS_ROOT / "samples")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--once", action="store_true", help="write one sample and exit")
    parser.add_argument("--no-write", action="store_true", help="print one sample without writing")
    parser.add_argument("--json", action="store_true", help="print each sample as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval_seconds <= 0 or args.retention_days <= 0:
        print("memory sampler configuration is invalid", file=sys.stderr)
        return 2
    stop = threading.Event()

    def stop_sampler(_: int, __: object) -> None:
        stop.set()

    if not args.once and not args.no_write:
        signal.signal(signal.SIGTERM, stop_sampler)
        signal.signal(signal.SIGINT, stop_sampler)
    while True:
        started = time.monotonic()
        sample = capture_sample(
            service=args.service,
            sample_interval_seconds=args.interval_seconds,
        )
        if not args.no_write:
            append_sample(
                sample,
                samples_dir=args.samples_dir,
                retention_days=args.retention_days,
            )
        if args.json or args.no_write:
            print(json.dumps(sample.to_json_dict(), ensure_ascii=False, sort_keys=True))
        if args.once or args.no_write:
            return 0
        stop.wait(max(0.0, args.interval_seconds - (time.monotonic() - started)))
        if stop.is_set():
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
