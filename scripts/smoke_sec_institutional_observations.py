#!/usr/bin/env python3
"""Run the finite real SEC smoke for linked institutional 13F observations."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ACCESSIONS = ("0001193125-26-226661", "0001193125-26-352200")
_ASSET_ID = "equity:us:aapl"
_CIK = "1067983"
_CUSIP = "037833100"
_CLASS = "COM"


def _git_revision() -> tuple[str, str]:
    repository = Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head, tree


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--filer-cik", default=_CIK)
    arguments = parser.parse_args()
    if arguments.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    if arguments.filer_cik.zfill(10) != _CIK.zfill(10):
        raise RuntimeError("smoke is fixed to Berkshire Hathaway CIK 1067983")
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT is required")

    started_at = datetime.now(UTC)
    head, tree = _git_revision()
    workspace = WorkspaceService().initialize(arguments.workspace).paths.root
    location = StorageLocationRequest(workspace=workspace)
    reports = SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
        request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik=arguments.filer_cik, accessions=_ACCESSIONS
        ),
        location=location,
        sec_identity=SecEdgarIdentity(user_agent),
    )
    if len(reports) != len(_ACCESSIONS):
        raise RuntimeError("SEC smoke did not persist both fixed institutional reports")
    report_ids = tuple(item.report_id for item in reports)
    now = datetime.now(UTC)
    semantics_request = InstitutionalSemanticsEnrichRequest(
        manager_cik=arguments.filer_cik, report_ids=report_ids, known_at=now
    )
    semantics = SecInstitutionalSemanticsApplication.create_default()
    first_enrichment = semantics.enrich(request=semantics_request, location=location)
    second_enrichment = semantics.enrich(request=semantics_request, location=location)
    if first_enrichment.created != 2 or second_enrichment.reused != 2:
        raise RuntimeError("SEC smoke did not prove semantic enrichment idempotence")

    correspondence = InstrumentCorrespondence.declare(
        asset_id=_ASSET_ID,
        cusip=_CUSIP,
        title_of_class=_CLASS,
        effective_from=date(1980, 12, 12),
        effective_to=None,
        available_at=now,
        recorded_at=now,
    )
    correspondence_app = InstrumentCorrespondenceApplication.create_default()
    correspondence_app.declare_correspondence(
        correspondence=correspondence,
        catalog_version=correspondence_app.catalog_version,
        declared_by="smoke_sec_institutional_observations",
        location=location,
    )
    storage_paths = StoragePaths.from_root(WorkspaceService().resolve(workspace).storage_root)
    with LocalStorage(storage_paths, read_only=True) as storage:
        raw_before = storage.raw_records.count()

    observations = CazatiburonesInstitutionalObservationsApplication.create_default()
    request = InstitutionalObservationRequest(
        asset_id=_ASSET_ID,
        manager_cik=arguments.filer_cik,
        report_ids=report_ids,
        known_at=now,
    )
    first = observations.normalize(request, location=location)
    second = observations.normalize(request, location=location)
    if first.observations_created == 0 or second.observations_reused != first.observations_created:
        raise RuntimeError("SEC smoke did not prove observation idempotence")
    with LocalStorage(storage_paths, read_only=True) as storage:
        if storage.raw_records.count() != raw_before:
            raise RuntimeError("observation normalization changed persisted bundle lineage")

    os.environ.pop("SEC_USER_AGENT", None)
    result = observations.query(
        InstitutionalObservationQuery(
            asset_id=_ASSET_ID,
            known_at=now + timedelta(seconds=1),
            cusip=_CUSIP,
            field_name="institutional_reported_fair_value",
            limit=1,
        ),
        location=location,
    )
    if result.total_matching < 2 or not result.truncated:
        raise RuntimeError("SEC smoke did not retain multiple AAPL rows with pagination")
    if any(
        view.observation.unit != "USD"
        or view.observation.quality.value != "valid"
        or view.row.cusip != _CUSIP
        or view.row.title_of_class != _CLASS
        for view in result.observations
    ):
        raise RuntimeError("SEC smoke did not preserve post-2023 USD AAPL lineage")
    prior = observations.query(
        InstitutionalObservationQuery(
            asset_id=_ASSET_ID,
            known_at=now - timedelta(microseconds=1),
            cusip=_CUSIP,
            field_name="institutional_reported_fair_value",
        ),
        location=location,
    )
    if prior.total_matching != 0 or prior.observations:
        raise RuntimeError("prior point-in-time cut did not hide correspondence-bound observations")
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "head": head,
                "tree": tree,
                "started_at": started_at.isoformat(),
                "accessions": _ACCESSIONS,
                "report_ids": [str(item) for item in report_ids],
                "correspondence_id": str(correspondence.correspondence_id),
                "first": first.model_dump(mode="json"),
                "second": second.model_dump(mode="json"),
                "total_matching": result.total_matching,
                "truncated": result.truncated,
                "prior_total_matching": prior.total_matching,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
