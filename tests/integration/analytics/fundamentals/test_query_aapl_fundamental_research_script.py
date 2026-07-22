"""Subprocess coverage for the read-only fundamental research command."""

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
    get_sec_fact_definition,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _ROOT / "scripts" / "query_aapl_fundamental_research.py"
_PERIOD_END = date(2025, 9, 27)


def _observation(field_name: str, value: str) -> NormalizedObservation:
    definition = get_sec_fact_definition(field_name)
    raw_record_id = uuid4()
    record_key = {
        "accession_number": "0000320193-25-000001",
        "taxonomy": definition.taxonomy,
        "tag": definition.tag,
        "unit": "USD",
        "period": _PERIOD_END.isoformat(),
        "companyfacts_record_id": str(raw_record_id),
        "submissions_record_id": str(uuid4()),
    }
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_record_id,
        asset_id=ASSET_ID,
        field_name=field_name,
        value=Decimal(value),
        unit="USD",
        frequency=DataFrequency.ANNUAL,
        observed_at=datetime(2025, 9, 27, tzinfo=UTC),
        period_end=datetime(2025, 9, 27, tzinfo=UTC),
        available_at=datetime(2025, 10, 31, 20, tzinfo=UTC),
        normalized_at=datetime(2025, 11, 2, tzinfo=UTC),
        source=SourceReference(
            source_id=COMPANYFACTS_SOURCE_ID,
            record_key=json.dumps(record_key, separators=(",", ":"), sort_keys=True),
            retrieved_at=datetime(2025, 11, 1, tzinfo=UTC),
        ),
        quality=DataQuality.VALID,
        transformation_version=TRANSFORMATION_VERSION,
    )


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_ROOT / "src")
    environment.pop("SEC_USER_AGENT", None)
    return environment


def test_script_emits_exact_evidence_and_does_not_modify_storage(tmp_path: Path) -> None:
    observations = [
        _observation("fundamental.current_assets", "500"),
        _observation("fundamental.current_liabilities", "250"),
    ]
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        for observation in observations:
            storage.observations.save(observation)
        before = storage.observations.list(asset_id=ASSET_ID)

    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path),
            "--known-at",
            "2025-10-31T15:00:00-05:00",
            "--frequency",
            "annual",
            "--start",
            _PERIOD_END.isoformat(),
            "--end",
            _PERIOD_END.isoformat(),
        ],
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)["result"]
    metrics = {item["metric_key"]: item for item in payload["periods"][0]["metrics"]}
    current_ratio = metrics["fundamental.research.current_ratio"]
    assert payload["schema_version"] == "aapl-fundamental-research-v2"
    assert payload["traceability_verified"] is True
    assert current_ratio["value"] == "2"
    assert {item["observation_id"] for item in current_ratio["inputs"]} == {
        str(observation.observation_id) for observation in observations
    }
    assert "SEC_USER_AGENT" not in completed.stdout
    assert "Traceback" not in completed.stderr
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        assert storage.observations.list(asset_id=ASSET_ID) == before


def test_script_rejects_naive_known_at_without_traceback(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)):
        pass

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
        cwd=_ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "timezone" in completed.stderr
    assert "Traceback" not in completed.stderr
