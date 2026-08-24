"""Hermetic tests for exact-SHA release acceptance observation."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from investment_analyst.application.release_acceptance import (
    ALLOWED_ENDPOINTS,
    HttpResponse,
    ProcMemory,
    ReleaseAcceptanceConfig,
    ReleaseAcceptanceInputError,
    observe_release_acceptance,
)

SHA = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
TREE = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"
OTHER_SHA = "2222222222222222222222222222222222222222"


class FakeClock:
    def __init__(self, sleep_factor: float = 1.0) -> None:
        self.current = 0.0
        self.sleep_factor = sleep_factor

    def monotonic(self) -> float:
        return self.current

    def now_utc(self) -> datetime:
        return datetime(2026, 8, 24, tzinfo=UTC) + timedelta(seconds=self.current)

    def sleep(self, seconds: float) -> None:
        self.current += seconds * self.sleep_factor


def _properties(sha: str = SHA, pid: int = 321, n_restarts: int = 0) -> dict[str, str]:
    release = f"/tmp/runtime/releases/{sha}"
    return {
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "MainPID": str(pid),
        "NRestarts": str(n_restarts),
        "WorkingDirectory": release,
        "ExecStart": f"{{ path={release}/.venv/bin/python ; argv[]={release}/.venv/bin/python }}",
    }


class FakeSystemctl:
    def __init__(self, values: Sequence[Mapping[str, str]]) -> None:
        self.values = [dict(value) for value in values]
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def show(self, service: str, properties: Sequence[str]) -> Mapping[str, str]:
        self.calls.append((service, tuple(properties)))
        index = min(len(self.calls) - 1, len(self.values) - 1)
        return self.values[index]


class FakeHttp:
    def __init__(self, responses: Mapping[str, HttpResponse]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def get(self, endpoint: str, timeout_seconds: float) -> HttpResponse:
        self.calls.append(endpoint)
        return self.responses[endpoint]


class FakeProc:
    def __init__(self, memory: ProcMemory) -> None:
        self.memory = memory
        self.calls: list[int] = []

    def read(self, pid: int) -> ProcMemory:
        self.calls.append(pid)
        return self.memory


def _config(
    tmp_path: Path, duration: float = 1.0, interval: float = 0.5
) -> ReleaseAcceptanceConfig:
    return ReleaseAcceptanceConfig(
        expected_sha=SHA,
        expected_tree=TREE,
        service="investment-analyst.service",
        workspace_root=tmp_path / "workspace",
        jsonl_path=tmp_path / "scratch" / "observations.jsonl",
        summary_path=tmp_path / "scratch" / "summary.json",
        duration_seconds=duration,
        interval_seconds=interval,
    )


def _healthy_http() -> FakeHttp:
    return FakeHttp(
        {
            endpoint: HttpResponse(200, b'{"status":"ok"}', "application/json")
            for endpoint in ALLOWED_ENDPOINTS
        }
    )


def test_observer_passes_with_fake_clock_and_atomic_outputs(tmp_path: Path) -> None:
    """PASS records exact target metadata and only compact, non-payload evidence."""
    http = _healthy_http()
    systemctl = FakeSystemctl([_properties()])
    proc = FakeProc(ProcMemory(rss_bytes=10_240, hwm_bytes=12_288, swap_bytes=0))

    summary = observe_release_acceptance(
        _config(tmp_path),
        http=http,
        systemctl=systemctl,
        proc=proc,
        clock=FakeClock(),
    )

    assert summary["status"] == "PASS"
    assert summary["sample_count"] == 3
    assert summary["expected_sha"] == SHA
    assert summary["expected_tree"] == TREE
    assert summary["memory"]["attribution"] == "observational_only"  # type: ignore[index]
    assert summary["endpoints"]["/api/v1/overview"]["p95_ms"] == 0.0  # type: ignore[index]
    records = [
        json.loads(line)
        for line in (tmp_path / "scratch" / "observations.jsonl").read_text().splitlines()
    ]
    assert len(records) == 3
    assert all(
        record["schema_version"] == "release-acceptance-observation-v1" for record in records
    )
    assert 'status":"ok' not in (tmp_path / "scratch" / "observations.jsonl").read_text()
    assert (tmp_path / "scratch" / "summary.json").is_file()
    assert len(systemctl.calls) == 3
    assert proc.calls == [321, 321, 321]


def test_observer_exposes_503_and_invalid_output_without_persisting_body(tmp_path: Path) -> None:
    """HTTP failures and malformed JSON remain visible as bounded reason codes."""
    http = _healthy_http()
    http.responses["/api/v1/overview"] = HttpResponse(503, b"provider-secret-payload", "text/plain")
    http.responses["/api/v1/capabilities"] = HttpResponse(200, b"not-json", "application/json")

    summary = observe_release_acceptance(
        _config(tmp_path, duration=0.0),
        http=http,
        systemctl=FakeSystemctl([_properties()]),
        proc=FakeProc(ProcMemory(rss_bytes=1, hwm_bytes=1, swap_bytes=0)),
        clock=FakeClock(),
    )

    assert summary["status"] == "FAIL"
    assert "http_503" in summary["failures"]
    assert "invalid_output" in summary["failures"]
    output = (tmp_path / "scratch" / "observations.jsonl").read_text()
    assert "provider-secret-payload" not in output


def test_observer_detects_gap_restart_and_sha_drift(tmp_path: Path) -> None:
    """A late sample and a changed unit cannot produce a false PASS."""
    http = _healthy_http()
    systemctl = FakeSystemctl([_properties(), _properties(OTHER_SHA, pid=322, n_restarts=1)])
    summary = observe_release_acceptance(
        _config(tmp_path, duration=0.25, interval=0.1),
        http=http,
        systemctl=systemctl,
        proc=FakeProc(ProcMemory(rss_bytes=1, hwm_bytes=1, swap_bytes=0)),
        clock=FakeClock(sleep_factor=3.0),
    )

    failures = summary["failures"]
    assert summary["status"] == "FAIL"
    assert "sampling_gap" in failures
    assert "process_restart" in failures
    assert "nrestarts_changed" in failures
    assert "release_sha_drift" in failures


def test_observer_rejects_existing_and_workspace_outputs(tmp_path: Path) -> None:
    """Output must be new scratch evidence and never a workspace mutation."""
    workspace = tmp_path / "workspace"
    existing = tmp_path / "existing.jsonl"
    existing.write_text("old\n", encoding="utf-8")
    config = _config(tmp_path)
    with pytest.raises(ReleaseAcceptanceInputError, match="output_must_be_new"):
        observe_release_acceptance(replace(config, jsonl_path=existing))

    with pytest.raises(ReleaseAcceptanceInputError, match="output_inside_workspace"):
        observe_release_acceptance(
            replace(
                config,
                jsonl_path=workspace / "observations.jsonl",
                summary_path=tmp_path / "summary.json",
            )
        )
