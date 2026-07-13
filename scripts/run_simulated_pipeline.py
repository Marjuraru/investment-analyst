#!/usr/bin/env python3
"""Run the deterministic simulated pipeline against an explicit local root."""

import argparse
import json
from pathlib import Path

from investment_analyst.simulation import SimulatedPipeline
from investment_analyst.storage import LocalStorage, StoragePaths


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the fictitious and deterministic Investment Analyst demonstration."
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Storage root for this simulation run.",
    )
    return parser.parse_args()


def main() -> None:
    """Execute the pipeline and print its audit summary as JSON."""
    args = _parse_args()
    paths = StoragePaths.from_root(args.root)
    with LocalStorage(paths) as storage:
        summary = SimulatedPipeline(storage).run()
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
