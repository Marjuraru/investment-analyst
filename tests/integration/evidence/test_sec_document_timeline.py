"""Integration tests for the SEC document timeline application boundary and CLI."""

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_document_corpus import SecDocumentCorpusApplication
from investment_analyst.application.sec_document_timeline import (
    SecDocumentTimelineApplication,
    SecDocumentTimelineApplicationError,
)
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentQuery,
    SecDocumentRevision,
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import revision_to_raw_record
from investment_analyst.evidence.sec_documents.timeline_models import SecDocumentTimelineQuery
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    filer_revision_to_raw_record,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "query_sec_document_timeline.py"


def _seed_workspace(workspace_root: Path) -> None:
    ws_service = WorkspaceService()
    runtime = ApplicationRuntime.create_default(workspace_service=ws_service)
    ws_service.initialize(explicit_path=workspace_root)
    loc = StorageLocationRequest(workspace=workspace_root)
    with runtime.open_storage(loc, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        # Asset document
        accepted_at = datetime(2025, 2, 10, 18, tzinfo=UTC)
        filing = SecFiling(
            filing_id=SecFiling.expected_id("0000320193", "0000320193-25-000001"),
            filer_cik="0000320193",
            accession="0000320193-25-000001",
            form="10-K",
            filing_date=date(2025, 2, 10),
            report_date=date(2024, 12, 31),
            accepted_at=accepted_at,
            is_amendment=False,
        )
        doc = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing.filing_id, "primary.htm"),
            filing=filing,
            name="primary.htm",
        )
        receipt = storage.documents.put(b"<html content/>")
        rev_id = SecDocumentRevision.expected_id(
            doc.document_id, receipt.sha256, REVISION_SCHEMA_VERSION_V2
        )
        discovery = RawRecord(
            record_id=uuid4(),
            asset_id="equity:us:aapl",
            source=SourceReference(
                source_id="sec-edgar:aapl:submissions", retrieved_at=accepted_at
            ),
            event_time=accepted_at,
            available_at=accepted_at,
            received_at=accepted_at,
            payload={"document": {"cik": "0000320193"}},
            schema_version="sec-submissions-v1",
        )
        storage.raw_records.save(discovery)
        revision = SecDocumentRevision(
            revision_id=rev_id,
            asset_id="equity:us:aapl",
            document=doc,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(rev_id),
            discovery_raw_record_id=discovery.record_id,
            content_sha256=receipt.sha256,
            content_size_bytes=receipt.size_bytes,
            available_at=accepted_at,
            retrieved_at=accepted_at,
            source_url="https://www.sec.gov/primary.htm",
            revision_schema_version=REVISION_SCHEMA_VERSION_V2,
        )
        storage.raw_records.save(revision_to_raw_record(revision))

        # Filer document
        filer_accepted = datetime(2025, 2, 12, 18, tzinfo=UTC)
        filer_filing = SecFiling(
            filing_id=SecFiling.expected_id("0001067983", "0000950123-25-000001"),
            filer_cik="0001067983",
            accession="0000950123-25-000001",
            form="13F-HR",
            filing_date=date(2025, 2, 12),
            report_date=None,
            accepted_at=filer_accepted,
            is_amendment=False,
        )
        filer_doc = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filer_filing.filing_id, "primary_doc.xml"),
            filing=filer_filing,
            name="primary_doc.xml",
        )
        filer_receipt = storage.documents.put(b"<filer xml/>")
        filer_rev_id = SecFilerDocumentRevision.expected_id(
            filer_doc.document_id, filer_receipt.sha256
        )
        filer_disc = RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id="sec-edgar:manager:0001067983:submissions",
                retrieved_at=filer_accepted,
            ),
            event_time=filer_accepted,
            available_at=filer_accepted,
            received_at=filer_accepted,
            payload={"document": {"cik": "0001067983"}},
            schema_version="sec-manager-submissions-snapshot-v1",
        )
        storage.raw_records.save(filer_disc)
        filer_revision = SecFilerDocumentRevision(
            revision_id=filer_rev_id,
            filer_cik="0001067983",
            document=filer_doc,
            raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(filer_rev_id),
            discovery_raw_record_id=filer_disc.record_id,
            content_sha256=filer_receipt.sha256,
            content_size_bytes=filer_receipt.size_bytes,
            available_at=filer_accepted,
            retrieved_at=filer_accepted,
            source_url="https://www.sec.gov/primary_doc.xml",
        )
        storage.raw_records.save(filer_revision_to_raw_record(filer_revision))


