"""Isolated streaming archive for unique historical bytes.

``continuity-archive-v1`` is not a workspace backup and cannot be opened as a
workspace. It stores safe relative files as ordered SHA-256 chunks and publishes
a complete manifest only after independent local readback of every chunk and
streaming reconstruction of every file.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

CONTINUITY_ARCHIVE_SCHEMA = "continuity-archive-v1"
CONTINUITY_ARCHIVE_MANIFEST_NAME = "continuity-archive-manifest.json"
_CONTINUITY_ARCHIVE_JOURNAL_NAME = "continuity-archive-journal.jsonl"
_MAX_CHUNK_BYTES = 64 * 1024 * 1024
_MAX_BUFFER_BYTES = 128 * 1024 * 1024


class ContinuityArchiveError(RuntimeError):
    """Raised when an archive cannot be created, verified, or restored safely."""


class ContinuityArchiveChunk(ContractModel):
    """One ordered content-addressed chunk of a file."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset_bytes: int = Field(ge=0)
    length_bytes: int = Field(gt=0, le=_MAX_CHUNK_BYTES)


class ContinuityArchiveFile(ContractModel):
    """One safe relative file reconstructed from ordered chunks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: NonEmptyStr
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunks: tuple[ContinuityArchiveChunk, ...]

    @model_validator(mode="after")
    def _validate_file(self) -> ContinuityArchiveFile:
        _safe_relative(self.path)
        offset = 0
        for chunk in self.chunks:
            if chunk.offset_bytes != offset:
                raise ValueError("archive chunks must be contiguous and ordered")
            offset += chunk.length_bytes
        if offset != self.size_bytes:
            raise ValueError("archive chunk lengths must sum to the file size")
        if self.size_bytes == 0 and self.chunks:
            raise ValueError("empty archive files cannot contain chunks")
        return self


class ContinuityArchiveManifest(ContractModel):
    """Complete archive authority published only after full verification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["continuity-archive-v1"] = CONTINUITY_ARCHIVE_SCHEMA
    archive_id: UUID
    source_workspace_id: UUID
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: UTCDateTime
    code_sha: NonEmptyStr
    files: tuple[ContinuityArchiveFile, ...]
    total_bytes: int = Field(ge=0)
    complete: Literal[True] = True
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_manifest(self) -> ContinuityArchiveManifest:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("archive files must be sorted by safe relative path")
        if len(paths) != len(set(paths)):
            raise ValueError("archive files must be unique")
        if sum(item.size_bytes for item in self.files) != self.total_bytes:
            raise ValueError("archive total bytes must match its files")
        if self.digest != _manifest_digest(self):
            raise ValueError("archive digest does not match its content")
        if self.archive_id != _archive_id(self.digest):
            raise ValueError("archive id does not match its digest")
        return self


