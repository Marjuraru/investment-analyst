"""Unit tests for Form 13F manager universe repository, RawRecords codecs, and blob lineage."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from investment_analyst.evidence.sec_institutional_universe.identity import (
    candidate_id,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
    SecInstitutionalUniverseRepositoryError,
    dataset_revision_from_raw_record,
    dataset_revision_to_raw_record,
    snapshot_from_raw_record,
    snapshot_to_raw_record,
)
from investment_analyst.storage.document_content import DocumentContentError
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.paths import StoragePaths


def _make_sample_revision(
    *,
    sha: str = "d" * 64,
    retrieved_at: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    period_end: date = date(2026, 5, 31),
) -> Sec13FDataSetRevision:
    return Sec13FDataSetRevision.create(
        dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
        period_start=date(2026, 3, 1),
        period_end=period_end,
        content_sha256=sha,
        size_bytes=5000,
        retrieved_at=retrieved_at,
    )


def _make_sample_snapshot(
    revision: Sec13FDataSetRevision,
    *,
    retrieved_at: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
) -> Sec13FManagerUniverseSnapshot:
    cand_id = candidate_id(
        dataset_sha256=revision.content_sha256,
        asset_id="equity:us:aapl",
        cusip="037833100",
        manager_cik="0001067983",
        accession="0001067983-26-000010",
        form="13F-HR",
        report_period=date(2026, 3, 31),
    )
    candidate = Sec13FManagerCandidate(
        candidate_id=cand_id,
        dataset_revision_id=revision.revision_id,
        asset_id="equity:us:aapl",
        cusip="037833100",
        manager_cik="0001067983",
        manager_name="BERKSHIRE HATHAWAY INC",
        accession="0001067983-26-000010",
        form="13F-HR",
        filing_date=date(2026, 5, 15),
        report_period=date(2026, 3, 31),
        value_as_filed=Decimal("150000"),
        is_amendment=False,
        is_selected=True,
        selection_rank=1,
    )
    return Sec13FManagerUniverseSnapshot.create(
        dataset_revision_id=revision.revision_id,
        dataset_sha256=revision.content_sha256,
        catalog_version=1,
        period_start=revision.period_start,
        period_end=revision.period_end,
        retrieved_at=retrieved_at,
        event_time=datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
        eligible_asset_count=1,
        matched_asset_count=1,
        candidate_manager_count=1,
        selected_manager_count=1,
        unselected_manager_count=0,
        covered_cusips=("037833100",),
        candidates=(candidate,),
    )


def test_codecs_roundtrip() -> None:
    rev = _make_sample_revision()
    rec_rev = dataset_revision_to_raw_record(rev)
    decoded_rev = dataset_revision_from_raw_record(rec_rev)
    assert decoded_rev == rev

    snap = _make_sample_snapshot(rev)
    rec_snap = snapshot_to_raw_record(snap)
    decoded_snap = snapshot_from_raw_record(rec_snap)
    assert decoded_snap == snap


def test_repository_save_and_pit_query() -> None:
    with TemporaryDirectory() as temp_dir:
        storage_paths = StoragePaths.from_root(Path(temp_dir))
        with LocalStorage(storage_paths, read_only=False) as storage:
            repo = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)

            # 1. Save blob
            blob_content = b"PK\x03\x04 fake zip content"
            blob_sha = hashlib.sha256(blob_content).hexdigest()
            receipt = repo.save_blob(blob_content)
            assert receipt.created is True
            assert receipt.sha256 == blob_sha

            # Rerun blob save is idempotent
            receipt_again = repo.save_blob(blob_content)
            assert receipt_again.created is False

            # 2. Save revision and snapshot available at T1
            t1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
            rev1 = _make_sample_revision(
                sha=blob_sha, retrieved_at=t1, period_end=date(2026, 5, 31)
            )
            snap1 = _make_sample_snapshot(rev1, retrieved_at=t1)

            repo.save_dataset_revision(rev1)
            repo.save_snapshot(snap1)

            # Idempotent re-save
            repo.save_dataset_revision(rev1)
            repo.save_snapshot(snap1)

            # 3. Query before T1: should find nothing
            before_t1 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
            found_before = repo.find_latest_snapshot(known_at=before_t1)
            assert found_before is None

            # 4. Query at or after T1: finds snap1
            found_at = repo.find_latest_snapshot(known_at=t1)
            assert found_at is not None
            assert found_at.snapshot_id == snap1.snapshot_id

            # 5. Future revision available at T2
            blob_content_2 = b"PK\x03\x04 newer content"
            blob_sha_2 = hashlib.sha256(blob_content_2).hexdigest()
            repo.save_blob(blob_content_2)

            t2 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
            rev2 = _make_sample_revision(
                sha=blob_sha_2, retrieved_at=t2, period_end=date(2026, 8, 31)
            )
            snap2 = _make_sample_snapshot(rev2, retrieved_at=t2)
            repo.save_dataset_revision(rev2)
            repo.save_snapshot(snap2)

            # Query at cut between T1 and T2: still returns snap1!
            cut_between = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
            found_mid = repo.find_latest_snapshot(known_at=cut_between)
            assert found_mid is not None
            assert found_mid.snapshot_id == snap1.snapshot_id

            # Query after T2: returns snap2
            found_after = repo.find_latest_snapshot(known_at=t2)
            assert found_after is not None
            assert found_after.snapshot_id == snap2.snapshot_id


def test_repository_lineage_verification_fails_if_blob_missing() -> None:
    with TemporaryDirectory() as temp_dir:
        storage_paths = StoragePaths.from_root(Path(temp_dir))
        with LocalStorage(storage_paths, read_only=False) as storage:
            repo = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)

            t1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
            rev = _make_sample_revision(sha="e" * 64, retrieved_at=t1)
            snap = _make_sample_snapshot(rev, retrieved_at=t1)

            # Save metadata without saving blob
            repo.save_dataset_revision(rev)
            repo.save_snapshot(snap)

            # Finding snapshot fails because blob lineage is missing!
            with pytest.raises(DocumentContentError):
                repo.find_latest_snapshot(known_at=t1)


def test_find_snapshot_for_period_reuses_only_exact_verifiable_evidence() -> None:
    with TemporaryDirectory() as temp_dir:
        storage_paths = StoragePaths.from_root(Path(temp_dir))
        with LocalStorage(storage_paths, read_only=False) as storage:
            repo = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)

            blob = (b"PK\x03\x04 exact dataset bytes" + b"\x00" * 5000)[:5000]
            sha = hashlib.sha256(blob).hexdigest()
            repo.save_blob(blob)
            t1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
            revision = _make_sample_revision(sha=sha, retrieved_at=t1)
            snapshot = _make_sample_snapshot(revision, retrieved_at=t1)
            repo.save_dataset_revision(revision)
            repo.save_snapshot(snapshot)

            resolved = repo.find_snapshot_for_period(
                period_start=date(2026, 3, 1),
                period_end=date(2026, 5, 31),
                dataset_url=revision.dataset_url,
                known_at=t1,
            )
            assert resolved is not None
            found_revision, found_snapshot = resolved
            assert found_revision.revision_id == revision.revision_id
            assert found_snapshot.snapshot_id == snapshot.snapshot_id

            # A contradictory URL for the same period is never selected.
            assert (
                repo.find_snapshot_for_period(
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 5, 31),
                    dataset_url=(
                        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
                        "01jun2024-31aug2024_form13f.zip"
                    ),
                    known_at=t1,
                )
                is None
            )

            # A different official period is never selected either.
            assert (
                repo.find_snapshot_for_period(
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 6, 30),
                    dataset_url=revision.dataset_url,
                    known_at=t1,
                )
                is None
            )

            # Evidence that is not yet available at the requested cut is not reused.
            before = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
            assert (
                repo.find_snapshot_for_period(
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 5, 31),
                    dataset_url=revision.dataset_url,
                    known_at=before,
                )
                is None
            )


def test_find_snapshot_for_period_fails_closed_when_blob_is_missing() -> None:
    with TemporaryDirectory() as temp_dir:
        storage_paths = StoragePaths.from_root(Path(temp_dir))
        with LocalStorage(storage_paths, read_only=False) as storage:
            repo = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)

            t1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
            revision = _make_sample_revision(sha="f" * 64, retrieved_at=t1)
            snapshot = _make_sample_snapshot(revision, retrieved_at=t1)
            repo.save_dataset_revision(revision)
            repo.save_snapshot(snapshot)

            with pytest.raises(DocumentContentError):
                repo.find_snapshot_for_period(
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 5, 31),
                    dataset_url=revision.dataset_url,
                    known_at=t1,
                )


def test_find_snapshot_for_period_rejects_competing_revisions_at_one_cut() -> None:
    with TemporaryDirectory() as temp_dir:
        storage_paths = StoragePaths.from_root(Path(temp_dir))
        with LocalStorage(storage_paths, read_only=False) as storage:
            repo = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)

            t1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
            first_bytes = (b"PK\x03\x04 first revision" + b"\x00" * 5000)[:5000]
            second_bytes = (b"PK\x03\x04 second revision" + b"\x00" * 5000)[:5000]
            repo.save_blob(first_bytes)
            repo.save_blob(second_bytes)
            first = _make_sample_revision(
                sha=hashlib.sha256(first_bytes).hexdigest(), retrieved_at=t1
            )
            second = _make_sample_revision(
                sha=hashlib.sha256(second_bytes).hexdigest(), retrieved_at=t1
            )
            repo.save_dataset_revision(first)
            repo.save_snapshot(_make_sample_snapshot(first, retrieved_at=t1))
            repo.save_dataset_revision(second)
            repo.save_snapshot(_make_sample_snapshot(second, retrieved_at=t1))

            with pytest.raises(
                SecInstitutionalUniverseRepositoryError,
                match="Incompatible competing revisions",
            ):
                repo.find_snapshot_for_period(
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 5, 31),
                    dataset_url=first.dataset_url,
                    known_at=t1,
                )
