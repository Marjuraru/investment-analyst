"""Focused contracts for the bounded operational-state transfer adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.workspace.workspace_v2_lifecycle_models import FrozenSourceCut
from investment_analyst.workspace.workspace_v2_operational_state import (
    OperationalStateTransferError,
    inventory_operational_state,
    seal_operational_state,
    transfer_operational_state,
)

_MOMENT = datetime(2026, 10, 15, tzinfo=UTC)


def _source(root: Path) -> FrozenSourceCut:
    return FrozenSourceCut(
        operation_id=uuid4(),
        source_root=root.resolve(),
        workspace_id=uuid4(),
        workspace_format_version=1,
        created_at=_MOMENT,
        source_fingerprint="a" * 64,
        frozen_at=_MOMENT,
    )


def _fixture(root: Path) -> None:
    (root / "state").mkdir(parents=True)
    (root / "storage/data/processed/cazatiburones_activity_events_v1").mkdir(parents=True)
    (root / "state/aapl_daily_run_state.json").write_text('{"terminal":true}\n', encoding="utf-8")
    (root / "state/multi_asset_schedule_state_v1.json").write_text("{}\n", encoding="utf-8")
    journal = root / "state/multi_asset_schedule_state_v1_journal"
    journal.mkdir()
    (journal / "manifest.json").write_text("{}\n", encoding="utf-8")
    (journal / ".legacy_v1_preserved").write_text("legacy\n", encoding="utf-8")
    (root / "storage/data/processed/cazatiburones_activity_events_v1/event.jsonl").write_text(
        '{"event":"x"}\n', encoding="utf-8"
    )


def _seal(source_root: Path, destination_root: Path, inventory: Path):
    return seal_operational_state(
        source_root,
        destination_root,
        source=_source(source_root),
        destination_workspace_id=uuid4(),
        inventory_path=inventory,
        clock=lambda: _MOMENT,
    )


def test_sealed_operational_state_transfers_hidden_journal_and_is_a_noop_after_receipt(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    _fixture(source_root)
    inventory = tmp_path / "inventory.jsonl"
    frozen = _source(source_root)
    destination_workspace_id = uuid4()
    seal = seal_operational_state(
        source_root,
        destination_root,
        source=frozen,
        destination_workspace_id=destination_workspace_id,
        inventory_path=inventory,
        clock=lambda: _MOMENT,
    )
    assert (
        seal_operational_state(
            source_root,
            destination_root,
            source=frozen,
            destination_workspace_id=destination_workspace_id,
            inventory_path=inventory,
            clock=lambda: _MOMENT,
        )
        == seal
    )

    receipt = transfer_operational_state(
        source_root,
        destination_root,
        seal=seal,
        inventory_path=inventory,
        checkpoint_path=tmp_path / "checkpoint.json",
        receipt_path=tmp_path / "receipt.json",
        available_bytes=lambda: 10_000_000,
        reserve_bytes=1,
        clock=lambda: _MOMENT,
    )

    assert receipt.artifact_count == 5
    assert receipt.transferred_bytes > 0
    assert (
        destination_root / "state/multi_asset_schedule_state_v1_journal/.legacy_v1_preserved"
    ).is_file()
    assert (
        transfer_operational_state(
            source_root,
            destination_root,
            seal=seal,
            inventory_path=inventory,
            checkpoint_path=tmp_path / "checkpoint.json",
            receipt_path=tmp_path / "receipt.json",
            available_bytes=lambda: 10_000_000,
            reserve_bytes=1,
            clock=lambda: datetime(2026, 10, 16, tzinfo=UTC),
        )
        == receipt
    )


def test_transfer_resumes_from_durable_cursor_without_resealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    _fixture(source_root)
    inventory = tmp_path / "inventory.jsonl"
    seal = _seal(source_root, destination_root, inventory)
    import investment_analyst.workspace.workspace_v2_operational_state as adapter

    original = adapter.copy_artifact
    calls = 0

    def interrupting_copy(*args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "copy_artifact", interrupting_copy)
    with pytest.raises(RuntimeError, match="interrupted"):
        transfer_operational_state(
            source_root,
            destination_root,
            seal=seal,
            inventory_path=inventory,
            checkpoint_path=tmp_path / "checkpoint.json",
            receipt_path=tmp_path / "receipt.json",
            available_bytes=lambda: 10_000_000,
            reserve_bytes=1,
            clock=lambda: _MOMENT,
        )
    assert (tmp_path / "checkpoint.json").is_file()

    monkeypatch.setattr(adapter, "copy_artifact", original)
    receipt = transfer_operational_state(
        source_root,
        destination_root,
        seal=seal,
        inventory_path=inventory,
        checkpoint_path=tmp_path / "checkpoint.json",
        receipt_path=tmp_path / "receipt.json",
        available_bytes=lambda: 10_000_000,
        reserve_bytes=1,
        clock=lambda: _MOMENT,
    )
    assert receipt.artifact_count == 5
    assert not (tmp_path / "checkpoint.json").exists()


def test_seal_and_transfer_fail_closed_for_changed_source_or_corrupt_inventory(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    _fixture(source_root)
    inventory = tmp_path / "inventory.jsonl"
    seal = _seal(source_root, destination_root, inventory)
    (source_root / "state/aapl_daily_run_state.json").write_text("changed\n", encoding="utf-8")
    with pytest.raises(OperationalStateTransferError, match="sealed source artifact changed"):
        transfer_operational_state(
            source_root,
            destination_root,
            seal=seal,
            inventory_path=inventory,
            checkpoint_path=tmp_path / "checkpoint.json",
            receipt_path=tmp_path / "receipt.json",
            available_bytes=lambda: 10_000_000,
            reserve_bytes=1,
        )
    assert len(tuple(inventory_operational_state(source_root))) == 5
    inventory.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(OperationalStateTransferError, match="inventory line is malformed"):
        transfer_operational_state(
            source_root,
            destination_root,
            seal=seal,
            inventory_path=inventory,
            checkpoint_path=tmp_path / "checkpoint.json",
            receipt_path=tmp_path / "receipt.json",
            available_bytes=lambda: 10_000_000,
            reserve_bytes=1,
        )


def test_seal_rejects_an_unclassified_hidden_durable_state_entry(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    _fixture(source_root)
    (source_root / "state/.unknown-durable").write_text("state\n", encoding="utf-8")

    with pytest.raises(OperationalStateTransferError, match="unclassified durable"):
        _seal(source_root, destination_root, tmp_path / "inventory.jsonl")


def test_transfer_preserves_checkpoint_when_current_capacity_cannot_fit_next_unit(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    _fixture(source_root)
    inventory = tmp_path / "inventory.jsonl"
    seal = _seal(source_root, destination_root, inventory)

    with pytest.raises(OperationalStateTransferError, match="capacity is insufficient"):
        transfer_operational_state(
            source_root,
            destination_root,
            seal=seal,
            inventory_path=inventory,
            checkpoint_path=tmp_path / "checkpoint.json",
            receipt_path=tmp_path / "receipt.json",
            available_bytes=lambda: 1,
            reserve_bytes=1,
        )
    assert not (tmp_path / "receipt.json").exists()
