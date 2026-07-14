#!/usr/bin/env python3
"""Bootstrap Apple data and diagnostics inside one persistent workspace."""

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.diagnostic_pipeline import (
    MarketDiagnosticPipeline,
)
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import (
    MarketStatisticsPipeline,
)
from investment_analyst.application.aapl_bootstrap import (
    AaplWorkspaceBootstrapError,
    AaplWorkspaceBootstrapPipeline,
    BootstrapIncompleteError,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
)
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecAaplFundamentalsPipeline,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
)
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage, StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
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
    parser.add_argument("--require-complete", action="store_true")
    return parser


def _build_pipeline(
    storage: LocalStorage,
    *,
    runtime: ApplicationRuntime,
    workspace_id,
    alpaca_credentials: AlpacaCredentials,
    sec_identity: SecEdgarIdentity,
) -> AaplWorkspaceBootstrapPipeline:
    alpaca_configuration = resolve_alpaca_configuration(runtime.provider_resolver)
    sec_configuration = resolve_sec_configuration(runtime.provider_resolver)
    transport = UrlLibHttpTransport()
    sec_client = SecEdgarClient(
        transport,
        sec_identity,
        cik=sec_configuration.cik,
        ticker=sec_configuration.ticker,
    )
    alpaca_client = AlpacaStockClient(transport, alpaca_credentials)
    point_in_time = SecAaplFundamentalPointInTimeService(storage)
    history = HistoricalMarketDataService(storage)
    return AaplWorkspaceBootstrapPipeline(
        storage,
        workspace_id=workspace_id,
        sec_fetch_pipeline=SecAaplFundamentalsPipeline(
            storage,
            sec_client,
            configuration=sec_configuration,
        ),
        sec_observation_pipeline=SecAaplObservationPipeline(
            storage,
            SecCompanyFactsNormalizer(),
        ),
        market_pipeline=AlpacaHistoricalPipeline(
            storage,
            alpaca_client,
            configuration=alpaca_configuration,
        ),
        fundamental_metric_pipeline=SecAaplFundamentalMetricPipeline(
            storage,
            point_in_time,
            SecFundamentalMetricEngine(),
        ),
        fundamental_diagnostic_pipeline=SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            SecFundamentalDiagnosticEngine(),
        ),
        market_statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
        ),
        market_diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
        ),
        consolidated_service=AaplConsolidatedDiagnosticService(storage),
    )


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
            requested_known_at=arguments.known_at,
            require_complete=arguments.require_complete,
        )
        credentials = AlpacaCredentials(api_key=api_key, secret_key=secret_key)
        identity = SecEdgarIdentity(sec_user_agent)
        runtime = ApplicationRuntime.create_default()
        initialization = runtime.workspace_service.initialize(arguments.workspace)
        location = StorageLocationRequest(workspace=initialization.paths.root)
        with runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            summary = _build_pipeline(
                storage,
                runtime=runtime,
                workspace_id=initialization.manifest.workspace_id,
                alpaca_credentials=credentials,
                sec_identity=identity,
            ).run(request)
        print(
            json.dumps(
                _payload(summary, initialization.paths.root),
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
