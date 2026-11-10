"""Exclusive, bounded and atomic JSON storage for experimental shadow runs.

The store is deliberately stricter than a generic directory writer: one non-blocking lock,
a fixed inventory/budget, atomic publication, idempotent reuse and fail-closed verification.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.research.inference_models import ModelInferenceResult
from investment_analyst.research.shadow_models import (
    MAX_SHADOW_MANIFEST_BYTES,
    MAX_SHADOW_RESULT_BYTES,
    MAX_SHADOW_RUNS,
    MAX_SHADOW_STORE_BYTES,
    ShadowRunManifest,
)

_LOCK_NAME = ".shadow.lock"
_RUN_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RESULT_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$")
_TEMP_PREFIX = ".staging-"
_MANIFEST_NAME = "manifest.json"
_MAX_ROOT_ENTRIES = MAX_SHADOW_RUNS + 1


class ShadowStoreError(RuntimeError):
    """Base error for the bounded shadow store."""


class ShadowStoreConflictError(ShadowStoreError):
    """The requested or persisted shadow identity has different content."""


class ShadowStoreBudgetError(ShadowStoreError):
    """The shadow store inventory or byte budget cannot admit the operation."""


class ShadowStoreLockError(ShadowStoreError):
    """The exclusive shadow writer lock is present or cannot be acquired."""


@dataclass(frozen=True, slots=True)
class ShadowPublishReceipt:
    created: bool
    run_id: UUID
    manifest_path: Path
    manifest_sha256: str


class ShadowStore:
    """Own one exclusive shadow root with bounded JSON artifacts."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_root: Path,
        protected_roots: tuple[Path, ...] = (),
    ) -> None:
        self._root = self._validated_root(
            root,
            workspace_root=workspace_root,
            protected_roots=protected_roots,
        )

    @staticmethod
    def _validated_root(
        root: Path,
        *,
        workspace_root: Path,
        protected_roots: tuple[Path, ...],
    ) -> Path:
        expanded = root.expanduser()
        if not expanded.is_absolute() or expanded == Path("/"):
            raise ShadowStoreError("shadow root must be one dedicated absolute directory")
        if ".." in expanded.parts:
            raise ShadowStoreError("shadow root must not contain traversal")
        normalized = Path(os.path.abspath(expanded))
        if normalized.resolve(strict=False) != normalized:
            raise ShadowStoreError("shadow root must not traverse symbolic links")
        workspace = Path(os.path.abspath(workspace_root.expanduser()))
        for protected in (*protected_roots, workspace):
            candidate = Path(os.path.abspath(protected.expanduser()))
            if (
                normalized == candidate
                or normalized in candidate.parents
                or candidate in normalized.parents
            ):
                raise ShadowStoreError(
                    "shadow root must be outside the workspace, repository and release roots"
                )
        if normalized.exists() or normalized.is_symlink():
            information = normalized.lstat()
            if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
                raise ShadowStoreError("shadow root must be a real directory")
        return normalized

    @property
    def root(self) -> Path:
        return self._root

    def publish(
        self,
        manifest: ShadowRunManifest,
        results: Mapping[UUID, bytes],
    ) -> ShadowPublishReceipt:
        """Publish one complete run atomically or return the verified existing run."""
        manifest_bytes = canonical_analytical_json(manifest.model_dump(mode="json"))
        if len(manifest_bytes) > MAX_SHADOW_MANIFEST_BYTES:
            raise ShadowStoreBudgetError(
                f"shadow manifest exceeds {MAX_SHADOW_MANIFEST_BYTES} bytes"
            )
        self._verify_results(manifest, results)
        self._ensure_root()
        lock_fd, lock_path = self._acquire_lock()
        try:
            inventory = self._inventory()
            final = self._root / str(manifest.run_id)
            if final.exists() or final.is_symlink():
                existing = self._verify_run(manifest.run_id, manifest_bytes, manifest)
                return ShadowPublishReceipt(
                    created=False,
                    run_id=manifest.run_id,
                    manifest_path=existing,
                    manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                )
            if inventory.temporary_entries:
                raise ShadowStoreConflictError(
                    "shadow root contains an incomplete staging directory"
                )
            if inventory.run_count + 1 > MAX_SHADOW_RUNS:
                raise ShadowStoreBudgetError("shadow run budget is exhausted")
            new_bytes = len(manifest_bytes) + sum(len(value) for value in results.values())
            if inventory.bytes_used + new_bytes > MAX_SHADOW_STORE_BYTES:
                raise ShadowStoreBudgetError("shadow byte budget is exhausted")
            self._write_run(manifest, manifest_bytes, results)
            return ShadowPublishReceipt(
                created=True,
                run_id=manifest.run_id,
                manifest_path=final / _MANIFEST_NAME,
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        finally:
            self._release_lock(lock_fd, lock_path)

    def _verify_results(self, manifest: ShadowRunManifest, results: Mapping[UUID, bytes]) -> None:
        expected = {item.inference_id for item in manifest.assets}
        if set(results) != expected:
            raise ShadowStoreConflictError(
                "shadow result files do not match the manifest identities"
            )
        by_id = {item.inference_id: item for item in manifest.assets}
        for inference_id, data in results.items():
            if len(data) > MAX_SHADOW_RESULT_BYTES:
                raise ShadowStoreBudgetError(
                    f"shadow result exceeds {MAX_SHADOW_RESULT_BYTES} bytes"
                )
            try:
                result = ModelInferenceResult.model_validate_json(data)
            except ValueError as error:
                raise ShadowStoreConflictError("shadow result is not a valid inference") from error
            if canonical_analytical_json(result.to_json_dict()) != data:
                raise ShadowStoreConflictError("shadow result is not canonical JSON")
            asset = by_id[inference_id]
            if (
                result.inference_id != inference_id
                or hashlib.sha256(data).hexdigest() != asset.inference_sha256
                or result.asset.asset_id != asset.asset_id
                or result.source_id != asset.source_id
                or result.workspace_uuid != manifest.workspace_uuid
                or result.artifact_id != manifest.artifact_id
                or result.artifact_sha256 != manifest.artifact_sha256
                or result.decision_at != manifest.decision_at
                or result.known_at != manifest.known_at
                or result.status is not asset.status
                or result.test_probability != asset.test_probability
            ):
                raise ShadowStoreConflictError(
                    "shadow result contradicts the manifest or its inference identity"
                )

    def _ensure_root(self) -> None:
        if self._root.exists():
            information = self._root.lstat()
            if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
                raise ShadowStoreError("shadow root must be a real directory")
            return
        try:
            self._root.mkdir(parents=True, mode=0o700)
        except FileExistsError as error:
            raise ShadowStoreError("shadow root cannot be created safely") from error

    def _acquire_lock(self) -> tuple[int, Path]:
        lock_path = self._root / _LOCK_NAME
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise ShadowStoreLockError(
                "shadow writer lock exists; an owner must resolve it explicitly"
            ) from error
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except OSError:
            os.close(descriptor)
            with suppress(OSError):
                lock_path.unlink()
            raise
        return descriptor, lock_path

    @staticmethod
    def _release_lock(descriptor: int, path: Path) -> None:
        try:
            os.close(descriptor)
        finally:
            with suppress(FileNotFoundError):
                path.unlink()

    def _inventory(self) -> _Inventory:
        entries = list(os.scandir(self._root))
        if len(entries) > _MAX_ROOT_ENTRIES:
            raise ShadowStoreBudgetError("shadow root exceeds the bounded entry inventory")
        run_count = 0
        temporary_entries = 0
        total = 0
        for entry in entries:
            if entry.name == _LOCK_NAME:
                information = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(information.st_mode):
                    raise ShadowStoreError("shadow lock must be a regular file")
                total += information.st_size
                continue
            if entry.name.startswith(_TEMP_PREFIX):
                if not entry.is_dir(follow_symlinks=False):
                    raise ShadowStoreError("shadow staging entry must be a real directory")
                temporary_entries += 1
                total += self._temporary_size(Path(entry.path))
                continue
            if _RUN_NAME.fullmatch(entry.name) is None:
                raise ShadowStoreError("shadow root mixes another study or file")
            if not entry.is_dir(follow_symlinks=False):
                raise ShadowStoreError("shadow run entry must be a real directory")
            run_count += 1
            total += self._run_size(Path(entry.path))
        return _Inventory(
            run_count=run_count,
            temporary_entries=temporary_entries,
            bytes_used=total,
        )

    @staticmethod
    def _temporary_size(path: Path) -> int:
        total = 0
        entries = list(os.scandir(path))
        if len(entries) > 3:
            raise ShadowStoreError("shadow staging directory has foreign entries")
        for entry in entries:
            if entry.name != _MANIFEST_NAME and _RESULT_NAME.fullmatch(entry.name) is None:
                raise ShadowStoreError("shadow staging directory has foreign entries")
            information = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(information.st_mode):
                raise ShadowStoreError("shadow staging file must be regular")
            total += information.st_size
        return total

    @staticmethod
    def _run_size(path: Path) -> int:
        total = 0
        names: set[str] = set()
        entries = list(os.scandir(path))
        if len(entries) > 3:
            raise ShadowStoreConflictError("shadow run directory has foreign entries")
        for entry in entries:
            information = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(information.st_mode):
                raise ShadowStoreConflictError("shadow run file must be regular")
            if entry.name == _MANIFEST_NAME or _RESULT_NAME.fullmatch(entry.name) is not None:
                names.add(entry.name)
            else:
                raise ShadowStoreConflictError("shadow run directory has foreign entries")
            total += information.st_size
        if _MANIFEST_NAME not in names:
            raise ShadowStoreConflictError("shadow run directory lacks its manifest")
        return total

    def _verify_run(
        self,
        run_id: UUID,
        expected_manifest: bytes,
        expected: ShadowRunManifest,
    ) -> Path:
        directory = self._root / str(run_id)
        manifest_path = directory / _MANIFEST_NAME
        manifest_bytes = _read_regular(manifest_path, MAX_SHADOW_MANIFEST_BYTES)
        if manifest_bytes != expected_manifest:
            raise ShadowStoreConflictError(
                "shadow run identity already has different manifest content"
            )
        try:
            manifest = ShadowRunManifest.model_validate_json(manifest_bytes)
        except ValueError as error:
            raise ShadowStoreConflictError("shadow manifest is not valid") from error
        if manifest.run_id != run_id or manifest != expected:
            raise ShadowStoreConflictError(
                "shadow run identity already has different manifest content"
            )
        expected_names = {_MANIFEST_NAME} | {item.result_file for item in manifest.assets}
        observed_names: set[str] = set()
        for entry in os.scandir(directory):
            if not entry.is_file(follow_symlinks=False):
                raise ShadowStoreConflictError("shadow run directory has a non-regular entry")
            observed_names.add(entry.name)
        if observed_names != expected_names:
            raise ShadowStoreConflictError("shadow run directory has incomplete or foreign files")
        for asset in manifest.assets:
            data = _read_regular(directory / asset.result_file, MAX_SHADOW_RESULT_BYTES)
            if hashlib.sha256(data).hexdigest() != asset.inference_sha256:
                raise ShadowStoreConflictError("shadow result checksum differs from its manifest")
            try:
                result = ModelInferenceResult.model_validate_json(data)
            except ValueError as error:
                raise ShadowStoreConflictError("shadow result is not valid") from error
            if canonical_analytical_json(result.to_json_dict()) != data:
                raise ShadowStoreConflictError("shadow result is not canonical JSON")
            if (
                result.inference_id != asset.inference_id
                or result.asset.asset_id != asset.asset_id
                or result.source_id != asset.source_id
                or result.workspace_uuid != manifest.workspace_uuid
                or result.status is not asset.status
                or result.test_probability != asset.test_probability
            ):
                raise ShadowStoreConflictError("shadow result contradicts its manifest")
        return manifest_path

    def _write_run(
        self,
        manifest: ShadowRunManifest,
        manifest_bytes: bytes,
        results: Mapping[UUID, bytes],
    ) -> None:
        final = self._root / str(manifest.run_id)
        temporary = self._root / (
            f"{_TEMP_PREFIX}{manifest.run_id}-{os.getpid()}-{secrets.token_hex(8)}"
        )
        os.mkdir(temporary, 0o700)
        try:
            _write_regular(temporary / _MANIFEST_NAME, manifest_bytes)
            for asset in manifest.assets:
                _write_regular(temporary / asset.result_file, results[asset.inference_id])
            _fsync_directory(temporary)
            os.rename(temporary, final)
            _fsync_directory(self._root)
        finally:
            if temporary.exists():
                for entry in os.scandir(temporary):
                    if entry.is_file(follow_symlinks=False):
                        Path(entry.path).unlink()
                temporary.rmdir()


@dataclass(frozen=True, slots=True)
class _Inventory:
    run_count: int
    temporary_entries: int
    bytes_used: int


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        information = path.lstat()
    except OSError as error:
        raise ShadowStoreError("shadow artifact is not readable") from error
    if not stat.S_ISREG(information.st_mode) or information.st_size > limit:
        raise ShadowStoreError("shadow artifact must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ShadowStoreError("shadow artifact is not readable") from error
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ShadowStoreError("shadow artifact exceeds its byte budget")
    return data


def _write_regular(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        with suppress(OSError):
            path.unlink()
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ShadowPublishReceipt",
    "ShadowStore",
    "ShadowStoreBudgetError",
    "ShadowStoreConflictError",
    "ShadowStoreError",
    "ShadowStoreLockError",
]
