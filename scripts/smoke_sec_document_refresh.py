#!/usr/bin/env python3
"""Run the SEC primary-document refresh twice in a disposable local workspace."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_document_refresh_models import (
    SecPrimaryDocumentRefreshRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity


def main() -> None:
    """Assert that a fresh repeat does not fetch SEC Archives primary documents."""
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent:
        raise SystemExit("SEC_USER_AGENT is required")
    request = SecPrimaryDocumentRefreshRequest(asset_id="equity:us:aapl")
    with TemporaryDirectory(prefix="investment-analyst-sec-documents-") as temporary:
        application = InvestmentAnalystApplication.create_default()
        location = StorageLocationRequest(legacy_root=Path(temporary))
        identity = SecEdgarIdentity(user_agent)
        first = application.refresh_sec_primary_documents(
            request,
            location=location,
            sec_identity=identity,
        )
        second = application.refresh_sec_primary_documents(
            request,
            location=location,
            sec_identity=identity,
        )
    if second.document_fetch_calls != 0 or not second.coverage_complete:
        raise SystemExit("repeat primary-document coverage was not safely reused")
    print(
        "sec-primary-document-refresh smoke passed "
        f"first_document_fetches={first.document_fetch_calls} "
        f"second_document_fetches={second.document_fetch_calls}"
    )


if __name__ == "__main__":
    main()
