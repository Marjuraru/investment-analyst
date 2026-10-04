"""Safe typed failure reasons for the fresh SEC Submissions persistence primitive."""

from pathlib import Path

import pytest

from investment_analyst.application.sec_submissions_refresh import (
    SecSubmissionsRefreshError,
    SecSubmissionsRefreshService,
)
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.failure_reasons import ProviderFailureReason
from investment_analyst.storage import LocalStorage, StoragePaths


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id="equity:us:aapl",
        cik="0000320193",
        ticker="AAPL",
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


class _FailedIssuer:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def fetch_submissions(self):
        raise self.error


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (RuntimeError("secret response body"), ProviderFailureReason.SEC_SUBMISSIONS_FETCH_FAILED),
        (
            SecSubmissionsRefreshError(
                "inner safe wrapper",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_FETCH_FAILED,
            ),
            ProviderFailureReason.SEC_DOCUMENT_FETCH_FAILED,
        ),
        (
            SecSubmissionsRefreshError("unreviewed", reason_code="secret-token"),
            ProviderFailureReason.SEC_SUBMISSIONS_FETCH_FAILED,
        ),
    ],
)
def test_fetch_failure_is_safe_and_preserves_only_known_typed_causes(
    tmp_path: Path,
    cause: Exception,
    expected: ProviderFailureReason,
) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = SecSubmissionsRefreshService(
            storage,
            configuration=_configuration(),
            issuer_client=_FailedIssuer(cause),
        )

        with pytest.raises(SecSubmissionsRefreshError) as raised:
            service.persist_fresh_snapshot()

    assert raised.value.reason_code == expected
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is cause


def test_fetch_failure_code_is_a_bounded_provider_reason(tmp_path: Path) -> None:
    class FailingIssuer:
        def fetch_submissions(self):
            raise TimeoutError("request timed out")

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        service = SecSubmissionsRefreshService(
            storage,
            configuration=_configuration(),
            issuer_client=FailingIssuer(),
        )
        with pytest.raises(SecSubmissionsRefreshError) as raised:
            service.persist_fresh_snapshot()

    assert raised.value.reason_code == ProviderFailureReason.SEC_SUBMISSIONS_FETCH_FAILED
    assert raised.value.__cause__.__class__ is TimeoutError
