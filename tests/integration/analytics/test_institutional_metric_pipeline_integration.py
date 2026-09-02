from datetime import UTC, datetime

import pytest

from investment_analyst.analytics.cazatiburones.institutional_metric_pipeline import (
    InstitutionalMetricPipeline,
)
from investment_analyst.storage import StorageError


class _Storage:
    read_only = True


def test_pipeline_requires_writable_storage() -> None:
    with pytest.raises(StorageError, match="writable"):
        InstitutionalMetricPipeline(_Storage()).compute(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            known_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
