"""Focused tests for the exact PIT historical repair transaction."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

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
from investment_analyst.storage.compact_analytical_v2 import (
    CompactAnalyticalError,
    CompactAnalyticalStore,
    HistoricalAnalyticalSeal,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_ASSET = "equity:us:aapl"
_SOURCE_ID = "fixture:pit-repair"


def _source(key: str) -> SourceReference:
    return SourceReference(source_id=_SOURCE_ID, record_key=key, retrieved_at=_BASE)


def _raw(key: str) -> RawRecord:
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"pit-repair-raw-{key}"),
        asset_id=_ASSET,
        source=_source(key),
        event_time=_BASE,
        available_at=_BASE,
        received_at=_BASE,
        payload={"key": key},
        schema_version="pit-repair-v1",
    )


def _observation(raw: RawRecord, *, key: str, available: datetime) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"pit-repair-observation-{key}"),
        raw_record_id=raw.record_id,
        asset_id=_ASSET,
        field_name="close",
        value=Decimal("100.00"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=available,
        available_at=available,
        normalized_at=available,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="pit-repair-v1",
    )


def _metric(observation: NormalizedObservation, *, key: str, available: datetime) -> MetricResult:
    return MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"pit-repair-metric-{key}"),
        asset_id=_ASSET,
        metric_key="market.technical.sma",
        value=Decimal("100.00"),
        unit="USD",
        as_of=available,
        available_at=available,
        computed_at=available,
        parameters={"source_id": _SOURCE_ID},
        input_observation_ids=[observation.observation_id],
        algorithm_version="pit-repair-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic(metric: MetricResult, *, key: str, available: datetime) -> DiagnosticResult:
    return DiagnosticResult(
        diagnostic_id=uuid5(NAMESPACE_URL, f"pit-repair-diagnostic-{key}"),
        asset_id=_ASSET,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("60.00"),
        confidence=Decimal("0.75"),
        as_of=available,
        available_at=available,
        computed_at=available,
        components=[
            DiagnosticComponent(
                component_key="trend",
                score=Decimal("60.00"),
                weight=Decimal("1.00"),
                weighted_contribution=Decimal("60.00"),
                metric_result_ids=[metric.result_id],
                explanation="Fixture component",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.75"),
                reason="Fixture evidence",
            )
        ],
        algorithm_version="pit-repair-v1",
        summary="Fixture diagnostic",
        quality=DataQuality.VALID,
    )


def _subset_digest(compact: CompactAnalyticalStore, kind: str, excluded: set) -> tuple[int, str]:
    digest = hashlib.sha256(b"").hexdigest()
    count = 0
    cursor_at = None
    cursor_id = None
    while True:
        identifiers = (
            compact.list_metric_ids_page(
                limit=256,
                after_available_at=cursor_at,
                after_result_id=cursor_id,
                origin="HISTORICAL",
            )
            if kind == "metric"
            else compact.list_diagnostic_ids_page(
                limit=256,
                after_available_at=cursor_at,
                after_diagnostic_id=cursor_id,
                origin="HISTORICAL",
            )
        )
        if not identifiers:
            return count, digest
        models = (
            compact.get_metrics(identifiers)
            if kind == "metric"
            else compact.get_diagnostics(identifiers)
        )
        for identifier in identifiers:
            if identifier in excluded:
                continue
            checksum = sha256_hex(canonical_json_bytes(models[identifier]))
            digest = hashlib.sha256(f"{digest}:{checksum}".encode("ascii")).hexdigest()
            count += 1
        last = models[identifiers[-1]]
        cursor_at = last.available_at
        cursor_id = last.result_id if kind == "metric" else last.diagnostic_id


def _seed(tmp_path: Path, *, with_survivor: bool = False):
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "v2", format_version=2)
    valid_raw = _raw("valid")
    invalid_raw = _raw("invalid")
    valid_observation = _observation(valid_raw, key="valid", available=_BASE)
    invalid_observation = _observation(
        invalid_raw, key="invalid", available=_BASE + timedelta(days=32)
    )
    valid_metric = _metric(valid_observation, key="valid", available=_BASE + timedelta(days=1))
    invalid_metric = _metric(
        invalid_observation, key="invalid", available=_BASE + timedelta(days=1)
    )
    valid_diagnostic = _diagnostic(valid_metric, key="valid", available=_BASE + timedelta(days=2))
    invalid_diagnostic = _diagnostic(
        invalid_metric, key="invalid", available=_BASE + timedelta(days=2)
    )
    storage = service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE)
    storage.raw_records.save(valid_raw)
    storage.raw_records.save(invalid_raw)
    storage.observations.save(valid_observation)
    storage.observations.save(invalid_observation)
    compact = CompactAnalyticalStore(storage.store.connection)
    compact.save_metrics([valid_metric, invalid_metric], origin="HISTORICAL")
    compact.save_diagnostics([valid_diagnostic, invalid_diagnostic], origin="HISTORICAL")
    if with_survivor:
        survivor_raw = _raw("survivor")
        survivor_observation = _observation(survivor_raw, key="survivor", available=_BASE)
        storage.raw_records.save(survivor_raw)
        storage.observations.save(survivor_observation)
        survivor = _metric(
            survivor_observation, key="survivor", available=_BASE + timedelta(days=3)
        ).model_copy(
            update={
                "result_id": uuid5(NAMESPACE_URL, "pit-repair-survivor"),
                "input_metric_result_ids": [invalid_metric.result_id],
            }
        )
        compact.save_metrics([survivor], origin="HISTORICAL")
    metric_count, metric_digest, diagnostic_count, diagnostic_digest = (
        compact.historical_inventory_digests()
    )
    seal = compact.seal_historical_inventory(
        source_fingerprint="a" * 64,
        metric_count=metric_count,
        metric_digest=metric_digest,
        diagnostic_count=diagnostic_count,
        diagnostic_digest=diagnostic_digest,
        sealed_at=_BASE + timedelta(days=3),
    )
    valid_metric_digest = _subset_digest(compact, "metric", {invalid_metric.result_id})
    valid_diagnostic_digest = _subset_digest(
        compact, "diagnostic", {invalid_diagnostic.diagnostic_id}
    )
    return (
        storage,
        compact,
        seal,
        valid_metric,
        invalid_metric,
        valid_diagnostic,
        invalid_diagnostic,
        valid_metric_digest,
        valid_diagnostic_digest,
    )


def test_repair_removes_exact_invalid_and_publishes_valid_seal(tmp_path: Path) -> None:
    (
        storage,
        compact,
        seal,
        valid_metric,
        invalid_metric,
        valid_diagnostic,
        invalid_diagnostic,
        valid_metric_digest,
        valid_diagnostic_digest,
    ) = _seed(tmp_path)
    before_raw = storage.raw_records.count()
    before_observations = storage.observations.count()
    before_live = compact.count_metrics(origin="LIVE")
    result = compact.repair_historical_inventory(
        metric_ids={invalid_metric.result_id},
        diagnostic_ids={invalid_diagnostic.diagnostic_id},
        before_seal=seal,
        after_metric_count=valid_metric_digest[0],
        after_metric_digest=valid_metric_digest[1],
        after_diagnostic_count=valid_diagnostic_digest[0],
        after_diagnostic_digest=valid_diagnostic_digest[1],
        sealed_at=_BASE + timedelta(days=4),
    )
    assert result.metric_count == 1
    assert result.diagnostic_count == 1
    assert result.metric_digest == valid_metric_digest[1]
    assert result.diagnostic_digest == valid_diagnostic_digest[1]
    assert compact.get_existing_metrics({valid_metric.result_id})
    assert not compact.get_existing_metrics({invalid_metric.result_id})
    assert compact.get_existing_diagnostics({valid_diagnostic.diagnostic_id})
    assert not compact.get_existing_diagnostics({invalid_diagnostic.diagnostic_id})
    assert compact.count_metrics(origin="HISTORICAL") == 1
    assert compact.count_diagnostics(origin="HISTORICAL") == 1
    compact.verify_historical_relationships()
    history = storage.store.connection.execute(
        "SELECT metric_count, diagnostic_count FROM workspace_v2_historical_seal_history"
    ).fetchall()
    assert (2, 2) in [(int(row[0]), int(row[1])) for row in history]
    assert storage.raw_records.count() == before_raw
    assert storage.observations.count() == before_observations
    assert compact.count_metrics(origin="LIVE") == before_live
    storage.close()


def test_repair_rejects_survivor_and_rolls_back(tmp_path: Path) -> None:
    (
        storage,
        compact,
        seal,
        _,
        invalid_metric,
        _,
        invalid_diagnostic,
        valid_metric_digest,
        valid_diagnostic_digest,
    ) = _seed(tmp_path, with_survivor=True)
    with pytest.raises(CompactAnalyticalError, match="surviving reference"):
        compact.repair_historical_inventory(
            metric_ids={invalid_metric.result_id},
            diagnostic_ids={invalid_diagnostic.diagnostic_id},
            before_seal=seal,
            after_metric_count=valid_metric_digest[0],
            after_metric_digest=valid_metric_digest[1],
            after_diagnostic_count=valid_diagnostic_digest[0],
            after_diagnostic_digest=valid_diagnostic_digest[1],
            sealed_at=_BASE + timedelta(days=4),
        )
    assert compact.get_existing_metrics({invalid_metric.result_id})
    assert compact.get_existing_diagnostics({invalid_diagnostic.diagnostic_id})
    assert compact.historical_seal() == seal
    storage.close()


def test_repair_rejects_wrong_seal_or_projection(tmp_path: Path) -> None:
    (
        storage,
        compact,
        seal,
        _,
        invalid_metric,
        _,
        invalid_diagnostic,
        valid_metric_digest,
        valid_diagnostic_digest,
    ) = _seed(tmp_path)
    with pytest.raises(CompactAnalyticalError, match="pre-seal"):
        compact.repair_historical_inventory(
            metric_ids={invalid_metric.result_id},
            diagnostic_ids={invalid_diagnostic.diagnostic_id},
            before_seal=HistoricalAnalyticalSeal(
                source_fingerprint="b" * 64,
                metric_count=2,
                metric_digest=seal.metric_digest,
                diagnostic_count=2,
                diagnostic_digest=seal.diagnostic_digest,
                sealed_at=seal.sealed_at,
            ),
            after_metric_count=valid_metric_digest[0],
            after_metric_digest=valid_metric_digest[1],
            after_diagnostic_count=valid_diagnostic_digest[0],
            after_diagnostic_digest=valid_diagnostic_digest[1],
            sealed_at=_BASE + timedelta(days=4),
        )
    with pytest.raises(CompactAnalyticalError, match="post-inventory"):
        compact.repair_historical_inventory(
            metric_ids={invalid_metric.result_id},
            diagnostic_ids={invalid_diagnostic.diagnostic_id},
            before_seal=seal,
            after_metric_count=2,
            after_metric_digest=valid_metric_digest[1],
            after_diagnostic_count=valid_diagnostic_digest[0],
            after_diagnostic_digest=valid_diagnostic_digest[1],
            sealed_at=_BASE + timedelta(days=4),
        )
    assert compact.get_existing_metrics({invalid_metric.result_id})
    assert compact.historical_seal() == seal
    storage.close()
