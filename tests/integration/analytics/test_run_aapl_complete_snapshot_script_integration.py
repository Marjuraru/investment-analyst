"""Subprocess coverage for the compact Apple complete-snapshot CLI."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
)
from investment_analyst.core.models import (
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def _fixture(path: Path) -> None:
    bars = []
    for day in range(1, 26):
        bars.append(
            {
                "t": f"2026-06-{day:02d}T00:00:00Z",
                "o": 100,
                "h": 101,
                "l": 99,
                "c": 100,
                "v": 2_000_000,
                "n": 20_000 + day,
                "vw": 100,
            }
        )
    path.write_text(
        json.dumps({"bars": bars, "symbol": "AAPL", "next_page_token": None}),
        encoding="utf-8",
    )


def _fundamental(root: Path) -> None:
    definition = next(
        item
        for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
        if item.metric_name == "fundamental.net_margin"
    )
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        metric = MetricResult(
            asset_id="equity:us:aapl",
            metric_key=definition.metric_name,
            value=Decimal("0.2"),
            unit="ratio",
            as_of=datetime(2026, 3, 31, tzinfo=UTC),
            available_at=datetime(2026, 7, 1, tzinfo=UTC),
            computed_at=datetime(2026, 7, 2, tzinfo=UTC),
            parameters={"frequency": "quarterly"},
            input_observation_ids=[uuid4()],
            algorithm_version=definition.algorithm_version,
            quality=DataQuality.VALID,
        )
        storage.metric_results.save(metric)
        storage.diagnostics.save(
            DiagnosticResult(
                asset_id="equity:us:aapl",
                mode=DiagnosticMode.FUNDAMENTAL,
                verdict=DiagnosticVerdict.POSITIVE,
                final_score=Decimal("70"),
                confidence=Decimal("0.8"),
                as_of=metric.as_of,
                available_at=metric.available_at,
                computed_at=metric.computed_at,
                components=[
                    DiagnosticComponent(
                        component_key="fundamental_test",
                        score=Decimal("70"),
                        weight=Decimal("1"),
                        weighted_contribution=Decimal("70"),
                        metric_result_ids=[metric.result_id],
                        explanation="Independent local fundamental component.",
                    )
                ],
                evidence=[
                    DiagnosticEvidence(
                        metric_result_id=metric.result_id,
                        direction=EvidenceDirection.SUPPORTS,
                        contribution=Decimal("0.4"),
                        reason="Independent local fundamental evidence.",
                    )
                ],
                algorithm_version=FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
                summary="Descriptive local fundamental diagnostic for Apple.",
                quality=DataQuality.VALID,
            )
        )


def _run(root: Path, fixture: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    launcher = "\n".join(
        (
            "import runpy, sys",
            "from pathlib import Path",
            "from investment_analyst.providers.http import HttpResponse",
            "import investment_analyst.providers.http as http_module",
            "class FakeTransport:",
            "    def __init__(self, *args, **kwargs): pass",
            "    def get(self, url, *, headers, timeout_seconds):",
            f"        body = Path({str(fixture)!r}).read_bytes()",
            "        return HttpResponse(status_code=200, body=body, headers={}, url=url)",
            "http_module.UrlLibHttpTransport = FakeTransport",
            "sys.argv = ['run_aapl_complete_snapshot.py', *sys.argv[1:]]",
            "runpy.run_path('scripts/run_aapl_complete_snapshot.py', run_name='__main__')",
        )
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ALPACA_API_KEY": "script-key",
            "ALPACA_API_SECRET": "script-secret",
            "PYTHONPATH": os.pathsep.join(
                [str(Path("src").resolve()), environment.get("PYTHONPATH", "")]
            ),
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            "--root",
            str(root),
            "--known-at",
            "2026-07-14T23:59:00Z",
            "--market-start",
            "2026-06-01",
            "--market-end",
            "2026-06-26",
            "--fundamental-frequency",
            "quarterly",
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_script_returns_complete_compact_json_without_secrets(tmp_path) -> None:
    root = tmp_path / "complete"
    fixture = tmp_path / "bars.json"
    _fixture(fixture)
    _fundamental(root)

    result = _run(root, fixture)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["overall_status"] == "complete"
    assert payload["source"]["feed"] == "iex"
    assert "single-exchange" in payload["source"]["coverage"]
    assert "combined_score" not in result.stdout
    assert "script-key" not in result.stdout + result.stderr
    assert "script-secret" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_script_partial_is_valid_but_require_complete_fails(tmp_path) -> None:
    fixture = tmp_path / "bars.json"
    _fixture(fixture)
    partial = _run(tmp_path / "partial", fixture)
    assert partial.returncode == 0, partial.stderr
    assert json.loads(partial.stdout)["summary"]["overall_status"] == "partial"

    required = _run(tmp_path / "required", fixture, "--require-complete")
    assert required.returncode != 0
    assert json.loads(required.stdout)["summary"]["overall_status"] == "partial"
    assert "can be resumed idempotently" in required.stderr
