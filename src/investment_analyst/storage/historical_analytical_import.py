"""Resumable, verified copy of persisted v1 metric and diagnostic history."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import DiagnosticResult, MetricResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.historical_analytical_archive import HistoricalAnalyticalArchive
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import extend_digest
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.models import WorkspaceManifest

HISTORICAL_ANALYTICAL_IMPORT_STATE_SCHEMA = "historical-analytical-import-state-v1"
HISTORICAL_ANALYTICAL_IMPORT_POLICY_VERSION = "historical-analytical-import-v1"
HISTORICAL_ANALYTICAL_IMPORT_SUMMARY_SCHEMA = "historical-analytical-import-summary-v1"
_STATE_FILENAME = "historical-analytical-import-state.json"
_MAX_PAGE = 256
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class HistoricalAnalyticalImportError(StorageError):
    """Raised when historical analytical import preconditions or checkpoints fail."""


class HistoricalAnalyticalImportCursor(ContractModel):
    """Stable keyset cursor over ``(available_at, UUID)``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    available_at: UTCDateTime
    identifier: UUID


class HistoricalAnalyticalImportState(ContractModel):
    """Portable checkpoint for analytical history imported into one staging identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-analytical-import-state-v1"] = (
        HISTORICAL_ANALYTICAL_IMPORT_STATE_SCHEMA
    )
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    raw_digest: NonEmptyStr
    observation_digest: NonEmptyStr
    staging_id: NonEmptyStr
    policy_version: Literal["historical-analytical-import-v1"] = (
        HISTORICAL_ANALYTICAL_IMPORT_POLICY_VERSION
    )
    phase: Literal["metrics", "diagnostics", "verification", "complete"]
    metric_cursor: HistoricalAnalyticalImportCursor | None = None
    diagnostic_cursor: HistoricalAnalyticalImportCursor | None = None
    metric_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    metric_digest: NonEmptyStr
    diagnostic_digest: NonEmptyStr
    complete: bool
    page_limit: int = Field(ge=1, le=256)
    updated_at: UTCDateTime

    @field_validator("page_limit", mode="before")
    @classmethod
    def reject_boolean_page_limit(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("historical analytical page limit must be an integer")
        return value


class HistoricalAnalyticalCorpusInventory(ContractModel):
    """Streaming digest and comparable categorical counts for one source family."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    count: int = Field(ge=0)
    corpus_digest: NonEmptyStr
    counts_by_asset: dict[str, int]
    counts_by_category: dict[str, int]
    counts_by_uuid_version: dict[str, int]
    max_page_requested: int = Field(ge=1, le=256)
    max_page_hydrated: int = Field(ge=0, le=256)


class HistoricalAnalyticalImportSummary(ContractModel):
    """Typed result of one complete, exact-source historical import."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-analytical-import-summary-v1"] = (
        HISTORICAL_ANALYTICAL_IMPORT_SUMMARY_SCHEMA
    )
    complete: Literal[True] = True
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    raw_digest: NonEmptyStr
    observation_digest: NonEmptyStr
    metric_digest: NonEmptyStr
    diagnostic_digest: NonEmptyStr
    metric_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    metrics_by_asset: dict[str, int]
    metrics_by_key: dict[str, int]
    metrics_by_uuid_version: dict[str, int]
    diagnostics_by_asset: dict[str, int]
    diagnostics_by_mode: dict[str, int]
    metric_created_count: int = Field(ge=0)
    metric_reused_count: int = Field(ge=0)
    diagnostic_created_count: int = Field(ge=0)
    diagnostic_reused_count: int = Field(ge=0)
    max_page_requested: int = Field(ge=1, le=256)
    max_page_hydrated: int = Field(ge=0, le=256)
    traceability_verified: Literal[True] = True

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe public summary."""
        return self.model_dump(mode="json")


def _read_state(path: Path) -> HistoricalAnalyticalImportState:
    if path.is_symlink() or not path.is_file():
        raise HistoricalAnalyticalImportError(
            "historical analytical import state is missing or unsafe"
        )
    try:
        return HistoricalAnalyticalImportState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HistoricalAnalyticalImportError(
            "historical analytical import state is malformed"
        ) from error


