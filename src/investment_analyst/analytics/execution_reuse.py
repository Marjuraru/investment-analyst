"""Fail-closed analytical access to persisted workspace-v2 execution reuse."""

from __future__ import annotations

from datetime import datetime

from investment_analyst.core.models import MetricResult
from investment_analyst.storage.execution_reuse_v2 import (
    ExecutionReuseBundleEntry,
    ExecutionReuseEntry,
    ExecutionReuseKey,
    ExecutionReuseV2Store,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.workspace_v2 import WorkspaceV2Store


class ExecutionReuseService:
    """Resolve a reusable persisted metric only when all semantic inputs match."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def lookup(self, key: ExecutionReuseKey, *, known_at: datetime) -> MetricResult | None:
        """Return a PIT-visible reusable result, or ``None`` for a cache miss."""
        store = self._reuse_store()
        if store is None:
            return None
        entry = store.find(key, known_at=known_at)
        if entry is None:
            return None
        result = self._storage.metric_results.get_existing([entry.result_id]).get(entry.result_id)
        if (
            result is None
            or result.available_at != entry.available_at
            or result.available_at > known_at
        ):
            return None
        return result

    def remember(
        self, key: ExecutionReuseKey, result: MetricResult, *, stored_at: datetime
    ) -> None:
        """Bind a persisted result to its exact semantic inputs after verification."""
        store = self._reuse_store()
        if store is None:
            return
        persisted = self._storage.metric_results.get_existing([result.result_id]).get(
            result.result_id
        )
        if persisted != result:
            return
        store.save(
            ExecutionReuseEntry(
                key=key,
                result_id=result.result_id,
                available_at=result.available_at,
                stored_at=stored_at,
            )
        )

    def lookup_bundle(
        self, key: ExecutionReuseKey, *, known_at: datetime
    ) -> tuple[MetricResult, ...] | None:
        """Return every persisted member of a PIT-visible reusable execution."""
        store = self._reuse_store()
        if store is None:
            return None
        entry = store.find_bundle(key, known_at=known_at)
        if entry is None:
            return None
        persisted = self._storage.metric_results.get_existing(entry.result_ids)
        if len(persisted) != len(entry.result_ids):
            return None
        results = tuple(persisted[result_id] for result_id in entry.result_ids)
        if any(
            result.available_at > known_at or result.available_at > entry.available_at
            for result in results
        ):
            return None
        return results

    def lookup_bundle_outcome(
        self, key: ExecutionReuseKey, *, known_at: datetime
    ) -> tuple[tuple[MetricResult, ...], str] | None:
        """Return a complete persisted result set with its canonical caller outcome."""
        store = self._reuse_store()
        if store is None:
            return None
        entry = store.find_bundle(key, known_at=known_at)
        if entry is None:
            return None
        persisted = self._storage.metric_results.get_existing(entry.result_ids)
        if len(persisted) != len(entry.result_ids):
            return None
        results = tuple(persisted[result_id] for result_id in entry.result_ids)
        if any(
            result.available_at > known_at or result.available_at > entry.available_at
            for result in results
        ):
            return None
        return results, entry.outcome_json

    def remember_bundle(
        self,
        key: ExecutionReuseKey,
        results: tuple[MetricResult, ...],
        *,
        stored_at: datetime,
        outcome_json: str = "{}",
        available_at: datetime | None = None,
    ) -> None:
        """Bind a complete persisted result set after independent persistence verification."""
        store = self._reuse_store()
        if store is None:
            return
        result_ids = tuple(result.result_id for result in results)
        if len(set(result_ids)) != len(result_ids):
            return
        persisted = self._storage.metric_results.get_existing(result_ids)
        if len(persisted) != len(results) or any(
            persisted.get(result.result_id) != result for result in results
        ):
            return
        bundle_available_at = max(
            (result.available_at for result in results),
            default=available_at or stored_at,
        )
        store.save_bundle(
            ExecutionReuseBundleEntry(
                key=key,
                result_ids=result_ids,
                outcome_json=outcome_json,
                available_at=bundle_available_at,
                stored_at=stored_at,
            )
        )

    def _reuse_store(self) -> ExecutionReuseV2Store | None:
        if not isinstance(self._storage.store, WorkspaceV2Store):
            return None
        return ExecutionReuseV2Store(self._storage.store.connection)


__all__ = ["ExecutionReuseService"]
