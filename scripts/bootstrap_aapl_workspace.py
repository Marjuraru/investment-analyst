#!/usr/bin/env python3
"""Bootstrap Apple data and diagnostics inside one persistent workspace."""

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.aapl_bootstrap import (
    AaplWorkspaceBootstrapError,
    BootstrapIncompleteError,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError

_NOTICE = (
    "Persistent and auditable Apple bootstrap using official SEC EDGAR documents and "
    "Alpaca Market Data IEX daily bars. IEX is a single-exchange feed, not consolidated "
    "SIP coverage. Point-in-time eligibility uses available_at, market and fundamental "
    "diagnostics remain separate, no recommendation is produced, and no orders execute."
)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


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


def _refresh_mode(value: str) -> AaplRefreshMode:
    mapping = {
        "auto": AaplRefreshMode.AUTO,
        "full": AaplRefreshMode.FULL,
    }
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("refresh-mode must be auto or full") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--market-start", required=True, type=_date_value)
    parser.add_argument("--market-end", required=True, type=_date_value)
    parser.add_argument(
        "--fundamental-frequency",
        required=True,
        type=_frequency,
    )
    parser.add_argument("--known-at", type=_aware_datetime)
    parser.add_argument("--refresh-mode", type=_refresh_mode, default=AaplRefreshMode.AUTO)
    parser.add_argument("--require-complete", action="store_true")
    return parser


def _payload(summary, workspace_root: Path) -> dict[str, object]:
    serialized = summary.to_json_dict()
    counts = serialized["counts"]
    return {
        "notice": _NOTICE,
        "workspace": {
            "workspace_id": serialized["workspace_id"],
            "root": str(workspace_root),
        },
        "request": serialized["request"],
        "refresh_plan": serialized["refresh_plan"],
        "effective_known_at": serialized["effective_known_at"],
        "source": {
            "fundamentals": "SEC EDGAR",
            "market": "Alpaca Market Data",
            "feed": serialized["feed"],
            "coverage": "IEX single-exchange feed; not consolidated SIP",
        },
        "stages": serialized["stages"],
        "consolidated": serialized["consolidated"],
        "summary": {
            "overall_status": serialized["overall_status"],
            "counts": counts,
            "traceability_verified": serialized["traceability_verified"],
        },
    }


def main() -> int:
    """Initialize one workspace, open one writer, and execute the complete bootstrap."""
    arguments = _parser().parse_args()
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_API_SECRET", "")
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not api_key.strip() or not secret_key.strip() or not sec_user_agent.strip():
        print(
            "ALPACA_API_KEY, ALPACA_API_SECRET, and SEC_USER_AGENT are required.",
            file=sys.stderr,
        )
        return 2

    try:
        request = AaplWorkspaceBootstrapRequest(
            market_start=arguments.market_start,
            market_end=arguments.market_end,
            fundamental_frequency=arguments.fundamental_frequency,
            refresh_mode=arguments.refresh_mode,
            requested_known_at=arguments.known_at,
            require_complete=arguments.require_complete,
        )
        credentials = AlpacaCredentials(api_key=api_key, secret_key=secret_key)
        identity = SecEdgarIdentity(sec_user_agent)
        result = InvestmentAnalystApplication.create_default().bootstrap_aapl_workspace(
            request,
            workspace=arguments.workspace,
            alpaca_credentials=credentials,
            sec_identity=identity,
        )
        print(
            json.dumps(
                _payload(result.summary, result.initialization.paths.root),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except BootstrapIncompleteError as error:
        print(f"Apple workspace bootstrap incomplete: {error}", file=sys.stderr)
        return 3
    except (
        AaplWorkspaceBootstrapError,
        ApplicationRuntimeError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"Apple workspace bootstrap failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"Apple workspace bootstrap failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
