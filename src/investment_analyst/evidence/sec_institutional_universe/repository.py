"""Append-only storage repository for official SEC Form 13F dataset evidence."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_universe.identity import (
    SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION,
    SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
    SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    _MAX_ZIP_BYTES,
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.storage import DocumentContentStore, RecordNotFoundError, StorageError
from investment_analyst.storage.document_content import DocumentContentError, DocumentContentReceipt


class SecInstitutionalUniverseRepositoryError(StorageError):
    """Failure reading, persisting, or validating Form 13F manager universe evidence."""


def dataset_revision_to_raw_record(revision: Sec13FDataSetRevision) -> RawRecord:
    event_time = datetime.combine(revision.period_end, time.min, tzinfo=UTC)
    return RawRecord(
        record_id=revision.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
            record_key=json.dumps(
                {"dataset_revision_id": str(revision.revision_id)}, sort_keys=True
            ),
            retrieved_at=revision.retrieved_at,
            raw_uri=revision.dataset_url,
            checksum_sha256=revision.content_sha256,
        ),
        event_time=event_time,
        available_at=revision.available_at,
        received_at=revision.retrieved_at,
        payload={
            "kind": "sec_13f_data_set_revision",
            "revision": revision.model_dump(mode="json"),
        },
        schema_version=revision.schema_version,
    )


def dataset_revision_from_raw_record(record: RawRecord) -> Sec13FDataSetRevision:
    if (
        record.asset_id is not None
        or record.source.source_id != SEC_13F_MANAGER_UNIVERSE_SOURCE_ID
        or record.schema_version != SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or set(record.payload) != {"kind", "revision"}
        or record.payload.get("kind") != "sec_13f_data_set_revision"
    ):
        raise SecInstitutionalUniverseRepositoryError("dataset revision RawRecord is malformed")

    try:
        revision = Sec13FDataSetRevision.model_validate_json(
            json.dumps(record.payload["revision"], separators=(",", ":"), sort_keys=True)
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise SecInstitutionalUniverseRepositoryError(
            "dataset revision payload is malformed"
        ) from error

    expected_key = json.dumps({"dataset_revision_id": str(revision.revision_id)}, sort_keys=True)
    expected_event_time = datetime.combine(revision.period_end, time.min, tzinfo=UTC)

    if (
        record.record_id != revision.raw_record_id
        or record.source.record_key != expected_key
        or record.event_time != expected_event_time
        or record.available_at != revision.available_at
        or record.received_at != revision.retrieved_at
        or record.source.retrieved_at != revision.retrieved_at
        or record.source.raw_uri != revision.dataset_url
        or record.source.checksum_sha256 != revision.content_sha256
    ):
        raise SecInstitutionalUniverseRepositoryError(
            "dataset revision RawRecord conflicts with model invariants"
        )

    return revision


def snapshot_to_raw_record(snapshot: Sec13FManagerUniverseSnapshot) -> RawRecord:
    return RawRecord(
        record_id=snapshot.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
            record_key=json.dumps({"snapshot_id": str(snapshot.snapshot_id)}, sort_keys=True),
            retrieved_at=snapshot.retrieved_at,
            raw_uri=SEC_13F_DATA_SETS_CATALOG_URL,
            checksum_sha256=snapshot.dataset_sha256,
        ),
        event_time=snapshot.event_time,
        available_at=snapshot.available_at,
        received_at=snapshot.retrieved_at,
        payload={
            "kind": "sec_13f_manager_universe_snapshot",
            "snapshot": snapshot.model_dump(mode="json"),
        },
        schema_version=snapshot.schema_version,
    )


def snapshot_from_raw_record(record: RawRecord) -> Sec13FManagerUniverseSnapshot:
    if (
        record.asset_id is not None
        or record.source.source_id != SEC_13F_MANAGER_UNIVERSE_SOURCE_ID
        or record.schema_version != SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or set(record.payload) != {"kind", "snapshot"}
        or record.payload.get("kind") != "sec_13f_manager_universe_snapshot"
    ):
        raise SecInstitutionalUniverseRepositoryError("snapshot RawRecord is malformed")

    try:
        snapshot = Sec13FManagerUniverseSnapshot.model_validate_json(
            json.dumps(record.payload["snapshot"], separators=(",", ":"), sort_keys=True)
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise SecInstitutionalUniverseRepositoryError("snapshot payload is malformed") from error

    expected_key = json.dumps({"snapshot_id": str(snapshot.snapshot_id)}, sort_keys=True)
    if (
        record.record_id != snapshot.raw_record_id
        or record.source.record_key != expected_key
        or record.event_time != snapshot.event_time
        or record.available_at != snapshot.available_at
        or record.received_at != snapshot.retrieved_at
        or record.source.retrieved_at != snapshot.retrieved_at
        or record.source.checksum_sha256 != snapshot.dataset_sha256
    ):
        raise SecInstitutionalUniverseRepositoryError(
            "snapshot RawRecord conflicts with model invariants"
        )

    return snapshot


def _dataset_revision_payload(record: RawRecord) -> dict[str, object] | None:
    if not isinstance(record.payload, dict):
        return None
    if record.payload.get("kind") != "sec_13f_data_set_revision":
        return None
    revision = record.payload.get("revision")
    return revision if isinstance(revision, dict) else None


def _is_dataset_revision_payload(record: RawRecord) -> bool:
    return _dataset_revision_payload(record) is not None


def _snapshot_payload(record: RawRecord) -> dict[str, object] | None:
    if not isinstance(record.payload, dict):
        return None
    if record.payload.get("kind") != "sec_13f_manager_universe_snapshot":
        return None
    snapshot = record.payload.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else None


class SecInstitutionalUniverseRepository:
    """Repository managing dataset archives, revisions, and universe snapshots."""

    def __init__(self, raw_records: Any, content_store: DocumentContentStore) -> None:
        self._raw_records = raw_records
        self._content_store = content_store

    def save_blob(self, content: bytes) -> DocumentContentReceipt:
        """Persist official dataset bytes into the document store respecting the 160 MiB limit."""
        if self._content_store._read_only:
            raise DocumentContentError("document content cannot be saved through read-only storage")
        if not isinstance(content, bytes) or not content:
            raise DocumentContentError("document content must be non-empty bytes")
        if len(content) > _MAX_ZIP_BYTES:
            raise DocumentContentError(
                f"document content exceeds the maximum limit of {_MAX_ZIP_BYTES}"
            )

        checksum = hashlib.sha256(content).hexdigest()
        target = self._content_store._path(checksum)
        self._content_store._assert_safe_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._content_store._assert_safe_path(target)

        if target.exists():
            self._content_store._verify_path(target, checksum, expected=content)
            return DocumentContentReceipt(checksum, len(content), False)

        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                self._content_store._verify_path(target, checksum, expected=content)
                return DocumentContentReceipt(checksum, len(content), False)
            os.replace(temporary, target)
            self._content_store._verify_path(target, checksum, expected=content)
            return DocumentContentReceipt(checksum, len(content), True)
        finally:
            temporary.unlink(missing_ok=True)

    def verify_blob(self, checksum: str, *, size_bytes: int | None = None) -> None:
        """Verify that the dataset blob exists and matches checksum without reading all bytes."""
        self._content_store.verify(checksum, size_bytes=size_bytes)

    def save_dataset_revision(self, revision: Sec13FDataSetRevision) -> Sec13FDataSetRevision:
        """Persist a dataset revision record append-only, checking for conflicts."""
        existing = self.get_dataset_revision(revision.revision_id)
        if existing is not None:
            if existing != revision:
                raise SecInstitutionalUniverseRepositoryError(
                    f"Dataset revision {revision.revision_id} conflicts with existing record"
                )
            return existing

        self._raw_records.save(dataset_revision_to_raw_record(revision))
        return revision

    def get_dataset_revision(self, revision_id: UUID) -> Sec13FDataSetRevision | None:
        """Retrieve one dataset revision by its deterministic identifier."""
        try:
            raw_id = (
                Sec13FDataSetRevision.expected_raw_record_id(revision_id)
                if hasattr(Sec13FDataSetRevision, "expected_raw_record_id")
                else None
            )
        except Exception:
            raw_id = None
        if raw_id is None:
            from investment_analyst.evidence.sec_institutional_universe.identity import (
                dataset_raw_record_id,
            )

            raw_id = dataset_raw_record_id(revision_id)

        try:
            record = self._raw_records.get(raw_id)
        except RecordNotFoundError:
            return None
        return dataset_revision_from_raw_record(record)

    def save_snapshot(
        self, snapshot: Sec13FManagerUniverseSnapshot
    ) -> Sec13FManagerUniverseSnapshot:
        """Persist a universe snapshot record append-only, checking for conflicts."""
        existing = self.get_snapshot(snapshot.snapshot_id)
        if existing is not None:
            if existing != snapshot:
                raise SecInstitutionalUniverseRepositoryError(
                    f"Snapshot {snapshot.snapshot_id} conflicts with existing record"
                )
            return existing

        self._raw_records.save(snapshot_to_raw_record(snapshot))
        return snapshot

    def get_snapshot(self, snapshot_id: UUID) -> Sec13FManagerUniverseSnapshot | None:
        """Retrieve one universe snapshot by its deterministic identifier."""
        from investment_analyst.evidence.sec_institutional_universe.identity import (
            snapshot_raw_record_id,
        )

        raw_id = snapshot_raw_record_id(snapshot_id)
        try:
            record = self._raw_records.get(raw_id)
        except RecordNotFoundError:
            return None
        return snapshot_from_raw_record(record)

    def list_dataset_revisions(
        self, *, period_start: date, period_end: date, known_at: datetime
    ) -> tuple[Sec13FDataSetRevision, ...]:
        """List every persisted revision of one exact official period available at the cut."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise SecInstitutionalUniverseRepositoryError(
                "known_at must be a timezone-aware UTC datetime"
            )

        records = self._raw_records.list(
            source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
            available_to=known_at,
        )
        revisions: list[Sec13FDataSetRevision] = []
        for record in records:
            if not _is_dataset_revision_payload(record):
                continue
            revision = dataset_revision_from_raw_record(record)
            if (revision.period_start, revision.period_end) != (period_start, period_end):
                continue
            if revision.available_at > known_at:
                continue
            revisions.append(revision)
        return tuple(revisions)

    def find_snapshot_for_period(
        self,
        *,
        period_start: date,
        period_end: date,
        dataset_url: str,
        known_at: datetime,
    ) -> tuple[Sec13FDataSetRevision, Sec13FManagerUniverseSnapshot] | None:
        """Resolve the newest verifiable revision and snapshot of one exact period and URL.

        Only evidence already persisted by the integrated universe pipeline is reused; the resolved
        revision must match the exact official URL of the catalog link, the dataset blob must exist
        and match its SHA-256, and the snapshot must be available at the requested cut. A
        contradictory or unverifiable store fails closed instead of silently selecting a competitor.
        """
        revisions = self.list_dataset_revisions(
            period_start=period_start, period_end=period_end, known_at=known_at
        )
        matching = tuple(item for item in revisions if item.dataset_url == dataset_url)
        if not matching:
            return None

        newest = max(matching, key=lambda item: (item.retrieved_at, str(item.revision_id)))
        for other in matching:
            if (
                other.retrieved_at == newest.retrieved_at
                and other.content_sha256 != newest.content_sha256
            ):
                raise SecInstitutionalUniverseRepositoryError(
                    f"Incompatible competing revisions for period "
                    f"{period_start}..{period_end} at the same retrieval time"
                )

        snapshot = self._newest_snapshot_for_revision(newest.revision_id, known_at=known_at)
        if snapshot is None:
            return None
        if snapshot.dataset_sha256 != newest.content_sha256:
            raise SecInstitutionalUniverseRepositoryError(
                "manager universe snapshot lineage does not verify against its revision"
            )
        if (snapshot.period_start, snapshot.period_end) != (period_start, period_end):
            raise SecInstitutionalUniverseRepositoryError(
                "manager universe snapshot period conflicts with its dataset revision"
            )
        self.verify_blob(snapshot.dataset_sha256, size_bytes=newest.size_bytes)
        return newest, snapshot

    def _newest_snapshot_for_revision(
        self, revision_id: UUID, *, known_at: datetime
    ) -> Sec13FManagerUniverseSnapshot | None:
        records = self._raw_records.list(
            source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
            available_to=known_at,
        )
        candidates: list[Sec13FManagerUniverseSnapshot] = []
        for record in records:
            raw_snapshot = _snapshot_payload(record)
            if raw_snapshot is None:
                continue
            if str(raw_snapshot.get("dataset_revision_id")) != str(revision_id):
                continue
            snapshot = snapshot_from_raw_record(record)
            if snapshot.available_at > known_at:
                continue
            candidates.append(snapshot)
        if not candidates:
            return None
        newest = max(candidates, key=lambda item: (item.retrieved_at, str(item.snapshot_id)))
        for other in candidates:
            if (
                other.retrieved_at == newest.retrieved_at
                and other.snapshot_id != newest.snapshot_id
            ):
                raise SecInstitutionalUniverseRepositoryError(
                    "competing manager universe snapshots share one retrieval time"
                )
        return newest

    def find_latest_snapshot(self, *, known_at: datetime) -> Sec13FManagerUniverseSnapshot | None:
        """Find the latest point-in-time snapshot strictly available at known_at."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise SecInstitutionalUniverseRepositoryError(
                "known_at must be a timezone-aware UTC datetime"
            )

        records = self._raw_records.list(
            source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
            available_to=known_at,
        )

        candidate_snapshots: list[Sec13FManagerUniverseSnapshot] = []
        for record in records:
            if (
                isinstance(record.payload, dict)
                and record.payload.get("kind") == "sec_13f_manager_universe_snapshot"
            ):
                if record.available_at > known_at:
                    continue  # Strict PIT defense
                snapshot = snapshot_from_raw_record(record)
                candidate_snapshots.append(snapshot)

        if not candidate_snapshots:
            return None

        # Sort descending by period_end, period_start, retrieved_at
        candidate_snapshots.sort(
            key=lambda s: (s.period_end, s.period_start, s.retrieved_at),
            reverse=True,
        )

        # Check for semantically incompatible ties at the latest period
        latest = candidate_snapshots[0]
        for other in candidate_snapshots[1:]:
            if (
                (other.period_end, other.period_start) == (latest.period_end, latest.period_start)
                and other.dataset_sha256 != latest.dataset_sha256
                and other.retrieved_at == latest.retrieved_at
            ):
                raise SecInstitutionalUniverseRepositoryError(
                    f"Incompatible competing snapshots for period "
                    f"{latest.period_start}..{latest.period_end}"
                )

        # Verify lineage: ensure the underlying ZIP blob exists and matches SHA
        self.verify_blob(latest.dataset_sha256)

        return latest
