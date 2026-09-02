#!/usr/bin/env python3
"""Run a finite real 13F metric smoke in a new external workspace."""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from investment_analyst.application.cazatiburones_institutional_metrics import (
    CazatiburonesInstitutionalMetricsApplication,
)
from investment_analyst.application.cazatiburones_institutional_observations import (
    CazatiburonesInstitutionalObservationsApplication,
)
from investment_analyst.application.instrument_correspondence import (
    InstrumentCorrespondenceApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_institutional_holdings import (
    SecInstitutionalHoldingsApplication,
)
from investment_analyst.application.sec_institutional_semantics import (
    SecInstitutionalSemanticsApplication,
)
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
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
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    if args.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        raise RuntimeError("SEC_USER_AGENT is required")
    repository = Path(__file__).resolve().parents[1]
    observed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/smoke_sec_institutional_observations.py"),
            "--workspace",
            str(args.workspace),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    evidence = json.loads(observed.stdout)
    known_at = datetime.now(UTC)
    application = CazatiburonesInstitutionalMetricsApplication.create_default()
    location = StorageLocationRequest(workspace=args.workspace)
    storage_paths = StoragePaths.from_root(WorkspaceService().resolve(args.workspace).storage_root)
    with LocalStorage(storage_paths, read_only=True) as storage:
        reports = InstitutionalHoldingsRepository(storage.raw_records).list_reports(
            manager_cik="1067983", known_at=known_at
        )
    periods = {report.report_period for report in reports}
    submissions = SecManagerSubmissionsClient(
        UrlLibHttpTransport(), SecEdgarIdentity(os.environ["SEC_USER_AGENT"])
    ).fetch("1067983")
    extra = next(
        filing
        for filing in institutional_holdings_filings(submissions, "1067983")
        if filing.report_date is not None and filing.report_date not in periods
    )
    imported = SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
        request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik="1067983", accessions=(extra.accession,)
        ),
        location=location,
        sec_identity=SecEdgarIdentity(os.environ["SEC_USER_AGENT"]),
    )
    report_id = imported[0].report_id
    SecInstitutionalSemanticsApplication.create_default().enrich(
        request=InstitutionalSemanticsEnrichRequest(
            manager_cik="1067983", report_ids=(report_id,), known_at=known_at
        ),
        location=location,
    )
    CazatiburonesInstitutionalObservationsApplication.create_default().normalize(
        InstitutionalObservationRequest(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            report_ids=(report_id,),
            known_at=known_at,
        ),
        location=location,
    )
    with LocalStorage(storage_paths, read_only=True) as storage:
        artifacts = tuple(
            semantics_from_raw_record(record)
            for record in storage.raw_records.list(
                source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
                schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
                available_to=known_at,
            )
        )
    by_period = {}
    for artifact in artifacts:
        if artifact.report_period is not None:
            by_period.setdefault(artifact.report_period, artifact)
    first_artifact, second_artifact = tuple(
        artifact for _period, artifact in sorted(by_period.items())[-2:]
    )
    first_counts = Counter((row.cusip, row.title_of_class) for row in first_artifact.rows)
    second_counts = Counter((row.cusip, row.title_of_class) for row in second_artifact.rows)
    cusip, title_of_class = next(
        key
        for key in sorted(set(first_counts) & set(second_counts))
        if first_counts[key] == second_counts[key] == 1
    )
    correspondence = InstrumentCorrespondence.declare(
        asset_id="equity:us:aapl",
        cusip=cusip,
        title_of_class=title_of_class,
        effective_from=date(1900, 1, 1),
        effective_to=None,
        available_at=known_at,
        recorded_at=known_at,
    )
    correspondence_app = InstrumentCorrespondenceApplication.create_default()
    correspondence_app.declare_correspondence(
        correspondence=correspondence,
        catalog_version=correspondence_app.catalog_version,
        declared_by="smoke_sec_institutional_metrics",
        location=location,
    )
    CazatiburonesInstitutionalObservationsApplication.create_default().normalize(
        InstitutionalObservationRequest(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            report_ids=tuple(UUID(value) for value in evidence["report_ids"]) + (report_id,),
            known_at=known_at,
        ),
        location=location,
    )
    first = application.compute(
        asset_id="equity:us:aapl", manager_cik="1067983", known_at=known_at, location=location
    )
    second = application.compute(
        asset_id="equity:us:aapl", manager_cik="1067983", known_at=known_at, location=location
    )
    if first.metrics_created == 0 or second.metrics_reused != first.metrics_created:
        raise RuntimeError("metric smoke did not prove idempotent persistence")
    os.environ.pop("SEC_USER_AGENT", None)
    print(
        json.dumps(
            {
                "known_at": known_at.isoformat(),
                "observations": evidence,
                "first": first.model_dump(mode="json"),
                "second": second.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
