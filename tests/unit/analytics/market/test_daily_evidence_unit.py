"""Tests for the content-addressed daily evidence prefix contract."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceError,
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
    make_daily_evidence_prefix,
    observation_rows_digest,
    verify_daily_evidence_chain,
)
from investment_analyst.core.models.enums import DataQuality


def _node(index: int, parent: DailyEvidencePrefix | None = None) -> DailyEvidencePrefix:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    observation_id = uuid5(NAMESPACE_URL, f"daily-close:{index}")
    rows = [[str(observation_id), str(index), timestamp.isoformat()]]
    return make_daily_evidence_prefix(
        asset_id="equity:us:test",
        source_id="simulated:daily-bars",
        field_group=DailyEvidenceFieldGroup.CLOSE,
        timestamp=timestamp,
        observation_ids=(observation_id,),
        observation_digest=observation_rows_digest(rows),
        current_available_at=timestamp + timedelta(minutes=1),
        quality=DataQuality.VALID,
        parent=parent,
    )


def test_daily_evidence_prefix_identity_commits_parent_and_projection() -> None:
    root = _node(0)
    child = _node(1, root)
    assert child.parent_prefix_id == root.prefix_id
    assert child.parent_hash == root.prefix_hash
    assert child.length == 2
    assert verify_daily_evidence_chain((root, child)) == child

    changed_observation = uuid5(NAMESPACE_URL, "daily-close:changed")
    changed = make_daily_evidence_prefix(
        asset_id=root.asset_id,
        source_id=root.source_id,
        field_group=DailyEvidenceFieldGroup.CLOSE,
        timestamp=root.timestamp,
        observation_ids=(changed_observation,),
        observation_digest=observation_rows_digest(
            [[str(changed_observation), "0", root.timestamp.isoformat()]]
        ),
        current_available_at=root.available_at,
        quality=DataQuality.VALID,
    )
    assert changed.prefix_id != root.prefix_id
    assert _node(1, changed).prefix_id != child.prefix_id


def test_daily_evidence_rejects_inconsistent_parent_or_tampered_content() -> None:
    root = _node(0)
    with pytest.raises(DailyEvidenceError, match="timestamps must advance"):
        _node(0, root)

    document = root.model_dump(mode="python")
    document["observation_digest"] = "0" * 64
    with pytest.raises(ValueError, match="hash does not match"):
        DailyEvidencePrefix.model_validate(document)

    second_root = _node(0)
    other_child = _node(1, second_root)
    with pytest.raises(DailyEvidenceError, match="cycle or duplicate"):
        verify_daily_evidence_chain((root, other_child, root))


def test_daily_evidence_verification_is_iterative_for_long_chains() -> None:
    nodes: list[DailyEvidencePrefix] = []
    parent = None
    for index in range(1100):
        parent = _node(index, parent)
        nodes.append(parent)
    assert verify_daily_evidence_chain(nodes).length == 1100
