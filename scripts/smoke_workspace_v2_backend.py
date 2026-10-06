#!/usr/bin/env python3
"""Run offline fidelity, recovery, and scaling profiles for workspace v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import duckdb
from duckdb import DuckDBPyConnection

from investment_analyst.analytics.cazatiburones.institutional_event_service import (
    InstitutionalEventService,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    Asset,
    AssetClass,
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricCategory,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import SOURCE_ID
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.historical_analytical_archive import HistoricalAnalyticalArchive
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.serialization import canonical_json_bytes
from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2AnalyticalImporter
from investment_analyst.workspace.backup import WorkspaceBackupError, WorkspaceBackupService
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2026, 8, 1, tzinfo=UTC)
_ASSETS: tuple[tuple[str, AssetClass, str, str], ...] = (
    ("equity:us:aapl", AssetClass.EQUITY, "AAPL", "Apple Inc."),
    ("crypto:btc-usd", AssetClass.CRYPTO, "BTC-USD", "Bitcoin"),
)
_FAMILY_KEYS: tuple[tuple[str, str, MetricCategory], ...] = (
    ("market", "market.close", MetricCategory.MARKET),
    ("fundamental", "fundamental.revenue", MetricCategory.FUNDAMENTAL),
    ("valuation", "valuation.corporate.pe_ratio", MetricCategory.VALUATION),
    (
        "derivatives",
        "crypto.derivatives.funding.sum_1h",
        MetricCategory.CRYPTO_DERIVATIVES,
    ),
    ("events", "cazatiburones.earnings.event_impact", MetricCategory.CAZATIBURONES),
)
_SELECTOR_PAYLOADS: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("report_manager", "0000000001", {"report": {"manager_cik": "0000000001"}}),
    ("outcome_filer", "0000000002", {"outcome": {"filing": {"filer_cik": "0000000002"}}}),
    (
        "position_report",
        "00000000-0000-4000-8000-000000000003",
        {"position": {"report_id": "00000000-0000-4000-8000-000000000003"}},
    ),
    ("semantics_manager", "0000000004", {"artifact": {"manager_cik": "0000000004"}}),
    (
        "correspondence_artifact",
        "artifact-5",
        {"correspondence": {"artifact_id": "artifact-5", "manager_cik": "0000000006"}},
    ),
    (
        "correspondence_manager",
        "0000000007",
        {"correspondence": {"artifact_id": "artifact-6", "manager_cik": "0000000007"}},
    ),
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _model_digest(items: Collection[MetricResult]) -> str:
    ordered = sorted(items, key=lambda item: str(item.result_id))
    digest = hashlib.sha256()
    for item in ordered:
        digest.update(canonical_json_bytes(item))
        digest.update(b"\n")
    return digest.hexdigest()


def _max_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _source_id(family: str) -> str:
    return f"smoke:{family}"


def _source_reference(source_id: str, index: int, moment: datetime) -> SourceReference:
    return SourceReference(
        source_id=source_id,
        record_key=f"smoke-{index}",
        retrieved_at=moment,
    )


def _record_pair(index: int) -> tuple[RawRecord, NormalizedObservation]:
    family, metric_key, _category = _FAMILY_KEYS[index % len(_FAMILY_KEYS)]
    moment = _BASE + timedelta(minutes=index)
    asset_id = _ASSETS[index % len(_ASSETS)][0]
    source_id = _source_id(family)
    payload: dict[str, object] = {"family": family, "ordinal": index, "value": str(index)}
    if index < len(_SELECTOR_PAYLOADS):
        _field, _selected, payload = _SELECTOR_PAYLOADS[index]
        source_id = "sec:smoke"
    if index >= 257:
        asset_id = "equity:us:aapl"
        source_id = SOURCE_ID
        source = SourceReference(
            source_id=source_id,
            record_key=json.dumps(
                {
                    "artifact_id": str(uuid5(NAMESPACE_URL, f"smoke-artifact-{index}")),
                    "manager_cik": "0001350694",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            retrieved_at=moment,
        )
    else:
        source = _source_reference(source_id, index, moment)
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"workspace-v2-smoke-raw-{index}"),
        asset_id=asset_id,
        source=source,
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload=payload,
        schema_version="workspace-v2-smoke-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"workspace-v2-smoke-observation-{index}"),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name=("institutional_reported_shares" if index >= 257 else f"{family}.value"),
        value=Decimal(f"{index}.2300"),
        unit="USD" if family != "derivatives" else "rate",
        frequency=DataFrequency.DAY_1,
        observed_at=moment,
        period_start=moment,
        period_end=moment,
        available_at=moment,
        normalized_at=moment,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="workspace-v2-smoke-v1",
    )
    return raw, observation


def _metric_for(
    observation: NormalizedObservation,
    *,
    family: str,
    key: str,
    identity: str,
    origin_historical: bool,
    parameters: dict[str, object] | None = None,
) -> MetricResult:
    candidate = MetricResult(
        result_id=uuid4(),
        asset_id=observation.asset_id,
        metric_key=key,
        value=Decimal("-0.0012300") if family == "derivatives" else Decimal("150.0000"),
        unit=observation.unit,
        as_of=observation.available_at,
        available_at=observation.available_at,
        computed_at=observation.available_at + timedelta(minutes=1),
        parameters=parameters or {"known_at": "smoke-run", "family": family},
        input_observation_ids=[observation.observation_id],
        algorithm_version="workspace-v2-smoke-v1",
        quality=DataQuality.VALID,
    )
    if origin_historical:
        return candidate.model_copy(update={"result_id": uuid5(NAMESPACE_URL, identity)})
    return candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})


def _institutional_metric(observation: NormalizedObservation, index: int) -> MetricResult:
    candidate = MetricResult(
        result_id=uuid4(),
        asset_id=observation.asset_id,
        metric_key="cazatiburones.institutional.delta_reported_shares",
        value=Decimal("500.00") + Decimal(index),
        unit="shares",
        as_of=observation.available_at,
        available_at=observation.available_at,
        computed_at=observation.available_at,
        parameters={
            "manager_cik": "0001350694",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
            "report_period": f"2024-{'06' if index == 0 else '12'}-30",
            "prior_report_period": "2024-03-31" if index == 0 else "2024-06-30",
        },
        input_observation_ids=[observation.observation_id],
        algorithm_version="cazatiburones-institutional-metrics-v1",
        quality=DataQuality.VALID,
    )
    return candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})


def _diagnostic(metric: MetricResult, identity: str) -> DiagnosticResult:
    family = "fundamental" if metric.metric_key.startswith("fundamental.") else "market"
    mode = DiagnosticMode.FUNDAMENTAL if family == "fundamental" else DiagnosticMode.MARKET
    component = DiagnosticComponent(
        component_key=family,
        score=Decimal("70.00"),
        weight=Decimal("1.00"),
        weighted_contribution=Decimal("70.00"),
        metric_result_ids=[metric.result_id],
        explanation="Offline workspace v2 smoke component.",
    )
    evidence = DiagnosticEvidence(
        metric_result_id=metric.result_id,
        direction=EvidenceDirection.SUPPORTS,
        contribution=Decimal("0.75"),
        reason="Offline workspace v2 smoke evidence.",
    )
    return DiagnosticResult(
        diagnostic_id=uuid5(NAMESPACE_URL, identity),
        asset_id=metric.asset_id,
        mode=mode,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("70.00"),
        confidence=Decimal("0.75"),
        as_of=metric.as_of,
        available_at=metric.available_at,
        computed_at=metric.computed_at,
        components=[component],
        evidence=[evidence],
        algorithm_version="workspace-v2-smoke-v1",
        summary="Offline workspace v2 smoke diagnostic.",
        quality=DataQuality.VALID,
    )


def _upsert_catalog(storage) -> None:
    for asset_id, asset_class, symbol, name in _ASSETS:
        storage.assets.upsert(
            Asset(
                asset_id=asset_id,
                symbol=symbol,
                name=name,
                asset_class=asset_class,
                quote_currency="USD",
            )
        )
    source_ids = {_source_id(family) for family, _, _ in _FAMILY_KEYS} | {
        "sec:smoke",
        SOURCE_ID,
    }
    for source_id in sorted(source_ids):
        storage.sources.upsert(
            SourceDefinition(
                source_id=source_id,
                provider_name="offline-smoke-fixture",
                dataset_name=source_id,
                source_type=SourceType.REGISTRY if source_id == "sec:smoke" else SourceType.MARKET,
                is_official=source_id == "sec:smoke",
            )
        )
    for family, key, category in _FAMILY_KEYS:
        storage.metric_definitions.upsert(
            MetricDefinition(
                metric_key=key,
                display_name=f"Smoke {family}",
                category=category,
                description="Offline storage fidelity fixture.",
                formula="fixture value",
                unit="unit",
                definition_version="workspace-v2-smoke-v1",
            )
        )


def _make_historical_archive(
    source_root: Path,
    pairs: list[tuple[RawRecord, NormalizedObservation]],
) -> tuple[
    RawV2Staging,
    HistoricalAnalyticalArchive,
    DuckDBPyConnection,
    list[MetricResult],
    list[DiagnosticResult],
]:
    source_root.mkdir(parents=True)
    connection = duckdb.connect(str(source_root / "historical-source.duckdb"))
    staging = RawV2Staging(source_root, connection)
    staging.open()
    archive = HistoricalAnalyticalArchive(connection)
    archive.ensure(create=True)
    staging.save_many([raw for raw, _ in pairs])
    staging.save_observations([observation for _, observation in pairs])
    metrics: list[MetricResult] = []
    diagnostics: list[DiagnosticResult] = []
    for index, (_raw, observation) in enumerate(pairs):
        family_index = index % 3
        family = ("market", "fundamental", "derivatives")[family_index]
        key = {
            "market": "market.close",
            "fundamental": "fundamental.revenue",
            "derivatives": "crypto.derivatives.funding.sum_1h",
        }[family]
        if family == "derivatives" and observation.asset_id != "crypto:btc-usd":
            family, key = "market", "market.close"
        metric = _metric_for(
            observation,
            family=family,
            key=key,
            identity=f"workspace-v2-history-metric-{index}",
            origin_historical=True,
            parameters={
                "known_at": f"archive-run-{index % 7}",
                "family": family,
                "lineage_refs": [str(observation.observation_id)],
            },
        )
        metrics.append(metric)
        diagnostics.append(_diagnostic(metric, f"workspace-v2-history-diagnostic-{index}"))
    for start in range(0, len(metrics), 256):
        archive.save_metrics(metrics[start : start + 256])
        archive.save_diagnostics(diagnostics[start : start + 256])
    return staging, archive, connection, metrics, diagnostics


def _parquet_columns(storage, table_name: str) -> tuple[str, ...]:
    export_path = storage.paths.exports_dir / f"{table_name}.parquet"
    rows = storage.store.connection.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)", [str(export_path)]
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _run_fidelity_and_recovery(scratch: Path) -> dict[str, object]:
    workspace_root = scratch / "fidelity-workspace"
    source_root = scratch / "historical-source"
    pairs = [_record_pair(index) for index in range(259)]
    raw_records = [raw for raw, _ in pairs]
    observations = [observation for _, observation in pairs]
    pit_cuts = tuple(item.available_at for item in observations[-2:])

    def event_clock() -> datetime:
        return datetime(2026, 10, 1, tzinfo=UTC)

    (
        source_staging,
        archive,
        source_connection,
        history_metrics,
        history_diagnostics,
    ) = _make_historical_archive(source_root, pairs)
    service = WorkspaceService(environ={}, home=scratch / "home")
    initialization = service.initialize(workspace_root, format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    workspace_request = StorageLocationRequest(workspace=workspace_root)
    expected_rows = len(pairs)

    injection_state = {"raised": False}

    def fail_after_committed_page(event: str) -> None:
        if event == "after_metric_page_write" and not injection_state["raised"]:
            injection_state["raised"] = True
            raise RuntimeError("smoke interruption after committed metric page")

    expected_fingerprint = _sha256(
        json.dumps(
            {
                "metrics": len(history_metrics),
                "diagnostics": len(history_diagnostics),
                "first_metric": str(history_metrics[0].result_id),
                "last_metric": str(history_metrics[-1].result_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )

    selector_cut_early = _BASE + timedelta(minutes=2)
    selector_cut_late = _BASE + timedelta(minutes=5)
    selector_results_by_cut: dict[str, dict[str, list[str]]] = {}
    selector_hydrations: list[int] = []
    with runtime.open_storage(
        workspace_request, access_mode=WorkspaceAccessMode.READ_WRITE
    ) as storage:
        _upsert_catalog(storage)
        storage.raw_records.save_many(raw_records)
        storage.observations.save_many(observations)
        raw_staging = storage.store.raw_staging
        selector_hydrations: list[int] = []
        original_get_many = raw_staging.get_many

        def tracked_raw_get_many(record_ids: Collection[UUID]):
            selector_hydrations.append(len(record_ids))
            return original_get_many(record_ids)

        with patch.object(raw_staging, "get_many", side_effect=tracked_raw_get_many):
            for cut in (selector_cut_early, selector_cut_late):
                cut_results: dict[str, list[str]] = {}
                max_index = int((cut - _BASE).total_seconds() // 60)
                for index, (field, value, _payload) in enumerate(_SELECTOR_PAYLOADS):
                    matches = storage.raw_records.select_record_ids_by_json_field(
                        field=field,
                        values=(value,),
                        source_id="sec:smoke",
                        schema_version="workspace-v2-smoke-v1",
                        available_to=cut,
                    )
                    expected = [raw_records[index].record_id] if index <= max_index else []
                    if matches != expected:
                        raise RuntimeError(
                            f"SQL projection selector {field!r} returned wrong IDs at PIT cut"
                        )
                    cut_results[field] = [str(identifier) for identifier in matches]
                selector_results_by_cut[cut.isoformat()] = cut_results
        if selector_hydrations:
            raise RuntimeError("raw JSON selector hydrated blobs before returning IDs")

        importer = WorkspaceV2AnalyticalImporter(
            archive,
            CompactAnalyticalStore(storage.store.connection),
            source_root=source_root,
            destination_root=storage.paths.root,
            state_root=storage.paths.root / "historical-import-state",
            source_workspace_id="scratch-historical-v1",
            source_fingerprint=expected_fingerprint,
            failure_injector=fail_after_committed_page,
        )
        try:
            importer.run()
            raise RuntimeError("historical importer did not exercise the interruption point")
        except RuntimeError as error:
            if "smoke interruption" not in str(error):
                raise

    # Close the DuckDB writer before reopening and resuming the durable archive transfer.
    live_metrics: list[MetricResult] = []
    live_by_family: dict[str, MetricResult] = {}
    live_diagnostics: list[DiagnosticResult] = []
    institutional_metrics: list[MetricResult] = []
    event_materializations: list[dict[str, object]] = []
    family_live_receipt_count = 0
    repeated_live_created_count = 0
    repeated_live_reused_count = 0
    institutional_observation_cut_ids: dict[str, list[str]] = {}
    recovered_import = None
    v2_parquet_columns: dict[str, tuple[str, ...]] = {}
    logical_sizes: dict[str, int] = {}
    historical_archive_logical_sizes: dict[str, int] = {}
    workspace_database_path: Path | None = None
    workspace_exports_dir: Path | None = None
    workspace_raw_dir: Path | None = None
    inspection_counts: dict[str, int] = {}
    export_names = (
        "assets",
        "source_definitions",
        "metric_definitions",
        "raw_record_index",
        "normalized_observations",
        "metric_results",
        "diagnostic_results",
    )
    with runtime.open_storage(
        workspace_request, access_mode=WorkspaceAccessMode.READ_WRITE
    ) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        recovered_import = WorkspaceV2AnalyticalImporter(
            archive,
            compact,
            source_root=source_root,
            destination_root=storage.paths.root,
            state_root=storage.paths.root / "historical-import-state",
            source_workspace_id="scratch-historical-v1",
            source_fingerprint=expected_fingerprint,
        ).run()
        if (
            recovered_import.metric_count != expected_rows
            or recovered_import.diagnostic_count != expected_rows
        ):
            raise RuntimeError("historical archive recovery did not preserve all rows")
        if recovered_import.metric_reused_count < 256:
            raise RuntimeError("historical committed page was not recognized on resume")
        if compact.historical_seal() is None:
            raise RuntimeError("historical source inventory was not sealed")
        historic_seal_before_live = compact.historical_seal()

        for index, (family, key, _category) in enumerate(_FAMILY_KEYS):
            observation = observations[index]
            if family == "derivatives" and observation.asset_id != "crypto:btc-usd":
                observation = next(
                    item for item in observations if item.asset_id == "crypto:btc-usd"
                )
            metric = _metric_for(
                observation,
                family=family,
                key=key,
                identity=f"workspace-v2-live-{family}",
                origin_historical=False,
                parameters={"run_id": "live-family-smoke", "shared": True},
            )
            live_metrics.append(metric)
            live_by_family[family] = metric
        family_live_receipt_count = storage.metric_results.save_many(live_metrics).created_count
        institutional_observations = observations[-2:]
        institutional_metrics = [
            _institutional_metric(observation, index)
            for index, observation in enumerate(institutional_observations)
        ]
        storage.metric_results.save_many(institutional_metrics)
        writer_event_service = InstitutionalEventService(storage, clock=event_clock)
        for cut in pit_cuts:
            summary = writer_event_service.materialize(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=cut,
            )
            event_materializations.append(
                {"snapshot_id": str(summary.snapshot_id), "event_count": summary.events}
            )
        live_diagnostics = [
            _diagnostic(live_by_family["market"], "workspace-v2-live-market-diagnostic"),
            _diagnostic(live_by_family["fundamental"], "workspace-v2-live-fundamental-diagnostic"),
            _diagnostic(live_by_family["derivatives"], "workspace-v2-live-derivatives-diagnostic"),
        ]
        storage.diagnostics.save_many(live_diagnostics)
        if compact.historical_seal() != historic_seal_before_live:
            raise RuntimeError("live-family writes changed the historical seal")
        for metric in (*history_metrics, *live_metrics, *institutional_metrics):
            if storage.metric_results.get(metric.result_id) != metric:
                raise RuntimeError(f"metric did not round-trip exactly: {metric.metric_key}")
        for diagnostic in (*history_diagnostics, *live_diagnostics):
            if storage.diagnostics.get(diagnostic.diagnostic_id) != diagnostic:
                raise RuntimeError("diagnostic did not round-trip exactly")

        repeated_live = storage.metric_results.save_many([*live_metrics, *institutional_metrics])
        repeated_diagnostics = storage.diagnostics.save_many(live_diagnostics)
        repeated_live_created_count = (
            repeated_live.created_count + repeated_diagnostics.created_count
        )
        repeated_live_reused_count = repeated_live.reused_count + repeated_diagnostics.reused_count
        if repeated_live_created_count != 0:
            raise RuntimeError("identical LIVE reimport created new rows")
        if len({asset_id for asset_id, *_ in _ASSETS}) < 2:
            raise RuntimeError("fidelity fixture did not span two asset classes")

        for table_name in export_names:
            storage.parquet.export_table(table_name)
        v2_parquet_columns = {
            table_name: _parquet_columns(storage, table_name) for table_name in export_names
        }
        logical_sizes = _logical_workspace_table_bytes(storage.store.connection)
        workspace_database_path = storage.store.paths.database_path
        workspace_exports_dir = storage.paths.exports_dir
        workspace_raw_dir = storage.paths.raw_dir
        inspection_counts = {
            "raw": storage.raw_records.count(),
            "observations": storage.observations.count(),
            "historical_metrics": compact.count_metrics(origin="HISTORICAL"),
            "live_metrics": compact.count_metrics(origin="LIVE"),
            "historical_diagnostics": compact.count_diagnostics(origin="HISTORICAL"),
            "live_diagnostics": compact.count_diagnostics(origin="LIVE"),
        }

    # Query both existing institutional consumers at two distinct PIT cuts.
    v2_observation_results_by_cut: dict[str, str] = {}
    v2_event_snapshots_by_cut: dict[str, dict[str, object]] = {}
    with runtime.open_storage(
        workspace_request, access_mode=WorkspaceAccessMode.READ_ONLY
    ) as storage:
        observation_service = InstitutionalObservationService(storage)
        for cut in pit_cuts:
            visible = observation_service.list_for_manager(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=cut,
                field_name="institutional_reported_shares",
            )
            institutional_observation_cut_ids[cut.isoformat()] = sorted(
                str(item.observation_id) for item in visible
            )
            v2_observation_results_by_cut[cut.isoformat()] = _sha256(
                b"\n".join(canonical_json_bytes(item) for item in visible)
            )
        event_service = InstitutionalEventService(storage)
        for cut, materialization in zip(pit_cuts, event_materializations, strict=True):
            snapshot = event_service.query(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=cut,
                snapshot_id_value=UUID(str(materialization["snapshot_id"])),
            )
            if snapshot is None or len(snapshot.events) != materialization["event_count"]:
                raise RuntimeError("institutional event snapshot failed to reopen at its PIT cut")
            v2_event_snapshots_by_cut[cut.isoformat()] = snapshot.model_dump(mode="json")

    # The v1 workspace is the live schema authority for Parquet compatibility.
    v1_root = scratch / "parquet-v1-reference"
    service.initialize(v1_root)
    with runtime.open_storage(
        StorageLocationRequest(workspace=v1_root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        _upsert_catalog(storage)
        storage.raw_records.save_many(raw_records)
        storage.observations.save_many(observations)
        storage.metric_results.save_many([*history_metrics, *live_metrics, *institutional_metrics])
        storage.diagnostics.save_many([*history_diagnostics, *live_diagnostics])
        v1_selector_results_by_cut: dict[str, dict[str, list[str]]] = {}
        for cut in (selector_cut_early, selector_cut_late):
            cut_results = {
                field: [
                    str(identifier)
                    for identifier in storage.raw_records.select_record_ids_by_json_field(
                        field=field,
                        values=(value,),
                        source_id="sec:smoke",
                        schema_version="workspace-v2-smoke-v1",
                        available_to=cut,
                    )
                ]
                for field, value, _payload in _SELECTOR_PAYLOADS
            }
            v1_selector_results_by_cut[cut.isoformat()] = cut_results
        if v1_selector_results_by_cut != selector_results_by_cut:
            raise RuntimeError("raw selector PIT results differ between v1 and v2")

        v1_materializations: list[dict[str, object]] = []
        writer_event_service = InstitutionalEventService(storage, clock=event_clock)
        for cut in pit_cuts:
            summary = writer_event_service.materialize(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=cut,
            )
            v1_materializations.append(
                {"snapshot_id": str(summary.snapshot_id), "event_count": summary.events}
            )
        for table_name in export_names:
            storage.parquet.export_table(table_name)
        v1_parquet_columns = {
            table_name: _parquet_columns(storage, table_name) for table_name in export_names
        }
    v1_observation_cut_ids: dict[str, list[str]] = {}
    v1_observation_results_by_cut: dict[str, str] = {}
    v1_event_snapshots_by_cut: dict[str, dict[str, object]] = {}
    with runtime.open_storage(
        StorageLocationRequest(workspace=v1_root),
        access_mode=WorkspaceAccessMode.READ_ONLY,
    ) as storage:
        reader_observation_service = InstitutionalObservationService(storage)
        reader_event_service = InstitutionalEventService(storage)
        for cut, materialization in zip(pit_cuts, v1_materializations, strict=True):
            visible = reader_observation_service.list_for_manager(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=cut,
                field_name="institutional_reported_shares",
            )
            v1_observation_cut_ids[cut.isoformat()] = sorted(
                str(item.observation_id) for item in visible
            )
            v1_observation_results_by_cut[cut.isoformat()] = _sha256(
                b"\n".join(canonical_json_bytes(item) for item in visible)
            )
            snapshot = reader_event_service.query(
                asset_id="equity:us:aapl",
                manager_cik="0001350694",
                known_at=cut,
                snapshot_id_value=UUID(str(materialization["snapshot_id"])),
            )
            if snapshot is None:
                raise RuntimeError("institutional event snapshot failed to reopen in v1")
            v1_event_snapshots_by_cut[cut.isoformat()] = snapshot.model_dump(mode="json")
    if v2_parquet_columns != v1_parquet_columns:
        raise RuntimeError("workspace v2 Parquet schema differs from the v1 exporter")
    if v1_observation_cut_ids != institutional_observation_cut_ids:
        raise RuntimeError("institutional observation results differ between v1 and v2")
    if v1_observation_results_by_cut != v2_observation_results_by_cut:
        raise RuntimeError("institutional observation models differ between v1 and v2")

    v2_materializations = event_materializations
    if v1_materializations != v2_materializations:
        raise RuntimeError("institutional event results differ between v1 and v2")
    if v1_event_snapshots_by_cut != v2_event_snapshots_by_cut:
        raise RuntimeError("institutional event snapshots differ between v1 and v2")
    if [item["event_count"] for item in v2_materializations] != [1, 2]:
        raise RuntimeError("institutional event consumer did not respect both PIT cuts")

    # Read-only open and close must leave the persistent workspace byte-identical.
    before_ro = _tree_snapshot(workspace_root)
    inspection = service.inspect(workspace_root)
    after_ro = _tree_snapshot(workspace_root)
    if inspection.status != "ready" or before_ro != after_ro:
        raise RuntimeError("read-only reopen modified workspace state or failed inspection")

    checkpoint = workspace_root / "state" / "smoke-checkpoint.json"
    checkpoint.write_bytes(b'{"checkpoint":"complete","opaque":true}\n')
    backup_service = WorkspaceBackupService(service)
    backup_root = scratch / "workspace-v2-backup"
    manifest = backup_service.create(workspace_root, backup_root)
    restored_roots = (scratch / "restore-a", scratch / "restore-b")
    restore_results = [backup_service.restore(backup_root, root) for root in restored_roots]
    if any(result.status != "ready" for result in restore_results):
        raise RuntimeError("workspace v2 restore did not reach ready")
    if any(
        (root / "state" / checkpoint.name).read_bytes() != checkpoint.read_bytes()
        for root in restored_roots
    ):
        raise RuntimeError("workspace state/journal bytes changed during restore")

    tampered_backup = scratch / "tampered-backup"
    shutil.copytree(backup_root, tampered_backup)
    manifest_data = json.loads((tampered_backup / "backup_manifest.json").read_text())
    tampered_file = tampered_backup / manifest_data["files"][0]["path"]
    with tampered_file.open("ab") as stream:
        stream.write(b"tamper")
    try:
        backup_service.restore(tampered_backup, scratch / "tampered-restore")
    except WorkspaceBackupError:
        tamper_rejected = True
    else:
        raise RuntimeError("restore accepted a tampered backup file")

    initialized_paths = service.resolve(workspace_root)
    writer = service.open_storage(initialized_paths, WorkspaceAccessMode.READ_WRITE)
    try:
        try:
            backup_service.create(workspace_root, scratch / "writer-conflict-backup")
        except WorkspaceBackupError:
            writer_exclusion = True
        else:
            raise RuntimeError("backup accepted an active workspace writer")
    finally:
        writer.close()

    historical_archive_logical_sizes = _logical_workspace_table_bytes(source_connection)
    source_staging.close()
    source_connection.close()
    historical_archive_database_path = source_root / "historical-source.duckdb"
    historical_archive_wal_path = historical_archive_database_path.with_name(
        f"{historical_archive_database_path.name}.wal"
    )
    historical_archive_raw_dir = source_root / "raw"
    historical_archive_measurements = {
        "logical_table_bytes": historical_archive_logical_sizes,
        "logical_total_bytes": sum(historical_archive_logical_sizes.values()),
        "database_bytes": historical_archive_database_path.stat().st_size,
        "wal_bytes": (
            historical_archive_wal_path.stat().st_size
            if historical_archive_wal_path.exists()
            else 0
        ),
        "raw_blob_file_bytes": sum(
            path.stat().st_size for path in historical_archive_raw_dir.rglob("*") if path.is_file()
        ),
        "total_file_bytes": sum(
            path.stat().st_size for path in source_root.rglob("*") if path.is_file()
        ),
    }
    if (
        workspace_database_path is None
        or workspace_exports_dir is None
        or workspace_raw_dir is None
    ):
        raise RuntimeError("workspace storage paths were not captured for byte measurement")
    workspace_wal_path = workspace_database_path.with_name(f"{workspace_database_path.name}.wal")
    workspace_measurements = {
        "logical_table_bytes": logical_sizes,
        "logical_total_bytes": sum(logical_sizes.values()),
        "database_bytes": workspace_database_path.stat().st_size,
        "wal_bytes": workspace_wal_path.stat().st_size if workspace_wal_path.exists() else 0,
        "raw_blob_file_bytes": sum(
            path.stat().st_size for path in workspace_raw_dir.rglob("*") if path.is_file()
        ),
        "parquet_export_file_bytes": sum(
            path.stat().st_size
            for path in workspace_exports_dir.glob("*.parquet")
            if path.is_file()
        ),
        "total_file_bytes": sum(
            path.stat().st_size for path in workspace_root.rglob("*") if path.is_file()
        ),
    }
    return {
        "profile": "workspace_v2_fidelity_and_recovery",
        "workspace_format": initialization.manifest.format_version,
        "historical_import": recovered_import.model_dump(mode="json"),
        "counts": inspection_counts,
        "asset_classes": sorted({asset_class.value for _, asset_class, _, _ in _ASSETS}),
        "family_live_created": family_live_receipt_count,
        "family_live_keys": sorted(metric.metric_key for metric in live_metrics),
        "institutional_live_metrics": len(institutional_metrics),
        "identical_live_reimport_created": repeated_live_created_count,
        "identical_live_reimport_reused": repeated_live_reused_count,
        "selector_fields": sorted(_field for _field, _value, _payload in _SELECTOR_PAYLOADS),
        "selector_results_by_pit_cut": selector_results_by_cut,
        "selector_hydrated_raw_records": sum(selector_hydrations),
        "institutional_observation_ids_by_pit_cut": institutional_observation_cut_ids,
        "institutional_observation_sha256_by_pit_cut": v2_observation_results_by_cut,
        "institutional_event_snapshots_by_pit_cut": v2_materializations,
        "institutional_event_snapshot_sha256_by_pit_cut": {
            cut: _sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode())
            for cut, snapshot in v2_event_snapshots_by_cut.items()
        },
        "institutional_consumers_match_v1": True,
        "parquet_column_names": {key: list(value) for key, value in v2_parquet_columns.items()},
        "parquet_schema_matches_v1": True,
        "byte_measurements": {
            "logical_size_policy": {
                "text_and_json": "UTF-8 payload bytes; blobs use stored bytes",
                "scalars": "fixed width per non-null value",
                "nulls": "zero logical payload bytes",
                "excluded_overhead": (
                    "row, tuple, index, and storage-block overhead; reported in physical files"
                ),
            },
            "historical_source_archive": historical_archive_measurements,
            "workspace_v2": workspace_measurements,
        },
        "backup_schema": manifest.schema_version,
        "backup_file_count": len(manifest.files),
        "double_restore_ready": [result.status for result in restore_results],
        "checkpoint_bytes_preserved": True,
        "tampered_backup_rejected": tamper_rejected,
        "active_writer_excluded": writer_exclusion,
        "interruption_recovered": True,
        "writer_closed_before_resume": True,
    }


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, str], ...]:
    result: list[tuple[str, int, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        result.append((path.relative_to(root).as_posix(), len(payload), _sha256(payload)))
    return tuple(result)


def _scale_observation(
    index: int, asset_id: str, *, prefix: str
) -> tuple[RawRecord, NormalizedObservation]:
    moment = _BASE + timedelta(minutes=index)
    source = _source_reference("smoke:scale", index, moment)
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"{prefix}-raw-{index}"),
        asset_id=asset_id,
        source=source,
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"index": index, "asset": asset_id},
        schema_version="workspace-v2-scale-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"{prefix}-observation-{index}"),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name="scale_value",
        value=Decimal(f"{index}.000100"),
        unit="unit",
        frequency=DataFrequency.DAY_1,
        observed_at=moment,
        available_at=moment,
        normalized_at=moment,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="workspace-v2-scale-v1",
    )
    return raw, observation


def _logical_workspace_table_bytes(connection: DuckDBPyConnection) -> dict[str, int]:
    tables = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    output: dict[str, int] = {}
    for row in tables:
        table_name = str(row[0])
        columns = connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
            [table_name],
        ).fetchall()
        expressions: list[str] = []
        for column, data_type in columns:
            name = '"' + str(column).replace('"', '""') + '"'
            kind = str(data_type).upper()
            if kind in {"VARCHAR", "JSON"}:
                expressions.append(f"coalesce(sum(octet_length(encode({name}))), 0)")
            elif kind == "BLOB":
                expressions.append(f"coalesce(sum(octet_length({name})), 0)")
            elif kind == "BOOLEAN" or kind in {"TINYINT", "UTINYINT"}:
                expressions.append(f"count({name})")
            elif kind in {"SMALLINT", "USMALLINT"}:
                expressions.append(f"count({name}) * 2")
            elif kind in {"INTEGER", "UINTEGER", "FLOAT", "DATE"}:
                expressions.append(f"count({name}) * 4")
            elif kind in {
                "BIGINT",
                "UBIGINT",
                "DOUBLE",
                "TIME",
                "TIMESTAMP",
                "TIMESTAMP WITH TIME ZONE",
            }:
                expressions.append(f"count({name}) * 8")
            elif kind == "HUGEINT" or kind == "UUID":
                expressions.append(f"count({name}) * 16")
            else:
                raise RuntimeError(
                    f"scale logical-size measurement does not support {table_name}.{column} "
                    f"({data_type})"
                )
        if not expressions:
            output[table_name] = 0
            continue
        table = '"' + table_name.replace('"', '""') + '"'
        result = connection.execute(f"SELECT {' + '.join(expressions)} FROM {table}").fetchone()
        output[table_name] = int(result[0]) if result is not None else 0
    return output


class _DuckDBQueryCounter:
    def __init__(self) -> None:
        self.phase: str | None = None
        self.counts: dict[str, int] = {}

    def record(self) -> None:
        if self.phase is not None:
            self.counts[self.phase] = self.counts.get(self.phase, 0) + 1


class _CountingDuckDBConnection:
    def __init__(
        self,
        connection: DuckDBPyConnection,
        counter: _DuckDBQueryCounter,
    ) -> None:
        self._connection = connection
        self._counter = counter

    def execute(self, query: str, parameters: object | None = None) -> _CountingDuckDBConnection:
        self._counter.record()
        if parameters is None:
            self._connection.execute(query)
        else:
            self._connection.execute(query, parameters)
        return self

    def executemany(
        self,
        query: str,
        parameters: object,
    ) -> _CountingDuckDBConnection:
        self._counter.record()
        self._connection.executemany(query, parameters)
        return self

    def sql(self, query: str) -> object:
        self._counter.record()
        return self._connection.sql(query)

    def query(self, query: str) -> object:
        self._counter.record()
        return self._connection.query(query)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def _run_scale(scratch: Path) -> dict[str, object]:
    scales = (257, 1537)
    scenarios = [_run_scale_case(scratch, scale_size=size) for size in scales]
    funding_shares = {
        str(scenario["counts"]["total_metrics"]): scenario["funding_metric_share"]
        for scenario in scenarios
    }
    return {
        "profile": "workspace_v2_scale",
        "mix_rule": "funding_count=round(total_metrics*100/1537); remaining metrics are unrelated",
        "scenarios": scenarios,
        "scenario_funding_metric_share": funding_shares,
        "scenario_sizes": list(scales),
    }


def _run_scale_case(scratch: Path, *, scale_size: int) -> dict[str, object]:
    started = time.perf_counter()
    workspace_root = scratch / f"scale-workspace-{scale_size}"
    service = WorkspaceService(environ={}, home=scratch / f"scale-home-{scale_size}")
    initialization = service.initialize(workspace_root, format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    request = StorageLocationRequest(workspace=workspace_root)
    target_asset = "crypto:btc-usd"
    unrelated_assets = tuple(f"equity:us:scale-{index:03d}" for index in range(32))
    shared_pairs = [
        _scale_observation(index, target_asset, prefix="scale-shared") for index in range(720)
    ]
    funding_count = round(scale_size * 100 / 1537)
    unrelated_metric_count = scale_size - funding_count
    unrelated_pairs: list[tuple[RawRecord, NormalizedObservation]] = []
    unrelated_metrics: list[MetricResult] = []
    for index in range(unrelated_metric_count):
        asset_id = unrelated_assets[index % len(unrelated_assets)]
        raw, observation = _scale_observation(index, asset_id, prefix="scale-unrelated")
        unrelated_pairs.append((raw, observation))
        candidate = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.scale.close",
            value=Decimal(f"{index}.0100"),
            unit="unit",
            as_of=observation.available_at,
            available_at=observation.available_at,
            computed_at=observation.available_at + timedelta(minutes=1),
            parameters={"unrelated_index": index},
            input_observation_ids=[observation.observation_id],
            algorithm_version="workspace-v2-scale-v1",
            quality=DataQuality.VALID,
        )
        unrelated_metrics.append(
            candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})
        )
    shared_observations = [observation for _, observation in shared_pairs]
    shared_ids = [item.observation_id for item in shared_observations]
    funding_metrics: list[MetricResult] = []
    funding_keys = (
        "crypto.derivatives.funding.sum_1h",
        "crypto.derivatives.funding.mean_1h",
    )
    available_at = max(item.available_at for item in shared_observations)
    for index in range(funding_count):
        rotated_ids = shared_ids[index:] + shared_ids[:index]
        candidate = MetricResult(
            result_id=uuid4(),
            asset_id=target_asset,
            metric_key=funding_keys[index % len(funding_keys)],
            value=Decimal(f"{index}.00012300"),
            unit="rate",
            as_of=available_at,
            available_at=available_at,
            computed_at=available_at + timedelta(minutes=1),
            parameters={"window": 720, "sequence_variant": index, "known_at": "scale-run"},
            input_observation_ids=rotated_ids,
            algorithm_version="workspace-v2-scale-v1",
            quality=DataQuality.VALID,
        )
        funding_metrics.append(
            candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})
        )

    metrics = [*unrelated_metrics, *funding_metrics]
    spill_directory = workspace_root.parent / f"duckdb-temp-{scale_size}"
    spill_directory.mkdir(parents=True)
    spill_samples: dict[str, int] = {}
    query_counter = _DuckDBQueryCounter()
    original_connect = duckdb.connect

    def tracked_connect(*args: object, **kwargs: object) -> _CountingDuckDBConnection:
        connection = original_connect(*args, **kwargs)
        return _CountingDuckDBConnection(connection, query_counter)

    def sample_spill(phase: str) -> None:
        spill_samples[phase] = sum(
            path.stat().st_size for path in spill_directory.rglob("*") if path.is_file()
        )

    connection_patcher = patch.object(duckdb, "connect", side_effect=tracked_connect)
    connection_patcher.start()
    query_counter.phase = "workspace_open_and_writes"
    with runtime.open_storage(
        request,
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        connection = storage.store.connection
        spill_setting = str(spill_directory).replace("'", "''")
        connection.execute(f"SET temp_directory = '{spill_setting}'")
        target_definition = next(item for item in _ASSETS if item[0] == target_asset)
        storage.assets.upsert(
            Asset(
                asset_id=target_definition[0],
                symbol=target_definition[2],
                name=target_definition[3],
                asset_class=target_definition[1],
                quote_currency="USD",
            )
        )
        for index, asset_id in enumerate(unrelated_assets):
            storage.assets.upsert(
                Asset(
                    asset_id=asset_id,
                    symbol=f"SCALE{index:03d}",
                    name=f"Scale fixture {index:03d}",
                    asset_class=AssetClass.EQUITY,
                    quote_currency="USD",
                )
            )
        storage.raw_records.save_many([raw for raw, _ in (*shared_pairs, *unrelated_pairs)])
        storage.observations.save_many(
            [observation for _, observation in (*shared_pairs, *unrelated_pairs)]
        )
        storage.metric_results.save_many(metrics)
        sample_spill("after_bulk_writes")
        compact = storage.metric_results._compact
        original_get_metrics = compact.get_metrics
        selected_hydrated: set[UUID] = set()
        selected_pages: list[int] = []

        def track_target_hydration(identifiers: Collection[UUID]):
            ids = tuple(identifiers)
            selected_pages.append(len(ids))
            selected_hydrated.update(ids)
            return original_get_metrics(ids)

        query_counter.phase = "target_family_query"
        target_query_started = time.perf_counter()
        with patch.object(compact, "get_metrics", side_effect=track_target_hydration):
            selected = storage.metric_results.list(
                asset_id=target_asset,
                metric_keys=funding_keys,
            )
        target_query_seconds = time.perf_counter() - target_query_started
        sample_spill("after_target_selection")
        expected_target_ids = {item.result_id for item in funding_metrics}
        if {item.result_id for item in selected} != expected_target_ids:
            raise RuntimeError("scale family selector returned an incomplete target set")
        if selected_hydrated != expected_target_ids:
            raise RuntimeError("scale family selector hydrated unrelated asset metrics")
        if any(page_size > 256 for page_size in selected_pages):
            raise RuntimeError("scale target query hydrated a model page larger than 256")

        full_page_sizes: list[int] = []

        def track_full_hydration(identifiers: Collection[UUID]):
            ids = tuple(identifiers)
            full_page_sizes.append(len(ids))
            return original_get_metrics(ids)

        query_counter.phase = "full_paged_query"
        full_query_started = time.perf_counter()
        with patch.object(compact, "get_metrics", side_effect=track_full_hydration):
            all_metrics = storage.metric_results.list()
        full_query_seconds = time.perf_counter() - full_query_started
        if len(all_metrics) != scale_size or not full_page_sizes or max(full_page_sizes) > 256:
            raise RuntimeError("scale list did not preserve full results in bounded pages")
        sample_spill("after_full_paged_read")
        expected_metrics_by_id = {item.result_id: item for item in metrics}
        actual_metrics_by_id = {item.result_id: item for item in all_metrics}
        if actual_metrics_by_id != expected_metrics_by_id:
            raise RuntimeError(
                "scale metric values or Decimal representations changed on round trip"
            )
        expected_result_sha256 = _model_digest(metrics)
        actual_result_sha256 = _model_digest(all_metrics)
        if expected_result_sha256 != actual_result_sha256:
            raise RuntimeError("scale metric result digest differs after round trip")
        query_counter.phase = "logical_size_measurement"
        logical_measurement_started = time.perf_counter()
        logical_table_bytes = _logical_workspace_table_bytes(connection)
        logical_measurement_seconds = time.perf_counter() - logical_measurement_started
        sample_spill("after_logical_size_measurement")
        query_counter.phase = "scale_metadata_counts"
        sequence_count = int(
            connection.execute(
                "SELECT count(*) FROM workspace_analytical_sequences_v2 "
                "WHERE link_type = 'metric-input-observation-v1'"
            ).fetchone()[0]
        )
        funding_sequence_count = int(
            connection.execute(
                "SELECT count(DISTINCT observation_sequence_id) "
                "FROM workspace_metric_results_v2 WHERE metric_key IN (?, ?)",
                list(funding_keys),
            ).fetchone()[0]
        )
        funding_parameter_variant_count = int(
            connection.execute(
                "SELECT count(DISTINCT parameters_content_id) "
                "FROM workspace_metric_results_v2 WHERE metric_key IN (?, ?)",
                list(funding_keys),
            ).fetchone()[0]
        )
        if funding_parameter_variant_count != funding_count:
            raise RuntimeError("funding metric parameter variants were unexpectedly shared")
        segment_count = int(
            connection.execute("SELECT count(*) FROM workspace_analytical_segments_v2").fetchone()[
                0
            ]
        )
        max_segment_size = int(
            connection.execute(
                "SELECT coalesce(max(member_count), 0) FROM workspace_analytical_segments_v2"
            ).fetchone()[0]
        )
        max_hydrated_page_size = max([*selected_pages, *full_page_sizes], default=0)
        if max_segment_size > 256 or max_hydrated_page_size > 256:
            raise RuntimeError("scale batch or hydration page exceeded 256 entries")
        database_path = storage.store.paths.database_path
        database_bytes = database_path.stat().st_size
        wal_path = database_path.with_name(f"{database_path.name}.wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        export_names = (
            "assets",
            "source_definitions",
            "metric_definitions",
            "raw_record_index",
            "normalized_observations",
            "metric_results",
            "diagnostic_results",
        )
        query_counter.phase = "parquet_export"
        parquet_started = time.perf_counter()
        for table_name in export_names:
            storage.parquet.export_table(table_name)
        parquet_seconds = time.perf_counter() - parquet_started
        parquet_file_bytes = sum(
            path.stat().st_size
            for path in storage.paths.exports_dir.glob("*.parquet")
            if path.is_file()
        )
        raw_blob_file_bytes = sum(
            path.stat().st_size for path in storage.paths.raw_dir.rglob("*") if path.is_file()
        )
        workspace_file_bytes = sum(
            path.stat().st_size for path in workspace_root.rglob("*") if path.is_file()
        )
        profile_counts = {
            "assets": len(storage.assets.list_all()),
            "unrelated_assets": len(unrelated_assets),
            "unrelated_metrics": len(unrelated_metrics),
            "funding_metrics": len(funding_metrics),
            "total_metrics": len(all_metrics),
            "funding_metric_share": funding_count / scale_size,
            "raw_records": storage.raw_records.count(),
            "observations": storage.observations.count(),
        }
        sample_spill("before_writer_close")
    connection_patcher.stop()
    elapsed_seconds = time.perf_counter() - started
    return {
        "profile": "workspace_v2_scale",
        "scale_size": scale_size,
        "workspace_format": initialization.manifest.format_version,
        "counts": profile_counts,
        "funding_metric_share": funding_count / scale_size,
        "target_selected_count": len(selected),
        "target_unrelated_hydrated": 0,
        "target_max_hydrated_page": max(selected_pages, default=0),
        "full_list_max_hydrated_page": max(full_page_sizes, default=0),
        "max_hydrated_page_size": max_hydrated_page_size,
        "shared_inputs_per_funding_metric": 720,
        "funding_sequence_count": funding_sequence_count,
        "funding_parameter_variant_count": funding_parameter_variant_count,
        "all_metric_input_sequence_count": sequence_count,
        "sequence_segment_count": segment_count,
        "max_sequence_segment_members": max_segment_size,
        "expected_result_sha256": expected_result_sha256,
        "round_trip_result_sha256": actual_result_sha256,
        "logical_workspace_table_bytes": logical_table_bytes,
        "logical_size_policy": {
            "text_and_json": "UTF-8 payload bytes; blobs use stored bytes",
            "scalars": "fixed width per non-null value",
            "nulls": "zero logical payload bytes",
            "excluded_overhead": (
                "row, tuple, index, and storage-block overhead; reported in physical files"
            ),
        },
        "queries_by_phase": dict(query_counter.counts),
        "query_counter_policy": (
            "counts DuckDB connection execute, executemany, sql, and query calls per phase"
        ),
        "raw_blob_file_bytes": raw_blob_file_bytes,
        "parquet_export_file_bytes": parquet_file_bytes,
        "workspace_file_bytes": workspace_file_bytes,
        "physical_database_bytes": database_bytes,
        "physical_wal_bytes": wal_bytes,
        "spill_temp_directory_bytes_observed_by_phase": spill_samples,
        "spill_temp_directory_bytes_max_observed": max(spill_samples.values(), default=0),
        "phase_durations_seconds": {
            "target_family_query": target_query_seconds,
            "full_paged_query": full_query_seconds,
            "logical_size_measurement": logical_measurement_seconds,
            "parquet_export": parquet_seconds,
        },
        "elapsed_seconds": elapsed_seconds,
        "max_rss_bytes": _max_rss_bytes(),
    }


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _output_path(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not path.is_relative_to(temp_root):
        raise argparse.ArgumentTypeError(
            "smoke output must be inside the system temporary directory"
        )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=_output_path)
    arguments = parser.parse_args()
    output: Path = arguments.output
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix="workspace-v2-smoke-", dir=output.parent
    ) as scratch_name:
        scratch = Path(scratch_name)
        fidelity = _run_fidelity_and_recovery(scratch)
        scale = _run_scale(scratch)
    result = {
        "schema_version": "workspace-v2-backend-smoke-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "code_sha": _git_head(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "duckdb": duckdb.__version__,
        },
        "profiles": {
            "workspace_v2_fidelity": fidelity,
            "workspace_v2_recovery": {
                "interruption_resume": fidelity["interruption_recovered"],
                "double_restore_ready": fidelity["double_restore_ready"],
                "state_checkpoint_preserved": fidelity["checkpoint_bytes_preserved"],
                "tamper_rejected": fidelity["tampered_backup_rejected"],
                "active_writer_excluded": fidelity["active_writer_excluded"],
            },
            "workspace_v2_scale": scale,
        },
        "elapsed_seconds": time.perf_counter() - started,
    }
    payload = (
        json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    )
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(output),
                "code_sha": result["code_sha"],
                "elapsed_seconds": result["elapsed_seconds"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
