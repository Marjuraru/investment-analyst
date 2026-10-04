"""Hermetic tests for the bounded, read-only operational memory sampler."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "memory_sampler.py"
spec = importlib.util.spec_from_file_location("memory_sampler", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
memory_sampler = importlib.util.module_from_spec(spec)
sys.modules["memory_sampler"] = memory_sampler
spec.loader.exec_module(memory_sampler)


class FakeSystemctl:
    def __init__(self, properties: Mapping[str, str]) -> None:
        self.properties = properties

    def show(self, service: str) -> Mapping[str, str]:
        assert service == "investment-analyst"
        return self.properties


def _host_fixture(
    root: pathlib.Path, *, sha: str = "a" * 40
) -> tuple[pathlib.Path, pathlib.Path, dict[str, str], str]:
    proc_root = root / "proc"
    cgroup_root = root / "cgroup"
    process = proc_root / "123"
    process.mkdir(parents=True)
    (proc_root / "sys/kernel/random").mkdir(parents=True)
    (proc_root / "sys/kernel/random/boot_id").write_text("boot-uuid\n", encoding="utf-8")
    stat_fields = ["S", *(["0"] * 19)]
    stat_fields[19] = "987654"
    (process / "stat").write_text(
        f"123 (python worker) {' '.join(stat_fields)}\n", encoding="utf-8"
    )
    (process / "status").write_text(
        "Name:\tpython\nVmRSS:\t12345 kB\nVmHWM:\t23456 kB\nVmSwap:\t7 kB\n",
        encoding="utf-8",
    )
    group = "/user.slice/user-1000.slice/user@1000.service/app.slice/investment-analyst.service"
    directory = cgroup_root.joinpath(*pathlib.PurePosixPath(group).parts[1:])
    directory.mkdir(parents=True)
    (directory / "memory.current").write_text("4096\n", encoding="utf-8")
    (directory / "memory.peak").write_text("8192\n", encoding="utf-8")
    (directory / "memory.swap.current").write_text("0\n", encoding="utf-8")
    (directory / "memory.stat").write_text("anon 1024\nfile 2048\nkernel 512\n", encoding="utf-8")
    (directory / "memory.events").write_text("high 2\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8")
    (directory / "memory.pressure").write_text(
        "some avg10=0.10 avg60=0.20 avg300=0.30 total=42\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        encoding="utf-8",
    )
    release_path = f"/srv/investment-analyst/releases/{sha}"
    properties = {
        "MainPID": "123",
        "ControlGroup": group,
        "InvocationID": "invocation-uuid",
        "WorkingDirectory": release_path,
        "ExecStart": f"{release_path}/.venv/bin/python -m investment_analyst",
        "MemoryCurrent": "4096",
        "MemoryPeak": "8192",
        "MemoryHigh": "10000",
        "MemoryMax": "20000",
        "MemorySwapMax": "infinity",
    }
    return proc_root, cgroup_root, properties, release_path


def test_capture_sample_reads_only_allowlisted_host_evidence_and_normalizes_units(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    proc_root, cgroup_root, properties, release_path = _host_fixture(tmp_path)
    monkeypatch.setattr(memory_sampler.os, "readlink", lambda path: release_path)
    sample = memory_sampler.capture_sample(
        systemctl=FakeSystemctl(properties),
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        clock=lambda: datetime(2026, 10, 4, 4, 30, tzinfo=UTC),
    )

    assert sample.at == "2026-10-04T04:30:00Z"
    assert sample.pid == 123
    assert sample.process_starttime_ticks == 987654
    assert sample.boot_id == "boot-uuid"
    assert sample.cgroup_generation is not None
    assert sample.release_sha == "a" * 40
    assert sample.release_sha_state == "known"
    assert sample.VmRSS == 12_345 * 1024
    assert sample.VmHWM == 23_456 * 1024
    assert sample.VmSwap == 7 * 1024
    assert sample.memory_current_bytes == 4096
    assert sample.memory_peak_bytes == 8192
    assert sample.high_events == 2
    assert sample.psi_some_total_us == 42
    assert sample.memory_swap_max_limit_bytes is None
    assert sample.missing_reasons["memory_swap_max_limit_bytes"] == "limit_unbounded"

    encoded = json.dumps(sample.to_json_dict(), sort_keys=True)
    assert "ExecStart" not in encoded
    assert "WorkingDirectory" not in encoded
    assert "secret" not in encoded.lower()


def test_missing_host_inputs_are_explicit_and_do_not_leak_reader_error(
    tmp_path: pathlib.Path,
) -> None:
    class UnavailableSystemctl:
        def show(self, service: str) -> Mapping[str, str]:
            raise RuntimeError("credential=do-not-print")

    sample = memory_sampler.capture_sample(
        systemctl=UnavailableSystemctl(),
        proc_root=tmp_path / "missing-proc",
        cgroup_root=tmp_path / "missing-cgroup",
        clock=lambda: datetime(2026, 10, 4, tzinfo=UTC),
    )

    assert sample.pid is None
    assert sample.release_sha is None
    assert sample.missing_reasons["systemd"] == "systemctl_unavailable"
    assert sample.missing_reasons["pid"] == "main_pid_missing"
    assert "do-not-print" not in json.dumps(sample.to_json_dict())


def test_release_identity_is_incoherent_when_unit_and_process_paths_disagree(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    proc_root, cgroup_root, properties, _ = _host_fixture(tmp_path, sha="a" * 40)
    monkeypatch.setattr(
        memory_sampler.os,
        "readlink",
        lambda path: f"/srv/investment-analyst/releases/{'b' * 40}/.venv/bin/python",
    )
    sample = memory_sampler.capture_sample(
        systemctl=FakeSystemctl(properties), proc_root=proc_root, cgroup_root=cgroup_root
    )

    assert sample.release_sha is None
    assert sample.release_sha_state == "incoherent"
    assert sample.missing_reasons["release_sha"] == "release_paths_incoherent"


def test_append_prunes_only_expired_sample_files_and_uses_lima_date(
    tmp_path: pathlib.Path,
) -> None:
    samples_dir = tmp_path / "ops" / "samples"
    samples_dir.mkdir(parents=True)
    expired = samples_dir / "mem-2026-09-01.jsonl"
    expired.write_text("old\n", encoding="utf-8")
    retained = samples_dir / "mem-2026-10-03.jsonl"
    retained.write_text("new\n", encoding="utf-8")
    report = samples_dir / "cycle-2026-09-01.json"
    report.write_text("report\n", encoding="utf-8")
    malformed = samples_dir / "mem-not-a-date.jsonl"
    malformed.write_text("keep\n", encoding="utf-8")
    sample = memory_sampler.OperationalMemorySampleV2(
        at="2026-10-04T02:00:00Z",
        service="investment-analyst",
        pid=None,
        process_starttime_ticks=None,
        boot_id=None,
        cgroup_path=None,
        cgroup_generation=None,
        service_invocation_id=None,
        release_sha=None,
        release_sha_state="unknown",
        VmRSS=None,
        VmHWM=None,
        VmSwap=None,
        memory_current_bytes=None,
        memory_peak_bytes=None,
        memory_stat_anon_bytes=None,
        memory_stat_file_bytes=None,
        memory_stat_kernel_bytes=None,
        swap_current_bytes=None,
        memory_high_limit_bytes=None,
        memory_max_limit_bytes=None,
        memory_swap_max_limit_bytes=None,
        high_events=None,
        max_events=None,
        oom_events=None,
        oom_kill_events=None,
        psi_some_avg10_pct=None,
        psi_some_avg60_pct=None,
        psi_some_avg300_pct=None,
        psi_some_total_us=None,
        psi_full_avg10_pct=None,
        psi_full_avg60_pct=None,
        psi_full_avg300_pct=None,
        psi_full_total_us=None,
        sample_interval_seconds=5,
        missing_reasons={"pid": "main_pid_missing"},
    )

    output = memory_sampler.append_sample(
        sample,
        samples_dir=samples_dir,
        retention_days=14,
        today=date(2026, 10, 4),
    )

    assert output.name == "mem-2026-10-03.jsonl"
    assert output.read_text(encoding="utf-8").count("\n") == 2
    assert not expired.exists()
    assert retained.exists()
    assert report.exists()
    assert malformed.exists()