def _fold_digest(previous: str, model: MetricResult | DiagnosticResult) -> str:
    return extend_digest(previous, sha256_hex(canonical_json_bytes(model)))


def _increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


class HistoricalAnalyticalImporter:
    """Import all persisted v1 metrics and diagnostics into an isolated archive."""

    def __init__(
        self,
        source: LocalStorage,
        staging: RawV2Staging,
        *,
        page_limit: int = _MAX_PAGE,
        clock: Callable[[], datetime] | None = None,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        source.require_open()
        if not source.read_only:
            raise HistoricalAnalyticalImportError("historical analytical source must be read-only")
        if (
            isinstance(page_limit, bool)
            or not isinstance(page_limit, int)
            or not 1 <= page_limit <= _MAX_PAGE
        ):
            raise HistoricalAnalyticalImportError(
                "historical analytical page limit must be between 1 and 256"
            )
        if not staging.is_open:
            raise HistoricalAnalyticalImportError(
                "historical analytical staging must hold its writer lock"
            )
        staging.require_open_for_import()
        self._source = source
        self._staging = staging
        self._page_limit = page_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._failure_injector = failure_injector
        self._workspace_id = self._read_workspace_id()
        self._staging_id = staging.staging_id
        if self._staging_id is None or not self._staging_id.strip():
            raise HistoricalAnalyticalImportError(
                "historical analytical staging identity is required"
            )
        self._source_root = source.paths.root.parent.resolve()
        self._staging_root = staging.destination.resolve(strict=False)
        if (
            self._source_root == self._staging_root
            or self._source_root in self._staging_root.parents
        ):
            raise HistoricalAnalyticalImportError(
                "historical analytical destination must be disjoint"
            )
        if self._staging_root in self._source_root.parents or self._staging_root.is_symlink():
            raise HistoricalAnalyticalImportError(
                "historical analytical destination must be disjoint"
            )

    def run(self) -> HistoricalAnalyticalImportSummary:
        """Resume from the verified checkpoint or start a complete import."""
        raw_digest, observation_digest = self._verify_raw_and_observation_prerequisites()
        metric_inventory = self._scan_source_metrics()
        diagnostic_inventory = self._scan_source_diagnostics()
        source_fingerprint = self._source_fingerprint(
            raw_digest,
            observation_digest,
            metric_inventory.corpus_digest,
            diagnostic_inventory.corpus_digest,
        )
        archive = self._staging.historical_analytical_archive(create=True)
        state_path = self._staging.destination / _STATE_FILENAME
        if state_path.exists() or state_path.is_symlink():
            state = _read_state(state_path)
            self._check_state_binding(
                state,
                source_fingerprint=source_fingerprint,
                raw_digest=raw_digest,
                observation_digest=observation_digest,
            )
            self._validate_confirmed_prefixes(state, archive)
        else:
            state = self._initial_state(source_fingerprint, raw_digest, observation_digest)
            self._write_state(state)

        metric_created = 0
        metric_reused = 0
        diagnostic_created = 0
        diagnostic_reused = 0
        max_hydrated = 0

        if state.phase == "metrics":
            state, created, reused, hydrated = self._import_metrics(state, archive)
            metric_created += created
            metric_reused += reused
            max_hydrated = max(max_hydrated, hydrated)
            state = self._updated_state(state, phase="diagnostics")
            self._write_state(state)

        if state.phase == "diagnostics":
            state, created, reused, hydrated = self._import_diagnostics(state, archive)
            diagnostic_created += created
            diagnostic_reused += reused
            max_hydrated = max(max_hydrated, hydrated)
            state = self._updated_state(state, phase="verification")
            self._write_state(state)

        if state.phase in ("verification", "complete"):
            self._verify_inventory(
                "metrics",
                archive,
                metric_inventory,
                inject_during_verification=state.phase != "complete",
            )
            self._verify_inventory(
                "diagnostics", archive, diagnostic_inventory, inject_during_verification=False
            )
            archive.verify_complete()
            if state.phase != "complete":
                state = self._updated_state(state, phase="complete", complete=True)
                self._write_state(state)
        if not state.complete or state.phase != "complete":
            raise HistoricalAnalyticalImportError(
                "historical analytical import did not reach completion"
            )

        return HistoricalAnalyticalImportSummary(
            source_workspace_id=self._workspace_id,
            source_fingerprint=source_fingerprint,
            raw_digest=raw_digest,
            observation_digest=observation_digest,
            metric_digest=metric_inventory.corpus_digest,
            diagnostic_digest=diagnostic_inventory.corpus_digest,
            metric_count=metric_inventory.count,
            diagnostic_count=diagnostic_inventory.count,
            metrics_by_asset=metric_inventory.counts_by_asset,
            metrics_by_key=metric_inventory.counts_by_category,
            metrics_by_uuid_version=metric_inventory.counts_by_uuid_version,
            diagnostics_by_asset=diagnostic_inventory.counts_by_asset,
            diagnostics_by_mode=diagnostic_inventory.counts_by_category,
            metric_created_count=metric_created,
            metric_reused_count=metric_reused,
            diagnostic_created_count=diagnostic_created,
            diagnostic_reused_count=diagnostic_reused,
            max_page_requested=self._page_limit,
            max_page_hydrated=max(
                max_hydrated,
                metric_inventory.max_page_hydrated,
                diagnostic_inventory.max_page_hydrated,
            ),
        )

    def _read_workspace_id(self) -> str:
        manifest_path = self._source.paths.root.parent / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise HistoricalAnalyticalImportError(
                "historical source workspace manifest is missing or unsafe"
            )
        try:
            manifest = WorkspaceManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise HistoricalAnalyticalImportError(
                "historical source workspace manifest is invalid"
            ) from error
        return str(manifest.workspace_id)

    def _verify_raw_and_observation_prerequisites(self) -> tuple[str, str]:
        raw_state_path = self._staging.destination / "raw-v2-import-state.json"
        observation_state_path = self._staging.destination / "observation-v2-import-state.json"
        raw_state = _read_json_model(raw_state_path, "raw import state")
        observation_state = _read_json_model(observation_state_path, "observation import state")
        if raw_state.get("source_workspace_id") != self._workspace_id:
            raise HistoricalAnalyticalImportError("raw import belongs to another source workspace")
        if observation_state.get("source_workspace_id") != self._workspace_id:
            raise HistoricalAnalyticalImportError(
                "observation import belongs to another source workspace"
            )
        if raw_state.get("source_fingerprint") != observation_state.get("source_fingerprint"):
            raise HistoricalAnalyticalImportError(
                "raw and observation checkpoints bind to different sources"
            )
        raw_fingerprint = str(raw_state["source_fingerprint"])
        raw_digest = str(observation_state.get("raw_digest", ""))
        from investment_analyst.storage.observation_v2_import import ObservationV2Importer

        summary = ObservationV2Importer(
            self._source,
            self._staging,
            source_workspace_id=self._workspace_id,
            source_fingerprint=raw_fingerprint,
            raw_digest=raw_digest,
            clock=self._clock,
        ).verify_complete()
        return summary.raw_digest, summary.corpus_digest

    def _scan_source_metrics(self) -> HistoricalAnalyticalCorpusInventory:
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        digest = _EMPTY_DIGEST
        count = 0
        by_asset: dict[str, int] = {}
        by_key: dict[str, int] = {}
        by_uuid_version: dict[str, int] = {}
        max_hydrated = 0
        while True:
            page = self._source.metric_results.list_import_page(
                limit=self._page_limit,
                after_available_at=cursor_at,
                after_result_id=cursor_id,
            )
            if not page:
                break
            max_hydrated = max(max_hydrated, len(page))
            found = self._source.metric_results.get_many(page)
            if len(found) != len(page):
                raise HistoricalAnalyticalImportError("historical metric page is incomplete")
            for result_id in page:
                result = found[result_id]
                digest = _fold_digest(digest, result)
                _increment(by_asset, result.asset_id)
                _increment(by_key, result.metric_key)
                _increment(by_uuid_version, str(result.result_id.version or 0))
                count += 1
            last = found[page[-1]]
            cursor_at, cursor_id = last.available_at, last.result_id
        return HistoricalAnalyticalCorpusInventory(
            count=count,
            corpus_digest=digest,
            counts_by_asset=by_asset,
            counts_by_category=by_key,
            counts_by_uuid_version=by_uuid_version,
            max_page_requested=self._page_limit,
            max_page_hydrated=max_hydrated,
        )

    def _scan_source_diagnostics(self) -> HistoricalAnalyticalCorpusInventory:
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        digest = _EMPTY_DIGEST
        count = 0
        by_asset: dict[str, int] = {}
        by_mode: dict[str, int] = {}
        max_hydrated = 0
        while True:
            page = self._source.diagnostics.list_import_page(
                limit=self._page_limit,
                after_available_at=cursor_at,
                after_diagnostic_id=cursor_id,
            )
            if not page:
                break
            max_hydrated = max(max_hydrated, len(page))
            found = self._source.diagnostics.get_many(page)
            if len(found) != len(page):
                raise HistoricalAnalyticalImportError("historical diagnostic page is incomplete")
            for diagnostic_id in page:
                result = found[diagnostic_id]
                digest = _fold_digest(digest, result)
                _increment(by_asset, result.asset_id)
                _increment(by_mode, result.mode.value)
                count += 1
            last = found[page[-1]]
            cursor_at, cursor_id = last.available_at, last.diagnostic_id
        return HistoricalAnalyticalCorpusInventory(
            count=count,
            corpus_digest=digest,
            counts_by_asset=by_asset,
            counts_by_category=by_mode,
            counts_by_uuid_version={},
            max_page_requested=self._page_limit,
            max_page_hydrated=max_hydrated,
        )

    def _source_fingerprint(
        self, raw_digest: str, observation_digest: str, metric_digest: str, diagnostic_digest: str
    ) -> str:
        payload = {
            "policy_version": HISTORICAL_ANALYTICAL_IMPORT_POLICY_VERSION,
            "source_workspace_id": self._workspace_id,
            "raw_digest": raw_digest,
            "observation_digest": observation_digest,
            "metric_digest": metric_digest,
            "diagnostic_digest": diagnostic_digest,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()

    def _initial_state(
        self, source_fingerprint: str, raw_digest: str, observation_digest: str
    ) -> HistoricalAnalyticalImportState:
        return HistoricalAnalyticalImportState(
            source_workspace_id=self._workspace_id,
            source_fingerprint=source_fingerprint,
            raw_digest=raw_digest,
            observation_digest=observation_digest,
            staging_id=str(self._staging_id),
            phase="metrics",
            metric_count=0,
            diagnostic_count=0,
            metric_digest=_EMPTY_DIGEST,
            diagnostic_digest=_EMPTY_DIGEST,
            complete=False,
            page_limit=self._page_limit,
            updated_at=self._now(),
        )

    def _check_state_binding(
        self,
        state: HistoricalAnalyticalImportState,
        *,
        source_fingerprint: str,
        raw_digest: str,
        observation_digest: str,
    ) -> None:
        if (
            state.source_workspace_id != self._workspace_id
            or state.source_fingerprint != source_fingerprint
            or state.raw_digest != raw_digest
            or state.observation_digest != observation_digest
            or state.staging_id != self._staging_id
            or state.page_limit != self._page_limit
        ):
            raise HistoricalAnalyticalImportError(
                "historical analytical checkpoint binds to different inputs"
            )
        if state.complete != (state.phase == "complete"):
            raise HistoricalAnalyticalImportError(
                "historical analytical complete flag and phase disagree"
            )

    def _validate_confirmed_prefixes(
        self, state: HistoricalAnalyticalImportState, archive: HistoricalAnalyticalArchive
    ) -> None:
        self._validate_confirmed_prefix(
            "metrics", state.metric_count, state.metric_digest, state.metric_cursor, archive
        )
        self._validate_confirmed_prefix(
            "diagnostics",
            state.diagnostic_count,
            state.diagnostic_digest,
            state.diagnostic_cursor,
            archive,
        )
        if state.phase == "metrics" and (
            state.diagnostic_count != 0
            or state.diagnostic_cursor is not None
            or state.diagnostic_digest != _EMPTY_DIGEST
        ):
            raise HistoricalAnalyticalImportError(
                "diagnostic checkpoint exists before metric phase completes"
            )
        if state.phase in ("diagnostics", "verification", "complete"):
            source_metrics = self._scan_source_metrics()
            if (
                state.metric_count != source_metrics.count
                or state.metric_digest != source_metrics.corpus_digest
            ):
                raise HistoricalAnalyticalImportError(
                    "confirmed metric phase does not match complete source"
                )
        if state.phase in ("verification", "complete"):
            source_diagnostics = self._scan_source_diagnostics()
            if (
                state.diagnostic_count != source_diagnostics.count
                or state.diagnostic_digest != source_diagnostics.corpus_digest
            ):
                raise HistoricalAnalyticalImportError(
                    "confirmed diagnostic phase does not match complete source"
                )

    def _validate_confirmed_prefix(
        self,
        family: Literal["metrics", "diagnostics"],
        count: int,
        expected_digest: str,
        expected_cursor: HistoricalAnalyticalImportCursor | None,
        archive: HistoricalAnalyticalArchive,
    ) -> None:
        source_cursor_at: datetime | None = None
        source_cursor_id: UUID | None = None
        target_cursor: HistoricalAnalyticalImportCursor | None = None
        digest = _EMPTY_DIGEST
        verified = 0
        while verified < count:
            page_size = min(self._page_limit, count - verified)
            if family == "metrics":
                source_ids = self._source.metric_results.list_import_page(
                    limit=page_size,
                    after_available_at=source_cursor_at,
                    after_result_id=source_cursor_id,
                )
                target_ids = archive.list_metric_ids_page(limit=page_size, after=target_cursor)
                source_models = self._source.metric_results.get_many(source_ids)
                target_models = archive.get_metrics(target_ids)
            else:
                source_ids = self._source.diagnostics.list_import_page(
                    limit=page_size,
                    after_available_at=source_cursor_at,
                    after_diagnostic_id=source_cursor_id,
                )
                target_ids = archive.list_diagnostic_ids_page(limit=page_size, after=target_cursor)
                source_models = self._source.diagnostics.get_many(source_ids)
                target_models = archive.get_diagnostics(target_ids)
            if not source_ids or len(source_ids) != len(target_ids):
                raise HistoricalAnalyticalImportError(
                    f"confirmed {family} prefix differs from source"
                )
            if source_ids != target_ids:
                raise HistoricalAnalyticalImportError(
                    f"confirmed {family} prefix differs from source"
                )
            for identifier in source_ids:
                source_model = source_models[identifier]
                target_model = target_models[identifier]
                if source_model != target_model:
                    raise HistoricalAnalyticalImportError(
                        f"confirmed {family} content differs from source"
                    )
                digest = _fold_digest(digest, source_model)
            last = source_models[source_ids[-1]]
            source_cursor_at = last.available_at
            source_cursor_id = last.result_id if family == "metrics" else last.diagnostic_id
            target_cursor = HistoricalAnalyticalImportCursor(
                available_at=last.available_at,
                identifier=source_cursor_id,
            )
            verified += len(source_ids)
        if count == 0:
            if expected_cursor is not None or expected_digest != _EMPTY_DIGEST:
                raise HistoricalAnalyticalImportError(
                    f"empty {family} cursor or digest is inconsistent"
                )
        elif target_cursor != expected_cursor or digest != expected_digest:
            raise HistoricalAnalyticalImportError(
                f"confirmed {family} cursor or digest is inconsistent"
            )

    def _import_metrics(
        self, state: HistoricalAnalyticalImportState, archive: HistoricalAnalyticalArchive
    ) -> tuple[HistoricalAnalyticalImportState, int, int, int]:
        created_total = 0
        reused_total = 0
        max_hydrated = 0
        cursor_at = state.metric_cursor.available_at if state.metric_cursor else None
        cursor_id = state.metric_cursor.identifier if state.metric_cursor else None
        digest = state.metric_digest
        count = state.metric_count
        while True:
            page = self._source.metric_results.list_import_page(
                limit=self._page_limit,
                after_available_at=cursor_at,
                after_result_id=cursor_id,
            )
            if not page:
                break
            max_hydrated = max(max_hydrated, len(page))
            records = self._source.metric_results.get_many(page)
            ordered = tuple(records[item] for item in page)
            self._inject("before_metric_page_write")
            created, reused = archive.save_metrics(ordered)
            created_total += len(created)
            reused_total += len(reused)
            self._inject("after_metric_page_commit_before_state")
            reread = archive.get_metrics(page)
            for result_id in page:
                source_model = records[result_id]
                if reread[result_id] != source_model:
                    raise HistoricalAnalyticalImportError("archived metric differs from source")
                digest = _fold_digest(digest, source_model)
            last = ordered[-1]
            cursor_at, cursor_id = last.available_at, last.result_id
            state = self._updated_state(
                state,
                metric_cursor=HistoricalAnalyticalImportCursor(
                    available_at=cursor_at, identifier=cursor_id
                ),
                metric_count=count + len(page),
                metric_digest=digest,
            )
            count += len(page)
            self._write_state(state)
            self._inject("after_metric_page_state")
        return state, created_total, reused_total, max_hydrated

    def _import_diagnostics(
        self, state: HistoricalAnalyticalImportState, archive: HistoricalAnalyticalArchive
    ) -> tuple[HistoricalAnalyticalImportState, int, int, int]:
        created_total = 0
        reused_total = 0
        max_hydrated = 0
        cursor_at = state.diagnostic_cursor.available_at if state.diagnostic_cursor else None
        cursor_id = state.diagnostic_cursor.identifier if state.diagnostic_cursor else None
        digest = state.diagnostic_digest
        count = state.diagnostic_count
        while True:
            page = self._source.diagnostics.list_import_page(
                limit=self._page_limit,
                after_available_at=cursor_at,
                after_diagnostic_id=cursor_id,
            )
            if not page:
                break
            max_hydrated = max(max_hydrated, len(page))
            records = self._source.diagnostics.get_many(page)
            ordered = tuple(records[item] for item in page)
            self._inject("before_diagnostic_page_write")
            created, reused = archive.save_diagnostics(ordered)
            created_total += len(created)
            reused_total += len(reused)
            self._inject("after_diagnostic_page_commit_before_state")
            reread = archive.get_diagnostics(page)
            for diagnostic_id in page:
                source_model = records[diagnostic_id]
                if reread[diagnostic_id] != source_model:
                    raise HistoricalAnalyticalImportError("archived diagnostic differs from source")
                digest = _fold_digest(digest, source_model)
            last = ordered[-1]
            cursor_at, cursor_id = last.available_at, last.diagnostic_id
            state = self._updated_state(
                state,
                diagnostic_cursor=HistoricalAnalyticalImportCursor(
                    available_at=cursor_at, identifier=cursor_id
                ),
                diagnostic_count=count + len(page),
                diagnostic_digest=digest,
            )
            count += len(page)
            self._write_state(state)
            self._inject("after_diagnostic_page_state")
        return state, created_total, reused_total, max_hydrated

    def _verify_inventory(
        self,
        family: Literal["metrics", "diagnostics"],
        archive: HistoricalAnalyticalArchive,
        expected: HistoricalAnalyticalCorpusInventory,
        *,
        inject_during_verification: bool,
    ) -> None:
        source_cursor_at: datetime | None = None
        source_cursor_id: UUID | None = None
        target_cursor = None
        digest = _EMPTY_DIGEST
        count = 0
        injected = False
        while True:
            if family == "metrics":
                source_ids = self._source.metric_results.list_import_page(
                    limit=self._page_limit,
                    after_available_at=source_cursor_at,
                    after_result_id=source_cursor_id,
                )
                target_ids = archive.list_metric_ids_page(
                    limit=self._page_limit, after=target_cursor
                )
                source_models = self._source.metric_results.get_many(source_ids)
                target_models = archive.get_metrics(target_ids)
            else:
                source_ids = self._source.diagnostics.list_import_page(
                    limit=self._page_limit,
                    after_available_at=source_cursor_at,
                    after_diagnostic_id=source_cursor_id,
                )
                target_ids = archive.list_diagnostic_ids_page(
                    limit=self._page_limit, after=target_cursor
                )
                source_models = self._source.diagnostics.get_many(source_ids)
                target_models = archive.get_diagnostics(target_ids)
            if not source_ids and not target_ids:
                break
            if not source_ids or source_ids != target_ids:
                raise HistoricalAnalyticalImportError(
                    f"historical {family} inventory differs from source"
                )
            for identifier in source_ids:
                source_model = source_models[identifier]
                if target_models[identifier] != source_model:
                    raise HistoricalAnalyticalImportError(
                        f"historical {family} model differs from source"
                    )
                digest = _fold_digest(digest, source_model)
            last = source_models[source_ids[-1]]
            source_cursor_at = last.available_at
            source_cursor_id = last.result_id if family == "metrics" else last.diagnostic_id
            target_cursor = HistoricalAnalyticalImportCursor(
                available_at=last.available_at, identifier=source_cursor_id
            )
            count += len(source_ids)
            if inject_during_verification and not injected:
                self._inject("during_final_verification")
                injected = True
        if count != expected.count or digest != expected.corpus_digest:
            raise HistoricalAnalyticalImportError(f"historical {family} digest differs from source")

    def _updated_state(
        self, state: HistoricalAnalyticalImportState, **updates: object
    ) -> HistoricalAnalyticalImportState:
        data = state.model_dump(mode="python")
        data.update(updates)
        data["updated_at"] = self._now()
        return HistoricalAnalyticalImportState.model_validate(data)

    def _write_state(self, state: HistoricalAnalyticalImportState) -> None:
        path = self._staging.destination / _STATE_FILENAME
        if path.is_symlink():
            raise HistoricalAnalyticalImportError(
                "historical analytical state must not be a symlink"
            )
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(state.model_dump_json() + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            raise HistoricalAnalyticalImportError(
                "historical analytical state could not be committed"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise HistoricalAnalyticalImportError(
                "historical analytical clock must be timezone-aware"
            )
        return value.astimezone(UTC)

    def _inject(self, point: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(point)


def _read_json_model(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise HistoricalAnalyticalImportError(f"{label} is missing or unsafe")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HistoricalAnalyticalImportError(f"{label} is malformed") from error
    if not isinstance(decoded, dict):
        raise HistoricalAnalyticalImportError(f"{label} is malformed")
    return decoded


def verify_saved_prefix(
    archive: HistoricalAnalyticalArchive,
    state: HistoricalAnalyticalImportState,
) -> None:
    """Recheck a portable checkpoint prefix without access to its v1 source."""
    for family, count, expected_digest, cursor in (
        ("metrics", state.metric_count, state.metric_digest, state.metric_cursor),
        ("diagnostics", state.diagnostic_count, state.diagnostic_digest, state.diagnostic_cursor),
    ):
        target_cursor = None
        digest = _EMPTY_DIGEST
        verified = 0
        while verified < count:
            limit = min(_MAX_PAGE, count - verified)
            if family == "metrics":
                ids = archive.list_metric_ids_page(limit=limit, after=target_cursor)
                records = archive.get_metrics(ids)
            else:
                ids = archive.list_diagnostic_ids_page(limit=limit, after=target_cursor)
                records = archive.get_diagnostics(ids)
            if not ids:
                raise HistoricalAnalyticalImportError("portable analytical prefix is truncated")
            for identifier in ids:
                model = records[identifier]
                digest = _fold_digest(digest, model)
            last = records[ids[-1]]
            last_id = last.result_id if family == "metrics" else last.diagnostic_id
            target_cursor = HistoricalAnalyticalImportCursor(
                available_at=last.available_at, identifier=last_id
            )
            verified += len(ids)
        if verified != count or digest != expected_digest or target_cursor != cursor:
            if count == 0 and cursor is None and expected_digest == _EMPTY_DIGEST:
                continue
            raise HistoricalAnalyticalImportError("portable analytical prefix does not match state")


__all__ = [
    "HISTORICAL_ANALYTICAL_IMPORT_POLICY_VERSION",
    "HISTORICAL_ANALYTICAL_IMPORT_STATE_SCHEMA",
    "HistoricalAnalyticalCorpusInventory",
    "HistoricalAnalyticalImportCursor",
    "HistoricalAnalyticalImportError",
    "HistoricalAnalyticalImporter",
    "HistoricalAnalyticalImportState",
    "HistoricalAnalyticalImportSummary",
    "verify_saved_prefix",
]
