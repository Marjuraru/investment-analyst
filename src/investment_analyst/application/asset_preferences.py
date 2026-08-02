"""Versioned workspace preferences for watchlist, favorites, and scheduled refresh."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    MarketAssetUniverse,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    RegisteredScheduledJob,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_MAX_ASSETS = 100
_MAX_REVISIONS = 1_000
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_ARCHIVE_AT_REVISIONS = 900
_ARCHIVE_AT_DOCUMENT_BYTES = 3 * 1024 * 1024
_RETAINED_REVISIONS = 100
_MAX_ARCHIVES = 10_000


class AssetPreferencesError(RuntimeError):
    """Base error for bounded, secret-safe preference operations."""


class AssetPreferencesConflictError(AssetPreferencesError):
    """Raised when an optimistic update targets a stale revision."""


class AssetPreferencesStateError(AssetPreferencesError):
    """Raised when preference state is corrupt, incompatible, or unwritable."""


class AssetPreferenceEntry(ContractModel):
    """Independent user choices for one explicit asset identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1, max_length=200)
    watchlist: bool
    favorite: bool
    scheduled_refresh: bool

    @field_validator("watchlist", "favorite", "scheduled_refresh", mode="before")
    @classmethod
    def require_boolean(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be a bool")
        return value

    @model_validator(mode="after")
    def validate_membership(self) -> AssetPreferenceEntry:
        if (self.favorite or self.scheduled_refresh) and not self.watchlist:
            raise ValueError("favorite and scheduled assets must belong to the watchlist")
        return self


class AssetPreferencesRevision(ContractModel):
    """One immutable, auditable preference revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preferences-revision-v1"] = "asset-preferences-revision-v1"
    revision_id: UUID
    parent_revision_id: UUID | None
    created_at: UTCDateTime
    fingerprint: NonEmptyStr
    entries: tuple[AssetPreferenceEntry, ...] = Field(max_length=_MAX_ASSETS)

    @field_validator("fingerprint")
    @classmethod
    def require_fingerprint(cls, value: str) -> str:
        _validate_fingerprint(value)
        return value

    @model_validator(mode="after")
    def validate_revision(self) -> AssetPreferencesRevision:
        _validate_entries(self.entries)
        if self.fingerprint != asset_preferences_fingerprint(self.entries):
            raise ValueError("preference fingerprint does not match its entries")
        if self.parent_revision_id == self.revision_id:
            raise ValueError("preference revision cannot be its own parent")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class AssetPreferencesArchiveReference(ContractModel):
    """Hash-bound link from active state to one immutable history segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preferences-archive-reference-v1"] = (
        "asset-preferences-archive-reference-v1"
    )
    file_name: NonEmptyStr
    sha256: NonEmptyStr
    size_bytes: int = Field(gt=0, le=_MAX_DOCUMENT_BYTES)
    previous_revision_id: UUID | None
    first_revision_id: UUID
    last_revision_id: UUID
    revision_count: int = Field(gt=0, le=_MAX_REVISIONS)
    first_created_at: UTCDateTime
    last_created_at: UTCDateTime

    @field_validator("file_name")
    @classmethod
    def require_safe_file_name(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if (
            candidate.is_absolute()
            or len(candidate.parts) != 1
            or candidate.name != value
            or not value.endswith(".json")
        ):
            raise ValueError("preference archive file name is invalid")
        return value

    @field_validator("sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        _validate_fingerprint(value)
        return value

    @model_validator(mode="after")
    def validate_reference(self) -> AssetPreferencesArchiveReference:
        if self.first_created_at > self.last_created_at:
            raise ValueError("preference archive timestamps are inconsistent")
        return self


class AssetPreferencesArchiveDocument(ContractModel):
    """One immutable, independently bounded segment of preference history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preferences-archive-v1"] = "asset-preferences-archive-v1"
    previous_revision_id: UUID | None
    revisions: tuple[AssetPreferencesRevision, ...] = Field(
        min_length=1,
        max_length=_MAX_REVISIONS,
    )

    @model_validator(mode="after")
    def validate_history(self) -> AssetPreferencesArchiveDocument:
        _validate_revision_chain(self.revisions, self.previous_revision_id)
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class AssetPreferencesDocument(ContractModel):
    """Active preference history plus hash-bound immutable archive references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preferences-state-v1"] = "asset-preferences-state-v1"
    archives: tuple[AssetPreferencesArchiveReference, ...] = Field(
        default=(),
        max_length=_MAX_ARCHIVES,
    )
    revisions: tuple[AssetPreferencesRevision, ...] = Field(
        min_length=1,
        max_length=_MAX_REVISIONS,
    )

    @model_validator(mode="after")
    def validate_history(self) -> AssetPreferencesDocument:
        previous_revision_id: UUID | None = None
        previous_created_at: datetime | None = None
        archive_names: set[str] = set()
        for archive in self.archives:
            if archive.file_name in archive_names:
                raise ValueError("preference archive file names must be unique")
            archive_names.add(archive.file_name)
            if archive.previous_revision_id != previous_revision_id:
                raise ValueError("preference archive parent chain is inconsistent")
            if previous_created_at is not None and archive.first_created_at < previous_created_at:
                raise ValueError("preference archive timestamps must not decrease")
            previous_revision_id = archive.last_revision_id
            previous_created_at = archive.last_created_at
        _validate_revision_chain(
            self.revisions,
            previous_revision_id,
            previous_created_at=previous_created_at,
        )
        return self

    @property
    def current(self) -> AssetPreferencesRevision:
        return self.revisions[-1]

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class AssetPreferencesUpdate(ContractModel):
    """Strict optimistic update contract accepted by the local HTTP API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preferences-update-v1"] = "asset-preferences-update-v1"
    expected_revision_id: UUID | None
    expected_fingerprint: NonEmptyStr
    entries: tuple[AssetPreferenceEntry, ...] = Field(max_length=_MAX_ASSETS)

    @field_validator("expected_fingerprint")
    @classmethod
    def require_expected_fingerprint(cls, value: str) -> str:
        _validate_fingerprint(value)
        return value

    @model_validator(mode="after")
    def validate_update(self) -> AssetPreferencesUpdate:
        _validate_entries(self.entries)
        return self


class EffectiveAssetPreferences(ContractModel):
    """Current persisted or in-memory CLI-derived preferences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["effective-asset-preferences-v1"] = "effective-asset-preferences-v1"
    source: Literal["cli_seed", "persisted"]
    revision_id: UUID | None
    created_at: UTCDateTime | None
    fingerprint: NonEmptyStr
    entries: tuple[AssetPreferenceEntry, ...] = Field(max_length=_MAX_ASSETS)

    @model_validator(mode="after")
    def validate_effective_state(self) -> EffectiveAssetPreferences:
        _validate_entries(self.entries)
        _validate_fingerprint(self.fingerprint)
        if self.fingerprint != asset_preferences_fingerprint(self.entries):
            raise ValueError("effective preference fingerprint does not match entries")
        if self.source == "cli_seed" and (
            self.revision_id is not None or self.created_at is not None
        ):
            raise ValueError("CLI seed preferences cannot claim a persisted revision")
        if self.source == "persisted" and (self.revision_id is None or self.created_at is None):
            raise ValueError("persisted preferences require revision metadata")
        return self


class AssetPreferenceProjection(ContractModel):
    """Compact catalog-derived UI/API projection for one asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preference-projection-v1"] = "asset-preference-projection-v1"
    asset_id: str = Field(min_length=1, max_length=200)
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: NonEmptyStr | None
    available: bool
    provider: NonEmptyStr | None
    source_ids: tuple[NonEmptyStr, ...]
    frequencies: tuple[NonEmptyStr, ...]
    has_fundamentals: bool
    supports_intraday: bool
    watchlist: bool
    favorite: bool
    scheduled_refresh: bool
    effective_scheduled_refresh: bool
    job_ids: tuple[NonEmptyStr, ...]


class AssetPreferencesView(ContractModel):
    """Versioned compact response for GET and PUT."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-preferences-view-v1"] = "asset-preferences-view-v1"
    source: Literal["cli_seed", "persisted"]
    revision_id: UUID | None
    created_at: UTCDateTime | None
    fingerprint: NonEmptyStr
    scheduler_enabled: bool
    watchlist_count: int = Field(ge=0)
    favorite_count: int = Field(ge=0)
    scheduled_asset_count: int = Field(ge=0)
    scheduled_job_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    assets: tuple[AssetPreferenceProjection, ...] = Field(max_length=_MAX_ASSETS)

    @field_validator("scheduler_enabled", mode="before")
    @classmethod
    def require_scheduler_boolean(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("scheduler_enabled must be a bool")
        return value

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class AssetPreferencesStore:
    """Load, archive, and atomically append bounded preference revisions."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        revision_id_factory: Callable[[], UUID] = uuid4,
        max_revisions: int = _MAX_REVISIONS,
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
        archive_at_revisions: int | None = None,
        archive_at_document_bytes: int | None = None,
        retained_revisions: int = _RETAINED_REVISIONS,
    ) -> None:
        if max_revisions < 1 or max_revisions > _MAX_REVISIONS:
            raise ValueError("preference max_revisions is invalid")
        if max_document_bytes < 1 or max_document_bytes > _MAX_DOCUMENT_BYTES:
            raise ValueError("preference max_document_bytes is invalid")
        if (retained_revisions < 1 or retained_revisions >= max_revisions) and (
            max_revisions != 1 or retained_revisions != _RETAINED_REVISIONS
        ):
            raise ValueError("preference retained_revisions is invalid")
        revision_trigger = archive_at_revisions
        if revision_trigger is None and max_revisions >= 3:
            revision_trigger = min(_ARCHIVE_AT_REVISIONS, max_revisions - 1)
        if revision_trigger is not None and (
            revision_trigger < 2 or revision_trigger >= max_revisions
        ):
            raise ValueError("preference archive_at_revisions is invalid")
        byte_trigger = archive_at_document_bytes
        if byte_trigger is None and max_document_bytes >= 3:
            byte_trigger = min(_ARCHIVE_AT_DOCUMENT_BYTES, max_document_bytes - 1)
        if byte_trigger is not None and (byte_trigger < 1 or byte_trigger >= max_document_bytes):
            raise ValueError("preference archive_at_document_bytes is invalid")
        expanded = path.expanduser()
        if not expanded.is_absolute():
            raise ValueError("asset preference state path must be absolute")
        self._path = expanded
        self._clock = clock
        self._revision_id_factory = revision_id_factory
        self._max_revisions = max_revisions
        self._max_document_bytes = max_document_bytes
        self._archive_at_revisions = revision_trigger
        self._archive_at_document_bytes = byte_trigger
        self._retained_revisions = min(retained_revisions, max_revisions - 1)
        self._archive_root = expanded.parent / f"{expanded.stem}_archives"
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AssetPreferencesDocument | None:
        """Read valid state without creating or repairing a missing/corrupt document."""
        with self._lock:
            return self._load_unlocked()

    def load_history(self) -> tuple[AssetPreferencesRevision, ...]:
        """Load the complete verified chain across archives and active state."""
        with self._lock:
            document = self._load_unlocked()
            if document is None:
                return ()
            archived = tuple(
                revision
                for reference in document.archives
                for revision in self._load_archive_unlocked(reference).revisions
            )
            return (*archived, *document.revisions)

    def apply(
        self,
        update: AssetPreferencesUpdate,
        seed: EffectiveAssetPreferences,
    ) -> tuple[EffectiveAssetPreferences, bool]:
        """Apply one optimistic update and return whether a revision was appended."""
        with self._lock:
            document = self._load_unlocked()
            current = effective_asset_preferences(document, seed)
            if (
                update.expected_revision_id != current.revision_id
                or update.expected_fingerprint != current.fingerprint
            ):
                raise AssetPreferencesConflictError(
                    "asset preferences changed; reload before saving"
                )
            fingerprint = asset_preferences_fingerprint(update.entries)
            if document is not None and fingerprint == current.fingerprint:
                return current, False
            revisions = document.revisions if document is not None else ()
            archives = document.archives if document is not None else ()
            if len(revisions) >= self._max_revisions and self._archive_at_revisions is None:
                raise AssetPreferencesStateError("asset preference history limit was reached")
            created_at = self._now()
            parent = revisions[-1] if revisions else None
            if parent is not None and created_at < parent.created_at:
                created_at = parent.created_at
            revision = AssetPreferencesRevision(
                revision_id=self._revision_id_factory(),
                parent_revision_id=parent.revision_id if parent else None,
                created_at=created_at,
                fingerprint=fingerprint,
                entries=update.entries,
            )
            appended = (*revisions, revision)
            pending_archive: tuple[Path, bytes] | None = None
            provisional: AssetPreferencesDocument | None = None
            if len(appended) <= _MAX_REVISIONS:
                provisional = AssetPreferencesDocument(archives=archives, revisions=appended)
            should_archive = (
                self._archive_at_revisions is not None
                and len(appended) >= self._archive_at_revisions
            ) or (
                provisional is not None
                and self._archive_at_document_bytes is not None
                and len(_encode_document(provisional)) >= self._archive_at_document_bytes
            )
            if should_archive:
                updated, pending_archive = self._compact(archives, appended)
            elif provisional is None or len(appended) > self._max_revisions:
                raise AssetPreferencesStateError("asset preference history limit was reached")
            else:
                updated = provisional
            encoded = _encode_document(updated)
            if len(encoded) > self._max_document_bytes:
                raise AssetPreferencesStateError("asset preference document size limit was reached")
            if pending_archive is not None:
                self._write_archive(*pending_archive)
            self._write_atomic(self._path, encoded, "asset preference state")
            return effective_asset_preferences(updated, seed), True

    def _load_unlocked(self) -> AssetPreferencesDocument | None:
        if not self._path.exists():
            if self._path.is_symlink():
                raise AssetPreferencesStateError("asset preference state must be a regular file")
            return None
        try:
            document = AssetPreferencesDocument.model_validate_json(
                self._read_bounded(self._path, "asset preference state")
            )
            if len(document.revisions) > self._max_revisions:
                raise ValueError("active preference history exceeds its configured limit")
            self._validate_archives_unlocked(document)
            return document
        except (OSError, UnicodeError, ValueError) as error:
            raise AssetPreferencesStateError(
                "asset preference state is malformed or unreadable"
            ) from error

    def _compact(
        self,
        archives: tuple[AssetPreferencesArchiveReference, ...],
        revisions: tuple[AssetPreferencesRevision, ...],
    ) -> tuple[AssetPreferencesDocument, tuple[Path, bytes]]:
        retained = min(self._retained_revisions, len(revisions) - 1)
        archived_revisions = revisions[:-retained]
        active_revisions = revisions[-retained:]
        previous_revision_id = (
            archives[-1].last_revision_id if archives else archived_revisions[0].parent_revision_id
        )
        archive = AssetPreferencesArchiveDocument(
            previous_revision_id=previous_revision_id,
            revisions=archived_revisions,
        )
        encoded_archive = _encode_archive(archive)
        if (
            len(archived_revisions) > self._max_revisions
            or len(encoded_archive) > self._max_document_bytes
        ):
            raise AssetPreferencesStateError("asset preference archive limit was reached")
        first = archived_revisions[0]
        last = archived_revisions[-1]
        file_name = f"asset_preferences_{first.revision_id.hex}_{last.revision_id.hex}.json"
        reference = AssetPreferencesArchiveReference(
            file_name=file_name,
            sha256=hashlib.sha256(encoded_archive).hexdigest(),
            size_bytes=len(encoded_archive),
            previous_revision_id=archive.previous_revision_id,
            first_revision_id=first.revision_id,
            last_revision_id=last.revision_id,
            revision_count=len(archived_revisions),
            first_created_at=first.created_at,
            last_created_at=last.created_at,
        )
        updated = AssetPreferencesDocument(
            archives=(*archives, reference),
            revisions=active_revisions,
        )
        return updated, (self._archive_root / file_name, encoded_archive)

    def _validate_archives_unlocked(self, document: AssetPreferencesDocument) -> None:
        seen_ids: set[UUID] = set()
        previous_revision_id: UUID | None = None
        previous_created_at: datetime | None = None
        for reference in document.archives:
            archive = self._load_archive_unlocked(reference)
            first = archive.revisions[0]
            last = archive.revisions[-1]
            if (
                archive.previous_revision_id != reference.previous_revision_id
                or first.revision_id != reference.first_revision_id
                or last.revision_id != reference.last_revision_id
                or len(archive.revisions) != reference.revision_count
                or first.created_at != reference.first_created_at
                or last.created_at != reference.last_created_at
                or archive.previous_revision_id != previous_revision_id
            ):
                raise ValueError("preference archive reference does not match its content")
            _validate_revision_chain(
                archive.revisions,
                previous_revision_id,
                previous_created_at=previous_created_at,
            )
            for revision in archive.revisions:
                if revision.revision_id in seen_ids:
                    raise ValueError("preference revision identities must be globally unique")
                seen_ids.add(revision.revision_id)
            previous_revision_id = last.revision_id
            previous_created_at = last.created_at
        _validate_revision_chain(
            document.revisions,
            previous_revision_id,
            previous_created_at=previous_created_at,
        )
        if any(revision.revision_id in seen_ids for revision in document.revisions):
            raise ValueError("preference revision identities must be globally unique")

    def _load_archive_unlocked(
        self,
        reference: AssetPreferencesArchiveReference,
    ) -> AssetPreferencesArchiveDocument:
        if self._archive_root.is_symlink():
            raise ValueError("preference archive directory must not be a symbolic link")
        path = self._archive_root / reference.file_name
        encoded = self._read_bounded(path, "asset preference archive")
        if len(encoded) != reference.size_bytes:
            raise ValueError("preference archive size does not match its reference")
        if hashlib.sha256(encoded).hexdigest() != reference.sha256:
            raise ValueError("preference archive hash does not match its reference")
        archive = AssetPreferencesArchiveDocument.model_validate_json(encoded)
        if len(archive.revisions) > self._max_revisions:
            raise ValueError("preference archive exceeds its configured revision limit")
        return archive

    def _read_bounded(self, path: Path, label: str) -> bytes:
        if path.is_symlink():
            raise OSError(f"{label} must be a regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"{label} must be a regular file")
            if metadata.st_size > self._max_document_bytes:
                raise ValueError(f"{label} exceeds its configured size limit")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(self._max_document_bytes + 1)
            if len(encoded) > self._max_document_bytes:
                raise ValueError(f"{label} exceeds its configured size limit")
            return encoded
        finally:
            os.close(descriptor)

    def _write_archive(self, path: Path, document: bytes) -> None:
        if self._archive_root.is_symlink():
            raise AssetPreferencesStateError(
                "asset preference archive directory must not be a symbolic link"
            )
        if path.exists() or path.is_symlink():
            try:
                existing = self._read_bounded(path, "asset preference archive")
            except (OSError, ValueError) as error:
                raise AssetPreferencesStateError(
                    "asset preference archive is malformed or unreadable"
                ) from error
            if existing != document:
                raise AssetPreferencesStateError("asset preference archive identity conflicts")
            return
        self._write_atomic(path, document, "asset preference archive")

    def _write_atomic(self, path: Path, document: bytes, label: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor: int | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.is_symlink():
                raise OSError("parent directory is a symbolic link")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise AssetPreferencesStateError(f"{label} could not be written") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AssetPreferencesStateError("asset preference clock must be timezone-aware")
        return value.astimezone(UTC)


AssetPreferenceJobFactory = Callable[
    [tuple[str, ...]],
    tuple[RegisteredScheduledJob, ...],
]


class AssetPreferencesService:
    """Coordinate strict preferences with a provider-free scheduler registry update."""

    def __init__(
        self,
        store: AssetPreferencesStore,
        universe: MarketAssetUniverse,
        seed: EffectiveAssetPreferences,
        *,
        scheduler: MultiAssetScheduler | None,
        job_factory: AssetPreferenceJobFactory | None,
    ) -> None:
        if (scheduler is None) != (job_factory is None):
            raise ValueError("scheduler and preference job factory must be configured together")
        self._store = store
        self._universe = universe
        self._seed = seed
        self._scheduler = scheduler
        self._job_factory = job_factory
        self._lock = threading.RLock()

    def effective(self) -> EffectiveAssetPreferences:
        with self._lock:
            return effective_asset_preferences(self._store.load(), self._seed)

    def view(self) -> AssetPreferencesView:
        with self._lock:
            return self._view(effective_asset_preferences(self._store.load(), self._seed))

    def update(self, request: AssetPreferencesUpdate) -> AssetPreferencesView:
        """Persist and publish one revision without waiting for providers or financial writes."""
        with self._lock:
            known_ids = {asset.asset_id for asset in self._universe.assets}
            current_entries = {
                entry.asset_id: entry
                for entry in effective_asset_preferences(
                    self._store.load(),
                    self._seed,
                ).entries
            }
            unknown = tuple(
                entry.asset_id
                for entry in request.entries
                if entry.asset_id not in known_ids and current_entries.get(entry.asset_id) != entry
            )
            if unknown:
                raise ValueError(f"asset preference is unavailable: {unknown[0]}")
            retained_unavailable = tuple(
                entry for asset_id, entry in current_entries.items() if asset_id not in known_ids
            )
            merged_entries = tuple(
                sorted(
                    {
                        entry.asset_id: entry for entry in (*request.entries, *retained_unavailable)
                    }.values(),
                    key=lambda entry: entry.asset_id,
                )
            )
            normalized_request = request.model_copy(update={"entries": merged_entries})
            scheduled_ids = tuple(
                entry.asset_id
                for entry in normalized_request.entries
                if entry.scheduled_refresh and entry.asset_id in known_ids
            )
            jobs: tuple[RegisteredScheduledJob, ...] | None = None
            if self._scheduler is not None:
                if not scheduled_ids:
                    raise ValueError(
                        "scheduler-enabled preferences require at least one scheduled asset"
                    )
                assert self._job_factory is not None
                jobs = self._job_factory(scheduled_ids)
            effective, changed = self._store.apply(normalized_request, self._seed)
            if changed and jobs is not None:
                self._scheduler.reconcile_jobs(jobs)
            return self._view(effective)

    def _view(self, effective: EffectiveAssetPreferences) -> AssetPreferencesView:
        entries = {item.asset_id: item for item in effective.entries}
        descriptors = {item.asset_id: item for item in self._universe.assets}
        job_ids_by_asset: dict[str, tuple[str, ...]] = {}
        scheduled_job_count = 0
        if self._scheduler is not None:
            definitions = self._scheduler.registered_job_definitions()
            scheduled_job_count = len(definitions)
            for definition in definitions:
                if definition.asset_id is not None:
                    job_ids_by_asset.setdefault(definition.asset_id, ())
                    job_ids_by_asset[definition.asset_id] = (
                        *job_ids_by_asset[definition.asset_id],
                        definition.job_id,
                    )
        projections = tuple(
            sorted(
                (
                    _projection(
                        asset_id,
                        entries.get(asset_id),
                        descriptors.get(asset_id),
                        job_ids_by_asset.get(asset_id, ()),
                        scheduler_enabled=self._scheduler is not None,
                    )
                    for asset_id in set(entries) | set(descriptors)
                ),
                key=lambda item: (
                    not item.favorite,
                    not item.watchlist,
                    not item.available,
                    item.asset_id,
                ),
            )
        )
        return AssetPreferencesView(
            source=effective.source,
            revision_id=effective.revision_id,
            created_at=effective.created_at,
            fingerprint=effective.fingerprint,
            scheduler_enabled=self._scheduler is not None,
            watchlist_count=sum(item.watchlist for item in projections),
            favorite_count=sum(item.favorite for item in projections),
            scheduled_asset_count=sum(item.effective_scheduled_refresh for item in projections),
            scheduled_job_count=scheduled_job_count,
            unavailable_count=sum(not item.available for item in projections),
            assets=projections,
        )


def cli_seed_asset_preferences(
    universe: MarketAssetUniverse,
    selected_asset_ids: tuple[str, ...],
) -> EffectiveAssetPreferences:
    """Derive compatible in-memory defaults without writing the workspace."""
    if selected_asset_ids != tuple(sorted(set(selected_asset_ids))):
        raise ValueError("CLI scheduled asset IDs must be unique and sorted")
    known_ids = {item.asset_id for item in universe.assets}
    unknown = set(selected_asset_ids) - known_ids
    if unknown:
        raise ValueError(f"scheduled asset_id is not supported: {sorted(unknown)[0]}")
    selected = set(selected_asset_ids) if selected_asset_ids else known_ids
    entries = tuple(
        AssetPreferenceEntry(
            asset_id=asset.asset_id,
            watchlist=asset.asset_id in selected,
            favorite=False,
            scheduled_refresh=asset.asset_id in selected,
        )
        for asset in universe.assets
    )
    return EffectiveAssetPreferences(
        source="cli_seed",
        revision_id=None,
        created_at=None,
        fingerprint=asset_preferences_fingerprint(entries),
        entries=entries,
    )


def effective_asset_preferences(
    document: AssetPreferencesDocument | None,
    seed: EffectiveAssetPreferences,
) -> EffectiveAssetPreferences:
    if document is None:
        return seed
    revision = document.current
    return EffectiveAssetPreferences(
        source="persisted",
        revision_id=revision.revision_id,
        created_at=revision.created_at,
        fingerprint=revision.fingerprint,
        entries=revision.entries,
    )


def scheduled_available_asset_ids(
    effective: EffectiveAssetPreferences,
    universe: MarketAssetUniverse,
) -> tuple[str, ...]:
    """Select only currently resolvable scheduled assets without deleting unavailable state."""
    available = {asset.asset_id for asset in universe.assets}
    return tuple(
        entry.asset_id
        for entry in effective.entries
        if entry.scheduled_refresh and entry.asset_id in available
    )


def asset_preferences_fingerprint(entries: tuple[AssetPreferenceEntry, ...]) -> str:
    document = json.dumps(
        [entry.model_dump(mode="json") for entry in entries],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(document).hexdigest()


def _validate_entries(entries: tuple[AssetPreferenceEntry, ...]) -> None:
    asset_ids = tuple(entry.asset_id for entry in entries)
    if asset_ids != tuple(sorted(asset_ids)):
        raise ValueError("asset preference entries must be sorted by asset_id")
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("asset preference entries must not contain duplicates")


def _validate_fingerprint(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("asset preference fingerprint is invalid")


def _validate_revision_chain(
    revisions: tuple[AssetPreferencesRevision, ...],
    previous_revision_id: UUID | None,
    *,
    previous_created_at: datetime | None = None,
) -> None:
    ids = tuple(item.revision_id for item in revisions)
    if len(ids) != len(set(ids)):
        raise ValueError("preference revision identities must be unique")
    expected_parent = previous_revision_id
    prior_created_at = previous_created_at
    for revision in revisions:
        if revision.parent_revision_id != expected_parent:
            raise ValueError("preference revision parent chain is inconsistent")
        if prior_created_at is not None and revision.created_at < prior_created_at:
            raise ValueError("preference revision timestamps must not decrease")
        expected_parent = revision.revision_id
        prior_created_at = revision.created_at


def _encode_document(document: AssetPreferencesDocument) -> bytes:
    return (
        json.dumps(
            document.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _encode_archive(document: AssetPreferencesArchiveDocument) -> bytes:
    return (
        json.dumps(
            document.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _projection(
    asset_id: str,
    entry: AssetPreferenceEntry | None,
    descriptor: MarketAssetDescriptor | None,
    job_ids: tuple[str, ...],
    *,
    scheduler_enabled: bool,
) -> AssetPreferenceProjection:
    preference = entry or AssetPreferenceEntry(
        asset_id=asset_id,
        watchlist=False,
        favorite=False,
        scheduled_refresh=False,
    )
    if descriptor is None:
        return AssetPreferenceProjection(
            asset_id=asset_id,
            symbol=asset_id,
            name="Activo no disponible",
            asset_class=None,
            available=False,
            provider=None,
            source_ids=(),
            frequencies=(),
            has_fundamentals=False,
            supports_intraday=False,
            watchlist=preference.watchlist,
            favorite=preference.favorite,
            scheduled_refresh=preference.scheduled_refresh,
            effective_scheduled_refresh=False,
            job_ids=(),
        )
    source_ids = tuple(
        sorted(
            {
                descriptor.source_id,
                *descriptor.fundamental_source_ids,
                *(
                    (descriptor.intraday_source_id,)
                    if descriptor.intraday_source_id is not None
                    else ()
                ),
            }
        )
    )
    frequencies = tuple(
        sorted(
            {
                "day_1",
                *(item.value for item in descriptor.fundamental_frequencies),
                *(("minute_1",) if descriptor.supports_intraday else ()),
            }
        )
    )
    return AssetPreferenceProjection(
        asset_id=asset_id,
        symbol=descriptor.symbol,
        name=descriptor.name,
        asset_class=descriptor.asset_class.value,
        available=True,
        provider=descriptor.provider,
        source_ids=source_ids,
        frequencies=frequencies,
        has_fundamentals=descriptor.has_fundamentals,
        supports_intraday=descriptor.supports_intraday,
        watchlist=preference.watchlist,
        favorite=preference.favorite,
        scheduled_refresh=preference.scheduled_refresh,
        effective_scheduled_refresh=preference.scheduled_refresh and scheduler_enabled,
        job_ids=tuple(sorted(job_ids)),
    )


__all__ = [
    "AssetPreferenceEntry",
    "AssetPreferenceProjection",
    "AssetPreferencesArchiveDocument",
    "AssetPreferencesArchiveReference",
    "AssetPreferencesConflictError",
    "AssetPreferencesDocument",
    "AssetPreferencesError",
    "AssetPreferencesRevision",
    "AssetPreferencesService",
    "AssetPreferencesStateError",
    "AssetPreferencesStore",
    "AssetPreferencesUpdate",
    "AssetPreferencesView",
    "EffectiveAssetPreferences",
    "asset_preferences_fingerprint",
    "cli_seed_asset_preferences",
    "effective_asset_preferences",
    "scheduled_available_asset_ids",
]
