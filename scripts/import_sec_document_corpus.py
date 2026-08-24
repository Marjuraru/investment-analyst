#!/usr/bin/env python3
"""Import a bounded set of official SEC primary filing documents."""

import argparse
import json
import os
import sys

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_document_corpus import SecDocumentCorpusApplication
from investment_analyst.providers.fundamentals.sec_document_pipeline import SecDocumentImportRequest
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--form", action="append", dest="forms")
    selection.add_argument("--accession", action="append", dest="accessions")
    parser.add_argument("--limit-per-form", type=int, default=1)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent.strip():
        print("SEC_USER_AGENT is required and was not provided.", file=sys.stderr)
        return 2
    try:
        result = SecDocumentCorpusApplication.create_default().import_documents(
            asset_id=arguments.asset_id,
            request=SecDocumentImportRequest(
                forms=tuple(arguments.forms or ()),
                accessions=tuple(arguments.accessions or ()),
                limit_per_form=arguments.limit_per_form,
            ),
            location=storage_location_from_namespace(arguments),
            sec_identity=SecEdgarIdentity(user_agent),
        )
        print(json.dumps(result.to_json_dict(), sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001
        print(f"SEC document corpus import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
