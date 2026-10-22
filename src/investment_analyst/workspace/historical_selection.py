"""Deterministic, resumable historical selection for a scratch v2 pilot."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency
from investment_analyst.core.models.observation import NormalizedObservation, RawRecord
from investment_analyst.evidence.sec_documents.repository import revision_from_raw_record
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

HISTORICAL_SELECTION_SCHEMA = "historical-selection-v1"
HISTORICAL_SELECTION_PROGRESS_SCHEMA = "historical-selection-progress-v1"
_PAGE_SIZE = 256
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class HistoricalSelectionError(RuntimeError):
    """Raised when a historical selection cannot be built or replayed safely."""


class HistoricalSelectionFamily(StrEnum):
    """Closed historical evidence families selected for the pilot."""

    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    DOCUMENTS = "documents"


class HistoricalSelectionEntityKind(StrEnum):
    """Persisted entity kinds selected by an exact identifier."""

    RAW_RECORD = "raw-record"
    OBSERVATION = "observation"


class HistoricalSelectionItem(ContractModel):
    """One exact source entity selected by identity and canonical content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family: HistoricalSelectionFamily
    entity_kind: HistoricalSelectionEntityKind
    entity_id: UUID
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    available_at: UTCDateTime
    reference: NonEmptyStr | None = None


class HistoricalSelectionExclusion(ContractModel):
    """One explicit family or selector that could not be selected."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family: HistoricalSelectionFamily
    reason: NonEmptyStr
    detail: NonEmptyStr


class HistoricalSelectionManifest(ContractModel):
    """Closed selection manifest written before the destination is mutated."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-selection-v1"] = HISTORICAL_SELECTION_SCHEMA
    selection_id: UUID
    source_workspace_id: UUID
    source_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    destination_workspace_id: UUID
    known_at: UTCDateTime
    created_at: UTCDateTime
    market_start: date
    market_end: date
    warmup_observations: int = Field(ge=0, le=10_000)
    fundamental_start: date
    items: tuple[HistoricalSelectionItem, ...]
    document_checksums: tuple[str, ...] = ()
    exclusions: tuple[HistoricalSelectionExclusion, ...] = ()
    digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_manifest(self) -> HistoricalSelectionManifest:
        keys = tuple(
            (item.family.value, item.entity_kind.value, str(item.entity_id)) for item in self.items
        )
        if keys != tuple(sorted(keys)):
            raise ValueError("historical selection items must be sorted")
        if len(keys) != len(set(keys)):
            raise ValueError("historical selection items must be unique")
        if self.document_checksums != tuple(sorted(set(self.document_checksums))):
            raise ValueError("document checksums must be sorted and unique")
        if self.digest != _manifest_digest(self):
            raise ValueError("historical selection digest does not match its content")
        if self.selection_id != _selection_id(self.digest):
            raise ValueError("historical selection id does not match its digest")
        if not self.items:
            raise ValueError("historical selection must contain at least one entity")
        return self


class HistoricalSelectionCheckpoint(ContractModel):
    """Durable progress bound to one exact manifest and destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-selection-progress-v1"] = (
        HISTORICAL_SELECTION_PROGRESS_SCHEMA
    )
    selection_id: UUID
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    source_workspace_id: UUID
    destination_workspace_id: UUID
    phase: Literal["PREPARED", "IMPORTING", "COMPLETE"]
    raw_cursor: int = Field(ge=0)
    observation_cursor: int = Field(ge=0)
    document_cursor: int = Field(ge=0)
    created_raw: int = Field(ge=0)
    reused_raw: int = Field(ge=0)
    created_observations: int = Field(ge=0)
    reused_observations: int = Field(ge=0)
    confirmed_raw_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    confirmed_observation_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    updated_at: UTCDateTime


class HistoricalSelectionImportSummary(ContractModel):
    """Compact receipt for one complete or resumed selection import."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["historical-selection-import-v1"] = "historical-selection-import-v1"
    selection_id: UUID
    selection_digest: str
    destination_workspace_id: UUID
    raw_items: int = Field(ge=0)
    observation_items: int = Field(ge=0)
    document_items: int = Field(ge=0)
    created_raw: int = Field(ge=0)
    reused_raw: int = Field(ge=0)
    created_observations: int = Field(ge=0)
    reused_observations: int = Field(ge=0)
    replay: bool
    source_unchanged: bool
    completed_at: UTCDateTime


