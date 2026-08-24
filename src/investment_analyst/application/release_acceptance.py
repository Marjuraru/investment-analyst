"""Read-only, exact-SHA operational observation for HUMAN release acceptance.

This module deliberately has no workspace, provider, scheduler, or product-state
dependencies.  Its real adapters are limited to loopback GET, ``systemctl show``
and ``/proc/<MainPID>/status``.  The resulting evidence contains measurements and
bounded reason codes, never response payloads, private paths, or causal claims.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SERVICE_NAME = "investment-analyst.service"
OBSERVATION_SCHEMA = "release-acceptance-observation-v1"
ALLOWED_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/overview",
    "/api/v1/capabilities",
    "/api/v1/market-assets",
    "/api/v1/candidate-notifications",
    "/api/overview",
)
SYSTEMCTL_PROPERTIES: tuple[str, ...] = (
    "ActiveState",
    "SubState",
    "UnitFileState",
    "MainPID",
    "NRestarts",
    "WorkingDirectory",
    "ExecStart",
)
MIN_INTERVAL_SECONDS = 0.05
MAX_INTERVAL_SECONDS = 60 * 60
MIN_TIMEOUT_SECONDS = 0.05
MAX_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 256 * 1024
RELEASE_PATH_SHA = re.compile(r"(?:^|/)releases/([0-9a-f]{40})(?:/|$)")
SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@:-]+$")


class ReleaseAcceptanceError(Exception):
    """Base error for fail-closed observer input or execution."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReleaseAcceptanceInputError(ReleaseAcceptanceError):
    """Raised for invalid target, timing, or output configuration."""


class ReleaseAcceptanceRuntimeError(ReleaseAcceptanceError):
    """Raised when evidence output cannot be safely completed."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Small response envelope; the body is consumed only for validation."""

    status: int
    body: bytes
    content_type: str | None = None


class HttpGetter(Protocol):
    """Read-only HTTP transport for one allowlisted endpoint."""

    def get(self, endpoint: str, timeout_seconds: float) -> HttpResponse: ...


class SystemctlReader(Protocol):
    """Read-only systemd property source."""

    def show(self, service: str, properties: Sequence[str]) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class ProcMemory:
    """Memory fields from one process status file, in bytes."""

    rss_bytes: int | None
    hwm_bytes: int | None
    swap_bytes: int | None


class ProcStatusReader(Protocol):
    """Read only /proc/<PID>/status memory fields."""

    def read(self, pid: int) -> ProcMemory: ...


