"""Bounded read-through access to persisted institutional semantic artifacts."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from threading import RLock
from uuid import UUID
from weakref import WeakKeyDictionary

from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
    InstitutionalHoldingsSemantics,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
)
from investment_analyst.storage.raw_records import JsonRawRecordRepository

_MAX_SESSION_ARTIFACTS = 256


class _ArtifactCache:
    def __init__(self) -> None:
        self.entries: OrderedDict[UUID, InstitutionalHoldingsSemantics] = OrderedDict()
        self.lock = RLock()


class InstitutionalSemanticsArtifactReader:
    """Resolve visible semantic artifacts once per open storage session.

    Caches are keyed by the repository instance, so they are ephemeral,
    workspace-local, and never shared across storage sessions.
    """

    _caches: WeakKeyDictionary[JsonRawRecordRepository, _ArtifactCache] = WeakKeyDictionary()
    _caches_lock = RLock()

    def __init__(self, raw_records: JsonRawRecordRepository) -> None:
        self._raw_records = raw_records

    def list_visible(self, *, known_at: datetime) -> tuple[InstitutionalHoldingsSemantics, ...]:
        """Return the same point-in-time sequence as the raw-record repository."""
        record_ids = self._raw_records.list_record_ids(
            source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
            schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
            available_to=known_at,
        )
        return tuple(self.get(record_id) for record_id in record_ids)

    def get(self, record_id: UUID) -> InstitutionalHoldingsSemantics:
        """Read, validate, and memoize one semantic artifact by raw-record identity."""
        cache = self._cache()
        with cache.lock:
            cached = cache.entries.get(record_id)
            if cached is not None:
                cache.entries.move_to_end(record_id)
                return cached

            artifact = semantics_from_raw_record(self._raw_records.get(record_id))
            cache.entries[record_id] = artifact
            if len(cache.entries) > _MAX_SESSION_ARTIFACTS:
                cache.entries.popitem(last=False)
            return artifact

    def _cache(self) -> _ArtifactCache:
        with self._caches_lock:
            cache = self._caches.get(self._raw_records)
            if cache is None:
                cache = _ArtifactCache()
                self._caches[self._raw_records] = cache
            return cache
