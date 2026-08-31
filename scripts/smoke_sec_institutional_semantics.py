#!/usr/bin/env python3
"""Run the finite real SEC 13F semantic-evidence smoke in a new scratch workspace."""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_institutional_holdings import (
    SecInstitutionalHoldingsApplication,
)
from investment_analyst.application.sec_institutional_semantics import (
    SecInstitutionalSemanticsApplication,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemanticsQuery,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_index import (
    institutional_holdings_filings,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    SecManagerSubmissionsClient,
)
from investment_analyst.workspace.service import WorkspaceService


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--filer-cik", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    arguments = parser.parse_args()
    if arguments.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT is required")
    workspace = WorkspaceService().initialize(arguments.workspace).paths.root
    identity = SecEdgarIdentity(user_agent)
    submissions = SecManagerSubmissionsClient(UrlLibHttpTransport(), identity).fetch(
        arguments.filer_cik
    )
    selected = tuple(
        item.accession
        for item in sorted(
            institutional_holdings_filings(submissions, arguments.filer_cik),
            key=lambda item: (item.accepted_at, item.accession),
            reverse=True,
        )[:2]
    )
    if len(selected) != 2:
        raise RuntimeError("SEC smoke requires two eligible recent 13F reports")
    location = StorageLocationRequest(workspace=workspace)
    reports = SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
        request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik=arguments.filer_cik, accessions=selected
        ),
        location=location,
        sec_identity=identity,
    )
    if len(reports) != 2:
        raise RuntimeError("SEC smoke could not persist two complete 13F reports")
    report_ids = tuple(item.report_id for item in reports)
    request = InstitutionalSemanticsEnrichRequest(
        manager_cik=arguments.filer_cik, report_ids=report_ids, known_at=arguments.known_at
    )
    application = SecInstitutionalSemanticsApplication.create_default()
    first = application.enrich(request=request, location=location)
    second = application.enrich(request=request, location=location)
    if first.created != 2 or second.reused != 2:
        raise RuntimeError("SEC semantic smoke did not prove idempotent enrichment")
    os.environ.pop("SEC_USER_AGENT", None)
    result = application.query(
        query=InstitutionalHoldingsSemanticsQuery(
            manager_cik=arguments.filer_cik,
            report_ids=report_ids,
            known_at=arguments.known_at,
        ),
        location=location,
    )
    if any(
        item.state != "found" or item.total_rows < item.matching_rows for item in result.reports
    ):
        raise RuntimeError("offline semantic query did not recover complete persisted reports")
    prior = min(item.available_at for item in reports) - timedelta(microseconds=1)
    prior_result = application.query(
        query=InstitutionalHoldingsSemanticsQuery(
            manager_cik=arguments.filer_cik,
            report_ids=report_ids,
            known_at=prior,
        ),
        location=location,
    )
    if any(item.state != "missing" for item in prior_result.reports):
        raise RuntimeError("prior point-in-time cut did not hide SEC semantic evidence")
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "known_at": arguments.known_at.isoformat(),
                "accessions": selected,
                "report_ids": [str(value) for value in report_ids],
                "first": first.model_dump(mode="json"),
                "second": second.model_dump(mode="json"),
                "rows": [item.total_rows for item in result.reports],
                "prior_missing": len(prior_result.reports),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
