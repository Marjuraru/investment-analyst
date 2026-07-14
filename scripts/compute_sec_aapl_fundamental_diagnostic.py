#!/usr/bin/env python3
"""Compute and persist one Apple SEC fundamental diagnostic from local metrics."""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
    SecFundamentalDiagnosticEngineError,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticRequest,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
    SecFundamentalDiagnosticPipelineError,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelectionError,
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

_NOTICE = (
    "Descriptive Apple fundamental diagnostic based only on local point-in-time metrics and "
    "explicit weights and rules. Confidence is not a probability, this is not a financial "
    "recommendation, and no operations are executed."
)


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("as-of must use YYYY-MM-DD") from error


def _frequency(value: str) -> DataFrequency:
    mapping = {
        "annual": DataFrequency.ANNUAL,
        "quarterly": DataFrequency.QUARTERLY,
    }
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("frequency must be annual or quarterly") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute an auditable Apple fundamental diagnostic from local metrics."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--frequency", required=True, type=_frequency)
    parser.add_argument("--as-of", type=_date_value)
    return parser


def _compact_diagnostic(diagnostic) -> dict[str, object]:
    return {
        "diagnostic_id": str(diagnostic.diagnostic_id),
        "mode": diagnostic.mode.value,
        "verdict": diagnostic.verdict.value,
        "final_score": str(diagnostic.final_score),
        "confidence": str(diagnostic.confidence),
        "as_of": diagnostic.as_of.isoformat(),
        "available_at": diagnostic.available_at.isoformat(),
        "computed_at": diagnostic.computed_at.isoformat(),
        "quality": diagnostic.quality.value,
        "algorithm_version": diagnostic.algorithm_version,
        "summary": diagnostic.summary,
        "components": [item.model_dump(mode="json") for item in diagnostic.components],
        "evidence": [item.model_dump(mode="json") for item in diagnostic.evidence],
    }


def main() -> int:
    """Run the local fundamental diagnostic pipeline and print compact JSON."""
    args = _parser().parse_args()
    try:
        request = SecFundamentalDiagnosticRequest(
            known_at=args.known_at,
            frequency=args.frequency,
            as_of_period_end=args.as_of,
        )
        runtime = ApplicationRuntime.create_default()
        with runtime.open_storage(
            storage_location_from_namespace(args),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            selector = SecFundamentalDiagnosticSelector(storage)
            pipeline = SecAaplFundamentalDiagnosticPipeline(
                storage,
                selector,
                SecFundamentalDiagnosticEngine(),
            )
            summary = pipeline.run(request)
            diagnostic = storage.diagnostics.get(summary.diagnostic_id)
        print(
            json.dumps(
                {
                    "notice": _NOTICE,
                    "diagnostic": _compact_diagnostic(diagnostic),
                    "summary": summary.to_json_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        SecFundamentalDiagnosticEngineError,
        SecFundamentalDiagnosticPipelineError,
        SecFundamentalDiagnosticSelectionError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"SEC fundamental diagnostic failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
