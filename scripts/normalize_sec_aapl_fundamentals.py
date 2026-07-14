#!/usr/bin/env python3
"""Normalize selected Apple SEC facts from previously stored local snapshots."""

import argparse
import json
from pathlib import Path

from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
    SecObservationPipelineError,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_NOTICE = (
    "Normalizes five explicit Apple facts from local SEC snapshots using filing acceptance time "
    "for point-in-time availability. It does not calculate ratios or diagnostics, execute "
    "operations, or provide financial advice."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize selected Apple fundamentals from local SEC raw snapshots."
    )
    parser.add_argument("--root", required=True, type=Path)
    return parser


def main() -> int:
    """Run the offline observation pipeline and print a compact JSON summary."""
    args = _parser().parse_args()
    try:
        with LocalStorage(StoragePaths.from_root(args.root)) as storage:
            summary = SecAaplObservationPipeline(
                storage,
                SecCompanyFactsNormalizer(),
            ).run()
    except (SecObservationPipelineError, StorageError, ValueError) as error:
        print(json.dumps({"error": str(error)}, indent=2, sort_keys=True))
        return 1

    print(
        json.dumps(
            {"notice": _NOTICE, "summary": summary.to_json_dict()},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
