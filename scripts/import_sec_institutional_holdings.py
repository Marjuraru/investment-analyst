#!/usr/bin/env python3
"""Import bounded official SEC Form 13F evidence for one manager CIK."""

import argparse
import json
import os
import sys

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_institutional_holdings import (
    SecInstitutionalHoldingsApplication,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--filer-cik", required=True)
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
        reports = (
            SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
                request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                    filer_cik=arguments.filer_cik,
                    forms=tuple(arguments.forms or ()),
                    accessions=tuple(arguments.accessions or ()),
                    limit_per_form=arguments.limit_per_form,
                ),
                location=storage_location_from_namespace(arguments),
                sec_identity=SecEdgarIdentity(user_agent),
            )
        )
        print(
            json.dumps(
                {
                    "state": "found" if reports else "missing",
                    "reports": [
                        {
                            "report_id": str(item.report_id),
                            "accession": item.cover_revision.document.filing.accession,
                            "form": item.cover_revision.document.filing.form,
                            "available_at": item.available_at.isoformat(),
                            "declared_entry_total": item.declared_entry_total,
                            "parsed_entry_total": item.parsed_entry_total,
                            "declared_value_total": (
                                None
                                if item.declared_value_total is None
                                else str(item.declared_value_total)
                            ),
                            "parsed_value_total": str(item.parsed_value_total),
                            "value_total_matches": item.value_total_matches,
                        }
                        for item in reports
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"SEC institutional holdings import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
