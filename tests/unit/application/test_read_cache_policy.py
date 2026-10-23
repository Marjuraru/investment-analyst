"""Unit tests for signature-bound read reuse and bounded telemetry."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from investment_analyst.application.read_cache_policy import (
    ReadCachePolicy,
    ReadPerformanceRegistry,
    estimate_payload_bytes,
)
from investment_analyst.storage.read_dependency_signature import (
    ReadDependency,
    ReadDependencyKind,
    ReadDependencySignature,
)


def _signature(*, seconds: int = 0) -> ReadDependencySignature:
    return ReadDependencySignature(
        workspace_id=UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe"),
        code_version="code-v1",
        contract_version="contract-v1",
        catalog_version="a" * 64,
        endpoint="/api/market-chart",
        arguments_digest="b" * 64,
        known_at=datetime(2026, 10, 22, tzinfo=UTC) + timedelta(seconds=seconds),
        asset_ids=("equity:us:amd",),
        semantic_selection="market",
        dependencies=(
            ReadDependency(
                kind=ReadDependencyKind.OBSERVATION,
                dependency_id="observation-1",
                content_sha256="c" * 64,
            ),
        ),
    )


def test_lookup_requires_the_exact_signature_digest() -> None:
    cache = ReadCachePolicy()
    first = _signature()
    second = _signature(seconds=1)
    assert cache.store(first, {"value": 1})
    assert cache.lookup(first) == {"value": 1}
    assert cache.lookup(second) is None


def test_lru_eviction_is_bounded_by_entries() -> None:
    cache = ReadCachePolicy(max_entries=2)
    first, second, third = (_signature(seconds=index) for index in range(3))
    assert cache.store(first, {"value": 1})
    assert cache.store(second, {"value": 2})
    assert cache.lookup(first) == {"value": 1}
    assert cache.store(third, {"value": 3})
    assert cache.lookup(second) is None
    assert cache.lookup(first) == {"value": 1}
    assert cache.entry_count == 2


def test_entry_larger_than_the_byte_budget_is_not_retained() -> None:
    cache = ReadCachePolicy(max_bytes=16)
    signature = _signature()
    payload = {"value": "x" * 64}
    assert estimate_payload_bytes(payload) > 16
    assert cache.store(signature, payload) is False
    assert cache.lookup(signature) is None
    telemetry = ReadPerformanceRegistry(generation="generation-1")
    cache = ReadCachePolicy(max_bytes=16, telemetry=telemetry)
    assert cache.get_or_load(signature, lambda: payload, family="market-bars") == payload
    summary = telemetry.summary()
    families = summary["families"]
    assert families["market-bars"]["uncacheable"] == 1


def test_identical_concurrent_loads_are_coalesced() -> None:
    cache = ReadCachePolicy()
    signature = _signature()
    calls: list[int] = []
    barrier = threading.Barrier(2)
    started = threading.Event()

    def loader() -> dict[str, int]:
        calls.append(1)
        started.set()
        time.sleep(0.05)
        return {"value": 7}

    results: list[dict[str, int]] = []

    def read() -> None:
        barrier.wait()
        results.append(cache.get_or_load(signature, loader))

    threads = [threading.Thread(target=read) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert started.is_set()
    assert len(calls) == 1
    assert results == [{"value": 7}, {"value": 7}]


def test_failed_loader_releases_waiters_and_is_not_cached() -> None:
    cache = ReadCachePolicy()
    signature = _signature()
    attempts: list[int] = []

    def failing() -> object:
        attempts.append(1)
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError):
        cache.get_or_load(signature, failing)
    assert cache.lookup(signature) is None
    assert attempts == [1]
    assert cache.get_or_load(signature, lambda: {"value": 9}) == {"value": 9}


def test_revalidation_mismatch_is_not_cached() -> None:
    cache = ReadCachePolicy()
    signature = _signature()
    changed = _signature(seconds=1)
    assert cache.get_or_load(signature, lambda: {"value": 1}, revalidate=lambda: changed) == {
        "value": 1
    }
    assert cache.lookup(signature) is None


def test_workspace_invalidation_and_bounded_telemetry() -> None:
    telemetry = ReadPerformanceRegistry(generation="generation-1")
    cache = ReadCachePolicy(telemetry=telemetry)
    signature = _signature()
    cache.get_or_load(signature, lambda: {"value": 1}, family="market-bars")
    cache.get_or_load(signature, lambda: {"value": 2}, family="market-bars")
    cache.get_or_load(_signature(seconds=2), lambda: {"value": 3}, family="documents")

    summary = telemetry.summary()
    assert summary["schema_version"] == "critical-read-performance-v1"
    assert summary["generation"] == "generation-1"
    assert summary["requests"] == 3
    families = summary["families"]
    assert isinstance(families, dict)
    assert families["market-bars"]["hits"] == 1
    assert families["documents"]["misses"] == 1
    assert families["market-bars"]["window_samples"] <= 64
    assert cache.invalidate_workspace(signature.workspace_id) == 2
    assert cache.entry_count == 0


def test_telemetry_window_is_bounded_to_sixty_four_samples() -> None:
    telemetry = ReadPerformanceRegistry(generation="generation-1")
    for index in range(100):
        telemetry.record("market-bars", float(index), "miss")
    summary = telemetry.summary()
    families = summary["families"]
    assert isinstance(families, dict)
    assert families["market-bars"]["window_samples"] == 64
    assert families["market-bars"]["requests"] == 100


def test_unknown_generation_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReadPerformanceRegistry(generation="")
    with pytest.raises(ValueError):
        ReadCachePolicy(max_entries=0)
    with pytest.raises(ValueError):
        ReadCachePolicy(max_bytes=0)
