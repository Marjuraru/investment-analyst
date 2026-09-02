from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones import institutional_composition_service
from investment_analyst.analytics.cazatiburones.institutional_composition_service import (
    InstitutionalCompositionService,
)
from investment_analyst.storage import StorageError


class _RawRecords:
    def __init__(self, records: tuple[object, ...]) -> None:
        self._records = records
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> tuple[object, ...]:
        self.calls.append(kwargs)
        return self._records


class _Storage:
    def __init__(self, *, read_only: bool, records: tuple[object, ...]) -> None:
        self.read_only = read_only
        self.raw_records = _RawRecords(records)


def _semantic(*, rows: tuple[object, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id=uuid4(),
        accession="0000950123-25-000001",
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        available_at=datetime(2025, 2, 14, tzinfo=UTC),
        is_amendment=False,
        amendment_number=None,
        amendment_type=None,
        declared_entry_total=1,
        declared_value_total=Decimal("0.10"),
        rows=rows,
    )


def test_service_requires_read_only_storage() -> None:
    with pytest.raises(StorageError, match="read-only"):
        InstitutionalCompositionService(_Storage(read_only=False, records=())).query(
            manager_cik="0001067983", known_at=datetime(2025, 2, 14, tzinfo=UTC)
        )


def test_service_is_read_only_and_preserves_missing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    record = object()
    row = SimpleNamespace(value_as_reported=Decimal("0.10"))
    semantic = _semantic(rows=(row,))
    storage = _Storage(read_only=True, records=(record,))
    monkeypatch.setattr(
        institutional_composition_service,
        "semantics_from_raw_record",
        lambda value: semantic if value is record else None,
    )

    result = InstitutionalCompositionService(storage).query(
        manager_cik="0001067983", known_at=datetime(2025, 2, 14, tzinfo=UTC)
    )

    assert result[0].status == "original_complete"
    assert result[0].observed_value_total == Decimal("0.10")
    assert storage.raw_records.calls == [
        {
            "source_id": "sec-edgar:institutional-holdings-semantics",
            "schema_version": "sec-institutional-holdings-semantics-v2",
            "available_to": datetime(2025, 2, 14, tzinfo=UTC),
        }
    ]
