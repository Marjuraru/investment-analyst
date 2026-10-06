"""Resumable verified raw v1 to raw v2 import over bounded keyset pages.

The importer walks a restored v1 workspace opened read-only and a new or
compatible raw v2 staging destination, one page of at most 256 identifiers at
a time. Every page is verified on the source, written idempotently to the
staging, re-read from the staging and only then confirmed with an atomic
checkpoint. A crash between the write and the checkpoint replays the same page
without duplicating deterministic identities. Reopening validates the confirmed
prefix against both sides before continuing; a foreign checkpoint fails closed.
Completion is declared only after both inventories walk page by page with exact
ID sets, canonical SHA-256 bytes, metadata, per-source/schema counts and an
ordered corpus digest.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import RawRecord
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

RAW_V2_IMPORT_STATE_FORMAT = "raw-v2-import-state-v1"
RAW_V2_IMPORT_STATE_FORMAT_V2 = "raw-v2-import-state-v2"
RAW_V2_IMPORT_SUMMARY_SCHEMA = "raw-v2-import-summary-v1"
RAW_V2_IMPORT_POLICY_VERSION = "raw-v2-import-v1"
_MAX_IMPORT_PAGE = 256
_IMPORT_STATE_FILENAME = "raw-v2-import-state.json"


class RawV2ImportError(StorageError):
    """Raised when a raw v2 import precondition, page or checkpoint cannot be trusted."""


class RawV2ImportCursor(ContractModel):
    """Stable import cursor over the ``(received_at, record_id)`` order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    received_at: UTCDateTime | None = None
    record_id: UUID | None = None

    @field_validator("record_id", mode="before")
    @classmethod
    def require_cursor_pair(cls, value: object, info) -> object:
        """Require the cursor fields to travel together."""
        del info
        return value

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class RawV2ImportState(ContractModel):
    """Atomic progress record for one resumable raw v1 to v2 import.

    Format v1 binds the checkpoint to the absolute destination path and only
    resumes on that same path. Format v2 binds it to the stable ``staging_id``
    recorded in the staging marker and remains resumable after a verified
    backup/restore relocates the staging to another path.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["raw-v2-import-state-v1", "raw-v2-import-state-v2"] = RAW_V2_IMPORT_STATE_FORMAT
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    destination: NonEmptyStr
    staging_id: str | None = None
    staging_format: Literal["raw-v2-staging-v1"] = "raw-v2-staging-v1"
    policy_version: Literal["raw-v2-import-v1"] = RAW_V2_IMPORT_POLICY_VERSION
    cursor: RawV2ImportCursor = RawV2ImportCursor()
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

    @field_validator("staging_id", mode="before")
    @classmethod
    def validate_staging_binding(cls, value: object, info) -> object:
        """Accept portable v2 bindings; legacy v1 checkpoints carry no identity."""
        del info
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("staging_id must be a non-empty string")
        return value


class RawV2ImportSummary(ContractModel):
    """Typed outcome of one verified raw v1 to v2 import."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["raw-v2-import-summary-v1"] = RAW_V2_IMPORT_SUMMARY_SCHEMA
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    destination: NonEmptyStr
    policy_version: Literal["raw-v2-import-v1"] = RAW_V2_IMPORT_POLICY_VERSION
    complete: bool
    imported_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    corpus_digest: NonEmptyStr
    counts_by_source: Mapping[str, int]
    counts_by_schema: Mapping[str, int]
    max_page_requested: int = Field(ge=1, le=256)
    max_page_hydrated: int = Field(ge=0, le=256)
    traceability_verified: Literal[True] = True

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def empty_digest() -> str:
    """Return the ordered digest of an empty imported corpus."""
    return hashlib.sha256(b"").hexdigest()


def extend_digest(accumulated: str, checksum: str) -> str:
    """Fold one record checksum into the ordered corpus digest."""
    if len(accumulated) != 64 or len(checksum) != 64:
        raise RawV2ImportError("import digest must be a SHA-256 hex string")
    return hashlib.sha256(f"{accumulated}:{checksum}".encode()).hexdigest()


