#!/usr/bin/env python3
"""Run the finite real SEC smoke for persisted institutional 13F metrics."""

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

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
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
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
    InstitutionalHoldingsFiling,
    institutional_holdings_filings,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    SecManagerSubmissionsClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_CIK = "1350694"
_CUSIP = "037833100"
_CLASS = "COM"


@dataclass(frozen=True, slots=True)
class _SelectedFiling:
    accession: str
    report_period: date
    accepted_at: datetime
    revisions_for_period: int


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


def _latest_filing_by_period(
    filings: tuple[InstitutionalHoldingsFiling, ...],
) -> tuple[_SelectedFiling, ...]:
    grouped: dict[date, list[InstitutionalHoldingsFiling]] = {}
    for filing in filings:
        if filing.report_date is None:
            continue
        if filing.report_date in grouped:
            grouped[filing.report_date].append(filing)
        else:
            grouped[filing.report_date] = [filing]
    selected: list[_SelectedFiling] = []
    for period, period_filings in sorted(grouped.items(), reverse=True):
        latest = max(period_filings, key=lambda item: (item.accepted_at, item.accession))
        selected.append(
            _SelectedFiling(
                accession=latest.accession,
                report_period=period,
                accepted_at=latest.accepted_at,
                revisions_for_period=len(period_filings),
            )
        )
    return tuple(selected)


def _aapl_row_count(artifact) -> int:
    return sum(row.cusip == _CUSIP and row.title_of_class == _CLASS for row in artifact.rows)


def _select_adjacent_aapl_filings(*, user_agent: str) -> tuple[_SelectedFiling, _SelectedFiling]:
    submissions = SecManagerSubmissionsClient(
        UrlLibHttpTransport(), SecEdgarIdentity(user_agent)
    ).fetch(_CIK)
    candidates = _latest_filing_by_period(institutional_holdings_filings(submissions, _CIK))
    if len(candidates) < 2:
        raise RuntimeError("SEC smoke requires at least two distinct report_period candidates")
    current, prior = candidates[:2]
    return prior, current


def _artifacts_for_reports(*, workspace: Path, reports):
    paths = StoragePaths.from_root(WorkspaceService().resolve(workspace).storage_root)
    with LocalStorage(paths, read_only=True) as storage:
        repository = InstitutionalSemanticsRepository(storage.raw_records)
        artifacts = tuple(repository.get_for_parent(report) for report in reports)
    if any(artifact is None for artifact in artifacts):
        raise RuntimeError("SEC smoke could not resolve every selected semantic artifact")
    return tuple(artifact for artifact in artifacts if artifact is not None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT is required")

    head, tree = _git_revision()
    selected = _select_adjacent_aapl_filings(user_agent=user_agent)
    workspace = WorkspaceService().initialize(arguments.workspace).paths.root
    location = StorageLocationRequest(workspace=workspace)
    identity = SecEdgarIdentity(user_agent)
    reports = SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
        request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik=_CIK, accessions=tuple(item.accession for item in selected)
        ),
        location=location,
        sec_identity=identity,
    )
    if len(reports) != len(selected):
        raise RuntimeError("SEC smoke could not persist both selected 13F reports")
    known_at = datetime.now(UTC)
    report_ids = tuple(report.report_id for report in reports)
    semantics = SecInstitutionalSemanticsApplication.create_default()
    semantics.enrich(
        request=InstitutionalSemanticsEnrichRequest(
            manager_cik=_CIK, report_ids=report_ids, known_at=known_at
        ),
        location=location,
    )
    artifacts = _artifacts_for_reports(workspace=workspace, reports=reports)
    artifact_by_accession = {artifact.accession: artifact for artifact in artifacts}
    if set(artifact_by_accession) != {item.accession for item in selected}:
        raise RuntimeError("SEC smoke semantic artifacts do not match selected accessions")
    if {artifact.report_period for artifact in artifacts} != {
        item.report_period for item in selected
    }:
        raise RuntimeError("SEC smoke semantic artifacts do not match selected report_periods")
    aapl_rows = {
        accession: _aapl_row_count(artifact)
        for accession, artifact in sorted(artifact_by_accession.items())
    }
    if any(count != 1 for count in aapl_rows.values()):
        raise RuntimeError(
            "selected SEC filings must each contain exactly one AAPL 037833100/COM row"
        )

    correspondence = InstrumentCorrespondence.declare(
        asset_id=_ASSET_ID,
        cusip=_CUSIP,
        title_of_class=_CLASS,
        effective_from=date(1980, 12, 12),
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
    observations = CazatiburonesInstitutionalObservationsApplication.create_default()
    observation_request = InstitutionalObservationRequest(
        asset_id=_ASSET_ID,
        manager_cik=_CIK,
        report_ids=report_ids,
        known_at=known_at,
    )
    observation_first = observations.normalize(observation_request, location=location)
    observation_second = observations.normalize(observation_request, location=location)
    if (
        observation_first.observations_created == 0
        or observation_second.observations_reused != observation_first.observations_created
    ):
        raise RuntimeError("SEC smoke did not prove AAPL observation idempotence")

    metrics = CazatiburonesInstitutionalMetricsApplication.create_default()
    first = metrics.compute(
        asset_id=_ASSET_ID, manager_cik=_CIK, known_at=known_at, location=location
    )
    second = metrics.compute(
        asset_id=_ASSET_ID, manager_cik=_CIK, known_at=known_at, location=location
    )
    if first.metrics_created == 0 or second.metrics_reused != first.metrics_created:
        raise RuntimeError("metric smoke did not prove idempotent persistence")

    os.environ.pop("SEC_USER_AGENT", None)
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "head": head,
                "tree": tree,
                "known_at": known_at.isoformat(),
                "accessions": [item.accession for item in selected],
                "report_periods": [item.report_period.isoformat() for item in selected],
                "selected_revision_counts": {
                    item.accession: item.revisions_for_period for item in selected
                },
                "aapl_037833100_com_rows": aapl_rows,
                "selection_omissions": {},
                "observations": {
                    "first": observation_first.model_dump(mode="json"),
                    "second": observation_second.model_dump(mode="json"),
                },
                "metrics": {
                    "first": first.model_dump(mode="json"),
                    "second": second.model_dump(mode="json"),
                    "omissions_by_reason": dict(sorted(first.skipped_by_reason.items())),
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
