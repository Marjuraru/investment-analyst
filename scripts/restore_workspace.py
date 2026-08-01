#!/usr/bin/env python3
"""Verify and restore a local backup into a new or empty workspace."""

import argparse
import json
import sys
from pathlib import Path

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.workspace.backup import WorkspaceBackupError, WorkspaceBackupService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    runtime = ApplicationRuntime.create_default()
    try:
        inspection = WorkspaceBackupService(runtime.workspace_service).restore(
            arguments.backup,
            arguments.destination,
        )
    except WorkspaceBackupError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(inspection.to_json_dict(), ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