def _manifest_digest(manifest: ContinuityArchiveManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"digest", "archive_id"})
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _archive_id(digest: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{CONTINUITY_ARCHIVE_SCHEMA}:{digest}")


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise ContinuityArchiveError("archive path must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ContinuityArchiveError("archive path must not escape its root")
    return path


def _sha256_file(path: Path, *, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            data = stream.read(chunk_bytes)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_chunk(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if (
            path.is_symlink()
            or not path.is_file()
            or _sha256_file(path) != hashlib.sha256(data).hexdigest()
        ):
            raise ContinuityArchiveError("archive chunk already exists with other bytes")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class ContinuityArchiveWriter:
    """Stream unique files into a new local archive without a full DB copy."""

    def __init__(
        self,
        root: Path,
        *,
        source_workspace_id: UUID,
        source_fingerprint: str,
        code_sha: str,
        created_at: datetime | None = None,
        chunk_bytes: int = _MAX_CHUNK_BYTES,
        buffer_bytes: int = _MAX_BUFFER_BYTES,
    ) -> None:
        if not 1 <= chunk_bytes <= _MAX_CHUNK_BYTES:
            raise ContinuityArchiveError("archive chunk size is out of bounds")
        if not 1024 <= buffer_bytes <= _MAX_BUFFER_BYTES:
            raise ContinuityArchiveError("archive buffer size is out of bounds")
        if len(source_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in source_fingerprint
        ):
            raise ContinuityArchiveError("archive source fingerprint is invalid")
        self._root = root.expanduser().resolve()
        if self._root.exists() and any(self._root.iterdir()):
            raise ContinuityArchiveError("archive root must be new or empty")
        self._root.mkdir(parents=True, exist_ok=True)
        self._chunks_root = self._root / "chunks"
        self._chunks_root.mkdir(parents=True, exist_ok=True)
        self._journal = self._root / _CONTINUITY_ARCHIVE_JOURNAL_NAME
        self._chunk_bytes = chunk_bytes
        self._buffer_bytes = buffer_bytes
        self._source_workspace_id = source_workspace_id
        self._source_fingerprint = source_fingerprint
        self._code_sha = code_sha
        self._created_at = created_at or datetime.now(UTC)
        self._files: dict[str, ContinuityArchiveFile] = {}
        self._finalized = False

    @property
    def root(self) -> Path:
        """Return the immutable archive root."""
        return self._root

    def add_file(self, source: Path, relative_path: str) -> ContinuityArchiveFile:
        """Hash and store one regular file in ordered content-addressed chunks."""
        if self._finalized:
            raise ContinuityArchiveError("archive is already finalized")
        relative = _safe_relative(relative_path)
        canonical = str(relative)
        candidate = source.expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise ContinuityArchiveError("archive source must be a regular file")
        before = candidate.stat()
        if before.st_size > 0 and not before.st_mode & 0o400:
            raise ContinuityArchiveError("archive source is not readable")
        hash_digest = hashlib.sha256()
        chunks: list[ContinuityArchiveChunk] = []
        offset = 0
        with candidate.open("rb") as stream:
            while True:
                data = stream.read(self._chunk_bytes)
                if not data:
                    break
                chunk_sha = hashlib.sha256(data).hexdigest()
                _write_chunk(self._chunks_root / chunk_sha, data)
                chunks.append(
                    ContinuityArchiveChunk(
                        sha256=chunk_sha,
                        offset_bytes=offset,
                        length_bytes=len(data),
                    )
                )
                hash_digest.update(data)
                offset += len(data)
                if len(data) > self._buffer_bytes:
                    raise ContinuityArchiveError("archive chunk exceeded the buffer budget")
                with self._journal.open("a", encoding="utf-8") as journal:
                    journal.write(
                        json.dumps(
                            {
                                "path": canonical,
                                "offset_bytes": offset - len(data),
                                "length_bytes": len(data),
                                "sha256": chunk_sha,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n"
                    )
        after = candidate.stat()
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ino != before.st_ino
        ):
            raise ContinuityArchiveError("archive source changed while it was read")
        file = ContinuityArchiveFile(
            path=canonical,
            size_bytes=offset,
            sha256=hash_digest.hexdigest(),
            chunks=tuple(chunks),
        )
        existing = self._files.get(canonical)
        if existing is not None and existing != file:
            raise ContinuityArchiveError("archive relative path maps to conflicting content")
        self._files[canonical] = file
        return file

    def finalize(self) -> ContinuityArchiveManifest:
        """Verify every chunk and file by readback, then publish the manifest."""
        if self._finalized:
            raise ContinuityArchiveError("archive is already finalized")
        if not self._files:
            raise ContinuityArchiveError("archive must contain at least one file")
        files = tuple(self._files[key] for key in sorted(self._files))
        provisional = ContinuityArchiveManifest.model_construct(
            schema_version=CONTINUITY_ARCHIVE_SCHEMA,
            archive_id=UUID(int=0),
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            created_at=self._created_at,
            code_sha=self._code_sha,
            files=files,
            total_bytes=sum(item.size_bytes for item in files),
            complete=True,
            digest="0" * 64,
        )
        digest = _manifest_digest(provisional)
        manifest = ContinuityArchiveManifest(
            schema_version=CONTINUITY_ARCHIVE_SCHEMA,
            archive_id=_archive_id(digest),
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            created_at=self._created_at,
            code_sha=self._code_sha,
            files=files,
            total_bytes=provisional.total_bytes,
            complete=True,
            digest=digest,
        )
        verify_continuity_archive(self._root, manifest)
        _write_manifest(self._root, manifest)
        self._finalized = True
        return manifest


def _write_manifest(root: Path, manifest: ContinuityArchiveManifest) -> None:
    destination = root / CONTINUITY_ARCHIVE_MANIFEST_NAME
    encoded = json.dumps(
        manifest.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise ContinuityArchiveError("archive manifest already exists with other content")
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def load_continuity_archive_manifest(root: Path) -> ContinuityArchiveManifest:
    """Load one complete manifest and revalidate its digest."""
    path = root.expanduser().resolve() / CONTINUITY_ARCHIVE_MANIFEST_NAME
    try:
        return ContinuityArchiveManifest.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ContinuityArchiveError("archive manifest is unreadable or invalid") from error


def _iter_file_bytes(
    root: Path,
    file: ContinuityArchiveFile,
) -> Iterable[bytes]:
    offset = 0
    for chunk in file.chunks:
        if chunk.offset_bytes != offset:
            raise ContinuityArchiveError("archive chunk order is invalid")
        path = root / "chunks" / chunk.sha256
        if path.is_symlink() or not path.is_file():
            raise ContinuityArchiveError("archive chunk is missing or unsafe")
        data = path.read_bytes()
        if len(data) != chunk.length_bytes:
            raise ContinuityArchiveError("archive chunk length does not match its manifest")
        if hashlib.sha256(data).hexdigest() != chunk.sha256:
            raise ContinuityArchiveError("archive chunk digest does not match its manifest")
        offset += len(data)
        yield data
    if offset != file.size_bytes:
        raise ContinuityArchiveError("archive file length does not match its manifest")


def verify_continuity_archive(root: Path, manifest: ContinuityArchiveManifest) -> None:
    """Reconstruct every file by streaming and compare length and SHA-256."""
    archive_root = root.expanduser().resolve()
    for file in manifest.files:
        digest = hashlib.sha256()
        size = 0
        for data in _iter_file_bytes(archive_root, file):
            digest.update(data)
            size += len(data)
        if size != file.size_bytes or digest.hexdigest() != file.sha256:
            raise ContinuityArchiveError("archive file reconstruction failed verification")


def restore_continuity_archive(
    root: Path,
    manifest: ContinuityArchiveManifest,
    destination: Path,
    *,
    paths: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Stream selected files into a new destination without materializing the archive."""
    target_root = destination.expanduser().resolve()
    if target_root.exists() and any(target_root.iterdir()):
        raise ContinuityArchiveError("archive restore destination must be new or empty")
    target_root.mkdir(parents=True, exist_ok=True)
    selected = set(paths) if paths is not None else None
    restored: list[str] = []
    for file in manifest.files:
        if selected is not None and file.path not in selected:
            continue
        relative = _safe_relative(file.path)
        target = target_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        if target_root != resolved_parent and target_root not in resolved_parent.parents:
            raise ContinuityArchiveError("archive restore path escapes its destination")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as stream:
                for data in _iter_file_bytes(root.expanduser().resolve(), file):
                    stream.write(data)
                    digest.update(data)
                    size += len(data)
                stream.flush()
                os.fsync(stream.fileno())
            if size != file.size_bytes or digest.hexdigest() != file.sha256:
                raise ContinuityArchiveError("archive restore verification failed")
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        restored.append(file.path)
    if selected is not None:
        missing = selected - set(restored)
        if missing:
            raise ContinuityArchiveError("archive restore selection is incomplete")
    return tuple(restored)


def archive_remote_objects(
    manifest: ContinuityArchiveManifest,
    *,
    prefix: str = "continuity-47",
) -> tuple[str, ...]:
    """Return every immutable remote object key for one complete archive."""
    safe_prefix = _safe_relative(prefix)
    chunks = sorted({chunk.sha256 for file in manifest.files for chunk in file.chunks})
    return (
        *(f"{safe_prefix}/chunks/{sha}" for sha in chunks),
        f"{safe_prefix}/manifests/{manifest.archive_id}.json",
    )


def upload_continuity_archive(
    transport,
    root: Path,
    manifest: ContinuityArchiveManifest,
    *,
    prefix: str = "continuity-47",
) -> tuple[str, ...]:
    """Upload chunks and the final manifest without replacing prior archives."""
    safe_prefix = _safe_relative(prefix)
    archive_root = root.expanduser().resolve()
    chunk_hashes = sorted({chunk.sha256 for file in manifest.files for chunk in file.chunks})
    for sha in chunk_hashes:
        transport.copy_immutable_object(
            archive_root / "chunks" / sha, f"{safe_prefix}/chunks/{sha}"
        )
    transport.copy_immutable_object(
        archive_root / CONTINUITY_ARCHIVE_MANIFEST_NAME,
        f"{safe_prefix}/manifests/{manifest.archive_id}.json",
    )
    return archive_remote_objects(manifest, prefix=str(safe_prefix))


def verify_remote_continuity_archive(
    transport,
    manifest: ContinuityArchiveManifest,
    staging: Path,
    *,
    prefix: str = "continuity-47",
) -> None:
    """Fetch every remote chunk independently and verify its digest and manifest binding."""
    safe_prefix = _safe_relative(prefix)
    target = staging.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise ContinuityArchiveError("remote verification staging must be new or empty")
    target.mkdir(parents=True, exist_ok=True)
    for sha in sorted({chunk.sha256 for file in manifest.files for chunk in file.chunks}):
        destination = target / "chunks" / sha
        transport.fetch_sealed_object(f"{safe_prefix}/chunks/{sha}", destination)
        if _sha256_file(destination) != sha:
            raise ContinuityArchiveError("remote archive chunk verification failed")
    verify_continuity_archive(target, manifest)


__all__ = [
    "CONTINUITY_ARCHIVE_MANIFEST_NAME",
    "CONTINUITY_ARCHIVE_SCHEMA",
    "ContinuityArchiveChunk",
    "ContinuityArchiveError",
    "ContinuityArchiveFile",
    "ContinuityArchiveManifest",
    "ContinuityArchiveWriter",
    "archive_remote_objects",
    "load_continuity_archive_manifest",
    "restore_continuity_archive",
    "upload_continuity_archive",
    "verify_continuity_archive",
    "verify_remote_continuity_archive",
]
