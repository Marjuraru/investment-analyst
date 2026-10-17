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
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import RawRecord
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.raw_v2 import (
    _INDEX_COLUMNS,
    WORKSPACE_RAW_JSON_PROJECTIONS_TABLE,
    RawV2Staging,
    _parse_instant_text,
    _project_13f_fields,
    _project_raw_json_fields,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV2MigrationWorkCheckpoint,
    WorkspaceV2MigrationWorkPhase,
)

RAW_V2_IMPORT_STATE_FORMAT = "raw-v2-import-state-v1"
RAW_V2_IMPORT_STATE_FORMAT_V2 = "raw-v2-import-state-v2"
RAW_V2_IMPORT_SUMMARY_SCHEMA = "raw-v2-import-summary-v1"
RAW_V2_IMPORT_POLICY_VERSION = "raw-v2-import-v1"
_MAX_IMPORT_PAGE = 256
_IMPORT_STATE_FILENAME = "raw-v2-import-state.json"
_VERIFICATION_CHECKPOINT_FILENAME = "raw-v2-import-verification-work.json"


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RawV2ImportError(StorageError):
    """Raised when a raw v2 import precondition, page or checkpoint cannot be trusted."""


class RawV2ImportProgress(ContractModel):
    """Safe, bounded progress evidence emitted by a raw import execution.

    The event deliberately carries counters only.  Record identifiers, raw
    payloads, environment variables and filesystem contents are never exposed
    to an operational supervisor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase: Literal[
        "prefix_validation_started",
        "prefix_validation_page_verified",
        "prefix_validation_completed",
        "import_page_started",
        "import_page_checkpointed",
        "completion_validation_started",
        "completion_validation_page_verified",
        "completion_validation_completed",
    ]
    confirmed_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    page_size: int = Field(ge=0, le=_MAX_IMPORT_PAGE)

    def to_json_dict(self) -> dict[str, object]:
        """Return a report-safe event document."""
        return self.model_dump(mode="json")


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
    counts_by_source: Mapping[NonEmptyStr, int] = Field(default_factory=dict)
    counts_by_schema: Mapping[NonEmptyStr, int] = Field(default_factory=dict)
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
        historic_source_fingerprint: str | None = None,
        epoch: UUID | None = None,
        destination_connection: DuckDBPyConnection | None = None,
        clock: Callable[[], datetime] | None = None,
        progress: Callable[[RawV2ImportProgress], None] | None = None,
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
        self._historic_fingerprint = historic_source_fingerprint
        self._epoch = epoch
        self._destination_connection = destination_connection
        if destination_connection is not None:
            if epoch is None:
                raise RawV2ImportError("bounded epoch reader requires an epoch")
            current = destination_connection.execute("SELECT current_database()").fetchone()
            databases = destination_connection.execute("PRAGMA database_list").fetchall()
            if current is None or not any(
                str(row[1]) == str(current[0])
                and row[2]
                and Path(str(row[2])).resolve().parent == staging_destination.resolve()
                for row in databases
            ):
                raise RawV2ImportError("epoch reader connection belongs to another staging root")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._progress = progress
        self._destination_key = str(staging_destination.absolute())
        self._staging_id = staging.staging_id
        if not self._workspace_id.strip():
            raise RawV2ImportError("import source workspace identity must be explicit")
        if not self._fingerprint.strip():
            raise RawV2ImportError("import source fingerprint must be explicit")
        if self._historic_fingerprint is not None and not self._historic_fingerprint.strip():
            raise RawV2ImportError("historic source fingerprint must be non-empty")

    def _verify_epoch_page(
        self,
        records: Mapping[UUID, RawRecord],
        *,
        allow_writes: bool,
    ) -> tuple[dict[UUID, str], int]:
        """Compare selected canonical bytes and projections once, in bounded SQL batches.

        Source models have already passed the v1 reader's hash/identity checks.
        Equal canonical bytes prove the same typed model without decoding a
        second copy; projections are compared using the staging's own extractors.
        Only absent IDs reach the ordinary writer API.
        """
        connection = self._destination_connection
        if connection is None or not 1 <= len(records) <= _MAX_IMPORT_PAGE:
            raise RawV2ImportError("bounded raw verifier is not bound to an epoch page")
        identifiers = [str(identifier) for identifier in records]
        placeholders = ", ".join("?" for _ in identifiers)
        columns = ", ".join(_INDEX_COLUMNS)

        def selected_rows() -> dict[UUID, tuple[object, ...]]:
            rows = connection.execute(
                f"SELECT {columns} FROM raw_v2_index WHERE record_id IN ({placeholders})",
                identifiers,
            ).fetchall()
            return {UUID(str(row[0])): row for row in rows}

        indexed = selected_rows()
        reused = len(indexed)
        missing = [record for identifier, record in records.items() if identifier not in indexed]
        if missing:
            if not allow_writes:
                raise RawV2ImportError("confirmed raw epoch page has missing destination records")
            receipt = self._staging.save_many(missing)
            if receipt.created_count + receipt.reused_count != len(missing):
                raise RawV2ImportError("epoch raw write did not settle every missing ID")
            reused += receipt.reused_count
            indexed = selected_rows()
        if set(indexed) != set(records):
            raise RawV2ImportError("epoch raw destination ID coverage differs")
        projection_table = (
            connection.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name=?",
                [WORKSPACE_RAW_JSON_PROJECTIONS_TABLE],
            ).fetchone()
            is not None
        )
        projections: dict[UUID, dict[str, str]] = {}
        if projection_table:
            rows = connection.execute(
                "SELECT record_id, field_name, field_value "
                f"FROM {WORKSPACE_RAW_JSON_PROJECTIONS_TABLE} "
                f"WHERE record_id IN ({placeholders})",
                identifiers,
            ).fetchall()
            for identifier, name, value in rows:
                projections.setdefault(UUID(str(identifier)), {})[str(name)] = str(value)
        checksums: dict[UUID, str] = {}
        for identifier, record in records.items():
            row = indexed[identifier]
            (
                _,
                relative_path,
                checksum,
                asset_id,
                source_id,
                event_at,
                available_at,
                received_at,
                schema,
                manager,
                report,
            ) = row
            if not isinstance(relative_path, str) or not isinstance(checksum, str):
                raise RawV2ImportError("epoch raw destination metadata is malformed")
            canonical = canonical_json_bytes(record)
            if (
                sha256_hex(canonical) != checksum
                or self._staging._read_blob(relative_path, checksum) != canonical
            ):
                raise RecordConflictError("epoch raw canonical bytes differ from the source")
            if (
                record.asset_id != asset_id
                or record.source.source_id != source_id
                or record.schema_version != schema
                or record.event_time != _parse_instant_text(event_at)
                or record.available_at != _parse_instant_text(available_at)
                or record.received_at != _parse_instant_text(received_at)
                or _project_13f_fields(record) != (manager, report)
                or projection_table
                and projections.get(identifier, {}) != dict(_project_raw_json_fields(record))
            ):
                raise RawV2ImportError(
                    "epoch raw destination projections differ from canonical source"
                )
            checksums[identifier] = checksum
        return checksums, reused

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
        return self._run_shared_pass(
            state,
            allow_writes=True,
            fail_after_page=fail_after_page,
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
        try:
            self._check_state_binding(state)
        except RawV2ImportError:
            state = self._transition_confirm_and_rebind(state, state.page_limit)
        return self._run_shared_pass(state, allow_writes=False, fail_after_page=None)

    def _run_shared_pass(
        self,
        state: RawV2ImportState,
        *,
        allow_writes: bool,
        fail_after_page: int | None,
    ) -> RawV2ImportSummary:
        """Read each source record once while verifying, resuming and closing.

        A checkpointed prefix is compared against staging during this traversal.
        Pages after it are saved, reread and checkpointed before the traversal
        advances.  The same hydrated source models feed the final digest and
        counts, so resuming cannot add a second deep source walk.
        """
        original_confirmed = state.confirmed_count
        terminal = self._load_f_terminal_checkpoint(state) if allow_writes else None
        verification_cursor, verified, digest = (
            terminal
            if terminal is not None
            else self._load_verification_checkpoint(state)
            if allow_writes
            else (None, 0, empty_digest())
        )
        if original_confirmed > 0:
            self._emit(
                "prefix_validation_started",
                confirmed_count=verified,
                page_count=0,
                page_size=0,
            )
        if verified == original_confirmed and verified > 0:
            if (
                verification_cursor is None
                or state.cursor.record_id != verification_cursor.record_id
                or state.cursor.received_at != verification_cursor.received_at
                or digest != state.accumulated_digest
            ):
                raise RawV2ImportError("verification checkpoint does not match import state")
            self._emit(
                "prefix_validation_completed",
                confirmed_count=verified,
                page_count=(verified + state.page_limit - 1) // state.page_limit,
                page_size=0,
            )
        counts_by_source = dict(state.counts_by_source) if verified > 0 else {}
        counts_by_schema = dict(state.counts_by_schema) if verified > 0 else {}
        source_at = verification_cursor.received_at if verification_cursor is not None else None
        source_id = verification_cursor.record_id if verification_cursor is not None else None
        max_hydrated = 0
        prefix_pages = 0
        imported_pages = 0
        reused_total = 0
        last_record_id: UUID | None = source_id
        epoch_inventory = None
        if self._epoch is not None:
            epoch_inventory = f"raw_epoch_inventory_{self._epoch.hex}"
            self._source.store.connection.execute(
                f"CREATE OR REPLACE TEMP TABLE {epoch_inventory} AS "
                "SELECT record_id, row_number() OVER (ORDER BY received_at, record_id) AS ordinal "
                "FROM raw_record_index"
            )
        while True:
            if epoch_inventory is None:
                page = self._source.raw_records.list_import_page(
                    limit=state.page_limit,
                    after_received_at=source_at,
                    after_record_id=source_id,
                )
                staged_page = self._staging.list_inventory_page(
                    limit=state.page_limit,
                    after_received_at=source_at,
                    after_record_id=source_id,
                )
            else:
                rows = self._source.store.connection.execute(
                    f"SELECT record_id FROM {epoch_inventory} WHERE ordinal > ? AND ordinal <= ? "
                    "ORDER BY ordinal",
                    [verified, verified + state.page_limit],
                ).fetchall()
                page = [UUID(row[0]) for row in rows]
                # save_many performs the bounded identity anti-join. Each
                # selected model is compared after reuse/write; final cardinality
                # rejects destination-only IDs without a second payload walk.
                staged_page = page
            if not page:
                if staged_page:
                    raise RawV2ImportError("staged inventory has records absent from the source")
                break
            max_hydrated = max(max_hydrated, len(page))
            records = self._source.raw_records.get_many(page)
            is_confirmed_prefix = verified < original_confirmed
            epoch_checksums: dict[UUID, str] | None = None
            if self._destination_connection is not None:
                if is_confirmed_prefix and verified + len(page) > original_confirmed:
                    raise RawV2ImportError("import state does not end on a confirmed page")
                if not is_confirmed_prefix:
                    self._emit(
                        "import_page_started",
                        confirmed_count=state.confirmed_count,
                        page_count=imported_pages + 1,
                        page_size=len(page),
                    )
                epoch_checksums, reused = self._verify_epoch_page(
                    records,
                    allow_writes=allow_writes and not is_confirmed_prefix,
                )
                if not is_confirmed_prefix:
                    reused_total += reused
                reread = None
            elif is_confirmed_prefix:
                if verified + len(page) > original_confirmed:
                    raise RawV2ImportError("import state does not end on a confirmed page")
                if [str(record_id) for record_id in staged_page] != [
                    str(record_id) for record_id in page
                ]:
                    raise RawV2ImportError(
                        "import state prefix does not match the staged inventory"
                    )
                reread = self._staging.get_many(staged_page)
            else:
                if not allow_writes:
                    raise RawV2ImportError("complete raw import state does not cover the source")
                if (
                    self._epoch is None
                    and staged_page
                    and [str(record_id) for record_id in staged_page]
                    != [str(record_id) for record_id in page]
                ):
                    raise RawV2ImportError("staged inventory has unconfirmed records")
                self._emit(
                    "import_page_started",
                    confirmed_count=state.confirmed_count,
                    page_count=imported_pages + 1,
                    page_size=len(page),
                )
                receipt = self._staging.save_many([records[record_id] for record_id in page])
                if receipt.created_count + receipt.reused_count != len(page):
                    raise RawV2ImportError("import page did not settle every record")
                reread = self._staging.get_many(page)
                reused_total += receipt.reused_count
            for record_id in page:
                source_record = records[record_id]
                if reread is not None and reread[record_id] != source_record:
                    raise RawV2ImportError("staged record differs from its verified source")
                checksum = (
                    epoch_checksums[record_id]
                    if epoch_checksums is not None
                    else sha256_hex(canonical_json_bytes(source_record))
                )
                digest = extend_digest(digest, checksum)
                counts_by_source[source_record.source.source_id] = (
                    counts_by_source.get(source_record.source.source_id, 0) + 1
                )
                counts_by_schema[source_record.schema_version] = (
                    counts_by_schema.get(source_record.schema_version, 0) + 1
                )
            last = records[page[-1]]
            source_at, source_id = last.received_at, last.record_id
            last_record_id = last.record_id
            verified += len(page)
            if is_confirmed_prefix:
                prefix_pages += 1
                if allow_writes and self._can_resume_verified_prefix():
                    state = self._write_state(
                        cursor=state.cursor,
                        confirmed_count=state.confirmed_count,
                        accumulated_digest=state.accumulated_digest,
                        counts_by_source=counts_by_source,
                        counts_by_schema=counts_by_schema,
                        page_limit=state.page_limit,
                    )
                if allow_writes:
                    self._write_verification_checkpoint(
                        state,
                        cursor=RawV2ImportCursor(
                            received_at=last.received_at,
                            record_id=last.record_id,
                        ),
                        confirmed_count=verified,
                        accumulated_digest=digest,
                    )
                self._emit(
                    "prefix_validation_page_verified",
                    confirmed_count=verified,
                    page_count=prefix_pages,
                    page_size=len(page),
                )
                if verified == original_confirmed:
                    if (
                        state.cursor.record_id != last_record_id
                        or state.cursor.received_at != last.received_at
                        or digest != state.accumulated_digest
                    ):
                        raise RawV2ImportError(
                            "import state digest does not match its confirmed prefix"
                        )
                    self._emit(
                        "prefix_validation_completed",
                        confirmed_count=verified,
                        page_count=prefix_pages,
                        page_size=0,
                    )
                continue
            state = self._write_state(
                cursor=RawV2ImportCursor(received_at=last.received_at, record_id=last.record_id),
                confirmed_count=verified,
                accumulated_digest=digest,
                counts_by_source=counts_by_source,
                counts_by_schema=counts_by_schema,
                page_limit=state.page_limit,
            )
            imported_pages += 1
            self._emit(
                "import_page_checkpointed",
                confirmed_count=state.confirmed_count,
                page_count=imported_pages,
                page_size=len(page),
            )
            if fail_after_page is not None and imported_pages > fail_after_page:
                raise RawV2ImportError("import interrupted before the next page")
        if self._epoch is not None and self._staging.count_records() != verified:
            raise RawV2ImportError("staged inventory has records absent from the source epoch")
        if verified != state.confirmed_count or (
            verified > 0 and state.cursor.record_id != last_record_id
        ):
            raise RawV2ImportError("import state cursor does not match the verified corpus")
        if digest != state.accumulated_digest:
            raise RawV2ImportError("import digest does not match the verified corpus")
        if original_confirmed > 0 and verified < original_confirmed:
            raise RawV2ImportError("import state prefix does not match the source inventory")
        if allow_writes and terminal is None:
            self._write_f_terminal_checkpoint(state)
        self._emit(
            "completion_validation_completed",
            confirmed_count=verified,
            page_count=0,
            page_size=0,
        )
        if epoch_inventory is not None:
            self._source.store.connection.execute(f"DROP TABLE IF EXISTS {epoch_inventory}")
        return RawV2ImportSummary(
            source_workspace_id=self._workspace_id,
            source_fingerprint=self._fingerprint,
            destination=self._destination_key,
            complete=True,
            imported_count=state.confirmed_count,
            reused_count=reused_total,
            corpus_digest=digest,
            counts_by_source=counts_by_source,
            counts_by_schema=counts_by_schema,
            max_page_requested=state.page_limit,
            max_page_hydrated=max_hydrated,
            traceability_verified=True,
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
        if self._epoch is not None:
            return self._staging.destination / f"raw-v2-import-state.F.{self._epoch}.json"
        return self._staging.destination / _IMPORT_STATE_FILENAME

    def _verification_checkpoint_path(self) -> Path:
        if self._epoch is not None:
            return (
                self._staging.destination / f"raw-v2-import-verification-work.F.{self._epoch}.json"
            )
        return self._staging.destination / _VERIFICATION_CHECKPOINT_FILENAME

    def _archive_verification_checkpoint(self) -> None:
        """Preserve a prior-source cursor before rebinding the active state to B."""
        path = self._verification_checkpoint_path()
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise RawV2ImportError("verification checkpoint is unsafe")
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        archive = path.with_name(f"raw-v2-import-verification-work.A.{digest}.json")
        if archive.exists():
            if archive.is_symlink() or archive.read_bytes() != original:
                raise RawV2ImportError("historic verification checkpoint cannot be preserved")
            path.unlink()
            _fsync_parent(path)
            return
        temporary = archive.with_name(f".{archive.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, archive)
            path.unlink()
            _fsync_parent(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _can_resume_verified_prefix(self) -> bool:
        """Return whether the coordinator supplied the immutable B transition binding."""
        return self._historic_fingerprint is not None

    def _f_terminal_checkpoint(
        self, state: RawV2ImportState
    ) -> WorkspaceV2MigrationWorkCheckpoint | None:
        """Bind a completed F comparison to fresh quiescence and actual destination bytes.

        Reopening still reads and hashes every raw blob and projection, but avoids
        hydrating the same source models after a later family was interrupted.
        Partial F prefixes never use this shortcut. Explicit verify_complete
        always performs the independent typed comparison again.
        """
        if self._epoch is None or self._destination_connection is None or not state.confirmed_count:
            return None
        # Local imports avoid the coordinator/importer module dependency cycle.
        from investment_analyst.workspace.service import WorkspaceService
        from investment_analyst.workspace.workspace_v2_migration import _live_quiescent_source

        source_root = self._source.paths.root.parent
        proof = _live_quiescent_source(source_root)
        if proof is None:
            return None
        if (
            proof.cut.operation_id != self._epoch
            or proof.cut.source_fingerprint != self._fingerprint
            or str(proof.cut.workspace_id) != self._workspace_id
        ):
            raise RawV2ImportError("terminal raw verification belongs to another F")
        destination_root = self._staging.destination.parents[1]
        identity = WorkspaceService().manifest(destination_root)
        binding = hashlib.sha256()
        columns = ", ".join(_INDEX_COLUMNS)
        reader = self._destination_connection.cursor()
        try:
            reader.execute(f"SELECT {columns} FROM raw_v2_index ORDER BY received_at, record_id")
            count = 0
            while rows := reader.fetchmany(_MAX_IMPORT_PAGE):
                for row in rows:
                    blob = self._staging._read_blob(str(row[1]), str(row[2]))
                    binding.update(json.dumps(row, separators=(",", ":")).encode())
                    binding.update(hashlib.sha256(blob).digest())
                    count += 1
            if count != state.confirmed_count:
                raise RawV2ImportError("terminal raw coverage changed")
            exists = reader.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name=?",
                [WORKSPACE_RAW_JSON_PROJECTIONS_TABLE],
            ).fetchone()
            if exists:
                reader.execute(
                    "SELECT record_id, field_name, field_value "
                    f"FROM {WORKSPACE_RAW_JSON_PROJECTIONS_TABLE} "
                    "ORDER BY record_id, field_name"
                )
                while rows := reader.fetchmany(_MAX_IMPORT_PAGE):
                    for row in rows:
                        binding.update(json.dumps(row, separators=(",", ":")).encode())
        finally:
            reader.close()
        return self._verification_checkpoint(
            state,
            cursor=state.cursor,
            confirmed_count=state.confirmed_count,
            accumulated_digest=state.accumulated_digest,
        ).model_copy(
            update={
                "operation_id": self._epoch,
                "snapshot_id": self._epoch,
                "source_root": source_root,
                "destination_root": destination_root,
                "destination_workspace_id": identity.workspace_id,
                "verifier_policy": "paired-raw-terminal-F-v1",
                "input_references": (
                    "raw",
                    str(proof.cut.workspace_id),
                    self._staging_id or "",
                    binding.hexdigest(),
                ),
            }
        )

    def _f_terminal_path(self) -> Path:
        return self._staging.destination / f"raw-v2-terminal-verification.F.{self._epoch}.json"

    def _load_f_terminal_checkpoint(
        self, state: RawV2ImportState
    ) -> tuple[RawV2ImportCursor, int, str] | None:
        path = self._f_terminal_path()
        if self._epoch is None or not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RawV2ImportError("terminal raw verification is unsafe")
        try:
            stored = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise RawV2ImportError("terminal raw verification is malformed") from error
        expected = self._f_terminal_checkpoint(state)
        if expected is None:
            # A durable proof grants no authority outside a genuine live F session.
            return None
        if stored.model_dump(exclude={"updated_at"}) != expected.model_dump(exclude={"updated_at"}):
            raise RawV2ImportError("terminal raw verification binding changed")
        return state.cursor, state.confirmed_count, state.accumulated_digest

    def _write_f_terminal_checkpoint(self, state: RawV2ImportState) -> None:
        checkpoint = self._f_terminal_checkpoint(state)
        if checkpoint is None:
            return
        path = self._f_terminal_path()
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(checkpoint.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_parent(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _verification_checkpoint(
        self,
        state: RawV2ImportState,
        *,
        cursor: RawV2ImportCursor,
        confirmed_count: int,
        accumulated_digest: str,
    ) -> WorkspaceV2MigrationWorkCheckpoint:
        if cursor.received_at is None or cursor.record_id is None:
            raise RawV2ImportError("verification checkpoint cursor is incomplete")
        return WorkspaceV2MigrationWorkCheckpoint(
            operation_id=uuid5(
                NAMESPACE_URL,
                f"{RAW_V2_IMPORT_POLICY_VERSION}:{self._fingerprint}:{self._destination_key}",
            ),
            phase=WorkspaceV2MigrationWorkPhase.VERIFY,
            snapshot_id=uuid5(NAMESPACE_URL, self._fingerprint),
            source_fingerprint=self._fingerprint,
            destination_workspace_id=uuid5(
                NAMESPACE_URL, self._staging_id or self._destination_key
            ),
            source_root=self._source.paths.root,
            destination_root=self._staging.destination,
            staging_root=self._staging.destination,
            policy_version=RAW_V2_IMPORT_POLICY_VERSION,
            reader_policy="snapshot-raw-readonly-v1",
            verifier_policy="paired-raw-inventory-v1",
            tree_fingerprint=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            environment_fingerprint=hashlib.sha256(
                f"{state.page_limit}:{RAW_V2_IMPORT_POLICY_VERSION}".encode("ascii")
            ).hexdigest(),
            cursor=f"{cursor.received_at.isoformat()}|{cursor.record_id}",
            ordinal=(confirmed_count + state.page_limit - 1) // state.page_limit,
            relative_path="storage/v2/raw",
            offset_bytes=0,
            page_limit=state.page_limit,
            confirmed_count=confirmed_count,
            confirmed_bytes=0,
            accumulated_digest=accumulated_digest,
            input_references=("raw",),
            updated_at=self._normalized_now(),
        )

    def _write_verification_checkpoint(
        self,
        state: RawV2ImportState,
        *,
        cursor: RawV2ImportCursor,
        confirmed_count: int,
        accumulated_digest: str,
    ) -> None:
        if not self._can_resume_verified_prefix() or confirmed_count == 0:
            return
        path = self._verification_checkpoint_path()
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    self._verification_checkpoint(
                        state,
                        cursor=cursor,
                        confirmed_count=confirmed_count,
                        accumulated_digest=accumulated_digest,
                    ).model_dump_json()
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_parent(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_verification_checkpoint(
        self, state: RawV2ImportState
    ) -> tuple[RawV2ImportCursor | None, int, str]:
        if not self._can_resume_verified_prefix() or state.confirmed_count == 0:
            return None, 0, empty_digest()
        path = self._verification_checkpoint_path()
        if not path.exists():
            return None, 0, empty_digest()
        if path.is_symlink() or not path.is_file():
            raise RawV2ImportError("verification checkpoint is unsafe")
        try:
            checkpoint = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            text_cursor = checkpoint.cursor
            timestamp, identifier = text_cursor.split("|", 1) if text_cursor else ("", "")
            cursor = RawV2ImportCursor(
                received_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                record_id=UUID(identifier),
            )
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise RawV2ImportError("verification checkpoint is malformed") from error
        if (
            checkpoint.phase is not WorkspaceV2MigrationWorkPhase.VERIFY
            or checkpoint.source_fingerprint != self._fingerprint
            or checkpoint.destination_root != self._staging.destination
            or checkpoint.staging_root != self._staging.destination
            or checkpoint.policy_version != RAW_V2_IMPORT_POLICY_VERSION
            or checkpoint.reader_policy != "snapshot-raw-readonly-v1"
            or checkpoint.verifier_policy != "paired-raw-inventory-v1"
            or checkpoint.page_limit != state.page_limit
            or checkpoint.input_references != ("raw",)
            or checkpoint.confirmed_count > state.confirmed_count
        ):
            raise RawV2ImportError("verification checkpoint binding changed")
        if checkpoint.confirmed_count == state.confirmed_count and (
            checkpoint.accumulated_digest != state.accumulated_digest
            or cursor.record_id != state.cursor.record_id
        ):
            raise RawV2ImportError("verification checkpoint does not match import state")
        return cursor, checkpoint.confirmed_count, checkpoint.accumulated_digest

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
        try:
            self._check_state_binding(state)
        except RawV2ImportError:
            state = self._transition_confirm_and_rebind(state, page_limit)
        if state.page_limit != page_limit:
            raise RawV2ImportError("import state page limit does not match this run")
        return state

    def _transition_confirm_and_rebind(
        self, state: RawV2ImportState, page_limit: int
    ) -> RawV2ImportState:
        """Advance a completed A checkpoint to a verified B snapshot.

        This is intentionally narrow: the source UUID, staging identity, page
        size and every record in the historic prefix must remain identical.
        The original checkpoint bytes are copied under a hash-derived immutable
        name before the active state is rebound to B.
        """
        if (
            self._historic_fingerprint is None
            or state.source_workspace_id != self._workspace_id
            or state.source_fingerprint != self._historic_fingerprint
            or state.page_limit != page_limit
        ):
            raise RawV2ImportError("import state belongs to another source or destination")
        if self._is_portable_state(state):
            if self._staging_id is None or state.staging_id != self._staging_id:
                raise RawV2ImportError("import state belongs to another source or destination")
        elif state.destination != self._destination_key:
            raise RawV2ImportError("import state belongs to another source or destination")
        counts_by_source, counts_by_schema = self._validate_confirmed_prefix(state)
        state_path = self._state_path()
        original = state_path.read_bytes()
        checkpoint_hash = hashlib.sha256(original).hexdigest()
        history_path = state_path.with_name(f"raw-v2-import-state.A.{checkpoint_hash}.json")
        if history_path.exists():
            if history_path.is_symlink() or history_path.read_bytes() != original:
                raise RawV2ImportError("historic import checkpoint cannot be preserved")
        else:
            temporary = history_path.with_name(f".{history_path.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, history_path)
            finally:
                temporary.unlink(missing_ok=True)
        self._archive_verification_checkpoint()
        rebound = self._write_state(
            cursor=state.cursor,
            confirmed_count=state.confirmed_count,
            accumulated_digest=state.accumulated_digest,
            counts_by_source=counts_by_source,
            counts_by_schema=counts_by_schema,
            page_limit=state.page_limit,
        )
        self._write_verification_checkpoint(
            rebound,
            cursor=state.cursor,
            confirmed_count=state.confirmed_count,
            accumulated_digest=state.accumulated_digest,
        )
        return rebound

    def _validate_confirmed_prefix(
        self, state: RawV2ImportState
    ) -> tuple[dict[str, int], dict[str, int]]:
        self._emit(
            "prefix_validation_started",
            confirmed_count=state.confirmed_count,
            page_count=0,
            page_size=0,
        )
        if state.confirmed_count == 0:
            if state.cursor.received_at is not None or state.cursor.record_id is not None:
                raise RawV2ImportError("import state cursor does not match its confirmed count")
            if state.accumulated_digest != empty_digest():
                raise RawV2ImportError("import state digest does not match its confirmed count")
            self._emit(
                "prefix_validation_completed",
                confirmed_count=0,
                page_count=0,
                page_size=0,
            )
            return {}, {}
        if state.cursor.received_at is None or state.cursor.record_id is None:
            raise RawV2ImportError("import state cursor does not match its confirmed count")
        digest = empty_digest()
        counts_by_source: dict[str, int] = {}
        counts_by_schema: dict[str, int] = {}
        confirmed = 0
        source_at: datetime | None = None
        source_id: UUID | None = None
        staged_at: datetime | None = None
        staged_id: UUID | None = None
        last_source_id: UUID | None = None
        page_count = 0
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
                counts_by_source[source_record.source.source_id] = (
                    counts_by_source.get(source_record.source.source_id, 0) + 1
                )
                counts_by_schema[source_record.schema_version] = (
                    counts_by_schema.get(source_record.schema_version, 0) + 1
                )
                last_source_id = record_id
            last_source = source_records[source_page[-1]]
            source_at, source_id = last_source.received_at, last_source.record_id
            staged_at, staged_id = source_at, source_id
            confirmed += len(source_page)
            page_count += 1
            self._emit(
                "prefix_validation_page_verified",
                confirmed_count=confirmed,
                page_count=page_count,
                page_size=len(source_page),
            )
        if last_source_id != state.cursor.record_id:
            raise RawV2ImportError("import state cursor does not match its confirmed count")
        if digest != state.accumulated_digest:
            raise RawV2ImportError("import state digest does not match its confirmed prefix")
        self._emit(
            "prefix_validation_completed",
            confirmed_count=confirmed,
            page_count=page_count,
            page_size=0,
        )
        return counts_by_source, counts_by_schema

    def _emit(
        self,
        phase: str,
        *,
        confirmed_count: int,
        page_count: int,
        page_size: int,
    ) -> None:
        if self._progress is None:
            return
        try:
            self._progress(
                RawV2ImportProgress(
                    phase=phase,
                    confirmed_count=confirmed_count,
                    page_count=page_count,
                    page_size=page_size,
                )
            )
        except Exception as error:
            raise RawV2ImportError("import progress observer failed") from error

    def _write_state(
        self,
        *,
        cursor: RawV2ImportCursor,
        confirmed_count: int,
        accumulated_digest: str,
        counts_by_source: Mapping[str, int] | None = None,
        counts_by_schema: Mapping[str, int] | None = None,
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
            counts_by_source=counts_by_source or {},
            counts_by_schema=counts_by_schema or {},
            page_limit=page_limit,
            updated_at=self._normalized_now(),
        )
        path = self._state_path()
        document = state.model_dump_json() + "\n"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_parent(path)
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
        self._emit(
            "completion_validation_started",
            confirmed_count=state.confirmed_count,
            page_count=0,
            page_size=0,
        )
        digest = empty_digest()
        counts_by_source: dict[str, int] = {}
        counts_by_schema: dict[str, int] = {}
        verified = 0
        max_hydrated = max_page_hydrated
        source_at: datetime | None = None
        source_id: UUID | None = None
        staged_at: datetime | None = None
        staged_id: UUID | None = None
        page_count = 0
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
            page_count += 1
            self._emit(
                "completion_validation_page_verified",
                confirmed_count=verified,
                page_count=page_count,
                page_size=len(source_page),
            )
        if digest != state.accumulated_digest:
            raise RawV2ImportError("import digest does not match the verified corpus")
        if imported_count != verified:
            raise RawV2ImportError("import confirmed count does not match the source inventory")
        self._emit(
            "completion_validation_completed",
            confirmed_count=verified,
            page_count=page_count,
            page_size=0,
        )
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
    "RawV2ImportProgress",
    "RawV2ImportState",
    "RawV2ImportSummary",
    "RawV2Importer",
    "empty_digest",
    "extend_digest",
]
