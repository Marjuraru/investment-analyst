"""Append-only segmented operational journal with verified digests and compact snapshots."""

import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field

from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_DEFAULT_MAX_SEGMENT_BYTES = 512 * 1024
_DEFAULT_MAX_SEGMENT_RECORDS = 5_000


class OperationalJournalError(AaplOperationalStateError):
    """Base error for operational journal failures."""


class OperationalJournalCorruptionError(OperationalJournalError):
    """Raised when a journal snapshot, manifest, or segment is corrupted."""


class OperationalJournalDigestMismatchError(OperationalJournalCorruptionError):
    """Raised when a closed segment or snapshot digest fails verification."""


class JournalSegmentDescriptor(ContractModel):
    """Immutable metadata for one closed journal segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    digest: NonEmptyStr
    byte_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    closed_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        """Return serializable representation."""
        return {
            "name": self.name,
            "digest": self.digest,
            "byte_count": self.byte_count,
            "record_count": self.record_count,
            "closed_at": self.closed_at.isoformat(),
        }


class JournalManifest(ContractModel):
    """Directory manifest tracking active segments and snapshot state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bounded-operational-journal-v1"] = "bounded-operational-journal-v1"
    journal_id: NonEmptyStr
    snapshot_filename: NonEmptyStr | None = None
    snapshot_digest: NonEmptyStr | None = None
    snapshot_record_count: int = Field(default=0, ge=0)
    legacy_v1_folded: bool = False
    closed_segments: tuple[JournalSegmentDescriptor, ...] = ()
    open_segment_name: NonEmptyStr | None = None

    def to_json_dict(self) -> dict[str, object]:
        """Return serializable representation."""
        return {
            "schema_version": self.schema_version,
            "journal_id": self.journal_id,
            "snapshot_filename": self.snapshot_filename,
            "snapshot_digest": self.snapshot_digest,
            "snapshot_record_count": self.snapshot_record_count,
            "legacy_v1_folded": self.legacy_v1_folded,
            "closed_segments": [segment.to_json_dict() for segment in self.closed_segments],
            "open_segment_name": self.open_segment_name,
        }


