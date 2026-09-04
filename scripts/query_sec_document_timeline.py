#!/usr/bin/env python3
"""Point-in-time timeline query for official SEC document revisions."""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_document_timeline import (
    SecDocumentTimelineApplication,
)
from investment_analyst.evidence.sec_documents.timeline_models import SecDocumentTimelineQuery


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    location_group = parser.add_mutually_exclusive_group(required=True)
    location_group.add_argument(
        "--workspace",
        type=Path,
        help="Path to an initialized investment-analyst workspace directory.",
    )
    location_group.add_argument(
        "--legacy-root",
        "--root",
        dest="legacy_root",
        type=Path,
        help="Path to a legacy direct storage root directory.",
    )

    parser.add_argument(
        "--known-at",
        required=True,
        type=_aware_datetime,
        help="Point-in-time cut timestamp.",
    )
    parser.add_argument(
        "--asset-id",
        action="append",
        default=[],
        dest="asset_ids",
        help="Catalog asset identifier to query (can be repeated).",
    )
    parser.add_argument(
        "--filer-cik",
        action="append",
        default=[],
        dest="filer_ciks",
        help="SEC filer CIK to query (can be repeated).",
    )
    parser.add_argument(
        "--form",
        action="append",
        default=[],
        dest="forms",
        help="SEC form filter (can be repeated).",
    )
    parser.add_argument("--accession", help="SEC accession number filter.")
    parser.add_argument(
        "--available-from",
        type=_iso_date,
        help="Public availability start date (inclusive).",
    )
    parser.add_argument(
        "--available-to",
        type=_iso_date,
        help="Public availability end date (inclusive).",
    )
    parser.add_argument("--limit", type=int, help="Maximum number of revisions to return.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        location = StorageLocationRequest(
            workspace=args.workspace,
            legacy_root=args.legacy_root,
        )
        query = SecDocumentTimelineQuery(
            known_at=args.known_at,
            asset_ids=tuple(args.asset_ids),
            filer_ciks=tuple(args.filer_ciks),
            forms=tuple(args.forms),
            accession=args.accession,
            available_from=args.available_from,
            available_to=args.available_to,
            limit=args.limit,
        )
        result = SecDocumentTimelineApplication.create_default().query(
            query=query,
            location=location,
        )
        output: dict[str, object] = {
            "state": result.state,
            "known_at": result.known_at.isoformat(),
            "matched_count": result.matched_count,
            "returned_count": result.returned_count,
            "legacy_records_excluded": result.legacy_records_excluded,
            "truncated": result.truncated,
            "entries": [
                {
                    "family": entry.family,
                    "revision_id": str(entry.revision_id),
                    "asset_id": entry.asset_id,
                    "filer_cik": entry.filer_cik,
                    "form": entry.form,
                    "accession": entry.accession,
                    "document_name": entry.document_name,
                    "filing_date": entry.filing_date.isoformat(),
                    "report_date": entry.report_date.isoformat() if entry.report_date else None,
                    "accepted_at": entry.accepted_at.isoformat(),
                    "available_at": entry.available_at.isoformat(),
                    "content_sha256": entry.content_sha256,
                    "content_size_bytes": entry.content_size_bytes,
                    "source_url": entry.source_url,
                    "is_amendment": entry.is_amendment,
                }
                for entry in result.entries
            ],
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001
        print(f"SEC document timeline query failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
