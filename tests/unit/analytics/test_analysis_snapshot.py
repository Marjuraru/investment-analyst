"""Unit tests for pure AnalysisSnapshot contract and deterministic identity."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from investment_analyst.analytics.analysis_snapshot import (
    AnalysisSnapshot,
    analysis_snapshot_identity,
    build_analysis_snapshot,
    canonical_evidence_set_digest,
)
from investment_analyst.analytics.evidence_set import canonical_lineage_hash


def test_snapshot_identity_is_pit_and_excludes_creation_clock() -> None:
    """A1: pure AnalysisSnapshot identity is UUIDv8, excludes creation clock, and respects PIT."""
    asset_id = "equity:us:aapl"
    domain = "market"
    cut_1 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    cut_2 = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    policy_version = "v1"

    metric_id_1 = UUID("018d0000-0000-8000-8000-000000000001")
    metric_id_2 = UUID("018d0000-0000-8000-8000-000000000002")
    diagnostic_id = UUID("018d0000-0000-8000-8000-000000000010")

    # When no EvidenceSets exist, digest matches canonical empty sequence hash
    empty_digest = canonical_evidence_set_digest([])
    assert empty_digest == canonical_lineage_hash([])

    created_at_1 = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    created_at_2 = datetime(2026, 8, 11, 9, 30, tzinfo=UTC)

    # 1. Creation clock exclusion: different created_at, identical coordinates -> identical id
    snap_a = build_analysis_snapshot(
        asset_id=asset_id,
        domain=domain,
        known_at=cut_1,
        policy_version=policy_version,
        metric_ids=[metric_id_2, metric_id_1],  # Passed unsorted
        diagnostic_ids=[diagnostic_id],
        evidence_set_digest=empty_digest,
        created_at=created_at_1,
    )

    snap_b = build_analysis_snapshot(
        asset_id=asset_id,
        domain=domain,
        known_at=cut_1,
        policy_version=policy_version,
        metric_ids=[metric_id_1, metric_id_2],
        diagnostic_ids=[diagnostic_id],
        evidence_set_digest=empty_digest,
        created_at=created_at_2,
    )

    assert snap_a.snapshot_id == snap_b.snapshot_id
    assert snap_a.snapshot_id.version == 8
    # References are sorted and deduplicated
    assert snap_a.metric_ids == (metric_id_1, metric_id_2)
    assert snap_b.metric_ids == (metric_id_1, metric_id_2)

    # 2. Point-in-time cut: two distinct cuts produce different snapshot identities
    snap_cut_2 = build_analysis_snapshot(
        asset_id=asset_id,
        domain=domain,
        known_at=cut_2,
        policy_version=policy_version,
        metric_ids=[metric_id_1, metric_id_2],
        diagnostic_ids=[diagnostic_id],
        evidence_set_digest=empty_digest,
        created_at=created_at_1,
    )
    assert snap_cut_2.snapshot_id != snap_a.snapshot_id

    # 3. Evidence set digest: with actual EvidenceSet canonical hashes
    hash_1 = "a" * 64
    hash_2 = "b" * 64
    digest_with_sets = canonical_evidence_set_digest([hash_2, hash_1, hash_1])
    assert digest_with_sets == canonical_evidence_set_digest([hash_1, hash_2])
    assert digest_with_sets != empty_digest

    snap_with_sets = build_analysis_snapshot(
        asset_id=asset_id,
        domain=domain,
        known_at=cut_1,
        policy_version=policy_version,
        metric_ids=[metric_id_1],
        diagnostic_ids=[],
        evidence_set_hashes=[hash_2, hash_1],
        created_at=created_at_1,
    )
    assert snap_with_sets.evidence_set_digest == digest_with_sets
    assert snap_with_sets.snapshot_id != snap_a.snapshot_id


def test_snapshot_model_validations() -> None:
    """Validate ordering, uniqueness, and deterministic ID enforcement."""
    asset_id = "equity:us:aapl"
    domain = "market"
    known_at = datetime(2026, 8, 1, tzinfo=UTC)
    policy_version = "v1"
    digest = canonical_evidence_set_digest([])
    created_at = datetime(2026, 8, 2, tzinfo=UTC)

    u1 = UUID("00000000-0000-0000-0000-000000000001")
    u2 = UUID("00000000-0000-0000-0000-000000000002")

    # Mismatched snapshot_id raises ValueError
    with pytest.raises(ValueError, match="snapshot identity is not deterministic"):
        AnalysisSnapshot(
            snapshot_id=uuid4(),
            asset_id=asset_id,
            domain=domain,
            known_at=known_at,
            policy_version=policy_version,
            metric_ids=(u1, u2),
            diagnostic_ids=(),
            evidence_set_digest=digest,
            created_at=created_at,
        )

    # Unsorted metric_ids raises ValueError
    correct_id = analysis_snapshot_identity(
        asset_id=asset_id,
        domain=domain,
        known_at=known_at,
        policy_version=policy_version,
        metric_ids=(u1, u2),
        diagnostic_ids=(),
        evidence_set_digest=digest,
    )
    with pytest.raises(ValueError, match="metric references must be sorted"):
        AnalysisSnapshot(
            snapshot_id=correct_id,
            asset_id=asset_id,
            domain=domain,
            known_at=known_at,
            policy_version=policy_version,
            metric_ids=(u2, u1),
            diagnostic_ids=(),
            evidence_set_digest=digest,
            created_at=created_at,
        )

    # Duplicate metric_ids raises ValueError
    with pytest.raises(ValueError, match="metric references must not contain duplicates"):
        AnalysisSnapshot(
            snapshot_id=correct_id,
            asset_id=asset_id,
            domain=domain,
            known_at=known_at,
            policy_version=policy_version,
            metric_ids=(u1, u1),
            diagnostic_ids=(),
            evidence_set_digest=digest,
            created_at=created_at,
        )

    # Naive datetime raises ValueError
    with pytest.raises(ValueError, match="timezone"):
        analysis_snapshot_identity(
            asset_id=asset_id,
            domain=domain,
            known_at=datetime(2026, 8, 1),
            policy_version=policy_version,
            metric_ids=(),
            diagnostic_ids=(),
            evidence_set_digest=digest,
        )
