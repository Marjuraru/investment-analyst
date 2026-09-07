"""Read-only integration tests for the Cazatiburones universe activity index."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from investment_analyst.application.cazatiburones_universe_activity import (
    CazatiburonesUniverseActivityApplication,
)
from investment_analyst.application.cazatiburones_universe_activity_models import (
    CazatiburonesUniverseActivityRequest,
)
from investment_analyst.application.cazatiburones_universe_activity_service import (
    CazatiburonesUniverseActivityService,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import SOURCE_ID
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService


@dataclass(frozen=True)
class _Record:
    available_at: datetime


class _FakeRawRecords:
    def list(self, **_filters: object) -> list[object]:
        return []


class _FakeObservations:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.records: tuple[_Record, ...] = ()

    def list(self, **filters: object) -> list[_Record]:
        self.calls.append(filters)
        source_id = filters.get("source_id")
        available_to = filters.get("available_to")
        if source_id != SOURCE_ID or not isinstance(available_to, datetime):
            return list(self.records)
        return [record for record in self.records if record.available_at <= available_to]


class _FakeStorage:
    def __init__(self, *, read_only: bool = True) -> None:
        self.read_only = read_only
        self.raw_records = _FakeRawRecords()
        self.observations = _FakeObservations()


def _service(tmp_path: Path, storage: _FakeStorage) -> CazatiburonesUniverseActivityService:
    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=tmp_path)
    )
    return CazatiburonesUniverseActivityService(
        storage,
        runtime.catalog,
        runtime.provider_resolver,
    )


def _request(
    *asset_ids: str, known_at: datetime | None = None
) -> CazatiburonesUniverseActivityRequest:
    return CazatiburonesUniverseActivityRequest(
        known_at=known_at or datetime(2026, 7, 16, 15, 47, tzinfo=UTC),
        asset_ids=tuple(sorted(asset_ids)),
    )


def test_service_emits_exactly_three_bounded_reads_per_eligible_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _FakeStorage()
    service = _service(tmp_path, storage)
    ownership_calls: list[tuple[str, datetime]] = []
    beneficial_calls: list[tuple[str, datetime]] = []

    def ownership_list(
        self: OwnershipRepository, *, asset_id: str, known_at: datetime
    ) -> list[_Record]:
        del self
        ownership_calls.append((asset_id, known_at))
        return [_Record(known_at - timedelta(days=1))]

    def beneficial_list(
        self: BeneficialOwnershipRepository, *, asset_id: str, known_at: datetime
    ) -> list[_Record]:
        del self
        beneficial_calls.append((asset_id, known_at))
        return [_Record(known_at - timedelta(days=2))]

    monkeypatch.setattr(OwnershipRepository, "list", ownership_list)
    monkeypatch.setattr(BeneficialOwnershipRepository, "list", beneficial_list)
    storage.observations.records = (_Record(datetime(2026, 7, 15, tzinfo=UTC)),)

    result = service.query(_request("equity:us:aapl", "equity:us:amd"))

    assert len(ownership_calls) == 2
    assert len(beneficial_calls) == 2
    assert len(storage.observations.calls) == 2
    assert all(call["source_id"] == SOURCE_ID for call in storage.observations.calls)
    assert all(item.insider.statements == 1 for item in result.assets)
    assert all(item.beneficial.statements == 1 for item in result.assets)
    assert all(item.institutional.statements == 1 for item in result.assets)


def test_service_emits_zero_reads_for_an_asset_without_sec_configuration(tmp_path: Path) -> None:
    storage = _FakeStorage()
    result = _service(tmp_path, storage).query(_request("crypto:btc-usd"))

    assert storage.observations.calls == []
    asset = result.assets[0]
    assert asset.insider.capability.value == "not_configured"
    assert asset.beneficial.capability.value == "not_configured"
    assert asset.institutional.capability.value == "not_configured"
    assert asset.insider.evidence.value == "not_queried"


def test_temp_workspace_probe_measures_three_reads_per_eligible_and_zero_per_ineligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialization = WorkspaceService().initialize(tmp_path / "workspace")
    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=tmp_path)
    )
    counts = {"insider": 0, "beneficial": 0, "institutional": 0}

    def ownership_list(
        self: OwnershipRepository, *, asset_id: str, known_at: datetime
    ) -> list[object]:
        del self, asset_id, known_at
        counts["insider"] += 1
        return []

    def beneficial_list(
        self: BeneficialOwnershipRepository, *, asset_id: str, known_at: datetime
    ) -> list[object]:
        del self, asset_id, known_at
        counts["beneficial"] += 1
        return []

    monkeypatch.setattr(OwnershipRepository, "list", ownership_list)
    monkeypatch.setattr(BeneficialOwnershipRepository, "list", beneficial_list)

    with LocalStorage(
        StoragePaths.from_root(initialization.paths.storage_root), read_only=True
    ) as storage:
        original_list = storage.observations.list

        def observation_list(**filters: object) -> list[object]:
            counts["institutional"] += 1
            return original_list(**filters)

        storage.observations.list = observation_list  # type: ignore[method-assign]
        result = CazatiburonesUniverseActivityService(
            storage,
            runtime.catalog,
            runtime.provider_resolver,
        ).query(_request("crypto:btc-usd", "equity:us:aapl"))

    assert counts == {"insider": 1, "beneficial": 1, "institutional": 1}
    assert result.assets[0].asset_id == "crypto:btc-usd"
    assert result.assets[0].insider.evidence.value == "not_queried"
    assert result.assets[1].asset_id == "equity:us:aapl"
    assert result.assets[1].insider.evidence.value == "missing"


def test_institutional_count_ignores_other_sources_under_the_same_asset_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _FakeStorage()
    service = _service(tmp_path, storage)
    monkeypatch.setattr(OwnershipRepository, "list", lambda self, **_: [])
    monkeypatch.setattr(BeneficialOwnershipRepository, "list", lambda self, **_: [])
    valid = _Record(datetime(2026, 7, 15, tzinfo=UTC))
    other_source = _Record(datetime(2026, 7, 15, 1, tzinfo=UTC))

    def list_observations(**filters: object) -> list[_Record]:
        storage.observations.calls.append(filters)
        return [valid] if filters.get("source_id") == SOURCE_ID else [valid, other_source]

    storage.observations.list = list_observations  # type: ignore[method-assign]
    result = service.query(_request("equity:us:aapl"))

    assert result.assets[0].institutional.statements == 1
    assert storage.observations.calls[0]["source_id"] == SOURCE_ID


def test_evidence_after_known_at_is_never_counted_and_counts_are_monotonic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _FakeStorage()
    service = _service(tmp_path, storage)
    past = _Record(datetime(2026, 7, 15, tzinfo=UTC))
    future = _Record(datetime(2026, 7, 17, tzinfo=UTC))

    def records_until(self: object, *, known_at: datetime, **_kwargs: object) -> list[_Record]:
        del self
        return [record for record in (past, future) if record.available_at <= known_at]

    monkeypatch.setattr(OwnershipRepository, "list", records_until)
    monkeypatch.setattr(BeneficialOwnershipRepository, "list", records_until)
    storage.observations.records = (past, future)

    early = service.query(
        _request("equity:us:aapl", known_at=datetime(2026, 7, 16, tzinfo=UTC))
    ).assets[0]
    late = service.query(
        _request("equity:us:aapl", known_at=datetime(2026, 7, 18, tzinfo=UTC))
    ).assets[0]

    assert early.insider.statements == 1
    assert late.insider.statements == 2
    assert early.institutional.statements == 1
    assert late.institutional.statements == 2
    assert late.institutional.latest_age_days == 1


def test_absence_grammar_keeps_not_configured_not_queried_and_missing_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _FakeStorage()
    service = _service(tmp_path, storage)
    monkeypatch.setattr(OwnershipRepository, "list", lambda self, **_: [])
    monkeypatch.setattr(BeneficialOwnershipRepository, "list", lambda self, **_: [])

    result = service.query(_request("crypto:btc-usd", "equity:us:aapl"))
    crypto, aapl = result.assets

    assert crypto.insider.capability.value == "not_configured"
    assert crypto.insider.evidence.value == "not_queried"
    assert aapl.insider.capability.value == "supported"
    assert aapl.insider.evidence.value == "missing"
    assert aapl.insider.statements == 0


def test_a_malformed_record_degrades_one_family_to_not_queried_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _FakeStorage()
    service = _service(tmp_path, storage)

    def malformed(self: OwnershipRepository, **_kwargs: object) -> list[_Record]:
        del self
        raise StorageError("malformed persisted record with internal detail")

    monkeypatch.setattr(OwnershipRepository, "list", malformed)
    monkeypatch.setattr(
        BeneficialOwnershipRepository,
        "list",
        lambda self, **_: [_Record(datetime(2026, 7, 15, tzinfo=UTC))],
    )
    storage.observations.records = (_Record(datetime(2026, 7, 14, tzinfo=UTC)),)

    asset = service.query(_request("equity:us:aapl")).assets[0]

    assert asset.insider.evidence.value == "not_queried"
    assert asset.insider.not_evaluable_reason == "malformed_persisted_record"
    assert asset.beneficial.evidence.value == "present"
    assert asset.institutional.evidence.value == "present"


def test_index_never_resolves_report_artifact_or_correspondence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _FakeStorage()
    service = _service(tmp_path, storage)
    record = _Record(datetime(2026, 7, 15, tzinfo=UTC))
    monkeypatch.setattr(OwnershipRepository, "list", lambda self, **_: [record])
    monkeypatch.setattr(BeneficialOwnershipRepository, "list", lambda self, **_: [record])
    storage.observations.records = (record,)

    payload = service.query(_request("equity:us:aapl")).model_dump(mode="json")
    serialized = str(payload)

    assert "report_id" not in serialized
    assert "artifact" not in serialized
    assert "correspondence" not in serialized
    assert "total" not in serialized


class _RuntimeProbe:
    def __init__(self, base: ApplicationRuntime, storage: _FakeStorage) -> None:
        self.catalog: AssetCatalogService = base.catalog
        self.provider_resolver: ProviderAssetContextResolver = base.provider_resolver
        self.storage = storage
        self.calls: list[tuple[StorageLocationRequest, WorkspaceAccessMode]] = []

    @contextmanager
    def open_storage(
        self,
        location: StorageLocationRequest,
        *,
        access_mode: WorkspaceAccessMode,
    ) -> Iterator[_FakeStorage]:
        self.calls.append((location, access_mode))
        yield self.storage


def test_application_opens_exactly_one_read_only_workspace(tmp_path: Path) -> None:
    base = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=tmp_path)
    )
    probe = _RuntimeProbe(base, _FakeStorage())
    location = StorageLocationRequest(workspace=tmp_path / "workspace")

    CazatiburonesUniverseActivityApplication(probe).query(location, _request("crypto:btc-usd"))

    assert len(probe.calls) == 1
    assert probe.calls[0] == (location, WorkspaceAccessMode.READ_ONLY)


def test_query_fails_closed_when_storage_is_not_read_only(tmp_path: Path) -> None:
    storage = _FakeStorage(read_only=False)

    with pytest.raises(StorageError, match="read-only"):
        _service(tmp_path, storage).query(_request("crypto:btc-usd"))
