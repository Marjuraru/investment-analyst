#!/usr/bin/env python3
"""Run the finite real SEC smoke for persisted institutional 13F events."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.cazatiburones_institutional_events import (
    CazatiburonesInstitutionalEventsApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest

_ASSET_ID = "equity:us:aapl"
_CIK = "1350694"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    arguments = parser.parse_args()

    custom_workspace = arguments.workspace is not None
    if custom_workspace:
        if arguments.workspace.exists():
            raise RuntimeError("workspace must be a new scratch path")
        workspace_dir = arguments.workspace
    else:
        temp_dir = tempfile.mkdtemp(prefix="investment-analyst-sec-events-")
        workspace_dir = Path(temp_dir) / "workspace"

    env = dict(os.environ)
    if not env.get("SEC_USER_AGENT"):
        env_file = Path(__file__).resolve().parents[1] / ".env"
        if not env_file.is_file():
            env_file = Path("/home/marjuraru/projects/investment-analyst/.env")
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("SEC_USER_AGENT=") or line.startswith("export SEC_USER_AGENT="):
                    _, _, val = line.partition("=")
                    cleaned = val.strip().strip("'\"")
                    if cleaned:
                        env["SEC_USER_AGENT"] = cleaned
                        os.environ["SEC_USER_AGENT"] = cleaned
                        break

    base_smoke = Path(__file__).with_name("smoke_sec_institutional_metrics.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(base_smoke), "--workspace", str(workspace_dir)],
            capture_output=True,
            check=True,
            text=True,
            env=env,
        )
        evidence = json.loads(completed.stdout)
        known_at = datetime.fromisoformat(evidence["known_at"].replace("Z", "+00:00")).astimezone(
            UTC
        )
        location = StorageLocationRequest(workspace=workspace_dir)
        application = CazatiburonesInstitutionalEventsApplication.create_default()

        # First materialization
        first = application.materialize(
            asset_id=_ASSET_ID,
            manager_cik=_CIK,
            known_at=known_at,
            location=location,
        )

        # Re-materialization (idempotent)
        second = application.materialize(
            asset_id=_ASSET_ID,
            manager_cik=_CIK,
            known_at=known_at,
            location=location,
        )

        if not first.created:
            raise RuntimeError("initial materialization did not create snapshot")
        if second.created:
            raise RuntimeError("second materialization was not idempotent")
        if first.snapshot_id != second.snapshot_id:
            raise RuntimeError("snapshot_id diverged across runs")
        if first.events != second.events or first.candidates != second.candidates:
            raise RuntimeError("counts diverged across runs")
        if first.events == 0 or first.candidates == 0:
            raise RuntimeError("event smoke did not produce events or candidates")

        # Query by snapshot_id
        snapshot = application.query(
            asset_id=_ASSET_ID,
            manager_cik=_CIK,
            known_at=known_at,
            snapshot_id=first.snapshot_id,
            location=location,
        )
        if snapshot is None:
            raise RuntimeError("query by snapshot_id returned None")
        if snapshot.snapshot_id != first.snapshot_id:
            raise RuntimeError("queried snapshot id does not match")

        report = {
            "asset_id": _ASSET_ID,
            "manager_cik": _CIK,
            "known_at": known_at.isoformat(),
            "first_created": first.created,
            "second_created": second.created,
            "snapshot_id": str(first.snapshot_id),
            "events_count": first.events,
            "candidates_count": first.candidates,
            "query_verified": True,
            "base_metrics": evidence["metrics"]["first"],
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        if not custom_workspace and workspace_dir.parent.exists():
            shutil.rmtree(workspace_dir.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
