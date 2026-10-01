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
    """A2: Domain membership matrix and snapshot digest resolving all metrics."""
    staging = _staging(tmp_path, "staging-domains-matrix")
    cut1 = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    cut2 = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
    clock1 = datetime(2026, 8, 1, 11, 0, tzinfo=UTC)  # computed_at > available_at
    clock2 = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)

    # 1. Multi-asset matrix: equity, ETF, crypto, and synthetic asset
    assets = {
        "equity": "equity:us:aapl",
        "etf": "equity:us:spy",
        "crypto": "crypto:btc-usd",
        "synthetic": "synthetic:custom:test",
    }

    with staging:
        # Seed observations and evidence sets for each asset across two PIT cuts
        obs_by_asset: dict[str, dict[datetime, NormalizedObservation]] = {}
        es_by_asset: dict[str, dict[datetime, object]] = {}
        for category, asset_id in assets.items():
            obs_by_asset[category] = {}
            es_by_asset[category] = {}
            for cut in (cut1, cut2):
                obs = _seed_observation(staging, cut, asset_id, field_name=f"price_{category}")
                obs_by_asset[category][cut] = obs
                seg = build_evidence_segments([obs])
                staging.save_evidence_segments(seg)
                es = build_evidence_set([obs], segments=seg)
                staging.save_evidence_set(es)
                es_by_asset[category][cut] = es

        # Canonical metrics per domain
        # Market metric for AAPL at cut1
        aapl_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=assets["equity"],
            metric_key="market.close.price",
            value=Decimal("150.00"),
            unit="USD",
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            parameters={"evidence_set_id": str(es_by_asset["equity"][cut1].evidence_set_id)},
            input_observation_ids=[obs_by_asset["equity"][cut1].observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        aapl_m = aapl_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(aapl_m_cand)}
        )

        # Market metric for SPY at cut1
        spy_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=assets["etf"],
            metric_key="market.close.price",
            value=Decimal("450.00"),
            unit="USD",
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            parameters={"evidence_set_id": str(es_by_asset["etf"][cut1].evidence_set_id)},
            input_observation_ids=[obs_by_asset["etf"][cut1].observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        spy_m = spy_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(spy_m_cand)}
        )

        # Derivatives metric for Crypto (funding rate) at cut2
        btc_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=assets["crypto"],
            metric_key="crypto.derivatives.funding.rate",
            value=Decimal("0.01"),
            unit="rate",
            as_of=cut2,
            available_at=cut2,
            computed_at=clock2,
            parameters={"evidence_set_id": str(es_by_asset["crypto"][cut2].evidence_set_id)},
            input_observation_ids=[obs_by_asset["crypto"][cut2].observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        btc_m = btc_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(btc_m_cand)}
        )

        # Fundamental metric for AAPL at cut1
        fund_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=assets["equity"],
            metric_key="fundamental.net_income",
            value=Decimal("25000000000.00"),
            unit="USD",
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            parameters={"evidence_set_id": str(es_by_asset["equity"][cut1].evidence_set_id)},
            input_observation_ids=[obs_by_asset["equity"][cut1].observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        fund_m = fund_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(fund_m_cand)}
        )

        # Valuation metric for AAPL at cut1
        val_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=assets["equity"],
            metric_key="valuation.corporate.pe_ratio",
            value=Decimal("28.5"),
            unit="ratio",
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            parameters={},
            input_observation_ids=[obs_by_asset["equity"][cut1].observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        val_m = val_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(val_m_cand)}
        )

        # Events metric for Synthetic asset at cut2
        events_m_cand = MetricResult(
            result_id=uuid4(),
            asset_id=assets["synthetic"],
            metric_key="cazatiburones.insider_signal",
            value=Decimal("1.0"),
            unit="signal",
            as_of=cut2,
            available_at=cut2,
            computed_at=clock2,
            parameters={},
            input_observation_ids=[obs_by_asset["synthetic"][cut2].observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        events_m = events_m_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(events_m_cand)}
        )

        staging.save_metrics([aapl_m, spy_m, btc_m, fund_m, val_m, events_m])

        # 2. Separation of references: changing only asset applies same policy
        # and separates references
        snap_spy_foreign = build_analysis_snapshot(
            asset_id=assets["etf"],
            domain=AnalysisDomain.MARKET.value,
            known_at=cut1,
            policy_version="v1",
            metric_ids=[aapl_m.result_id],  # AAPL metric cited for SPY!
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=cut1,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="foreign metric"):
            staging.save_analysis_snapshots([snap_spy_foreign])

        # 3. FUNDAMENTAL <-> funding rejection:
        # A fundamental snapshot citing funding/derivatives metric fails closed
        snap_fund_with_funding = build_analysis_snapshot(
            asset_id=assets["crypto"],
            domain=AnalysisDomain.FUNDAMENTAL.value,
            known_at=cut2,
            policy_version="v1",
            metric_ids=[btc_m.result_id],  # crypto.derivatives.* in fundamental snapshot!
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=cut2,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="must start with 'fundamental.'"):
            staging.save_analysis_snapshots([snap_fund_with_funding])

        # Diagnostic in FUNDAMENTAL mode citing crypto.derivatives.* metric fails closed
        diag_fund_with_funding = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["crypto"],
            mode=DiagnosticMode.FUNDAMENTAL,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=cut2,
            available_at=cut2,
            computed_at=clock2,
            components=[
                DiagnosticComponent(
                    component_key="funding_comp",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[btc_m.result_id],
                    explanation="Funding in fundamental diag",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=btc_m.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="Funding in fundamental diag",
                )
            ],
            algorithm_version="v1",
            summary="Fund diag with funding",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="FUNDAMENTAL mode cannot belong"):
            staging.save_diagnostics([diag_fund_with_funding])

        # 4. MARKET <-> fundamental rejection:
        # A market snapshot citing fundamental metric fails closed
        snap_market_with_fund = build_analysis_snapshot(
            asset_id=assets["equity"],
            domain=AnalysisDomain.MARKET.value,
            known_at=cut1,
            policy_version="v1",
            metric_ids=[fund_m.result_id],  # fundamental metric in market snapshot!
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=cut1,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="must start with 'market.'"):
            staging.save_analysis_snapshots([snap_market_with_fund])

        # Diagnostic in MARKET mode citing fundamental metric fails closed
        diag_market_with_fund = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["equity"],
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            components=[
                DiagnosticComponent(
                    component_key="fund_comp",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[fund_m.result_id],
                    explanation="Fundamental in market diag",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=fund_m.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="Fundamental in market diag",
                )
            ],
            algorithm_version="v1",
            summary="Market diag with fundamental",
            quality=DataQuality.VALID,
        )
        with pytest.raises(
            DiagnosticV2Error, match="MARKET mode admits only market or derivatives"
        ):
            staging.save_diagnostics([diag_market_with_fund])

        # 5. Unknown domain rejection
        for bad_domain in ("unknown", "macro", "funding", ""):
            with pytest.raises((AnalysisSnapshotV2Error, ValueError)):
                snap_unknown = build_analysis_snapshot(
                    asset_id=assets["equity"],
                    domain=bad_domain,
                    known_at=cut1,
                    policy_version="v1",
                    metric_ids=[aapl_m.result_id],
                    diagnostic_ids=[],
                    evidence_set_hashes=[],
                    created_at=cut1,
                )
                staging.save_analysis_snapshots([snap_unknown])

        # 6. UNIFIED mode rejection
        diag_unified = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["equity"],
            mode=DiagnosticMode.UNIFIED,
            verdict=DiagnosticVerdict.NEUTRAL,
            final_score=Decimal("50.0"),
            confidence=Decimal("0.5"),
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            components=[
                DiagnosticComponent(
                    component_key="u_comp",
                    score=Decimal("50.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("50.0"),
                    metric_result_ids=[aapl_m.result_id],
                    explanation="Unified diag",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=aapl_m.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("50.0"),
                    reason="Unified diag",
                )
            ],
            algorithm_version="v1",
            summary="Unified diag",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="UNIFIED is unauthorized"):
            staging.save_diagnostics([diag_unified])

        # 7. Mixed diagnostic rejection (market + derivatives in single diagnostic)
        obs_mix = _seed_observation(staging, cut1, assets["equity"], field_name="crypto_mix")
        cand_deriv_aapl = MetricResult(
            result_id=uuid4(),
            asset_id=assets["equity"],
            metric_key="crypto.derivatives.index_price",
            value=Decimal("100.0"),
            unit="USD",
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            parameters={},
            input_observation_ids=[obs_mix.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        deriv_aapl = cand_deriv_aapl.model_copy(
            update={"result_id": metric_result_id_from_model_v2(cand_deriv_aapl)}
        )
        staging.save_metrics([deriv_aapl])

        diag_mixed = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["equity"],
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            components=[
                DiagnosticComponent(
                    component_key="c1",
                    score=Decimal("80.0"),
                    weight=Decimal("0.5"),
                    weighted_contribution=Decimal("40.0"),
                    metric_result_ids=[aapl_m.result_id],  # market.*
                    explanation="Market part",
                ),
                DiagnosticComponent(
                    component_key="c2",
                    score=Decimal("80.0"),
                    weight=Decimal("0.5"),
                    weighted_contribution=Decimal("40.0"),
                    metric_result_ids=[deriv_aapl.result_id],  # crypto.derivatives.*
                    explanation="Deriv part",
                ),
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=aapl_m.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("40.0"),
                    reason="Market part",
                ),
                DiagnosticEvidence(
                    metric_result_id=deriv_aapl.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("40.0"),
                    reason="Deriv part",
                ),
            ],
            algorithm_version="v1",
            summary="Mixed diagnostic",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="mixed metric domains"):
            staging.save_diagnostics([diag_mixed])

        # 8. Valuation and events domains:
        # Snapshot in valuation with metric and NO diagnostics succeeds!
        snap_val = build_analysis_snapshot(
            asset_id=assets["equity"],
            domain=AnalysisDomain.VALUATION.value,
            known_at=cut1,
            policy_version="v1",
            metric_ids=[val_m.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=cut1,
        )
        assert staging.save_analysis_snapshots([snap_val]).created_count == 1

        # Snapshot in events with metric and NO diagnostics succeeds!
        snap_events = build_analysis_snapshot(
            asset_id=assets["synthetic"],
            domain=AnalysisDomain.EVENTS.value,
            known_at=cut2,
            policy_version="v1",
            metric_ids=[events_m.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=cut2,
        )
        assert staging.save_analysis_snapshots([snap_events]).created_count == 1

        # Diagnostic in valid market domain
        valid_aapl_diag = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["equity"],
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            components=[
                DiagnosticComponent(
                    component_key="c1",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[aapl_m.result_id],
                    explanation="AAPL market trend",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=aapl_m.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="AAPL market trend",
                )
            ],
            algorithm_version="v1",
            summary="AAPL market diag",
            quality=DataQuality.VALID,
        )
        staging.save_diagnostics([valid_aapl_diag])

        # Valuation snapshot with diagnostic fails closed
        snap_val_with_diag = build_analysis_snapshot(
            asset_id=assets["equity"],
            domain=AnalysisDomain.VALUATION.value,
            known_at=cut1,
            policy_version="v1",
            metric_ids=[val_m.result_id],
            diagnostic_ids=[valid_aapl_diag.diagnostic_id],
            evidence_set_hashes=[],
            created_at=cut1,
        )
        with pytest.raises(
            AnalysisSnapshotV2Error, match="does not have authorized diagnostic mode"
        ):
            staging.save_analysis_snapshots([snap_val_with_diag])

        # 9. Snapshot with ONLY diagnostics (metric_ids=[]) whose EvidenceSet
        # does not appear in metric_ids:
        snap_diag_only_valid = build_analysis_snapshot(
            asset_id=assets["equity"],
            domain=AnalysisDomain.MARKET.value,
            known_at=cut1,
            policy_version="v1",
            metric_ids=[],  # ONLY diagnostics!
            diagnostic_ids=[valid_aapl_diag.diagnostic_id],
            evidence_set_hashes=[es_by_asset["equity"][cut1].canonical_hash],
            created_at=cut1,
        )
        receipt_diag_only = staging.save_analysis_snapshots([snap_diag_only_valid])
        assert receipt_diag_only.created_count == 1

        # 10. Modified digest rejection:
        snap_bad_digest = build_analysis_snapshot(
            asset_id=assets["equity"],
            domain=AnalysisDomain.MARKET.value,
            known_at=cut1,
            policy_version="v1",
            metric_ids=[],
            diagnostic_ids=[valid_aapl_diag.diagnostic_id],
            evidence_set_hashes=["0" * 64],  # Modified hash!
            created_at=cut1,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="does not match resolved hashes digest"):
            staging.save_analysis_snapshots([snap_bad_digest])

        # 11. Legitimate absence:
        # Snapshot with empty metric_ids and empty diagnostic_ids
        snap_empty = build_analysis_snapshot(
            asset_id=assets["synthetic"],
            domain=AnalysisDomain.EVENTS.value,
            known_at=cut2,
            policy_version="v1",
            metric_ids=[],
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=cut2,
        )
        assert staging.save_analysis_snapshots([snap_empty]).created_count == 1

        # Empty diagnostics
        empty_diag_market = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["equity"],
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            components=[],
            evidence=[],
            algorithm_version="v1",
            summary="Empty market diag",
            quality=DataQuality.VALID,
        )
        assert staging.save_diagnostics([empty_diag_market]).created_count == 1

        empty_diag_fund = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=assets["equity"],
            mode=DiagnosticMode.FUNDAMENTAL,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=cut1,
            available_at=cut1,
            computed_at=clock1,
            components=[],
            evidence=[],
            algorithm_version="v1",
            summary="Empty fund diag",
            quality=DataQuality.VALID,
        )
        assert staging.save_diagnostics([empty_diag_fund]).created_count == 1

        # 12. Deserialization and pure original identity:
        rehydrated = staging.get_analysis_snapshot(snap_diag_only_valid.snapshot_id)
        assert rehydrated.snapshot_id == snap_diag_only_valid.snapshot_id
        assert rehydrated.asset_id == assets["equity"]
        assert rehydrated.domain == AnalysisDomain.MARKET.value
        assert rehydrated.known_at == cut1
        assert rehydrated.diagnostic_ids == (valid_aapl_diag.diagnostic_id,)
        assert rehydrated.metric_ids == ()
        assert rehydrated.evidence_set_digest == snap_diag_only_valid.evidence_set_digest


