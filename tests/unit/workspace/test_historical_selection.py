"""Unit tests for deterministic historical selection and resumable import."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.storage.errors import RecordConflictError
from investment_analyst.workspace.historical_selection import (
    HistoricalSelectionCheckpoint,
    HistoricalSelectionError,
    HistoricalSelectionImporter,
    HistoricalSelectionRequest,
    MarketFamilySelection,
    build_historical_selection,
    load_selection_manifest,
    workspace_fingerprint,
    write_selection_manifest,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_KNOWN_AT = datetime(2026, 10, 22, 12, tzinfo=UTC)
_AMD = "equity:us:amd"
_MARKET_SOURCE = "fixture:market"
_FUNDAMENTAL_SOURCE = "sec-edgar:companyfacts"
_DOCUMENT_SOURCE = "sec-edgar:primary-documents"


def _source_reference(source_id: str, key: str) -> SourceReference:
    return SourceReference(source_id=source_id, record_key=key, retrieved_at=_KNOWN_AT)


def _raw(
    *,
    source_id: str,
    key: str,
    payload: dict[str, object],
    available_at: datetime,
    schema_version: str,
) -> RawRecord:
    return RawRecord(
        record_id=uuid4(),
        asset_id=_AMD,
        source=_source_reference(source_id, key),
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload=payload,
        schema_version=schema_version,
    )


def _observation(
    record: RawRecord,
    *,
    field_name: str,
    value: Decimal,
    frequency: DataFrequency,
    observed_at: datetime | None,
    period_end: datetime | None,
    available_at: datetime,
) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=record.record_id,
        asset_id=_AMD,
        field_name=field_name,
        value=value,
        unit="USD",
        frequency=frequency,
        observed_at=observed_at,
        period_end=period_end,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=record.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )


def _seed_source(path: Path) -> tuple:
    service = WorkspaceService(environ={}, home=path / "home")
    initialized = service.initialize(path / "v1", format_version=1)
    storage = service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE)
    storage.open()
    for offset in range(40):
        observed = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=offset)
        record = _raw(
            source_id=_MARKET_SOURCE,
            key=f"AMD:{observed.date()}",
            payload={"close": str(observed.day)},
            available_at=observed + timedelta(hours=1),
            schema_version="market-v1",
        )
        storage.raw_records.save(record)
        storage.observations.save(
            _observation(
                record,
                field_name="close",
                value=Decimal(observed.day),
                frequency=DataFrequency.DAY_1,
                observed_at=observed,
                period_end=None,
                available_at=observed + timedelta(hours=1),
            )
        )
    for year in (2022, 2023, 2024):
        period_end = datetime(year, 12, 31, tzinfo=UTC)
        record = _raw(
            source_id=_FUNDAMENTAL_SOURCE,
            key=f"AMD:annual:{year}",
            payload={"value": year},
            available_at=datetime(year + 1, 2, 1, tzinfo=UTC),
            schema_version="companyfacts-v1",
        )
        storage.raw_records.save(record)
        storage.observations.save(
            _observation(
                record,
                field_name="Revenues",
                value=Decimal(year),
                frequency=DataFrequency.ANNUAL,
                observed_at=None,
                period_end=period_end,
                available_at=datetime(year + 1, 2, 1, tzinfo=UTC),
            )
        )
    for quarter in range(1, 5):
        period_end = datetime(2024, quarter * 3, 1, tzinfo=UTC) - timedelta(days=1)
        record = _raw(
            source_id=_FUNDAMENTAL_SOURCE,
            key=f"AMD:quarterly:2024:{quarter}",
            payload={"value": quarter},
            available_at=period_end + timedelta(days=45),
            schema_version="companyfacts-v1",
        )
        storage.raw_records.save(record)
        storage.observations.save(
            _observation(
                record,
                field_name="Revenues",
                value=Decimal(quarter),
                frequency=DataFrequency.QUARTERLY,
                observed_at=None,
                period_end=period_end,
                available_at=period_end + timedelta(days=45),
            )
        )
    accepted_at = datetime(2025, 2, 10, 18, tzinfo=UTC)
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0000002488", "0000002488-25-000001"),
        filer_cik="0000002488",
        accession="0000002488-25-000001",
        form="10-K",
        filing_date=date(2025, 2, 10),
        report_date=date(2024, 12, 31),
        accepted_at=accepted_at,
        is_amendment=False,
    )
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, "primary.htm"),
        filing=filing,
        name="primary.htm",
    )
    receipt = storage.documents.put(b"<html>AMD annual report</html>")
    revision_id = SecDocumentRevision.expected_id(
        document.document_id, receipt.sha256, REVISION_SCHEMA_VERSION_V2
    )
    discovery = _raw(
        source_id="sec-edgar:amd:submissions",
        key="AMD:submissions:2025",
        payload={"cik": "0000002488"},
        available_at=accepted_at,
        schema_version="sec-submissions-v1",
    )
    storage.raw_records.save(discovery)
    revision = SecDocumentRevision(
        revision_id=revision_id,
        asset_id=_AMD,
        document=document,
        raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=discovery.record_id,
        content_sha256=receipt.sha256,
        content_size_bytes=receipt.size_bytes,
        available_at=accepted_at,
        retrieved_at=accepted_at,
        source_url="https://www.sec.gov/primary.htm",
        revision_schema_version=REVISION_SCHEMA_VERSION_V2,
    )
    storage.raw_records.save(revision_to_raw_record(revision))
    storage.close()
    inspection = service.inspect(initialized.paths.root)
    manifest = service.manifest(initialized.paths.root)
    fingerprint = workspace_fingerprint(
        workspace_id=manifest.workspace_id,
        format_version=manifest.format_version,
        manifest_sha256=hashlib.sha256(initialized.paths.manifest_path.read_bytes()).hexdigest(),
        raw_count=inspection.raw_record_count,
        observation_count=inspection.observation_count,
        metric_count=inspection.metric_result_count,
        diagnostic_count=inspection.diagnostic_result_count,
    )
    return service, initialized, manifest, fingerprint


def _request(
    source_workspace_id,
    destination_workspace_id,
    fingerprint: str,
) -> HistoricalSelectionRequest:
    return HistoricalSelectionRequest(
        source_workspace_id=source_workspace_id,
        source_fingerprint=fingerprint,
        destination_workspace_id=destination_workspace_id,
        known_at=_KNOWN_AT,
        created_at=_KNOWN_AT,
        market_selections=(
            MarketFamilySelection(
                asset_id=_AMD,
                source_id=_MARKET_SOURCE,
                start=date(2026, 7, 1),
                end=date(2026, 7, 10),
            ),
        ),
        warmup_observations=2,
        fundamental_asset_id=_AMD,
        fundamental_source_id=_FUNDAMENTAL_SOURCE,
        fundamental_start=date(2023, 1, 1),
        document_asset_id=_AMD,
        document_source_id=_DOCUMENT_SOURCE,
    )


def test_selection_is_deterministic_and_covers_all_families(tmp_path: Path) -> None:
    service, source, source_manifest, fingerprint = _seed_source(tmp_path)
    destination = service.initialize(tmp_path / "v2", format_version=2)
    request = _request(source_manifest.workspace_id, destination.manifest.workspace_id, fingerprint)
    with service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as storage:
        first = build_historical_selection(storage, request)
        second = build_historical_selection(storage, request)
    assert first.selection_id == second.selection_id
    assert first.digest == second.digest
    families = {item.family.value for item in first.items}
    assert {"market", "fundamentals", "documents"} <= families
    assert first.document_checksums
    assert not first.exclusions
    output = tmp_path / "selection.json"
    write_selection_manifest(output, first)
    assert load_selection_manifest(output).digest == first.digest


def test_import_is_idempotent_and_replay_creates_no_entities(tmp_path: Path) -> None:
    service, source, source_manifest, fingerprint = _seed_source(tmp_path)
    destination = service.initialize(tmp_path / "v2", format_version=2)
    request = _request(source_manifest.workspace_id, destination.manifest.workspace_id, fingerprint)
    with service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as read_source:
        manifest = build_historical_selection(read_source, request)
    checkpoint = tmp_path / "checkpoint.json"
    with (
        service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as read_source,
        service.open_storage(destination.paths, WorkspaceAccessMode.READ_WRITE) as writable,
    ):
        first = HistoricalSelectionImporter(
            source=read_source,
            destination=writable,
            manifest=manifest,
            checkpoint_path=checkpoint,
            source_fingerprint=fingerprint,
        ).run()
        assert first.created_raw > 0
        assert first.created_observations > 0
        assert first.replay is False
        second = HistoricalSelectionImporter(
            source=read_source,
            destination=writable,
            manifest=manifest,
            checkpoint_path=checkpoint,
            source_fingerprint=fingerprint,
        ).run()
        assert second.replay is True
        assert second.created_raw == 0
        assert second.created_observations == 0
        assert second.reused_raw == first.raw_items
        assert second.reused_observations == first.observation_items


def test_conflicting_destination_content_fails_closed(tmp_path: Path) -> None:
    service, source, source_manifest, fingerprint = _seed_source(tmp_path)
    destination = service.initialize(tmp_path / "v2", format_version=2)
    request = _request(source_manifest.workspace_id, destination.manifest.workspace_id, fingerprint)
    with service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as read_source:
        manifest = build_historical_selection(read_source, request)
    raw_item = next(item for item in manifest.items if item.entity_kind.value == "raw-record")
    conflict = RawRecord(
        record_id=raw_item.entity_id,
        asset_id=_AMD,
        source=_source_reference(_MARKET_SOURCE, "conflict"),
        event_time=_KNOWN_AT,
        available_at=_KNOWN_AT,
        received_at=_KNOWN_AT,
        payload={"close": "different"},
        schema_version="market-v1",
    )
    with service.open_storage(destination.paths, WorkspaceAccessMode.READ_WRITE) as writable:
        writable.raw_records.save(conflict)
    checkpoint = tmp_path / "checkpoint.json"
    with (
        service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as read_source,
        service.open_storage(destination.paths, WorkspaceAccessMode.READ_WRITE) as writable,
        pytest.raises(RecordConflictError),
    ):
        HistoricalSelectionImporter(
            source=read_source,
            destination=writable,
            manifest=manifest,
            checkpoint_path=checkpoint,
            source_fingerprint=fingerprint,
        ).run()


def test_checkpoint_bound_to_another_manifest_fails_closed(tmp_path: Path) -> None:
    service, source, source_manifest, fingerprint = _seed_source(tmp_path)
    destination = service.initialize(tmp_path / "v2", format_version=2)
    request = _request(source_manifest.workspace_id, destination.manifest.workspace_id, fingerprint)
    with service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as read_source:
        manifest = build_historical_selection(read_source, request)
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        HistoricalSelectionCheckpoint(
            selection_id=uuid4(),
            selection_digest="f" * 64,
            source_workspace_id=manifest.source_workspace_id,
            destination_workspace_id=manifest.destination_workspace_id,
            phase="IMPORTING",
            raw_cursor=0,
            observation_cursor=0,
            document_cursor=0,
            created_raw=0,
            reused_raw=0,
            created_observations=0,
            reused_observations=0,
            updated_at=_KNOWN_AT,
        ).model_dump_json(),
        encoding="utf-8",
    )
    with (
        service.open_storage(source.paths, WorkspaceAccessMode.READ_ONLY) as read_source,
        service.open_storage(destination.paths, WorkspaceAccessMode.READ_WRITE) as writable,
        pytest.raises(HistoricalSelectionError),
    ):
        HistoricalSelectionImporter(
            source=read_source,
            destination=writable,
            manifest=manifest,
            checkpoint_path=checkpoint,
            source_fingerprint=fingerprint,
        ).run()
