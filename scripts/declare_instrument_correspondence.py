#!/usr/bin/env python3
"""Persist one explicit catalog-backed CUSIP correspondence."""

import argparse
from datetime import UTC, date, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.instrument_correspondence import (
    InstrumentCorrespondenceApplication,
)
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--cusip", required=True)
    parser.add_argument("--title-of-class", required=True)
    parser.add_argument("--effective-from", required=True, type=_date)
    parser.add_argument("--effective-to", type=_date)
    parser.add_argument("--available-at", required=True, type=_time)
    parser.add_argument("--recorded-at", required=True, type=_time)
    parser.add_argument("--declared-by", required=True)
    args = parser.parse_args()
    app = InstrumentCorrespondenceApplication.create_default()
    item = InstrumentCorrespondence.declare(
        asset_id=args.asset_id,
        cusip=args.cusip,
        title_of_class=args.title_of_class,
        effective_from=args.effective_from,
        effective_to=args.effective_to,
        available_at=args.available_at,
        recorded_at=args.recorded_at,
    )
    print(
        app.declare_correspondence(
            correspondence=item,
            catalog_version=app.catalog_version,
            declared_by=args.declared_by,
            location=storage_location_from_namespace(args),
        ).model_dump_json()
    )


if __name__ == "__main__":
    main()
