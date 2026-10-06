"""Resumable verified v1 observation to observation v2 import.

The importer walks a restored v1 workspace opened read-only and a raw v2
staging whose raw corpus is already complete and verified, one page of at most
256 observation identifiers at a time. Every page is hydrated from the source,
written idempotently to the typed ``normalized_observations_v2`` table,
re-read from the staging and only then confirmed with an atomic checkpoint.
A crash between the write and the checkpoint replays the same page without
duplicating deterministic identities. Reopening validates the confirmed
prefix against both sides in bounded streaming before continuing; a foreign
checkpoint fails closed. Completion compares both inventories page by page
with exact ID sets, canonical content, per-source/frequency counts and an
ordered digest, without loading the corpus at once.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import NormalizedObservation
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.observation_v2 import (
    ObservationV2Error,
    ObservationV2Store,
    ensure_observation_v2_table,
    observation_to_row,
)
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer, RawV2ImportSummary
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

OBSERVATION_V2_IMPORT_STATE_FORMAT = "observation-v2-import-state-v1"
OBSERVATION_V2_IMPORT_SUMMARY_SCHEMA = "observation-v2-import-summary-v1"
OBSERVATION_V2_IMPORT_POLICY_VERSION = "observation-v2-import-v1"
_MAX_OBSERVATION_IMPORT_PAGE = 256
_OBSERVATION_IMPORT_STATE_FILENAME = "observation-v2-import-state.json"


class ObservationV2ImportError(StorageError):
    """Raised when an observation v2 import page or checkpoint cannot be trusted."""


class ObservationV2ImportCursor(ContractModel):
    """Stable import cursor over the ``(available_at, observation_id)`` order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available_at: UTCDateTime | None = None
    observation_id: UUID | None = None


class ObservationV2ImportState(ContractModel):
    """Atomic progress record for one resumable observation import."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["observation-v2-import-state-v1"] = OBSERVATION_V2_IMPORT_STATE_FORMAT
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    raw_digest: NonEmptyStr
    destination: NonEmptyStr
    staging_id: str | None = None
    staging_format: Literal["raw-v2-staging-v1"] = "raw-v2-staging-v1"
    policy_version: Literal["observation-v2-import-v1"] = OBSERVATION_V2_IMPORT_POLICY_VERSION
    cursor: ObservationV2ImportCursor = ObservationV2ImportCursor()
    confirmed_count: int = Field(ge=0)
    accumulated_digest: NonEmptyStr
    page_limit: int = Field(ge=1, le=256)
    updated_at: UTCDateTime

    @field_validator("page_limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("page_limit must be an integer")
        return value


class ObservationV2ImportSummary(ContractModel):
    """Typed outcome of one verified observation v1 to v2 import."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["observation-v2-import-summary-v1"] = (
        OBSERVATION_V2_IMPORT_SUMMARY_SCHEMA
    )
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    raw_digest: NonEmptyStr
    destination: NonEmptyStr
    policy_version: Literal["observation-v2-import-v1"] = OBSERVATION_V2_IMPORT_POLICY_VERSION
    complete: bool
    imported_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    corpus_digest: NonEmptyStr
    counts_by_source: dict[str, int]
    counts_by_frequency: dict[str, int]
    max_page_requested: int = Field(ge=1, le=256)
    max_page_hydrated: int = Field(ge=0, le=256)
    traceability_verified: Literal[True] = True


def observation_empty_digest() -> str:
    """Return the ordered digest of an empty observation corpus."""
    return hashlib.sha256(b"").hexdigest()


def observation_extend_digest(accumulated: str, checksum: str) -> str:
    """Fold one observation checksum into the ordered corpus digest."""
    if len(accumulated) != 64 or len(checksum) != 64:
        raise ObservationV2ImportError("observation import digest must be SHA-256 hex")
    return hashlib.sha256(f"{accumulated}:{checksum}".encode()).hexdigest()


def observation_checksum(observation: NormalizedObservation) -> str:
    """Return the canonical checksum bound into the import digest."""
    return sha256_hex(canonical_json_bytes(observation))


