#!/usr/bin/env python3
"""Normalize selected Apple SEC facts from previously stored local snapshots."""

import argparse
import json

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
    SecObservationPipelineError,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

_NOTICE = (
    "Normalizes the five core Apple facts and the additional research catalog from local SEC "
    "snapshots using filing acceptance time for point-in-time availability. It does not "
    "calculate ratios or diagnostics, execute operations, or provide financial advice."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize selected Apple fundamentals from local SEC raw snapshots."
    )
    add_storage_location_arguments(parser)
    return parser


def main() -> int:
    """Run the offline observation pipeline and print a compact JSON summary."""
    args = _parser().parse_args()
    try:
        runtime = ApplicationRuntime.create_default()
        with runtime.open_storage(
            storage_location_from_namespace(args),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            summary = SecAaplObservationPipeline(
                storage,
                SecCompanyFactsNormalizer(),
            ).run()
    except (
        ApplicationRuntimeError,
        SecObservationPipelineError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
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
