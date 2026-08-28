#!/usr/bin/env python3
"""Read one local SEC primary-document revision at a point in time."""

import argparse
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_document_corpus import SecDocumentCorpusApplication
from investment_analyst.evidence.sec_documents.models import SecDocumentQuery


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("revision-id must be a UUID") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--form")
    parser.add_argument("--accession")
    parser.add_argument("--revision-id", type=_uuid)
    parser.add_argument("--read-content", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        result = SecDocumentCorpusApplication.create_default().replay(
            query=SecDocumentQuery(
                asset_id=arguments.asset_id,
                known_at=arguments.known_at,
                form=arguments.form,
                accession=arguments.accession,
                revision_id=arguments.revision_id,
                include_content=arguments.read_content,
            ),
            location=storage_location_from_namespace(arguments),
        )
        output: dict[str, object] = {
            "state": result.state,
            "legacy_records_excluded": result.legacy_records_excluded,
        }
        if result.revision is not None:
            output["revision"] = {
                "revision_id": str(result.revision.revision_id),
                "accession": result.revision.document.filing.accession,
                "form": result.revision.document.filing.form,
                "content_sha256": result.revision.content_sha256,
                "content_size_bytes": result.revision.content_size_bytes,
                "available_at": result.revision.available_at.isoformat(),
            }
        print(json.dumps(output, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001
        print(f"SEC document corpus query failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
