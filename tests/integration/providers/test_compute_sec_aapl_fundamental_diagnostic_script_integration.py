"""Subprocess tests for the Apple fundamental diagnostic CLI."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import DataFrequency, DataQuality, MetricResult
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricCandidate,
    SecFundamentalMetricInput,
    get_sec_fundamental_metric_definition,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    sec_fundamental_metric_result_id,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_SCRIPT = Path(__file__).parents[3] / "scripts" / "compute_sec_aapl_fundamental_diagnostic.py"
_ROLES = {
    "fundamental.net_margin": ("current_net_income", "current_revenue"),
    "fundamental.liabilities_to_assets": ("current_assets", "current_liabilities"),
    "fundamental.liabilities_to_equity": ("current_equity", "current_liabilities"),
}


def _save_metric(storage: LocalStorage, metric_name: str, value: str) -> None:
    definition = get_sec_fundamental_metric_definition(metric_name)
    inputs = tuple(
        SecFundamentalMetricInput(role=role, observation_id=uuid4()) for role in _ROLES[metric_name]
    )
    period_end = datetime(2025, 9, 27, tzinfo=UTC)
    available_at = datetime(2025, 10, 31, tzinfo=UTC)
    candidate = SecFundamentalMetricCandidate(
        asset_id="equity:us:aapl",
        metric_name=metric_name,
        value=Decimal(value),
        unit="ratio",
        frequency=DataFrequency.ANNUAL,
        period_end=period_end,
        available_at=available_at,
        input_roles=inputs,
        formula=definition.formula,
        algorithm_version=definition.algorithm_version,
        comparison=definition.comparison,
        fiscal_year="2025",
        fiscal_period="FY",
        quality=DataQuality.VALID,
    )
    storage.metric_results.save(
        MetricResult(
            result_id=sec_fundamental_metric_result_id(candidate),
            asset_id=candidate.asset_id,
            metric_key=metric_name,
            value=candidate.value,
            unit="ratio",
            as_of=period_end,
            available_at=available_at,
            computed_at=datetime(2025, 11, 1, tzinfo=UTC),
            parameters={
                "source_id": "sec-edgar:aapl:companyfacts",
                "frequency": "annual",
                "period_end": period_end.isoformat(),
                "comparison": definition.comparison.value,
                "formula": definition.formula,
                "input_roles": [
                    {"role": item.role, "observation_id": str(item.observation_id)}
                    for item in inputs
                ],
                "fiscal_year": "2025",
                "fiscal_period": "FY",
            },
            input_observation_ids=list(candidate.input_observation_ids()),
            algorithm_version=definition.algorithm_version,
            quality=DataQuality.VALID,
        )
    )


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("SEC_USER_AGENT", None)
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(root),
            "--known-at",
            "2026-01-01T00:00:00Z",
            "--frequency",
            "annual",
            *extra,
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).parents[3],
        env=environment,
        text=True,
    )


def test_script_outputs_compact_json_and_reuses_diagnostic(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _save_metric(storage, "fundamental.net_margin", "0.20")
        _save_metric(storage, "fundamental.liabilities_to_assets", "0.50")
        _save_metric(storage, "fundamental.liabilities_to_equity", "1.00")

    first = _run(tmp_path)
    second = _run(tmp_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["summary"]["diagnostics_created"] == 1
    assert second_payload["summary"]["diagnostics_reused"] == 1
    assert first_payload["diagnostic"]["mode"] == "fundamental"
    assert (
        first_payload["diagnostic"]["diagnostic_id"]
        == second_payload["diagnostic"]["diagnostic_id"]
    )
    combined = first.stdout + first.stderr
    assert "record_key" not in combined
    assert "document_json" not in combined
    assert "SEC_USER_AGENT" not in combined
    assert "Traceback" not in combined


def test_script_accepts_offset_and_exact_as_of(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _save_metric(storage, "fundamental.net_margin", "0.20")
        _save_metric(storage, "fundamental.liabilities_to_assets", "0.50")

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path),
            "--known-at",
            "2025-12-31T19:00:00-05:00",
            "--frequency",
            "annual",
            "--as-of",
            "2025-09-27",
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).parents[3],
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["summary"]["target_period_end"].startswith("2025-09-27")


def test_script_handles_empty_storage_with_insufficient_result(tmp_path) -> None:
    completed = _run(tmp_path)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["diagnostic"]["verdict"] == "insufficient_data"


def test_script_rejects_naive_known_at_without_stack_trace(tmp_path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path),
            "--known-at",
            "2026-01-01T00:00:00",
            "--frequency",
            "annual",
        ],
        check=False,
        capture_output=True,
        cwd=Path(__file__).parents[3],
        env=environment,
        text=True,
    )

    assert completed.returncode != 0
    assert "timezone" in completed.stderr
    assert "Traceback" not in completed.stderr