class RawV2Importer:
    """Import raw v1 records into raw v2 staging with resume and verification."""

    def __init__(
        self,
        source: LocalStorage,
        staging: RawV2Staging,
        *,
        source_workspace_id: str,
        source_fingerprint: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        source.require_open()
        if not source.read_only:
            raise RawV2ImportError("import source must be opened read-only")
        staging_destination = staging.destination
        if staging.is_open:
            staging.require_open_for_import()
        self._source = source
        self._staging = staging
        self._workspace_id = source_workspace_id
        self._fingerprint = source_fingerprint
        self._clock = clock or (lambda: datetime.now(UTC))
        self._destination_key = str(staging_destination.absolute())
        self._staging_id = staging.staging_id
        if not self._workspace_id.strip():
            raise RawV2ImportError("import source workspace identity must be explicit")
        if not self._fingerprint.strip():
            raise RawV2ImportError("import source fingerprint must be explicit")

    def run(
        self,
        *,
        page_limit: int = _MAX_IMPORT_PAGE,
        fail_after_page: int | None = None,
    ) -> RawV2ImportSummary:
        """Walk every source page, confirm checkpoints and verify the corpus."""
        if isinstance(page_limit, bool) or not isinstance(page_limit, int):
            raise RawV2ImportError("import page limit must be an integer")
        if page_limit < 1 or page_limit > _MAX_IMPORT_PAGE:
            raise RawV2ImportError("import page limit must be between 1 and 256")
        if fail_after_page is not None and (
            isinstance(fail_after_page, bool) or not isinstance(fail_after_page, int)
        ):
            raise RawV2ImportError("fail_after_page must be an integer")
        self._require_disjoint_destination()
        state = self._load_or_initialize_state(page_limit)
        imported = state.confirmed_count
        reused_total = 0
        max_hydrated = 0
        pages_confirmed = 0
        while True:
            page = self._source.raw_records.list_import_page(
                limit=state.page_limit,
                after_received_at=state.cursor.received_at,
                after_record_id=state.cursor.record_id,
            )
            if not page:
                break
            max_hydrated = max(max_hydrated, len(page))
            self._source.raw_records.verify_index_integrity(page)
            records = self._source.raw_records.get_many(page)
            ordered = [records[record_id] for record_id in page]
            receipt = self._staging.save_many(ordered)
            if receipt.created_count + receipt.reused_count != len(page):
                raise RawV2ImportError("import page did not settle every record")
            reread = self._staging.get_many(page)
            checksums: list[str] = []
            for record_id in page:
                source_record = records[record_id]
                staged_record = reread[record_id]
                if staged_record != source_record:
                    raise RawV2ImportError("staged record differs from its verified source")
                checksums.append(sha256_hex(canonical_json_bytes(source_record)))
            digest = state.accumulated_digest
            for checksum in checksums:
                digest = extend_digest(digest, checksum)
            last = records[page[-1]]
            state = self._write_state(
                cursor=RawV2ImportCursor(received_at=last.received_at, record_id=last.record_id),
                confirmed_count=state.confirmed_count + len(page),
                accumulated_digest=digest,
                page_limit=state.page_limit,
            )
            imported = state.confirmed_count
            reused_total += receipt.reused_count
            pages_confirmed += 1
            if fail_after_page is not None and pages_confirmed > fail_after_page:
                raise RawV2ImportError("import interrupted before the next page")
        return self._verify_completion(
            state,
            imported_count=imported,
            reused_count=reused_total,
            max_page_requested=state.page_limit,
            max_page_hydrated=max_hydrated,
        )

    def verify_complete(self) -> RawV2ImportSummary:
        """Reverify a completed raw inventory without creating or changing state."""
        self._require_disjoint_destination()
        path = self._state_path()
        if path.is_symlink() or not path.is_file():
            raise RawV2ImportError("complete raw import state is missing or unsafe")
        try:
            state = RawV2ImportState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RawV2ImportError("complete raw import state is incompatible") from error
        self._check_state_binding(state)
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
            raise RawV2ImportError("import destination must be disjoint from the source")
        if source_root in staging_root.parents:
            raise RawV2ImportError("import destination must be disjoint from the source")
        if staging_root.is_symlink():
            raise RawV2ImportError("import destination must not use symbolic links")

    def _state_path(self) -> Path:
        return self._staging.destination / _IMPORT_STATE_FILENAME

    def _is_portable_state(self, state: RawV2ImportState) -> bool:
        """Return whether a checkpoint binds by stable identity instead of path."""
        return state.format == RAW_V2_IMPORT_STATE_FORMAT_V2 and state.staging_id is not None

    def _check_state_binding(self, state: RawV2ImportState) -> None:
        if state.source_workspace_id != self._workspace_id:
            raise RawV2ImportError("import state belongs to another source or destination")
        if state.source_fingerprint != self._fingerprint:
            raise RawV2ImportError("import state belongs to another source or destination")
        if self._is_portable_state(state):
            if self._staging_id is None or state.staging_id != self._staging_id:
                raise RawV2ImportError("import state belongs to another source or destination")
        elif state.destination != self._destination_key:
            raise RawV2ImportError("import state belongs to another source or destination")

    def _load_or_initialize_state(self, page_limit: int) -> RawV2ImportState:
        path = self._state_path()
        if path.is_symlink():
            raise RawV2ImportError("import state must not be a symbolic link")
        if not path.exists():
            return self._write_state(
                cursor=RawV2ImportCursor(),
                confirmed_count=0,
                accumulated_digest=empty_digest(),
                page_limit=page_limit,
            )
        try:
            state = RawV2ImportState.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise RawV2ImportError("import state is truncated or incompatible") from error
        self._check_state_binding(state)
        if state.page_limit != page_limit:
            raise RawV2ImportError("import state page limit does not match this run")
        self._validate_confirmed_prefix(state)
        return state

    def _validate_confirmed_prefix(self, state: RawV2ImportState) -> None:
        if state.confirmed_count == 0:
            if state.cursor.received_at is not None or state.cursor.record_id is not None:
                raise RawV2ImportError("import state cursor does not match its confirmed count")
            if state.accumulated_digest != empty_digest():
                raise RawV2ImportError("import state digest does not match its confirmed count")
            return
        if state.cursor.received_at is None or state.cursor.record_id is None:
            raise RawV2ImportError("import state cursor does not match its confirmed count")
        digest = empty_digest()
        confirmed = 0
        source_at: datetime | None = None
        source_id: UUID | None = None
        staged_at: datetime | None = None
        staged_id: UUID | None = None
        last_source_id: UUID | None = None
        while confirmed < state.confirmed_count:
            remaining = state.confirmed_count - confirmed
            source_page = self._source.raw_records.list_import_page(
                limit=min(state.page_limit, remaining),
                after_received_at=source_at,
                after_record_id=source_id,
            )
            staged_page = self._staging.list_inventory_page(
                limit=min(state.page_limit, remaining),
                after_received_at=staged_at,
                after_record_id=staged_id,
            )
            if not source_page or not staged_page or len(source_page) != len(staged_page):
                raise RawV2ImportError("import state prefix does not match the source inventory")
            if [str(record_id) for record_id in staged_page] != [
                str(record_id) for record_id in source_page
            ]:
                raise RawV2ImportError("import state prefix does not match the staged inventory")
            source_records = self._source.raw_records.get_many(source_page)
            staged_records = self._staging.get_many(staged_page)
            for record_id in source_page:
                source_record = source_records[record_id]
                staged_record = staged_records[record_id]
                if staged_record != source_record:
                    raise RawV2ImportError("confirmed prefix differs between source and staging")
                digest = extend_digest(digest, sha256_hex(canonical_json_bytes(source_record)))
                last_source_id = record_id
            last_source = source_records[source_page[-1]]
            source_at, source_id = last_source.received_at, last_source.record_id
            staged_at, staged_id = source_at, source_id
            confirmed += len(source_page)
        if last_source_id != state.cursor.record_id:
            raise RawV2ImportError("import state cursor does not match its confirmed count")
        if digest != state.accumulated_digest:
            raise RawV2ImportError("import state digest does not match its confirmed prefix")

    def _write_state(
        self,
        *,
        cursor: RawV2ImportCursor,
        confirmed_count: int,
        accumulated_digest: str,
        page_limit: int,
    ) -> RawV2ImportState:
        if self._staging_id is None:
            state_format: str = RAW_V2_IMPORT_STATE_FORMAT
            binding_id: str | None = None
        else:
            state_format = RAW_V2_IMPORT_STATE_FORMAT_V2
            binding_id = self._staging_id
        state = RawV2ImportState(
            format=state_format,  # type: ignore[arg-type]
            source_workspace_id=self._workspace_id,
            source_fingerprint=self._fingerprint,
            destination=self._destination_key,
            staging_id=binding_id,
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
        state: RawV2ImportState,
        *,
        imported_count: int,
        reused_count: int,
        max_page_requested: int,
        max_page_hydrated: int,
    ) -> RawV2ImportSummary:
        digest = empty_digest()
        counts_by_source: dict[str, int] = {}
        counts_by_schema: dict[str, int] = {}
        verified = 0
        max_hydrated = max_page_hydrated
        source_at: datetime | None = None
        source_id: UUID | None = None
        staged_at: datetime | None = None
        staged_id: UUID | None = None
        while True:
            source_page = self._source.raw_records.list_import_page(
                limit=state.page_limit,
                after_received_at=source_at,
                after_record_id=source_id,
            )
            staged_page = self._staging.list_inventory_page(
                limit=state.page_limit,
                after_received_at=staged_at,
                after_record_id=staged_id,
            )
            if not source_page and not staged_page:
                break
            if not source_page or not staged_page or len(source_page) != len(staged_page):
                raise RawV2ImportError("staged inventory does not match the source inventory")
            max_hydrated = max(max_hydrated, len(source_page))
            if [str(record_id) for record_id in staged_page] != [
                str(record_id) for record_id in source_page
            ]:
                raise RawV2ImportError("staged inventory does not match the source inventory")
            self._source.raw_records.verify_index_integrity(source_page)
            source_records = self._source.raw_records.get_many(source_page)
            staged_records = self._staging.get_many(staged_page)
            for record_id in source_page:
                source_record = source_records[record_id]
                staged_record = staged_records[record_id]
                if staged_record != source_record:
                    raise RawV2ImportError("staged record differs from its verified source")
                self._assert_same_projection(source_record, staged_record)
                digest = extend_digest(digest, sha256_hex(canonical_json_bytes(source_record)))
                counts_by_source[source_record.source.source_id] = (
                    counts_by_source.get(source_record.source.source_id, 0) + 1
                )
                counts_by_schema[source_record.schema_version] = (
                    counts_by_schema.get(source_record.schema_version, 0) + 1
                )
            last_source = source_records[source_page[-1]]
            source_at, source_id = last_source.received_at, last_source.record_id
            staged_at, staged_id = source_at, source_id
            verified += len(source_page)
        if digest != state.accumulated_digest:
            raise RawV2ImportError("import digest does not match the verified corpus")
        if imported_count != verified:
            raise RawV2ImportError("import confirmed count does not match the source inventory")
        return RawV2ImportSummary(
            source_workspace_id=self._workspace_id,
            source_fingerprint=self._fingerprint,
            destination=self._destination_key,
            complete=True,
            imported_count=imported_count,
            reused_count=reused_count,
            corpus_digest=digest,
            counts_by_source=counts_by_source,
            counts_by_schema=counts_by_schema,
            max_page_requested=max_page_requested,
            max_page_hydrated=max_hydrated,
            traceability_verified=True,
        )

    @staticmethod
    def _assert_same_projection(source_record: RawRecord, staged_record: RawRecord) -> None:
        if (
            staged_record.asset_id != source_record.asset_id
            or staged_record.source.source_id != source_record.source.source_id
            or staged_record.schema_version != source_record.schema_version
            or staged_record.event_time != source_record.event_time
            or staged_record.available_at != source_record.available_at
            or staged_record.received_at != source_record.received_at
        ):
            raise RawV2ImportError("staged record metadata does not match its source")

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RawV2ImportError("import clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _missing_error(record_id: UUID, error: Exception) -> RawV2ImportError:
        if isinstance(error, RecordNotFoundError | RecordConflictError):
            return RawV2ImportError(f"import cannot declare a complete corpus: {record_id}")
        return RawV2ImportError("import verification cannot trust its inventory")


__all__ = [
    "RAW_V2_IMPORT_POLICY_VERSION",
    "RAW_V2_IMPORT_STATE_FORMAT",
    "RAW_V2_IMPORT_STATE_FORMAT_V2",
    "RAW_V2_IMPORT_SUMMARY_SCHEMA",
    "RawV2ImportCursor",
    "RawV2ImportError",
    "RawV2ImportState",
    "RawV2ImportSummary",
    "RawV2Importer",
    "empty_digest",
    "extend_digest",
]
