from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from investment_analyst.evidence.sec_institutional_semantics import artifact_reader
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)


class _RawRecords:
    def __init__(self, record_id: UUID) -> None:
        self._record_id = record_id
        self.list_calls: list[datetime] = []
        self.get_calls: list[UUID] = []

    def list_record_ids(
        self,
        *,
        source_id: str,
        schema_version: str,
        available_to: datetime,
    ) -> list[UUID]:
        assert source_id == "sec-edgar:institutional-holdings-semantics"
        assert schema_version == "sec-institutional-holdings-semantics-v2"
        self.list_calls.append(available_to)
        return [self._record_id]

    def get(self, record_id: UUID) -> object:
        self.get_calls.append(record_id)
        return object()


def test_reader_memoizes_validated_artifacts_per_repository_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_id = uuid4()
    raw_records = _RawRecords(record_id)
    artifact = SimpleNamespace(raw_record_id=record_id)
    parsed: list[object] = []
    monkeypatch.setattr(
        artifact_reader,
        "semantics_from_raw_record",
        lambda record: parsed.append(record) or artifact,
    )

    first = InstitutionalSemanticsArtifactReader(raw_records)  # type: ignore[arg-type]
    second = InstitutionalSemanticsArtifactReader(raw_records)  # type: ignore[arg-type]
    known_at = datetime(2025, 2, 14, tzinfo=UTC)

    assert first.list_visible(known_at=known_at) == (artifact,)
    assert second.list_visible(known_at=known_at) == (artifact,)
    assert raw_records.list_calls == [known_at, known_at]
    assert raw_records.get_calls == [record_id]
    assert len(parsed) == 1


def test_reader_keeps_caches_independent_between_storage_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_id = uuid4()
    first_records = _RawRecords(record_id)
    second_records = _RawRecords(record_id)
    monkeypatch.setattr(
        artifact_reader,
        "semantics_from_raw_record",
        lambda record: SimpleNamespace(raw_record_id=record_id, record=record),
    )
    known_at = datetime(2025, 2, 14, tzinfo=UTC)

    first = InstitutionalSemanticsArtifactReader(first_records)  # type: ignore[arg-type]
    second = InstitutionalSemanticsArtifactReader(second_records)  # type: ignore[arg-type]

    assert (
        first.list_visible(known_at=known_at)[0].record
        is not second.list_visible(known_at=known_at)[0].record
    )
    assert first_records.get_calls == [record_id]
    assert second_records.get_calls == [record_id]
