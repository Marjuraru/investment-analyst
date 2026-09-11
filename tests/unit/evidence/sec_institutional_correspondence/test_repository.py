"""Unit tests for the append-only row-correspondence repository and its point-in-time reads."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
    ROW_CORRESPONDENCE_SOURCE_ID,
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
    SecInstitutionalRowCorrespondenceRepositoryError,
    row_correspondence_from_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_REPORT_PERIOD = date(2026, 3, 31)
_AVAILABLE_AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_RECORDED_AT = datetime(2026, 7, 3, 9, 30, tzinfo=UTC)


def _claim(
    *,
    artifact_id: UUID,
    row_id: UUID,
    asset_id: str = "equity:us:aapl",
    cusip: str = "037833100",
    title_of_class: str = "COM",
    available_at: datetime = _AVAILABLE_AT,
    recorded_at: datetime = _RECORDED_AT,
) -> SecInstitutionalRowCorrespondence:
    return SecInstitutionalRowCorrespondence.claim(
        asset_id=asset_id,
        cusip=cusip,
        title_of_class=title_of_class,
        report_period=_REPORT_PERIOD,
        manager_cik="0002012383",
        report_id=uuid4(),
        artifact_id=artifact_id,
        row_id=row_id,
        universe_snapshot_id=uuid4(),
        dataset_revision_id=uuid4(),
        candidate_id=uuid4(),
        available_at=available_at,
        recorded_at=recorded_at,
    )


def test_repository_roundtrip_and_idempotent_save(tmp_path: Path) -> None:
    artifact_id, row_id = uuid4(), uuid4()
    claim = _claim(artifact_id=artifact_id, row_id=row_id)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        assert repository.get(claim.correspondence_id) is None
        assert repository.save(claim) == claim
        assert repository.save(claim) == claim
        assert repository.get(claim.correspondence_id) == claim

        record = storage.raw_records.get(
            SecInstitutionalRowCorrespondence.expected_raw_record_id(claim.correspondence_id)
        )
        assert record.source.source_id == ROW_CORRESPONDENCE_SOURCE_ID
        assert record.schema_version == ROW_CORRESPONDENCE_SCHEMA_VERSION
        assert record.asset_id == claim.asset_id
        assert record.event_time == datetime(2026, 3, 31, tzinfo=UTC)
        assert record.available_at == claim.available_at
        assert record.received_at == claim.recorded_at
        assert record.source.retrieved_at == claim.recorded_at
        assert record.source.raw_uri == f"sec-universe-snapshot:{claim.universe_snapshot_id}"
        assert row_correspondence_from_raw_record(record) == claim
        assert storage.raw_records.count(schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION) == 1


def test_repository_conflict_and_tamper_fail_closed(tmp_path: Path) -> None:
    artifact_id, row_id = uuid4(), uuid4()
    claim = _claim(artifact_id=artifact_id, row_id=row_id)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        repository.save(claim)
        record = storage.raw_records.get(
            SecInstitutionalRowCorrespondence.expected_raw_record_id(claim.correspondence_id)
        )
        for update in (
            {"asset_id": "equity:us:msft"},
            {"event_time": datetime(2026, 4, 1, tzinfo=UTC)},
            {"available_at": datetime(2026, 7, 2, tzinfo=UTC)},
            {"received_at": datetime(2026, 7, 4, tzinfo=UTC)},
            {"payload": {"kind": "other", "correspondence": {}}},
            {"schema_version": "sec-institutional-row-correspondence-v2"},
        ):
            with pytest.raises(SecInstitutionalRowCorrespondenceRepositoryError):
                row_correspondence_from_raw_record(record.model_copy(update=update))
        with pytest.raises(SecInstitutionalRowCorrespondenceRepositoryError):
            row_correspondence_from_raw_record(
                record.model_copy(
                    update={
                        "source": record.source.model_copy(
                            update={"source_id": "sec-edgar:unknown"}
                        )
                    }
                )
            )
        with pytest.raises(SecInstitutionalRowCorrespondenceRepositoryError):
            row_correspondence_from_raw_record(
                record.model_copy(
                    update={
                        "source": record.source.model_copy(
                            update={"raw_uri": "catalog:default_assets.v1.json"}
                        )
                    }
                )
            )

        replayed = claim.model_copy(update={"recorded_at": claim.recorded_at.replace(day=5)})
        assert repository.save(replayed) == claim
        assert repository.get(claim.correspondence_id) == claim

        conflicting = claim.model_copy(update={"title_of_class": "COM CL A"})
        assert conflicting.correspondence_id == claim.correspondence_id
        with pytest.raises(SecInstitutionalRowCorrespondenceRepositoryError):
            repository.save(conflicting)


def test_repository_list_applies_point_in_time_and_filters(tmp_path: Path) -> None:
    first_artifact, first_row = uuid4(), uuid4()
    second_artifact, second_row = uuid4(), uuid4()
    early = _claim(artifact_id=first_artifact, row_id=first_row)
    late = _claim(
        artifact_id=first_artifact,
        row_id=first_row,
        available_at=_AVAILABLE_AT.replace(hour=18),
    )
    other_row = _claim(artifact_id=second_artifact, row_id=second_row, cusip="594918104")
    other_asset = _claim(
        artifact_id=first_artifact,
        row_id=first_row,
        asset_id="equity:us:msft",
        cusip="594918104",
    )

    def expected(items) -> list[UUID]:
        return [item.correspondence_id for item in sorted(items, key=_order)]

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        for claim in (early, late, other_row, other_asset):
            repository.save(claim)

        assert repository.list(known_at=_AVAILABLE_AT.replace(hour=11)) == []
        visible = repository.list(known_at=_AVAILABLE_AT.replace(hour=12))
        assert [item.correspondence_id for item in visible] == expected(
            (early, other_row, other_asset)
        )
        assert len(repository.list(known_at=_RECORDED_AT)) == 4
        assert [
            item.correspondence_id
            for item in repository.list(known_at=_RECORDED_AT, asset_id="equity:us:aapl")
        ] == expected((early, late, other_row))
        assert [
            item.correspondence_id
            for item in repository.list(
                known_at=_RECORDED_AT, artifact_id=first_artifact, row_id=first_row
            )
        ] == expected((early, late, other_asset))
        assert repository.list(known_at=_RECORDED_AT, row_id=uuid4()) == []
        assert storage.raw_records.count(schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION) == 4


def _order(item: SecInstitutionalRowCorrespondence) -> tuple[datetime, str]:
    return (item.available_at, str(item.correspondence_id))
