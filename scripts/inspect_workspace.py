#!/usr/bin/env python3
"""Inspect a persistent workspace without creating or modifying storage."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from investment_analyst.workspace.service import WorkspaceError, WorkspaceService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    return parser


def main() -> int:
    """Inspect the resolved workspace in genuine read-only mode."""
    arguments = _parser().parse_args()
    try:
        inspection = WorkspaceService().inspect(arguments.workspace)
        print(json.dumps(inspection.to_json_dict(), indent=2, sort_keys=True))
        return 0
    except (WorkspaceError, ValidationError, OSError, ValueError) as error:
        print(f"workspace inspection failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
