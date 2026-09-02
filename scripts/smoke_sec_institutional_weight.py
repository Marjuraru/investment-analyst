#!/usr/bin/env python3
"""Run the finite real SEC smoke for declared 13F position-value weights."""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.analytics.cazatiburones.institutional_close_totals import (
    effective_close_total,
)
from investment_analyst.application.cazatiburones_institutional_weight import (
    CazatiburonesInstitutionalWeightApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_CIK = "1350694"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    base_smoke = Path(__file__).with_name("smoke_sec_institutional_metrics.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(base_smoke), "--workspace", str(arguments.workspace)],
            capture_output=True,
            check=True,
            text=True,
        )
        evidence = json.loads(completed.stdout)
        known_at = datetime.fromisoformat(evidence["known_at"].replace("Z", "+00:00")).astimezone(
            UTC
        )
        location = StorageLocationRequest(workspace=arguments.workspace)
        application = CazatiburonesInstitutionalWeightApplication.create_default()
        first = application.compute(
            asset_id=_ASSET_ID, manager_cik=_CIK, known_at=known_at, location=location
        )
        second = application.compute(
            asset_id=_ASSET_ID, manager_cik=_CIK, known_at=known_at, location=location
        )
        if first.metrics_created == 0 or second.metrics_reused != first.metrics_created:
            raise RuntimeError("weight smoke did not prove idempotent persistence")
        paths = StoragePaths.from_root(WorkspaceService().resolve(arguments.workspace).storage_root)
        with LocalStorage(paths, read_only=True) as storage:
            closes = tuple(
                semantics_from_raw_record(record)
                for record in storage.raw_records.list(
                    source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
                    schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
                    available_to=known_at,
                )
            )
        totals = {
            item.accession: {
                "total": str(effective_close_total(item)[0]),
                "quality": effective_close_total(item)[1].value,
            }
            for item in closes
        }
        print(
            json.dumps(
                {
                    "head": evidence["head"],
                    "tree": evidence["tree"],
                    "known_at": known_at.isoformat(),
                    "effective_close": {
                        "accessions": evidence["accessions"],
                        "report_periods": evidence["report_periods"],
                        "totals": totals,
                    },
                    "weights": {
                        "first": first.model_dump(mode="json"),
                        "second": second.model_dump(mode="json"),
                        "omissions_by_reason": dict(sorted(first.skipped_by_reason.items())),
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        shutil.rmtree(arguments.workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