class ObserverClock(Protocol):
    """Clock boundary used by the observer and its hermetic tests."""

    def monotonic(self) -> float: ...

    def now_utc(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class RealObserverClock:
    """Production wall and monotonic clock implementation."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class LoopbackHttpGetter:
    """GET-only transport restricted to the local service and fixed paths."""

    def __init__(self, port: int) -> None:
        if isinstance(port, bool) or not 1 <= port <= 65535:
            raise ReleaseAcceptanceInputError("invalid_port")
        self.port = port

    def get(self, endpoint: str, timeout_seconds: float) -> HttpResponse:
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ReleaseAcceptanceRuntimeError("endpoint_not_allowlisted")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{endpoint}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "release-acceptance-observer"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=int(response.status),
                    body=response.read(MAX_RESPONSE_BYTES + 1),
                    content_type=response.headers.get_content_type(),
                )
        except urllib.error.HTTPError as error:
            body = error.read(MAX_RESPONSE_BYTES + 1)
            return HttpResponse(
                status=int(error.code),
                body=body,
                content_type=error.headers.get_content_type() if error.headers else None,
            )
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise ReleaseAcceptanceRuntimeError("http_transport_error") from error


class SystemctlShowReader:
    """Run only ``systemctl --user show`` with a fixed property allowlist."""

    def show(self, service: str, properties: Sequence[str]) -> Mapping[str, str]:
        if not SERVICE_NAME_PATTERN.fullmatch(service):
            raise ReleaseAcceptanceRuntimeError("invalid_service_name")
        if tuple(properties) != SYSTEMCTL_PROPERTIES:
            raise ReleaseAcceptanceRuntimeError("systemctl_property_not_allowlisted")
        command = ["systemctl", "--user", "show", service]
        for property_name in SYSTEMCTL_PROPERTIES:
            command.extend(["-p", property_name])
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseAcceptanceRuntimeError("systemctl_show_error") from error
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in SYSTEMCTL_PROPERTIES:
                values[key] = value.strip()
        return values


class ProcStatusFileReader:
    """Read exactly the permitted process status file and no other proc data."""

    @staticmethod
    def _kilobytes(value: str) -> int | None:
        if value == "":
            return None
        match = re.fullmatch(r"([0-9]+) kB", value.strip())
        if match is None:
            raise ReleaseAcceptanceRuntimeError("proc_status_invalid")
        return int(match.group(1)) * 1024

    def read(self, pid: int) -> ProcMemory:
        if isinstance(pid, bool) or pid <= 0:
            raise ReleaseAcceptanceRuntimeError("main_pid_missing")
        values: dict[str, str] = {}
        try:
            status_path = Path("/proc") / str(pid) / "status"
            for line in status_path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"VmRSS", "VmHWM", "VmSwap"}:
                    values[key] = value.strip()
        except (OSError, UnicodeError) as error:
            raise ReleaseAcceptanceRuntimeError("proc_status_unavailable") from error
        return ProcMemory(
            rss_bytes=self._kilobytes(values.get("VmRSS", "")),
            hwm_bytes=self._kilobytes(values.get("VmHWM", "")),
            swap_bytes=self._kilobytes(values.get("VmSwap", "")),
        )


@dataclass(frozen=True, slots=True)
class ReleaseAcceptanceConfig:
    """Immutable observer target and bounded sampling configuration."""

    expected_sha: str
    expected_tree: str
    service: str
    workspace_root: Path
    jsonl_path: Path
    summary_path: Path
    duration_seconds: float
    interval_seconds: float
    timeout_seconds: float = 5.0
    port: int = 8765

    def validate(self) -> ReleaseAcceptanceConfig:
        expected_sha = self.expected_sha.strip().lower()
        expected_tree = self.expected_tree.strip().lower()
        if not FULL_SHA.fullmatch(expected_sha) or not FULL_SHA.fullmatch(expected_tree):
            raise ReleaseAcceptanceInputError("expected_sha_and_tree_must_be_full")
        if not SERVICE_NAME_PATTERN.fullmatch(self.service):
            raise ReleaseAcceptanceInputError("invalid_service_name")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ReleaseAcceptanceInputError("invalid_duration")
        if (
            not math.isfinite(self.interval_seconds)
            or not MIN_INTERVAL_SECONDS <= self.interval_seconds <= MAX_INTERVAL_SECONDS
        ):
            raise ReleaseAcceptanceInputError("invalid_interval")
        if (
            not math.isfinite(self.timeout_seconds)
            or not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ReleaseAcceptanceInputError("invalid_timeout")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ReleaseAcceptanceInputError("invalid_port")
        if not self.workspace_root.is_absolute():
            raise ReleaseAcceptanceInputError("workspace_root_must_be_absolute")
        if self.jsonl_path.resolve(strict=False) == self.summary_path.resolve(strict=False):
            raise ReleaseAcceptanceInputError("output_paths_must_differ")
        _validate_new_output_path(self.jsonl_path, self.workspace_root)
        _validate_new_output_path(self.summary_path, self.workspace_root)
        return ReleaseAcceptanceConfig(
            expected_sha=expected_sha,
            expected_tree=expected_tree,
            service=self.service,
            workspace_root=self.workspace_root.resolve(strict=False),
            jsonl_path=self.jsonl_path.resolve(strict=False),
            summary_path=self.summary_path.resolve(strict=False),
            duration_seconds=self.duration_seconds,
            interval_seconds=self.interval_seconds,
            timeout_seconds=self.timeout_seconds,
            port=self.port,
        )


def _path_has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.anchor else Path()
    for part in path.parts[1:] if path.anchor else path.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _validate_new_output_path(path: Path, workspace_root: Path) -> None:
    candidate = path.expanduser().resolve(strict=False)
    workspace = workspace_root.expanduser().resolve(strict=False)
    if candidate == workspace or workspace in candidate.parents:
        raise ReleaseAcceptanceInputError("output_inside_workspace")
    if _path_has_symlink_component(path.expanduser()):
        raise ReleaseAcceptanceInputError("output_path_symlink")
    if os.path.lexists(path):
        raise ReleaseAcceptanceInputError("output_must_be_new")


class _JsonlWriter:
    """Create one new evidence file and append flushed JSON records only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> _JsonlWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags | nofollow, 0o600)
            self._file = os.fdopen(descriptor, "w", encoding="utf-8")
        except (OSError, ValueError) as error:
            raise ReleaseAcceptanceRuntimeError("jsonl_open_failed") from error
        return self

    def append(self, record: Mapping[str, object]) -> None:
        if self._file is None:
            raise ReleaseAcceptanceRuntimeError("jsonl_not_open")
        try:
            self._file.write(
                json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            )
            self._file.write("\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        except (OSError, TypeError, ValueError) as error:
            raise ReleaseAcceptanceRuntimeError("jsonl_write_failed") from error

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def _write_summary_atomic(path: Path, summary: Mapping[str, object]) -> None:
    """Publish a new summary atomically without overwriting a competing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        raise ReleaseAcceptanceRuntimeError("summary_must_be_new")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(summary, output, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ReleaseAcceptanceRuntimeError("summary_must_be_new") from error
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except (OSError, TypeError, ValueError) as error:
        if isinstance(error, ReleaseAcceptanceRuntimeError):
            raise
        raise ReleaseAcceptanceRuntimeError("summary_write_failed") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ReleaseAcceptanceRuntimeError("summary_cleanup_failed") from error


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ReleaseAcceptanceRuntimeError("clock_must_be_timezone_aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered))) - 1
    return round(ordered[rank], 3)


def _extract_release_sha(properties: Mapping[str, str]) -> tuple[str | None, bool]:
    found: list[str] = []
    for property_name in ("WorkingDirectory", "ExecStart"):
        matches = RELEASE_PATH_SHA.findall(properties.get(property_name, ""))
        found.extend(matches)
    unique = tuple(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], True
    if len(unique) > 1:
        return None, False
    return None, True


def _parse_systemctl(properties: Mapping[str, str]) -> dict[str, object]:
    required = {name: properties.get(name, "") for name in SYSTEMCTL_PROPERTIES}
    if any(required[name] == "" for name in SYSTEMCTL_PROPERTIES):
        raise ReleaseAcceptanceRuntimeError("systemctl_output_invalid")
    try:
        pid = int(required["MainPID"])
        n_restarts = int(required["NRestarts"])
    except ValueError as error:
        raise ReleaseAcceptanceRuntimeError("systemctl_output_invalid") from error
    if pid <= 0:
        raise ReleaseAcceptanceRuntimeError("main_pid_missing")
    if n_restarts < 0:
        raise ReleaseAcceptanceRuntimeError("systemctl_output_invalid")
    observed_sha, unit_sha_consistent = _extract_release_sha(required)
    return {
        "active_state": required["ActiveState"],
        "sub_state": required["SubState"],
        "unit_file_state": required["UnitFileState"],
        "pid": pid,
        "n_restarts": n_restarts,
        "observed_sha": observed_sha,
        "unit_sha_consistent": unit_sha_consistent,
    }


def _http_observation(response: HttpResponse, latency_ms: float) -> dict[str, object]:
    if not isinstance(response.status, int) or not 100 <= response.status <= 599:
        return {
            "status": None,
            "latency_ms": round(latency_ms, 3),
            "bytes": None,
            "json_valid": False,
            "error": "invalid_http_status",
        }
    body_size = len(response.body)
    result: dict[str, object] = {
        "status": response.status,
        "latency_ms": round(latency_ms, 3),
        "bytes": body_size,
        "json_valid": False,
        "error": None,
    }
    if body_size > MAX_RESPONSE_BYTES:
        result["error"] = "output_too_large"
        return result
    if response.status != 200:
        result["error"] = "http_503" if response.status == 503 else "http_non_200"
        return result
    if response.content_type is not None and response.content_type.lower() != "application/json":
        result["error"] = "invalid_content_type"
        return result
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        result["error"] = "invalid_output"
        return result
    if not isinstance(payload, (dict, list)):
        result["error"] = "invalid_output"
        return result
    result["json_valid"] = True
    result["payload_kind"] = "object" if isinstance(payload, dict) else "array"
    return result


class ReleaseAcceptanceObserver:
    """Collect bounded exact-SHA observations without touching product state."""

    def __init__(
        self,
        config: ReleaseAcceptanceConfig,
        http: HttpGetter | None = None,
        systemctl: SystemctlReader | None = None,
        proc: ProcStatusReader | None = None,
        clock: ObserverClock | None = None,
    ) -> None:
        self.config = config.validate()
        self.http = http if http is not None else LoopbackHttpGetter(self.config.port)
        self.systemctl = systemctl if systemctl is not None else SystemctlShowReader()
        self.proc = proc if proc is not None else ProcStatusFileReader()
        self.clock = clock if clock is not None else RealObserverClock()

    def _cycle(
        self,
        sample: int,
        started_monotonic: float,
        previous_monotonic: float | None,
        baseline_pid: int | None,
        baseline_restarts: int | None,
    ) -> tuple[dict[str, object], set[str], int | None, int | None]:
        now_monotonic = self.clock.monotonic()
        elapsed = now_monotonic - started_monotonic
        failures: set[str] = set()
        if elapsed < 0:
            failures.add("clock_regressed")
            elapsed = 0.0
        gap_seconds: float | None = None
        if previous_monotonic is not None:
            gap_seconds = now_monotonic - previous_monotonic
            if gap_seconds > self.config.interval_seconds * 1.5:
                failures.add("sampling_gap")

        service_data: dict[str, object] = {
            "active_state": None,
            "sub_state": None,
            "unit_file_state": None,
            "pid": None,
            "n_restarts": None,
            "observed_sha": None,
            "unit_sha_consistent": False,
        }
        memory_data: dict[str, object] = {
            "rss_bytes": None,
            "hwm_bytes": None,
            "swap_bytes": None,
        }
        try:
            properties = self.systemctl.show(self.config.service, SYSTEMCTL_PROPERTIES)
            service_data = _parse_systemctl(properties)
            if service_data["active_state"] != "active":
                failures.add("service_not_active")
            if service_data["sub_state"] != "running":
                failures.add("service_not_running")
            if service_data["unit_file_state"] != "enabled":
                failures.add("service_not_enabled")
            observed_sha = service_data["observed_sha"]
            if not service_data["unit_sha_consistent"]:
                failures.add("release_sha_mismatch")
            elif observed_sha is None:
                failures.add("release_sha_missing")
            elif observed_sha != self.config.expected_sha:
                failures.add("release_sha_drift")
            pid = service_data["pid"]
            if not isinstance(pid, int):
                failures.add("main_pid_missing")
            else:
                if baseline_pid is not None and pid != baseline_pid:
                    failures.add("process_restart")
                if baseline_pid is None:
                    baseline_pid = pid
                try:
                    memory = self.proc.read(pid)
                    memory_data = {
                        "rss_bytes": memory.rss_bytes,
                        "hwm_bytes": memory.hwm_bytes,
                        "swap_bytes": memory.swap_bytes,
                    }
                    if (
                        memory.rss_bytes is None
                        or memory.hwm_bytes is None
                        or memory.swap_bytes is None
                    ):
                        failures.add("memory_field_missing")
                    if any(
                        value is not None and (not isinstance(value, int) or value < 0)
                        for value in (memory.rss_bytes, memory.hwm_bytes, memory.swap_bytes)
                    ):
                        failures.add("memory_field_invalid")
                    if memory.swap_bytes not in (None, 0):
                        failures.add("swap_nonzero")
                except ReleaseAcceptanceError as error:
                    failures.add(error.code)
        except ReleaseAcceptanceError as error:
            failures.add(error.code)

        if service_data.get("n_restarts") is not None:
            n_restarts = service_data["n_restarts"]
            if isinstance(n_restarts, int):
                if baseline_restarts is not None and n_restarts != baseline_restarts:
                    failures.add("nrestarts_changed")
                if baseline_restarts is None:
                    baseline_restarts = n_restarts

        http_data: dict[str, object] = {}
        for endpoint in ALLOWED_ENDPOINTS:
            request_started = self.clock.monotonic()
            try:
                response = self.http.get(endpoint, self.config.timeout_seconds)
                latency_ms = max(0.0, (self.clock.monotonic() - request_started) * 1000.0)
                endpoint_data = _http_observation(response, latency_ms)
            except ReleaseAcceptanceError as error:
                latency_ms = max(0.0, (self.clock.monotonic() - request_started) * 1000.0)
                endpoint_data = {
                    "status": None,
                    "latency_ms": round(latency_ms, 3),
                    "bytes": None,
                    "json_valid": False,
                    "error": error.code,
                }
            if endpoint_data["error"] is not None:
                error_code = endpoint_data["error"]
                if isinstance(error_code, str):
                    failures.add(error_code)
            http_data[endpoint] = endpoint_data

        record: dict[str, object] = {
            "schema_version": OBSERVATION_SCHEMA,
            "sample": sample,
            "observed_at": _utc_text(self.clock.now_utc()),
            "monotonic_seconds": round(now_monotonic, 6),
            "elapsed_seconds": round(elapsed, 6),
            "expected_sha": self.config.expected_sha,
            "expected_tree": self.config.expected_tree,
            "service": self.config.service,
            "unit": service_data,
            "memory": memory_data,
            "http": http_data,
            "gap_seconds": None if gap_seconds is None else round(gap_seconds, 6),
            "events": sorted(failures),
            "memory_attribution": "observational_only",
        }
        return record, failures, baseline_pid, baseline_restarts

    def run(self) -> dict[str, object]:
        """Collect the explicit duration and atomically publish its summary."""
        started_monotonic = self.clock.monotonic()
        started_at = _utc_text(self.clock.now_utc())
        records: list[dict[str, object]] = []
        failures: set[str] = set()
        latencies: dict[str, list[float]] = {endpoint: [] for endpoint in ALLOWED_ENDPOINTS}
        statuses: dict[str, dict[str, int]] = {endpoint: {} for endpoint in ALLOWED_ENDPOINTS}
        memory_samples: list[dict[str, int | None]] = []
        pids: list[int] = []
        n_restarts: list[int] = []
        previous_monotonic: float | None = None
        baseline_pid: int | None = None
        baseline_restarts: int | None = None
        max_samples = max(
            1,
            math.ceil(self.config.duration_seconds / self.config.interval_seconds) + 2,
        )

        with _JsonlWriter(self.config.jsonl_path) as writer:
            for sample in range(1, max_samples + 1):
                record, cycle_failures, baseline_pid, baseline_restarts = self._cycle(
                    sample,
                    started_monotonic,
                    previous_monotonic,
                    baseline_pid,
                    baseline_restarts,
                )
                records.append(record)
                writer.append(record)
                failures.update(cycle_failures)
                previous_monotonic = float(record["monotonic_seconds"])

                unit = record["unit"]
                if isinstance(unit, dict):
                    pid = unit.get("pid")
                    restart_count = unit.get("n_restarts")
                    if isinstance(pid, int):
                        pids.append(pid)
                    if isinstance(restart_count, int):
                        n_restarts.append(restart_count)
                memory = record["memory"]
                if isinstance(memory, dict):
                    memory_samples.append(
                        {
                            "rss_bytes": memory.get("rss_bytes")
                            if isinstance(memory.get("rss_bytes"), int)
                            else None,
                            "hwm_bytes": memory.get("hwm_bytes")
                            if isinstance(memory.get("hwm_bytes"), int)
                            else None,
                            "swap_bytes": memory.get("swap_bytes")
                            if isinstance(memory.get("swap_bytes"), int)
                            else None,
                        }
                    )
                http = record["http"]
                if isinstance(http, dict):
                    for endpoint, result in http.items():
                        if not isinstance(endpoint, str) or not isinstance(result, dict):
                            continue
                        latency = result.get("latency_ms")
                        status = result.get("status")
                        if isinstance(latency, (int, float)) and math.isfinite(float(latency)):
                            latencies[endpoint].append(float(latency))
                        status_key = str(status) if isinstance(status, int) else "none"
                        endpoint_statuses = statuses.setdefault(endpoint, {})
                        endpoint_statuses[status_key] = endpoint_statuses.get(status_key, 0) + 1

                elapsed = float(record["elapsed_seconds"])
                if elapsed >= self.config.duration_seconds:
                    break
                if sample == max_samples:
                    failures.add("duration_incomplete")
                    break
                remaining = self.config.duration_seconds - elapsed
                self.clock.sleep(min(self.config.interval_seconds, max(0.0, remaining)))
            else:
                failures.add("duration_incomplete")

        ended_monotonic = self.clock.monotonic()
        ended_at = _utc_text(self.clock.now_utc())
        elapsed_total = max(0.0, ended_monotonic - started_monotonic)
        if elapsed_total + 1e-6 < self.config.duration_seconds:
            failures.add("duration_incomplete")

        if pids and len(set(pids)) > 1:
            failures.add("process_restart")
        if n_restarts and len(set(n_restarts)) > 1:
            failures.add("nrestarts_changed")

        endpoint_summary: dict[str, object] = {}
        for endpoint in ALLOWED_ENDPOINTS:
            endpoint_summary[endpoint] = {
                "samples": len(latencies[endpoint]),
                "p50_ms": _percentile(latencies[endpoint], 0.50),
                "p95_ms": _percentile(latencies[endpoint], 0.95),
                "statuses": statuses[endpoint],
            }

        summary: dict[str, object] = {
            "schema_version": OBSERVATION_SCHEMA,
            "status": "PASS" if not failures else "FAIL",
            "expected_sha": self.config.expected_sha,
            "expected_tree": self.config.expected_tree,
            "service": self.config.service,
            "started_at": started_at,
            "ended_at": ended_at,
            "elapsed_seconds": round(elapsed_total, 6),
            "requested_duration_seconds": self.config.duration_seconds,
            "interval_seconds": self.config.interval_seconds,
            "sample_count": len(records),
            "failures": sorted(failures),
            "pids": sorted(set(pids)),
            "n_restarts": sorted(set(n_restarts)),
            "endpoints": endpoint_summary,
            "memory": {
                "samples": memory_samples,
                "max_rss_bytes": max(
                    (
                        sample["rss_bytes"]
                        for sample in memory_samples
                        if sample["rss_bytes"] is not None
                    ),
                    default=None,
                ),
                "max_hwm_bytes": max(
                    (
                        sample["hwm_bytes"]
                        for sample in memory_samples
                        if sample["hwm_bytes"] is not None
                    ),
                    default=None,
                ),
                "max_swap_bytes": max(
                    (
                        sample["swap_bytes"]
                        for sample in memory_samples
                        if sample["swap_bytes"] is not None
                    ),
                    default=None,
                ),
                "attribution": "observational_only",
            },
            "correlation": "no_job_attempt_payloads_collected",
            "evidence": {
                "jsonl_append_only": True,
                "summary_atomic": True,
                "source_scope": ["loopback_get", "systemctl_show", "proc_pid_status"],
            },
        }
        _write_summary_atomic(self.config.summary_path, summary)
        return summary


def observe_release_acceptance(
    config: ReleaseAcceptanceConfig,
    http: HttpGetter | None = None,
    systemctl: SystemctlReader | None = None,
    proc: ProcStatusReader | None = None,
    clock: ObserverClock | None = None,
) -> dict[str, object]:
    """Convenience entrypoint for the one-shot observer."""
    return ReleaseAcceptanceObserver(
        config,
        http=http,
        systemctl=systemctl,
        proc=proc,
        clock=clock,
    ).run()


__all__ = [
    "ALLOWED_ENDPOINTS",
    "FULL_SHA",
    "HttpGetter",
    "HttpResponse",
    "LoopbackHttpGetter",
    "OBSERVATION_SCHEMA",
    "ObserverClock",
    "ProcMemory",
    "ProcStatusFileReader",
    "ProcStatusReader",
    "RealObserverClock",
    "ReleaseAcceptanceConfig",
    "ReleaseAcceptanceError",
    "ReleaseAcceptanceInputError",
    "ReleaseAcceptanceObserver",
    "ReleaseAcceptanceRuntimeError",
    "SystemctlReader",
    "SystemctlShowReader",
    "SYSTEMCTL_PROPERTIES",
    "observe_release_acceptance",
]
