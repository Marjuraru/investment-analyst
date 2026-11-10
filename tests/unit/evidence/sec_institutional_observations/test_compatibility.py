"""Explicit, bounded compatibility between persisted 13F observations and their replay."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_observations.compatibility import (
    InstitutionalObservationCompatibilityError,
    _unambiguous_new_york_utc,
    compare_equivalent_reconstruction,
    compare_reconstruction,
    parse_record_key,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_observations.normalizer import normalize_row
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
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_COVER_TEMPLATE = """<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>{period}</reportCalendarOrQuarter>
</edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>037833100</cusip><value>60</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt>
<sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable></informationTable>"""


class _Submissions:
    def __init__(self, accepted: datetime, report_date) -> None:
        self._accepted = accepted
        self._report_date = report_date

    def fetch(self, filer_cik):
        accepted = self._accepted
        return RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{filer_cik}:submissions", retrieved_at=accepted
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
                            "accessionNumber": ["0000950123-25-000001"],
                            "filingDate": [accepted.date().isoformat()],
                            "reportDate": [self._report_date.isoformat()],
                            "acceptanceDateTime": [accepted.strftime("%Y-%m-%dT%H:%M:%SZ")],
                            "form": ["13F-HR"],
                            "primaryDocument": ["primary_doc.xml"],
                        }
                    },
                }
            },
            schema_version="sec-manager-submissions-snapshot-v1",
        )


class _Documents:
    def __init__(self, retrieved_at: datetime, cover: bytes) -> None:
        self.retrieved_at = retrieved_at
        self._cover = cover

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
        content = self._cover if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self.retrieved_at,
        )


def _seed(
    root: Path,
    *,
    accepted: datetime,
    report_date,
    now: datetime,
):
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        cover = _COVER_TEMPLATE.format(period=report_date.strftime("%m-%d-%Y")).encode()
        report = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _Submissions(accepted, report_date), _Documents(accepted, cover)
        ).run(
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                filer_cik="1067983", forms=("13F-HR",)
            )
        )[0]
        InstitutionalHoldingsSemanticsService(storage, clock=lambda: now).enrich(
            InstitutionalSemanticsEnrichRequest(
                manager_cik="1067983", report_ids=(report.report_id,), known_at=now
            )
        )
        correspondence = InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip="037833100",
            title_of_class="COM",
            effective_from=report.report_period - timedelta(days=365),
            effective_to=None,
            available_at=now,
            recorded_at=now,
        )
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            correspondence, catalog_version=1, declared_by="test"
        )
        artifact = InstitutionalSemanticsRepository(storage.raw_records).get_for_parent(report)
    return report, artifact, correspondence


def _with_acceptance(artifact, accepted: datetime, *, parsed_at: datetime):
    filing = artifact.cover_revision.document.filing.model_copy(update={"accepted_at": accepted})
    document = artifact.cover_revision.document.model_copy(update={"filing": filing})
    cover = artifact.cover_revision.model_copy(update={"document": document})
    return artifact.model_copy(
        update={"cover_revision": cover, "available_at": accepted, "parsed_at": parsed_at},
        deep=True,
    )


def _legacy_row_observations(
    artifact,
    correspondence,
    row,
    *,
    old_accepted: datetime,
    old_parsed_at: datetime,
    old_normalized_at: datetime,
):
    legacy_artifact = _with_acceptance(artifact, old_accepted, parsed_at=old_parsed_at)
    return normalize_row(legacy_artifact, row, correspondence, normalized_at=old_normalized_at)


def _legacy_observations(
    artifact,
    correspondence,
    *,
    old_accepted: datetime,
    old_parsed_at: datetime,
    old_normalized_at: datetime,
):
    observations = []
    for row in artifact.rows:
        observations.extend(
            _legacy_row_observations(
                artifact,
                correspondence,
                row,
                old_accepted=old_accepted,
                old_parsed_at=old_parsed_at,
                old_normalized_at=old_normalized_at,
            )
        )
    return observations


def _current_row_observations(artifact, correspondence, row, *, normalized_at: datetime):
    return {
        item.field_name: item
        for item in normalize_row(artifact, row, correspondence, normalized_at=normalized_at)
    }


def _current_observations(artifact, correspondence, *, normalized_at: datetime):
    observations = []
    for row in artifact.rows:
        observations.extend(
            normalize_row(artifact, row, correspondence, normalized_at=normalized_at)
        )
    return observations


def _save_legacy(storage, observations) -> None:
    for observation in observations:
        storage.observations.save(observation)


def _query(root: Path, known_at: datetime):
    with LocalStorage(StoragePaths.from_root(root), read_only=True) as storage:
        return InstitutionalObservationService(storage).query(
            InstitutionalObservationQuery(
                asset_id="equity:us:aapl",
                known_at=known_at,
                manager_cik="1067983",
                limit=100,
            )
        )


def test_exact_replay_reuses_observations_without_compatibility_notes(tmp_path: Path) -> None:
    now = datetime(2025, 2, 16, 12, tzinfo=UTC)
    accepted = datetime(2025, 2, 14, 23, tzinfo=UTC)
    report, artifact, correspondence = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2024, 12, 31).date(),
        now=now,
    )
    current = _current_observations(
        artifact, correspondence, normalized_at=now + timedelta(hours=1)
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _save_legacy(storage, current)
        before = storage.observations.count(
            source_id="sec-edgar:institutional-holdings-observations"
        )
        summary = InstitutionalObservationService(
            storage, clock=lambda: now + timedelta(hours=2)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=now,
            )
        )
        after = storage.observations.count(
            source_id="sec-edgar:institutional-holdings-observations"
        )
    result = _query(tmp_path, now + timedelta(days=1))
    assert summary.observations_created == 0
    assert summary.observations_reused == 4
    assert before == after == 4
    assert {view.compatibility_notes for view in result.observations} == {()}


def test_replayed_retrieval_clock_is_explained_and_original_is_returned(tmp_path: Path) -> None:
    now = datetime(2025, 2, 16, 12, tzinfo=UTC)
    accepted = datetime(2025, 2, 14, 23, tzinfo=UTC)
    report, artifact, correspondence = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2024, 12, 31).date(),
        now=now,
    )
    legacy = _legacy_observations(
        artifact,
        correspondence,
        old_accepted=accepted,
        old_parsed_at=now - timedelta(hours=1),
        old_normalized_at=now,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _save_legacy(storage, legacy)
        summary = InstitutionalObservationService(
            storage, clock=lambda: now + timedelta(hours=2)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=now,
            )
        )
    result = _query(tmp_path, now + timedelta(days=1))
    assert summary.observations_created == 0
    assert summary.observations_reused == 4
    by_id = {view.observation.observation_id: view for view in result.observations}
    for observation in legacy:
        view = by_id[observation.observation_id]
        assert view.observation == observation
        assert view.compatibility_notes == ("replayed_retrieval_time",)


@pytest.mark.parametrize(
    ("accepted", "old_accepted"),
    [
        (datetime(2025, 2, 14, 23, tzinfo=UTC), datetime(2025, 2, 14, 18, tzinfo=UTC)),
        (datetime(2025, 7, 15, 23, tzinfo=UTC), datetime(2025, 7, 15, 19, tzinfo=UTC)),
    ],
)
def test_legacy_new_york_acceptance_is_explained_for_both_dst_seasons(
    tmp_path: Path, accepted: datetime, old_accepted: datetime
) -> None:
    now = accepted + timedelta(days=2)
    report, artifact, correspondence = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2024, 12, 31).date(),
        now=now,
    )
    legacy = _legacy_observations(
        artifact,
        correspondence,
        old_accepted=old_accepted,
        old_parsed_at=now - timedelta(hours=1),
        old_normalized_at=now,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _save_legacy(storage, legacy)
        summary = InstitutionalObservationService(
            storage, clock=lambda: now + timedelta(hours=2)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=now,
            )
        )
    result = _query(tmp_path, now + timedelta(days=1))
    assert summary.observations_created == 0
    assert summary.observations_reused == 4
    by_id = {view.observation.observation_id: view for view in result.observations}
    for observation in legacy:
        view = by_id[observation.observation_id]
        assert view.observation == observation
        assert view.compatibility_notes == (
            "replayed_retrieval_time",
            "legacy_acceptance_timezone",
        )
        assert view.observation.available_at == max(
            view.artifact.available_at, view.correspondence.available_at
        )


@pytest.mark.parametrize("tamper", ["value", "available_at", "hash"])
def test_unproved_differences_remain_identity_conflicts(tmp_path: Path, tamper: str) -> None:
    now = datetime(2025, 2, 16, 12, tzinfo=UTC)
    accepted = datetime(2025, 2, 14, 23, tzinfo=UTC)
    report, artifact, correspondence = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2024, 12, 31).date(),
        now=now,
    )
    legacy = list(
        _legacy_observations(
            artifact,
            correspondence,
            old_accepted=accepted,
            old_parsed_at=now - timedelta(hours=1),
            old_normalized_at=now,
        )
    )
    if tamper == "value":
        legacy[0] = legacy[0].model_copy(update={"value": legacy[0].value + 1})
    elif tamper == "available_at":
        legacy[0] = legacy[0].model_copy(
            update={"available_at": legacy[0].available_at - timedelta(days=1)}
        )
    else:
        key = json.loads(legacy[0].source.record_key)
        key["cover_content_sha256"] = "f" * 64
        legacy[0] = legacy[0].model_copy(
            update={
                "source": legacy[0].source.model_copy(
                    update={"record_key": json.dumps(key, sort_keys=True, separators=(",", ":"))}
                )
            }
        )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _save_legacy(storage, legacy)
        with pytest.raises(StorageError, match="identity conflicts"):
            InstitutionalObservationService(
                storage, clock=lambda: now + timedelta(hours=2)
            ).normalize(
                InstitutionalObservationRequest(
                    asset_id="equity:us:aapl",
                    manager_cik="1067983",
                    report_ids=(report.report_id,),
                    known_at=now,
                )
            )


def test_arbitrary_acceptance_shift_is_rejected(tmp_path: Path) -> None:
    now = datetime(2025, 2, 16, 12, tzinfo=UTC)
    accepted = datetime(2025, 2, 14, 23, tzinfo=UTC)
    _, artifact, correspondence = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2024, 12, 31).date(),
        now=now,
    )
    row = artifact.rows[0]
    legacy = _legacy_row_observations(
        artifact,
        correspondence,
        row,
        old_accepted=datetime(2025, 2, 14, 17, tzinfo=UTC),
        old_parsed_at=now - timedelta(hours=1),
        old_normalized_at=now,
    )
    candidates = _current_row_observations(
        artifact, correspondence, row, normalized_at=now + timedelta(hours=1)
    )
    report = _report_of(tmp_path, artifact)
    with pytest.raises(
        InstitutionalObservationCompatibilityError, match="does not convert exactly"
    ):
        compare_reconstruction(
            legacy[0],
            candidates[legacy[0].field_name],
            existing_key=parse_record_key(legacy[0]),
            candidate_key=parse_record_key(candidates[legacy[0].field_name]),
            artifact=artifact,
            report=report,
            row=row,
            correspondence=correspondence,
        )


def _report_of(root: Path, artifact):
    from investment_analyst.evidence.sec_institutional_holdings.repository import (
        InstitutionalHoldingsRepository,
    )

    with LocalStorage(StoragePaths.from_root(root), read_only=True) as storage:
        report = InstitutionalHoldingsRepository(storage.raw_records).get_report(
            artifact.parent_report_id
        )
    assert report is not None
    return report


def test_ambiguous_and_nonexistent_new_york_wall_times_are_rejected() -> None:
    assert _unambiguous_new_york_utc(datetime(2025, 11, 2, 1, 30, tzinfo=UTC)) is None
    assert _unambiguous_new_york_utc(datetime(2025, 3, 9, 2, 30, tzinfo=UTC)) is None
    assert _unambiguous_new_york_utc(datetime(2025, 2, 14, 18, 0, tzinfo=UTC)) == datetime(
        2025, 2, 14, 23, 0, tzinfo=UTC
    )


def test_monetary_threshold_crossing_is_rejected(tmp_path: Path) -> None:
    accepted = datetime(2023, 1, 3, 4, tzinfo=UTC)
    now = datetime(2023, 2, 1, tzinfo=UTC)
    _, artifact, correspondence = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2022, 12, 31).date(),
        now=now,
    )
    report = _report_of(tmp_path, artifact)
    row = artifact.rows[0]
    current = _current_row_observations(
        artifact, correspondence, row, normalized_at=now + timedelta(hours=1)
    )
    candidate = current["institutional_reported_fair_value"]
    legacy = _legacy_row_observations(
        artifact,
        correspondence,
        row,
        old_accepted=datetime(2023, 1, 2, 23, tzinfo=UTC),
        old_parsed_at=now - timedelta(hours=1),
        old_normalized_at=now,
    )
    existing = next(item for item in legacy if item.field_name == candidate.field_name).model_copy(
        update={"value": candidate.value, "quality": candidate.quality}
    )
    with pytest.raises(
        InstitutionalObservationCompatibilityError,
        match="monetary policy threshold",
    ):
        compare_reconstruction(
            existing,
            candidate,
            existing_key=parse_record_key(existing),
            candidate_key=parse_record_key(candidate),
            artifact=artifact,
            report=report,
            row=row,
            correspondence=correspondence,
        )


def test_equivalent_verified_claims_reuse_the_same_economics(tmp_path: Path) -> None:
    now = datetime(2025, 2, 16, 12, tzinfo=UTC)
    accepted = datetime(2025, 2, 14, 23, tzinfo=UTC)
    report, artifact, _ = _seed(
        tmp_path,
        accepted=accepted,
        report_date=datetime(2024, 12, 31).date(),
        now=now,
    )
    row = artifact.rows[0]
    dataset_revision_id, candidate_id = uuid4(), uuid4()

    def claim(available_at: datetime):
        return SecInstitutionalRowCorrespondence.claim(
            asset_id="equity:us:aapl",
            cusip=row.cusip,
            title_of_class=row.title_of_class,
            report_period=report.report_period,
            manager_cik=report.manager_cik,
            report_id=report.report_id,
            artifact_id=artifact.artifact_id,
            row_id=row.row_id,
            universe_snapshot_id=uuid4(),
            dataset_revision_id=dataset_revision_id,
            candidate_id=candidate_id,
            available_at=available_at,
            recorded_at=now,
        )

    old_claim = claim(artifact.available_at)
    current_claim = claim(now)
    existing = next(
        item
        for item in normalize_row(artifact, row, old_claim, normalized_at=now)
        if item.field_name == "institutional_reported_fair_value"
    )
    candidate = next(
        item
        for item in normalize_row(artifact, row, current_claim, normalized_at=now)
        if item.field_name == "institutional_reported_fair_value"
    )
    compare_equivalent_reconstruction(
        existing,
        candidate,
        existing_key=parse_record_key(existing),
        candidate_key=parse_record_key(candidate),
        original_claim=old_claim,
        current_claim=current_claim,
        artifact=artifact,
        report=report,
        row=row,
    )
    with pytest.raises(InstitutionalObservationCompatibilityError, match="economic or identity"):
        compare_equivalent_reconstruction(
            existing.model_copy(update={"value": existing.value + 1}),
            candidate,
            existing_key=parse_record_key(existing),
            candidate_key=parse_record_key(candidate),
            original_claim=old_claim,
            current_claim=current_claim,
            artifact=artifact,
            report=report,
            row=row,
        )
