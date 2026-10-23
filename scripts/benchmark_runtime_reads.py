#!/usr/bin/env python3
"""Benchmark repeatable local runtime reads without triggering provider work."""

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import urlopen

from investment_analyst.application.read_cache_policy import ReadPerformanceRegistry


@dataclass(frozen=True, slots=True)
class _Target:
    name: str
    path: str


_TARGETS = (
    _Target("overview", "/api/v1/overview"),
    _Target("catalog", "/api/market-assets"),
    _Target("capabilities", "/api/v1/capabilities"),
    _Target("state", "/api/overview"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--include-critical", action="store_true")
    arguments = parser.parse_args()
    if not 5 <= arguments.samples <= 1_000:
        parser.error("samples must be between 5 and 1000")
    results: list[dict[str, object]] = []
    try:
        for target in _TARGETS:
            durations: list[float] = []
            payload_bytes = 0
            for _ in range(arguments.samples):
                started = time.perf_counter_ns()
                with urlopen(  # noqa: S310 - fixed loopback URL is validated below
                    f"{arguments.base_url.rstrip('/')}{target.path}",
                    timeout=5,
                ) as response:
                    payload = response.read()
                durations.append((time.perf_counter_ns() - started) / 1_000_000)
                json.loads(payload)
                payload_bytes = len(payload)
            ordered = sorted(durations)
            p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
            results.append(
                {
                    "name": target.name,
                    "samples": arguments.samples,
                    "p50_ms": round(statistics.median(ordered), 3),
                    "p95_ms": round(ordered[p95_index], 3),
                    "max_ms": round(ordered[-1], 3),
                    "payload_bytes": payload_bytes,
                }
            )
        critical: dict[str, object] | None = None
        if arguments.include_critical:
            with urlopen(  # noqa: S310 - fixed loopback URL is validated below
                f"{arguments.base_url.rstrip('/')}/api/v1/read-performance",
                timeout=5,
            ) as response:
                critical = json.loads(response.read())
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        print("local runtime benchmark could not read a valid endpoint", file=sys.stderr)
        return 2
    overview = results[0]
    overview_p95 = overview["p95_ms"]
    overview_size = overview["payload_bytes"]
    if not isinstance(overview_p95, float) or not isinstance(overview_size, int):
        return 2
    passed = overview_p95 < 100 and overview_size < 20 * 1024
    document: dict[str, object] = {
        "schema_version": "runtime-read-benchmark-v1",
        "overview_target_passed": passed,
        "results": results,
    }
    if arguments.include_critical:
        document["critical_read_performance"] = critical
        document["telemetry_overhead"] = _telemetry_overhead()
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0 if passed else 1


def _telemetry_overhead() -> dict[str, object]:
    """Measure the per-record instrumentation cost across 20 paired repetitions."""
    registry = ReadPerformanceRegistry(generation="benchmark-overhead")
    baseline: list[float] = []
    instrumented: list[float] = []
    iterations = 1_000
    for _ in range(20):
        started = time.perf_counter_ns()
        for _ in range(iterations):
            pass
        baseline.append((time.perf_counter_ns() - started) / 1_000_000 / iterations)
        started = time.perf_counter_ns()
        for _ in range(iterations):
            registry.record("market-bars", 0.1, "miss")
        instrumented.append((time.perf_counter_ns() - started) / 1_000_000 / iterations)
    baseline_ms = statistics.median(baseline)
    instrumented_ms = statistics.median(instrumented)
    delta_ms = max(0.0, instrumented_ms - baseline_ms)
    budget_ms = max(1.0, 0.05 * baseline_ms)
    return {
        "pairs": 20,
        "iterations_per_pair": iterations,
        "baseline_p50_ms": round(baseline_ms, 6),
        "instrumented_p50_ms": round(instrumented_ms, 6),
        "delta_ms": round(delta_ms, 6),
        "budget_ms": round(budget_ms, 6),
        "passed": delta_ms <= budget_ms,
    }


if __name__ == "__main__":
    raise SystemExit(main())
