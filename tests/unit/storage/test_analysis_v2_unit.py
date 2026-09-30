"""Unit tests for analytical v2 storage and integrity."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.analytics.analysis_snapshot import (
    build_analysis_snapshot,
)
from investment_analyst.analytics.metric_identity_v2 import (
    metric_result_id_from_model_v2,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
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
    AnalysisSnapshotV2Error,
)
from investment_analyst.storage.errors import (
    RecordNotFoundError,
)
from investment_analyst.storage.metric_v2 import MetricV2Error
from investment_analyst.storage.raw_v2 import RawV2Staging


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _seed_observation(
    staging: RawV2Staging, moment: datetime, asset_id: str
) -> NormalizedObservation:
    raw = RawRecord(
        record_id=uuid4(),
        asset_id=asset_id,
        source=SourceReference(
            source_id="test:source",
            record_key=f"obs-{moment.isoformat()}",
            retrieved_at=moment,
        ),
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"v": 1},
        schema_version="v1",
    )
    staging.save(raw)
    obs = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name="close",
        value=Decimal("150.00"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=moment,
        available_at=moment,
        normalized_at=moment,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    staging.save_observations([obs])
    return obs


def test_empty_collections_and_failed_lot_preserve_prior_progress(tmp_path: Path) -> None:
    """Empty collections return BatchWriteReceipt() and failed lots preserve prior progress."""
    staging = _staging(tmp_path, "staging-empty-and-failures")
    base_time = datetime(2026, 9, 1, tzinfo=UTC)
    asset_id = "equity:us:aapl"

    with staging:
        # 1. Empty collections return empty BatchWriteReceipt without errors
        r_met = staging.save_metrics([])
        assert isinstance(r_met, BatchWriteReceipt)
        assert r_met.created_count == 0
        assert r_met.reused_count == 0

        r_diag = staging.save_diagnostics([])
        assert isinstance(r_diag, BatchWriteReceipt)
        assert r_diag.created_count == 0
        assert r_diag.reused_count == 0

        r_snap = staging.save_analysis_snapshots([])
        assert isinstance(r_snap, BatchWriteReceipt)
        assert r_snap.created_count == 0
        assert r_snap.reused_count == 0

        # 2. Seed valid metric
        obs = _seed_observation(staging, base_time, asset_id)
        m1_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.close.v1",
            value=Decimal("150.00"),
            unit="USD",
            as_of=base_time,
            available_at=base_time,
            computed_at=base_time,
            parameters={},
            input_observation_ids=[obs.observation_id],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        m1 = m1_cand.model_copy(update={"result_id": metric_result_id_from_model_v2(m1_cand)})
        receipt_m1 = staging.save_metrics([m1])
        assert receipt_m1.created_count == 1

        # 3. Failed metric lot: references missing observation
        invalid_metric_cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.close.v1",
            value=Decimal("200.00"),
            unit="USD",
            as_of=base_time + timedelta(days=1),
            available_at=base_time + timedelta(days=1),
            computed_at=base_time + timedelta(days=1),
            parameters={},
            input_observation_ids=[uuid4()],  # Missing observation
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        invalid_metric = invalid_metric_cand.model_copy(
            update={"result_id": metric_result_id_from_model_v2(invalid_metric_cand)}
        )
        with pytest.raises(MetricV2Error, match="missing observation"):
            staging.save_metrics([invalid_metric])

        # Prior progress intact: m1 is still there and matches
        assert staging.get_metrics([m1.result_id])[m1.result_id] == m1

        # 4. Save valid diagnostic
        d1 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=m1.as_of,
            available_at=m1.available_at,
            computed_at=m1.computed_at,
            components=[
                DiagnosticComponent(
                    component_key="trend",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[m1.result_id],
                    explanation="Uptrend",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=m1.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="High price",
                )
            ],
            algorithm_version="v1",
            summary="Market check",
            quality=DataQuality.VALID,
        )
        receipt_d1 = staging.save_diagnostics([d1])
        assert receipt_d1.created_count == 1

        # 5. Failed diagnostic lot: references missing metric
        invalid_diag = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
            final_score=Decimal("0.0"),
            confidence=Decimal("0.0"),
            as_of=m1.as_of,
            available_at=m1.available_at,
            computed_at=m1.computed_at,
            components=[],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=uuid4(),  # Missing metric
                    direction=EvidenceDirection.NEUTRAL,
                    contribution=Decimal("0.0"),
                    reason="Missing",
                )
            ],
            algorithm_version="v1",
            summary="Bad diag",
            quality=DataQuality.VALID,
        )
        with pytest.raises(RecordNotFoundError):
            staging.save_diagnostics([invalid_diag])

        # Prior diagnostic progress intact: d1 is still there
        assert staging.get_diagnostics([d1.diagnostic_id])[d1.diagnostic_id] == d1

        # 6. Save valid snapshot
        s1 = build_analysis_snapshot(
            asset_id=asset_id,
            domain="market",
            known_at=m1.available_at,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[d1.diagnostic_id],
            evidence_set_hashes=[],
            created_at=base_time,
        )
        receipt_s1 = staging.save_analysis_snapshots([s1])
        assert receipt_s1.created_count == 1

        # 7. Failed snapshot lot: references future diagnostic or foreign metric
        foreign_snap = build_analysis_snapshot(
            asset_id="equity:us:msft",  # Foreign asset
            domain="market",
            known_at=m1.available_at,
            policy_version="v1",
            metric_ids=[m1.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[],
            created_at=base_time,
        )
        with pytest.raises(AnalysisSnapshotV2Error, match="foreign metric"):
            staging.save_analysis_snapshots([foreign_snap])

        # Prior snapshot progress intact: s1 is still there
        assert staging.get_analysis_snapshot(s1.snapshot_id) == s1
