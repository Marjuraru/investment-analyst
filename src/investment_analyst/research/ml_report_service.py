"""Bounded, read-only access to one sealed ML baseline evaluation.

The service resolves exactly ``<configured-root>/<run_id>``, validates the
sealed artifact before hydrating its report and caches only a bounded
projection. It never searches for runs, trains a model, opens the operational
workspace, acquires providers or writes domain data.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.research.ml_export import (
    MlExportError,
    peak_rss_bytes,
    read_ml_baseline_artifact,
)
from investment_analyst.research.ml_models import (
    MANIFEST_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    SELECTION_FILE_NAME,
    MlBaselineManifest,
    MlBaselineProtocol,
    MlBaselineReport,
)
from investment_analyst.research.ml_report_models import (
    MlReportArtifact,
    MlReportProtocol,
    MlReportProvenance,
    MlResearchReportView,
)

MAX_MANIFEST_BYTES = 64 * 1024
MAX_PROTOCOL_BYTES = 64 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_SELECTION_BYTES = 1024 * 1024
MAX_PREDICTIONS_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_CACHE_ENTRIES = 8
MAX_CACHE_BYTES = 2 * 1024 * 1024
_PENDING_TIMEOUT_SECONDS = 30.0
_READ_CHUNK_BYTES = 1024 * 1024

_READ_CAPS: dict[str, int] = {
    MANIFEST_FILE_NAME: MAX_MANIFEST_BYTES,
    PROTOCOL_FILE_NAME: MAX_PROTOCOL_BYTES,
    REPORT_FILE_NAME: MAX_REPORT_BYTES,
    SELECTION_FILE_NAME: MAX_SELECTION_BYTES,
    PREDICTIONS_FILE_NAME: MAX_PREDICTIONS_BYTES,
}
_ALLOWED_NAMES = frozenset(_READ_CAPS)
_REQUIRED_NAMES = frozenset({MANIFEST_FILE_NAME, PROTOCOL_FILE_NAME, REPORT_FILE_NAME})


class MlReportError(RuntimeError):
    """Base error for the local ML research report projection."""


class MlReportNotFoundError(MlReportError):
    """The configured root or the requested sealed run does not exist."""


class MlReportConflictError(MlReportError):
    """The artifact is incompatible, corrupt, unsafe or changing."""


@dataclass(frozen=True, slots=True)
class _FileDigest:
    name: str
    sha256: str
    size: int
    mtime_ns: int
    inode: int
    device: int


@dataclass(frozen=True, slots=True)
class _ArtifactSignature:
    files: tuple[_FileDigest, ...]
    manifest_sha256: str
    total_bytes: int

    @property
    def names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.files)


@dataclass(slots=True)
class _CacheEntry:
    signature: _ArtifactSignature
    projection: MlResearchReportView
    bytes: int


@dataclass(slots=True)
class _PendingRead:
    event: threading.Event = field(default_factory=threading.Event)
    result: MlResearchReportView | None = None
    error: Exception | None = None


class MlResearchReportService:
    """Serve one explicitly requested sealed run without treating it as a signal."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("ml results root must be an absolute path")
        self._root = root
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, str], _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._pending: dict[tuple[str, str, str], _PendingRead] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._full_verifications = 0
        self._scans = 0
        self._bytes_read = 0
        self._cold_load_ms: float | None = None
        self._warm_load_ms: float | None = None

    @property
    def root(self) -> Path:
        """Return the configured read-only root; no existence check is performed."""
        return self._root

    def performance(self) -> dict[str, object]:
        """Return bounded observed cost; absent values remain ``None``, never zero."""
        with self._lock:
            return {
                "cache_entries": len(self._cache),
                "cache_bytes": self._cache_bytes,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "full_verifications": self._full_verifications,
                "artifact_scans": self._scans,
                "bytes_read": self._bytes_read,
                "cold_load_ms": self._cold_load_ms,
                "warm_load_ms": self._warm_load_ms,
                "peak_rss_bytes": peak_rss_bytes(),
            }

    def report(self, *, asset_id: str, run_id: UUID) -> MlResearchReportView:
        """Resolve, verify and project exactly one sealed run for one catalog asset."""
        if not asset_id or not asset_id.strip():
            raise ValueError("asset_id is required")
        root = self._resolved_root()
        run_dir = self._run_directory(root, run_id)
        key = (str(root), str(run_id), asset_id)

        with self._lock:
            pending = self._pending.get(key)
            leader = pending is None
            if pending is None:
                pending = _PendingRead()
                self._pending[key] = pending
        if not leader:
            if not pending.event.wait(timeout=_PENDING_TIMEOUT_SECONDS):
                raise MlReportConflictError("timed out waiting for an identical read")
            if pending.result is not None:
                return pending.result
            return self._load_with_retry(key, run_dir, asset_id, run_id)

        try:
            result = self._load_with_retry(key, run_dir, asset_id, run_id)
            pending.result = result
            return result
        except Exception as error:
            pending.error = error
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)
            pending.event.set()

    def _resolved_root(self) -> Path:
        root = self._root
        try:
            info = os.lstat(root)
        except FileNotFoundError as error:
            raise MlReportNotFoundError("ML research results root is not available") from error
        except OSError as error:
            raise MlReportConflictError("ML research results root is not readable") from error
        if stat.S_ISLNK(info.st_mode):
            raise MlReportConflictError("ML research results root must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise MlReportNotFoundError("ML research results root is not a directory")
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise MlReportConflictError("ML research results root is not readable") from error
        if not resolved.is_dir():
            raise MlReportNotFoundError("ML research results root is not a directory")
        return resolved

    def _run_directory(self, root: Path, run_id: UUID) -> Path:
        candidate = root / str(run_id)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError as error:
            raise MlReportNotFoundError("sealed run was not found") from error
        except OSError as error:
            raise MlReportConflictError("sealed run is not readable") from error
        if stat.S_ISLNK(info.st_mode):
            raise MlReportConflictError("sealed run must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise MlReportConflictError("sealed run is not a directory")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise MlReportConflictError("sealed run is not readable") from error
        if resolved.parent != root or not resolved.is_relative_to(root):
            raise MlReportConflictError("sealed run escapes its configured root")
        return resolved

    def _load_with_retry(
        self,
        key: tuple[str, str, str],
        run_dir: Path,
        asset_id: str,
        run_id: UUID,
    ) -> MlResearchReportView:
        for attempt in range(2):
            started = time.perf_counter()
            signature, payloads = self._scan_artifact(
                run_dir,
                include=frozenset({MANIFEST_FILE_NAME, PROTOCOL_FILE_NAME}),
            )
            entry = self._cached_entry(key, signature)
            if entry is not None and self._stat_recheck(run_dir, signature):
                with self._lock:
                    self._cache_hits += 1
                    self._warm_load_ms = (time.perf_counter() - started) * 1000.0
                return entry.projection
            if entry is not None:
                self._discard_cache(key)
            try:
                projection = self._build_projection(
                    run_dir=run_dir,
                    run_id=run_id,
                    asset_id=asset_id,
                    signature=signature,
                    payloads=payloads,
                )
            except MlReportConflictError:
                current, _ = self._scan_artifact(run_dir, include=frozenset())
                if attempt == 0 and current != signature:
                    continue
                raise
            current, _ = self._scan_artifact(run_dir, include=frozenset())
            if current != signature:
                if attempt == 0:
                    continue
                raise MlReportConflictError("artifact changed during read")
            self._store_cache(key, signature, projection)
            with self._lock:
                self._cache_misses += 1
                self._cold_load_ms = (time.perf_counter() - started) * 1000.0
            return projection
        raise MlReportConflictError("artifact changed during read")

    def _directory_files(self, run_dir: Path) -> dict[str, Path]:
        try:
            entries = tuple(run_dir.iterdir())
        except OSError as error:
            raise MlReportConflictError("sealed run is not readable") from error
        if len(entries) > len(_ALLOWED_NAMES):
            raise MlReportConflictError("sealed run contains unexpected entries")
        files: dict[str, Path] = {}
        for entry in entries:
            name = entry.name
            if name not in _ALLOWED_NAMES:
                raise MlReportConflictError("sealed run contains an unexpected descriptor name")
            try:
                info = os.lstat(entry)
            except OSError as error:
                raise MlReportConflictError("sealed file is not readable") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise MlReportConflictError("sealed entry must be a regular file")
            files[name] = entry
        missing = _REQUIRED_NAMES - files.keys()
        if missing:
            raise MlReportConflictError("sealed run is incomplete")
        return files

    def _read_file(
        self, path: Path, *, name: str, retain: bool
    ) -> tuple[bytes | None, _FileDigest]:
        cap = _READ_CAPS[name]
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise MlReportConflictError("sealed file is not readable") from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise MlReportConflictError("sealed entry must be a regular file")
            if before.st_size > cap:
                raise MlReportConflictError("sealed file exceeds its declared read cap")
            hasher = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            while total <= cap:
                chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, cap + 1 - total))
                if not chunk:
                    break
                hasher.update(chunk)
                if retain:
                    chunks.append(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
        except OSError as error:
            raise MlReportConflictError("sealed file is not readable") from error
        finally:
            os.close(descriptor)
        if total > cap:
            raise MlReportConflictError("sealed file exceeds its declared read cap")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MlReportConflictError("sealed file changed during read")
        if total != after.st_size:
            raise MlReportConflictError("sealed file changed during read")
        return (
            b"".join(chunks) if retain else None,
            _FileDigest(
                name=name,
                sha256=hasher.hexdigest(),
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
                inode=after.st_ino,
                device=after.st_dev,
            ),
        )

    def _scan_artifact(
        self, run_dir: Path, *, include: frozenset[str]
    ) -> tuple[_ArtifactSignature, dict[str, bytes]]:
        files = self._directory_files(run_dir)
        digests: list[_FileDigest] = []
        payloads: dict[str, bytes] = {}
        total_bytes = 0
        for name in sorted(files):
            payload, digest = self._read_file(files[name], name=name, retain=name in include)
            total_bytes += digest.size
            if total_bytes > MAX_ARTIFACT_BYTES:
                raise MlReportConflictError("sealed run exceeds the aggregate read cap")
            if payload is not None:
                payloads[name] = payload
            digests.append(digest)
        manifest_digest = next(
            digest.sha256 for digest in digests if digest.name == MANIFEST_FILE_NAME
        )
        signature = _ArtifactSignature(
            files=tuple(digests),
            manifest_sha256=manifest_digest,
            total_bytes=total_bytes,
        )
        with self._lock:
            self._scans += 1
            self._bytes_read += total_bytes
        return signature, payloads

    def _stat_recheck(self, run_dir: Path, signature: _ArtifactSignature) -> bool:
        try:
            files = self._directory_files(run_dir)
        except MlReportConflictError:
            return False
        current: list[tuple[str, int, int, int, int]] = []
        for name in sorted(files):
            try:
                info = os.lstat(files[name])
            except OSError:
                return False
            if not stat.S_ISREG(info.st_mode):
                return False
            if info.st_size > _READ_CAPS[name]:
                return False
            current.append((name, info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev))
        expected = [
            (item.name, item.size, item.mtime_ns, item.inode, item.device)
            for item in signature.files
        ]
        return current == expected

    def _cached_entry(
        self, key: tuple[str, str, str], signature: _ArtifactSignature
    ) -> _CacheEntry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.signature != signature:
                self._cache.pop(key, None)
                self._cache_bytes -= entry.bytes
                return None
            if entry.bytes > MAX_CACHE_BYTES:
                self._cache.pop(key, None)
                self._cache_bytes -= entry.bytes
                return None
            self._cache.move_to_end(key)
            return entry

    def _discard_cache(self, key: tuple[str, str, str]) -> None:
        with self._lock:
            entry = self._cache.pop(key, None)
            if entry is not None:
                self._cache_bytes -= entry.bytes

    def _store_cache(
        self,
        key: tuple[str, str, str],
        signature: _ArtifactSignature,
        projection: MlResearchReportView,
    ) -> None:
        size = len(canonical_analytical_json(projection.model_dump(mode="json")))
        if size > MAX_CACHE_BYTES:
            return
        with self._lock:
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= previous.bytes
            self._cache[key] = _CacheEntry(
                signature=signature,
                projection=projection,
                bytes=size,
            )
            self._cache_bytes += size
            while len(self._cache) > MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= evicted.bytes

    def _build_projection(
        self,
        *,
        run_dir: Path,
        run_id: UUID,
        asset_id: str,
        signature: _ArtifactSignature,
        payloads: dict[str, bytes],
    ) -> MlResearchReportView:
        manifest_bytes = payloads.get(MANIFEST_FILE_NAME)
        protocol_bytes = payloads.get(PROTOCOL_FILE_NAME)
        if manifest_bytes is None or protocol_bytes is None:
            raise MlReportConflictError("sealed run is incomplete")
        manifest = self._parse_manifest(manifest_bytes, run_id)
        if signature.manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest():
            raise MlReportConflictError("sealed manifest changed during read")
        expected_names = {MANIFEST_FILE_NAME, PROTOCOL_FILE_NAME, REPORT_FILE_NAME}
        if manifest.selection is not None:
            expected_names.add(SELECTION_FILE_NAME)
        if manifest.predictions is not None:
            expected_names.add(PREDICTIONS_FILE_NAME)
        if signature.names != expected_names:
            raise MlReportConflictError("sealed descriptors differ from the manifest")
        self._require_descriptor(signature, manifest.protocol.name, manifest.protocol)
        self._require_descriptor(signature, manifest.report.name, manifest.report)
        if manifest.selection is not None:
            self._require_descriptor(signature, manifest.selection.name, manifest.selection)
        if manifest.predictions is not None:
            self._require_descriptor(signature, manifest.predictions.name, manifest.predictions)

        protocol = self._parse_protocol(protocol_bytes)
        if (
            protocol.protocol_id != manifest.protocol_id
            or protocol.dataset_id != manifest.dataset_id
            or protocol.dataset_content_sha256 != manifest.dataset_content_sha256
            or manifest.protocol.sha256 != hashlib.sha256(protocol_bytes).hexdigest()
        ):
            raise MlReportConflictError("sealed protocol disagrees with the manifest")
        if protocol.asset_id != asset_id:
            raise MlReportConflictError("sealed artifact belongs to a different asset")

        with self._lock:
            self._full_verifications += 1
        try:
            verified_manifest, report = read_ml_baseline_artifact(run_dir)
        except MlExportError as error:
            raise MlReportConflictError("sealed artifact is incompatible or corrupt") from error
        if verified_manifest != manifest or report.run_id != run_id:
            raise MlReportConflictError("sealed artifact changed during verification")

        return self._projection(
            manifest=manifest,
            report=report,
            protocol=protocol,
            signature=signature,
        )

    @staticmethod
    def _parse_manifest(payload: bytes, run_id: UUID) -> MlBaselineManifest:
        try:
            manifest = MlBaselineManifest.model_validate_json(payload)
        except ValueError as error:
            raise MlReportConflictError("sealed manifest is malformed") from error
        if canonical_analytical_json(manifest.model_dump(mode="json")) != payload:
            raise MlReportConflictError("sealed manifest is not in canonical encoding")
        if manifest.run_id != run_id:
            raise MlReportConflictError("sealed manifest belongs to a different run")
        return manifest

    @staticmethod
    def _parse_protocol(payload: bytes) -> MlBaselineProtocol:
        try:
            protocol = MlBaselineProtocol.model_validate_json(payload)
        except ValueError as error:
            raise MlReportConflictError("sealed protocol is malformed") from error
        if canonical_analytical_json(protocol.model_dump(mode="json")) != payload:
            raise MlReportConflictError("sealed protocol is not in canonical encoding")
        return protocol

    @staticmethod
    def _require_descriptor(signature: _ArtifactSignature, name: str, descriptor: object) -> None:
        digest = next((item for item in signature.files if item.name == name), None)
        if digest is None:
            raise MlReportConflictError("sealed descriptor is absent")
        expected_size = getattr(descriptor, "bytes", None)
        expected_sha256 = getattr(descriptor, "sha256", None)
        if digest.size != expected_size or digest.sha256 != expected_sha256:
            raise MlReportConflictError("sealed descriptor differs from its manifest")

    @staticmethod
    def _projection(
        *,
        manifest: MlBaselineManifest,
        report: MlBaselineReport,
        protocol: MlBaselineProtocol,
        signature: _ArtifactSignature,
    ) -> MlResearchReportView:
        return MlResearchReportView(
            provenance=MlReportProvenance(
                asset_id=protocol.asset_id,
                dataset_id=manifest.dataset_id,
                dataset_content_sha256=manifest.dataset_content_sha256,
                dataset_manifest_sha256=manifest.dataset_manifest_sha256,
                dataset_rows_sha256=manifest.dataset_rows_sha256,
            ),
            artifact=MlReportArtifact(
                run_id=manifest.run_id,
                status=manifest.status,
                created_at=manifest.created_at,
                protocol_sha256=manifest.protocol.sha256,
                report_sha256=manifest.report.sha256,
                manifest_sha256=signature.manifest_sha256,
                selection_sha256=(
                    manifest.selection.sha256 if manifest.selection is not None else None
                ),
                predictions_sha256=(
                    manifest.predictions.sha256 if manifest.predictions is not None else None
                ),
                code_material_sha256=manifest.code_material_sha256,
                artifact_bytes=signature.total_bytes,
                prediction_rows=report.prediction_rows,
                environment=manifest.environment,
            ),
            protocol=MlReportProtocol.from_protocol(protocol),
            development=report.development,
            holdout=report.holdout,
            insufficiency_reason=report.insufficiency_reason,
            timings=report.timings,
            peak_rss_bytes=report.peak_rss_bytes,
        )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_CACHE_BYTES",
    "MAX_CACHE_ENTRIES",
    "MAX_MANIFEST_BYTES",
    "MAX_PREDICTIONS_BYTES",
    "MAX_PROTOCOL_BYTES",
    "MAX_REPORT_BYTES",
    "MAX_SELECTION_BYTES",
    "MlReportConflictError",
    "MlReportError",
    "MlReportNotFoundError",
    "MlResearchReportService",
]
