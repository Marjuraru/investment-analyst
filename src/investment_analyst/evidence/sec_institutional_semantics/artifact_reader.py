"""Bounded read-through access to persisted institutional semantic artifacts."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection
from datetime import datetime
from threading import RLock
from uuid import UUID
from weakref import WeakKeyDictionary

from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
    InstitutionalHoldingsSemantics,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
)
from investment_analyst.storage.errors import RecordNotFoundError, StorageError
from investment_analyst.storage.raw_records import JsonRawRecordRepository

_MAX_SESSION_ARTIFACTS = 256
_MANAGER_SELECTION_BATCH_SIZE = 512


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

    def list_for_manager(
        self, *, manager_cik: str, known_at: datetime
    ) -> tuple[InstitutionalHoldingsSemantics, ...]:
        """Return exactly the PIT revisions of one manager in the repository order.

        Selection by closed-set identity precedes hydration: only the manager's
        candidates are read, validated and ordered.
        """
        manager = normalize_cik(manager_cik)
        selected_ids = self._raw_records.select_record_ids_by_json_field(
            field="semantics_manager",
            values=(manager,),
            source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
            schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
            available_to=known_at,
        )
        return self._hydrate_selected_in_order(selected_ids, expected_manager=manager)

    def get_many(self, record_ids: Collection[UUID]) -> dict[UUID, InstitutionalHoldingsSemantics]:
        """Hydrate and validate exactly the selected artifacts, preserving determinism."""
        ordered_ids = tuple(dict.fromkeys(record_ids))
        if not ordered_ids:
            return {}
        try:
            records_by_id = self._raw_records.get_many(ordered_ids)
        except RecordNotFoundError as error:
            raise StorageError("selected institutional semantic artifact is absent") from error
        resolved: dict[UUID, InstitutionalHoldingsSemantics] = {}
        for record_id in ordered_ids:
            try:
                record = records_by_id[record_id]
            except KeyError as error:
                raise StorageError("selected institutional semantic artifact is absent") from error
            resolved[record_id] = self._validated(record_id, record)
        return resolved

    def _hydrate_selected_in_order(
        self, record_ids: list[UUID], *, expected_manager: str
    ) -> tuple[InstitutionalHoldingsSemantics, ...]:
        ordered: list[InstitutionalHoldingsSemantics] = []
        for offset in range(0, len(record_ids), _MANAGER_SELECTION_BATCH_SIZE):
            batch_ids = record_ids[offset : offset + _MANAGER_SELECTION_BATCH_SIZE]
            try:
                records_by_id = self._raw_records.get_many(batch_ids)
            except RecordNotFoundError as error:
                raise StorageError("selected institutional semantic artifact is absent") from error
            for record_id in batch_ids:
                try:
                    record = records_by_id[record_id]
                except KeyError as error:
                    raise StorageError(
                        "selected institutional semantic artifact is absent"
                    ) from error
                artifact = self._validated(record_id, record)
                if artifact.manager_cik != expected_manager:
                    raise StorageError(
                        "selected institutional semantic artifact belongs to another manager"
                    )
                ordered.append(artifact)
        return tuple(ordered)

    def _validated(self, record_id: UUID, record: object) -> InstitutionalHoldingsSemantics:
        artifact = semantics_from_raw_record(record)  # type: ignore[arg-type]
        cache = self._cache()
        with cache.lock:
            cached = cache.entries.get(record_id)
            if cached is not None:
                cache.entries.move_to_end(record_id)
                return cached
            cache.entries[record_id] = artifact
            if len(cache.entries) > _MAX_SESSION_ARTIFACTS:
                cache.entries.popitem(last=False)
            return artifact

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
