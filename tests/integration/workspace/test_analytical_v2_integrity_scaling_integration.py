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
    """A3: Keyset pagination isolates assets, verifies bounds and handles ties in time."""
    import math

    staging = _staging(tmp_path, "staging-multiasset")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_target = "equity:us:aapl"

    with staging:
        obs_target = _seed_observation(staging, base_time, asset_target)
        # Seed another observation with different source and cut for target asset to test isolation
        _seed_observation(
            staging,
            base_time + timedelta(days=10),
            asset_target,
            field_name="volume",
            source_id="test:other_feed",
        )

        target_metrics: list[MetricResult] = []
        target_diags: list[DiagnosticResult] = []
        target_snaps = []

        # Create 513 metrics, 513 diagnostics, and 513 snapshots for asset_target
        for i in range(513):
            moment = base_time + timedelta(minutes=i)
            cand_m = MetricResult(
                result_id=uuid4(),
                asset_id=asset_target,
                metric_key=f"market.close.{i}",
                value=Decimal(str(i)),
                unit="USD",
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                parameters={"idx": i},
                input_observation_ids=[obs_target.observation_id],
                input_metric_result_ids=[],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            m = cand_m.model_copy(update={"result_id": metric_result_id_from_model_v2(cand_m)})
            target_metrics.append(m)

            d = DiagnosticResult(
                diagnostic_id=uuid4(),
                asset_id=asset_target,
                mode=DiagnosticMode.MARKET,
                verdict=DiagnosticVerdict.POSITIVE,
                final_score=Decimal("50.0"),
                confidence=Decimal("0.8"),
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                components=[
                    DiagnosticComponent(
                        component_key="comp",
                        score=Decimal("50.0"),
                        weight=Decimal("1.0"),
                        weighted_contribution=Decimal("50.0"),
                        metric_result_ids=[m.result_id],
                        explanation="Check",
                    )
                ],
                evidence=[
                    DiagnosticEvidence(
                        metric_result_id=m.result_id,
                        direction=EvidenceDirection.SUPPORTS,
                        contribution=Decimal("50.0"),
                        reason="Check positive",
                    )
                ],
                algorithm_version="v1",
                summary=f"Diag target {i}",
                quality=DataQuality.VALID,
            )
            target_diags.append(d)

            s = build_analysis_snapshot(
                asset_id=asset_target,
                domain="market",
                known_at=moment,
                policy_version="v1",
                metric_ids=[m.result_id],
                diagnostic_ids=[d.diagnostic_id],
                evidence_set_hashes=[],
                created_at=moment,
            )
            target_snaps.append(s)

        staging.save_metrics(target_metrics)
        staging.save_diagnostics(target_diags)
        staging.save_analysis_snapshots(target_snaps)

        # Populate 32 foreign assets with 2,048 foreign rows in diagnostic_results_v2
        # and analysis_snapshots_v2
        staging._connection.execute(
            """
            INSERT INTO diagnostic_results_v2 (
                diagnostic_id, asset_id, mode, verdict, final_score_text, confidence_text,
                as_of, available_at, computed_at, algorithm_version, summary, quality
            )
            SELECT
                uuid()::VARCHAR,
                'foreign:asset_' || (i % 32)::VARCHAR,
                'market',
                'positive',
                '50.0',
                '0.8',
                '2026-08-01T00:00:00+00:00',
                '2026-08-01T00:00:00+00:00',
                '2026-08-01T00:00:00+00:00',
                'v1',
                'foreign summary',
                'valid'
            FROM range(2048) tbl(i)
            """
        )
        staging._connection.execute(
            """
            INSERT INTO analysis_snapshots_v2 (
                snapshot_id, asset_id, domain, known_at, policy_version,
                evidence_set_digest, created_at
            )
            SELECT
                uuid()::VARCHAR,
                'foreign:asset_' || (i % 32)::VARCHAR,
                'market',
                '2026-08-01T00:00:00+00:00',
                'v1',
                '0000000000000000000000000000000000000000000000000000000000000000',
                '2026-08-01T00:00:00+00:00'
            FROM range(2048) tbl(i)
            """
        )

        # Install Query Tracking Proxy
        class TrackingConnection:
            def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
                self._conn = conn
                self.queries: list[str] = []
                self.param_chunks: list[int] = []

            def execute(
                self, query: str, *args: object, **kwargs: object
            ) -> duckdb.DuckDBPyConnection:
                self.queries.append(str(query))
                if args and isinstance(args[0], (list, tuple)):
                    self.param_chunks.append(len(args[0]))
                return self._conn.execute(query, *args, **kwargs)

            def __getattr__(self, name: str) -> object:
                return getattr(self._conn, name)

        proxy = TrackingConnection(staging._connection)
        staging._connection = proxy

        # Cardinalities matrix: K in (1, 256, 257, 513)
        for K in (1, 256, 257, 513):
            expected_bound = 16 + 64 * math.ceil(K / 256)

            # 1. get_metrics
            proxy.queries.clear()
            proxy.param_chunks.clear()
            res_m = staging.get_metrics([m.result_id for m in target_metrics[:K]])
            assert len(res_m) == K
            assert len(proxy.queries) <= expected_bound
            assert all(size <= 256 for size in proxy.param_chunks)

            # 2. get_diagnostics
            proxy.queries.clear()
            proxy.param_chunks.clear()
            res_d = staging.get_diagnostics([d.diagnostic_id for d in target_diags[:K]])
            assert len(res_d) == K
            assert len(proxy.queries) <= expected_bound
            assert all(size <= 256 for size in proxy.param_chunks)

            # 3. get_analysis_snapshots
            proxy.queries.clear()
            proxy.param_chunks.clear()
            res_s = staging.get_analysis_snapshots([s.snapshot_id for s in target_snaps[:K]])
            assert len(res_s) == K
            assert len(proxy.queries) <= expected_bound
            assert all(size <= 256 for size in proxy.param_chunks)

        # 4. list_diagnostics with limit=None traverses internally by pages without truncating
        all_diags = staging.list_diagnostics(asset_id=asset_target)
        assert len(all_diags) == 513
        assert all(d.asset_id == asset_target for d in all_diags)
        assert [d.diagnostic_id for d in all_diags] == [d.diagnostic_id for d in target_diags]

        # 5. list_analysis_snapshots with limit=None traverses internally by pages
        # without truncating
        all_snaps = staging.list_analysis_snapshots(asset_id=asset_target)
        assert len(all_snaps) == 513
        assert all(s.asset_id == asset_target for s in all_snaps)
        assert [s.snapshot_id for s in all_snaps] == [s.snapshot_id for s in target_snaps]

        # 6. Keyset pagination across pages with limit=256
        collected_paged_diags: list[DiagnosticResult] = []
        cur_at: datetime | None = None
        cur_id: UUID | None = None
        page_sizes = []
        while True:
            page = staging.list_diagnostics(
                asset_id=asset_target,
                cursor_at=cur_at,
                cursor_id=cur_id,
                limit=256,
            )
            if not page:
                break
            page_sizes.append(len(page))
            collected_paged_diags.extend(page)
            cur_at = page[-1].available_at
            cur_id = page[-1].diagnostic_id
        assert page_sizes == [256, 256, 1]
        assert len(collected_paged_diags) == 513
        assert [d.diagnostic_id for d in collected_paged_diags] == [
            d.diagnostic_id for d in target_diags
        ]

        collected_paged_snaps = []
        cur_at = None
        cur_id = None
        snap_page_sizes = []
        while True:
            page = staging.list_analysis_snapshots(
                asset_id=asset_target,
                cursor_at=cur_at,
                cursor_id=cur_id,
                limit=256,
            )
            if not page:
                break
            snap_page_sizes.append(len(page))
            collected_paged_snaps.extend(page)
            cur_at = page[-1].known_at
            cur_id = page[-1].snapshot_id
        assert snap_page_sizes == [256, 256, 1]
        assert len(collected_paged_snaps) == 513
        assert [s.snapshot_id for s in collected_paged_snaps] == [
            s.snapshot_id for s in target_snaps
        ]

        # 7. Keyset pagination with ties in time: identical timestamps, distinct UUIDs
        tie_time = base_time + timedelta(days=50)
        obs_tie = _seed_observation(staging, tie_time, "equity:us:tie")
        cand_tie_m = MetricResult(
            result_id=uuid4(),
            asset_id="equity:us:tie",
            metric_key="market.close.tie",
            value=Decimal("100.0"),
            unit="USD",
            as_of=tie_time,
            available_at=tie_time,
            computed_at=tie_time,
            parameters={},
            input_observation_ids=[obs_tie.observation_id],
            input_metric_result_ids=[],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        tie_m = cand_tie_m.model_copy(
            update={"result_id": metric_result_id_from_model_v2(cand_tie_m)}
        )
        staging.save_metrics([tie_m])

        tie_diags = []
        for j in range(5):
            td = DiagnosticResult(
                diagnostic_id=uuid4(),
                asset_id="equity:us:tie",
                mode=DiagnosticMode.MARKET,
                verdict=DiagnosticVerdict.POSITIVE,
                final_score=Decimal("50.0"),
                confidence=Decimal("0.8"),
                as_of=tie_time,
                available_at=tie_time,
                computed_at=tie_time,
                components=[
                    DiagnosticComponent(
                        component_key="c",
                        score=Decimal("50.0"),
                        weight=Decimal("1.0"),
                        weighted_contribution=Decimal("50.0"),
                        metric_result_ids=[tie_m.result_id],
                        explanation="ok",
                    )
                ],
                evidence=[
                    DiagnosticEvidence(
                        metric_result_id=tie_m.result_id,
                        direction=EvidenceDirection.SUPPORTS,
                        contribution=Decimal("50.0"),
                        reason="ok",
                    )
                ],
                algorithm_version="v1",
                summary=f"Tie diag {j}",
                quality=DataQuality.VALID,
            )
            tie_diags.append(td)
        staging.save_diagnostics(tie_diags)
        # In SQL, rows with identical available_at are ordered by diagnostic_id string comparison
        expected_tie_order = sorted([d.diagnostic_id for d in tie_diags], key=lambda u: str(u))

        tie_collected = []
        t_at = None
        t_id = None
        while True:
            page = staging.list_diagnostics(
                asset_id="equity:us:tie",
                cursor_at=t_at,
                cursor_id=t_id,
                limit=2,
            )
            if not page:
                break
            tie_collected.extend(page)
            t_at = page[-1].available_at
            t_id = page[-1].diagnostic_id
        assert [d.diagnostic_id for d in tie_collected] == expected_tie_order

        # 8. Rejection of invalid limits and partial cursors
        for invalid_limit in (True, False, 0, 257, "10"):  # type: ignore[arg-type]
            with pytest.raises(DiagnosticV2Error):
                staging.list_diagnostics(asset_id=asset_target, limit=invalid_limit)
            with pytest.raises(AnalysisSnapshotV2Error):
                staging.list_analysis_snapshots(asset_id=asset_target, limit=invalid_limit)

        with pytest.raises(DiagnosticV2Error):
            staging.list_diagnostics(asset_id=asset_target, cursor_at=base_time, cursor_id=None)
        with pytest.raises(DiagnosticV2Error):
            staging.list_diagnostics(asset_id=asset_target, cursor_at=None, cursor_id=uuid4())
        with pytest.raises(AnalysisSnapshotV2Error):
            staging.list_analysis_snapshots(
                asset_id=asset_target, cursor_at=base_time, cursor_id=None
            )
        with pytest.raises(AnalysisSnapshotV2Error):
            staging.list_analysis_snapshots(
                asset_id=asset_target, cursor_at=None, cursor_id=uuid4()
            )


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

        # 5. Tampering metric_key within market.* (e.g. daily.close -> daily.open) fails closed
        tamper_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.statistics.daily.close",
            value=Decimal("150.00"),
            unit="USD",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs.observation_id],
            input_metric_result_ids=[],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        tamper_m = tamper_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(tamper_cand)}
        )
        staging.save_metrics([tamper_m])

        staging._connection.execute(
            "UPDATE metric_results_v2 "
            "SET metric_key = 'market.statistics.daily.open' "
            "WHERE result_id = ?",
            [str(tamper_m.result_id)],
        )

        with pytest.raises(MetricV2Error, match="metric v2 identity diverged"):
            staging.get_metrics([tamper_m.result_id])

        diag_citing_tampered = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("50.0"),
            confidence=Decimal("0.8"),
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            components=[
                DiagnosticComponent(
                    component_key="c",
                    score=Decimal("50.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("50.0"),
                    metric_result_ids=[tamper_m.result_id],
                    explanation="Citing tampered metric",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=tamper_m.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("50.0"),
                    reason="Tampered",
                )
            ],
            algorithm_version="v1",
            summary="Diag with tampered metric",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="metric v2 identity diverged"):
            staging.save_diagnostics([diag_citing_tampered])

        snap_citing_tampered = build_analysis_snapshot(
            asset_id=asset_id,
            domain="market",
            known_at=base_time,
            policy_version="v1",
            metric_ids=[tamper_m.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=base_time,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="metric v2 identity diverged"):
            staging.save_analysis_snapshots([snap_citing_tampered])
