from datetime import UTC, date, datetime

import pytest

from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
    InstrumentCorrespondenceRepositoryError,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def _declared(*, recorded_at: datetime = datetime(2025, 2, 15, tzinfo=UTC)):
    return InstrumentCorrespondence.declare(
        asset_id="equity:us:aapl",
        cusip="037833100",
        title_of_class="COM",
        effective_from=date(2020, 1, 1),
        effective_to=None,
        available_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
        recorded_at=recorded_at,
    )


def test_save_is_append_only_and_preserves_first_provenance(tmp_path) -> None:
    item = _declared()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstrumentCorrespondenceRepository(storage.raw_records)
        assert repository.save(item, catalog_version=1, declared_by="analyst-a") == item
        original = storage.raw_records.get(item.raw_record_id)

        assert repository.save(item, catalog_version=2, declared_by="analyst-b") == item
        assert storage.raw_records.get(item.raw_record_id) == original


def test_save_rejects_semantic_conflict_with_same_identity(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstrumentCorrespondenceRepository(storage.raw_records)
        repository.save(_declared(), catalog_version=1, declared_by="analyst-a")

        with pytest.raises(InstrumentCorrespondenceRepositoryError, match="identity conflicts"):
            repository.save(
                _declared(recorded_at=datetime(2025, 2, 16, tzinfo=UTC)),
                catalog_version=1,
                declared_by="analyst-a",
            )