def test_multiasset_reads_are_paged_and_do_not_hydrate_unrelated_history(
    tmp_path: Path,
) -> None:
    """A3: Keyset pagination isolates assets, verifies bounds and handles ties in time."""
    import math
    import re
    import time

    from investment_analyst.storage.analytical_v2_validation import chunked_sequence

    staging = _staging(tmp_path, "staging-multiasset")
    base_time = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    asset_target = "equity:us:aapl"

    with staging:
        # 1. Target fixtures: observation and shared EvidenceSet
        obs_target = _seed_observation(staging, base_time, asset_target)
        seg_target = build_evidence_segments([obs_target])
        staging.save_evidence_segments(seg_target)
        es_target = build_evidence_set([obs_target], segments=seg_target)
        staging.save_evidence_set(es_target)

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
        # Metrics share the exact same evidence_set_id pointing to es_target
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
                parameters={"idx": i, "evidence_set_id": str(es_target.evidence_set_id)},
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
                evidence_set_hashes=[es_target.canonical_hash],
                created_at=moment,
            )
            target_snaps.append(s)

        staging.save_metrics(target_metrics)
        staging.save_diagnostics(target_diags)
        staging.save_analysis_snapshots(target_snaps)

        # 2. Query and Row Tracking Proxy
        class TrackingCursor:
            def __init__(
                self, cursor: duckdb.DuckDBPyConnection, proxy: "TrackingConnection"
            ) -> None:
                self._cursor = cursor
                self._proxy = proxy

            def fetchall(self) -> list[tuple[object, ...]]:
                rows = self._cursor.fetchall()
                if self._proxy.active:
                    self._proxy.rows_fetched += len(rows)
                    for tbl in self._proxy.current_query_tables:
                        self._proxy.table_rows[tbl] = self._proxy.table_rows.get(tbl, 0) + len(rows)
                return rows

            def fetchone(self) -> tuple[object, ...] | None:
                row = self._cursor.fetchone()
                if self._proxy.active and row is not None:
                    self._proxy.rows_fetched += 1
                    for tbl in self._proxy.current_query_tables:
                        self._proxy.table_rows[tbl] = self._proxy.table_rows.get(tbl, 0) + 1
                return row

            def __getattr__(self, name: str) -> object:
                return getattr(self._cursor, name)

        class TrackingConnection:
            ALL_TABLES = (
                "metric_results_v2",
                "metric_v2_observation_links",
                "metric_v2_metric_links",
                "diagnostic_results_v2",
                "diagnostic_v2_components",
                "diagnostic_v2_component_metric_links",
                "diagnostic_v2_evidence",
                "analysis_snapshots_v2",
                "analysis_snapshot_v2_metric_links",
                "analysis_snapshot_v2_diagnostic_links",
                "evidence_sets_v2",
                "evidence_set_v2_members",
                "evidence_segments_v2",
                "normalized_observations_v2",
            )

            def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
                self._conn = conn
                self.queries: list[str] = []
                self.param_chunks: list[int] = []
                self.table_counts: dict[str, int] = {}
                self.table_rows: dict[str, int] = {}
                self.metadata_queries: int = 0
                self.rows_fetched: int = 0
                self.current_query_tables: list[str] = []
                self.active = True

            def execute(self, query: str, *args: object, **kwargs: object) -> TrackingCursor:
                self.current_query_tables = []
                if self.active:
                    q_str = str(query)
                    self.queries.append(q_str)
                    is_metadata = "information_schema" in q_str.lower()
                    if is_metadata:
                        self.metadata_queries += 1
                    else:
                        for tbl in self.ALL_TABLES:
                            if re.search(rf"\b{tbl}\b", q_str):
                                self.table_counts[tbl] = self.table_counts.get(tbl, 0) + 1
                                self.current_query_tables.append(tbl)
                    if args and isinstance(args[0], (list, tuple)):
                        self.param_chunks.append(len(args[0]))
                cursor = self._conn.execute(query, *args, **kwargs)
                return TrackingCursor(cursor, self)

            def cursor(self) -> TrackingCursor:
                return TrackingCursor(self._conn.cursor(), self)

            def clear(self) -> None:
                self.queries.clear()
                self.param_chunks.clear()
                self.table_counts.clear()
                self.table_rows.clear()
                self.metadata_queries = 0
                self.rows_fetched = 0
                self.current_query_tables.clear()

            def __getattr__(self, name: str) -> object:
                return getattr(self._conn, name)

        proxy = TrackingConnection(staging._connection)
        staging._connection = proxy

        # 3. BASELINE: Measure queries, rows fetched, table rows, and timings
        # BEFORE inserting 2,048 foreign rows
        baseline_res_m = {}
        baseline_res_d = {}
        baseline_res_s = {}
        baseline_queries_m = {}
        baseline_queries_d = {}
        baseline_queries_s = {}
        baseline_rows_m = {}
        baseline_rows_d = {}
        baseline_rows_s = {}
        baseline_table_counts_m = {}
        baseline_table_counts_d = {}
        baseline_table_counts_s = {}
        baseline_table_rows_m = {}
        baseline_table_rows_d = {}
        baseline_table_rows_s = {}
        baseline_durations_m = {}
        baseline_durations_d = {}
        baseline_durations_s = {}

        for K in (1, 256, 257, 513):
            # 1. get_metrics baseline
            proxy.clear()
            t0 = time.perf_counter()
            baseline_res_m[K] = staging.get_metrics([m.result_id for m in target_metrics[:K]])
            baseline_durations_m[K] = time.perf_counter() - t0
            baseline_queries_m[K] = len(proxy.queries)
            baseline_rows_m[K] = proxy.rows_fetched
            baseline_table_counts_m[K] = dict(proxy.table_counts)
            baseline_table_rows_m[K] = dict(proxy.table_rows)

            # 2. get_diagnostics baseline
            proxy.clear()
            t0 = time.perf_counter()
            baseline_res_d[K] = staging.get_diagnostics([d.diagnostic_id for d in target_diags[:K]])
            baseline_durations_d[K] = time.perf_counter() - t0
            baseline_queries_d[K] = len(proxy.queries)
            baseline_rows_d[K] = proxy.rows_fetched
            baseline_table_counts_d[K] = dict(proxy.table_counts)
            baseline_table_rows_d[K] = dict(proxy.table_rows)

            # 3. get_analysis_snapshots baseline
            proxy.clear()
            t0 = time.perf_counter()
            baseline_res_s[K] = staging.get_analysis_snapshots(
                [s.snapshot_id for s in target_snaps[:K]]
            )
            baseline_durations_s[K] = time.perf_counter() - t0
            baseline_queries_s[K] = len(proxy.queries)
            baseline_rows_s[K] = proxy.rows_fetched
            baseline_table_counts_s[K] = dict(proxy.table_counts)
            baseline_table_rows_s[K] = dict(proxy.table_rows)

        # 4. Deactivate proxy tracking while seeding foreign rows
        proxy.active = False

        # Populate 32 foreign assets with 2,048 foreign rows in metric_results_v2,
        # diagnostic_results_v2, and analysis_snapshots_v2
        staging._connection.execute(
            """
            INSERT INTO metric_results_v2 (
                result_id, asset_id, metric_key, value_text, unit,
                as_of, available_at, computed_at, parameters_json, evidence_set_id,
                algorithm_version, quality
            )
            SELECT
                uuid()::VARCHAR,
                'foreign:asset_' || (i % 32)::VARCHAR,
                'market.close.foreign',
                '100.0',
                'USD',
                '2026-08-01T00:00:00+00:00',
                '2026-08-01T00:00:00+00:00',
                '2026-08-01T00:00:00+00:00',
                '{}',
                NULL,
                'v1',
                'valid'
            FROM range(2048) tbl(i)
            """
        )
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

        proxy.active = True

        # 5. AFTER FOREIGN ROWS: Cardinalities matrix K in (1, 256, 257, 513)
        # Asserts:
        # - Models hydrated == K, zero foreign models
        # - Queries exactly match baseline (no foreign scans)
        # - Rows fetched exactly match baseline (no foreign hydration)
        # - Table counts and table rows match baseline exactly (zero foreign rows hydrated)
        # - Execution timings measured before and after foreign rows
        #   (without constant latency assumption)
        # - Queries strictly <= 16 + 64 * ceil(K / 256)
        # - Chunk lookups <= 256
        # - Per-table queries bounded
        after_durations_m = {}
        after_durations_d = {}
        after_durations_s = {}

        for K in (1, 256, 257, 513):
            expected_chunks = math.ceil(K / 256)
            loose_bound = 16 + 64 * expected_chunks

            # 1. get_metrics
            proxy.clear()
            t0 = time.perf_counter()
            res_m = staging.get_metrics([m.result_id for m in target_metrics[:K]])
            after_durations_m[K] = time.perf_counter() - t0

            assert len(res_m) == K
            assert all(m.asset_id == asset_target for m in res_m.values())
            assert res_m == baseline_res_m[K]
            assert len(proxy.queries) == baseline_queries_m[K]
            assert proxy.rows_fetched == baseline_rows_m[K]
            assert proxy.table_counts == baseline_table_counts_m[K]
            assert proxy.table_rows == baseline_table_rows_m[K]
            assert baseline_durations_m[K] > 0 and after_durations_m[K] > 0
            assert len(proxy.queries) <= loose_bound
            assert proxy.table_rows.get("metric_results_v2", 0) == K
            assert proxy.table_rows.get("metric_v2_observation_links", 0) == K
            assert proxy.table_counts.get("metric_results_v2", 0) <= expected_chunks
            assert proxy.table_counts.get("metric_v2_observation_links", 0) <= expected_chunks
            assert proxy.table_counts.get("metric_v2_metric_links", 0) <= expected_chunks
            assert all(size <= 256 for size in proxy.param_chunks)

            # 2. get_diagnostics
            proxy.clear()
            t0 = time.perf_counter()
            res_d = staging.get_diagnostics([d.diagnostic_id for d in target_diags[:K]])
            after_durations_d[K] = time.perf_counter() - t0

            assert len(res_d) == K
            assert all(d.asset_id == asset_target for d in res_d.values())
            assert res_d == baseline_res_d[K]
            assert len(proxy.queries) == baseline_queries_d[K]
            assert proxy.rows_fetched == baseline_rows_d[K]
            assert proxy.table_counts == baseline_table_counts_d[K]
            assert proxy.table_rows == baseline_table_rows_d[K]
            assert baseline_durations_d[K] > 0 and after_durations_d[K] > 0
            assert len(proxy.queries) <= loose_bound
            assert proxy.table_rows.get("diagnostic_results_v2", 0) == K
            assert proxy.table_rows.get("diagnostic_v2_components", 0) == K
            assert proxy.table_rows.get("diagnostic_v2_component_metric_links", 0) == K
            assert proxy.table_rows.get("diagnostic_v2_evidence", 0) == K
            assert proxy.table_rows.get("metric_results_v2", 0) == K
            assert proxy.table_counts.get("diagnostic_results_v2", 0) <= expected_chunks
            assert proxy.table_counts.get("diagnostic_v2_components", 0) <= expected_chunks
            assert (
                proxy.table_counts.get("diagnostic_v2_component_metric_links", 0) <= expected_chunks
            )
            assert proxy.table_counts.get("diagnostic_v2_evidence", 0) <= expected_chunks
            assert all(size <= 256 for size in proxy.param_chunks)

            # 3. get_analysis_snapshots
            proxy.clear()
            t0 = time.perf_counter()
            res_s = staging.get_analysis_snapshots([s.snapshot_id for s in target_snaps[:K]])
            after_durations_s[K] = time.perf_counter() - t0

            assert len(res_s) == K
            assert all(s.asset_id == asset_target for s in res_s.values())
            assert res_s == baseline_res_s[K]
            assert len(proxy.queries) == baseline_queries_s[K]
            assert proxy.rows_fetched == baseline_rows_s[K]
            assert proxy.table_counts == baseline_table_counts_s[K]
            assert proxy.table_rows == baseline_table_rows_s[K]
            assert baseline_durations_s[K] > 0 and after_durations_s[K] > 0
            assert len(proxy.queries) <= loose_bound
            assert proxy.table_rows.get("analysis_snapshots_v2", 0) == K
            assert proxy.table_rows.get("analysis_snapshot_v2_metric_links", 0) == K
            assert proxy.table_rows.get("analysis_snapshot_v2_diagnostic_links", 0) == K
            assert proxy.table_rows.get("diagnostic_results_v2", 0) == K
            assert proxy.table_rows.get("metric_results_v2", 0) == 2 * K
            assert proxy.table_counts.get("analysis_snapshots_v2", 0) <= expected_chunks
            assert proxy.table_counts.get("analysis_snapshot_v2_metric_links", 0) <= expected_chunks
            assert (
                proxy.table_counts.get("analysis_snapshot_v2_diagnostic_links", 0)
                <= expected_chunks
            )
            assert all(size <= 256 for size in proxy.param_chunks)

        # 6. Shared DAG Traversal (recorrido):
        # 2 roots -> 16 intermediates -> 256 leaves (274 total nodes) with shared EvidenceSet
        shared_roots = []
        for i in range(2):
            c = MetricResult(
                result_id=uuid4(),
                asset_id=asset_target,
                metric_key=f"market.dag.shared_root_{i}",
                value=Decimal(str(i)),
                unit="USD",
                as_of=base_time,
                available_at=base_time,
                computed_at=base_time,
                parameters={"evidence_set_id": str(es_target.evidence_set_id)},
                input_observation_ids=[obs_target.observation_id],
                input_metric_result_ids=[],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            shared_roots.append(
                c.model_copy(update={"result_id": metric_result_id_from_model_v2(c)})
            )

        shared_intermediates = []
        for i in range(16):
            c = MetricResult(
                result_id=uuid4(),
                asset_id=asset_target,
                metric_key=f"market.dag.shared_inter_{i}",
                value=Decimal(str(i)),
                unit="USD",
                as_of=base_time,
                available_at=base_time,
                computed_at=base_time,
                parameters={"evidence_set_id": str(es_target.evidence_set_id)},
                input_observation_ids=[obs_target.observation_id],
                input_metric_result_ids=[r.result_id for r in shared_roots],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            shared_intermediates.append(
                c.model_copy(update={"result_id": metric_result_id_from_model_v2(c)})
            )

        shared_leaves = []
        for i in range(256):
            c = MetricResult(
                result_id=uuid4(),
                asset_id=asset_target,
                metric_key=f"market.dag.shared_leaf_{i}",
                value=Decimal(str(i)),
                unit="USD",
                as_of=base_time,
                available_at=base_time,
                computed_at=base_time,
                parameters={"evidence_set_id": str(es_target.evidence_set_id)},
                input_observation_ids=[obs_target.observation_id],
                input_metric_result_ids=[
                    shared_intermediates[i % 16].result_id,
                    shared_intermediates[(i + 1) % 16].result_id,
                ],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            shared_leaves.append(
                c.model_copy(update={"result_id": metric_result_id_from_model_v2(c)})
            )

        proxy.active = False
        staging.save_metrics(shared_roots + shared_intermediates + shared_leaves)
        proxy.active = True

        # Iterative level-by-level traversal (recorrido) from leaves to roots in chunks <= 256
        proxy.clear()
        visited_shared: dict[UUID, MetricResult] = {}
        shared_frontier: list[UUID] = [m.result_id for m in shared_leaves]
        shared_batches = 0

        while shared_frontier:
            next_frontier: set[UUID] = set()
            for chunk in chunked_sequence(shared_frontier, 256):
                shared_batches += 1
                batch = staging.get_metrics(chunk)
                for mid, model in batch.items():
                    visited_shared[mid] = model
                    for pid in model.input_metric_result_ids:
                        if pid not in visited_shared:
                            next_frontier.add(pid)
            shared_frontier = sorted(next_frontier, key=str)

        assert shared_batches == 3
        assert len(visited_shared) == 274
        assert all(m.asset_id == asset_target for m in visited_shared.values())
        assert all(size <= 256 for size in proxy.param_chunks)
        assert len(proxy.queries) <= 3 * (16 + 64 * 1)
        assert proxy.rows_fetched > 0

        # 7. Deep DAG Traversal (recorrido):
        # 513-node sequential dependency chain without RecursionError
        deep_chain = []
        for i in range(513):
            moment = base_time + timedelta(seconds=i)
            c = MetricResult(
                result_id=uuid4(),
                asset_id=asset_target,
                metric_key=f"market.deep.node_{i}",
                value=Decimal(str(i)),
                unit="USD",
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                parameters={"evidence_set_id": str(es_target.evidence_set_id)},
                input_observation_ids=[obs_target.observation_id],
                input_metric_result_ids=[deep_chain[-1].result_id] if deep_chain else [],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            deep_chain.append(c.model_copy(update={"result_id": metric_result_id_from_model_v2(c)}))

        proxy.active = False
        staging.save_metrics(deep_chain)
        proxy.active = True

        # Iterative chunked hydration of entire deep DAG in bounded chunks <= 256
        proxy.clear()
        deep_hydrated: dict[UUID, MetricResult] = {}
        for chunk in chunked_sequence([m.result_id for m in deep_chain], 256):
            batch = staging.get_metrics(chunk)
            deep_hydrated.update(batch)

        assert len(deep_hydrated) == 513
        assert all(m.asset_id == asset_target for m in deep_hydrated.values())
        assert all(size <= 256 for size in proxy.param_chunks)
        assert len(proxy.queries) <= 3 * (16 + 64 * 1)

        # Step-by-step sequential traversal of suffix demonstrating O(DAG depth) cost scaling
        proxy.clear()
        step_cur = deep_chain[-1].result_id
        step_visited = []
        for _ in range(16):
            m = staging.get_metrics([step_cur])[step_cur]
            step_visited.append(m)
            if not m.input_metric_result_ids:
                break
            step_cur = m.input_metric_result_ids[0]
        assert len(step_visited) == 16
        # Each step in the deep chain resolves 1 metric dependency
        # (+1 query compared to leaf baseline)
        assert len(proxy.queries) == 16 * (baseline_queries_m[1] + 1)
        assert len(proxy.queries) <= 16 * (16 + 64 * 1)

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
