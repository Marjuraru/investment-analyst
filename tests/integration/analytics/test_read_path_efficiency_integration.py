"""Integration coverage for the reproducible evidence read-path benchmark."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from investment_analyst.frontend.local_web import AaplLocalHttpServer


class _BenchmarkApplication:
    def overview(self) -> dict[str, object]:
        return {"status": "ready"}

    def compact_overview(self) -> dict[str, object]:
        return {"status": "ready", "workspace_id": "fixture"}

    def market_assets(self) -> dict[str, object]:
        return {"assets": []}

    def runtime_capabilities(self) -> dict[str, object]:
        return {"capabilities": []}

    def read_performance(self) -> dict[str, object]:
        return {
            "schema_version": "critical-read-performance-v1",
            "generation": "fixture",
            "uptime_seconds": 0.0,
            "requests": 0,
            "errors": 0,
            "families": {},
        }


def test_read_path_benchmark_uses_temporary_corpus_and_reduces_repeated_work() -> None:
    repository_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "src")

    completed = subprocess.run(
        [sys.executable, str(repository_root / "scripts" / "benchmark_evidence_read_path.py")],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["schema_version"] == "evidence-read-path-benchmark-v1"
    assert result["baseline"]["checksum_reads"] == 128
    assert result["optimized"]["checksum_reads"] == 32
    assert result["baseline"]["semantic_parses"] == 128
    assert result["optimized"]["semantic_parses"] == 32
    assert result["baseline"]["cache_invalidations"] == 10
    assert result["optimized"]["cache_invalidations"] == 7


def test_runtime_read_benchmark_reports_critical_telemetry_and_bounded_overhead() -> None:
    repository_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "src")
    server = AaplLocalHttpServer(
        ("127.0.0.1", 0),
        _BenchmarkApplication(),  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        completed = subprocess.run(
            [
                sys.executable,
                str(repository_root / "scripts" / "benchmark_runtime_reads.py"),
                "--base-url",
                base_url,
                "--samples",
                "5",
                "--include-critical",
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            check=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    result = json.loads(completed.stdout)
    assert result["schema_version"] == "runtime-read-benchmark-v1"
    assert result["results"][0]["name"] == "overview"
    assert "max_ms" in result["results"][0]
    critical = result["critical_read_performance"]
    assert critical["schema_version"] == "critical-read-performance-v1"
    overhead = result["telemetry_overhead"]
    assert overhead["pairs"] == 20
    assert overhead["passed"] is True