class MarketFamilySelection(ContractModel):
    """One explicit market asset range selected for the pilot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    start: date
    end: date


class HistoricalSelectionRequest(ContractModel):
    """Inputs that fully determine a historical selection manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_workspace_id: UUID
    source_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    destination_workspace_id: UUID
    known_at: UTCDateTime
    created_at: UTCDateTime
    market_selections: tuple[MarketFamilySelection, ...]
    warmup_observations: int = Field(default=200, ge=0, le=10_000)
    fundamental_asset_id: NonEmptyStr
    fundamental_source_id: NonEmptyStr
    fundamental_start: date
    document_asset_id: NonEmptyStr
    document_source_id: NonEmptyStr

    @model_validator(mode="after")
    def _validate_request(self) -> HistoricalSelectionRequest:
        if not self.market_selections:
            raise ValueError("historical selection requires at least one market asset")
        if self.source_workspace_id == self.destination_workspace_id:
            raise ValueError("historical selection requires distinct source and destination")
        for selection in self.market_selections:
            if selection.start > selection.end:
                raise ValueError("market selection range must be non-empty")
        return self


def _manifest_digest(manifest: HistoricalSelectionManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"digest", "selection_id"})
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _selection_id(digest: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{HISTORICAL_SELECTION_SCHEMA}:{digest}")


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _content_digest(model: RawRecord | NormalizedObservation) -> str:
    return sha256_hex(canonical_json_bytes(model))


def _observation_item(
    observation: NormalizedObservation,
    family: HistoricalSelectionFamily,
) -> HistoricalSelectionItem:
    return HistoricalSelectionItem(
        family=family,
        entity_kind=HistoricalSelectionEntityKind.OBSERVATION,
        entity_id=observation.observation_id,
        content_sha256=_content_digest(observation),
        asset_id=observation.asset_id,
        source_id=observation.source.source_id,
        available_at=observation.available_at,
        reference=str(observation.raw_record_id),
    )


def _raw_item(
    record: RawRecord,
    family: HistoricalSelectionFamily,
) -> HistoricalSelectionItem:
    return HistoricalSelectionItem(
        family=family,
        entity_kind=HistoricalSelectionEntityKind.RAW_RECORD,
        entity_id=record.record_id,
        content_sha256=_content_digest(record),
        asset_id=record.asset_id or "unscoped",
        source_id=record.source.source_id,
        available_at=record.available_at,
        reference=record.schema_version,
    )


def _market_dates(
    observations: Iterable[NormalizedObservation],
    *,
    start: date,
    end: date,
    warmup_observations: int,
) -> tuple[set[date], tuple[date, ...]]:
    dates = sorted(
        {
            observation.observed_at.date()
            for observation in observations
            if observation.observed_at is not None
        }
    )
    window = {item for item in dates if start <= item <= end}
    prior = [item for item in dates if item < start]
    warmup = set(prior[-warmup_observations:]) if warmup_observations else set()
    return window | warmup, tuple(sorted(window))


def _select_market(
    source: LocalStorage,
    selection: MarketFamilySelection,
    *,
    known_at: datetime,
    warmup_observations: int,
) -> tuple[list[HistoricalSelectionItem], list[HistoricalSelectionExclusion]]:
    observations = list(
        source.observations.list(
            asset_id=selection.asset_id,
            source_id=selection.source_id,
            frequency=DataFrequency.DAY_1,
            available_to=known_at,
        )
    )
    selected_dates, window_dates = _market_dates(
        observations,
        start=selection.start,
        end=selection.end,
        warmup_observations=warmup_observations,
    )
    if not window_dates:
        return [], [
            HistoricalSelectionExclusion(
                family=HistoricalSelectionFamily.MARKET,
                reason="empty-window",
                detail=f"{selection.asset_id}: {selection.start}..{selection.end}",
            )
        ]
    selected = [
        observation
        for observation in observations
        if observation.observed_at is not None and observation.observed_at.date() in selected_dates
    ]
    return [
        _observation_item(observation, HistoricalSelectionFamily.MARKET) for observation in selected
    ], []


def _quarter_start(value: date, quarters_back: int) -> date:
    month_index = value.year * 12 + (value.month - 1) - quarters_back * 3
    return date(month_index // 12, month_index % 12 + 1, 1)


def _select_fundamentals(
    source: LocalStorage,
    request: HistoricalSelectionRequest,
) -> tuple[list[HistoricalSelectionItem], list[HistoricalSelectionExclusion]]:
    items: list[HistoricalSelectionItem] = []
    exclusions: list[HistoricalSelectionExclusion] = []
    annual_floor = date(request.fundamental_start.year - 2, 1, 1)
    quarterly_floor = _quarter_start(request.fundamental_start, 4)
    for frequency, floor in (
        (DataFrequency.ANNUAL, annual_floor),
        (DataFrequency.QUARTERLY, quarterly_floor),
    ):
        observations = list(
            source.observations.list(
                asset_id=request.fundamental_asset_id,
                source_id=request.fundamental_source_id,
                frequency=frequency,
                available_to=request.known_at,
            )
        )
        selected = [
            observation
            for observation in observations
            if observation.period_end is not None and observation.period_end.date() >= floor
        ]
        if not selected:
            exclusions.append(
                HistoricalSelectionExclusion(
                    family=HistoricalSelectionFamily.FUNDAMENTALS,
                    reason="empty-family",
                    detail=f"{frequency.value} period_end >= {floor.isoformat()}",
                )
            )
        items.extend(
            _observation_item(observation, HistoricalSelectionFamily.FUNDAMENTALS)
            for observation in selected
        )
    return items, exclusions


def _select_documents(
    source: LocalStorage,
    request: HistoricalSelectionRequest,
) -> tuple[list[HistoricalSelectionItem], list[str], list[HistoricalSelectionExclusion]]:
    records = list(
        source.raw_records.list(
            asset_id=request.document_asset_id,
            source_id=request.document_source_id,
            available_to=request.known_at,
        )
    )
    candidates: list[tuple[RawRecord, str]] = []
    for record in records:
        try:
            revision = revision_from_raw_record(record)
        except Exception:
            continue
        if revision.content_size_bytes <= 0:
            continue
        if (
            revision.document.filing.report_date is not None
            and revision.document.filing.report_date < request.fundamental_start
        ):
            continue
        candidates.append((record, revision.content_sha256))
    if not candidates:
        return (
            [],
            [],
            [
                HistoricalSelectionExclusion(
                    family=HistoricalSelectionFamily.DOCUMENTS,
                    reason="empty-family",
                    detail="no non-empty AMD SEC filing linked to the selected period",
                )
            ],
        )
    record, checksum = max(
        candidates,
        key=lambda item: (item[0].available_at, str(item[0].record_id)),
    )
    source.documents.verify(checksum)
    return (
        [_raw_item(record, HistoricalSelectionFamily.DOCUMENTS)],
        [checksum],
        [],
    )


def build_historical_selection(
    source: LocalStorage,
    request: HistoricalSelectionRequest,
) -> HistoricalSelectionManifest:
    """Materialize the closed manifest from a read-only source before any write."""
    if not source.read_only:
        raise HistoricalSelectionError("historical selection source must be read-only")
    market_items: list[HistoricalSelectionItem] = []
    exclusions: list[HistoricalSelectionExclusion] = []
    for selection in request.market_selections:
        items, skipped = _select_market(
            source,
            selection,
            known_at=request.known_at,
            warmup_observations=request.warmup_observations,
        )
        market_items.extend(items)
        exclusions.extend(skipped)
    fundamental_items, fundamental_skips = _select_fundamentals(source, request)
    exclusions.extend(fundamental_skips)
    document_items, document_checksums, document_skips = _select_documents(source, request)
    exclusions.extend(document_skips)
    observation_items = (*market_items, *fundamental_items)
    observation_ids = {
        item.entity_id
        for item in observation_items
        if item.entity_kind is HistoricalSelectionEntityKind.OBSERVATION
    }
    raw_ids = {
        UUID(item.reference)
        for item in observation_items
        if item.reference is not None
        and item.entity_kind is HistoricalSelectionEntityKind.OBSERVATION
    } | {
        item.entity_id
        for item in document_items
        if item.entity_kind is HistoricalSelectionEntityKind.RAW_RECORD
    }
    if not observation_ids:
        raise HistoricalSelectionError("historical selection contains no observations")
    raw_items: list[HistoricalSelectionItem] = []
    for record_id in sorted(raw_ids):
        record = source.raw_records.get(record_id)
        family = (
            HistoricalSelectionFamily.DOCUMENTS
            if any(item.entity_id == record_id for item in document_items)
            else HistoricalSelectionFamily.MARKET
            if any(
                item.entity_kind is HistoricalSelectionEntityKind.OBSERVATION
                and item.reference == str(record_id)
                and item.family is HistoricalSelectionFamily.MARKET
                for item in market_items
            )
            else HistoricalSelectionFamily.FUNDAMENTALS
        )
        raw_items.append(_raw_item(record, family))
    items = tuple(
        sorted(
            (*raw_items, *observation_items),
            key=lambda item: (item.family.value, item.entity_kind.value, str(item.entity_id)),
        )
    )
    provisional = HistoricalSelectionManifest.model_construct(
        schema_version=HISTORICAL_SELECTION_SCHEMA,
        selection_id=UUID(int=0),
        source_workspace_id=request.source_workspace_id,
        source_fingerprint=request.source_fingerprint,
        destination_workspace_id=request.destination_workspace_id,
        known_at=request.known_at,
        created_at=request.created_at,
        market_start=min(item.start for item in request.market_selections),
        market_end=max(item.end for item in request.market_selections),
        warmup_observations=request.warmup_observations,
        fundamental_start=request.fundamental_start,
        items=items,
        document_checksums=tuple(sorted(set(document_checksums))),
        exclusions=tuple(
            sorted(exclusions, key=lambda item: (item.family.value, item.reason, item.detail))
        ),
        digest="0" * 64,
    )
    digest = _manifest_digest(provisional)
    return HistoricalSelectionManifest(
        schema_version=HISTORICAL_SELECTION_SCHEMA,
        selection_id=_selection_id(digest),
        source_workspace_id=request.source_workspace_id,
        source_fingerprint=request.source_fingerprint,
        destination_workspace_id=request.destination_workspace_id,
        known_at=request.known_at,
        created_at=request.created_at,
        market_start=provisional.market_start,
        market_end=provisional.market_end,
        warmup_observations=provisional.warmup_observations,
        fundamental_start=provisional.fundamental_start,
        items=provisional.items,
        document_checksums=provisional.document_checksums,
        exclusions=provisional.exclusions,
        digest=digest,
    )


def write_selection_manifest(path: Path, manifest: HistoricalSelectionManifest) -> None:
    """Write the closed manifest once; never overwrite a different selection."""
    encoded = json.dumps(
        manifest.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise HistoricalSelectionError("selection manifest already exists with other content")
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def load_selection_manifest(path: Path) -> HistoricalSelectionManifest:
    """Load and validate one closed manifest from external operational storage."""
    try:
        return HistoricalSelectionManifest.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HistoricalSelectionError("selection manifest is unreadable or invalid") from error


def _checkpoint_bytes(checkpoint: HistoricalSelectionCheckpoint) -> bytes:
    return json.dumps(
        checkpoint.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_checkpoint(path: Path, checkpoint: HistoricalSelectionCheckpoint) -> None:
    encoded = _checkpoint_bytes(checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_checkpoint(path: Path) -> HistoricalSelectionCheckpoint | None:
    if not path.exists():
        return None
    try:
        return HistoricalSelectionCheckpoint.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HistoricalSelectionError("historical selection checkpoint is invalid") from error


def _ids_digest(items: Iterable[HistoricalSelectionItem]) -> str:
    digest = hashlib.sha256()
    for item in items:
        digest.update(f"{item.entity_id}:{item.content_sha256}\n".encode())
    return digest.hexdigest()


def _chunks(
    values: tuple[HistoricalSelectionItem, ...],
) -> Iterable[tuple[HistoricalSelectionItem, ...]]:
    for start in range(0, len(values), _PAGE_SIZE):
        yield values[start : start + _PAGE_SIZE]


class HistoricalSelectionImporter:
    """Write one closed selection into a fresh v2 destination with durable progress."""

    def __init__(
        self,
        *,
        source: LocalStorage,
        destination: LocalStorage,
        manifest: HistoricalSelectionManifest,
        checkpoint_path: Path,
        source_fingerprint: str,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if not source.read_only:
            raise HistoricalSelectionError("historical selection source must be read-only")
        if destination.read_only:
            raise HistoricalSelectionError("historical selection destination must be writable")
        if source.paths.workspace_id != manifest.source_workspace_id:
            raise HistoricalSelectionError("source workspace does not match the selection")
        if destination.paths.workspace_id != manifest.destination_workspace_id:
            raise HistoricalSelectionError("destination workspace does not match the selection")
        if destination.paths.format_version != 2:
            raise HistoricalSelectionError("historical selection destination must be v2")
        if source_fingerprint != manifest.source_fingerprint:
            raise HistoricalSelectionError("source fingerprint does not match the selection")
        self._source = source
        self._destination = destination
        self._manifest = manifest
        self._checkpoint_path = checkpoint_path
        self._source_fingerprint = source_fingerprint
        self._clock = clock

    def _checkpoint(self, **changes: object) -> HistoricalSelectionCheckpoint:
        existing = _load_checkpoint(self._checkpoint_path)
        base = (
            existing.model_dump()
            if existing is not None
            else {
                "schema_version": HISTORICAL_SELECTION_PROGRESS_SCHEMA,
                "selection_id": self._manifest.selection_id,
                "selection_digest": self._manifest.digest,
                "source_workspace_id": self._manifest.source_workspace_id,
                "destination_workspace_id": self._manifest.destination_workspace_id,
                "phase": "PREPARED",
                "raw_cursor": 0,
                "observation_cursor": 0,
                "document_cursor": 0,
                "created_raw": 0,
                "reused_raw": 0,
                "created_observations": 0,
                "reused_observations": 0,
                "confirmed_raw_digest": None,
                "confirmed_observation_digest": None,
                "updated_at": self._clock(),
            }
        )
        if (
            base["selection_id"] != self._manifest.selection_id
            or base["selection_digest"] != self._manifest.digest
            or base["source_workspace_id"] != self._manifest.source_workspace_id
            or base["destination_workspace_id"] != self._manifest.destination_workspace_id
        ):
            raise HistoricalSelectionError("checkpoint is bound to another selection")
        base.update(changes)
        base["updated_at"] = self._clock()
        checkpoint = HistoricalSelectionCheckpoint.model_validate(base)
        _write_checkpoint(self._checkpoint_path, checkpoint)
        return checkpoint

    def _raw_items(self) -> tuple[HistoricalSelectionItem, ...]:
        return tuple(
            item
            for item in self._manifest.items
            if item.entity_kind is HistoricalSelectionEntityKind.RAW_RECORD
        )

    def _observation_items(self) -> tuple[HistoricalSelectionItem, ...]:
        return tuple(
            item
            for item in self._manifest.items
            if item.entity_kind is HistoricalSelectionEntityKind.OBSERVATION
        )

    def run(self) -> HistoricalSelectionImportSummary:
        """Import or resume the exact selection and verify the destination subset."""
        checkpoint = _load_checkpoint(self._checkpoint_path)
        replay = checkpoint is not None and checkpoint.phase == "COMPLETE"
        raw_items = self._raw_items()
        observation_items = self._observation_items()
        created_raw = checkpoint.created_raw if checkpoint else 0
        reused_raw = checkpoint.reused_raw if checkpoint else 0
        created_observations = checkpoint.created_observations if checkpoint else 0
        reused_observations = checkpoint.reused_observations if checkpoint else 0
        raw_cursor = checkpoint.raw_cursor if checkpoint else 0
        observation_cursor = checkpoint.observation_cursor if checkpoint else 0
        document_cursor = checkpoint.document_cursor if checkpoint else 0
        if not replay:
            self._checkpoint(phase="IMPORTING")
            for page in _chunks(raw_items[raw_cursor:]):
                records = self._source.raw_records.get_many([item.entity_id for item in page])
                ordered = [records[item.entity_id] for item in page]
                self._verify_source_hashes(ordered, page)
                receipt = self._destination.raw_records.save_many(ordered)
                self._verify_destination_records(ordered, page)
                created_raw += len(receipt.created_ids)
                reused_raw += len(receipt.reused_ids)
                raw_cursor += len(page)
                self._checkpoint(
                    phase="IMPORTING",
                    raw_cursor=raw_cursor,
                    created_raw=created_raw,
                    reused_raw=reused_raw,
                    confirmed_raw_digest=_ids_digest(page),
                )
            for page in _chunks(observation_items[observation_cursor:]):
                observations = self._source.observations.get_many([item.entity_id for item in page])
                ordered = [observations[item.entity_id] for item in page]
                self._verify_source_hashes(ordered, page)
                receipt = self._destination.observations.save_many(ordered)
                self._verify_destination_observations(ordered, page)
                created_observations += len(receipt.created_ids)
                reused_observations += len(receipt.reused_ids)
                observation_cursor += len(page)
                self._checkpoint(
                    phase="IMPORTING",
                    observation_cursor=observation_cursor,
                    created_observations=created_observations,
                    reused_observations=reused_observations,
                    confirmed_observation_digest=_ids_digest(page),
                )
            for checksum in self._manifest.document_checksums[document_cursor:]:
                content = self._source.documents.read(checksum)
                self._destination.documents.put(content)
                document_cursor += 1
                self._checkpoint(phase="IMPORTING", document_cursor=document_cursor)
        self._verify_subset(raw_items, observation_items)
        if not replay:
            self._checkpoint(phase="COMPLETE")
        else:
            created_raw = 0
            reused_raw = len(raw_items)
            created_observations = 0
            reused_observations = len(observation_items)
        return HistoricalSelectionImportSummary(
            selection_id=self._manifest.selection_id,
            selection_digest=self._manifest.digest,
            destination_workspace_id=self._manifest.destination_workspace_id,
            raw_items=len(raw_items),
            observation_items=len(observation_items),
            document_items=len(self._manifest.document_checksums),
            created_raw=created_raw,
            reused_raw=reused_raw,
            created_observations=created_observations,
            reused_observations=reused_observations,
            replay=replay,
            source_unchanged=self._source_fingerprint == self._manifest.source_fingerprint,
            completed_at=self._clock(),
        )

    def _verify_source_hashes(
        self,
        models: Iterable[RawRecord | NormalizedObservation],
        items: Iterable[HistoricalSelectionItem],
    ) -> None:
        expected = {item.entity_id: item.content_sha256 for item in items}
        for model in models:
            if (
                _content_digest(model)
                != expected[
                    model.record_id if isinstance(model, RawRecord) else model.observation_id
                ]
            ):
                raise HistoricalSelectionError("source content changed after selection")

    def _verify_destination_records(
        self,
        models: Iterable[RawRecord],
        items: Iterable[HistoricalSelectionItem],
    ) -> None:
        expected = {item.entity_id: item.content_sha256 for item in items}
        stored = self._destination.raw_records.get_many([item.entity_id for item in items])
        for model in models:
            if _content_digest(stored[model.record_id]) != expected[model.record_id]:
                raise HistoricalSelectionError("destination raw content does not match selection")

    def _verify_destination_observations(
        self,
        models: Iterable[NormalizedObservation],
        items: Iterable[HistoricalSelectionItem],
    ) -> None:
        expected = {item.entity_id: item.content_sha256 for item in items}
        stored = self._destination.observations.get_many([item.entity_id for item in items])
        for model in models:
            if _content_digest(stored[model.observation_id]) != expected[model.observation_id]:
                raise HistoricalSelectionError(
                    "destination observation content does not match selection"
                )

    def _verify_subset(
        self,
        raw_items: tuple[HistoricalSelectionItem, ...],
        observation_items: tuple[HistoricalSelectionItem, ...],
    ) -> None:
        for page in _chunks(raw_items):
            stored = self._destination.raw_records.get_many([item.entity_id for item in page])
            for item in page:
                if _content_digest(stored[item.entity_id]) != item.content_sha256:
                    raise HistoricalSelectionError("destination raw subset verification failed")
        for page in _chunks(observation_items):
            stored = self._destination.observations.get_many([item.entity_id for item in page])
            for item in page:
                if _content_digest(stored[item.entity_id]) != item.content_sha256:
                    raise HistoricalSelectionError(
                        "destination observation subset verification failed"
                    )
        for checksum in self._manifest.document_checksums:
            self._destination.documents.verify(checksum)


def workspace_fingerprint(
    *,
    workspace_id: UUID,
    format_version: int,
    manifest_sha256: str,
    raw_count: int,
    observation_count: int,
    metric_count: int,
    diagnostic_count: int,
) -> str:
    """Return a compact identity for a source without hashing the whole database."""
    payload = {
        "workspace_id": str(workspace_id),
        "format_version": format_version,
        "manifest_sha256": manifest_sha256,
        "raw_records": raw_count,
        "observations": observation_count,
        "metric_results": metric_count,
        "diagnostic_results": diagnostic_count,
    }
    return hashlib.sha256(
        json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def selection_window_start(value: date) -> datetime:
    """Return the inclusive public date as a UTC instant for diagnostics."""
    return datetime.combine(value, time.min, tzinfo=UTC)


def selection_window_end(value: date) -> datetime:
    """Return the half-open end of an inclusive public date window."""
    return datetime.combine(value + timedelta(days=1), time.min, tzinfo=UTC)


__all__ = [
    "HistoricalSelectionCheckpoint",
    "HistoricalSelectionError",
    "HistoricalSelectionExclusion",
    "HistoricalSelectionFamily",
    "HistoricalSelectionImportSummary",
    "HistoricalSelectionImporter",
    "HistoricalSelectionItem",
    "HistoricalSelectionManifest",
    "HistoricalSelectionRequest",
    "MarketFamilySelection",
    "build_historical_selection",
    "load_selection_manifest",
    "selection_window_end",
    "selection_window_start",
    "workspace_fingerprint",
    "write_selection_manifest",
]
