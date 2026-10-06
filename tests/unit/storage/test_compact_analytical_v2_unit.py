"""Unit coverage for compact analytical persistence and historical immutability."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
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
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _observation(index: int) -> tuple[RawRecord, NormalizedObservation]:
    moment = _BASE + timedelta(minutes=index)
    source = SourceReference(
        source_id="fixture:market",
        record_key=f"observation-{index}",
        retrieved_at=moment,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"compact-v2-raw-{index}"),
        asset_id="crypto:btc-usd",
        source=source,
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"value": index},
        schema_version="compact-v2-unit-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"compact-v2-observation-{index}"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="funding_rate",
        value=Decimal(f"{index}.00100"),
        unit="rate",
        frequency=DataFrequency.HOUR_1,
        observed_at=moment,
        available_at=moment,
        normalized_at=moment,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    return raw, observation


def _metric(
    observations: list[NormalizedObservation],
    *,
    key: str,
    identity: str,
    value: Decimal = Decimal("-0.0012300"),
    v8: bool = True,
) -> MetricResult:
    available_at = max(item.available_at for item in observations)
    candidate = MetricResult(
        result_id=uuid4(),
        asset_id="crypto:btc-usd",
        metric_key=key,
        value=value,
        unit="rate",
        as_of=available_at,
        available_at=available_at,
        computed_at=available_at + timedelta(minutes=1),
        parameters={"known_at": "historical-run", "scale": {"decimal": "-0.0012300"}},
        input_observation_ids=[item.observation_id for item in observations],
        algorithm_version="compact-v2-unit-v1",
        quality=DataQuality.VALID,
    )
    if v8:
        return candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})
    return candidate.model_copy(update={"result_id": uuid5(NAMESPACE_URL, identity)})


def _diagnostic(metric: MetricResult, *, identity: str) -> DiagnosticResult:
    component = DiagnosticComponent(
        component_key="funding",
        score=Decimal("60.00"),
        weight=Decimal("1.00"),
        weighted_contribution=Decimal("60.00"),
        metric_result_ids=[metric.result_id],
        explanation="Compact analytical test component.",
    )
    evidence = DiagnosticEvidence(
        metric_result_id=metric.result_id,
        direction=EvidenceDirection.SUPPORTS,
        contribution=Decimal("0.75"),
        reason="Compact analytical test evidence.",
    )
    return DiagnosticResult(
        diagnostic_id=uuid5(NAMESPACE_URL, identity),
        asset_id=metric.asset_id,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("60.00"),
        confidence=Decimal("0.75"),
        as_of=metric.as_of,
        available_at=metric.available_at,
        computed_at=metric.computed_at,
        components=[component],
        evidence=[evidence],
        algorithm_version="compact-v2-unit-v1",
        summary="Compact analytical test result.",
        quality=DataQuality.VALID,
    )


def test_compact_metric_diagnostic_round_trip_and_segment_bounds(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    pairs = [_observation(index) for index in range(600)]
    observations = [observation for _, observation in pairs]
    metric = _metric(
        observations,
        key="crypto.derivatives.funding.sum_1h",
        identity="live-sum",
    )
    second_metric = _metric(
        observations,
        key="crypto.derivatives.funding.mean_1h",
        identity="live-mean",
        value=Decimal("-0.000056700"),
    )
    diagnostic = _diagnostic(metric, identity="live-diagnostic")

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save_many([raw for raw, _ in pairs])
        storage.observations.save_many(observations)
        receipt = storage.metric_results.save_many([metric, second_metric])
        assert receipt.created_count == 2
        assert storage.metric_results.save_many([metric]).reused_count == 1
        storage.diagnostics.save(diagnostic)

        assert storage.metric_results.get(metric.result_id) == metric
        assert str(storage.metric_results.get(metric.result_id).value) == "-0.0012300"
        assert storage.metric_results.get(second_metric.result_id) == second_metric
        assert storage.diagnostics.get(diagnostic.diagnostic_id) == diagnostic
        assert storage.metric_results.count(asset_id="crypto:btc-usd") == 2
        assert (
            len(storage.metric_results.list_ids(metric_keys=("crypto.derivatives.funding.sum_1h",)))
            == 1
        )

        connection = storage.store.connection
        segment_rows = connection.execute(
            "SELECT member_count FROM workspace_analytical_segments_v2"
        ).fetchall()
        assert segment_rows
        assert max(int(row[0]) for row in segment_rows) <= 256
        content_count = int(
            connection.execute(
                "SELECT count(*) FROM workspace_analytical_content_v2 "
                "WHERE content_kind = 'parameters'"
            ).fetchone()[0]
        )
        assert content_count == 1
        sequence_count = int(
            connection.execute("SELECT count(*) FROM workspace_analytical_sequences_v2").fetchone()[
                0
            ]
        )
        assert sequence_count == 3


def test_historical_seal_rejects_new_history_but_accepts_live_rows(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    raw, observation = _observation(0)
    history_metric = _metric(
        [observation],
        key="crypto.derivatives.funding.sum_1h",
        identity="historic-metric-v5",
        v8=False,
    )
    history_diagnostic = _diagnostic(history_metric, identity="historic-diagnostic")

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        compact = CompactAnalyticalStore(storage.store.connection)
        compact.save_metrics([history_metric], origin="HISTORICAL")
        compact.save_diagnostics([history_diagnostic], origin="HISTORICAL")
        metric_count, metric_digest, diagnostic_count, diagnostic_digest = (
            compact.historical_inventory_digests()
        )
        fingerprint = hashlib.sha256(b"source historical inventory").hexdigest()
        seal = compact.seal_historical_inventory(
            source_fingerprint=fingerprint,
            metric_count=metric_count,
            metric_digest=metric_digest,
            diagnostic_count=diagnostic_count,
            diagnostic_digest=diagnostic_digest,
            sealed_at=_BASE + timedelta(days=1),
        )
        assert compact.verify_historical_seal() == seal
        with pytest.raises(CompactAnalyticalError, match="sealed historical"):
            compact.save_metrics(
                [
                    _metric(
                        [observation],
                        key="crypto.derivatives.funding.mean_1h",
                        identity="late-historical-metric-v5",
                        v8=False,
                    )
                ],
                origin="HISTORICAL",
            )

        live_metric = _metric(
            [observation],
            key="crypto.derivatives.funding.mean_1h",
            identity="live-after-seal",
        )
        storage.metric_results.save(live_metric)
        assert compact.verify_historical_seal() == seal
        assert storage.metric_results.get(live_metric.result_id) == live_metric


def test_compact_content_hash_corruption_fails_closed(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    raw, observation = _observation(0)
    metric = _metric(
        [observation],
        key="crypto.derivatives.funding.sum_1h",
        identity="tamper-test",
    )
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save(metric)
        row = storage.store.connection.execute(
            "SELECT content_id FROM workspace_analytical_content_v2 "
            "WHERE content_kind = 'parameters'"
        ).fetchone()
        assert row is not None
        storage.store.connection.execute(
            "UPDATE workspace_analytical_content_v2 SET value_bytes = ? WHERE content_id = ?",
            [b"tampered", row[0]],
        )
        with pytest.raises(CompactAnalyticalError):
            storage.metric_results.get(metric.result_id)
