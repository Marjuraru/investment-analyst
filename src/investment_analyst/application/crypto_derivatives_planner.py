"""Receipt-backed edge-only refresh planning for Deribit historical datasets."""

from datetime import datetime

from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesDatasetPlan,
    CryptoDerivativesInterval,
    CryptoDerivativesPlanMode,
    CryptoDerivativesRefreshMode,
    CryptoDerivativesRefreshPlan,
)
from investment_analyst.providers.asset_config import DeribitAssetConfiguration
from investment_analyst.providers.crypto.deribit_pipeline import (
    DeribitDataset,
    DeribitFetchReceipt,
    list_deribit_receipts,
)
from investment_analyst.storage import LocalStorage


class CryptoDerivativesPlanner:
    """Detect only missing prefix/suffix edges; never infer calendar gaps."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        configuration: DeribitAssetConfiguration,
    ) -> None:
        self._storage = storage
        self._configuration = configuration

    def plan(
        self,
        start: datetime,
        end: datetime,
        *,
        refresh_mode: CryptoDerivativesRefreshMode,
    ) -> CryptoDerivativesRefreshPlan:
        """Build independent funding and DVOL edge plans for one requested range."""
        requested = CryptoDerivativesInterval(start=start, end=end)
        if refresh_mode is CryptoDerivativesRefreshMode.FULL:
            full = CryptoDerivativesDatasetPlan(
                mode=CryptoDerivativesPlanMode.FULL,
                intervals=(requested,),
            )
            return CryptoDerivativesRefreshPlan(
                requested_start=start,
                requested_end=end,
                funding=full,
                dvol=full,
            )
        funding_receipts = list_deribit_receipts(
            self._storage,
            source_id=self._configuration.funding_source_id,
            dataset="funding_history",
        )
        dvol_receipts = list_deribit_receipts(
            self._storage,
            source_id=self._configuration.dvol_source_id,
            dataset="dvol_daily",
        )
        return CryptoDerivativesRefreshPlan(
            requested_start=start,
            requested_end=end,
            funding=_edge_plan(start, end, funding_receipts, dataset="funding_history"),
            dvol=_edge_plan(start, end, dvol_receipts, dataset="dvol_daily"),
        )


def _edge_plan(
    start: datetime,
    end: datetime,
    receipts: tuple[DeribitFetchReceipt, ...],
    *,
    dataset: DeribitDataset,
) -> CryptoDerivativesDatasetPlan:
    relevant = tuple(receipt for receipt in receipts if receipt.dataset == dataset)
    requested = CryptoDerivativesInterval(start=start, end=end)
    if not relevant:
        return CryptoDerivativesDatasetPlan(
            mode=CryptoDerivativesPlanMode.INITIAL,
            intervals=(requested,),
        )
    earliest = min(receipt.requested_start for receipt in relevant)
    latest = max(receipt.requested_end for receipt in relevant)
    intervals: list[CryptoDerivativesInterval] = []
    if start < earliest:
        intervals.append(CryptoDerivativesInterval(start=start, end=min(end, earliest)))
    if end > latest:
        intervals.append(CryptoDerivativesInterval(start=max(start, latest), end=end))
    if not intervals:
        return CryptoDerivativesDatasetPlan(
            mode=CryptoDerivativesPlanMode.ALREADY_CURRENT,
            intervals=(),
        )
    if len(intervals) == 1 and intervals[0].start < earliest:
        mode = CryptoDerivativesPlanMode.BACKFILL
    else:
        mode = CryptoDerivativesPlanMode.INCREMENTAL
    return CryptoDerivativesDatasetPlan(mode=mode, intervals=tuple(intervals))


__all__ = ["CryptoDerivativesPlanner"]
