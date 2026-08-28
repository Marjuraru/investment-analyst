#!/usr/bin/env python3
"""Import bounded official SEC Schedule 13D/13G evidence."""

import argparse
import json
import os
import sys

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_beneficial_ownership import (
    SecBeneficialOwnershipApplication,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_pipeline import (
    SecBeneficialOwnershipImportRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--form", action="append", dest="forms")
    group.add_argument("--accession", action="append", dest="accessions")
    parser.add_argument("--limit-per-form", type=int, default=1)
    arguments = parser.parse_args()
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent.strip():
        print("SEC_USER_AGENT is required and was not provided.", file=sys.stderr)
        return 2
    try:
        statements = SecBeneficialOwnershipApplication.create_default().import_beneficial_ownership(
            asset_id=arguments.asset_id,
            request=SecBeneficialOwnershipImportRequest(
                forms=tuple(arguments.forms or ()),
                accessions=tuple(arguments.accessions or ()),
                limit_per_form=arguments.limit_per_form,
            ),
            location=storage_location_from_namespace(arguments),
            sec_identity=SecEdgarIdentity(user_agent),
        )
        print(
            json.dumps(
                {
                    "state": "found" if statements else "missing",
                    "statements": [
                        {
                            "statement_id": str(item.statement_id),
                            "accession": item.document_revision.document.filing.accession,
                            "form": item.form,
                            "available_at": item.available_at.isoformat(),
                        }
                        for item in statements
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"SEC beneficial ownership import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
