"""Unit tests for scheduled Form 13F cycle state persistence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from investment_analyst.application.sec_institutional_cycle_state import (
    SEC_INSTITUTIONAL_CYCLE_STATE_SCHEMA_VERSION,
    SecInstitutionalCycleState,
    SecInstitutionalCycleStateError,
    SecInstitutionalCycleStateStore,
)


def test_absent_state_file_returns_initial_state_without_creating_file() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "state" / "sec_institutional_cycle_state_v1.json"
        store = SecInstitutionalCycleStateStore(state_path)

        state = store.load()

        assert not state_path.exists()
        assert state.schema_version == SEC_INSTITUTIONAL_CYCLE_STATE_SCHEMA_VERSION
        assert state.manager_cursor == 0
        assert state.snapshot_id is None
        assert state.cycle_count == 0


def test_valid_state_survives_restart() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "state" / "sec_institutional_cycle_state_v1.json"
        store = SecInstitutionalCycleStateStore(state_path)

        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        snapshot_id = uuid4()
        initial = SecInstitutionalCycleState(
            updated_at=now,
            dataset_period_start=date(2026, 4, 1),
            dataset_period_end=date(2026, 6, 30),
            dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01apr2026-30jun2026_form13f.zip",
            dataset_sha256="b" * 64,
            dataset_last_validated_at=now,
            snapshot_id=snapshot_id,
            manager_cursor=2,
            total_managers=10,
            cycle_count=3,
            last_processed_cik="0001067983",
            last_status="success",
        )

        written = store.write(initial)
        assert state_path.exists()
        assert written.checksum_sha256 is not None

        # Reopen and reload
        store_reloaded = SecInstitutionalCycleStateStore(state_path)
        loaded = store_reloaded.load()

        assert loaded.schema_version == initial.schema_version
        assert loaded.snapshot_id == snapshot_id
        assert loaded.manager_cursor == 2
        assert loaded.total_managers == 10
        assert loaded.cycle_count == 3
        assert loaded.last_processed_cik == "0001067983"
        assert loaded.last_status == "success"
        assert loaded.checksum_sha256 == written.checksum_sha256


def test_truncated_state_bytes_fail_closed_without_overwriting() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "state" / "sec_institutional_cycle_state_v1.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        corrupted_bytes = b'{"schema_version": "sec-institutional-cycle-state-v1", "upda'
        state_path.write_bytes(corrupted_bytes)

        store = SecInstitutionalCycleStateStore(state_path)

        with pytest.raises(SecInstitutionalCycleStateError):
            store.load()

        # Verify file is not deleted or overwritten
        assert state_path.exists()
        assert state_path.read_bytes() == corrupted_bytes


def test_checksum_mismatch_fails_closed_without_overwriting() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "state" / "sec_institutional_cycle_state_v1.json"
        store = SecInstitutionalCycleStateStore(state_path)

        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        initial = SecInstitutionalCycleState(
            updated_at=now,
            manager_cursor=0,
            snapshot_id=None,
        )
        store.write(initial)

        # Tamper with file
        raw_text = state_path.read_text(encoding="utf-8")
        tampered_text = raw_text.replace('"cycle_count":0', '"cycle_count":999')
        state_path.write_text(tampered_text, encoding="utf-8")

        with pytest.raises(SecInstitutionalCycleStateError, match="state checksum mismatch"):
            store.load()

        assert state_path.read_text(encoding="utf-8") == tampered_text


def test_contradictory_snapshot_cursor_fails_closed() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "state" / "sec_institutional_cycle_state_v1.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)

        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

        # Case 1: cursor > 0 but snapshot is None
        bad_state_1 = {
            "schema_version": "sec-institutional-cycle-state-v1",
            "updated_at": now.isoformat(),
            "snapshot_id": None,
            "manager_cursor": 5,
        }
        state_path.write_text(json.dumps(bad_state_1), encoding="utf-8")
        store = SecInstitutionalCycleStateStore(state_path)
        with pytest.raises(SecInstitutionalCycleStateError, match="manager cursor must be zero"):
            store.load()

        # Case 2: snapshot present but missing dates
        bad_state_2 = {
            "schema_version": "sec-institutional-cycle-state-v1",
            "updated_at": now.isoformat(),
            "snapshot_id": str(uuid4()),
            "manager_cursor": 0,
        }
        state_path.write_text(json.dumps(bad_state_2), encoding="utf-8")
        with pytest.raises(
            SecInstitutionalCycleStateError, match="dataset metadata must be complete"
        ):
            store.load()

        # Case 3: cursor exceeds total_managers
        bad_state_3 = {
            "schema_version": "sec-institutional-cycle-state-v1",
            "updated_at": now.isoformat(),
            "snapshot_id": str(uuid4()),
            "dataset_period_start": "2026-04-01",
            "dataset_period_end": "2026-06-30",
            "dataset_sha256": "c" * 64,
            "dataset_last_validated_at": now.isoformat(),
            "manager_cursor": 15,
            "total_managers": 10,
        }
        state_path.write_text(json.dumps(bad_state_3), encoding="utf-8")
        with pytest.raises(
            SecInstitutionalCycleStateError, match="manager cursor cannot exceed total managers"
        ):
            store.load()