class JournalSnapshot(ContractModel):
    """Compacted operational state checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bounded-operational-journal-v1"] = "bounded-operational-journal-v1"
    journal_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    created_at: UTCDateTime
    record_count: int = Field(ge=0)
    records: tuple[dict[str, object], ...]
    legacy_v1_folded: bool = False

    def to_json_dict(self) -> dict[str, object]:
        """Return serializable representation."""
        return {
            "schema_version": self.schema_version,
            "journal_id": self.journal_id,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at.isoformat(),
            "record_count": self.record_count,
            "records": list(self.records),
            "legacy_v1_folded": self.legacy_v1_folded,
        }


def _compute_sha256(content: bytes) -> str:
    """Compute SHA-256 hexadecimal digest for raw bytes."""
    return hashlib.sha256(content).hexdigest()


def atomic_write(target_path: Path, content: bytes) -> None:
    """Write bytes atomically to target_path with sync."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target_path)
        directory = os.open(target_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise OperationalJournalError(
            f"journal file {target_path.name} could not be written atomically"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


_atomic_write = atomic_write


class BoundedOperationalJournal:
    """Append-only segmented operational journal with verified digests and compact snapshots."""

    def __init__(
        self,
        directory: Path,
        *,
        state_root: Path | None = None,
        max_segment_bytes: int = _DEFAULT_MAX_SEGMENT_BYTES,
        max_segment_records: int = _DEFAULT_MAX_SEGMENT_RECORDS,
        journal_id: str = "default",
        reducer: Callable[[Sequence[dict[str, object]]], Sequence[dict[str, object]]] | None = None,
    ) -> None:
        self._directory = directory.expanduser().resolve(strict=False)
        self._state_root = (
            state_root.expanduser().resolve(strict=False)
            if state_root is not None
            else self._directory
        )
        if not self._directory.is_relative_to(self._state_root):
            raise OperationalJournalError("journal directory must be within declared state_root")
        if max_segment_bytes <= 0:
            raise OperationalJournalError("max_segment_bytes must be positive")
        if max_segment_records <= 0:
            raise OperationalJournalError("max_segment_records must be positive")

        self._max_segment_bytes = max_segment_bytes
        self._max_segment_records = max_segment_records
        self._journal_id = journal_id
        self._reducer = reducer
        self._lock = threading.RLock()
        self._manifest_path = self._directory / "manifest.json"

    @property
    def directory(self) -> Path:
        """Return the root directory of this journal."""
        return self._directory

    def has_data(self) -> bool:
        """Return True if the journal manifest or any data exists."""
        with self._lock:
            if self._manifest_path.exists():
                return True
            if not self._directory.exists():
                return False
            return any(self._directory.iterdir())

    def has_snapshot(self) -> bool:
        """Return True if a valid snapshot is referenced in the manifest."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is None:
                return False
            return manifest.snapshot_filename is not None

    def has_legacy_v1_folded(self) -> bool:
        """Return True if legacy v1 state has already been folded into snapshot."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is None:
                return False
            return manifest.legacy_v1_folded

    def _load_manifest(self) -> JournalManifest | None:
        """Load and validate manifest.json if present, failing closed on corruption."""
        if not self._manifest_path.exists():
            return None
        try:
            content = self._manifest_path.read_text(encoding="utf-8")
            return JournalManifest.model_validate_json(content)
        except (OSError, UnicodeError, ValueError) as error:
            raise OperationalJournalCorruptionError("journal manifest is corrupted") from error

    def _save_manifest(self, manifest: JournalManifest) -> None:
        """Atomically persist manifest.json."""
        document = (
            json.dumps(
                manifest.to_json_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        _atomic_write(self._manifest_path, document)

    def _load_snapshot(
        self, manifest: JournalManifest
    ) -> tuple[tuple[dict[str, object], ...], bool]:
        """Load snapshot records and legacy folding status, failing closed on corruption."""
        if manifest.snapshot_filename is None:
            return (), manifest.legacy_v1_folded
        snapshot_path = self._directory / manifest.snapshot_filename
        if not snapshot_path.exists():
            raise OperationalJournalCorruptionError("journal snapshot file is missing")
        try:
            content = snapshot_path.read_bytes()
        except OSError as error:
            raise OperationalJournalCorruptionError("journal snapshot could not be read") from error

        digest = _compute_sha256(content)
        if manifest.snapshot_digest is not None and digest != manifest.snapshot_digest:
            raise OperationalJournalDigestMismatchError("journal snapshot digest mismatch")

        try:
            snapshot = JournalSnapshot.model_validate_json(content)
        except (UnicodeError, ValueError) as error:
            raise OperationalJournalCorruptionError("journal snapshot is malformed") from error

        if len(snapshot.records) != snapshot.record_count:
            raise OperationalJournalCorruptionError("snapshot record count mismatch")
        return snapshot.records, snapshot.legacy_v1_folded

    def _read_closed_segment(self, descriptor: JournalSegmentDescriptor) -> list[dict[str, object]]:
        """Read and verify a closed segment against its manifest digest."""
        segment_path = self._directory / descriptor.name
        if not segment_path.exists():
            raise OperationalJournalCorruptionError(f"closed segment {descriptor.name} is missing")
        try:
            content = segment_path.read_bytes()
        except OSError as error:
            raise OperationalJournalCorruptionError(
                f"closed segment {descriptor.name} could not be read"
            ) from error

        digest = _compute_sha256(content)
        if digest != descriptor.digest:
            raise OperationalJournalDigestMismatchError(
                f"closed segment {descriptor.name} digest mismatch"
            )

        records: list[dict[str, object]] = []
        for line in content.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            try:
                item = json.loads(line.decode("utf-8"))
                if not isinstance(item, dict):
                    raise ValueError("record is not a dict")
                records.append(item)
            except (UnicodeError, ValueError) as error:
                raise OperationalJournalCorruptionError(
                    f"closed segment {descriptor.name} contains invalid record"
                ) from error
        return records

    def _read_open_segment(self, segment_name: str) -> list[dict[str, object]]:
        """Read the open segment with trailing partial line recovery."""
        segment_path = self._directory / segment_name
        if not segment_path.exists():
            return []
        try:
            content = segment_path.read_bytes()
        except OSError as error:
            raise OperationalJournalCorruptionError(
                f"open segment {segment_name} could not be read"
            ) from error

        if not content:
            return []

        raw_chunks = content.split(b"\n")
        valid_records: list[dict[str, object]] = []
        valid_byte_slices: list[bytes] = []

        total_chunks = len(raw_chunks)
        for index, chunk in enumerate(raw_chunks):
            is_last = index == total_chunks - 1
            if is_last and not chunk:
                # File ended cleanly with \n
                break
            if not chunk.strip():
                continue

            try:
                record = json.loads(chunk.decode("utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("record is not a dict")
                valid_records.append(record)
                valid_byte_slices.append(chunk + b"\n")
            except (UnicodeError, ValueError) as error:
                if is_last:
                    # Recover from trailing partial line at EOF without losing prior records
                    break
                raise OperationalJournalCorruptionError(
                    f"open segment {segment_name} contains corrupted record before EOF"
                ) from error

        recovered_bytes = b"".join(valid_byte_slices)
        if len(recovered_bytes) < len(content):
            # Truncate to last confirmed valid line so subsequent writes stay aligned
            try:
                with open(segment_path, "wb") as stream:
                    stream.write(recovered_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                raise OperationalJournalError(
                    f"open segment {segment_name} could not be truncated"
                ) from error

        return valid_records

    def read_entries(self) -> tuple[dict[str, object], ...]:
        """Read all verified records from snapshot, closed segments, and open segment."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is None:
                return ()

            records: list[dict[str, object]] = []
            snapshot_records, _ = self._load_snapshot(manifest)
            records.extend(snapshot_records)

            for descriptor in manifest.closed_segments:
                records.extend(self._read_closed_segment(descriptor))

            if manifest.open_segment_name is not None:
                records.extend(self._read_open_segment(manifest.open_segment_name))

            return tuple(records)

    def append(self, record: dict[str, object]) -> None:
        """Append one record to the open segment in amortized O(1) with sync."""
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            manifest = self._load_manifest()
            if manifest is None:
                manifest = JournalManifest(
                    journal_id=self._journal_id,
                    open_segment_name="segment-000001.jsonl",
                )
                self._save_manifest(manifest)

            open_name = manifest.open_segment_name
            if open_name is None:
                open_name = "segment-000001.jsonl"
                manifest = manifest.model_copy(update={"open_segment_name": open_name})
                self._save_manifest(manifest)

            segment_path = self._directory / open_name
            line = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )

            try:
                descriptor = os.open(segment_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "ab", closefd=True) as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                raise OperationalJournalError(
                    f"failed to append record to segment {open_name}"
                ) from error

            # Check if open segment exceeded size/count threshold
            try:
                stat = segment_path.stat()
                if stat.st_size >= self._max_segment_bytes:
                    self.rotate(fold=True)
            except OSError:
                pass

    def rotate(self, *, fold: bool = True) -> None:
        """Close the current open segment, record digest, and optionally fold into snapshot."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is None or manifest.open_segment_name is None:
                return

            open_name = manifest.open_segment_name
            segment_path = self._directory / open_name
            if not segment_path.exists() or segment_path.stat().st_size == 0:
                return

            content = segment_path.read_bytes()
            digest = _compute_sha256(content)
            records = self._read_open_segment(open_name)
            now = datetime.now(UTC)

            descriptor = JournalSegmentDescriptor(
                name=open_name,
                digest=digest,
                byte_count=len(content),
                record_count=len(records),
                closed_at=now,
            )

            # Determine next open segment name
            index = 1
            if open_name.startswith("segment-") and open_name.endswith(".jsonl"):
                try:
                    index = int(open_name[len("segment-") : -len(".jsonl")]) + 1
                except ValueError:
                    index = len(manifest.closed_segments) + 2
            next_open_name = f"segment-{index:06d}.jsonl"

            updated_closed = manifest.closed_segments + (descriptor,)
            manifest = manifest.model_copy(
                update={
                    "closed_segments": updated_closed,
                    "open_segment_name": next_open_name,
                }
            )
            self._save_manifest(manifest)

            if fold:
                self.fold_snapshot()

    def fold_snapshot(
        self,
        reducer: Callable[[Sequence[dict[str, object]]], Sequence[dict[str, object]]] | None = None,
    ) -> None:
        """Compact snapshot and closed segments into a new snapshot, updating manifest."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is None:
                return

            records_to_fold: list[dict[str, object]] = []
            snapshot_records, legacy_v1_folded = self._load_snapshot(manifest)
            records_to_fold.extend(snapshot_records)

            for descriptor in manifest.closed_segments:
                records_to_fold.extend(self._read_closed_segment(descriptor))

            effective_reducer = reducer if reducer is not None else self._reducer
            if effective_reducer is not None:
                folded = tuple(effective_reducer(records_to_fold))
            else:
                folded = tuple(records_to_fold)

            raw_payload = json.dumps(folded, separators=(",", ":"), sort_keys=True).encode("utf-8")
            snapshot_id = hashlib.sha256(b"snapshot:" + raw_payload).hexdigest()[:32]
            created_at = datetime(2026, 1, 1, tzinfo=UTC)
            if folded and "started_at" in folded[0] and isinstance(folded[0]["started_at"], str):
                with contextlib.suppress(ValueError):
                    created_at = datetime.fromisoformat(folded[0]["started_at"])

            snapshot = JournalSnapshot(
                journal_id=self._journal_id,
                snapshot_id=snapshot_id,
                created_at=created_at,
                record_count=len(folded),
                records=folded,
                legacy_v1_folded=legacy_v1_folded,
            )

            snapshot_bytes = (
                json.dumps(
                    snapshot.to_json_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )

            snapshot_path = self._directory / "snapshot.json"
            _atomic_write(snapshot_path, snapshot_bytes)
            snapshot_digest = _compute_sha256(snapshot_bytes)

            # Clean up closed segment files that are now compacted into snapshot
            for descriptor in manifest.closed_segments:
                (self._directory / descriptor.name).unlink(missing_ok=True)

            manifest = manifest.model_copy(
                update={
                    "snapshot_filename": "snapshot.json",
                    "snapshot_digest": snapshot_digest,
                    "snapshot_record_count": len(folded),
                    "legacy_v1_folded": legacy_v1_folded,
                    "closed_segments": (),
                }
            )
            self._save_manifest(manifest)

    def mark_legacy_v1_folded(self) -> None:
        """Mark legacy v1 as folded in manifest without creating a snapshot file."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is None:
                manifest = JournalManifest(
                    journal_id=self._journal_id,
                    open_segment_name="segment-000001.jsonl",
                    legacy_v1_folded=True,
                )
            else:
                manifest = manifest.model_copy(update={"legacy_v1_folded": True})
            self._save_manifest(manifest)

    def initialize_snapshot_with_legacy(
        self,
        records: Sequence[dict[str, object]],
    ) -> None:
        """Initialize the snapshot with legacy v1 records once, setting legacy_v1_folded=True."""
        with self._lock:
            manifest = self._load_manifest()
            if manifest is not None and manifest.legacy_v1_folded:
                return

            self._directory.mkdir(parents=True, exist_ok=True)
            effective_reducer = self._reducer
            if effective_reducer is not None:
                folded = tuple(effective_reducer(records))
            else:
                folded = tuple(records)

            raw_payload = json.dumps(folded, separators=(",", ":"), sort_keys=True).encode("utf-8")
            snapshot_id = hashlib.sha256(b"legacy-snapshot:" + raw_payload).hexdigest()[:32]
            created_at = datetime(2026, 1, 1, tzinfo=UTC)
            if folded and "started_at" in folded[0] and isinstance(folded[0]["started_at"], str):
                with contextlib.suppress(ValueError):
                    created_at = datetime.fromisoformat(folded[0]["started_at"])

            snapshot = JournalSnapshot(
                journal_id=self._journal_id,
                snapshot_id=snapshot_id,
                created_at=created_at,
                record_count=len(folded),
                records=folded,
                legacy_v1_folded=True,
            )

            snapshot_bytes = (
                json.dumps(
                    snapshot.to_json_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )

            snapshot_path = self._directory / "snapshot.json"
            _atomic_write(snapshot_path, snapshot_bytes)
            snapshot_digest = _compute_sha256(snapshot_bytes)

            open_name = "segment-000001.jsonl"
            closed_segments = ()
            if manifest is not None:
                if manifest.open_segment_name is not None:
                    open_name = manifest.open_segment_name
                closed_segments = manifest.closed_segments

            manifest = JournalManifest(
                journal_id=self._journal_id,
                snapshot_filename="snapshot.json",
                snapshot_digest=snapshot_digest,
                snapshot_record_count=len(folded),
                legacy_v1_folded=True,
                closed_segments=closed_segments,
                open_segment_name=open_name,
            )
            self._save_manifest(manifest)
