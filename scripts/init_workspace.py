#!/usr/bin/env python3
"""Initialize or reuse a persistent investment-analyst workspace."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_NOTICE = (
    "Persistent workspace initialized without reading .env or storing credentials. "
    "The operation is idempotent and preserves existing compatible data."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    return parser


def main() -> int:
    """Initialize the resolved workspace and print compact JSON."""
    arguments = _parser().parse_args()
    try:
        result = WorkspaceService().initialize(arguments.workspace)
        payload = result.to_json_dict()
        payload["notice"] = _NOTICE
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (WorkspaceError, ValidationError, OSError, ValueError) as error:
        print(f"workspace initialization failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
