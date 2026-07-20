#!/usr/bin/env python3
"""Calculate transparent Apple fundamental research metrics point-in-time."""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
)
from investment_analyst.analytics.fundamentals.research_service import (
    AaplFundamentalResearchService,
    FundamentalResearchError,
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
    "Consulta local point-in-time de métricas fundamentales descriptivas de Apple. "
    "Cada fórmula, versión e input SEC se incluye en el resultado. No combina análisis "
    "fundamental y de mercado, no genera puntuación o recomendación y no ejecuta órdenes."
)


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected date in YYYY-MM-DD format") from error


def _frequency(value: str) -> DataFrequency:
    mapping = {
        "annual": DataFrequency.ANNUAL,
        "quarterly": DataFrequency.QUARTERLY,
    }
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("frequency must be annual or quarterly") from error


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Calculate point-in-time Apple fundamental research metrics from local "
            "SEC observations."
        )
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--frequency", required=True, type=_frequency)
    parser.add_argument("--start", type=_date_value)
    parser.add_argument("--end", type=_date_value)
    parser.add_argument("--limit", type=_limit)
    return parser


def main() -> int:
    """Execute the read-only calculation and print exact deterministic JSON."""
    args = _parser().parse_args()
    try:
        request = AaplFundamentalResearchRequest(
            known_at=args.known_at,
            frequency=args.frequency,
            start_period_end=args.start,
            end_period_end=args.end,
            limit=args.limit,
        )
        runtime = ApplicationRuntime.create_default()
        with runtime.open_storage(
            storage_location_from_namespace(args),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            result = AaplFundamentalResearchService(storage).query(request)
        print(
            json.dumps(
                {"notice": _NOTICE, "result": result.to_json_dict()},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        FundamentalResearchError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"Fundamental research query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
