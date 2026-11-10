"""Structural read-path coverage for compact 13F observation pages."""

import hashlib
import json
import resource
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalHoldingsSemanticsService,
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_NOW = datetime(2025, 2, 18, 12, tzinfo=UTC)
_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
</edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>037833100</cusip><value>60</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt>
<sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable></informationTable>"""


class _Submissions:
    def fetch(self, filer_cik):
        accepted = datetime(2025, 2, 14, 18, tzinfo=UTC)
        suffix = 1 if filer_cik == "1067983" else 2
        return RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{filer_cik}:submissions",
                retrieved_at=accepted,
            ),
            event_time=accepted,
            available_at=accepted,
            received_at=accepted,
            payload={
                "document": {
                    "cik": filer_cik,
                    "name": "Manager LLC",
                    "filings": {
                        "recent": {
                            "accessionNumber": [f"0000950123-25-00000{suffix}"],
                            "filingDate": ["2025-02-14"],
                            "reportDate": ["2024-12-31"],
                            "acceptanceDateTime": ["2025-02-14T18:00:00Z"],
                            "form": ["13F-HR"],
                            "primaryDocument": ["primary_doc.xml"],
                        }
                    },
                }
            },
            schema_version="sec-manager-submissions-snapshot-v1",
        )


class _Documents:
    retrieved_at = datetime(2025, 2, 15, tzinfo=UTC)

    def fetch_manifest(self, document):
        del document
        return SecAccessionManifest(
            entries=("primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self.retrieved_at,
        )

    def fetch(self, document):
        content = _COVER if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self.retrieved_at,
        )


def _seed(root: Path):
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        reports = []
        for manager_cik in ("1067983", "0001234567"):
            reports.extend(
                sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
                    storage, _Submissions(), _Documents()
                ).run(
                    sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                        filer_cik=manager_cik, forms=("13F-HR",)
                    )
                )
            )
        correspondence = InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip="037833100",
            title_of_class="COM",
            effective_from=reports[0].report_period - timedelta(days=365),
            effective_to=None,
            available_at=_NOW,
            recorded_at=_NOW,
        )
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            correspondence, catalog_version=1, declared_by="test"
        )
        for report in reports:
            InstitutionalHoldingsSemanticsService(storage, clock=lambda: _NOW).enrich(
                InstitutionalSemanticsEnrichRequest(
                    manager_cik=report.manager_cik,
                    report_ids=(report.report_id,),
                    known_at=_NOW,
                )
            )
            InstitutionalObservationService(
                storage, clock=lambda: _NOW + timedelta(hours=1)
            ).normalize(
                InstitutionalObservationRequest(
                    asset_id="equity:us:aapl",
                    manager_cik=report.manager_cik,
                    report_ids=(report.report_id,),
                    known_at=_NOW,
                )
            )
    return tuple(report.report_id for report in reports)


def _foreign_corpus(root: Path, *, multiplier: int) -> set[UUID]:
    foreign_ids: set[UUID] = set()
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        first = storage.observations.list(asset_id="equity:us:aapl")[0]
        for index in range(10 * multiplier):
            record_id = uuid4()
            foreign_ids.add(record_id)
            storage.raw_records.save(
                RawRecord(
                    record_id=record_id,
                    asset_id="equity:us:amd",
                    source=SourceReference(
                        source_id="foreign:corpus",
                        retrieved_at=_NOW,
                    ),
                    event_time=_NOW,
                    available_at=_NOW,
                    received_at=_NOW,
                    payload={"kind": "foreign", "index": index},
                    schema_version="foreign-corpus-v1",
                )
            )
            storage.observations.save(
                first.model_copy(
                    update={
                        "observation_id": uuid4(),
                        "asset_id": "equity:us:amd",
                        "raw_record_id": record_id,
                    }
                )
            )
    return foreign_ids


def _query(
    service: InstitutionalObservationService,
) -> tuple[object, object, dict[str, object]]:
    raw_ids: list[UUID] = []
    artifact_ids: list[UUID] = []
    claim_batches: list[tuple[UUID, ...]] = []
    report_ids: list[UUID] = []

    original_raw_many = service._storage.raw_records.get_many

    def spy_raw_many(record_ids):
        raw_ids.extend(record_ids)
        return original_raw_many(record_ids)

    original_artifact_get = InstitutionalSemanticsRepository.get

    def spy_artifact_get(self, artifact_id):
        artifact_ids.append(artifact_id)
        return original_artifact_get(self, artifact_id)

    original_claim_many = SecInstitutionalRowCorrespondenceRepository.get_many

    def spy_claim_many(self, ids, *, known_at, asset_id=None):
        claim_batches.append(tuple(ids))
        return original_claim_many(self, ids, known_at=known_at, asset_id=asset_id)

    original_report_get = InstitutionalHoldingsRepository.get_report

    def spy_report_get(self, report_id):
        report_ids.append(report_id)
        return original_report_get(self, report_id)

    patcher = pytest.MonkeyPatch()
    patcher.setattr(service._storage.raw_records, "get_many", spy_raw_many)
    patcher.setattr(InstitutionalSemanticsRepository, "get", spy_artifact_get)
    patcher.setattr(SecInstitutionalRowCorrespondenceRepository, "get_many", spy_claim_many)
    patcher.setattr(InstitutionalHoldingsRepository, "get_report", spy_report_get)
    try:
        query = InstitutionalObservationQuery(
            asset_id="equity:us:aapl",
            known_at=_NOW + timedelta(days=1),
            offset=0,
            limit=200,
        )
        started = time.perf_counter()
        full = service.query(query)
        full_s = time.perf_counter() - started
        full_metrics = {
            "raw_ids": tuple(raw_ids),
            "artifact_ids": tuple(artifact_ids),
            "claim_batches": tuple(claim_batches),
            "report_ids": tuple(report_ids),
        }
        raw_ids.clear()
        artifact_ids.clear()
        claim_batches.clear()
        report_ids.clear()
        started = time.perf_counter()
        summary = service.query_summary(query)
        summary_s = time.perf_counter() - started
        summary_metrics = {
            "raw_ids": tuple(raw_ids),
            "artifact_ids": tuple(artifact_ids),
            "claim_batches": tuple(claim_batches),
            "report_ids": tuple(report_ids),
        }
    finally:
        patcher.undo()
    metrics = {
        "full": full_metrics,
        "summary": summary_metrics,
        "full_s": round(full_s, 4),
        "summary_s": round(summary_s, 4),
    }
    return full, summary, metrics


def test_summary_reads_one_parent_per_identity_and_never_foreign_corpus(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _seed(root)
    with LocalStorage(StoragePaths.from_root(root), read_only=True) as storage:
        service = InstitutionalObservationService(storage)
        full, summary, metrics = _query(service)
        first_ids = [item.observation.observation_id for item in summary.observations]
        first_values = [
            (item.observation.field_name, item.observation.value, item.observation.available_at)
            for item in summary.observations
        ]
        first_total = summary.total_matching
        expected_artifacts = {item.artifact_reference.artifact_id for item in summary.observations}
        expected_reports = {item.report.report_id for item in summary.observations}
        expected_claims = {item.correspondence_id for item in summary.observations}
        assert summary.total_matching == full.total_matching
        assert summary.truncated == full.truncated
        assert first_ids == [view.observation.observation_id for view in full.observations]
        for query_metrics in (metrics["full"], metrics["summary"]):
            assert set(query_metrics["artifact_ids"]) == expected_artifacts
            assert len(query_metrics["artifact_ids"]) == len(expected_artifacts)
            assert set(query_metrics["report_ids"]) == expected_reports
            assert len(query_metrics["report_ids"]) == len(expected_reports)
            assert len(query_metrics["claim_batches"]) == 1
            assert set(query_metrics["claim_batches"][0]) == expected_claims
        payload = summary.model_dump(mode="json")
        assert payload["schema_version"] == "institutional-observation-summary-v1"
        assert "artifact" not in payload["observations"][0]
        assert "correspondence" not in payload["observations"][0]
        assert "rows" not in payload["observations"][0]
        assert payload["observations"][0]["artifact_reference"]["artifact_id"]
        assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > 0

    foreign_ids = _foreign_corpus(root, multiplier=1)
    with LocalStorage(StoragePaths.from_root(root), read_only=True) as storage:
        service = InstitutionalObservationService(storage)
        _, summary, metrics = _query(service)
        assert [item.observation.observation_id for item in summary.observations] == first_ids
        assert [
            (item.observation.field_name, item.observation.value, item.observation.available_at)
            for item in summary.observations
        ] == first_values
        assert summary.total_matching == first_total
        assert not foreign_ids.intersection(metrics["summary"]["raw_ids"])
        assert set(metrics["summary"]["artifact_ids"]) == expected_artifacts

    _foreign_corpus(root, multiplier=9)
    with LocalStorage(StoragePaths.from_root(root), read_only=True) as storage:
        service = InstitutionalObservationService(storage)
        _, summary, metrics = _query(service)
        assert [item.observation.observation_id for item in summary.observations] == first_ids
        assert summary.total_matching == first_total
        assert not foreign_ids.intersection(metrics["summary"]["raw_ids"])
        assert set(metrics["summary"]["artifact_ids"]) == expected_artifacts
        print(
            f"13f_summary_reads: page={len(summary.observations)} "
            f"total={summary.total_matching} "
            f"artifacts={len(set(metrics['summary']['artifact_ids']))} "
            f"claim_batches={len(metrics['summary']['claim_batches'])} "
            f"full_s={metrics['full_s']} summary_s={metrics['summary_s']} "
            f"peak_rss_kib={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}"
        )
