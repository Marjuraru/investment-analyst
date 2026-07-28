"""CLI integration tests for the local FRED/ALFRED vertical slice."""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.macro.fred_alfred import FredAlfredClient, FredApiKey
from investment_analyst.providers.macro.fred_pipeline import FredVintagePipeline
from investment_analyst.storage import LocalStorage, StoragePaths

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FETCH_SCRIPT = REPOSITORY_ROOT / "scripts" / "fetch_fred_vintage.py"
QUERY_SCRIPT = REPOSITORY_ROOT / "scripts" / "query_fred_point_in_time.py"
FIXTURE = Path("tests/fixtures/fred/gdp_vintage_2020-01-15.json").read_bytes()


class FixtureTransport:
    """Offline transport used to prepare a queryable storage root."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        return HttpResponse(200, FIXTURE, {}, url)


def _run(args: list[str], *, cwd: Path, environment: dict[str, str]):
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_fetch_script_requires_key_without_revealing_configuration(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("FRED_API_KEY", None)

    completed = _run(
        [
            str(FETCH_SCRIPT),
            "--root",
            str(tmp_path / "unused"),
            "--series-id",
            "GDP",
            "--vintage-date",
            "2020-01-15",
            "--start",
            "2019-01-01",
            "--end",
            "2020-01-01",
        ],
        cwd=tmp_path,
        environment=environment,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == "error: FRED_API_KEY is required"


def test_query_script_is_local_point_in_time_and_cwd_independent(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    with LocalStorage(StoragePaths.from_root(storage_root)) as storage:
        client = FredAlfredClient(
            FixtureTransport(),
            FredApiKey("d" * 32),
            clock=lambda: datetime(2020, 3, 1, 12, tzinfo=UTC),
        )
        FredVintagePipeline(storage, client).run(
            "GDP",
            vintage_date=date(2020, 1, 15),
            observation_start=date(2019, 1, 1),
            observation_end=date(2020, 1, 1),
        )

    completed = _run(
        [
            str(QUERY_SCRIPT),
            "--root",
            str(storage_root),
            "--series-id",
            "GDP",
            "--known-at",
            "2020-01-16T00:00:00Z",
            "--start",
            "2019-01-01",
            "--end",
            "2020-01-01",
        ],
        cwd=tmp_path,
        environment=dict(os.environ),
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["query"]["known_at"] == "2020-01-16T00:00:00Z"
    assert payload["observations_selected"] == 2
    assert payload["observations"][0]["value"] == "100.125"
    assert payload["observations"][1]["value"] is None
    assert payload["traceability_verified"] is True
