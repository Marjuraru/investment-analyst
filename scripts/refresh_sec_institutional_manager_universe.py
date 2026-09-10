#!/usr/bin/env python3
"""Execute official SEC Form 13F manager universe refresh into local storage."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh official SEC Form 13F manager universe")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Path to workspace root directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        sys.stderr.write("Error: SEC_USER_AGENT environment variable is required\n")
        return 1

    sec_identity = SecEdgarIdentity(user_agent)
    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(root=args.workspace) if args.workspace else None
    )
    application = SecInstitutionalUniverseApplication(runtime)

    location = (
        StorageLocationRequest(workspace_root=args.workspace)
        if args.workspace
        else StorageLocationRequest()
    )
    result = application.refresh(
        SecInstitutionalUniverseRefreshRequest(),
        sec_identity=sec_identity,
        location=location,
    )

    output = {
        "status": "PASS",
        "revision_id": str(result.revision_id),
        "snapshot_id": str(result.snapshot_id),
        "period_start": result.period_start.isoformat(),
        "period_end": result.period_end.isoformat(),
        "dataset_url": result.dataset_url,
        "dataset_sha256": result.dataset_sha256,
        "size_bytes": result.size_bytes,
        "retrieved_at": result.retrieved_at.isoformat(),
        "eligible_asset_count": result.eligible_asset_count,
        "matched_asset_count": result.matched_asset_count,
        "candidate_manager_count": result.candidate_manager_count,
        "selected_manager_count": result.selected_manager_count,
        "unselected_manager_count": result.unselected_manager_count,
        "coverage_complete": result.coverage_complete,
        "created": result.created,
    }
    sys.stdout.write(json.dumps(output, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
