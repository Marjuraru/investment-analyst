"""Subprocess tests for the Apple SEC fundamental metric CLI."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    SEC_FACT_DEFINITIONS,
    TRANSFORMATION_VERSION,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_NAMESPACE = UUID("d3a3cf66-6678-4b9d-aef1-40b86d87cbf4")
_TAGS = {item.field_name: item.tag for item in SEC_FACT_DEFINITIONS}


def _seed(storage: LocalStorage) -> None:
    periods = (
        (
            2024,
            datetime(2024, 9, 28, tzinfo=UTC),
            datetime(2024, 11, 1, tzinfo=UTC),
            {
                "fundamental.revenue": "100",
                "fundamental.net_income": "20",
                "fundamental.assets": "200",
                "fundamental.liabilities": "80",
                "fundamental.stockholders_equity": "120",
            },
        ),
        (
            2025,
            datetime(2025, 9, 27, tzinfo=UTC),
            datetime(2025, 10, 31, tzinfo=UTC),
            {
                "fundamental.revenue": "125",
                "fundamental.net_income": "30",
                "fundamental.assets": "250",
                "fundamental.liabilities": "100",
                "fundamental.stockholders_equity": "150",
            },
        ),
    )
    for fiscal_year, period_end, available_at, values in periods:
        for field_name, value in values.items():
            raw_record_id = uuid5(_NAMESPACE, f"raw:{fiscal_year}:{field_name}:{value}")
            record_key = json.dumps(
                {
                    "accession_number": f"0000320193-{str(fiscal_year)[-2:]}-000001",
                    "taxonomy": "us-gaap",
                    "tag": _TAGS[field_name],
                    "unit": "USD",
                    "period": period_end.date().isoformat(),
                    "form": "10-K",
                    "fiscal_year": str(fiscal_year),
                    "fiscal_period": "FY",
                    "companyfacts_record_id": str(raw_record_id),
                    "submissions_record_id": str(uuid5(_NAMESPACE, f"submissions:{fiscal_year}")),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            duration = field_name in {
                "fundamental.revenue",
                "fundamental.net_income",
            }
            storage.observations.save(
                NormalizedObservation(
                    observation_id=uuid5(
                        _NAMESPACE,
                        f"observation:{fiscal_year}:{field_name}:{value}",
                    ),
                    raw_record_id=raw_record_id,
                    asset_id="equity:us:aapl",
                    field_name=field_name,
                    value=Decimal(value),
                    unit="USD",
                    frequency=DataFrequency.ANNUAL,
                    observed_at=period_end,
                    period_start=(
                        period_end.replace(year=period_end.year - 1) if duration else None
                    ),
                    period_end=period_end,
                    available_at=available_at,
                    normalized_at=datetime(2026, 1, 1, tzinfo=UTC),
                    source=SourceReference(
                        source_id="sec-edgar:aapl:companyfacts",
                        record_key=record_key,
                        retrieved_at=datetime(2025, 12, 1, tzinfo=UTC),
                        raw_uri="https://data.sec.gov/test",
                        checksum_sha256="b" * 64,
                    ),
                    quality=DataQuality.VALID,
                    transformation_version=TRANSFORMATION_VERSION,
                )
            )


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).parents[3] / "scripts" / "compute_sec_aapl_fundamental_metrics.py"
    environment = os.environ.copy()
    environment.pop("SEC_USER_AGENT", None)
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(root),
            "--known-at",
            "2026-12-31T00:00:00Z",
            "--frequency",
            "annual",
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_script_prints_compact_json_and_reuses_results(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed(storage)

    first = _run(tmp_path, "--start", "2025-01-01", "--limit", "1")
    second = _run(tmp_path, "--start", "2025-01-01", "--limit", "1")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["summary"]["metrics_created"] == 5
    assert first_payload["summary"]["target_periods"] == 1
    assert second_payload["summary"]["metrics_created"] == 0
    assert second_payload["summary"]["metrics_reused"] == 5
    assert "record_key" not in first.stdout
    assert "companyfacts" not in first.stdout.casefold()
    assert "traceback" not in first.stderr.casefold()


def test_script_rejects_naive_known_at_without_stack_trace(tmp_path) -> None:
    script = Path(__file__).parents[3] / "scripts" / "compute_sec_aapl_fundamental_metrics.py"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(tmp_path),
            "--known-at",
            "2026-12-31T00:00:00",
            "--frequency",
            "annual",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode != 0
    assert "timezone" in completed.stderr.casefold()
    assert "traceback" not in completed.stderr.casefold()
