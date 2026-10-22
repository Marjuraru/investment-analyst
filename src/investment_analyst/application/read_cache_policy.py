"""Bounded read reuse and lightweight critical-read telemetry."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from time import monotonic, perf_counter
from typing import Literal, TypeVar

from investment_analyst.core.models.base import NonEmptyStr
from investment_analyst.storage.read_dependency_signature import ReadDependencySignature

DEFAULT_MAX_CACHE_ENTRIES = 128
DEFAULT_MAX_CACHE_BYTES = 32 * 1024 * 1024
_DURATION_WINDOW = 64
_CRITICAL_READ_PERFORMANCE_SCHEMA = "critical-read-performance-v1"

T = TypeVar("T")
CacheOutcome = Literal["hit", "miss", "coalesced", "uncacheable", "error"]
ReadFamily = Literal[
    "market-bars",
    "fundamentals",
    "documents",
    "institutional",
    "metrics",
    "valuation",
    "coverage",
    "listed-company-report",
]


class ReadCacheError(RuntimeError):
    """Raised when a read cache entry cannot be represented safely."""


def estimate_payload_bytes(value: object) -> int:
    """Estimate the retained size of one cached payload without mutating it."""
    if hasattr(value, "model_dump_json"):
        payload = value.model_dump_json()  # type: ignore[attr-defined]
    elif isinstance(value, (dict, list, tuple)):
        payload = json.dumps(value, allow_nan=False, separators=(",", ":"), default=str)
    else:
        payload = json.dumps(value, allow_nan=False, default=str)
    return len(payload.encode("utf-8"))


@dataclass
class _Inflight:
    event: threading.Event = field(default_factory=threading.Event)
    value: object | None = None
    error: BaseException | None = None


@dataclass
class _Entry:
    signature: ReadDependencySignature
    value: object
    size_bytes: int


class ReadCachePolicy:
    """Thread-safe LRU cache keyed exclusively by a content-bound signature."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        telemetry: ReadPerformanceRegistry | None = None,
    ) -> None:
        if isinstance(max_entries, bool) or not 1 <= max_entries <= 1024:
            raise ValueError("read cache max_entries must be between 1 and 1024")
        if isinstance(max_bytes, bool) or not 1 <= max_bytes <= 256 * 1024 * 1024:
            raise ValueError("read cache max_bytes must be between 1 and 256 MiB")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._telemetry = telemetry
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, _Inflight] = {}
        self._bytes = 0

    @property
    def entry_count(self) -> int:
        """Return the current bounded entry count."""
        with self._lock:
            return len(self._entries)

    @property
    def byte_count(self) -> int:
        """Return the current retained byte estimate."""
        with self._lock:
            return self._bytes

    def lookup(self, signature: ReadDependencySignature) -> object | None:
        """Return a cached payload only for an identical signature digest."""
        with self._lock:
            entry = self._entries.get(signature.digest)
            if entry is None:
                return None
            if not entry.signature.matches(signature):
                self._remove(signature.digest)
                return None
            self._entries.move_to_end(signature.digest)
            return entry.value

    def store(self, signature: ReadDependencySignature, value: object) -> bool:
        """Store one payload when it fits the bounded cache."""
        size_bytes = estimate_payload_bytes(value)
        if size_bytes > self._max_bytes:
            return False
        with self._lock:
            key = signature.digest
            self._remove(key)
            self._entries[key] = _Entry(
                signature=signature,
                value=value,
                size_bytes=size_bytes,
            )
            self._entries.move_to_end(key)
            self._bytes += size_bytes
            while len(self._entries) > self._max_entries:
                oldest = next(iter(self._entries))
                self._remove(oldest)
            while self._bytes > self._max_bytes and self._entries:
                oldest = next(iter(self._entries))
                self._remove(oldest)
            return True

    def invalidate_assets(self, asset_ids: Iterable[str]) -> int:
        """Drop entries whose semantic selection names any invalidated asset."""
        targets = set(asset_ids)
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if targets.intersection(entry.signature.asset_ids)
            ]
            for key in keys:
                self._remove(key)
            return len(keys)

    def invalidate_workspace(self, workspace_id: object) -> int:
        """Drop every entry that belongs to one workspace identity."""
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if entry.signature.workspace_id == workspace_id
            ]
            for key in keys:
                self._remove(key)
            return len(keys)

    def clear(self) -> None:
        """Drop all payloads while preserving in-flight coalescing."""
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def get_or_load(
        self,
        signature: ReadDependencySignature,
        loader: Callable[[], T],
        *,
        revalidate: Callable[[], ReadDependencySignature] | None = None,
        family: ReadFamily | None = None,
    ) -> T:
        """Return a cached value, coalesce identical loads, or run the loader once."""
        started = perf_counter()
        key = signature.digest
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.signature.matches(signature):
                self._entries.move_to_end(key)
                self._record(family, "hit", started)
                return entry.value  # type: ignore[return-value]
            inflight = self._inflight.get(key)
            if inflight is not None:
                wait_for = inflight
            else:
                wait_for = _Inflight()
                self._inflight[key] = wait_for
        if inflight is not None:
            wait_for.event.wait()
            if wait_for.error is not None:
                self._record(family, "error", started)
                raise wait_for.error
            self._record(family, "coalesced", started)
            return wait_for.value  # type: ignore[return-value]
        try:
            value = loader()
            cacheable = True
            if revalidate is not None:
                try:
                    cacheable = revalidate().digest == key
                except Exception:
                    cacheable = False
            if cacheable:
                self.store(signature, value)
            self._record(family, "miss" if cacheable else "uncacheable", started)
            return value
        except BaseException as error:
            wait_for.error = error
            self._record(family, "error", started)
            raise
        finally:
            wait_for.value = locals().get("value")
            with self._lock:
                wait_for.event.set()
                self._inflight.pop(key, None)

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size_bytes

    def _record(self, family: ReadFamily | None, outcome: CacheOutcome, started: float) -> None:
        if self._telemetry is not None and family is not None:
            self._telemetry.record(
                family,
                (perf_counter() - started) * 1000.0,
                outcome,
            )


