#!/usr/bin/env python3
"""Query independent persisted Apple diagnostics without recomputation or writes."""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from investment_analyst.analytics.aapl_daily_report_formatter import (
    format_aapl_daily_report,
)
from investment_analyst.analytics.aapl_daily_report_service import (
    AaplDailyReportError,
    AaplDailyReportService,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticView,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
    ConsolidatedDiagnosticQueryError,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

_NOTICE = (
    "Read-only consolidated view: market and fundamental diagnostics remain separate; "
    "there is no combined score or verdict. computed_at may be later than known_at in a "
    "retrospective reconstruction, while available_at controls point-in-time eligibility. "
    "This is not financial advice and no operations are executed."
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
        raise argparse.ArgumentTypeError("as-of values must use YYYY-MM-DD") from error


def _frequency(value: str) -> DataFrequency:
    mapping = {
        "annual": DataFrequency.ANNUAL,
        "quarterly": DataFrequency.QUARTERLY,
    }
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError(
            "fundamental-frequency must be annual or quarterly"
        ) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query separate persisted Apple market and fundamental diagnostics."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument(
        "--fundamental-frequency",
        required=True,
        type=_frequency,
    )
    parser.add_argument("--market-as-of", type=_date_value)
    parser.add_argument("--fundamental-as-of", type=_date_value)
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("json", "daily-json", "text"),
        default="json",
        help=(
            "output contract: legacy compact JSON (default), versioned enriched JSON, "
            "or human-readable text"
        ),
    )
    return parser


def _legacy_payload(view: ConsolidatedDiagnosticView) -> dict[str, object]:
    """Preserve the original compact JSON output contract byte-for-byte."""
    result = view.to_json_dict()
    return {
        "notice": _NOTICE,
        "query": result["request"],
        "status": result["status"],
        "market": result["market"],
        "fundamental": result["fundamental"],
        "temporal_context": result["temporal_context"],
        "traceability": {
            "diagnostics_examined": result["diagnostics_examined"],
            "metric_results_examined": result["metric_results_examined"],
            "ignored_algorithm_versions": result["ignored_algorithm_versions"],
            "verified": result["traceability_verified"],
        },
    }


def main() -> int:
    """Execute one read-only query and print the selected output contract."""
    args = _parser().parse_args()
    try:
        request = ConsolidatedDiagnosticRequest(
            known_at=args.known_at,
            fundamental_frequency=args.fundamental_frequency,
            market_as_of=args.market_as_of,
            fundamental_as_of=args.fundamental_as_of,
        )
        runtime = ApplicationRuntime.create_default()
        with runtime.open_storage(
            storage_location_from_namespace(args),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            if args.output_format == "json":
                view = AaplConsolidatedDiagnosticService(storage).query(request)
                output = json.dumps(_legacy_payload(view), indent=2, sort_keys=True)
            else:
                report = AaplDailyReportService(storage).query(request)
                if args.output_format == "daily-json":
                    output = json.dumps(report.to_json_dict(), indent=2, sort_keys=True)
                else:
                    output = format_aapl_daily_report(report)
        print(output)
        return 0
    except (
        AaplDailyReportError,
        ApplicationRuntimeError,
        ConsolidatedDiagnosticQueryError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"consolidated diagnostic query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
