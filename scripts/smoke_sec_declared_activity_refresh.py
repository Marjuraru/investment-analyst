#!/usr/bin/env python3
"""Run the incremental SEC declared-activity refresh twice in a disposable workspace.

The smoke touches the official SEC hosts through the counted transport, never the permanent
workspace, and prints only counts, accessions, cuts, and source identities. The configured
``SEC_USER_AGENT`` value is never printed or persisted by this script.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from investment_analyst.application.cazatiburones_universe_activity_models import (
    CazatiburonesUniverseActivityRequest,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_declared_activity_refresh_models import (
    SecDeclaredActivityRefreshRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport

DEFAULT_ASSET_ID = "equity:us:aapl"
_ARCHIVES_CALLS_PER_ACCESSION = 3


class _CountingSecTransport:
    """Count SEC requests by official host surface while preserving the exact request."""

    def __init__(self) -> None:
        self._transport = UrlLibHttpTransport()
        self.submissions_calls = 0
        self.archives_calls = 0

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        if parsed.hostname == "www.sec.gov" and parsed.path.startswith("/Archives/"):
            self.archives_calls += 1
        elif parsed.hostname == "data.sec.gov" and parsed.path.startswith("/submissions/"):
            self.submissions_calls += 1
        return self._transport.get(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset-id", default=os.environ.get("SEC_SMOKE_ASSET_ID", DEFAULT_ASSET_ID)
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Execute the real refresh, its repeat, and a read-only query against one SEC issuer."""
    arguments = _parser().parse_args(argv)
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent:
        raise SystemExit("SEC_USER_AGENT is required")
    request = SecDeclaredActivityRefreshRequest(asset_id=arguments.asset_id)
    transport = _CountingSecTransport()
    application = InvestmentAnalystApplication(
        ApplicationRuntime.create_default(),
        transport_factory=lambda: transport,
    )
    identity = SecEdgarIdentity(user_agent)
    with TemporaryDirectory(prefix="investment-analyst-sec-declared-activity-") as temporary:
        location = StorageLocationRequest(legacy_root=Path(temporary))
        first = application.refresh_sec_declared_activity(
            request,
            location=location,
            sec_identity=identity,
        )
        first_submissions = transport.submissions_calls
        first_archives = transport.archives_calls
        first_declared = application.query_cazatiburones_declared_activity(
            asset_id=arguments.asset_id,
            known_at=first.submissions_checked_at,
            location=location,
        )
        second = application.refresh_sec_declared_activity(
            request,
            location=location,
            sec_identity=identity,
        )
        repeat_submissions = transport.submissions_calls - first_submissions
        repeat_archives = transport.archives_calls - first_archives
        cut = second.submissions_checked_at
        declared = application.query_cazatiburones_declared_activity(
            asset_id=arguments.asset_id,
            known_at=cut,
            location=location,
        )
        universe = application.query_cazatiburones_universe_activity(
            CazatiburonesUniverseActivityRequest(known_at=cut),
            location=location,
        )
    _assert_contract(
        first=first,
        second=second,
        declared=declared,
        first_declared=first_declared,
        universe=universe,
        first_submissions=first_submissions,
        first_archives=first_archives,
        repeat_submissions=repeat_submissions,
        repeat_archives=repeat_archives,
    )
    print(
        json.dumps(
            {
                "asset_id": arguments.asset_id,
                "cut": first.submissions_checked_at.isoformat(),
                "repeat_cut": second.submissions_checked_at.isoformat(),
                "submissions_calls": [first_submissions, repeat_submissions],
                "archives_calls": [first_archives, repeat_archives],
                "source_ids": sorted(
                    {
                        first.submissions_source_id,
                        first.insider.source_id,
                        first.beneficial.source_id,
                    }
                ),
                "insider_selected": list(first.insider.accessions_selected),
                "beneficial_selected": list(first.beneficial.accessions_selected),
                "insider_statements": [
                    first.insider.statements_created,
                    first.insider.statements_reused,
                ],
                "beneficial_statements": [
                    first.beneficial.statements_created,
                    first.beneficial.statements_reused,
                ],
                "observations": [
                    first.observations_created,
                    first.observations_reused,
                    first.observations_skipped,
                ],
                "metrics": [
                    first.metrics_created,
                    first.metrics_reused,
                    first.metrics_skipped,
                ],
                "repeat_observations": [
                    second.observations_created,
                    second.observations_reused,
                ],
                "repeat_metrics": [second.metrics_created, second.metrics_reused],
                "backlog": [first.backlog_count, second.backlog_count],
                "coverage_complete": [first.coverage_complete, second.coverage_complete],
                "traceability_verified": second.traceability_verified,
                "read_only_statements": [
                    first_declared.total_statements,
                    declared.total_statements,
                ],
                "universe_families": [
                    {
                        "asset_id": item.asset_id,
                        "insider": item.insider.evidence.value,
                        "insider_statements": item.insider.statements,
                        "beneficial": item.beneficial.evidence.value,
                        "beneficial_statements": item.beneficial.statements,
                        "institutional": item.institutional.evidence.value,
                    }
                    for item in universe.assets
                    if item.asset_id == arguments.asset_id
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _assert_contract(
    *,
    first,
    second,
    declared,
    first_declared,
    universe,
    first_submissions: int,
    first_archives: int,
    repeat_submissions: int,
    repeat_archives: int,
) -> None:
    selected = len(first.insider.accessions_selected) + len(first.beneficial.accessions_selected)
    if first_submissions != 1 or repeat_submissions != 1:
        raise SystemExit("each run must perform exactly one Submissions request")
    if selected and not 1 <= first_archives <= _ARCHIVES_CALLS_PER_ACCESSION * selected:
        raise SystemExit("initial Archives requests were not bounded by the selection")
    if not selected and first_archives:
        raise SystemExit("an empty selection must not request any Archives resource")
    if repeat_archives:
        raise SystemExit("the repeat must not request any Archives resource")
    if second.submissions_created or second.submissions_reused != 1:
        raise SystemExit("the repeat must reuse the verified Submissions snapshot")
    if second.submissions_checked_at < first.submissions_checked_at:
        raise SystemExit("the repeat cut must not move backwards")
    # The repeat reuses every already-persisted layer-1 and layer-2 identity. Layer 3 is the
    # only append-only layer that may add a row, and only because its integrated identity embeds
    # the point-in-time cut: the repeat observes a later `known_at`, so it persists the metric
    # result of that new cut instead of rewriting the previous one.
    if second.insider.statements_created or second.beneficial.statements_created:
        raise SystemExit("the repeat must not create declared statements")
    if second.observations_created:
        raise SystemExit("the repeat must reuse every declared-activity observation")
    if declared.known_at != second.submissions_checked_at:
        raise SystemExit("the read-only query must observe the declared cut")
    if declared.total_statements != first_declared.total_statements:
        raise SystemExit("the repeat must not change the persisted statement count")
    statements = len(first.insider.accessions_imported) + len(first.beneficial.accessions_imported)
    if declared.total_statements < statements:
        raise SystemExit("persisted statements are not visible at the declared cut")
    for activity in universe.assets:
        if activity.asset_id != first.asset_id:
            continue
        if activity.insider.evidence.value == "present" and not first.insider.accessions_imported:
            raise SystemExit("insider activity is present without persisted statements")
        if (
            activity.beneficial.evidence.value == "present"
            and not first.beneficial.accessions_imported
        ):
            raise SystemExit("beneficial activity is present without persisted statements")
    if not universe.assets:
        raise SystemExit("the universe activity read must answer for the refreshed asset")


if __name__ == "__main__":
    main()
