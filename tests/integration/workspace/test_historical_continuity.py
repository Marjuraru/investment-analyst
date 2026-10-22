"""Integration tests for the historical continuity pilot and archive restore."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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
from investment_analyst.workspace.continuity_archive import (
    ContinuityArchiveWriter,
    load_continuity_archive_manifest,
    restore_continuity_archive,
    verify_continuity_archive,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "manage_historical_continuity.py"
_KNOWN_AT = datetime(2026, 10, 22, 12, tzinfo=UTC)
_AMD = "equity:us:amd"
_MARKET_SOURCE = "fixture:market"
_FUNDAMENTAL_SOURCE = "sec-edgar:companyfacts"
_DOCUMENT_SOURCE = "sec-edgar:primary-documents"


def _reference(source_id: str, key: str) -> SourceReference:
    return SourceReference(source_id=source_id, record_key=key, retrieved_at=_KNOWN_AT)


def _raw(
    source_id: str,
    key: str,
    payload: dict[str, object],
    at: datetime,
    schema: str,
) -> RawRecord:
    return RawRecord(
        record_id=uuid4(),
        asset_id=_AMD,
        source=_reference(source_id, key),
        event_time=at,
        available_at=at,
        received_at=at,
        payload=payload,
        schema_version=schema,
    )


def _observation(
    record: RawRecord,
    *,
    field_name: str,
    value: Decimal,
    frequency: DataFrequency,
    observed_at: datetime | None,
    period_end: datetime | None,
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
        available_at=record.available_at,
        normalized_at=record.received_at + timedelta(minutes=1),
        source=record.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )


def _seed_source(root: Path) -> WorkspaceService:
    service = WorkspaceService(environ={}, home=root / "home")
    initialized = service.initialize(root / "v1", format_version=1)
    storage = service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE)
    storage.open()
    for offset in range(40):
        observed = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=offset)
        record = _raw(
            _MARKET_SOURCE,
            f"AMD:{observed.date()}",
            {"close": str(offset)},
            observed + timedelta(hours=1),
            "market-v1",
        )
        storage.raw_records.save(record)
        storage.observations.save(
            _observation(
                record,
                field_name="close",
                value=Decimal(offset),
                frequency=DataFrequency.DAY_1,
                observed_at=observed,
                period_end=None,
            )
        )
    for year in (2022, 2023, 2024):
        at = datetime(year + 1, 2, 1, tzinfo=UTC)
        record = _raw(
            _FUNDAMENTAL_SOURCE,
            f"AMD:annual:{year}",
            {"value": year},
            at,
            "companyfacts-v1",
        )
        storage.raw_records.save(record)
        storage.observations.save(
            _observation(
                record,
                field_name="Revenues",
                value=Decimal(year),
                frequency=DataFrequency.ANNUAL,
                observed_at=None,
                period_end=datetime(year, 12, 31, tzinfo=UTC),
            )
        )
    for quarter in range(1, 5):
        period_end = datetime(2024, quarter * 3, 1, tzinfo=UTC) - timedelta(days=1)
        at = period_end + timedelta(days=45)
        record = _raw(
            _FUNDAMENTAL_SOURCE,
            f"AMD:quarterly:2024:{quarter}",
            {"value": quarter},
            at,
            "companyfacts-v1",
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
        "sec-edgar:amd:submissions",
        "AMD:submissions:2025",
        {"cik": "0000002488"},
        accepted_at,
        "sec-submissions-v1",
    )
    storage.raw_records.save(discovery)
    storage.raw_records.save(
        revision_to_raw_record(
            SecDocumentRevision(
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
        )
    )
    storage.close()
    return service


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(*arguments: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), *arguments],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def test_historical_pilot_selects_imports_and_replays_without_touching_source(
    tmp_path: Path,
) -> None:
    service = _seed_source(tmp_path)
    source_root = tmp_path / "v1"
    destination = service.initialize(tmp_path / "v2", format_version=2)
    source_before = _tree_digest(source_root)
    manifest_path = tmp_path / "selection.json"
    checkpoint = tmp_path / "checkpoint.json"
    select = _run(
        "select",
        "--source-workspace",
        str(source_root),
        "--destination-workspace",
        str(destination.paths.root),
        "--known-at",
        "2026-10-22T12:00:00Z",
        "--market-selection",
        f"{_AMD}|{_MARKET_SOURCE}|2026-07-01|2026-07-10",
        "--warmup-observations",
        "2",
        "--fundamental-asset",
        _AMD,
        "--fundamental-source",
        _FUNDAMENTAL_SOURCE,
        "--fundamental-start",
        "2023-01-01",
        "--document-asset",
        _AMD,
        "--document-source",
        _DOCUMENT_SOURCE,
        "--output",
        str(manifest_path),
    )
    assert select["items"] > 0
    assert select["documents"] == 1
    assert select["exclusions"] == 0

    first = _run(
        "import",
        "--source-workspace",
        str(source_root),
        "--destination-workspace",
        str(destination.paths.root),
        "--manifest",
        str(manifest_path),
        "--checkpoint",
        str(checkpoint),
    )
    assert first["created_raw"] > 0
    assert first["created_observations"] > 0
    assert first["replay"] is False

    second = _run(
        "import",
        "--source-workspace",
        str(source_root),
        "--destination-workspace",
        str(destination.paths.root),
        "--manifest",
        str(manifest_path),
        "--checkpoint",
        str(checkpoint),
    )
    assert second["replay"] is True
    assert second["created_raw"] == 0
    assert second["created_observations"] == 0
    assert second["source_unchanged"] is True
    assert _tree_digest(source_root) == source_before


def test_continuity_archive_restores_selected_bytes_by_streaming(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    (source / "storage" / "v2").mkdir(parents=True)
    (source / "storage" / "v2" / "index.duckdb").write_bytes(b"d" * 4099)
    (source / "data" / "documents").mkdir(parents=True)
    (source / "data" / "documents" / "filing.htm").write_bytes(b"<html>filing</html>")
    writer = ContinuityArchiveWriter(
        tmp_path / "archive",
        source_workspace_id=uuid4(),
        source_fingerprint="a" * 64,
        code_sha="0" * 40,
        chunk_bytes=1024,
        buffer_bytes=4096,
    )
    writer.add_file(source / "storage" / "v2" / "index.duckdb", "storage/v2/index.duckdb")
    writer.add_file(source / "data" / "documents" / "filing.htm", "data/documents/filing.htm")
    manifest = writer.finalize()
    verify_continuity_archive(writer.root, manifest)
    restored = restore_continuity_archive(
        writer.root,
        manifest,
        tmp_path / "restore",
        paths=("data/documents/filing.htm",),
    )
    assert restored == ("data/documents/filing.htm",)
    assert (tmp_path / "restore" / "data" / "documents" / "filing.htm").read_bytes() == (
        b"<html>filing</html>"
    )
    reloaded = load_continuity_archive_manifest(writer.root)
    assert reloaded.digest == manifest.digest