def test_cli_requires_explicit_known_at_and_location(tmp_path: Path) -> None:
    # 1. Missing known-at
    proc1 = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--workspace", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc1.returncode != 0
    assert "--known-at" in proc1.stderr

    # 2. Missing location
    proc2 = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--known-at", "2025-02-15T00:00:00Z"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode != 0
    assert "one of the arguments --workspace --legacy-root" in proc2.stderr

    # 3. Successful run with explicit arguments
    _seed_workspace(tmp_path)
    proc3 = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--workspace",
            str(tmp_path),
            "--known-at",
            "2025-02-15T00:00:00Z",
            "--asset-id",
            "equity:us:aapl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc3.returncode == 0
    data = json.loads(proc3.stdout)
    assert data["state"] == "found"
    assert data["matched_count"] == 1


def test_asset_without_sec_configuration_fails_closed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    app = SecDocumentTimelineApplication.create_default()
    loc = StorageLocationRequest(workspace=tmp_path)

    # Calling with an asset without SEC config in catalog (e.g. crypto:spot:btc-usd)
    with pytest.raises(SecDocumentTimelineApplicationError, match="has no SEC configuration"):
        app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 15, tzinfo=UTC),
                asset_ids=("crypto:spot:btc-usd",),
            ),
            location=loc,
        )

    # Calling via CLI fails closed with exit code 1
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--workspace",
            str(tmp_path),
            "--known-at",
            "2025-02-15T00:00:00Z",
            "--asset-id",
            "crypto:spot:btc-usd",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "has no SEC configuration" in proc.stderr


def test_cli_full_flow_with_both_families(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--workspace",
            str(tmp_path),
            "--known-at",
            "2025-02-20T00:00:00Z",
            "--asset-id",
            "equity:us:aapl",
            "--filer-cik",
            "0001067983",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["state"] == "found"
    assert data["matched_count"] == 2
    assert data["returned_count"] == 2
    assert data["truncated"] is False
    assert len(data["entries"]) == 2

    # Check entries have both families and correct fields
    families = {e["family"] for e in data["entries"]}
    assert families == {"asset_document", "filer_document"}
    for e in data["entries"]:
        assert "content" not in e
        assert "content_sha256" in e
        assert "content_size_bytes" in e

    # Test with limit
    proc_limit = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--workspace",
            str(tmp_path),
            "--known-at",
            "2025-02-20T00:00:00Z",
            "--asset-id",
            "equity:us:aapl",
            "--filer-cik",
            "0001067983",
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_limit.returncode == 0
    data_limit = json.loads(proc_limit.stdout)
    assert data_limit["matched_count"] == 2
    assert data_limit["returned_count"] == 1
    assert data_limit["truncated"] is True
    assert len(data_limit["entries"]) == 1


def test_document_replay_contract_unchanged(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    replay_app = SecDocumentCorpusApplication.create_default()
    result = replay_app.replay(
        query=SecDocumentQuery(
            asset_id="equity:us:aapl",
            known_at=datetime(2025, 2, 20, tzinfo=UTC),
            form="10-K",
        ),
        location=StorageLocationRequest(workspace=tmp_path),
    )
    assert result.state == "found"
    assert result.revision is not None
    assert result.revision.asset_id == "equity:us:aapl"


def test_no_full_text_search_or_fragment_extraction() -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    help_text = proc.stdout.lower()
    forbidden_terms = [
        "full-text",
        "search-text",
        "fragment",
        "embedding",
        "vector",
        "score",
        "rank",
    ]
    for forbidden in forbidden_terms:
        assert forbidden not in help_text