def canonical_observation_digest(observations: list[NormalizedObservation]) -> str:
    """Fold one page of verified observations in stable order."""
    digest = observation_empty_digest()
    for observation in sorted(observations, key=lambda item: str(item.observation_id)):
        digest = observation_extend_digest(digest, observation_checksum(observation))
    return digest


class ObservationV2Importer:
    """Import v1 observations into typed v2 staging with resume and verification."""

    def __init__(
        self,
        source: LocalStorage,
        staging: RawV2Staging,
        *,
        source_workspace_id: str,
        source_fingerprint: str,
        raw_digest: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        source.require_open()
        if not source.read_only:
            raise ObservationV2ImportError("observation import source must be read-only")
        if staging.is_open:
            staging.require_open_for_import()
        self._source = source
        self._staging = staging
        self._workspace_id = source_workspace_id
        self._fingerprint = source_fingerprint
        self._raw_digest = raw_digest
        self._clock = clock or (lambda: datetime.now(UTC))
        self._destination_key = str(staging.destination.absolute())
        self._staging_id = staging.staging_id
        if not self._workspace_id.strip():
            raise ObservationV2ImportError("observation import workspace identity is required")
        if not self._fingerprint.strip():
            raise ObservationV2ImportError("observation import fingerprint is required")
        if len(self._raw_digest) != 64:
            raise ObservationV2ImportError("observation import raw digest must be SHA-256 hex")

    def run(
        self,
        *,
        page_limit: int = _MAX_OBSERVATION_IMPORT_PAGE,
        fail_after_page: int | None = None,
    ) -> ObservationV2ImportSummary:
        """Walk every source page, confirm checkpoints and verify the corpus."""
        if isinstance(page_limit, bool) or not isinstance(page_limit, int):
            raise ObservationV2ImportError("observation import page limit must be an integer")
        if page_limit < 1 or page_limit > _MAX_OBSERVATION_IMPORT_PAGE:
            raise ObservationV2ImportError(
                "observation import page limit must be between 1 and 256"
            )
        if fail_after_page is not None and (
            isinstance(fail_after_page, bool) or not isinstance(fail_after_page, int)
        ):
            raise ObservationV2ImportError("fail_after_page must be an integer")
        self._require_disjoint_destination()
        self._require_complete_raw()
        state = self._load_or_initialize_state(page_limit)
        imported = state.confirmed_count
        reused_total = 0
        max_hydrated = 0
        pages_confirmed = 0
        while True:
            page = self._source.observations.list_observation_import_page(
                limit=state.page_limit,
                after_available_at=state.cursor.available_at,
                after_observation_id=state.cursor.observation_id,
            )
            if not page:
                break
            max_hydrated = max(max_hydrated, len(page))
            source_records = self._source.observations.get_many(page)
            ordered = [source_records[observation_id] for observation_id in page]
            try:
                receipt = self._staging.save_observations(ordered)
            except ObservationV2Error as error:
                raise ObservationV2ImportError(str(error)) from error
            if receipt.created_count + receipt.reused_count != len(page):
                raise ObservationV2ImportError("observation import page did not settle")
            reread = self._staging.get_observations(page)
            checksums: list[str] = []
            for observation_id in page:
                source_observation = source_records[observation_id]
                staged_observation = reread[observation_id]
                if staged_observation != source_observation:
                    raise ObservationV2ImportError("staged observation differs from source")
                self._assert_same_raw_reference(source_observation, staged_observation)
                checksums.append(observation_checksum(source_observation))
            digest = state.accumulated_digest
            for checksum in checksums:
                digest = observation_extend_digest(digest, checksum)
            last = source_records[page[-1]]
            state = self._write_state(
                cursor=ObservationV2ImportCursor(
                    available_at=last.available_at,
                    observation_id=last.observation_id,
                ),
                confirmed_count=state.confirmed_count + len(page),
                accumulated_digest=digest,
                page_limit=state.page_limit,
            )
            imported = state.confirmed_count
            reused_total += receipt.reused_count
            pages_confirmed += 1
            if fail_after_page is not None and pages_confirmed > fail_after_page:
                raise ObservationV2ImportError("observation import interrupted before next page")
        return self._verify_completion(
            state,
            imported_count=imported,
            reused_count=reused_total,
            max_page_requested=state.page_limit,
            max_page_hydrated=max_hydrated,
        )

    def verify_complete(self) -> ObservationV2ImportSummary:
        """Verify raw and observation checkpoints against both live inventories."""
        self._require_disjoint_destination()
        raw_summary: RawV2ImportSummary = RawV2Importer(
            self._source,
            self._staging,
            source_workspace_id=self._workspace_id,
            source_fingerprint=self._fingerprint,
            clock=self._clock,
        ).verify_complete()
        if not raw_summary.complete or raw_summary.corpus_digest != self._raw_digest:
            raise ObservationV2ImportError("verified raw corpus does not match observation import")
        path = self._state_path()
        if path.is_symlink() or not path.is_file():
            raise ObservationV2ImportError("complete observation import state is missing or unsafe")
        try:
            state = ObservationV2ImportState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ObservationV2ImportError(
                "complete observation import state is incompatible"
            ) from error
        self._check_state_binding(state)
        if state.raw_digest != raw_summary.corpus_digest:
            raise ObservationV2ImportError(
                "observation checkpoint does not bind to verified raw corpus"
            )
        self._validate_confirmed_prefix(state)
        return self._verify_completion(
            state,
            imported_count=state.confirmed_count,
            reused_count=0,
            max_page_requested=state.page_limit,
            max_page_hydrated=0,
        )

    def _require_disjoint_destination(self) -> None:
        source_root = self._source.paths.root.resolve()
        staging_root = Path(self._destination_key).resolve(strict=False)
        if staging_root == source_root or staging_root in source_root.parents:
            raise ObservationV2ImportError("observation import destination must be disjoint")
        if source_root in staging_root.parents:
            raise ObservationV2ImportError("observation import destination must be disjoint")
        if staging_root.is_symlink():
            raise ObservationV2ImportError("observation import destination must not be a symlink")

    def _require_complete_raw(self) -> None:
        staging = self._staging
        marker = staging.destination / "raw-v2-import-state.json"
        if not marker.is_file() or marker.is_symlink():
            raise ObservationV2ImportError("observation import requires a complete raw import")
        try:
            document = marker.read_text(encoding="utf-8")
        except OSError as error:
            raise ObservationV2ImportError(
                "observation import raw checkpoint is unreadable"
            ) from error
        if self._raw_digest not in document and '"confirmed_count"' not in document:
            raise ObservationV2ImportError("observation import raw checkpoint is incompatible")

    def _state_path(self) -> Path:
        return self._staging.destination / _OBSERVATION_IMPORT_STATE_FILENAME

    def _check_state_binding(self, state: ObservationV2ImportState) -> None:
        if state.source_workspace_id != self._workspace_id:
            raise ObservationV2ImportError("observation import state belongs to another source")
        if state.source_fingerprint != self._fingerprint:
            raise ObservationV2ImportError("observation import state belongs to another source")
        if state.raw_digest != self._raw_digest:
            raise ObservationV2ImportError("observation import raw digest does not match staging")
        if state.staging_id is not None and self._staging_id is not None:
            if state.staging_id != self._staging_id:
                raise ObservationV2ImportError(
                    "observation import state belongs to another staging"
                )
        elif state.destination != self._destination_key:
            raise ObservationV2ImportError("observation import state belongs to another staging")

    def _load_or_initialize_state(self, page_limit: int) -> ObservationV2ImportState:
        path = self._state_path()
        if path.is_symlink():
            raise ObservationV2ImportError("observation import state must not be a symlink")
        if not path.exists():
            return self._write_state(
                cursor=ObservationV2ImportCursor(),
                confirmed_count=0,
                accumulated_digest=observation_empty_digest(),
                page_limit=page_limit,
            )
        try:
            state = ObservationV2ImportState.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ObservationV2ImportError("observation import state is truncated") from error
        self._check_state_binding(state)
        if state.page_limit != page_limit:
            raise ObservationV2ImportError("observation import page limit does not match state")
        self._validate_confirmed_prefix(state)
        return state

    def _validate_confirmed_prefix(self, state: ObservationV2ImportState) -> None:
        if state.confirmed_count == 0:
            if state.cursor.available_at is not None or state.cursor.observation_id is not None:
                raise ObservationV2ImportError("observation import cursor mismatches count")
            if state.accumulated_digest != observation_empty_digest():
                raise ObservationV2ImportError("observation import digest mismatches count")
            return
        if state.cursor.available_at is None or state.cursor.observation_id is None:
            raise ObservationV2ImportError("observation import cursor mismatches count")
        digest = observation_empty_digest()
        confirmed = 0
        source_at: datetime | None = None
        source_id: UUID | None = None
        staged_at: datetime | None = None
        staged_id: UUID | None = None
        last_source_id: UUID | None = None
        while confirmed < state.confirmed_count:
            remaining = state.confirmed_count - confirmed
            source_page = self._source.observations.list_observation_import_page(
                limit=min(state.page_limit, remaining),
                after_available_at=source_at,
                after_observation_id=source_id,
            )
            staged_page = self._staging.list_observation_inventory_page(
                limit=min(state.page_limit, remaining),
                after_available_at=staged_at,
                after_observation_id=staged_id,
            )
            if not source_page or not staged_page or len(source_page) != len(staged_page):
                raise ObservationV2ImportError("observation import prefix mismatches source")
            if [str(item) for item in staged_page] != [str(item) for item in source_page]:
                raise ObservationV2ImportError("observation import prefix mismatches staging")
            source_records = self._source.observations.get_many(source_page)
            staged_records = self._staging.get_observations(staged_page)
            for observation_id in source_page:
                source_observation = source_records[observation_id]
                staged_observation = staged_records[observation_id]
                if staged_observation != source_observation:
                    raise ObservationV2ImportError("confirmed observation prefix diverged")
                digest = observation_extend_digest(digest, observation_checksum(source_observation))
                last_source_id = observation_id
            last_source = source_records[source_page[-1]]
            source_at, source_id = last_source.available_at, last_source.observation_id
            staged_at, staged_id = source_at, source_id
            confirmed += len(source_page)
        if last_source_id != state.cursor.observation_id:
            raise ObservationV2ImportError("observation import cursor mismatches count")
        if digest != state.accumulated_digest:
            raise ObservationV2ImportError("observation import digest mismatches prefix")

    def _write_state(
        self,
        *,
        cursor: ObservationV2ImportCursor,
        confirmed_count: int,
        accumulated_digest: str,
        page_limit: int,
    ) -> ObservationV2ImportState:
        state = ObservationV2ImportState(
            source_workspace_id=self._workspace_id,
            source_fingerprint=self._fingerprint,
            raw_digest=self._raw_digest,
            destination=self._destination_key,
            staging_id=self._staging_id,
            cursor=cursor,
            confirmed_count=confirmed_count,
            accumulated_digest=accumulated_digest,
            page_limit=page_limit,
            updated_at=self._normalized_now(),
        )
        path = self._state_path()
        document = state.model_dump_json() + "\n"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(document, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return state

    def _verify_completion(
        self,
        state: ObservationV2ImportState,
        *,
        imported_count: int,
        reused_count: int,
        max_page_requested: int,
        max_page_hydrated: int,
    ) -> ObservationV2ImportSummary:
        digest = observation_empty_digest()
        counts_by_source: dict[str, int] = {}
        counts_by_frequency: dict[str, int] = {}
        verified = 0
        max_hydrated = max_page_hydrated
        source_at: datetime | None = None
        source_id: UUID | None = None
        staged_at: datetime | None = None
        staged_id: UUID | None = None
        while True:
            source_page = self._source.observations.list_observation_import_page(
                limit=state.page_limit,
                after_available_at=source_at,
                after_observation_id=source_id,
            )
            staged_page = self._staging.list_observation_inventory_page(
                limit=state.page_limit,
                after_available_at=staged_at,
                after_observation_id=staged_id,
            )
            if not source_page and not staged_page:
                break
            if not source_page or not staged_page or len(source_page) != len(staged_page):
                raise ObservationV2ImportError("staged observations mismatch the source")
            max_hydrated = max(max_hydrated, len(source_page))
            if [str(item) for item in staged_page] != [str(item) for item in source_page]:
                raise ObservationV2ImportError("staged observations mismatch the source")
            source_records = self._source.observations.get_many(source_page)
            staged_records = self._staging.get_observations(staged_page)
            for observation_id in source_page:
                source_observation = source_records[observation_id]
                staged_observation = staged_records[observation_id]
                if staged_observation != source_observation:
                    raise ObservationV2ImportError("staged observation differs from source")
                self._assert_same_projection(source_observation, staged_observation)
                digest = observation_extend_digest(digest, observation_checksum(source_observation))
                counts_by_source[source_observation.source.source_id] = (
                    counts_by_source.get(source_observation.source.source_id, 0) + 1
                )
                counts_by_frequency[source_observation.frequency.value] = (
                    counts_by_frequency.get(source_observation.frequency.value, 0) + 1
                )
            last_source = source_records[source_page[-1]]
            source_at, source_id = last_source.available_at, last_source.observation_id
            staged_at, staged_id = source_at, source_id
            verified += len(source_page)
        if digest != state.accumulated_digest:
            raise ObservationV2ImportError("observation digest mismatches verified corpus")
        if imported_count != verified:
            raise ObservationV2ImportError("observation count mismatches the source inventory")
        return ObservationV2ImportSummary(
            source_workspace_id=self._workspace_id,
            source_fingerprint=self._fingerprint,
            raw_digest=self._raw_digest,
            destination=self._destination_key,
            complete=True,
            imported_count=imported_count,
            reused_count=reused_count,
            corpus_digest=digest,
            counts_by_source=counts_by_source,
            counts_by_frequency=counts_by_frequency,
            max_page_requested=max_page_requested,
            max_page_hydrated=max_hydrated,
            traceability_verified=True,
        )

    @staticmethod
    def _assert_same_raw_reference(
        source_observation: NormalizedObservation,
        staged_observation: NormalizedObservation,
    ) -> None:
        if (
            staged_observation.raw_record_id != source_observation.raw_record_id
            or staged_observation.source.source_id != source_observation.source.source_id
            or staged_observation.available_at != source_observation.available_at
            or staged_observation.value != source_observation.value
        ):
            raise ObservationV2ImportError("staged observation metadata mismatches source")

    @staticmethod
    def _assert_same_projection(
        source_observation: NormalizedObservation,
        staged_observation: NormalizedObservation,
    ) -> None:
        if observation_to_row(source_observation) != observation_to_row(staged_observation):
            raise ObservationV2ImportError("staged observation projection mismatches source")

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ObservationV2ImportError("observation import clock must be timezone-aware")
        return value.astimezone(UTC)

    def store(self) -> ObservationV2Store:
        """Expose the typed staging store for verifiers on the same connection."""
        ensure_observation_v2_table(self._staging._connection, create=False)
        return ObservationV2Store(self._staging._connection)


__all__ = [
    "OBSERVATION_V2_IMPORT_POLICY_VERSION",
    "OBSERVATION_V2_IMPORT_STATE_FORMAT",
    "OBSERVATION_V2_IMPORT_SUMMARY_SCHEMA",
    "ObservationV2ImportCursor",
    "ObservationV2ImportError",
    "ObservationV2ImportState",
    "ObservationV2ImportSummary",
    "ObservationV2Importer",
    "canonical_observation_digest",
    "observation_checksum",
    "observation_empty_digest",
    "observation_extend_digest",
]
