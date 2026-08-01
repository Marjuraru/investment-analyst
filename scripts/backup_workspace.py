#!/usr/bin/env python3
"""Create a verified local backup of an initialized workspace."""

import argparse
import json
import sys
from pathlib import Path

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.workspace.backup import WorkspaceBackupError, WorkspaceBackupService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    runtime = ApplicationRuntime.create_default()
    try:
        manifest = WorkspaceBackupService(runtime.workspace_service).create(
            arguments.workspace,
            arguments.destination,
        )
    except WorkspaceBackupError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(manifest.to_json_dict(), ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
