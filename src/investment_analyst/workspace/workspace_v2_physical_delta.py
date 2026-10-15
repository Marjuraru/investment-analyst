"""Streaming physical parent index for packed workspace-v2 backups.

The index binds each source-file range to an immutable, content-addressed
parent volume.  It deliberately writes JSONL incrementally: neither the
inventory nor the packed journal is materialized as a Python collection.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2BackupService,
    WorkspaceV2PackedStreamManifest,
    _iter_packed_journal,
)
from investment_analyst.workspace.workspace_v2_backup_inventory import (
    WorkspaceV2BackupInventoryError,
    iter_inventory,
)


class WorkspaceV2PhysicalDeltaError(RuntimeError):
    """The sealed physical parent index cannot be constructed or reused safely."""


class PhysicalDeltaIndexEntry(ContractModel):
    """One source file range, or an explicit zero-byte file, in its parent backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-entry-v1"
    path: NonEmptyStr
    file_sha256: NonEmptyStr
    file_size_bytes: int = Field(ge=0)
    offset_bytes: int = Field(ge=0)
    length_bytes: int = Field(ge=0)
    fragment_sha256: NonEmptyStr | None = None
    parent_volume_id: NonEmptyStr | None = None
    parent_object_key: NonEmptyStr | None = None
    parent_object_sha256: NonEmptyStr | None = None
    stream_offset_bytes: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def require_safe_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("physical delta path must be normalized and relative")
        return value

    @field_validator("file_sha256", "fragment_sha256", "parent_object_sha256", mode="after")
    @classmethod
    def require_digest(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("physical delta digest is invalid")
        return value

    @model_validator(mode="after")
    def require_consistent_range(self) -> PhysicalDeltaIndexEntry:
        parent = (
            self.fragment_sha256,
            self.parent_volume_id,
            self.parent_object_key,
            self.parent_object_sha256,
            self.stream_offset_bytes,
        )
        if self.length_bytes == 0:
            if (
                self.file_size_bytes
                or self.offset_bytes
                or any(value is not None for value in parent)
            ):
                raise ValueError("empty physical delta entry is invalid")
        elif self.offset_bytes + self.length_bytes > self.file_size_bytes or any(
            value is None for value in parent
        ):
            raise ValueError("physical delta range is invalid")
        return self


class PhysicalDeltaIndexTrailer(ContractModel):
    """Hash-bound terminal record for the incremental physical index."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-trailer-v1"
    parent_backup_id: UUID
    source_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    volume_count: int = Field(gt=0)
    file_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    index_sha256: NonEmptyStr

    @field_validator("inventory_sha256", "journal_sha256", "index_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("physical delta trailer digest is invalid")
        return value


class PhysicalDeltaIndexManifest(ContractModel):
    """Small immutable control record for a parent-volume physical index."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-manifest-v1"
    parent_backup_id: UUID
    source_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    index_path: NonEmptyStr
    index_sha256: NonEmptyStr
    volume_count: int = Field(gt=0)
    file_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    created_at: UTCDateTime

    @field_validator("index_path")
    @classmethod
    def require_relative_index_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("physical delta index path must be normalized and relative")
        return value

    @field_validator("inventory_sha256", "journal_sha256", "index_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("physical delta manifest digest is invalid")
        return value


class PhysicalDeltaIndexCheckpoint(ContractModel):
    """Durable cursor for a resumable JSONL physical index derivation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-checkpoint-v1"
    phase: Literal["writing", "sealed"]
    parent_backup_id: UUID
    source_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    entry_count: int = Field(ge=0)
    partial_index_sha256: NonEmptyStr

    @field_validator("inventory_sha256", "journal_sha256", "partial_index_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("physical delta checkpoint digest is invalid")
        return value


class WorkspaceV2PhysicalDeltaIndexService:
    """Derive and verify one resumable physical index without global collections."""

    def __init__(
        self,
        backup_service: WorkspaceV2BackupService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        checkpoint_interval: int = 256,
    ) -> None:
        if checkpoint_interval <= 0:
            raise ValueError("physical delta checkpoint interval must be positive")
        self._backup_service = backup_service
        self._clock = clock
        self._checkpoint_interval = checkpoint_interval

    def build(self, backup_root: Path, destination: Path) -> PhysicalDeltaIndexManifest:
        """Seal an index, resuming only an exact validated partial prefix."""
        root, backup_manifest = self._inspect(backup_root)
        destination_root = _new_destination(destination)
        index_path = destination_root / "physical-delta-index.jsonl"
        manifest_path = destination_root / "physical-delta-manifest.json"
        checkpoint_path = destination_root / "physical-delta-checkpoint.json"
        if index_path.exists():
            trailer = verify_physical_delta_index(index_path)
            _replace_json(
                checkpoint_path,
                _checkpoint_from_trailer(trailer, phase="sealed").model_dump(mode="json"),
            )
            if manifest_path.exists():
                try:
                    persisted = PhysicalDeltaIndexManifest.model_validate_json(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as error:
                    raise WorkspaceV2PhysicalDeltaError(
                        "physical delta manifest is malformed"
                    ) from error
                expected = _manifest_from_trailer(
                    trailer,
                    index_path=index_path.name,
                    created_at=persisted.created_at,
                )
                if persisted != expected:
                    raise WorkspaceV2PhysicalDeltaError("physical delta manifest binding differs")
                return persisted
            manifest = _manifest_from_trailer(
                trailer, index_path=index_path.name, created_at=self._now()
            )
            _write_new_json(manifest_path, manifest.model_dump(mode="json"))
            return manifest
        temporary = _partial_index_path(index_path)
        if temporary.exists() and (temporary.is_symlink() or not temporary.is_file()):
            raise WorkspaceV2PhysicalDeltaError("physical delta partial index is unsafe")
        prefix_count, prefix_digest = self._verify_partial_prefix(temporary, root, backup_manifest)
        try:
            with temporary.open("a", encoding="utf-8", newline="\n") as stream:
                trailer = self._write_records(
                    stream,
                    root,
                    backup_manifest,
                    skip_count=prefix_count,
                    digest=prefix_digest,
                )
                stream.write(_compact_json(trailer.model_dump(mode="json")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, index_path)
            _fsync_directory(destination_root)
        finally:
            if index_path.exists():
                temporary.unlink(missing_ok=True)
        if verify_physical_delta_index(index_path) != trailer:
            raise WorkspaceV2PhysicalDeltaError("physical delta index changed after sealing")
        _replace_json(
            checkpoint_path,
            _checkpoint_from_trailer(trailer, phase="sealed").model_dump(mode="json"),
        )
        manifest = _manifest_from_trailer(
            trailer, index_path=index_path.name, created_at=self._now()
        )
        _write_new_json(manifest_path, manifest.model_dump(mode="json"))
        return manifest

    def _inspect(self, backup_root: Path) -> tuple[Path, WorkspaceV2PackedStreamManifest]:
        try:
            root, manifest = self._backup_service.inspect_packed_for_restore(backup_root)
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2PhysicalDeltaError(
                "physical delta parent backup is not reusable"
            ) from error
        if not isinstance(manifest, WorkspaceV2PackedStreamManifest):
            raise WorkspaceV2PhysicalDeltaError("physical delta requires streamable packed backup")
        _verify_parent_volumes(root, manifest)
        return root, manifest

    def _verify_partial_prefix(
        self,
        partial: Path,
        root: Path,
        manifest: WorkspaceV2PackedStreamManifest,
    ) -> tuple[int, hashlib._Hash]:
        digest = hashlib.sha256()
        if not partial.exists():
            return 0, digest
        count = 0
        expected = _iter_entries(root, manifest)
        try:
            with partial.open("rb") as stream:
                for raw_line in stream:
                    if not raw_line.endswith(b"\n"):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta partial index is incomplete"
                        )
                    try:
                        actual = PhysicalDeltaIndexEntry.model_validate_json(raw_line)
                        wanted = next(expected)
                    except (StopIteration, ValueError) as error:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta partial index binding differs"
                        ) from error
                    if actual != wanted:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta partial index binding differs"
                        )
                    digest.update(raw_line)
                    count += 1
        except OSError as error:
            raise WorkspaceV2PhysicalDeltaError(
                "physical delta partial index is unavailable"
            ) from error
        return count, digest

    def _write_records(
        self,
        stream: object,
        root: Path,
        manifest: WorkspaceV2PackedStreamManifest,
        *,
        skip_count: int,
        digest: hashlib._Hash,
    ) -> PhysicalDeltaIndexTrailer:
        file_count = 0
        entry_count = 0
        logical_bytes = 0
        for entry in _iter_entries(root, manifest):
            entry_count += 1
            if entry.offset_bytes == 0:
                file_count += 1
                logical_bytes += entry.file_size_bytes
            encoded = (_compact_json(entry.model_dump(mode="json")) + "\n").encode("utf-8")
            if entry_count > skip_count:
                stream.write(encoded.decode("utf-8"))
                digest.update(encoded)
                if entry_count % self._checkpoint_interval == 0:
                    stream.flush()
                    os.fsync(stream.fileno())
        if entry_count < skip_count:
            raise WorkspaceV2PhysicalDeltaError("physical delta partial index exceeds parent")
        return PhysicalDeltaIndexTrailer(
            parent_backup_id=manifest.backup_id,
            source_workspace_id=manifest.source_workspace_id,
            inventory_sha256=manifest.inventory.digest_sha256,
            journal_sha256=manifest.journal_sha256,
            volume_count=manifest.volume_count,
            file_count=file_count,
            entry_count=entry_count,
            logical_bytes=logical_bytes,
            index_sha256=digest.hexdigest(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2PhysicalDeltaError("physical delta clock must be timezone-aware")
        return value.astimezone(UTC)


def _iter_entries(
    root: Path, manifest: WorkspaceV2PackedStreamManifest
) -> Iterator[PhysicalDeltaIndexEntry]:
    """Join one inventory entry and one packed fragment at a time."""
    entries = iter_inventory(root / "source-inventory.jsonl")
    current = _next_inventory_entry(entries, previous_path=None)
    offset = 0
    last_path: str | None = None
    journal_digest = hashlib.sha256()
    volume_count = 0
    try:
        for volume in _iter_packed_journal(
            root / "packed-backup-volumes.jsonl", digest=journal_digest
        ):
            volume_count += 1
            for fragment in volume.fragments:
                while current is not None and current[1] == 0:
                    path, _, file_digest = current
                    yield PhysicalDeltaIndexEntry(
                        path=path,
                        file_sha256=file_digest,
                        file_size_bytes=0,
                        offset_bytes=0,
                        length_bytes=0,
                    )
                    last_path = path
                    current = _next_inventory_entry(entries, previous_path=last_path)
                if current is None:
                    raise WorkspaceV2PhysicalDeltaError("packed journal exceeds parent inventory")
                path, size, file_digest = current
                if (
                    fragment.path != path
                    or fragment.offset_bytes != offset
                    or fragment.length_bytes > size - offset
                    or fragment.stream_offset_bytes < 0
                ):
                    raise WorkspaceV2PhysicalDeltaError(
                        "packed journal mapping differs from inventory"
                    )
                yield PhysicalDeltaIndexEntry(
                    path=path,
                    file_sha256=file_digest,
                    file_size_bytes=size,
                    offset_bytes=offset,
                    length_bytes=fragment.length_bytes,
                    fragment_sha256=fragment.sha256,
                    parent_volume_id=volume.volume_id,
                    parent_object_key=f"volumes/{volume.compressed_sha256}.gz",
                    parent_object_sha256=volume.compressed_sha256,
                    stream_offset_bytes=fragment.stream_offset_bytes,
                )
                offset += fragment.length_bytes
                if offset == size:
                    last_path = path
                    current = _next_inventory_entry(entries, previous_path=last_path)
                    offset = 0
    except (WorkspaceV2BackupError, WorkspaceV2BackupInventoryError) as error:
        raise WorkspaceV2PhysicalDeltaError(
            "physical delta parent sidecars are malformed"
        ) from error
    while current is not None and current[1] == 0:
        path, _, file_digest = current
        yield PhysicalDeltaIndexEntry(
            path=path,
            file_sha256=file_digest,
            file_size_bytes=0,
            offset_bytes=0,
            length_bytes=0,
        )
        last_path = path
        current = _next_inventory_entry(entries, previous_path=last_path)
    if current is not None or offset:
        raise WorkspaceV2PhysicalDeltaError("packed journal does not cover parent inventory")
    if (
        volume_count != manifest.volume_count
        or journal_digest.hexdigest() != manifest.journal_sha256
    ):
        raise WorkspaceV2PhysicalDeltaError("packed journal binding differs from parent manifest")


def _next_inventory_entry(
    entries: Iterator[dict[str, object]], *, previous_path: str | None
) -> tuple[str, int, str] | None:
    try:
        entry = next(entries)
    except StopIteration:
        return None
    path = entry["path"]
    size = entry["size_bytes"]
    digest = entry["sha256"]
    if (
        not isinstance(path, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or (previous_path is not None and path == previous_path)
    ):
        raise WorkspaceV2PhysicalDeltaError("parent inventory ordering is invalid")
    return path, size, digest


def _verify_parent_volumes(root: Path, manifest: WorkspaceV2PackedStreamManifest) -> None:
    """Read each parent volume once before declaring it reusable."""
    count = 0
    journal_digest = hashlib.sha256()
    try:
        for volume in _iter_packed_journal(
            root / "packed-backup-volumes.jsonl", digest=journal_digest
        ):
            candidate = root / volume.path
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or candidate.stat().st_size != volume.compressed_bytes
                or _sha256(candidate) != volume.compressed_sha256
            ):
                raise WorkspaceV2PhysicalDeltaError(
                    "physical delta parent volume is absent or corrupt"
                )
            count += 1
    except WorkspaceV2BackupError as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta parent journal is malformed") from error
    if count != manifest.volume_count or journal_digest.hexdigest() != manifest.journal_sha256:
        raise WorkspaceV2PhysicalDeltaError("physical delta parent journal binding differs")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_physical_delta_index(path: Path) -> PhysicalDeltaIndexTrailer:
    """Validate the index in one pass without retaining its records."""
    target = path.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise WorkspaceV2PhysicalDeltaError("physical delta index is unavailable or unsafe")
    digest = hashlib.sha256()
    count = 0
    files = 0
    bytes_total = 0
    previous_path: str | None = None
    previous_offset = 0
    previous_size = 0
    try:
        with target.open("rb") as stream:
            for raw_line in stream:
                if not raw_line.endswith(b"\n"):
                    raise WorkspaceV2PhysicalDeltaError("physical delta index is incomplete")
                document = json.loads(raw_line)
                if isinstance(document, dict) and document.get("schema_version") == (
                    "workspace-v2-physical-delta-trailer-v1"
                ):
                    trailer = PhysicalDeltaIndexTrailer.model_validate_json(raw_line)
                    if stream.read(1):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta trailer is not terminal"
                        )
                    if (
                        trailer.index_sha256 != digest.hexdigest()
                        or trailer.entry_count != count
                        or trailer.file_count != files
                        or trailer.logical_bytes != bytes_total
                    ):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta trailer does not bind entries"
                        )
                    if previous_path is not None and previous_offset != previous_size:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta entries do not cover a complete file"
                        )
                    return trailer
                entry = PhysicalDeltaIndexEntry.model_validate_json(raw_line)
                if entry.path != previous_path:
                    if previous_path is not None and entry.path == previous_path:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta path reappears after completion"
                        )
                    if previous_path is not None and previous_offset != previous_size:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta entries do not cover a complete file"
                        )
                    previous_path = entry.path
                    previous_offset = 0
                    previous_size = entry.file_size_bytes
                    files += 1
                    bytes_total += entry.file_size_bytes
                elif entry.file_size_bytes != previous_size:
                    raise WorkspaceV2PhysicalDeltaError("physical delta file size binding differs")
                if entry.offset_bytes != previous_offset:
                    raise WorkspaceV2PhysicalDeltaError(
                        "physical delta entries have a gap or overlap"
                    )
                previous_offset += entry.length_bytes
                if previous_offset > entry.file_size_bytes:
                    raise WorkspaceV2PhysicalDeltaError("physical delta entry exceeds its file")
                digest.update(raw_line)
                count += 1
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta index is malformed") from error
    raise WorkspaceV2PhysicalDeltaError("physical delta index is missing its trailer")


def _new_destination(path: Path) -> Path:
    target = path.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink():
        raise WorkspaceV2PhysicalDeltaError("physical delta destination is unsafe")
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise WorkspaceV2PhysicalDeltaError("physical delta destination is unsafe")
    return target


def _partial_index_path(index_path: Path) -> Path:
    candidates = tuple(
        item
        for item in index_path.parent.glob(f".{index_path.name}.*.tmp")
        if item.is_file() and not item.is_symlink()
    )
    if len(candidates) > 1:
        raise WorkspaceV2PhysicalDeltaError("physical delta has ambiguous partial progress")
    return (
        candidates[0]
        if candidates
        else index_path.with_name(f".{index_path.name}.{uuid4().hex}.tmp")
    )


def _manifest_from_trailer(
    trailer: PhysicalDeltaIndexTrailer, *, index_path: str, created_at: datetime
) -> PhysicalDeltaIndexManifest:
    return PhysicalDeltaIndexManifest(
        parent_backup_id=trailer.parent_backup_id,
        source_workspace_id=trailer.source_workspace_id,
        inventory_sha256=trailer.inventory_sha256,
        journal_sha256=trailer.journal_sha256,
        index_path=index_path,
        index_sha256=trailer.index_sha256,
        volume_count=trailer.volume_count,
        file_count=trailer.file_count,
        entry_count=trailer.entry_count,
        logical_bytes=trailer.logical_bytes,
        created_at=created_at,
    )


def _checkpoint_from_trailer(
    trailer: PhysicalDeltaIndexTrailer, *, phase: Literal["writing", "sealed"]
) -> PhysicalDeltaIndexCheckpoint:
    return PhysicalDeltaIndexCheckpoint(
        phase=phase,
        parent_backup_id=trailer.parent_backup_id,
        source_workspace_id=trailer.source_workspace_id,
        inventory_sha256=trailer.inventory_sha256,
        journal_sha256=trailer.journal_sha256,
        entry_count=trailer.entry_count,
        partial_index_sha256=trailer.index_sha256,
    )


def _compact_json(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _write_new_json(path: Path, document: object) -> None:
    if path.exists() or path.is_symlink():
        raise WorkspaceV2PhysicalDeltaError("physical delta manifest path is unavailable")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_compact_json(document))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _replace_json(path: Path, document: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_compact_json(document))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "PhysicalDeltaIndexEntry",
    "PhysicalDeltaIndexCheckpoint",
    "PhysicalDeltaIndexManifest",
    "PhysicalDeltaIndexTrailer",
    "WorkspaceV2PhysicalDeltaError",
    "WorkspaceV2PhysicalDeltaIndexService",
    "verify_physical_delta_index",
]
