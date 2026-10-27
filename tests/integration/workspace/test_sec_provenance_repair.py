"""Integration coverage for the narrow SEC provenance repair."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from investment_analyst.core.models import NormalizedObservation, RawRecord
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecIssuerObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    aapl_sec_configuration,
    create_sec_aapl_asset,
    get_sec_source_definitions,
    sec_document_to_raw_record,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.sec_provenance_repair import (
    SecProvenanceConflictReason,
    SecProvenanceRepairConflictError,
    SecProvenanceRepairScopeError,
    SecProvenanceRepairService,
)
from investment_analyst.workspace.service import WorkspaceService

FIXTURE_DIR = Path("tests/fixtures/sec")
ORIGINAL_RETRIEVAL = datetime(2026, 7, 13, 18, tzinfo=UTC)
LATER_RETRIEVAL = datetime(2026, 10, 21, 20, 49, 52, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)
LATER_NORMALIZED_AT = datetime(2026, 10, 21, 20, 49, 53, tzinfo=UTC)
ANNUAL_ACCN = "0000320193-26-000001"
QUARTERLY_ACCN = "0000320193-26-000002"
EXECUTOR_SHA = "a" * 40


class FixtureTransport:
    """Offline transport for two prepared SEC documents."""

    def __init__(self, submissions: bytes, companyfacts: bytes) -> None:
        self._bodies = [submissions, companyfacts]

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        return HttpResponse(200, self._bodies.pop(0), {}, url)


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads(
        (FIXTURE_DIR / name).read_text(),
        parse_int=str,
        parse_float=str,
    )


def _prepared_documents(marker: str) -> tuple[bytes, bytes]:
    submissions = _load_fixture("aapl_submissions.json")
    recent = submissions["filings"]["recent"]
    recent["acceptanceDateTime"] = [
        "2026-01-30T21:00:00Z",
        "2026-04-30T21:00:00Z",
    ]
    recent["primaryDocument"] = [
        "aapl-20251231x10k.htm",
        "aapl-20260331x10q.htm",
    ]

    def duration(annual: bool, value: str) -> dict[str, str]:
        return {
            "start": "2025-01-01" if annual else "2026-01-01",
            "end": "2025-12-31" if annual else "2026-03-31",
            "val": value,
            "accn": ANNUAL_ACCN if annual else QUARTERLY_ACCN,
            "fy": "2025" if annual else "2026",
            "fp": "FY" if annual else "Q1",
            "form": "10-K" if annual else "10-Q",
            "filed": "2026-01-30" if annual else "2026-04-30",
        }

    def instant(annual: bool, value: str) -> dict[str, str]:
        result = duration(annual, value)
        del result["start"]
        return result

    companyfacts = _load_fixture("aapl_companyfacts.json")
    companyfacts["description"] = marker
    companyfacts["facts"] = {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [duration(True, "1000.25"), duration(False, "260.50")]}
            },
            "NetIncomeLoss": {
                "units": {"USD": [duration(True, "200.10"), duration(False, "52.25")]}
            },
            "Assets": {"units": {"USD": [instant(True, "5000"), instant(False, "5100")]}},
        },
        "dei": companyfacts["facts"]["dei"],
    }
    return (
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    )


def _records_at(
    retrieved_at: datetime,
    *,
    marker: str,
    configuration: SecAssetConfiguration,
) -> tuple[RawRecord, RawRecord]:
    submissions, companyfacts = _prepared_documents(marker)
    client = SecEdgarClient(
        FixtureTransport(submissions, companyfacts),
        SecEdgarIdentity("Investment Analyst integration@example.com"),
        cik=configuration.cik,
        ticker=configuration.ticker,
        sleep=lambda _: None,
        clock=lambda: retrieved_at,
    )
    return tuple(
        sec_document_to_raw_record(document, configuration)
        for document in client.fetch_issuer_documents().documents
    )


def _seed_source_workspace(
    service: WorkspaceService,
    root: Path,
    configuration: SecAssetConfiguration,
) -> tuple[RawRecord, RawRecord, tuple[NormalizedObservation, ...]]:
    storage = service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE)
    with storage:
        records = _records_at(ORIGINAL_RETRIEVAL, marker="original", configuration=configuration)
        storage.assets.upsert(create_sec_aapl_asset())
        for source in get_sec_source_definitions(configuration):
            storage.sources.upsert(source)
        for record in records:
            storage.raw_records.save(record)
        summary = SecIssuerObservationPipeline(
            storage,
            SecCompanyFactsNormalizer(configuration),
            configuration=configuration,
            clock=lambda: NORMALIZED_AT,
        ).run()
        assert summary.observations_created == summary.observations_generated
        identifiers = [
            UUID(str(row[0]))
            for row in storage.store.connection.execute(
                "SELECT observation_id FROM normalized_observations ORDER BY observation_id"
            ).fetchall()
        ]
        observations = tuple(storage.observations.get_many(identifiers).values())
    return records[0], records[1], observations


def _seed_destination_workspace(
    service: WorkspaceService,
    root: Path,
    configuration: SecAssetConfiguration,
    observations: tuple[NormalizedObservation, ...],
) -> tuple[RawRecord, RawRecord]:
    storage = service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE)
    with storage:
        storage.assets.upsert(create_sec_aapl_asset())
        for source in get_sec_source_definitions(configuration):
            storage.sources.upsert(source)
        records = _records_at(LATER_RETRIEVAL, marker="original", configuration=configuration)
        for record in records:
            storage.raw_records.save(record)
        storage.observations.save_many(observations)
    return records[0], records[1]


def _workspaces(tmp_path: Path, configuration: SecAssetConfiguration):
    service = WorkspaceService()
    source_root = service.initialize(tmp_path / "v1", format_version=1).paths.root
    destination_root = service.initialize(tmp_path / "v2", format_version=2).paths.root
    submissions, companyfacts, observations = _seed_source_workspace(
        service, source_root, configuration
    )
    destination_records = _seed_destination_workspace(
        service, destination_root, configuration, observations
    )
    return service, source_root, destination_root, (submissions, companyfacts), destination_records


def _service(
    service: WorkspaceService,
    source_root: Path,
    destination_root: Path,
    configuration: SecAssetConfiguration,
    *,
    write: bool,
) -> tuple[SecProvenanceRepairService, object, object]:
    destination_paths = service.resolve(destination_root)
    source_paths = service.resolve(source_root)
    destination = service.open_storage(
        destination_paths,
        WorkspaceAccessMode.READ_WRITE if write else WorkspaceAccessMode.READ_ONLY,
    )
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    repair = SecProvenanceRepairService(
        destination,
        source=source,
        configurations=(configuration,),
        destination_workspace_id=service.manifest(destination_paths.root).workspace_id,
        source_workspace_id=service.manifest(source_paths.root).workspace_id,
        executor_sha=EXECUTOR_SHA,
    )
    return repair, destination, source


def test_plan_apply_and_second_apply_reconstruct_every_dependent(tmp_path: Path) -> None:
    configuration = aapl_sec_configuration()
    service, source_root, destination_root, source_records, destination_records = _workspaces(
        tmp_path, configuration
    )
    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=False
    )
    with destination, source:
        plan = repair.build_plan()
    assert len(plan.entries) == 1
    entry = plan.entries[0]
    assert entry.raw_record_id == destination_records[1].record_id
    assert entry.before.source_retrieved_at == LATER_RETRIEVAL
    assert entry.after.source_retrieved_at == ORIGINAL_RETRIEVAL
    assert entry.before.available_at == LATER_RETRIEVAL
    assert entry.after.available_at == LATER_RETRIEVAL
    assert entry.event_time == LATER_RETRIEVAL
    assert entry.dependent_observations > 0
    assert {conflict.reason for conflict in plan.conflicts} == {
        SecProvenanceConflictReason.NO_DEPENDENTS
    }
    assert plan.digest() == plan.digest()

    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=True
    )
    with destination, source:
        receipt = repair.apply(plan)
        assert receipt.changed() == 1
        verification = repair.verify(plan)
        assert verification.verified is True
        assert verification.divergent == 0
        assert verification.reconstructed == entry.dependent_observations
        repeated = repair.apply(plan)
        assert repeated.changed() == 0
        assert repair.verify(plan).verified is True
        repaired = destination.raw_records.get(entry.raw_record_id)
        assert repaired.source.retrieved_at == ORIGINAL_RETRIEVAL
        assert repaired.available_at == LATER_RETRIEVAL
        assert repaired.received_at == LATER_RETRIEVAL
        assert repaired.event_time == LATER_RETRIEVAL
        assert repaired.payload == destination_records[1].payload
        assert repaired.source.checksum_sha256 == destination_records[1].source.checksum_sha256
        assert source.raw_records.get(entry.raw_record_id) == source_records[1]


def test_conflicting_dependent_clocks_are_never_mutated(tmp_path: Path) -> None:
    configuration = aapl_sec_configuration()
    service, source_root, destination_root, _, destination_records = _workspaces(
        tmp_path, configuration
    )
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with destination:
        companyfacts = destination.raw_records.get(destination_records[1].record_id)
        identifiers = destination.observations.list_ids_for_raw_record(companyfacts.record_id)
        imported = destination.observations.get_many(identifiers)
        native = tuple(
            stored.model_copy(
                update={
                    "observation_id": uuid5(NAMESPACE_URL, f"native-lineage-{index}"),
                    "normalized_at": LATER_NORMALIZED_AT,
                    "source": stored.source.model_copy(update={"retrieved_at": LATER_RETRIEVAL}),
                }
            )
            for index, stored in enumerate(imported.values())
        )
        destination.observations.save_many(native)
        before = destination.raw_records.get(companyfacts.record_id)
    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=False
    )
    with destination, source:
        plan = repair.build_plan()
    assert plan.entries == ()
    companyfacts_conflict = next(
        conflict
        for conflict in plan.conflicts
        if conflict.raw_record_id == destination_records[1].record_id
    )
    assert companyfacts_conflict.reason is SecProvenanceConflictReason.DEPENDENT_CLOCKS_INCOMPATIBLE
    assert companyfacts_conflict.dependent_observations == len(imported) + len(native)
    assert before.source.retrieved_at == LATER_RETRIEVAL
    assert any(
        conflict.reason is SecProvenanceConflictReason.NO_DEPENDENTS for conflict in plan.conflicts
    )


def test_apply_rejects_a_third_temporal_state(tmp_path: Path) -> None:
    configuration = aapl_sec_configuration()
    service, source_root, destination_root, _, destination_records = _workspaces(
        tmp_path, configuration
    )
    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=False
    )
    with destination, source:
        plan = repair.build_plan()
    entry = plan.entries[0]
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with destination:
        staging = destination.raw_records
        third = entry.before.model_copy(
            update={"source_retrieved_at": entry.before.source_retrieved_at - timedelta(days=1)}
        )
        third_repair = staging.plan_temporal_repair(
            entry.raw_record_id, before=entry.before, after=third
        )
        assert staging.repair_temporal_metadata(third_repair).changed is True
    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=True
    )
    with (
        destination,
        source,
        pytest.raises(SecProvenanceRepairConflictError, match="unexpected temporal state"),
    ):
        repair.apply(plan)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_ONLY
    )
    with destination:
        current = destination.raw_records.get(entry.raw_record_id)
        assert current.source.retrieved_at != entry.before.source_retrieved_at
        assert current.source.retrieved_at != entry.after.source_retrieved_at
        assert destination_records[1].record_id == entry.raw_record_id


def test_sealed_plan_refuses_a_foreign_scope(tmp_path: Path) -> None:
    configuration = aapl_sec_configuration()
    service, source_root, destination_root, _, _ = _workspaces(tmp_path, configuration)
    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=False
    )
    with destination, source:
        plan = repair.build_plan()
    empty = plan.model_copy(update={"entries": ()})
    repair, destination, source = _service(
        service, source_root, destination_root, configuration, write=True
    )
    with destination, source:
        with pytest.raises(SecProvenanceRepairScopeError, match="no admissible record"):
            repair.apply(empty)
        with pytest.raises(SecProvenanceRepairScopeError, match="executor"):
            repair.apply(plan.model_copy(update={"executor_sha": "b" * 40}))
