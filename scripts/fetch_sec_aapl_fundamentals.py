#!/usr/bin/env python3
"""Fetch the two fixed Apple SEC EDGAR foundation documents."""

import argparse
import json
import os
import sys

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecAaplFundamentalsPipeline,
)
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    return parser


def main() -> int:
    """Run the raw SEC document import and print a compact JSON summary."""
    arguments = _parser().parse_args()
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent.strip():
        print("SEC_USER_AGENT is required and was not provided.", file=sys.stderr)
        return 2

    try:
        runtime = ApplicationRuntime.create_default()
        configuration = resolve_sec_configuration(runtime.provider_resolver)
        identity = SecEdgarIdentity(user_agent=user_agent)
        client = SecEdgarClient(
            UrlLibHttpTransport(),
            identity,
            cik=configuration.cik,
            ticker=configuration.ticker,
        )
        with runtime.open_storage(
            storage_location_from_namespace(arguments),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            summary = SecAaplFundamentalsPipeline(
                storage,
                client,
                configuration=configuration,
            ).run()
        output = {
            "notice": (
                "Official SEC EDGAR source. This command only stores raw issuer documents; it "
                "does not calculate fundamental ratios or diagnostics, execute operations, or "
                "provide financial advice."
            ),
            "summary": summary.to_json_dict(),
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except (ApplicationRuntimeError, WorkspaceError, StorageError) as error:
        print(f"SEC Apple fundamentals import failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001
        print(f"SEC Apple fundamentals import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
