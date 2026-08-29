#!/usr/bin/env python3
"""Resolve persisted Form 13F positions through declared correspondence."""

import argparse
from datetime import UTC, date, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.instrument_correspondence import (
    InstrumentCorrespondenceApplication,
)
from investment_analyst.evidence.instrument_correspondence.service import (
    InstrumentCorrespondenceQuery,
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--filer-cik", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--period-from", type=_date)
    parser.add_argument("--period-to", type=_date)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    result = (
        InstrumentCorrespondenceApplication.create_default().query_institutional_holdings_by_asset(
            query=InstrumentCorrespondenceQuery(
                asset_id=args.asset_id,
                manager_cik=args.filer_cik,
                known_at=args.known_at,
                period_from=args.period_from,
                period_to=args.period_to,
                limit=args.limit,
            ),
            location=storage_location_from_namespace(args),
        )
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
