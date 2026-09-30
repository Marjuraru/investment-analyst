"""Integration tests for diagnostic v2 and analysis snapshot v2 staging substrate."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.analytics.analysis_snapshot import (
    build_analysis_snapshot,
    canonical_evidence_set_digest,
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
    ANALYSIS_SNAPSHOT_V2_TABLE,
    AnalysisSnapshotV2Error,
)
from investment_analyst.storage.diagnostic_v2 import (
    DIAGNOSTIC_V2_TABLE,
    DiagnosticV2Error,
)
from investment_analyst.storage.errors import (
    RecordConflictError,
    RecordNotFoundError,
)
from investment_analyst.storage.raw_v2 import RawV2Staging


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _setup_metrics_and_evidence(
    staging: RawV2Staging,
    base_time: datetime,
    asset_id: str = "crypto:btc-usd",
) -> tuple[list[MetricResult], str]:
    """Helper to populate observations, evidence set and metrics in staging."""
    observations: list[NormalizedObservation] = []
    for index in range(24):
        moment = base_time + timedelta(hours=index)
        raw = RawRecord(
            record_id=uuid4(),
            asset_id=asset_id,
            source=SourceReference(
                source_id="deribit:funding",
                record_key=f"obs-{index}",
                retrieved_at=moment,
            ),
            event_time=moment,
            available_at=moment,
            received_at=moment,
            payload={"v": str(index)},
            schema_version="import-v1",
        )
        staging.save(raw)
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw.record_id,
                asset_id=asset_id,
                field_name="funding_rate",
                value=Decimal("0.01"),
                unit="rate",
                frequency=DataFrequency.HOUR_1,
                observed_at=moment,
                available_at=moment,
                normalized_at=moment,
                source=raw.source,
                quality=DataQuality.VALID,
                transformation_version="1.0.0",
            )
        )
    staging.save_observations(observations)
    segments = build_evidence_segments(observations)
    staging.save_evidence_segments(segments)
    evidence_set = build_evidence_set(observations, segments=segments)
    staging.save_evidence_set(evidence_set)

    # Metric 1
    m1_candidate = MetricResult(
        result_id=uuid4(),
        asset_id=asset_id,
        metric_key="funding.sum_1h",
        value=Decimal("1.50"),
        unit="rate",
        as_of=base_time + timedelta(hours=23),
        available_at=evidence_set.available_at,
        computed_at=evidence_set.available_at,
        parameters={
            "window": len(observations),
            "evidence_set_id": str(evidence_set.evidence_set_id),
        },
        input_observation_ids=[item.observation_id for item in observations],
        algorithm_version="v1",
        quality=DataQuality.VALID,
    )
    m1 = m1_candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(m1_candidate)})

    # Metric 2 (available 1 hour later)
    m2_candidate = MetricResult(
        result_id=uuid4(),
        asset_id=asset_id,
        metric_key="funding.mean_1h",
        value=Decimal("0.0625"),
        unit="rate",
        as_of=base_time + timedelta(hours=23),
        available_at=evidence_set.available_at + timedelta(hours=1),
        computed_at=evidence_set.available_at + timedelta(hours=1),
        parameters={
            "window": len(observations),
            "evidence_set_id": str(evidence_set.evidence_set_id),
        },
        input_observation_ids=[item.observation_id for item in observations],
        algorithm_version="v1",
        quality=DataQuality.VALID,
    )
    m2 = m2_candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(m2_candidate)})

    staging.save_metrics([m1, m2])
    return [m1, m2], evidence_set.canonical_hash


def test_diagnostic_and_snapshot_round_trip_two_pit_cuts(tmp_path: Path) -> None:
    """A2: round-trip diagnostics and snapshots across two PIT cuts without document_json."""
    staging = _staging(tmp_path, "staging-round-trip")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "crypto:btc-usd"

    with staging:
        metrics, es_hash = _setup_metrics_and_evidence(staging, base_time, asset_id=asset_id)
        m1, m2 = metrics[0], metrics[1]
        es_digest = canonical_evidence_set_digest([es_hash])

        # Cut 1: visible up to m1.available_at
        cut_1 = m1.available_at
        diag_1 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.NEUTRAL,
            final_score=Decimal("50.0"),
            confidence=Decimal("0.85"),
            as_of=m1.as_of,
            available_at=cut_1,
            computed_at=cut_1,
            components=[
                DiagnosticComponent(
                    component_key="funding_pressure",
                    score=Decimal("50.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("50.0"),
                    metric_result_ids=[m1.result_id],
                    explanation="Neutral funding rate pressure",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m1.result_id,
                    direction=EvidenceDirection.NEUTRAL,
                    contribution=Decimal("0.0"),
                    reason="Funding sum is near median",
                )
            ],
            algorithm_version="diag-v1",
            summary="Market diagnostic at cut 1",
            quality=DataQuality.VALID,
        )
        diag_receipt_1 = staging.save_diagnostics([diag_1])
        assert diag_receipt_1.created_count == 1

        snap_1 = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=cut_1,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[diag_1.diagnostic_id],
            evidence_set_hashes=[es_hash],
            created_at=datetime(2026, 8, 2, 10, 0, tzinfo=UTC),
        )
        snap_receipt_1 = staging.save_analysis_snapshots([snap_1])
        assert snap_receipt_1.created_count == 1

        # Cut 2: visible up to m2.available_at (cut_2 > cut_1)
        cut_2 = m2.available_at
        assert cut_2 > cut_1
        diag_2 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("75.0"),
            confidence=Decimal("0.90"),
            as_of=m2.as_of,
            available_at=cut_2,
            computed_at=cut_2,
            components=[
                DiagnosticComponent(
                    component_key="funding_mean",
                    score=Decimal("75.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("75.0"),
                    metric_result_ids=[m2.result_id],
                    explanation="Positive funding rate",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m2.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("25.0"),
                    reason="Funding mean elevated",
                )
            ],
            algorithm_version="diag-v1",
            summary="Market diagnostic at cut 2",
            quality=DataQuality.VALID,
        )
        diag_receipt_2 = staging.save_diagnostics([diag_2])
        assert diag_receipt_2.created_count == 1

        snap_2 = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=cut_2,
            policy_version="v1",
            metric_ids=[m1.result_id, m2.result_id],
            diagnostic_ids=[diag_1.diagnostic_id, diag_2.diagnostic_id],
            evidence_set_hashes=[es_hash],
            created_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        )
        snap_receipt_2 = staging.save_analysis_snapshots([snap_2])
        assert snap_receipt_2.created_count == 1

        # Two cuts recover exactly their references
        read_snap_1 = staging.get_analysis_snapshot(snap_1.snapshot_id)
        assert read_snap_1.metric_ids == (m1.result_id,)
        assert read_snap_1.diagnostic_ids == (diag_1.diagnostic_id,)
        assert read_snap_1.evidence_set_digest == es_digest

        read_snap_2 = staging.get_analysis_snapshot(snap_2.snapshot_id)
        assert set(read_snap_2.metric_ids) == {m1.result_id, m2.result_id}
        assert set(read_snap_2.diagnostic_ids) == {diag_1.diagnostic_id, diag_2.diagnostic_id}

        # PIT filtering
        snaps_at_cut_1 = staging.list_analysis_snapshots(known_to=cut_1)
        assert len(snaps_at_cut_1) == 1
        assert snaps_at_cut_1[0].snapshot_id == snap_1.snapshot_id

        snaps_at_cut_2 = staging.list_analysis_snapshots(known_to=cut_2)
        assert len(snaps_at_cut_2) == 2

        # Idempotence: save identical snapshot with different created_at reuses first row
        snap_1_variant = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=cut_1,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[diag_1.diagnostic_id],
            evidence_set_hashes=[es_hash],
            created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),  # Different clock
        )
        assert snap_1_variant.snapshot_id == snap_1.snapshot_id
        reuse_receipt = staging.save_analysis_snapshots([snap_1_variant])
        assert reuse_receipt.created_count == 0
        assert reuse_receipt.reused_count == 1
        # Preserves original created_at
        assert staging.get_analysis_snapshot(snap_1.snapshot_id).created_at == snap_1.created_at

        # Verify no document_json exists in any diagnostic or snapshot tables
        for table in (
            DIAGNOSTIC_V2_TABLE,
            ANALYSIS_SNAPSHOT_V2_TABLE,
            "diagnostic_v2_components",
            "diagnostic_v2_evidence",
            "analysis_snapshot_v2_metric_links",
            "analysis_snapshot_v2_diagnostic_links",
        ):
            col_rows = staging._connection.execute(
                f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"
            ).fetchall()
            columns = {str(r[0]) for r in col_rows}
            assert "document_json" not in columns


def test_missing_future_foreign_or_mutated_references_fail_closed(tmp_path: Path) -> None:
    """X1: missing, future, foreign or mutated references fail closed."""
    staging = _staging(tmp_path, "staging-negatives")
    base_time = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "crypto:btc-usd"

    with staging:
        metrics, es_hash = _setup_metrics_and_evidence(staging, base_time, asset_id=asset_id)
        m1 = metrics[0]
        valid_cut = m1.available_at

        # 1. Missing metric reference in diagnostic
        missing_mid = uuid4()
        diag_missing_metric = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=m1.as_of,
            available_at=valid_cut,
            computed_at=valid_cut,
            components=[],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=missing_mid,
                    direction=EvidenceDirection.NEUTRAL,
                    contribution=Decimal("0.0"),
                    reason="Missing metric",
                )
            ],
            algorithm_version="v1",
            summary="Negative test",
            quality=DataQuality.VALID,
        )
        with pytest.raises(RecordNotFoundError, match="missing metric"):
            staging.save_diagnostics([diag_missing_metric])

        # 2. Future metric reference in diagnostic
        diag_future_metric = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=m1.as_of,
            available_at=m1.available_at - timedelta(hours=1),  # Earlier than metric available_at
            computed_at=m1.computed_at,
            components=[],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m1.result_id,
                    direction=EvidenceDirection.NEUTRAL,
                    contribution=Decimal("0.0"),
                    reason="Future metric",
                )
            ],
            algorithm_version="v1",
            summary="Negative test",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="future metric"):
            staging.save_diagnostics([diag_future_metric])

        # 3. Foreign metric reference in diagnostic (asset mismatch)
        diag_foreign_metric = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id="equity:us:aapl",  # Foreign asset
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=m1.as_of,
            available_at=valid_cut,
            computed_at=valid_cut,
            components=[],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m1.result_id,
                    direction=EvidenceDirection.NEUTRAL,
                    contribution=Decimal("0.0"),
                    reason="Foreign metric",
                )
            ],
            algorithm_version="v1",
            summary="Negative test",
            quality=DataQuality.VALID,
        )
        with pytest.raises(DiagnosticV2Error, match="foreign metric"):
            staging.save_diagnostics([diag_foreign_metric])

        # Save a valid diagnostic for snapshot tests
        valid_diag = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=m1.as_of,
            available_at=valid_cut,
            computed_at=valid_cut,
            components=[],
            evidence=[],
            algorithm_version="v1",
            summary="Valid diagnostic",
            quality=DataQuality.VALID,
        )
        staging.save_diagnostics([valid_diag])

        # 4. Diagnostic content conflict (same ID, different content)
        conflicting_diag = valid_diag.model_copy(update={"summary": "Mutated summary"})
        with pytest.raises(RecordConflictError, match="diagnostic content conflict"):
            staging.save_diagnostics([conflicting_diag])

        # 5. Missing metric in snapshot
        snap_missing_metric = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=valid_cut,
            policy_version="v1",
            metric_ids=[uuid4()],
            diagnostic_ids=[valid_diag.diagnostic_id],
            evidence_set_hashes=[],
            created_at=valid_cut,
        )
        with pytest.raises(RecordNotFoundError, match="missing metric"):
            staging.save_analysis_snapshots([snap_missing_metric])

        # 6. Future metric in snapshot (cut is before metric available_at)
        snap_future_metric = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=m1.available_at - timedelta(hours=1),
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[es_hash],
            created_at=valid_cut,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="future metric"):
            staging.save_analysis_snapshots([snap_future_metric])

        # 7. Foreign metric in snapshot
        snap_foreign_metric = build_analysis_snapshot(
            asset_id="equity:us:aapl",
            domain="derivatives",
            known_at=valid_cut,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[es_hash],
            created_at=valid_cut,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="foreign metric"):
            staging.save_analysis_snapshots([snap_foreign_metric])

        # 8. Mismatched EvidenceSet digest in snapshot
        snap_bad_digest = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=valid_cut,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[],
            evidence_set_digest="0" * 64,  # Contradicts the metric's referenced EvidenceSet
            created_at=valid_cut,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="does not match resolved hashes digest"):
            staging.save_analysis_snapshots([snap_bad_digest])
