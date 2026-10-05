"""Offline integration smoke for collector cost, recovery, and cycle linkage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "smoke_operational_observability.py"


def _smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("operational_observability_smoke", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("observability smoke script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_operational_observability_profiles_are_finite_and_linked(
    tmp_path: Path,
) -> None:
    result = _smoke_module().run_smoke(scratch_parent=tmp_path)

    assert result["schema_version"] == "operational-observability-smoke-v1"
    assert result["status"] == "pass"
    assert result["environment"]["scratch_only"] is True
    assert result["environment"]["provider_calls"] == 0
    profiles = result["profiles"]
    cost = profiles["operational_observability_cost"]
    assert set(cost) == {"257", "1537"}
    for row_count, profile in cost.items():
        assert profile["fixture_rows"] == int(row_count)
        assert profile["terminal_attempts"] == 102
        assert profile["observability_records"] == 102
        assert profile["execution_digest_matches_without_collector"] is True
        assert profile["payload_document_scans_per_attempt"] == 0
        assert profile["full_logical_measurement_scans_after_cycle"] == 1
        assert profile["database_sha256_unchanged"] is True
        assert profile["wal_unchanged"] is True
        assert profile["latency_interpretation"].startswith("descriptive;")

    series = profiles["operational_observability_series"]
    assert series["loaded_samples_from_three_days"] == 8419
    assert series["cycle_samples"] == 354
    assert series["external_samples_excluded"] == 8065
    assert series["cycle_comparable"] is True
    assert series["internal_identity_change_comparable"] is False
    assert series["complete_coverage"] == {
        "expected": 102,
        "observed": 102,
        "missing": 0,
        "collector_overhead_ms_known": 204,
    }
    assert series["missing_coverage"]["missing"] == 102
    assert series["missing_coverage"]["collector_overhead_ms_known"] is None
    assert series["missing_coverage"]["collector_overhead_ms_unknown_attempts"] == 102
    assert series["restart_and_rollover"]["closed_30_day_attempts"] == 30
    assert series["restart_and_rollover"]["budget_reader_available"] is True
    assert series["collector_failure_isolation"]["begin"]["provider_call_count"] == 1
    assert series["collector_failure_isolation"]["complete"]["provider_call_count"] == 1
