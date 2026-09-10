#!/usr/bin/env python3
"""Execute point-in-time read-only query of SEC Form 13F manager universe."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
    WorkspaceService,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseQueryRequest,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query official SEC Form 13F manager universe")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Path to workspace root directory",
    )
    parser.add_argument(
        "--known-at",
        type=str,
        required=True,
        help="ISO 8601 UTC timestamp of point-in-time cut",
    )
    parser.add_argument(
        "--asset-id",
        type=str,
        default=None,
        help="Filter candidates by asset ID",
    )
    parser.add_argument(
        "--cik",
        type=str,
        default=None,
        help="Filter candidates by manager CIK",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        known_at = datetime.fromisoformat(args.known_at)
    except ValueError as error:
        sys.stderr.write(f"Error parsing --known-at: {error}\n")
        return 1

    if known_at.tzinfo is None or known_at.utcoffset() is None:
        sys.stderr.write("Error: --known-at must include timezone offset (e.g. Z or +00:00)\n")
        return 1

    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(root=args.workspace) if args.workspace else None
    )
    application = SecInstitutionalUniverseApplication(runtime)

    location = (
        StorageLocationRequest(workspace_root=args.workspace)
        if args.workspace
        else StorageLocationRequest()
    )
    result = application.query(
        SecInstitutionalUniverseQueryRequest(
            known_at=known_at,
            asset_id=args.asset_id,
            cik=args.cik,
        ),
        location=location,
    )

    output = {
        "status": "PASS",
        "snapshot_id": str(result.snapshot.snapshot_id),
        "dataset_revision_id": str(result.snapshot.dataset_revision_id),
        "period_start": result.snapshot.period_start.isoformat(),
        "period_end": result.snapshot.period_end.isoformat(),
        "dataset_sha256": result.snapshot.dataset_sha256,
        "eligible_asset_count": result.snapshot.eligible_asset_count,
        "matched_asset_count": result.snapshot.matched_asset_count,
        "candidate_manager_count": result.snapshot.candidate_manager_count,
        "selected_manager_count": result.snapshot.selected_manager_count,
        "filtered_candidate_count": len(result.filtered_candidates),
        "covered_cusips": list(result.snapshot.covered_cusips),
        "missing_cusips": list(result.snapshot.missing_cusips),
        "candidates": [
            {
                "candidate_id": str(c.candidate_id),
                "asset_id": c.asset_id,
                "cusip": c.cusip,
                "manager_cik": c.manager_cik,
                "manager_name": c.manager_name,
                "accession": c.accession,
                "form": c.form,
                "filing_date": c.filing_date.isoformat(),
                "report_period": c.report_period.isoformat(),
                "value_as_filed": str(c.value_as_filed),
                "is_amendment": c.is_amendment,
                "is_selected": c.is_selected,
                "selection_rank": c.selection_rank,
            }
            for c in result.filtered_candidates
        ],
    }
    sys.stdout.write(json.dumps(output, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
