"""Unit coverage for selective historical recovery selection and import."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.workspace.historical_recovery import (
    HistoricalRecoveryError,
    HistoricalRecoveryImporter,
    HistoricalRecoveryPause,
    HistoricalRecoverySelectionBuilder,
    load_historical_recovery_manifest,
    verify_historical_recovery_selection,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_KNOWN_AT = datetime(2026, 10, 22, 12, tzinfo=UTC)
_ASSET = "equity:us:aapl"
_MARKET_SOURCE = "fixture:market"
_FUNDAMENTAL_SOURCE = "sec-edgar:aapl:companyfacts"
_DOCUMENT_SOURCE = "sec-edgar:primary-documents"
_DOCUMENT_BYTES = b"fixture primary document bytes"


def _source(source_id: str, key: str, checksum: str | None = None) -> SourceReference:
    return SourceReference(
        source_id=source_id,
        record_key=key,
        checksum_sha256=checksum,
        retrieved_at=_KNOWN_AT,
    )


def _raw(
    *,
    source_id: str,
    key: str,
    schema_version: str,
    available_at: datetime,
    payload: dict[str, object] | None = None,
    checksum: str | None = None,
    identity: str,
) -> RawRecord:
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"recovery-raw-{identity}"),
        asset_id=_ASSET,
        source=_source(source_id, key, checksum),
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload=payload or {"identity": identity},
        schema_version=schema_version,
    )


def _observation(
    raw: RawRecord,
    *,
    field_name: str,
    value: Decimal,
    frequency: DataFrequency,
    observed_at: datetime | None,
    period_end: datetime | None,
    available_at: datetime,
    identity: str,
    unit: str = "USD",
) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"recovery-observation-{identity}"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name=field_name,
        value=value,
        unit=unit,
        frequency=frequency,
        observed_at=observed_at,
        period_end=period_end,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="recovery-fixture-v1",
    )


def _metric(observation: NormalizedObservation, *, identity: str) -> MetricResult:
    return MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"recovery-metric-{identity}"),
        asset_id=_ASSET,
        metric_key="market.technical.sma",
        value=Decimal("101.2500"),
        unit="USD",
        as_of=observation.available_at,
        available_at=observation.available_at,
        computed_at=observation.available_at + timedelta(minutes=2),
        parameters={"source_id": _MARKET_SOURCE, "window": 3},
        input_observation_ids=[observation.observation_id],
        algorithm_version="recovery-fixture-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic(metric: MetricResult, *, identity: str) -> DiagnosticResult:
    return DiagnosticResult(
        diagnostic_id=uuid5(NAMESPACE_URL, f"recovery-diagnostic-{identity}"),
        asset_id=_ASSET,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("60.00"),
        confidence=Decimal("0.75"),
        as_of=metric.as_of,
        available_at=metric.available_at,
        computed_at=metric.computed_at + timedelta(minutes=1),
        components=[
            DiagnosticComponent(
                component_key="trend",
                score=Decimal("60.00"),
                weight=Decimal("1.00"),
                weighted_contribution=Decimal("60.00"),
                metric_result_ids=[metric.result_id],
                explanation="Recovery fixture component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.75"),
                reason="Recovery fixture evidence.",
            )
        ],
        algorithm_version="recovery-fixture-v1",
        summary="Recovery fixture diagnostic.",
        quality=DataQuality.VALID,
    )


def _seed_source(root: Path) -> tuple[Path, str]:
    service = WorkspaceService(environ={}, home=root / "home")
    initialized = service.initialize(root / "v1", format_version=1)
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        document_receipt = storage.documents.put(_DOCUMENT_BYTES)
        market_raw = _raw(
            source_id=_MARKET_SOURCE,
            key="AAPL:2026-10-20",
            schema_version="fixture-market-v1",
            available_at=datetime(2026, 10, 20, tzinfo=UTC),
            identity="market",
        )
        market_observation = _observation(
            market_raw,
            field_name="close",
            value=Decimal("101.25"),
            frequency=DataFrequency.DAY_1,
            observed_at=datetime(2026, 10, 20, tzinfo=UTC),
            period_end=None,
            available_at=datetime(2026, 10, 20, 20, tzinfo=UTC),
            identity="market",
        )
        fundamental_raw = _raw(
            source_id=_FUNDAMENTAL_SOURCE,
            key="AAPL:FY2025",
            schema_version="fixture-companyfacts-v1",
            available_at=datetime(2025, 10, 30, tzinfo=UTC),
            identity="fundamental",
        )
        fundamental_observation = _observation(
            fundamental_raw,
            field_name="Revenues",
            value=Decimal("400000000000"),
            frequency=DataFrequency.ANNUAL,
            observed_at=None,
            period_end=datetime(2025, 9, 27, tzinfo=UTC),
            available_at=datetime(2025, 10, 30, tzinfo=UTC),
            identity="fundamental",
        )
        document_raw = _raw(
            source_id=_DOCUMENT_SOURCE,
            key="AAPL:document:1",
            schema_version="fixture-document-v1",
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload={"content_sha256": document_receipt.sha256},
            checksum=document_receipt.sha256,
            identity="document",
        )
        for raw, observation in (
            (market_raw, market_observation),
            (fundamental_raw, fundamental_observation),
            (document_raw, None),
        ):
            storage.raw_records.save(raw)
            if observation is not None:
                storage.observations.save(observation)
        metric = _metric(market_observation, identity="market")
        diagnostic = _diagnostic(metric, identity="market")
        storage.metric_results.save(metric)
        storage.diagnostics.save(diagnostic)
    return initialized.paths.root, document_receipt.sha256


def _seed_destination(root: Path) -> Path:
    service = WorkspaceService(environ={}, home=root / "home")
    return service.initialize(root / "v2", format_version=2).paths.root


def _builder(
    *,
    service: WorkspaceService,
    source_root: Path,
    destination_root: Path,
) -> HistoricalRecoverySelectionBuilder:
    source_paths = service.resolve(source_root)
    destination_paths = service.resolve(destination_root)
    return HistoricalRecoverySelectionBuilder(
        service=service,
        source_paths=source_paths,
        destination_paths=destination_paths,
        source_inspection=service.inspect(source_paths.root),
        destination_inspection=service.inspect(destination_paths.root),
    )


def _build(
    root: Path,
    *,
    selection_name: str = "selection",
) -> tuple[Path, Path, Path, HistoricalRecoverySelectionBuilder]:
    source_root, document_sha = _seed_source(root)
    destination_root = _seed_destination(root)
    service = WorkspaceService(environ={}, home=root / "home")
    builder = _builder(
        service=service,
        source_root=source_root,
        destination_root=destination_root,
    )
    output = root / selection_name
    builder.build(known_at=_KNOWN_AT, output_root=output)
    return source_root, destination_root, output, builder


def test_selection_is_read_only_paginated_and_bound(tmp_path: Path) -> None:
    source_root, destination_root, output, builder = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    assert manifest.source_workspace_id == builder._source_inspection.workspace_id
    assert manifest.destination_workspace_id == builder._destination_inspection.workspace_id
    assert manifest.totals.raw_records == 3
    assert manifest.totals.observations == 2
    assert manifest.totals.metric_results == 1
    assert manifest.totals.diagnostic_results == 1
    assert manifest.totals.document_contents == 1
    assert manifest.totals.import_required == manifest.totals.items
    assert manifest.pages
    verify_historical_recovery_selection(manifest, output)
    matrix = json.loads((output / "matrix.json").read_text(encoding="utf-8"))
    assert matrix["assets"][_ASSET]["families"]["market"]["selected"] == 4
    assert matrix["assets"][_ASSET]["families"]["fundamentals"]["selected"] == 2
    budget = json.loads((output / "budget.json").read_text(encoding="utf-8"))
    assert budget["schema_version"] == "historical-recovery-budget-v1"
    source_inspection = WorkspaceService(environ={}, home=tmp_path / "home").inspect(source_root)
    assert source_inspection.raw_record_count == 3


def test_import_is_idempotent_replay_reports_no_new_rows(tmp_path: Path) -> None:
    source_root, destination_root, output, builder = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    checkpoint = tmp_path / "checkpoint.json"
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination:
        summary = HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()
    assert summary.created_raw == 3
    assert summary.created_observations == 2
    assert summary.created_metrics == 1
    assert summary.created_diagnostics == 1
    assert summary.created_documents == 1
    inspection = service.inspect(destination_root)
    assert inspection.raw_record_count == 3
    assert inspection.observation_count == 2
    assert inspection.metric_result_count == 1
    assert inspection.diagnostic_result_count == 1
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination:
        replay = HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()
    assert replay.replay is True
    assert replay.created_raw == 0
    assert replay.created_observations == 0
    assert replay.created_metrics == 0
    assert replay.created_diagnostics == 0
    assert replay.created_documents == 0
    assert replay.reused_raw == 3


def test_crash_after_page_persist_resumes_without_duplicates(tmp_path: Path) -> None:
    source_root, destination_root, output, _ = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    checkpoint = tmp_path / "checkpoint.json"

    def _fail(point: str) -> None:
        raise RuntimeError(f"injected failure at {point}")

    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination, pytest.raises(RuntimeError, match="injected failure"):
        HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
            failure_injector=_fail,
        ).run()
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["phase"] == "IMPORTING"
    assert (destination_root / "data" / "documents").is_dir()
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination:
        summary = HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()
    assert summary.created_raw == 3
    inspection = service.inspect(destination_root)
    assert inspection.raw_record_count == 3
    assert inspection.observation_count == 2


def test_foreign_checkpoint_and_out_of_range_cursor_fail_closed(tmp_path: Path) -> None:
    source_root, destination_root, output, _ = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": "historical-recovery-progress-v1",
                "selection_id": str(uuid4()),
                "selection_digest": manifest.digest,
                "source_workspace_id": str(manifest.source_workspace_id),
                "destination_workspace_id": str(manifest.destination_workspace_id),
                "phase": "IMPORTING",
                "current_kind": None,
                "page_index": 0,
                "completed_kinds": [],
                "updated_at": _KNOWN_AT.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination, pytest.raises(HistoricalRecoveryError, match="another selection"):
        HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": "historical-recovery-progress-v1",
                "selection_id": str(manifest.selection_id),
                "selection_digest": manifest.digest,
                "source_workspace_id": str(manifest.source_workspace_id),
                "destination_workspace_id": str(manifest.destination_workspace_id),
                "phase": "IMPORTING",
                "current_kind": "raw-record",
                "page_index": 999,
                "completed_kinds": [],
                "updated_at": _KNOWN_AT.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination, pytest.raises(HistoricalRecoveryError, match="cursor"):
        HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()


def test_source_content_drift_and_page_digest_fail_closed(tmp_path: Path) -> None:
    source_root, destination_root, output, _ = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    page = manifest.pages[0]
    page_path = output / page.relative_path
    original = page_path.read_bytes()
    page_path.write_bytes(original[:-1] + b"X")
    with pytest.raises(HistoricalRecoveryError, match="page digest"):
        verify_historical_recovery_selection(manifest, output)
    page_path.write_bytes(original)
    verify_historical_recovery_selection(manifest, output)


def test_conflicting_destination_identity_fails_selection(tmp_path: Path) -> None:
    source_root, document_sha = _seed_source(tmp_path)
    destination_root = _seed_destination(tmp_path)
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    raw = _raw(
        source_id=_MARKET_SOURCE,
        key="AAPL:2026-10-20",
        schema_version="fixture-market-v1",
        available_at=datetime(2026, 10, 20, tzinfo=UTC),
        identity="market",
    )
    unrelated = RawRecord(
        record_id=raw.record_id,
        asset_id=_ASSET,
        source=_source(_MARKET_SOURCE, "other-record-key", "0" * 64),
        event_time=raw.event_time,
        available_at=raw.available_at,
        received_at=raw.received_at,
        payload={"identity": "conflict"},
        schema_version=raw.schema_version,
    )
    with service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        storage.raw_records.save(unrelated)
    builder = _builder(
        service=service,
        source_root=source_root,
        destination_root=destination_root,
    )
    with pytest.raises(HistoricalRecoveryError, match="conflict"):
        builder.build(known_at=_KNOWN_AT, output_root=tmp_path / "conflicting")


def test_budget_rejects_insufficient_host_space(tmp_path: Path) -> None:
    from investment_analyst.workspace.historical_recovery_budget import (
        HistoricalRecoveryBudgetError,
        measure_historical_recovery_budget,
        require_historical_recovery_admission,
    )

    budget = measure_historical_recovery_budget(
        selected_payload_bytes=1_000,
        present_payload_bytes=0,
        import_peak_bytes=1_000,
        backup_new_bytes=1_000,
        backup_reused_bytes=0,
        restore_peak_bytes=1_000,
        host_free_bytes=lambda: 1,
        wsl_free_bytes=lambda: 1_000_000,
    )
    assert budget.admitted is False
    with pytest.raises(HistoricalRecoveryBudgetError, match="not admitted"):
        require_historical_recovery_admission(budget)


def test_page_budget_pauses_without_losing_the_checkpoint(tmp_path: Path) -> None:
    source_root, destination_root, output, _ = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    checkpoint = tmp_path / "checkpoint.json"
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination, pytest.raises(HistoricalRecoveryPause):
        HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run(max_pages=1)
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["phase"] == "IMPORTING"
    assert persisted["current_kind"] == "document-content"
    assert persisted["page_index"] == 1
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination:
        summary = HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()
    assert summary.replay is False
    assert summary.created_raw == 3
    assert summary.created_documents == 1
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["phase"] == "COMPLETE"


def test_stable_record_key_ignores_only_volatile_submissions_snapshot() -> None:
    from investment_analyst.workspace.historical_recovery import _stable_record_key

    first = (
        '{"accession_number":"0000320193-24-000081","companyfacts_record_id":"fact",'
        '"submissions_record_id":"snapshot-a","tag":"Revenues"}'
    )
    second = (
        '{"accession_number":"0000320193-24-000081","companyfacts_record_id":"fact",'
        '"submissions_record_id":"snapshot-b","tag":"Revenues"}'
    )
    changed = (
        '{"accession_number":"0000320193-24-000081","companyfacts_record_id":"fact",'
        '"submissions_record_id":"snapshot-b","tag":"OtherTag"}'
    )
    assert _stable_record_key(first) == _stable_record_key(second)
    assert _stable_record_key(first) != _stable_record_key(changed)
    assert _stable_record_key("plain-key") == "plain-key"


def test_manager_closure_uses_source_and_destination_growth_is_inert(tmp_path: Path) -> None:
    from investment_analyst.workspace.historical_recovery import (
        _13F_SOURCE,
        _MANAGER_OBSERVATION_SOURCE,
        _POSITION_SCHEMA,
    )

    source_root, _ = _seed_source(tmp_path)
    destination_root = _seed_destination(tmp_path)
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    manager_a = "0000019617"
    manager_b = "0000070858"

    def manager_raw(manager: str, identity: str) -> RawRecord:
        return _raw(
            source_id=_MANAGER_OBSERVATION_SOURCE,
            key=json.dumps({"manager_cik": manager}, sort_keys=True),
            schema_version="fixture-manager-v1",
            available_at=_KNOWN_AT,
            identity=identity,
        )

    def position_raw(manager: str, identity: str) -> RawRecord:
        return _raw(
            source_id=_13F_SOURCE,
            key=json.dumps({"manager_cik": manager}, sort_keys=True),
            schema_version=_POSITION_SCHEMA,
            available_at=_KNOWN_AT,
            payload={"position": {"information_table_revision": {"filer_cik": manager}}},
            identity=identity,
        )

    with service.open_storage(
        service.resolve(source_root), WorkspaceAccessMode.READ_WRITE
    ) as source:
        for manager in (manager_a, manager_b):
            raw = manager_raw(manager, f"manager-{manager}")
            source.raw_records.save(raw)
            source.observations.save(
                _observation(
                    raw,
                    field_name="manager_cik",
                    value=Decimal("1"),
                    frequency=DataFrequency.EVENT,
                    observed_at=_KNOWN_AT,
                    period_end=None,
                    available_at=_KNOWN_AT,
                    identity=f"manager-{manager}",
                    unit="CIK",
                )
            )
            source.raw_records.save(position_raw(manager, f"position-{manager}"))
    with service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    ) as destination:
        raw = manager_raw(manager_a, f"manager-{manager_a}")
        destination.raw_records.save(raw)
        destination.observations.save(
            _observation(
                raw,
                field_name="manager_cik",
                value=Decimal("1"),
                frequency=DataFrequency.EVENT,
                observed_at=_KNOWN_AT,
                period_end=None,
                available_at=_KNOWN_AT,
                identity=f"manager-{manager_a}",
                unit="CIK",
            )
        )

    first_builder = _builder(
        service=service,
        source_root=source_root,
        destination_root=destination_root,
    )
    first_output = tmp_path / "selection-first"
    first_builder.build(known_at=_KNOWN_AT, output_root=first_output)
    first = load_historical_recovery_manifest(first_output / "selection.json")
    with service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    ) as destination:
        raw = manager_raw(manager_b, f"manager-{manager_b}")
        destination.raw_records.save(raw)
        destination.observations.save(
            _observation(
                raw,
                field_name="manager_cik",
                value=Decimal("1"),
                frequency=DataFrequency.EVENT,
                observed_at=_KNOWN_AT,
                period_end=None,
                available_at=_KNOWN_AT,
                identity=f"manager-{manager_b}",
                unit="CIK",
            )
        )
    second_builder = _builder(
        service=service,
        source_root=source_root,
        destination_root=destination_root,
    )
    second_output = tmp_path / "selection-second"
    second_builder.build(known_at=_KNOWN_AT, output_root=second_output)
    second = load_historical_recovery_manifest(second_output / "selection.json")

    def entity_ids(manifest, root: Path) -> set[str]:
        identifiers: set[str] = set()
        for page in manifest.pages:
            for line in (root / page.relative_path).read_text(encoding="utf-8").splitlines():
                if line:
                    identifiers.add(json.loads(line)["entity_id"])
        return identifiers

    assert entity_ids(first, first_output) == entity_ids(second, second_output)
    assert first.totals.items == second.totals.items
    assert first.totals.raw_records == second.totals.raw_records
    expected = position_raw(manager_b, f"position-{manager_b}").record_id
    for output in (first_output, second_output):
        found = False
        for page in load_historical_recovery_manifest(output / "selection.json").pages:
            if page.entity_kind.value != "raw-record":
                continue
            for line in (output / page.relative_path).read_text(encoding="utf-8").splitlines():
                if line and json.loads(line)["entity_id"] == str(expected):
                    found = True
        assert found


def test_r2_manager_set_digest_and_admission_binding() -> None:
    from pydantic import ValidationError

    from investment_analyst.workspace.historical_recovery import (
        HistoricalRecoveryAdmission,
        _manager_set_digest,
    )

    managers = (
        "0000019617",
        "0000070858",
        "0000073124",
        "0000093751",
        "0000315066",
        "0000764068",
        "0000861177",
        "0000884546",
        "0000886982",
        "0000895421",
        "0000902219",
        "0000914208",
        "0000933478",
        "0001000275",
        "0001067983",
        "0001214717",
        "0001330387",
        "0001390777",
        "0001422848",
        "0001446194",
        "0001610520",
        "0001871926",
        "0002012383",
        "0002100119",
        "0002100121",
    )
    digest = _manager_set_digest(managers)
    assert digest == "e094473b77602d1d969a0076c56ccf38c5ace53ff327a6a13bc1e21c0e6cea00"
    admission = HistoricalRecoveryAdmission(
        admission_id=uuid4(),
        selection_id=uuid4(),
        selection_digest="a" * 64,
        source_workspace_id=uuid4(),
        destination_workspace_id=uuid4(),
        policy_version="historical-recovery-v2-pit",
        manager_ciks=managers,
        manager_set_sha256=digest,
        authorized_delta_count=369_691,
        authorized_delta_digest="b" * 64,
        created_at=_KNOWN_AT,
    )
    assert admission.manager_ciks == managers
    with pytest.raises(ValidationError, match="manager-set digest"):
        HistoricalRecoveryAdmission.model_validate(
            admission.model_dump() | {"manager_set_sha256": "c" * 64}
        )