@dataclass
class _FamilyCounters:
    requests: int = 0
    errors: int = 0
    hits: int = 0
    misses: int = 0
    coalesced: int = 0
    uncacheable: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    durations_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_DURATION_WINDOW))


class ReadPerformanceRegistry:
    """Bounded, in-memory counters and duration windows for critical reads."""

    def __init__(self, *, generation: str, clock: Callable[[], float] = monotonic) -> None:
        if not generation:
            raise ValueError("read performance generation must not be empty")
        self._generation = generation
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.Lock()
        self._families: dict[str, _FamilyCounters] = {}
        self._requests = 0
        self._errors = 0

    def record(self, family: ReadFamily, elapsed_ms: float, outcome: CacheOutcome) -> None:
        """Record one completed read without retaining request labels or URLs."""
        if elapsed_ms < 0:
            elapsed_ms = 0.0
        with self._lock:
            counters = self._families.setdefault(family, _FamilyCounters())
            counters.requests += 1
            counters.total_ms += elapsed_ms
            counters.max_ms = max(counters.max_ms, elapsed_ms)
            counters.durations_ms.append(elapsed_ms)
            self._requests += 1
            if outcome == "hit":
                counters.hits += 1
            elif outcome == "coalesced":
                counters.coalesced += 1
            elif outcome == "uncacheable":
                counters.uncacheable += 1
            elif outcome == "error":
                counters.errors += 1
                self._errors += 1
            else:
                counters.misses += 1

    def summary(self) -> dict[str, object]:
        """Return the additive read-only telemetry document."""
        with self._lock:
            now = self._clock()
            families = {
                name: _summarize(counters) for name, counters in sorted(self._families.items())
            }
            return {
                "schema_version": _CRITICAL_READ_PERFORMANCE_SCHEMA,
                "generation": self._generation,
                "uptime_seconds": round(max(0.0, now - self._started_at), 3),
                "requests": self._requests,
                "errors": self._errors,
                "families": families,
            }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))
    return round(ordered[index], 3)


def _summarize(counters: _FamilyCounters) -> dict[str, object]:
    durations = list(counters.durations_ms)
    return {
        "requests": counters.requests,
        "errors": counters.errors,
        "hits": counters.hits,
        "misses": counters.misses,
        "coalesced": counters.coalesced,
        "uncacheable": counters.uncacheable,
        "total_ms": round(counters.total_ms, 3),
        "max_ms": round(counters.max_ms, 3),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "window_samples": len(durations),
    }


def parse_read_family(value: NonEmptyStr) -> ReadFamily:
    """Validate one telemetry family name."""
    allowed = {
        "market-bars",
        "fundamentals",
        "documents",
        "institutional",
        "metrics",
        "valuation",
        "coverage",
        "listed-company-report",
    }
    if value not in allowed:
        raise ReadCacheError("critical read family is not recognized")
    return value  # type: ignore[return-value]
