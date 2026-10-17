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
from investment_analyst.storage.resource_policy import DuckDBResourcePolicy
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.mark.parametrize("parents,members", [(32, 32), (128, 256), (512, 512)])
def test_shared_reference_metadata_scales_with_segments_not_parent_fanout(
    tmp_path: Path,
    parents: int,
    members: int,
) -> None:
    root = tmp_path / "workspace"
    policy = DuckDBResourcePolicy(memory_limit_bytes=256_000_000, threads=1, spill_limit_bytes=0)
    service = WorkspaceService(
        environ={},
        home=tmp_path / "home",
        resource_policies={root / "storage/v2/index.duckdb": policy},
    )
    initialized = service.initialize(root, format_version=2)
    pairs = [_observation(index) for index in range(members)]
    observations = [observation for _, observation in pairs]
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save_many([raw for raw, _ in pairs])
        storage.observations.save_many(observations)
        for start in range(0, parents, 128):
            storage.metric_results.save_many(
                [
                    _metric(
                        observations, key="fixture.shared", identity=f"shared-{index}", v8=False
                    )
                    for index in range(start, min(start + 128, parents))
                ]
            )
        compact = CompactAnalyticalStore(storage.store.connection)
        compact._build_relationship_metadata("workspace_metric_results_v2")
        connection = storage.store.connection
        assert connection.execute(
            "SELECT count(*) FROM _workspace_v2_reference_segments"
        ).fetchone() == ((members + 255) // 256,)
        row = connection.execute(
            "SELECT member_count,actual_members,covered_members "
            "FROM _workspace_v2_reference_sequences WHERE link_type='metric-input-observation-v1'"
        ).fetchone()
        assert row == (members, members, members)
        compact.verify_historical_relationships()
        assert not connection.execute(
            "SELECT table_name FROM duckdb_tables() WHERE temporary"
        ).fetchall()


@pytest.mark.parametrize(
    "fault",
    [
        "missing_child",
        "foreign_child",
        "future_child",
        "missing_sequence",
        "missing_segment",
        "missing_member",
        "member_order",
        "link_order",
        "wrong_type",
        "diagnostic_component",
        "diagnostic_evidence",
    ],
)
def test_shared_reference_verifier_rejects_full_coverage_and_pit_faults(
    tmp_path: Path,
    fault: str,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    pairs = [_observation(index) for index in range(3)]
    metric = _metric(
        [observation for _, observation in pairs],
        key="market.funding_rate",
        identity="shared-fault",
        v8=False,
    )
    diagnostic = _diagnostic(metric, identity="shared-diagnostic")
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save_many([raw for raw, _ in pairs])
        storage.observations.save_many([observation for _, observation in pairs])
        storage.metric_results.save(metric)
        storage.diagnostics.save(diagnostic)
        connection = storage.store.connection
        mutations = {
            "missing_child": "DELETE FROM normalized_observations_v2 WHERE observation_id=?",
            "foreign_child": "UPDATE normalized_observations_v2 "
            "SET asset_id='equity:us:aapl' WHERE observation_id=?",
            "future_child": "UPDATE normalized_observations_v2 "
            "SET available_at='2099-01-01T00:00:00Z' WHERE observation_id=?",
            "missing_sequence": "DELETE FROM workspace_analytical_sequences_v2 "
            "WHERE link_type='metric-input-observation-v1'",
            "missing_segment": "DELETE FROM workspace_analytical_segments_v2 "
            "WHERE link_type='metric-input-observation-v1'",
            "missing_member": "DELETE FROM workspace_analytical_segment_members_v2 "
            "WHERE member_id=?",
            "member_order": "UPDATE workspace_analytical_segment_members_v2 "
            "SET member_position=10 WHERE member_id=?",
            "link_order": "UPDATE workspace_analytical_sequence_segments_v2 "
            "SET segment_position=10",
            "wrong_type": "UPDATE workspace_analytical_sequences_v2 SET link_type='unknown' "
            "WHERE link_type='metric-input-observation-v1'",
            "diagnostic_component": "UPDATE workspace_analytical_components_v2 "
            "SET metric_sequence_id='missing'",
            "diagnostic_evidence": "UPDATE workspace_analytical_evidence_v2 "
            "SET metric_result_id='00000000-0000-0000-0000-000000000000'",
        }
        sql = mutations[fault]
        connection.execute(sql, [pairs[0][1].observation_id] if "?" in sql else None)
        with pytest.raises(CompactAnalyticalError, match="missing, foreign or future"):
            CompactAnalyticalStore(connection).verify_historical_relationships()


@pytest.mark.parametrize("cycle", [False, True])
def test_compact_typed_cycle_graph_preserves_shared_paths_without_false_cycles(
    tmp_path: Path,
    cycle: bool,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    raw, observation = _observation(0)
    metrics = [
        _metric([observation], key="fixture.graph", identity=f"graph-{i}", v8=False)
        for i in range(4)
    ]
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save_many(metrics)
        compact = CompactAnalyticalStore(storage.store.connection)
        # 0 and 1 share exactly the same sequence/segment; sharing is not a cycle.
        adjacency = [[2, 3], [2, 3], [1] if cycle else [], []]
        sequences = compact._ensure_sequences_batch(
            [
                ("metric-input-result-v1", tuple(metrics[child].result_id for child in children))
                for children in adjacency
            ]
        )
        for metric, sequence in zip(metrics, sequences, strict=True):
            storage.store.connection.execute(
                "UPDATE workspace_metric_results_v2 SET metric_sequence_id=? WHERE result_id=?",
                [sequence, metric.result_id],
            )
        profile = compact.profile_historical_relationships()
        assert profile["metric_equal_edges"] == (3 if cycle else 2)
        assert profile["sequence_equal_edges"] == (2 if cycle else 1)
        assert profile["segment_equal_edges"] == (3 if cycle else 2)
        if cycle:
            with pytest.raises(CompactAnalyticalError, match="contains a cycle"):
                compact.verify_historical_relationships()
        else:
            compact.verify_historical_relationships()


def test_cycle_scan_retains_only_equal_time_metric_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    pairs = [_observation(index) for index in range(3)]
    metrics = [
        _metric([observation], key="fixture.partition", identity=str(index), v8=False)
        for index, (_, observation) in enumerate(pairs)
    ]
    # Earlier-time dependencies are valid but cannot be part of a cycle.
    metrics[1] = metrics[1].model_copy(update={"input_metric_result_ids": [metrics[0].result_id]})
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save_many([raw for raw, _ in pairs])
        storage.observations.save_many([observation for _, observation in pairs])
        storage.metric_results.save_many(metrics)
        compact = CompactAnalyticalStore(storage.store.connection)
        metric_edges: list[int] = []
        original = CompactAnalyticalStore._relationship_sql

        def visit(instance: CompactAnalyticalStore, sql: str, parameters=None):
            result = original(instance, sql, parameters)
            if sql.startswith("CREATE OR REPLACE TEMP TABLE _workspace_v2_cycle_edges AS"):
                metric_edges.append(
                    int(
                        instance._connection.execute(
                            "SELECT count(*) FROM _workspace_v2_cycle_edges "
                            "WHERE starts_with(parent_id,'m:')"
                        ).fetchone()[0]
                    )
                )
            return result

        monkeypatch.setattr(CompactAnalyticalStore, "_relationship_sql", visit)
        compact._require_acyclic_inventory()
        assert metric_edges == []
        peer = metrics[2].model_copy(
            update={"result_id": uuid4(), "input_metric_result_ids": [metrics[2].result_id]}
        )
        storage.metric_results.save(peer)
        compact._require_acyclic_inventory()
        assert metric_edges == [1]


@pytest.mark.parametrize("cycle_in_last_bucket", [False, True])
def test_read_only_units_preserve_all_buckets_and_reject_late_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cycle_in_last_bucket: bool,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    raw, observation = _observation(0)
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        connection = storage.store.connection
        moments = {}
        for hour in range(1, 17):
            moment = _BASE + timedelta(hours=hour)
            bucket = int(
                connection.execute("SELECT hash(?::TIMESTAMPTZ)%16", [moment]).fetchone()[0]
            )
            moments[bucket] = moment
        low, high = min(moments), max(moments)
        assert low < high
        metrics = []
        for index, bucket in enumerate((low, low, high, high)):
            moment = moments[bucket]
            metrics.append(
                _metric(
                    [observation], key="fixture.bucket", identity=f"bucket-{index}", v8=False
                ).model_copy(
                    update={"available_at": moment, "computed_at": moment + timedelta(minutes=1)}
                )
            )
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save_many(metrics)
        compact = CompactAnalyticalStore(connection)
        adjacency = [[], [0], [3] if cycle_in_last_bucket else [], [2]]
        sequences = compact._ensure_sequences_batch(
            [
                ("metric-input-result-v1", tuple(metrics[i].result_id for i in children))
                for children in adjacency
            ]
        )
        for metric, sequence in zip(metrics, sequences, strict=True):
            connection.execute(
                "UPDATE workspace_metric_results_v2 SET metric_sequence_id=? WHERE result_id=?",
                [sequence, metric.result_id],
            )
        with pytest.raises(CompactAnalyticalError, match="physical read-only"):
            compact.verify_historical_relationships(read_only_units=True)
    visited: list[int] = []
    original = CompactAnalyticalStore._require_acyclic_bucket

    def visit(instance: CompactAnalyticalStore, bucket: int) -> None:
        visited.append(bucket)
        original(instance, bucket)

    monkeypatch.setattr(CompactAnalyticalStore, "_require_acyclic_bucket", visit)
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_ONLY) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        if cycle_in_last_bucket:
            with pytest.raises(CompactAnalyticalError, match="contains a cycle"):
                compact.verify_historical_relationships(read_only_units=True)
        else:
            compact.verify_historical_relationships(read_only_units=True)
            assert storage.store.connection.execute(
                "SELECT count(*) FROM workspace_metric_results_v2"
            ).fetchone() == (4,)
        assert visited == [low, high]


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


def test_compact_metric_id_selection_filters_parameters_cut_and_range(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    raw, observation = _observation(0)
    known_at = _BASE + timedelta(days=1)

    def result(
        *,
        identity: str,
        source_id: str,
        version_two: bool,
        legacy_cut: datetime = _BASE,
    ) -> MetricResult:
        item = MetricResult(
            result_id=uuid4(),
            asset_id="crypto:btc-usd",
            metric_key="market.history.sma",
            value=Decimal("10"),
            unit="USD",
            as_of=_BASE,
            available_at=_BASE,
            computed_at=known_at,
            parameters={
                "source_id": source_id,
                "window": 2,
                **({} if version_two else {"known_at": legacy_cut.isoformat()}),
            },
            input_observation_ids=[observation.observation_id],
            algorithm_version="market-sma-v1-decimal34",
            quality=DataQuality.VALID,
        )
        return item.model_copy(
            update={
                "result_id": (
                    metric_result_id_from_model_v2(item)
                    if version_two
                    else uuid5(NAMESPACE_URL, identity)
                )
            }
        )

    matching_v2 = result(identity="matching-v2", source_id="fixture:market", version_two=True)
    wrong_source_v2 = result(identity="wrong-source", source_id="fixture:other", version_two=True)
    matching_v1 = result(
        identity="matching-v1",
        source_id="fixture:market",
        version_two=False,
        legacy_cut=known_at,
    )
    other_cut_v1 = result(identity="other-cut-v1", source_id="fixture:market", version_two=False)

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save_many([matching_v2, wrong_source_v2, matching_v1, other_cut_v1])

        selected = storage.metric_results.list_ids(
            asset_id="crypto:btc-usd",
            metric_keys=("market.history.sma",),
            available_to=known_at,
            as_of_from=_BASE - timedelta(days=1),
            as_of_before=known_at,
            parameter_equals={"source_id": "fixture:market"},
            cut_known_at=known_at,
        )

    assert set(selected) == {matching_v2.result_id, matching_v1.result_id}


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
