#!/usr/bin/env python3
"""Run a finite real 13F metric smoke in a new external workspace."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.cazatiburones_institutional_metrics import (
    CazatiburonesInstitutionalMetricsApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    if args.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        raise RuntimeError("SEC_USER_AGENT is required")
    repository = Path(__file__).resolve().parents[1]
    observed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/smoke_sec_institutional_observations.py"),
            "--workspace",
            str(args.workspace),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    evidence = json.loads(observed.stdout)
    known_at = datetime.now(UTC)
    application = CazatiburonesInstitutionalMetricsApplication.create_default()
    location = StorageLocationRequest(workspace=args.workspace)
    first = application.compute(
        asset_id="equity:us:aapl", manager_cik="1067983", known_at=known_at, location=location
    )
    second = application.compute(
        asset_id="equity:us:aapl", manager_cik="1067983", known_at=known_at, location=location
    )
    if first.metrics_created == 0 or second.metrics_reused != first.metrics_created:
        raise RuntimeError("metric smoke did not prove idempotent persistence")
    os.environ.pop("SEC_USER_AGENT", None)
    print(
        json.dumps(
            {
                "known_at": known_at.isoformat(),
                "observations": evidence,
                "first": first.model_dump(mode="json"),
                "second": second.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
