"""Unit tests for two-close institutional 13F history state persistence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from investment_analyst.application.sec_institutional_history_state import (
    SEC_INSTITUTIONAL_HISTORY_STATE_SCHEMA_VERSION,
    SecInstitutionalHistoryDatasetState,
    SecInstitutionalHistoryState,
    SecInstitutionalHistoryStateError,
    SecInstitutionalHistoryStateStore,
)

_OLDER = SecInstitutionalHistoryDatasetState(
    period_start=date(2025, 12, 1),
    period_end=date(2026, 2, 28),
    dataset_url=(
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
        "01dec2025-28feb2026_form13f.zip"
    ),
    dataset_sha256="a" * 64,
    snapshot_id=uuid4(),
)
_NEWER = SecInstitutionalHistoryDatasetState(
    period_start=date(2026, 3, 1),
    period_end=date(2026, 5, 31),
    dataset_url=(
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
        "01mar2026-31may2026_form13f.zip"
    ),
    dataset_sha256="b" * 64,
    snapshot_id=uuid4(),
)


def _store(temp_dir: str) -> SecInstitutionalHistoryStateStore:
    return SecInstitutionalHistoryStateStore(
        Path(temp_dir) / "state" / "sec_institutional_history_state_v1.json"
    )


def test_absent_state_file_returns_initial_state_without_creating_file() -> None:
    with TemporaryDirectory() as temp_dir:
        store = _store(temp_dir)

        state = store.load()

        assert not store.path.exists()
        assert state.schema_version == SEC_INSTITUTIONAL_HISTORY_STATE_SCHEMA_VERSION
        assert state.phase == "preparing"
        assert state.older is None and state.newer is None
        assert state.target_cursor == 0
        assert state.total_targets is None
        assert state.cycle_count == 0


def test_valid_state_survives_restart_with_pair_phase_and_cursor() -> None:
    with TemporaryDirectory() as temp_dir:
        store = _store(temp_dir)
        now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        initial = SecInstitutionalHistoryState(
            updated_at=now,
            phase="ready",
            older=_OLDER,
            newer=_NEWER,
            target_cursor=2,
            total_targets=6,
            cycle_count=4,
            last_processed_cik="0001067983",
            last_status="success",
        )

        written = store.write(initial)
        assert store.path.exists()
        assert written.checksum_sha256 is not None

        loaded = SecInstitutionalHistoryStateStore(store.path).load()

        assert loaded.older == _OLDER
        assert loaded.newer == _NEWER
        assert loaded.phase == "ready"
        assert loaded.target_cursor == 2
        assert loaded.total_targets == 6
        assert loaded.cycle_count == 4
        assert loaded.last_status == "success"
        assert loaded.checksum_sha256 == written.checksum_sha256


def test_truncated_state_bytes_fail_closed_without_overwriting() -> None:
    with TemporaryDirectory() as temp_dir:
        store = _store(temp_dir)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        corrupted = b'{"schema_version": "sec-institutional-history-state-v1", "upd'
        store.path.write_bytes(corrupted)

        with pytest.raises(SecInstitutionalHistoryStateError):
            store.load()

        assert store.path.read_bytes() == corrupted


def test_checksum_mismatch_fails_closed_without_overwriting() -> None:
    with TemporaryDirectory() as temp_dir:
        store = _store(temp_dir)
        store.write(
            SecInstitutionalHistoryState(
                updated_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
                phase="ready",
                older=_OLDER,
                newer=_NEWER,
                target_cursor=1,
                total_targets=3,
            )
        )
        tampered = store.path.read_text(encoding="utf-8").replace(
            '"cycle_count":0', '"cycle_count":9'
        )
        store.path.write_text(tampered, encoding="utf-8")

        with pytest.raises(SecInstitutionalHistoryStateError, match="state checksum mismatch"):
            store.load()

        assert store.path.read_text(encoding="utf-8") == tampered


def test_non_adjacent_inverted_and_overlapping_windows_fail_closed() -> None:
    with TemporaryDirectory() as temp_dir:
        store = _store(temp_dir)
        now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

        gapped = SecInstitutionalHistoryDatasetState(
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/x.zip",
            dataset_sha256="c" * 64,
            snapshot_id=uuid4(),
        )
        raw = {
            "schema_version": "sec-institutional-history-state-v1",
            "updated_at": now.isoformat(),
            "phase": "ready",
            "older": _OLDER.model_dump(mode="json"),
            "newer": gapped.model_dump(mode="json"),
            "total_targets": 1,
            "target_cursor": 0,
        }
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SecInstitutionalHistoryStateError, match="must be adjacent"):
            store.load()

        raw["older"], raw["newer"] = raw["newer"], raw["older"]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SecInstitutionalHistoryStateError, match="ordered older then newer"):
            store.load()

        overlapping = SecInstitutionalHistoryDatasetState(
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/y.zip",
            dataset_sha256="d" * 64,
            snapshot_id=uuid4(),
        )
        raw["older"] = _OLDER.model_dump(mode="json")
        raw["newer"] = overlapping.model_dump(mode="json")
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(SecInstitutionalHistoryStateError, match="must not overlap"):
            store.load()


def test_single_sided_and_contradictory_cursor_states_fail_closed() -> None:
    with TemporaryDirectory() as temp_dir:
        store = _store(temp_dir)
        now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        store.path.parent.mkdir(parents=True, exist_ok=True)

        only_older = {
            "schema_version": "sec-institutional-history-state-v1",
            "updated_at": now.isoformat(),
            "phase": "preparing",
            "older": _OLDER.model_dump(mode="json"),
            "newer": None,
            "target_cursor": 0,
            "total_targets": None,
        }
        store.path.write_text(json.dumps(only_older), encoding="utf-8")
        assert store.load().older == _OLDER

        incomplete = dict(only_older)
        incomplete["older"] = {
            "period_start": "2025-12-01",
            "period_end": "2026-02-28",
            "dataset_url": _OLDER.dataset_url,
        }
        store.path.write_text(json.dumps(incomplete), encoding="utf-8")
        with pytest.raises(SecInstitutionalHistoryStateError):
            store.load()

        cursor_over_total = {
            "schema_version": "sec-institutional-history-state-v1",
            "updated_at": now.isoformat(),
            "phase": "ready",
            "older": _OLDER.model_dump(mode="json"),
            "newer": _NEWER.model_dump(mode="json"),
            "target_cursor": 5,
            "total_targets": 2,
        }
        store.path.write_text(json.dumps(cursor_over_total), encoding="utf-8")
        with pytest.raises(SecInstitutionalHistoryStateError, match="cannot exceed"):
            store.load()

        preparing_with_targets = dict(cursor_over_total)
        preparing_with_targets["phase"] = "preparing"
        preparing_with_targets["target_cursor"] = 0
        store.path.write_text(json.dumps(preparing_with_targets), encoding="utf-8")
        with pytest.raises(
            SecInstitutionalHistoryStateError, match="a preparing window cannot declare targets"
        ):
            store.load()

        pending_completion = dict(cursor_over_total)
        pending_completion["phase"] = "completed"
        pending_completion["target_cursor"] = 1
        store.path.write_text(json.dumps(pending_completion), encoding="utf-8")
        with pytest.raises(
            SecInstitutionalHistoryStateError, match="must have processed every target"
        ):
            store.load()
