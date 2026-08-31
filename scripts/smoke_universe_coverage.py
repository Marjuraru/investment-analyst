#!/usr/bin/env python3
"""Run the real twelve-asset coverage smoke in a new scratch workspace."""

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from investment_analyst.application.crypto_spot_daily_models import CryptoSpotDailyRefreshRequest
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.listed_market_refresh_models import ListedMarketRefreshRequest
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
)
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import (
    EvidenceState,
    UniverseCoverageRequest,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.workspace.service import WorkspaceService

_MARKET_START = date(2026, 6, 1)
_MARKET_END = date(2026, 8, 28)
_FUNDAMENTAL_START = date(2020, 1, 1)
_LISTED_ASSETS = (
    "equity:us:amzn",
    "equity:us:cat",
    "equity:us:cvx",
    "equity:us:jnj",
    "equity:us:msft",
    "equity:us:nvda",
    "etf:us:qqq",
    "etf:us:spy",
    "etf:us:tlt",
)
_CRYPTO_ASSETS = ("crypto:ada-usd", "crypto:link-usd", "crypto:sol-usd")
_SEC_ASSETS = _LISTED_ASSETS[:6]
_ALL_NEW_ASSETS = tuple(sorted((*_LISTED_ASSETS, *_CRYPTO_ASSETS)))
_SECRET_NAMES = ("ALPACA_API_KEY", "ALPACA_API_SECRET", "SEC_USER_AGENT")


def _known_at(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    result = parsed.astimezone(UTC)
    if result.date() <= _MARKET_END:
        raise argparse.ArgumentTypeError("known-at must follow the complete final market day")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the real multi-domain coverage smoke.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--known-at", required=True, type=_known_at)
    return parser


def _credentials() -> tuple[AlpacaCredentials, SecEdgarIdentity]:
    values = {name: os.environ.get(name, "").strip() for name in _SECRET_NAMES}
    missing = tuple(name for name, value in values.items() if not value)
    if missing:
        raise RuntimeError(f"missing required environment variable: {missing[0]}")
    return (
        AlpacaCredentials(
            api_key=values["ALPACA_API_KEY"],
            secret_key=values["ALPACA_API_SECRET"],
        ),
        SecEdgarIdentity(user_agent=values["SEC_USER_AGENT"]),
    )


def _require_new_scratch_workspace(path: Path) -> Path:
    if path.exists():
        raise RuntimeError(
            "workspace must be a new scratch path; existing evidence is never replaced"
        )
    return WorkspaceService().initialize(path).paths.root


def _run_once(
    application: InvestmentAnalystApplication,
    *,
    location: StorageLocationRequest,
    known_at: datetime,
    alpaca: AlpacaCredentials,
    sec: SecEdgarIdentity,
    expect_new_evidence: bool,
) -> dict[str, object]:
    listed = tuple(
        application.refresh_listed_market(
            ListedMarketRefreshRequest(
                asset_id=asset_id,
                market_start=_MARKET_START,
                market_end=_MARKET_END,
                requested_known_at=known_at,
            ),
            location=location,
            alpaca_credentials=alpaca,
        )
        for asset_id in _LISTED_ASSETS
    )
    crypto = tuple(
        application.refresh_crypto_spot_daily(
            CryptoSpotDailyRefreshRequest(
                asset_id=asset_id,
                market_start=_MARKET_START,
                market_end=_MARKET_END,
                requested_known_at=known_at,
            ),
            location=location,
        )
        for asset_id in _CRYPTO_ASSETS
    )
    fundamentals = tuple(
        application.refresh_sec_fundamentals(
            SecIssuerFundamentalRefreshRequest(
                asset_id=asset_id,
                frequency=frequency,
                requested_known_at=known_at,
            ),
            location=location,
            sec_identity=sec,
        )
        for asset_id in _SEC_ASSETS
        for frequency in (DataFrequency.ANNUAL, DataFrequency.QUARTERLY)
    )
    if expect_new_evidence:
        if any(summary.bars_received == 0 for summary in listed):
            raise RuntimeError(
                "listed market smoke requires non-empty bars for every new listed asset"
            )
        if any(summary.candles_received == 0 for summary in crypto):
            raise RuntimeError("crypto market smoke requires non-empty candles for every new asset")
    else:
        if any(summary.raw_records_created or summary.observations_created for summary in listed):
            raise RuntimeError(
                "idempotent listed market smoke must not create new raw records or observations"
            )
        if any(summary.raw_records_created or summary.observations_created for summary in crypto):
            raise RuntimeError(
                "idempotent crypto market smoke must not create new raw records or observations"
            )
    if any(not summary.traceability_verified for summary in (*listed, *crypto, *fundamentals)):
        raise RuntimeError("smoke refresh returned unverified traceability")
    return {
        "earliest_sec_fetched_at": min(summary.fetched_at for summary in fundamentals).isoformat(),
        "listed": [
            {
                "asset_id": summary.asset_id,
                "bars_received": summary.bars_received,
                "raw_records_created": summary.raw_records_created,
                "raw_records_reused": summary.raw_records_reused,
                "observations_created": summary.observations_created,
                "observations_reused": summary.observations_reused,
                "coverage_receipts_created": summary.coverage_receipts_created,
                "coverage_receipts_reused": summary.coverage_receipts_reused,
            }
            for summary in listed
        ],
        "crypto": [
            {
                "asset_id": summary.asset_id,
                "candles_received": summary.candles_received,
                "raw_records_created": summary.raw_records_created,
                "raw_records_reused": summary.raw_records_reused,
                "observations_created": summary.observations_created,
                "observations_reused": summary.observations_reused,
            }
            for summary in crypto
        ],
        "fundamentals": [
            {
                "asset_id": summary.asset_id,
                "frequency": summary.request.frequency.value,
                "raw_records_created": summary.raw_records_created,
                "raw_records_reused": summary.raw_records_reused,
                "observations_created": summary.observations_created,
                "observations_reused": summary.observations_reused,
                "metric_results_created": summary.metric_results_created,
                "metric_results_reused": summary.metric_results_reused,
            }
            for summary in fundamentals
        ],
    }


def _query_offline(workspace: Path, known_at: datetime):
    for name in _SECRET_NAMES:
        os.environ.pop(name, None)
    return UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        UniverseCoverageRequest(
            known_at=known_at,
            market_start=_MARKET_START,
            market_end=_MARKET_END,
            fundamental_start=_FUNDAMENTAL_START,
            fundamental_end=_MARKET_END,
            asset_ids=_ALL_NEW_ASSETS,
        ),
    )


def main() -> int:
    args = _parser().parse_args()
    workspace = _require_new_scratch_workspace(args.workspace)
    alpaca, sec = _credentials()
    application = InvestmentAnalystApplication.create_default()
    location = StorageLocationRequest(workspace=workspace)
    first = _run_once(
        application,
        location=location,
        known_at=args.known_at,
        alpaca=alpaca,
        sec=sec,
        expect_new_evidence=True,
    )
    second = _run_once(
        application,
        location=location,
        known_at=args.known_at,
        alpaca=alpaca,
        sec=sec,
        expect_new_evidence=False,
    )
    result = _query_offline(workspace, args.known_at)
    by_asset = {item.asset_id: item for item in result.assets}
    if tuple(sorted(by_asset)) != _ALL_NEW_ASSETS:
        raise RuntimeError("offline coverage query did not return every new catalog asset")
    if any(by_asset[item].market.evidence is not EvidenceState.PRESENT for item in _ALL_NEW_ASSETS):
        raise RuntimeError("offline coverage query did not recover all persisted market evidence")
    if any(
        by_asset[item].fundamentals.evidence is not EvidenceState.PRESENT for item in _SEC_ASSETS
    ):
        raise RuntimeError("offline coverage query did not recover all persisted SEC evidence")
    # SEC fundamentals gate on each filing's real acceptance_at, which for established issuers
    # already predates any cut this smoke can construct within the requested-range validator
    # (fundamental_end must never be after known_at's date), so it cannot demonstrate missing
    # SEC evidence without hardcoding external, out-of-repo filing-calendar knowledge. Market bars
    # instead gate on our own local ingestion time, which this run performs live: a known_at fixed
    # at the first instant market_end is legally queryable (market_end + 1 UTC day) is guaranteed
    # to predate that real fetch, which only happens later, during this same execution.
    prior = datetime(_MARKET_END.year, _MARKET_END.month, _MARKET_END.day, tzinfo=UTC) + timedelta(
        days=1
    )
    prior_result = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        UniverseCoverageRequest(
            known_at=prior,
            market_start=_MARKET_START,
            market_end=_MARKET_END,
            fundamental_start=_FUNDAMENTAL_START,
            fundamental_end=_MARKET_END,
            asset_ids=_ALL_NEW_ASSETS,
        ),
    )
    if not all(item.market.evidence is EvidenceState.MISSING for item in prior_result.assets):
        raise RuntimeError(
            "prior cut did not demonstrate market point-in-time missing evidence "
            "(local ingestion available_at must gate visibility, not just the elapsed trading day)"
        )
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "known_at": args.known_at.isoformat(),
                "new_assets": list(_ALL_NEW_ASSETS),
                "first": first,
                "second": second,
                "offline_assets": len(result.assets),
                "prior_missing_market": sum(
                    item.market.evidence is EvidenceState.MISSING for item in prior_result.assets
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
