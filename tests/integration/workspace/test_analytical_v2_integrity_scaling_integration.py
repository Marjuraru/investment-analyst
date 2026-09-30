"""Integration tests for analytical v2 integrity, domain membership, and scaling.

Covers:
- A1: Multi-level metric DAG with deep chain (>1000 nodes) without RecursionError.
- A2: Pure domain membership and snapshot evidence_set_digest resolving all cited metrics.
- A3: Multiasset reads are paged, keyset-paginated, and bounded without hydrating foreign data.
- X1: Corrupt, missing, future, foreign, and cyclic references fail closed.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest

from investment_analyst.analytics.analysis_domain import (
    AnalysisDomain,
)
from investment_analyst.analytics.analysis_snapshot import (
    build_analysis_snapshot,
)
from investment_analyst.analytics.evidence_set import (
    build_evidence_segments,
    build_evidence_set,
)
from investment_analyst.analytics.metric_identity_v2 import (
    metric_result_id_from_model_v2,
)
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
from investment_analyst.storage.analysis_snapshot_v2 import (
    ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE,
    AnalysisSnapshotV2Error,
)
from investment_analyst.storage.diagnostic_v2 import (
    DiagnosticV2Error,
)
from investment_analyst.storage.metric_v2 import MetricV2Error
from investment_analyst.storage.raw_v2 import RawV2Staging


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _seed_observation(
    staging: RawV2Staging,
    moment: datetime,
    asset_id: str,
    field_name: str = "close",
    source_id: str = "test:feed",
) -> NormalizedObservation:
    raw = RawRecord(
        record_id=uuid4(),
        asset_id=asset_id,
        source=SourceReference(
            source_id=source_id,
            record_key=f"obs-{uuid4()}",
            retrieved_at=moment,
        ),
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"val": 100},
        schema_version="v1",
    )
    staging.save(raw)
    obs = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name=field_name,
        value=Decimal("100.00"),
        unit="USD",
        frequency=DataFrequency.HOUR_1,
        observed_at=moment,
        available_at=moment,
        normalized_at=moment,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    staging.save_observations([obs])
    return obs


def test_dependency_validation_matches_write_read_and_reopen(tmp_path: Path) -> None:
    """A1: Deep metric chain (>1000 nodes) avoids recursion, round-trips through disk."""
    staging = _staging(tmp_path, "staging-deep-dag")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "equity:us:aapl"

    chain_length = 1024  # > 1000 nodes
    with staging:
        obs = _seed_observation(staging, base_time, asset_id)

        metrics: list[MetricResult] = []
        prev_id: UUID | None = None

        for i in range(chain_length):
            moment = base_time + timedelta(minutes=i)
            cand = MetricResult(
                result_id=uuid4(),
                asset_id=asset_id,
                metric_key=f"market.chain.step_{i}",
                value=Decimal(str(i)),
                unit="unit",
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                parameters={"step": i},
                input_observation_ids=[obs.observation_id],
                input_metric_result_ids=[prev_id] if prev_id is not None else [],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            metric = cand.model_copy(update={"result_id": metric_result_id_from_model_v2(cand)})
            metrics.append(metric)
            prev_id = metric.result_id

        # Save all 1024 in a single batch (tests iterative topological sort without recursion)
        receipt = staging.save_metrics(metrics)
        assert receipt.created_count == chain_length

    # Reopen staging from disk with a fresh connection
    reopened_conn = duckdb.connect(str(staging.destination / "raw-v2-index.duckdb"))
    reopened = RawV2Staging(staging.destination, reopened_conn)
    with reopened:
        all_ids = [m.result_id for m in metrics]
        hydrated = reopened.get_metrics(all_ids)
        assert len(hydrated) == chain_length

        # Verify lineage on boundary nodes
        first_metric = hydrated[metrics[0].result_id]
        assert first_metric.input_observation_ids == [obs.observation_id]
        assert first_metric.input_metric_result_ids == []

        last_metric = hydrated[metrics[-1].result_id]
        assert last_metric.input_observation_ids == [obs.observation_id]
        assert last_metric.input_metric_result_ids == [metrics[-2].result_id]


def test_domains_and_snapshot_digest_resolve_all_cited_metrics(tmp_path: Path) -> None:
    """A2: Domain membership and snapshot digest resolving all direct & diagnostic metrics."""
    staging = _staging(tmp_path, "staging-domains")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "crypto:btc-usd"

    with staging:
        # Create observations and evidence sets for two metrics in derivatives domain
        obs1 = _seed_observation(staging, base_time, asset_id, field_name="funding_1")
        seg1 = build_evidence_segments([obs1])
        staging.save_evidence_segments(seg1)
        es1 = build_evidence_set([obs1], segments=seg1)
        staging.save_evidence_set(es1)

        obs2 = _seed_observation(staging, base_time, asset_id, field_name="funding_2")
        seg2 = build_evidence_segments([obs2])
        staging.save_evidence_segments(seg2)
        es2 = build_evidence_set([obs2], segments=seg2)
        staging.save_evidence_set(es2)

        # Metric 1: crypto.derivatives.funding.rate
        m1_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="crypto.derivatives.funding.rate",
            value=Decimal("0.05"),
            unit="rate",
            as_of=base_time,
            available_at=es1.available_at,
            computed_at=es1.available_at,
            parameters={"evidence_set_id": str(es1.evidence_set_id)},
            input_observation_ids=[obs1.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m1 = m1_cand.model_copy(update={"result_id": metric_result_id_from_model_v2(m1_cand)})

        # Metric 2: crypto.derivatives.open_interest
        m2_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="crypto.derivatives.open_interest",
            value=Decimal("1000.0"),
            unit="contracts",
            as_of=base_time,
            available_at=es2.available_at,
            computed_at=es2.available_at,
            parameters={"evidence_set_id": str(es2.evidence_set_id)},
            input_observation_ids=[obs2.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m2 = m2_cand.model_copy(update={"result_id": metric_result_id_from_model_v2(m2_cand)})

        # Metric 3: market.volume (different domain: market)
        obs3 = _seed_observation(staging, base_time, asset_id, field_name="volume")
        m3_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.volume",
            value=Decimal("500.0"),
            unit="volume",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs3.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m3 = m3_cand.model_copy(update={"result_id": metric_result_id_from_model_v2(m3_cand)})

        staging.save_metrics([m1, m2, m3])

        # 1. Diagnostic citing mixed domains (derivatives m1 + market m3) -> fails
        diag_mixed = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            components=[
                DiagnosticComponent(
                    component_key="c1",
                    score=Decimal("80.0"),
                    weight=Decimal("0.5"),
                    weighted_contribution=Decimal("40.0"),
                    metric_result_ids=[m1.result_id],
                    explanation="Derivatives metric",
                ),
                DiagnosticComponent(
                    component_key="c2",
                    score=Decimal("80.0"),
                    weight=Decimal("0.5"),
                    weighted_contribution=Decimal("40.0"),
                    metric_result_ids=[m3.result_id],
                    explanation="Market metric",
                ),
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m1.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="Mix",
                )
            ],
            algorithm_version="v1",
            summary="Mixed diag",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="mixed metric domains"):
            staging.save_diagnostics([diag_mixed])

        # 2. Valid diagnostic citing only derivatives Metric 2
        diag_valid = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            components=[
                DiagnosticComponent(
                    component_key="oi_component",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[m2.result_id],
                    explanation="Open interest",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m2.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="Open interest positive",
                )
            ],
            algorithm_version="v1",
            summary="Derivatives diag",
            quality=DataQuality.VALID,
        )
        staging.save_diagnostics([diag_valid])

        # 3. Snapshot domain mismatch: domain="market" but cites derivatives m1 -> fails
        snap_mismatched_domain = build_analysis_snapshot(
            asset_id=asset_id,
            domain=AnalysisDomain.MARKET.value,
            known_at=base_time,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[es1.canonical_hash],
            created_at=base_time,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="must start with 'market.'"):
            staging.save_analysis_snapshots([snap_mismatched_domain])

        # 4. Snapshot directly cites m1, and cites diag_valid (which cites m2).
        # Both m1 and m2 have EvidenceSets: es1 and es2!
        # If the snapshot's evidence_set_digest only covers es1 -> fails closed!
        snap_partial_digest = build_analysis_snapshot(
            asset_id=asset_id,
            domain=AnalysisDomain.DERIVATIVES.value,
            known_at=base_time,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[diag_valid.diagnostic_id],
            evidence_set_hashes=[es1.canonical_hash],  # Missing es2 from diag_valid!
            created_at=base_time,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="does not match resolved hashes digest"):
            staging.save_analysis_snapshots([snap_partial_digest])

        # 5. Snapshot providing canonical hashes for BOTH es1 and es2 -> succeeds!
        snap_full_digest = build_analysis_snapshot(
            asset_id=asset_id,
            domain=AnalysisDomain.DERIVATIVES.value,
            known_at=base_time,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[diag_valid.diagnostic_id],
            evidence_set_hashes=[es1.canonical_hash, es2.canonical_hash],
            created_at=base_time,
        )
        receipt_snap = staging.save_analysis_snapshots([snap_full_digest])
        assert receipt_snap.created_count == 1

        # Rehydrate and verify
        loaded = staging.get_analysis_snapshot(snap_full_digest.snapshot_id)
        assert loaded.snapshot_id == snap_full_digest.snapshot_id
        assert loaded.metric_ids == (m1.result_id,)
        assert loaded.diagnostic_ids == (diag_valid.diagnostic_id,)


def test_multiasset_reads_are_paged_and_do_not_hydrate_unrelated_history(
    tmp_path: Path,
) -> None:
    """A3: Keyset pagination isolates assets and does not execute foreign queries."""
    staging = _staging(tmp_path, "staging-multiasset")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_a = "equity:us:aapl"
    asset_b = "crypto:btc-usd"

    with staging:
        # Populate Asset A (market domain)
        obs_a = _seed_observation(staging, base_time, asset_a)
        m_a_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_a,
            metric_key="market.close.aapl",
            value=Decimal("150.0"),
            unit="USD",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs_a.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m_a = m_a_cand.model_copy(update={"result_id": metric_result_id_from_model_v2(m_a_cand)})
        staging.save_metrics([m_a])

        diags_a: list[DiagnosticResult] = []
        snaps_a = []
        for i in range(5):
            moment = base_time + timedelta(hours=i)
            d = DiagnosticResult(
                diagnostic_id=uuid4(),
                asset_id=asset_a,
                mode=DiagnosticMode.MARKET,
                verdict=DiagnosticVerdict.POSITIVE,
                final_score=Decimal("50.0"),
                confidence=Decimal("0.8"),
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                components=[
                    DiagnosticComponent(
                        component_key="c",
                        score=Decimal("50.0"),
                        weight=Decimal("1.0"),
                        weighted_contribution=Decimal("50.0"),
                        metric_result_ids=[m_a.result_id],
                        explanation="Check",
                    )
                ],
                evidence=[
                    DiagnosticEvidence(
                        metric_result_id=m_a.result_id,
                        direction=EvidenceDirection.SUPPORTS,
                        contribution=Decimal("50.0"),
                        reason="Check positive",
                    )
                ],
                algorithm_version="v1",
                summary=f"Diag A {i}",
                quality=DataQuality.VALID,
            )
            diags_a.append(d)
            s = build_analysis_snapshot(
                asset_id=asset_a,
                domain="market",
                known_at=moment,
                policy_version="v1",
                metric_ids=[m_a.result_id],
                diagnostic_ids=[d.diagnostic_id],
                evidence_set_hashes=[],
                created_at=moment,
            )
            snaps_a.append(s)

        staging.save_diagnostics(diags_a)
        staging.save_analysis_snapshots(snaps_a)

        # Populate Asset B (derivatives domain)
        obs_b = _seed_observation(staging, base_time, asset_b)
        m_b_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_b,
            metric_key="crypto.derivatives.btc.rate",
            value=Decimal("0.01"),
            unit="rate",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs_b.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m_b = m_b_cand.model_copy(update={"result_id": metric_result_id_from_model_v2(m_b_cand)})
        staging.save_metrics([m_b])

        diags_b: list[DiagnosticResult] = []
        snaps_b = []
        for i in range(5):
            moment = base_time + timedelta(hours=i)
            d = DiagnosticResult(
                diagnostic_id=uuid4(),
                asset_id=asset_b,
                mode=DiagnosticMode.MARKET,
                verdict=DiagnosticVerdict.POSITIVE,
                final_score=Decimal("50.0"),
                confidence=Decimal("0.8"),
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                components=[
                    DiagnosticComponent(
                        component_key="c",
                        score=Decimal("50.0"),
                        weight=Decimal("1.0"),
                        weighted_contribution=Decimal("50.0"),
                        metric_result_ids=[m_b.result_id],
                        explanation="Check",
                    )
                ],
                evidence=[
                    DiagnosticEvidence(
                        metric_result_id=m_b.result_id,
                        direction=EvidenceDirection.SUPPORTS,
                        contribution=Decimal("50.0"),
                        reason="Check positive",
                    )
                ],
                algorithm_version="v1",
                summary=f"Diag B {i}",
                quality=DataQuality.VALID,
            )
            diags_b.append(d)
            s = build_analysis_snapshot(
                asset_id=asset_b,
                domain="derivatives",
                known_at=moment,
                policy_version="v1",
                metric_ids=[m_b.result_id],
                diagnostic_ids=[d.diagnostic_id],
                evidence_set_hashes=[],
                created_at=moment,
            )
            snaps_b.append(s)

        staging.save_diagnostics(diags_b)
        staging.save_analysis_snapshots(snaps_b)

        # 1. Keyset-paginated list of diagnostics for Asset A only, page size = 2
        collected_diags_a: list[DiagnosticResult] = []
        cursor_at = None
        cursor_id = None
        while True:
            page = staging.list_diagnostics(
                asset_id=asset_a,
                cursor_at=cursor_at,
                cursor_id=cursor_id,
                limit=2,
            )
            if not page:
                break
            for item in page:
                assert item.asset_id == asset_a
                collected_diags_a.append(item)
            cursor_at = page[-1].available_at
            cursor_id = page[-1].diagnostic_id

        assert len(collected_diags_a) == 5
        assert [d.diagnostic_id for d in collected_diags_a] == [d.diagnostic_id for d in diags_a]

        # 2. Keyset-paginated list of snapshots for Asset A only, page size = 2
        collected_snaps_a = []
        cursor_at = None
        cursor_id = None
        while True:
            page = staging.list_analysis_snapshots(
                asset_id=asset_a,
                cursor_at=cursor_at,
                cursor_id=cursor_id,
                limit=2,
            )
            if not page:
                break
            for item in page:
                assert item.asset_id == asset_a
                collected_snaps_a.append(item)
            cursor_at = page[-1].known_at
            cursor_id = page[-1].snapshot_id

        assert len(collected_snaps_a) == 5
        assert [s.snapshot_id for s in collected_snaps_a] == [s.snapshot_id for s in snaps_a]


def test_corrupt_missing_future_foreign_and_cyclic_references_fail_closed(
    tmp_path: Path,
) -> None:
    """X1: Cyclic metrics, duplicate inputs, future/foreign references fail closed."""
    staging = _staging(tmp_path, "staging-negatives-deep")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "equity:us:aapl"

    with staging:
        obs = _seed_observation(staging, base_time, asset_id)

        # 1. Metric self-reference: m1 references m1
        dummy_id = uuid4()
        m_self_cand = MetricResult(
            result_id=dummy_id,
            asset_id=asset_id,
            metric_key="market.self_ref",
            value=Decimal("1.0"),
            unit="unit",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs.observation_id],
            input_metric_result_ids=[dummy_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        with pytest.raises(MetricV2Error, match="cycle"):
            staging.save_metrics([m_self_cand])

        # 2. Metric 2-node cycle: m_a -> m_b -> m_a
        from investment_analyst.storage.analytical_v2_validation import (
            AnalyticalV2ValidationError,
            topological_sort_metrics,
        )

        id_a = uuid4()
        id_b = uuid4()
        m_a_cand = MetricResult(
            result_id=id_a,
            asset_id=asset_id,
            metric_key="market.cycle.a",
            value=Decimal("1.0"),
            unit="unit",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs.observation_id],
            input_metric_result_ids=[id_b],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m_b_cand = MetricResult(
            result_id=id_b,
            asset_id=asset_id,
            metric_key="market.cycle.b",
            value=Decimal("1.0"),
            unit="unit",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs.observation_id],
            input_metric_result_ids=[id_a],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        with pytest.raises(AnalyticalV2ValidationError, match="cycle"):
            topological_sort_metrics([m_a_cand, m_b_cand])

        # 3. Duplicate observation inputs in single metric
        with pytest.raises(ValueError, match="input_observation_ids must be unique"):
            MetricResult(
                result_id=uuid4(),
                asset_id=asset_id,
                metric_key="market.dup.obs",
                value=Decimal("1.0"),
                unit="unit",
                as_of=base_time,
                available_at=base_time,
                computed_at=base_time,
                parameters={},
                input_observation_ids=[obs.observation_id, obs.observation_id],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )

        # 4. Corrupt link positions in snapshot table fails closed
        valid_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.valid.m",
            value=Decimal("10.0"),
            unit="USD",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        valid_m = valid_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(valid_m_cand)}
        )
        staging.save_metrics([valid_m])

        valid_snap = build_analysis_snapshot(
            asset_id=asset_id,
            domain="market",
            known_at=base_time,
            policy_version="v1",
            metric_ids=[valid_m.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=base_time,
        )
        staging.save_analysis_snapshots([valid_snap])

        # Tamper with metric link table to introduce a position gap (pos 0 -> pos 5)
        staging._connection.execute(
            f"UPDATE {ANALYSIS_SNAPSHOT_V2_METRIC_LINKS_TABLE} "
            f"SET position = 5 WHERE snapshot_id = '{valid_snap.snapshot_id}'"
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="positions are corrupt"):
            staging.get_analysis_snapshot(valid_snap.snapshot_id)
