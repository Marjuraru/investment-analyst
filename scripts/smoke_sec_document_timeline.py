#!/usr/bin/env python3
"""Local reproducible smoke test for the SEC document timeline."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_document_timeline import (
    SecDocumentTimelineApplication,
)
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION,
    REVISION_SCHEMA_VERSION_V2,
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

_AAPL_ASSET_ID = "equity:us:aapl"
_AAPL_CIK = "0000320193"
_BRK_CIK = "0001067983"


def _seed_smoke_workspace(workspace_root: Path) -> None:
    ws_service = WorkspaceService()
    runtime = ApplicationRuntime.create_default(workspace_service=ws_service)
    ws_service.initialize(explicit_path=workspace_root)
    loc = StorageLocationRequest(workspace=workspace_root)

    with runtime.open_storage(loc, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        # 1. Asset doc 1 (10-K, accepted 2025-02-10T18:00:00Z)
        t1 = datetime(2025, 2, 10, 18, tzinfo=UTC)
        filing1 = SecFiling(
            filing_id=SecFiling.expected_id(_AAPL_CIK, "0000320193-25-000001"),
            filer_cik=_AAPL_CIK,
            accession="0000320193-25-000001",
            form="10-K",
            filing_date=date(2025, 2, 10),
            report_date=date(2024, 12, 31),
            accepted_at=t1,
            is_amendment=False,
        )
        doc1 = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing1.filing_id, "primary.htm"),
            filing=filing1,
            name="primary.htm",
        )
        receipt1 = storage.documents.put(b"<aapl 10-K/>")
        rev_id1 = SecDocumentRevision.expected_id(
            doc1.document_id, receipt1.sha256, REVISION_SCHEMA_VERSION_V2
        )
        disc1 = RawRecord(
            record_id=uuid4(),
            asset_id=_AAPL_ASSET_ID,
            source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=t1),
            event_time=t1,
            available_at=t1,
            received_at=t1,
            payload={"document": {"cik": _AAPL_CIK}},
            schema_version="sec-submissions-v1",
        )
        storage.raw_records.save(disc1)
        rev1 = SecDocumentRevision(
            revision_id=rev_id1,
            asset_id=_AAPL_ASSET_ID,
            document=doc1,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(rev_id1),
            discovery_raw_record_id=disc1.record_id,
            content_sha256=receipt1.sha256,
            content_size_bytes=receipt1.size_bytes,
            available_at=t1,
            retrieved_at=t1,
            source_url="https://www.sec.gov/Archives/10-K.htm",
            revision_schema_version=REVISION_SCHEMA_VERSION_V2,
        )
        storage.raw_records.save(revision_to_raw_record(rev1))

        # 2. Asset doc 2 (10-Q, accepted 2025-02-25T18:00:00Z) - FUTURE relative to initial cut
        t2 = datetime(2025, 2, 25, 18, tzinfo=UTC)
        filing2 = SecFiling(
            filing_id=SecFiling.expected_id(_AAPL_CIK, "0000320193-25-000002"),
            filer_cik=_AAPL_CIK,
            accession="0000320193-25-000002",
            form="10-Q",
            filing_date=date(2025, 2, 25),
            report_date=date(2025, 3, 31),
            accepted_at=t2,
            is_amendment=False,
        )
        doc2 = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing2.filing_id, "primary.htm"),
            filing=filing2,
            name="primary.htm",
        )
        receipt2 = storage.documents.put(b"<aapl 10-Q/>")
        rev_id2 = SecDocumentRevision.expected_id(
            doc2.document_id, receipt2.sha256, REVISION_SCHEMA_VERSION_V2
        )
        disc2 = RawRecord(
            record_id=uuid4(),
            asset_id=_AAPL_ASSET_ID,
            source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=t2),
            event_time=t2,
            available_at=t2,
            received_at=t2,
            payload={"document": {"cik": _AAPL_CIK}},
            schema_version="sec-submissions-v1",
        )
        storage.raw_records.save(disc2)
        rev2 = SecDocumentRevision(
            revision_id=rev_id2,
            asset_id=_AAPL_ASSET_ID,
            document=doc2,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(rev_id2),
            discovery_raw_record_id=disc2.record_id,
            content_sha256=receipt2.sha256,
            content_size_bytes=receipt2.size_bytes,
            available_at=t2,
            retrieved_at=t2,
            source_url="https://www.sec.gov/Archives/10-Q.htm",
            revision_schema_version=REVISION_SCHEMA_VERSION_V2,
        )
        storage.raw_records.save(revision_to_raw_record(rev2))

        # 3. Filer doc 1 (13F-HR, accepted 2025-02-14T23:30:00Z)
        t3 = datetime(2025, 2, 14, 23, 30, tzinfo=UTC)
        filing3 = SecFiling(
            filing_id=SecFiling.expected_id(_BRK_CIK, "0000950123-25-000001"),
            filer_cik=_BRK_CIK,
            accession="0000950123-25-000001",
            form="13F-HR",
            filing_date=date(2025, 2, 14),
            report_date=None,
            accepted_at=t3,
            is_amendment=False,
        )
        doc3 = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing3.filing_id, "primary_doc.xml"),
            filing=filing3,
            name="primary_doc.xml",
        )
        receipt3 = storage.documents.put(b"<brk 13F/>")
        rev_id3 = SecFilerDocumentRevision.expected_id(doc3.document_id, receipt3.sha256)
        disc3 = RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{_BRK_CIK}:submissions", retrieved_at=t3
            ),
            event_time=t3,
            available_at=t3,
            received_at=t3,
            payload={"document": {"cik": _BRK_CIK}},
            schema_version="sec-manager-submissions-snapshot-v1",
        )
        storage.raw_records.save(disc3)
        rev3 = SecFilerDocumentRevision(
            revision_id=rev_id3,
            filer_cik=_BRK_CIK,
            document=doc3,
            raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(rev_id3),
            discovery_raw_record_id=disc3.record_id,
            content_sha256=receipt3.sha256,
            content_size_bytes=receipt3.size_bytes,
            available_at=t3,
            retrieved_at=t3,
            source_url="https://www.sec.gov/Archives/13F.xml",
        )
        storage.raw_records.save(filer_revision_to_raw_record(rev3))

        # 4. Legacy v1 asset doc (accepted 2024-02-10)
        t_leg = datetime(2024, 2, 10, 18, tzinfo=UTC)
        filing_leg = SecFiling(
            filing_id=SecFiling.expected_id(_AAPL_CIK, "0000320193-24-000001"),
            filer_cik=_AAPL_CIK,
            accession="0000320193-24-000001",
            form="10-K",
            filing_date=date(2024, 2, 10),
            report_date=date(2023, 12, 31),
            accepted_at=t_leg,
            is_amendment=False,
        )
        doc_leg = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing_leg.filing_id, "primary.htm"),
            filing=filing_leg,
            name="primary.htm",
        )
        receipt_leg = storage.documents.put(b"<legacy v1/>")
        rev_id_leg = SecDocumentRevision.expected_id(
            doc_leg.document_id, receipt_leg.sha256, REVISION_SCHEMA_VERSION
        )
        disc_leg = RawRecord(
            record_id=uuid4(),
            asset_id=_AAPL_ASSET_ID,
            source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=t_leg),
            event_time=t_leg,
            available_at=t_leg,
            received_at=t_leg,
            payload={"document": {"cik": _AAPL_CIK}},
            schema_version="sec-submissions-v1",
        )
        storage.raw_records.save(disc_leg)
        rev_leg = SecDocumentRevision(
            revision_id=rev_id_leg,
            asset_id=_AAPL_ASSET_ID,
            document=doc_leg,
            raw_record_id=SecDocumentRevision.expected_raw_record_id(rev_id_leg),
            discovery_raw_record_id=disc_leg.record_id,
            content_sha256=receipt_leg.sha256,
            content_size_bytes=receipt_leg.size_bytes,
            available_at=t_leg,
            retrieved_at=t_leg,
            source_url="https://www.sec.gov/Archives/legacy.htm",
            revision_schema_version=REVISION_SCHEMA_VERSION,
        )
        storage.raw_records.save(revision_to_raw_record(rev_leg))


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace_root = Path(temp_dir)
        _seed_smoke_workspace(workspace_root)

        app = SecDocumentTimelineApplication.create_default()
        loc = StorageLocationRequest(workspace=workspace_root)

        # Step 1: Point-in-time cut excludes later revisions
        cut_t1 = datetime(2025, 2, 20, 0, tzinfo=UTC)
        res_pit = app.query(
            query=SecDocumentTimelineQuery(
                known_at=cut_t1,
                asset_ids=(_AAPL_ASSET_ID,),
                filer_ciks=(_BRK_CIK,),
            ),
            location=loc,
        )
        assert res_pit.state == "found"
        # rev1 (Feb 10) and rev3 (Feb 14); rev2 (Feb 25) is excluded
        assert res_pit.matched_count == 2
        assert res_pit.legacy_records_excluded == 1
        assert not any(e.accession == "0000320193-25-000002" for e in res_pit.entries)

        # Step 2: Inclusive public range keeps final date
        # Feb 14 document was accepted at 23:30 UTC. Range 2025-02-10 to 2025-02-14 must include it.
        res_range = app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                filer_ciks=(_BRK_CIK,),
                available_from=date(2025, 2, 10),
                available_to=date(2025, 2, 14),
            ),
            location=loc,
        )
        assert res_range.state == "found"
        assert res_range.matched_count == 1
        assert res_range.entries[0].accession == "0000950123-25-000001"

        # Step 3: Separation of families
        # Asset document only
        res_asset = app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                asset_ids=(_AAPL_ASSET_ID,),
            ),
            location=loc,
        )
        assert all(e.family == "asset_document" for e in res_asset.entries)
        assert all(e.asset_id == _AAPL_ASSET_ID for e in res_asset.entries)

        # Filer document only
        res_filer = app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                filer_ciks=(_BRK_CIK,),
            ),
            location=loc,
        )
        assert all(e.family == "filer_document" for e in res_filer.entries)
        assert all(e.asset_id is None for e in res_filer.entries)

        # Step 4: Explicit missing state
        res_missing = app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                filer_ciks=(_BRK_CIK,),
                forms=("10-K",),  # Berkshire CIK has only 13F-HR in test workspace
            ),
            location=loc,
        )
        assert res_missing.state == "missing"
        assert res_missing.matched_count == 0
        assert res_missing.returned_count == 0
        assert len(res_missing.entries) == 0
        assert res_missing.truncated is False

        # Step 5: Limit and coherent counters
        res_limit = app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                asset_ids=(_AAPL_ASSET_ID,),
                limit=1,
            ),
            location=loc,
        )
        assert res_limit.matched_count == 2
        assert res_limit.returned_count == 1
        assert res_limit.truncated is True
        assert len(res_limit.entries) == 1

        # Step 6: Reproducibility between two executions
        exec1 = app.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                asset_ids=(_AAPL_ASSET_ID,),
                filer_ciks=(_BRK_CIK,),
            ),
            location=loc,
        )
        # Second execution with a newly instantiated application
        app2 = SecDocumentTimelineApplication.create_default()
        exec2 = app2.query(
            query=SecDocumentTimelineQuery(
                known_at=datetime(2025, 2, 28, 0, tzinfo=UTC),
                asset_ids=(_AAPL_ASSET_ID,),
                filer_ciks=(_BRK_CIK,),
            ),
            location=loc,
        )
        assert exec1.model_dump(mode="json") == exec2.model_dump(mode="json")

        summary = {
            "status": "PASS",
            "schema_version": "sec-document-timeline-smoke-v1",
            "workspace_temporary": True,
            "permanent_workspace_opened": False,
            "point_in_time_cut_verified": True,
            "inclusive_range_verified": True,
            "families_separated_verified": True,
            "missing_state_explicit_verified": True,
            "counters_coherent": True,
            "reproducibility_verified": True,
            "legacy_records_excluded": res_pit.legacy_records_excluded,
            "matched_count": exec1.matched_count,
            "returned_count": exec1.returned_count,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
