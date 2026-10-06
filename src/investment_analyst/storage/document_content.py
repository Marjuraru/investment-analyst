"""Immutable content-addressed storage for primary source documents."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.paths import StoragePaths

_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class DocumentContentError(StorageError):
    """Raised when immutable document bytes cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class DocumentContentReceipt:
    """The immutable identity and write result of one document blob."""

    sha256: str
    size_bytes: int
    created: bool


class DocumentContentStore:
    """Store exact bytes once under a SHA-256 path without public mutation APIs."""

    def __init__(self, paths: StoragePaths, *, read_only: bool = False) -> None:
        try:
            self._boundary_root = paths.documents_boundary_root
        except ValueError as error:
            raise DocumentContentError("document content storage layout is invalid") from error
        self._documents_root = paths.documents_dir
        self._root = self._documents_root / "sha256"
        self._read_only = read_only

    def put(self, content: bytes) -> DocumentContentReceipt:
        """Atomically persist one bounded blob, reusing only byte-identical content."""
        if self._read_only:
            raise DocumentContentError("document content cannot be saved through read-only storage")
        if not isinstance(content, bytes) or not content:
            raise DocumentContentError("document content must be non-empty bytes")
        if len(content) > _MAX_DOCUMENT_BYTES:
            raise DocumentContentError("document content exceeds the configured size limit")
        checksum = hashlib.sha256(content).hexdigest()
        target = self._path(checksum)
        self._assert_safe_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_path(target)
        if target.exists():
            self._verify_path(target, checksum, expected=content)
            return DocumentContentReceipt(checksum, len(content), False)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                self._verify_path(target, checksum, expected=content)
                return DocumentContentReceipt(checksum, len(content), False)
            os.replace(temporary, target)
            self._verify_path(target, checksum, expected=content)
            return DocumentContentReceipt(checksum, len(content), True)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, checksum: str) -> bytes:
        """Read explicitly requested content only after hash verification."""
        target = self._path(checksum)
        self._assert_safe_path(target)
        self._verify_path(target, checksum)
        return target.read_bytes()

    def verify(self, checksum: str, *, size_bytes: int | None = None) -> None:
        """Verify an existing blob without returning or materializing its bytes."""
        target = self._path(checksum)
        self._assert_safe_path(target)
        self._verify_path(target, checksum, size_bytes=size_bytes)

    def _path(self, checksum: str) -> Path:
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise DocumentContentError("document content checksum is invalid")
        target = self._root / checksum[:2] / checksum[2:4] / checksum
        try:
            target.relative_to(self._root)
        except ValueError as error:
            raise DocumentContentError(
                "document content path escapes the configured store"
            ) from error
        return target

    def _assert_safe_path(self, target: Path) -> None:
        """Reject ancestor symlinks before any document-store filesystem operation."""
        try:
            documents_relative = self._documents_root.relative_to(self._boundary_root)
            target_relative = target.relative_to(self._boundary_root)
            target.relative_to(self._documents_root)
        except ValueError as error:
            raise DocumentContentError(
                "document content path escapes the configured store"
            ) from error
        if (
            not self._boundary_root.is_absolute()
            or any(part == ".." for part in documents_relative.parts)
            or any(part == ".." for part in target_relative.parts)
        ):
            raise DocumentContentError("document content path escapes the configured store")

        candidates = [self._boundary_root]
        candidate = self._boundary_root
        for part in target_relative.parts:
            candidate = candidate / part
            candidates.append(candidate)
        for candidate in candidates:
            if candidate.is_symlink():
                raise DocumentContentError("document content store cannot use symbolic links")

        boundary_root = self._boundary_root.resolve(strict=False)
        if boundary_root != self._boundary_root:
            raise DocumentContentError("document content store cannot use symbolic links")
        documents_root = self._documents_root.resolve(strict=False)
        target_root = target.resolve(strict=False)
        if not documents_root.is_relative_to(boundary_root) or not target_root.is_relative_to(
            documents_root
        ):
            raise DocumentContentError("document content path escapes the configured store")

    def _verify_path(
        self,
        path: Path,
        checksum: str,
        *,
        expected: bytes | None = None,
        size_bytes: int | None = None,
    ) -> None:
        self._assert_safe_path(path)
        if path.is_symlink() or not path.is_file():
            raise DocumentContentError("document content blob is missing or not a regular file")
        if size_bytes is not None and path.stat().st_size != size_bytes:
            raise DocumentContentError("document content size does not match its revision")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if expected is not None else None
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
        if digest.hexdigest() != checksum:
            raise DocumentContentError("document content checksum mismatch")
        if chunks is not None and b"".join(chunks) != expected:
            raise DocumentContentError("document content hash collision or conflicting bytes")
