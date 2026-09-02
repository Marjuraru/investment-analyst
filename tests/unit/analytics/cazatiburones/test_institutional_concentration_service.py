from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones import institutional_concentration_service
from investment_analyst.analytics.cazatiburones.institutional_concentration_service import (
    InstitutionalConcentrationService,
)
from investment_analyst.core.models.enums import DataQuality
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


def _semantic(
    *,
    report_period: date | None = date(2024, 12, 31),
    available_at: datetime = datetime(2025, 2, 14, tzinfo=UTC),
    is_amendment: bool = False,
    amendment_number: str | None = None,
    amendment_type: str | None = None,
) -> SimpleNamespace:
    artifact_id = uuid4()
    return SimpleNamespace(
        artifact_id=artifact_id,
        accession="0000950123-25-000001",
        manager_cik="0001067983",
        report_period=report_period,
        available_at=available_at,
        is_amendment=is_amendment,
        amendment_number=amendment_number,
        amendment_type=amendment_type,
        declared_entry_total=1,
        declared_value_total=Decimal("100"),
        rows=(
            SimpleNamespace(
                cusip="037833100",
                title_of_class="COM",
                put_call=None,
                value_as_reported=Decimal("100"),
            ),
        ),
        cover_revision=SimpleNamespace(
            document=SimpleNamespace(filing=SimpleNamespace(accepted_at=available_at))
        ),
    )


def test_service_requires_read_only_storage() -> None:
    with pytest.raises(StorageError, match="read-only"):
        InstitutionalConcentrationService(_Storage(read_only=False, records=())).query(
            manager_cik="0001067983", known_at=datetime(2025, 2, 14, tzinfo=UTC)
        )


def test_service_uses_one_visible_artifact_and_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = object()
    semantic = _semantic()
    storage = _Storage(read_only=True, records=(record,))
    monkeypatch.setattr(
        institutional_concentration_service,
        "semantics_from_raw_record",
        lambda value: semantic if value is record else None,
    )
    monkeypatch.setattr(
        institutional_concentration_service,
        "effective_close_total",
        lambda item: (Decimal("100"), DataQuality.VALID),
    )

    result = InstitutionalConcentrationService(storage).query(
        manager_cik="1067983", known_at=datetime(2025, 2, 14, tzinfo=UTC)
    )

    assert len(result) == 1
    assert result[0].status == "calculated"
    assert result[0].effective_artifact_id == semantic.artifact_id
    assert storage.raw_records.calls == [
        {
            "source_id": "sec-edgar:institutional-holdings-semantics",
            "schema_version": "sec-institutional-holdings-semantics-v2",
            "available_to": datetime(2025, 2, 14, tzinfo=UTC),
        }
    ]


def test_service_preserves_an_unresolved_period_as_a_typed_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = object()
    semantic = _semantic(report_period=None)
    monkeypatch.setattr(
        institutional_concentration_service,
        "semantics_from_raw_record",
        lambda value: semantic if value is record else None,
    )

    result = InstitutionalConcentrationService(_Storage(read_only=True, records=(record,))).query(
        manager_cik="1067983", known_at=datetime(2025, 2, 14, tzinfo=UTC)
    )

    assert [(item.report_period, item.reason, item.position_count) for item in result] == [
        (None, "unresolved_close", None)
    ]


def test_service_uses_only_the_selected_effective_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_record = object()
    amended_record = object()
    original = _semantic()
    amendment = _semantic(
        available_at=datetime(2025, 2, 15, tzinfo=UTC),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
    )
    monkeypatch.setattr(
        institutional_concentration_service,
        "semantics_from_raw_record",
        lambda value: original if value is original_record else amendment,
    )
    monkeypatch.setattr(
        institutional_concentration_service,
        "effective_close_total",
        lambda item: (Decimal("100"), DataQuality.VALID),
    )

    result = InstitutionalConcentrationService(
        _Storage(read_only=True, records=(original_record, amended_record))
    ).query(manager_cik="1067983", known_at=datetime(2025, 2, 15, tzinfo=UTC))

    assert [(item.effective_artifact_id, item.close_status) for item in result] == [
        (amendment.artifact_id, "amended")
    ]
