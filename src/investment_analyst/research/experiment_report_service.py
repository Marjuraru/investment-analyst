"""Bounded discovery and verification of sealed research experiments.

The service enumerates only direct children of the three configured roots, reads
just the manifest and protocol headers of the candidates on the requested page,
filters by catalog asset before hydrating anything else and delegates every
baseline detail to the existing sealed-run projection. Funding and volatility
detail are copied from one fully verified artifact without recomputing an
evaluation.

Nothing here trains a model, opens the operational workspace, acquires a provider
or writes domain data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel
from investment_analyst.research.experiment_report_models import (
    DEFAULT_EXPERIMENT_PAGE_LIMIT,
    EXPERIMENT_CURSOR_VERSION,
    MAX_EXPERIMENT_ARTIFACT_BYTES,
    MAX_EXPERIMENT_ENTRIES_PER_ROOT,
    MAX_EXPERIMENT_HEADER_BYTES,
    MAX_EXPERIMENT_PAGE_LIMIT,
    MAX_EXPERIMENT_PREDICTIONS_BYTES,
    MAX_EXPERIMENT_REPORT_BYTES,
    MAX_FUNDING_CACHE_BYTES,
    MAX_FUNDING_CACHE_ENTRIES,
    MAX_VOLATILITY_CACHE_BYTES,
    MAX_VOLATILITY_CACHE_ENTRIES,
    MIN_EXPERIMENT_PAGE_LIMIT,
    BaselineExperimentPayload,
    ExperimentArtifactStatus,
    ExperimentFamily,
    ExperimentFamilyFilter,
    ExperimentListContext,
    ExperimentListItem,
    ExperimentListPage,
    ExperimentReportContext,
    ExperimentReportView,
    ExperimentRootState,
    ExperimentRootStatus,
    ExperimentWarning,
    ExperimentWarningCode,
    FundingExperimentPayload,
    FundingProvenanceView,
    VolatilityExperimentPayload,
    VolatilityProvenanceView,
)
from investment_analyst.research.ml_export import peak_rss_bytes
from investment_analyst.research.ml_funding_export import (
    FundingMlExportError,
    read_funding_ml_artifact,
)
from investment_analyst.research.ml_funding_models import (
    MANIFEST_FILE_NAME as FUNDING_MANIFEST_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    PREDICTIONS_FILE_NAME as FUNDING_PREDICTIONS_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    PROTOCOL_FILE_NAME as FUNDING_PROTOCOL_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    REPORT_FILE_NAME as FUNDING_REPORT_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    FundingArtifactStatus,
    FundingManifest,
    FundingProtocol,
)
from investment_analyst.research.ml_models import (
    MANIFEST_FILE_NAME,
    PROTOCOL_FILE_NAME,
    MlArtifactStatus,
    MlBaselineManifest,
    MlBaselineProtocol,
)
from investment_analyst.research.ml_report_models import MlResearchReportView
from investment_analyst.research.ml_report_service import (
    MlReportConflictError,
    MlReportNotFoundError,
    MlResearchReportService,
)
from investment_analyst.research.volatility_export import (
    VolatilityExportCapError,
    VolatilityExportConflictError,
    read_volatility_artifact,
)
from investment_analyst.research.volatility_models import (
    MANIFEST_FILE_NAME as VOLATILITY_MANIFEST_FILE_NAME,
)
from investment_analyst.research.volatility_models import (
    PREDICTIONS_FILE_NAME as VOLATILITY_PREDICTIONS_FILE_NAME,
)
from investment_analyst.research.volatility_models import (
    PROTOCOL_FILE_NAME as VOLATILITY_PROTOCOL_FILE_NAME,
)
from investment_analyst.research.volatility_models import (
    REPORT_FILE_NAME as VOLATILITY_REPORT_FILE_NAME,
)
from investment_analyst.research.volatility_models import (
    VolatilityArtifactStatus as SealedVolatilityStatus,
)
from investment_analyst.research.volatility_models import (
    VolatilityHoldoutPolicy,
    VolatilityManifest,
    VolatilityProtocol,
)

_READ_CHUNK_BYTES = 1024 * 1024
_PENDING_TIMEOUT_SECONDS = 30.0
_MAX_CURSOR_LENGTH = 4096

_VOLATILITY_READ_CAPS: dict[str, int] = {
    VOLATILITY_MANIFEST_FILE_NAME: MAX_EXPERIMENT_HEADER_BYTES,
    VOLATILITY_PROTOCOL_FILE_NAME: MAX_EXPERIMENT_HEADER_BYTES,
    VOLATILITY_REPORT_FILE_NAME: MAX_EXPERIMENT_REPORT_BYTES,
    VOLATILITY_PREDICTIONS_FILE_NAME: MAX_EXPERIMENT_PREDICTIONS_BYTES,
}
_VOLATILITY_ALLOWED_NAMES = frozenset(_VOLATILITY_READ_CAPS)
_VOLATILITY_HEADERS = (VOLATILITY_MANIFEST_FILE_NAME, VOLATILITY_PROTOCOL_FILE_NAME)

_FUNDING_READ_CAPS: dict[str, int] = {
    FUNDING_MANIFEST_FILE_NAME: MAX_EXPERIMENT_HEADER_BYTES,
    FUNDING_PROTOCOL_FILE_NAME: MAX_EXPERIMENT_HEADER_BYTES,
    FUNDING_REPORT_FILE_NAME: MAX_EXPERIMENT_REPORT_BYTES,
    FUNDING_PREDICTIONS_FILE_NAME: MAX_EXPERIMENT_PREDICTIONS_BYTES,
}
_FUNDING_ALLOWED_NAMES = frozenset(_FUNDING_READ_CAPS)
_FUNDING_HEADERS = (FUNDING_MANIFEST_FILE_NAME, FUNDING_PROTOCOL_FILE_NAME)

_BASELINE_READ_CAPS: dict[str, int] = {
    MANIFEST_FILE_NAME: MAX_EXPERIMENT_HEADER_BYTES,
    PROTOCOL_FILE_NAME: MAX_EXPERIMENT_HEADER_BYTES,
}

_STATUS_BY_SEALED_VALUE = {
    value.value: value
    for value in (
        ExperimentArtifactStatus.COMPLETED,
        ExperimentArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE,
        ExperimentArtifactStatus.INSUFFICIENT_SAMPLE,
        ExperimentArtifactStatus.ERROR,
    )
}


class ExperimentReportError(RuntimeError):
    """Base error for the local research-experiment surface."""


class ExperimentReportNotFoundError(ExperimentReportError):
    """The configured root or the requested sealed run does not exist."""


class ExperimentReportConflictError(ExperimentReportError):
    """The artifact is incompatible, corrupt, unsafe, incomplete or changing."""


class ExperimentReportCapacityError(ExperimentReportConflictError):
    """A configured root exceeds the declared bounded enumeration cap."""


class ExperimentCursorStaleError(ExperimentReportConflictError):
    """The opaque cursor no longer matches the requested context or inventory."""


class _IncompleteArtifact(ExperimentReportConflictError):
    """A funding candidate has no sealed manifest yet and is not a study."""


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
    manifest_sha256: str | None
    total_bytes: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    family: ExperimentFamily
    run_id: UUID

    @property
    def key(self) -> tuple[str, str]:
        return (self.family.value, str(self.run_id))


@dataclass(slots=True)
class _CacheEntry:
    signature: _ArtifactSignature
    projection: ExperimentReportView
    bytes: int


@dataclass(slots=True)
class _PendingRead:
    event: threading.Event = field(default_factory=threading.Event)
    result: ExperimentReportView | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _Inventory:
    candidates: tuple[_Candidate, ...]
    roots: tuple[ExperimentRootState, ...]
    warnings: tuple[ExperimentWarning, ...]
    fingerprint: str


class ExperimentReportService:
    """Serve discoverable research runs without promoting any study to a signal."""

    def __init__(
        self,
        *,
        baseline_reports: MlResearchReportService,
        funding_root: Path,
        volatility_root: Path,
    ) -> None:
        if not funding_root.is_absolute():
            raise ValueError("ml funding results root must be an absolute path")
        if not volatility_root.is_absolute():
            raise ValueError("ml volatility results root must be an absolute path")
        self._baseline = baseline_reports
        self._funding_root = funding_root
        self._volatility_root = volatility_root
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._volatility_cache: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._volatility_cache_bytes = 0
        self._pending: dict[tuple[str, str], _PendingRead] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._full_verifications = 0
        self._scans = 0
        self._listings = 0
        self._header_reads = 0
        self._bytes_read = 0
        self._cold_load_ms: float | None = None
        self._warm_load_ms: float | None = None
        self._list_ms: float | None = None

    @property
    def baseline_root(self) -> Path:
        """Return the baseline root already owned by the injected service."""
        return self._baseline.root

    @property
    def funding_root(self) -> Path:
        """Return the configured funding root; no existence check is performed."""
        return self._funding_root

    @property
    def volatility_root(self) -> Path:
        """Return the configured volatility root; no existence check is performed."""
        return self._volatility_root

    def performance(self) -> dict[str, object]:
        """Return bounded observed cost; absent values remain ``None``, never zero."""
        with self._lock:
            return {
                "cache_entries": len(self._cache),
                "cache_bytes": self._cache_bytes,
                "volatility_cache_entries": len(self._volatility_cache),
                "volatility_cache_bytes": self._volatility_cache_bytes,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "full_verifications": self._full_verifications,
                "artifact_scans": self._scans,
                "listings": self._listings,
                "header_reads": self._header_reads,
                "bytes_read": self._bytes_read,
                "cold_load_ms": self._cold_load_ms,
                "warm_load_ms": self._warm_load_ms,
                "list_ms": self._list_ms,
                "baseline": self._baseline.performance(),
                "peak_rss_bytes": peak_rss_bytes(),
            }

    def list_experiments(
        self,
        *,
        asset_id: str,
        family: str = ExperimentFamilyFilter.ALL.value,
        limit: int = DEFAULT_EXPERIMENT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> ExperimentListPage:
        """Return one bounded page of sealed runs for exactly one catalog asset."""
        if not asset_id or not asset_id.strip():
            raise ValueError("asset_id is required")
        try:
            family_filter = ExperimentFamilyFilter(family)
        except ValueError as error:
            raise ValueError("family must be all, baseline, funding or volatility") from error
        if not MIN_EXPERIMENT_PAGE_LIMIT <= limit <= MAX_EXPERIMENT_PAGE_LIMIT:
            raise ValueError(
                f"limit must be between {MIN_EXPERIMENT_PAGE_LIMIT} and {MAX_EXPERIMENT_PAGE_LIMIT}"
            )
        started = time.perf_counter()
        inventory = self._inventory()
        context = ExperimentListContext(asset_id=asset_id, family=family_filter, limit=limit)
        start = 0
        if cursor is not None:
            start = self._resume_index(inventory, context=context, cursor=cursor)
        window = inventory.candidates[start : start + limit]
        remaining = inventory.candidates[start + len(window) :]

        items: list[ExperimentListItem] = []
        warnings: list[ExperimentWarning] = list(inventory.warnings)
        for candidate in window:
            item, warning = self._list_candidate(candidate, asset_id=asset_id)
            if item is not None:
                items.append(item)
            if warning is not None:
                warnings.append(warning)

        next_cursor = None
        if remaining and window:
            next_cursor = self._encode_cursor(
                context=context,
                fingerprint=inventory.fingerprint,
                last=window[-1],
            )
        page = ExperimentListPage(
            context=context,
            items=tuple(items),
            next_cursor=next_cursor,
            roots=inventory.roots,
            warnings=tuple(warnings),
            examined_candidates=len(window),
        )
        with self._lock:
            self._listings += 1
            self._list_ms = (time.perf_counter() - started) * 1000.0
        return page

    def report(
        self,
        *,
        asset_id: str,
        family: str,
        run_id: UUID,
    ) -> ExperimentReportView:
        """Resolve, verify and project exactly one sealed run of one family."""
        if not asset_id or not asset_id.strip():
            raise ValueError("asset_id is required")
        try:
            resolved_family = ExperimentFamily(family)
        except ValueError as error:
            raise ValueError("family must be baseline, funding or volatility") from error
        if resolved_family is ExperimentFamily.BASELINE:
            return self._baseline_view(asset_id=asset_id, run_id=run_id)
        if resolved_family is ExperimentFamily.VOLATILITY:
            return self._volatility_view(asset_id=asset_id, run_id=run_id)
        return self._funding_view(asset_id=asset_id, run_id=run_id)

    def _baseline_view(self, *, asset_id: str, run_id: UUID) -> ExperimentReportView:
        try:
            view: MlResearchReportView = self._baseline.report(asset_id=asset_id, run_id=run_id)
        except MlReportNotFoundError as error:
            raise ExperimentReportNotFoundError(str(error)) from error
        except MlReportConflictError as error:
            raise ExperimentReportConflictError(str(error)) from error
        return ExperimentReportView(
            context=ExperimentReportContext(
                asset_id=view.provenance.asset_id,
                family=ExperimentFamily.BASELINE,
                run_id=view.artifact.run_id,
                manifest_sha256=view.artifact.manifest_sha256,
                protocol_sha256=view.artifact.protocol_sha256,
                report_sha256=view.artifact.report_sha256,
                artifact_bytes=view.artifact.artifact_bytes,
                created_at=view.artifact.created_at,
            ),
            payload=BaselineExperimentPayload(report=view),
        )

    def _volatility_view(self, *, asset_id: str, run_id: UUID) -> ExperimentReportView:
        root = self._resolved_volatility_root()
        run_dir = self._run_directory(root, run_id)
        key = (asset_id, str(run_id))

        with self._lock:
            pending = self._pending.get(key)
            leader = pending is None
            if pending is None:
                pending = _PendingRead()
                self._pending[key] = pending
        if not leader:
            if not pending.event.wait(timeout=_PENDING_TIMEOUT_SECONDS):
                raise ExperimentReportConflictError("timed out waiting for an identical read")
            if pending.result is not None:
                return pending.result
            return self._load_volatility_with_retry(key, run_dir, asset_id, run_id)

        try:
            result = self._load_volatility_with_retry(key, run_dir, asset_id, run_id)
            pending.result = result
            return result
        except Exception as error:
            pending.error = error
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)
            pending.event.set()

    def _load_volatility_with_retry(
        self,
        key: tuple[str, str],
        run_dir: Path,
        asset_id: str,
        run_id: UUID,
    ) -> ExperimentReportView:
        for attempt in range(2):
            started = time.perf_counter()
            signature, payloads = self._scan_volatility(
                run_dir, retain=frozenset(_VOLATILITY_HEADERS)
            )
            entry = self._cached_volatility_entry(key, signature)
            if entry is not None:
                with self._lock:
                    self._cache_hits += 1
                    self._warm_load_ms = (time.perf_counter() - started) * 1000.0
                return entry.projection
            try:
                projection = self._build_volatility_projection(
                    run_dir=run_dir,
                    run_id=run_id,
                    asset_id=asset_id,
                    signature=signature,
                    payloads=payloads,
                )
            except ExperimentReportConflictError:
                current, _ = self._scan_volatility(run_dir, retain=frozenset())
                if attempt == 0 and current != signature:
                    continue
                raise
            current, _ = self._scan_volatility(run_dir, retain=frozenset())
            if current != signature:
                if attempt == 0:
                    continue
                raise ExperimentReportConflictError("artifact changed during read")
            self._store_volatility_cache(key, signature, projection)
            with self._lock:
                self._cache_misses += 1
                self._cold_load_ms = (time.perf_counter() - started) * 1000.0
            return projection
        raise ExperimentReportConflictError("artifact changed during read")

    def _cached_volatility_entry(
        self, key: tuple[str, str], signature: _ArtifactSignature
    ) -> _CacheEntry | None:
        with self._lock:
            entry = self._volatility_cache.get(key)
            if entry is None:
                return None
            if entry.signature != signature or entry.bytes > MAX_VOLATILITY_CACHE_BYTES:
                self._volatility_cache.pop(key, None)
                self._volatility_cache_bytes -= entry.bytes
                return None
            self._volatility_cache.move_to_end(key)
            return entry

    def _store_volatility_cache(
        self,
        key: tuple[str, str],
        signature: _ArtifactSignature,
        projection: ExperimentReportView,
    ) -> None:
        size = len(canonical_analytical_json(projection.model_dump(mode="json")))
        if size > MAX_VOLATILITY_CACHE_BYTES:
            return
        with self._lock:
            previous = self._volatility_cache.pop(key, None)
            if previous is not None:
                self._volatility_cache_bytes -= previous.bytes
            self._volatility_cache[key] = _CacheEntry(
                signature=signature,
                projection=projection,
                bytes=size,
            )
            self._volatility_cache_bytes += size
            while (
                len(self._volatility_cache) > MAX_VOLATILITY_CACHE_ENTRIES
                or self._volatility_cache_bytes > MAX_VOLATILITY_CACHE_BYTES
            ):
                _, evicted = self._volatility_cache.popitem(last=False)
                self._volatility_cache_bytes -= evicted.bytes

    def _build_volatility_projection(
        self,
        *,
        run_dir: Path,
        run_id: UUID,
        asset_id: str,
        signature: _ArtifactSignature,
        payloads: dict[str, bytes],
    ) -> ExperimentReportView:
        manifest_bytes = payloads.get(VOLATILITY_MANIFEST_FILE_NAME)
        protocol_bytes = payloads.get(VOLATILITY_PROTOCOL_FILE_NAME)
        if manifest_bytes is None or protocol_bytes is None:
            raise ExperimentReportConflictError("sealed volatility run is incomplete")
        manifest = self._parse_canonical(
            VolatilityManifest, manifest_bytes, "sealed volatility manifest"
        )
        protocol = self._parse_canonical(
            VolatilityProtocol, protocol_bytes, "sealed volatility protocol"
        )
        protocol_sha256 = hashlib.sha256(protocol_bytes).hexdigest()
        if manifest.run_id != run_id:
            raise ExperimentReportConflictError("sealed volatility manifest belongs to another run")
        if (
            manifest.protocol.name != VOLATILITY_PROTOCOL_FILE_NAME
            or manifest.protocol.sha256 != protocol_sha256
            or manifest.protocol.bytes != len(protocol_bytes)
        ):
            raise ExperimentReportConflictError(
                "sealed volatility protocol differs from its manifest"
            )
        if protocol.asset_id != asset_id:
            raise ExperimentReportConflictError("sealed artifact belongs to a different asset")

        with self._lock:
            self._full_verifications += 1
        try:
            loaded = read_volatility_artifact(run_dir)
        except (VolatilityExportCapError, VolatilityExportConflictError) as error:
            raise ExperimentReportConflictError(
                "sealed volatility artifact is incompatible or corrupt"
            ) from error
        if loaded.manifest is None:
            raise ExperimentReportConflictError("sealed volatility run has no manifest")
        report = loaded.report
        if (
            loaded.manifest != manifest
            or loaded.protocol != protocol
            or report.run_id != run_id
            or report.status is not SealedVolatilityStatus.COMPLETED
            or report.decision is None
            or report.decision_reason is None
        ):
            raise ExperimentReportConflictError(
                "sealed volatility artifact changed during verification"
            )

        return ExperimentReportView(
            context=ExperimentReportContext(
                asset_id=protocol.asset_id,
                family=ExperimentFamily.VOLATILITY,
                run_id=manifest.run_id,
                manifest_sha256=(
                    signature.manifest_sha256 or hashlib.sha256(manifest_bytes).hexdigest()
                ),
                protocol_sha256=protocol_sha256,
                report_sha256=manifest.report.sha256,
                artifact_bytes=manifest.root_bytes,
                created_at=manifest.created_at,
            ),
            payload=VolatilityExperimentPayload(
                protocol=protocol,
                provenance=VolatilityProvenanceView(
                    archive_id=report.archive_id,
                    archive_semantic_sha256=report.archive_semantic_sha256,
                    archive_candles_sha256=report.archive_candles_sha256,
                    code_material_sha256=report.code_material_sha256,
                    code_material_files=report.code_material_files,
                    source_id=protocol.source_id,
                ),
                environment=report.environment,
                decision=report.decision,
                decision_reason=report.decision_reason,
                decision_rows=protocol.decision_count,
                prediction_rows=report.prediction_rows,
                development_fits=report.development_fits,
                folds=report.folds,
                pooled_scores=report.pooled_scores,
                pooled_comparisons=report.pooled_comparisons,
                holdout=VolatilityHoldoutPolicy(),
                timings=report.timings,
                counts=report.counts,
                peak_rss_bytes=report.peak_rss_bytes,
            ),
        )

    def _funding_view(self, *, asset_id: str, run_id: UUID) -> ExperimentReportView:
        root = self._resolved_funding_root()
        run_dir = self._run_directory(root, run_id)
        key = (asset_id, str(run_id))

        with self._lock:
            pending = self._pending.get(key)
            leader = pending is None
            if pending is None:
                pending = _PendingRead()
                self._pending[key] = pending
        if not leader:
            if not pending.event.wait(timeout=_PENDING_TIMEOUT_SECONDS):
                raise ExperimentReportConflictError("timed out waiting for an identical read")
            if pending.result is not None:
                return pending.result
            return self._load_funding_with_retry(key, run_dir, asset_id, run_id)

        try:
            result = self._load_funding_with_retry(key, run_dir, asset_id, run_id)
            pending.result = result
            return result
        except Exception as error:
            pending.error = error
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)
            pending.event.set()

    def _load_funding_with_retry(
        self,
        key: tuple[str, str],
        run_dir: Path,
        asset_id: str,
        run_id: UUID,
    ) -> ExperimentReportView:
        for attempt in range(2):
            started = time.perf_counter()
            signature, payloads = self._scan_funding(run_dir, retain=frozenset(_FUNDING_HEADERS))
            entry = self._cached_entry(key, signature)
            if entry is not None:
                with self._lock:
                    self._cache_hits += 1
                    self._warm_load_ms = (time.perf_counter() - started) * 1000.0
                return entry.projection
            try:
                projection = self._build_funding_projection(
                    run_dir=run_dir,
                    run_id=run_id,
                    asset_id=asset_id,
                    signature=signature,
                    payloads=payloads,
                )
            except ExperimentReportConflictError:
                current, _ = self._scan_funding(run_dir, retain=frozenset())
                if attempt == 0 and current != signature:
                    continue
                raise
            current, _ = self._scan_funding(run_dir, retain=frozenset())
            if current != signature:
                if attempt == 0:
                    continue
                raise ExperimentReportConflictError("artifact changed during read")
            self._store_cache(key, signature, projection)
            with self._lock:
                self._cache_misses += 1
                self._cold_load_ms = (time.perf_counter() - started) * 1000.0
            return projection
        raise ExperimentReportConflictError("artifact changed during read")

    def _build_funding_projection(
        self,
        *,
        run_dir: Path,
        run_id: UUID,
        asset_id: str,
        signature: _ArtifactSignature,
        payloads: dict[str, bytes],
    ) -> ExperimentReportView:
        manifest_bytes = payloads.get(FUNDING_MANIFEST_FILE_NAME)
        protocol_bytes = payloads.get(FUNDING_PROTOCOL_FILE_NAME)
        if manifest_bytes is None or protocol_bytes is None:
            raise ExperimentReportConflictError("sealed funding run is incomplete")
        manifest = self._parse_canonical(FundingManifest, manifest_bytes, "sealed funding manifest")
        protocol = self._parse_canonical(FundingProtocol, protocol_bytes, "sealed funding protocol")
        protocol_sha256 = hashlib.sha256(protocol_bytes).hexdigest()
        if manifest.run_id != run_id:
            raise ExperimentReportConflictError("sealed funding manifest belongs to another run")
        if (
            manifest.protocol.name != FUNDING_PROTOCOL_FILE_NAME
            or manifest.protocol.sha256 != protocol_sha256
            or manifest.protocol.bytes != len(protocol_bytes)
        ):
            raise ExperimentReportConflictError("sealed funding protocol differs from its manifest")
        if (
            manifest.dataset_id != protocol.dataset_id
            or manifest.dataset_content_sha256 != protocol.dataset_content_sha256
            or manifest.bundle_id != protocol.bundle_id
            or manifest.bundle_content_sha256 != protocol.bundle_content_sha256
            or manifest.criteria != protocol.criteria
        ):
            raise ExperimentReportConflictError(
                "sealed funding manifest disagrees with its protocol"
            )
        if protocol.asset_id != asset_id:
            raise ExperimentReportConflictError("sealed artifact belongs to a different asset")

        with self._lock:
            self._full_verifications += 1
        try:
            loaded = read_funding_ml_artifact(run_dir)
        except FundingMlExportError as error:
            raise ExperimentReportConflictError(
                "sealed funding artifact is incompatible or corrupt"
            ) from error
        if loaded.manifest is None:
            raise ExperimentReportConflictError("sealed funding run has no manifest")
        if (
            loaded.manifest != manifest
            or loaded.protocol != protocol
            or loaded.report.run_id != run_id
            or loaded.report.status is not FundingArtifactStatus.COMPLETED
            or loaded.report.decision is None
        ):
            raise ExperimentReportConflictError(
                "sealed funding artifact changed during verification"
            )

        report = loaded.report
        return ExperimentReportView(
            context=ExperimentReportContext(
                asset_id=protocol.asset_id,
                family=ExperimentFamily.FUNDING,
                run_id=manifest.run_id,
                manifest_sha256=(
                    signature.manifest_sha256 or hashlib.sha256(manifest_bytes).hexdigest()
                ),
                protocol_sha256=protocol_sha256,
                report_sha256=manifest.report.sha256,
                artifact_bytes=manifest.root_bytes,
                created_at=manifest.created_at,
            ),
            payload=FundingExperimentPayload(
                protocol=protocol,
                provenance=FundingProvenanceView(
                    dataset_manifest_sha256=report.dataset_manifest_sha256,
                    bundle_manifest_sha256=report.bundle_manifest_sha256,
                    code_material_sha256=report.code_material_sha256,
                    code_material_files=report.code_material_files,
                ),
                environment=report.environment,
                decision=report.decision,
                decision_rows=protocol.decision_rows,
                prediction_rows=report.prediction_rows,
                development_fits=report.development_fits,
                folds=report.folds,
                pooled_scores=report.pooled_scores,
                pooled_comparisons=report.pooled_comparisons,
                calibration=report.calibration,
                holdout=report.holdout,
                timings=report.timings,
                peak_rss_bytes=report.peak_rss_bytes,
            ),
        )

    def _inventory(self) -> _Inventory:
        candidates: list[_Candidate] = []
        roots: list[ExperimentRootState] = []
        warnings: list[ExperimentWarning] = []
        inventory_names: list[list[str]] = []
        for family, root in (
            (ExperimentFamily.BASELINE, self.baseline_root),
            (ExperimentFamily.FUNDING, self._funding_root),
            (ExperimentFamily.VOLATILITY, self._volatility_root),
        ):
            names = self._root_entries(root)
            if names is None:
                roots.append(
                    ExperimentRootState(
                        family=family,
                        status=ExperimentRootStatus.MISSING,
                        entries=0,
                    )
                )
                inventory_names.append([f"{family.value}:missing"])
                continue
            roots.append(
                ExperimentRootState(
                    family=family,
                    status=ExperimentRootStatus.AVAILABLE,
                    entries=len(names),
                )
            )
            unsupported = 0
            for name in names:
                run_id = self._canonical_uuid(name)
                if run_id is None:
                    unsupported += 1
                    inventory_names.append([family.value, f"unsupported:{name}"])
                    continue
                candidates.append(_Candidate(family=family, run_id=run_id))
                inventory_names.append([family.value, str(run_id)])
            if unsupported:
                warnings.append(
                    ExperimentWarning(
                        code=ExperimentWarningCode.UNSUPPORTED_ENTRY,
                        family=family,
                        run_id=None,
                        message=(
                            f"la raíz declara {unsupported} entrada(s) con nombre no reconocido;"
                            " no se presentan como estudios"
                        ),
                    )
                )
        ordered = tuple(sorted(candidates, key=lambda item: item.key))
        fingerprint = hashlib.sha256(canonical_analytical_json(sorted(inventory_names))).hexdigest()
        return _Inventory(
            candidates=ordered,
            roots=tuple(roots),
            warnings=tuple(warnings),
            fingerprint=fingerprint,
        )

    def _root_entries(self, root: Path) -> tuple[str, ...] | None:
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ExperimentReportConflictError(
                "configured research root is not readable"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise ExperimentReportConflictError("configured research root must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise ExperimentReportConflictError("configured research root is not a directory")
        try:
            resolved = root.resolve(strict=True)
            entries = tuple(entry.name for entry in resolved.iterdir())
        except OSError as error:
            raise ExperimentReportConflictError(
                "configured research root is not readable"
            ) from error
        if len(entries) > MAX_EXPERIMENT_ENTRIES_PER_ROOT:
            raise ExperimentReportCapacityError(
                "configured research root exceeds the bounded entry cap"
            )
        return tuple(sorted(entries))

    def _list_candidate(
        self,
        candidate: _Candidate,
        *,
        asset_id: str,
    ) -> tuple[ExperimentListItem | None, ExperimentWarning | None]:
        try:
            if candidate.family is ExperimentFamily.BASELINE:
                return self._list_baseline(candidate, asset_id=asset_id), None
            if candidate.family is ExperimentFamily.VOLATILITY:
                return self._list_volatility(candidate, asset_id=asset_id), None
            return self._list_funding(candidate, asset_id=asset_id), None
        except _IncompleteArtifact:
            return (
                None,
                ExperimentWarning(
                    code=ExperimentWarningCode.INCOMPLETE_ARTIFACT,
                    family=candidate.family,
                    run_id=candidate.run_id,
                    message=(
                        "el run no tiene manifest sellado; no se presenta como estudio verificado"
                    ),
                ),
            )
        except (ExperimentReportConflictError, ValueError):
            return (
                None,
                ExperimentWarning(
                    code=ExperimentWarningCode.INCOMPATIBLE_ARTIFACT,
                    family=candidate.family,
                    run_id=candidate.run_id,
                    message=(
                        "la cabecera sellada no es compatible con el lector vigente;"
                        " el run no se presenta como estudio verificado"
                    ),
                ),
            )

    def _list_baseline(self, candidate: _Candidate, *, asset_id: str) -> ExperimentListItem | None:
        root = self._resolved_header_root(self.baseline_root)
        run_dir = self._run_directory(root, candidate.run_id)
        manifest_bytes = self._read_header(
            run_dir / MANIFEST_FILE_NAME, cap=MAX_EXPERIMENT_HEADER_BYTES
        )
        protocol_bytes = self._read_header(
            run_dir / PROTOCOL_FILE_NAME, cap=MAX_EXPERIMENT_HEADER_BYTES
        )
        manifest = self._parse_canonical(MlBaselineManifest, manifest_bytes, "sealed manifest")
        protocol = self._parse_canonical(MlBaselineProtocol, protocol_bytes, "sealed protocol")
        protocol_sha256 = hashlib.sha256(protocol_bytes).hexdigest()
        if manifest.run_id != candidate.run_id:
            raise ExperimentReportConflictError("sealed manifest belongs to another run")
        if (
            manifest.protocol.name != PROTOCOL_FILE_NAME
            or manifest.protocol.sha256 != protocol_sha256
            or manifest.protocol.bytes != len(protocol_bytes)
            or manifest.protocol_id != protocol.protocol_id
            or manifest.dataset_id != protocol.dataset_id
            or manifest.dataset_content_sha256 != protocol.dataset_content_sha256
        ):
            raise ExperimentReportConflictError("sealed manifest disagrees with its protocol")
        if protocol.asset_id != asset_id:
            return None
        return ExperimentListItem(
            family=ExperimentFamily.BASELINE,
            run_id=manifest.run_id,
            asset_id=protocol.asset_id,
            protocol_id=manifest.protocol_id,
            artifact_status=self._baseline_status(manifest.status),
            created_at=manifest.created_at,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            protocol_sha256=protocol_sha256,
            report_sha256=manifest.report.sha256,
            artifact_bytes=manifest.root_bytes,
        )

    def _list_volatility(
        self, candidate: _Candidate, *, asset_id: str
    ) -> ExperimentListItem | None:
        root = self._resolved_header_root(self._volatility_root)
        run_dir = self._run_directory(root, candidate.run_id)
        try:
            manifest_bytes = self._read_header(
                run_dir / VOLATILITY_MANIFEST_FILE_NAME, cap=MAX_EXPERIMENT_HEADER_BYTES
            )
        except ExperimentReportConflictError as error:
            raise _IncompleteArtifact("volatility run has no sealed manifest") from error
        protocol_bytes = self._read_header(
            run_dir / VOLATILITY_PROTOCOL_FILE_NAME, cap=MAX_EXPERIMENT_HEADER_BYTES
        )
        manifest = self._parse_canonical(
            VolatilityManifest, manifest_bytes, "sealed volatility manifest"
        )
        protocol = self._parse_canonical(
            VolatilityProtocol, protocol_bytes, "sealed volatility protocol"
        )
        protocol_sha256 = hashlib.sha256(protocol_bytes).hexdigest()
        if manifest.run_id != candidate.run_id:
            raise ExperimentReportConflictError("sealed volatility manifest belongs to another run")
        if (
            manifest.protocol.name != VOLATILITY_PROTOCOL_FILE_NAME
            or manifest.protocol.sha256 != protocol_sha256
            or manifest.protocol.bytes != len(protocol_bytes)
            or manifest.archive_id != protocol.archive_id
            or manifest.archive_semantic_sha256 != protocol.archive_semantic_sha256
            or manifest.archive_candles_sha256 != protocol.archive_candles_sha256
            or manifest.criteria != protocol.criteria
        ):
            raise ExperimentReportConflictError(
                "sealed volatility manifest disagrees with its protocol"
            )
        if protocol.asset_id != asset_id:
            return None
        return ExperimentListItem(
            family=ExperimentFamily.VOLATILITY,
            run_id=manifest.run_id,
            asset_id=protocol.asset_id,
            protocol_id=manifest.protocol_id,
            artifact_status=self._volatility_status(manifest.status),
            created_at=manifest.created_at,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            protocol_sha256=protocol_sha256,
            report_sha256=manifest.report.sha256,
            artifact_bytes=manifest.root_bytes,
        )

    def _list_funding(self, candidate: _Candidate, *, asset_id: str) -> ExperimentListItem | None:
        root = self._resolved_header_root(self._funding_root)
        run_dir = self._run_directory(root, candidate.run_id)
        try:
            manifest_bytes = self._read_header(
                run_dir / FUNDING_MANIFEST_FILE_NAME, cap=MAX_EXPERIMENT_HEADER_BYTES
            )
        except ExperimentReportConflictError as error:
            raise _IncompleteArtifact("funding run has no sealed manifest") from error
        protocol_bytes = self._read_header(
            run_dir / FUNDING_PROTOCOL_FILE_NAME, cap=MAX_EXPERIMENT_HEADER_BYTES
        )
        manifest = self._parse_canonical(FundingManifest, manifest_bytes, "sealed funding manifest")
        protocol = self._parse_canonical(FundingProtocol, protocol_bytes, "sealed funding protocol")
        protocol_sha256 = hashlib.sha256(protocol_bytes).hexdigest()
        if manifest.run_id != candidate.run_id:
            raise ExperimentReportConflictError("sealed funding manifest belongs to another run")
        if (
            manifest.protocol.name != FUNDING_PROTOCOL_FILE_NAME
            or manifest.protocol.sha256 != protocol_sha256
            or manifest.protocol.bytes != len(protocol_bytes)
            or manifest.dataset_id != protocol.dataset_id
            or manifest.dataset_content_sha256 != protocol.dataset_content_sha256
            or manifest.bundle_id != protocol.bundle_id
            or manifest.bundle_content_sha256 != protocol.bundle_content_sha256
            or manifest.criteria != protocol.criteria
        ):
            raise ExperimentReportConflictError(
                "sealed funding manifest disagrees with its protocol"
            )
        if protocol.asset_id != asset_id:
            return None
        return ExperimentListItem(
            family=ExperimentFamily.FUNDING,
            run_id=manifest.run_id,
            asset_id=protocol.asset_id,
            protocol_id=manifest.protocol_id,
            artifact_status=ExperimentArtifactStatus.COMPLETED,
            created_at=manifest.created_at,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            protocol_sha256=protocol_sha256,
            report_sha256=manifest.report.sha256,
            artifact_bytes=manifest.root_bytes,
        )

    @staticmethod
    def _volatility_status(status: SealedVolatilityStatus) -> ExperimentArtifactStatus:
        resolved = _STATUS_BY_SEALED_VALUE.get(status.value)
        if resolved is None:
            raise ExperimentReportConflictError("sealed status is not recognized")
        return resolved

    @staticmethod
    def _baseline_status(status: MlArtifactStatus) -> ExperimentArtifactStatus:
        resolved = _STATUS_BY_SEALED_VALUE.get(status.value)
        if resolved is None:
            raise ExperimentReportConflictError("sealed status is not recognized")
        return resolved

    def _scan_volatility(
        self,
        run_dir: Path,
        *,
        retain: frozenset[str],
    ) -> tuple[_ArtifactSignature, dict[str, bytes]]:
        try:
            entries = tuple(run_dir.iterdir())
        except OSError as error:
            raise ExperimentReportConflictError("sealed run is not readable") from error
        files: dict[str, Path] = {}
        for entry in entries:
            name = entry.name
            if name not in _VOLATILITY_ALLOWED_NAMES:
                raise ExperimentReportConflictError("sealed run contains unexpected entries")
            try:
                info = os.lstat(entry)
            except OSError as error:
                raise ExperimentReportConflictError("sealed file is not readable") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ExperimentReportConflictError("sealed entry must be a regular file")
            files[name] = entry
        if VOLATILITY_PROTOCOL_FILE_NAME not in files:
            raise ExperimentReportConflictError("sealed volatility run is incomplete")
        digests: list[_FileDigest] = []
        payloads: dict[str, bytes] = {}
        total_bytes = 0
        for name in sorted(files):
            payload, digest = self._read_file(
                files[name],
                name=name,
                cap=_VOLATILITY_READ_CAPS[name],
                retain=name in retain,
            )
            total_bytes += digest.size
            if total_bytes > MAX_EXPERIMENT_ARTIFACT_BYTES:
                raise ExperimentReportConflictError("sealed run exceeds the aggregate read cap")
            if payload is not None:
                payloads[name] = payload
            digests.append(digest)
        manifest = next(
            (digest for digest in digests if digest.name == VOLATILITY_MANIFEST_FILE_NAME),
            None,
        )
        signature = _ArtifactSignature(
            files=tuple(digests),
            manifest_sha256=manifest.sha256 if manifest is not None else None,
            total_bytes=total_bytes,
        )
        with self._lock:
            self._scans += 1
            self._bytes_read += total_bytes
        return signature, payloads

    def _scan_funding(
        self,
        run_dir: Path,
        *,
        retain: frozenset[str],
    ) -> tuple[_ArtifactSignature, dict[str, bytes]]:
        try:
            entries = tuple(run_dir.iterdir())
        except OSError as error:
            raise ExperimentReportConflictError("sealed run is not readable") from error
        files: dict[str, Path] = {}
        for entry in entries:
            name = entry.name
            if name not in _FUNDING_ALLOWED_NAMES:
                raise ExperimentReportConflictError("sealed run contains unexpected entries")
            try:
                info = os.lstat(entry)
            except OSError as error:
                raise ExperimentReportConflictError("sealed file is not readable") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ExperimentReportConflictError("sealed entry must be a regular file")
            files[name] = entry
        if FUNDING_PROTOCOL_FILE_NAME not in files:
            raise ExperimentReportConflictError("sealed funding run is incomplete")
        digests: list[_FileDigest] = []
        payloads: dict[str, bytes] = {}
        total_bytes = 0
        for name in sorted(files):
            payload, digest = self._read_file(
                files[name],
                name=name,
                cap=_FUNDING_READ_CAPS[name],
                retain=name in retain,
            )
            total_bytes += digest.size
            if total_bytes > MAX_EXPERIMENT_ARTIFACT_BYTES:
                raise ExperimentReportConflictError("sealed run exceeds the aggregate read cap")
            if payload is not None:
                payloads[name] = payload
            digests.append(digest)
        manifest = next(
            (digest for digest in digests if digest.name == FUNDING_MANIFEST_FILE_NAME),
            None,
        )
        signature = _ArtifactSignature(
            files=tuple(digests),
            manifest_sha256=manifest.sha256 if manifest is not None else None,
            total_bytes=total_bytes,
        )
        with self._lock:
            self._scans += 1
            self._bytes_read += total_bytes
        return signature, payloads

    def _read_header(self, path: Path, *, cap: int) -> bytes:
        payload, _ = self._read_file(path, name=path.name, cap=cap, retain=True)
        if payload is None:
            raise ExperimentReportConflictError("sealed header is not readable")
        with self._lock:
            self._header_reads += 1
        return payload

    def _read_file(
        self,
        path: Path,
        *,
        name: str,
        cap: int,
        retain: bool,
    ) -> tuple[bytes | None, _FileDigest]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ExperimentReportConflictError("sealed file is not readable") from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ExperimentReportConflictError("sealed entry must be a regular file")
            if before.st_size > cap:
                raise ExperimentReportConflictError("sealed file exceeds its declared read cap")
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
            raise ExperimentReportConflictError("sealed file is not readable") from error
        finally:
            os.close(descriptor)
        if total > cap:
            raise ExperimentReportConflictError("sealed file exceeds its declared read cap")
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
            raise ExperimentReportConflictError("sealed file changed during read")
        if total != after.st_size:
            raise ExperimentReportConflictError("sealed file changed during read")
        with self._lock:
            self._bytes_read += total
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

    @staticmethod
    def _parse_canonical[T: ContractModel](
        model: type[T],
        payload: bytes,
        label: str,
    ) -> T:
        try:
            parsed = model.model_validate_json(payload)
        except ValueError as error:
            raise ExperimentReportConflictError(f"{label} is malformed") from error
        if canonical_analytical_json(parsed.model_dump(mode="json")) != payload:
            raise ExperimentReportConflictError(f"{label} is not in canonical encoding")
        return parsed

    def _resolved_header_root(self, root: Path) -> Path:
        try:
            info = os.lstat(root)
        except FileNotFoundError as error:
            raise ExperimentReportNotFoundError(
                "configured research root is not available"
            ) from error
        except OSError as error:
            raise ExperimentReportConflictError(
                "configured research root is not readable"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise ExperimentReportConflictError("configured research root must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise ExperimentReportNotFoundError("configured research root is not a directory")
        try:
            return root.resolve(strict=True)
        except OSError as error:
            raise ExperimentReportConflictError(
                "configured research root is not readable"
            ) from error

    @staticmethod
    def _run_directory(root: Path, run_id: UUID) -> Path:
        candidate = root / str(run_id)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError as error:
            raise ExperimentReportNotFoundError("sealed run was not found") from error
        except OSError as error:
            raise ExperimentReportConflictError("sealed run is not readable") from error
        if stat.S_ISLNK(info.st_mode):
            raise ExperimentReportConflictError("sealed run must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise ExperimentReportConflictError("sealed run is not a directory")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ExperimentReportConflictError("sealed run is not readable") from error
        if resolved.parent != root or not resolved.is_relative_to(root):
            raise ExperimentReportConflictError("sealed run escapes its configured root")
        return resolved

    def _resolved_volatility_root(self) -> Path:
        try:
            return self._resolved_header_root(self._volatility_root)
        except ExperimentReportNotFoundError as error:
            raise ExperimentReportNotFoundError(
                "ML volatility research results root is not available"
            ) from error

    def _resolved_funding_root(self) -> Path:
        try:
            return self._resolved_header_root(self._funding_root)
        except ExperimentReportNotFoundError as error:
            raise ExperimentReportNotFoundError(
                "ML funding research results root is not available"
            ) from error

    @staticmethod
    def _canonical_uuid(value: str) -> UUID | None:
        try:
            parsed = UUID(value)
        except ValueError:
            return None
        return parsed if str(parsed) == value else None

    def _cached_entry(
        self,
        key: tuple[str, str],
        signature: _ArtifactSignature,
    ) -> _CacheEntry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.signature != signature or entry.bytes > MAX_FUNDING_CACHE_BYTES:
                self._cache.pop(key, None)
                self._cache_bytes -= entry.bytes
                return None
            self._cache.move_to_end(key)
            return entry

    def _store_cache(
        self,
        key: tuple[str, str],
        signature: _ArtifactSignature,
        projection: ExperimentReportView,
    ) -> None:
        size = len(canonical_analytical_json(projection.model_dump(mode="json")))
        if size > MAX_FUNDING_CACHE_BYTES:
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
            while (
                len(self._cache) > MAX_FUNDING_CACHE_ENTRIES
                or self._cache_bytes > MAX_FUNDING_CACHE_BYTES
            ):
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= evicted.bytes

    def _encode_cursor(
        self,
        *,
        context: ExperimentListContext,
        fingerprint: str,
        last: _Candidate,
    ) -> str:
        payload = {
            "version": EXPERIMENT_CURSOR_VERSION,
            "asset_id": context.asset_id,
            "family": context.family.value,
            "limit": context.limit,
            "fingerprint": fingerprint,
            "last_family": last.family.value,
            "last_run_id": str(last.run_id),
        }
        encoded = base64.urlsafe_b64encode(canonical_analytical_json(payload)).decode("ascii")
        return encoded.rstrip("=")

    def _resume_index(
        self,
        inventory: _Inventory,
        *,
        context: ExperimentListContext,
        cursor: str,
    ) -> int:
        if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
            raise ValueError("cursor must be a bounded opaque token")
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = base64.urlsafe_b64decode(f"{cursor}{padding}".encode("ascii"))
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ValueError("cursor is not a valid opaque token") from error
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("cursor is not a valid opaque token") from error
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "asset_id",
            "family",
            "limit",
            "fingerprint",
            "last_family",
            "last_run_id",
        }:
            raise ValueError("cursor is not a valid opaque token")
        if payload["version"] != EXPERIMENT_CURSOR_VERSION:
            raise ValueError("cursor version is not recognized")
        if (
            payload["asset_id"] != context.asset_id
            or payload["family"] != context.family.value
            or payload["limit"] != context.limit
        ):
            raise ExperimentCursorStaleError(
                "el cursor no corresponde a este activo, familia o límite; reinicia el listado"
            )
        if payload["fingerprint"] != inventory.fingerprint:
            raise ExperimentCursorStaleError(
                "el inventario de estudios cambió desde el cursor; reinicia el listado"
            )
        run_id = self._canonical_uuid(str(payload["last_run_id"]))
        if run_id is None:
            raise ValueError("cursor is not a valid opaque token")
        key = (str(payload["last_family"]), str(run_id))
        for index, candidate in enumerate(inventory.candidates):
            if candidate.key == key:
                return index + 1
        raise ExperimentCursorStaleError(
            "el run del cursor ya no está disponible; reinicia el listado"
        )


__all__ = [
    "ExperimentCursorStaleError",
    "ExperimentReportCapacityError",
    "ExperimentReportConflictError",
    "ExperimentReportError",
    "ExperimentReportNotFoundError",
    "ExperimentReportService",
]
