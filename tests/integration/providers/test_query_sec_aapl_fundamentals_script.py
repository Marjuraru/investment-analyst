"""Subprocess tests for the local SEC point-in-time query script."""

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.storage import LocalStorage, StoragePaths

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "scripts" / "query_sec_aapl_fundamentals.py"


def _observation() -> NormalizedObservation:
    raw_record_id = uuid4()
    period_end = date(2025, 9, 27)
    key = {
        "accession_number": "test-accession",
        "taxonomy": "us-gaap",
        "tag": "Assets",
        "unit": "USD",
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(raw_record_id),
        "submissions_record_id": str(uuid4()),
    }
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_record_id,
        asset_id=ASSET_ID,
        field_name="fundamental.assets",
        value=Decimal("200"),
        unit="USD",
        frequency=DataFrequency.ANNUAL,
        observed_at=datetime(2025, 9, 27, tzinfo=UTC),
        period_end=datetime(2025, 9, 27, tzinfo=UTC),
        available_at=datetime(2025, 10, 31, 20, tzinfo=UTC),
        normalized_at=datetime(2025, 11, 2, tzinfo=UTC),
        source=SourceReference(
            source_id=COMPANYFACTS_SOURCE_ID,
            record_key=json.dumps(key, separators=(",", ":"), sort_keys=True),
            retrieved_at=datetime(2025, 11, 1, tzinfo=UTC),
        ),
        quality=DataQuality.VALID,
        transformation_version=TRANSFORMATION_VERSION,
    )


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, environment.get("PYTHONPATH", "")) if part
    )
    environment.pop("SEC_USER_AGENT", None)
    return environment


def test_script_prints_json_and_does_not_modify_storage(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.observations.save(_observation())
        before = storage.observations.list(asset_id=ASSET_ID)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--known-at",
            "2025-10-31T15:00:00-05:00",
            "--frequency",
            "annual",
            "--limit",
            "4",
        ],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["result"]["periods_returned"] == 1
    assert "SEC_USER_AGENT" not in completed.stdout
    assert "Traceback" not in completed.stderr
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        assert storage.observations.list(asset_id=ASSET_ID) == before


def test_script_empty_database_and_invalid_arguments_are_clear(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)):
        pass
    empty = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--known-at",
            "2026-01-01T00:00:00Z",
            "--frequency",
            "quarterly",
        ],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    invalid = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--known-at",
            "2026-01-01T00:00:00",
            "--frequency",
            "annual",
        ],
        cwd=PROJECT_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert empty.returncode == 0
    assert json.loads(empty.stdout)["result"]["periods"] == []
    assert invalid.returncode != 0
    assert "timezone" in invalid.stderr
    assert "Traceback" not in invalid.stderr
