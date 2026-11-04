"""Project one deterministic research dataset into an explainable readiness view."""

from __future__ import annotations

import json

from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService, AssetNotFoundError
from investment_analyst.research.predictive_dataset import (
    PredictiveDatasetBuilder,
    PredictiveDatasetError,
    PredictiveDatasetIntegrityError,
    PredictiveDatasetRequestError,
    UnsupportedPredictiveAssetError,
)
from investment_analyst.research.predictive_models import (
    CLOSE_RETURN_ALGORITHM_VERSION,
    FEATURE_COLUMNS,
    FEATURE_RATIO_UNIT,
    PREREGISTERED_EXPERIMENT,
    FeatureState,
    LabelState,
    PredictiveDataset,
    PredictiveDatasetCounts,
    PredictiveDatasetRequest,
    PredictiveSample,
    ReasonCode,
)
from investment_analyst.research.readiness_models import (
    MAX_READINESS_VIEW_BYTES,
    ReadinessColumnState,
    ReadinessFeatureColumn,
    ReadinessReasonCount,
    ResearchPitReadinessView,
)
from investment_analyst.storage.local import LocalStorage

_FEATURE_STATE_ORDER: tuple[FeatureState, ...] = (
    FeatureState.AVAILABLE,
    FeatureState.MISSING,
    FeatureState.NOT_YET_AVAILABLE,
    FeatureState.INCOMPATIBLE,
    FeatureState.AMBIGUOUS,
)

_LABEL_STATE_ORDER: tuple[LabelState, ...] = (
    LabelState.AVAILABLE,
    LabelState.MISSING,
    LabelState.NOT_YET_AVAILABLE,
    LabelState.INCOMPATIBLE,
    LabelState.AMBIGUOUS,
)


class ReadinessServiceError(RuntimeError):
    """Readiness cannot be projected from the persisted evidence."""


class ReadinessRequestError(ReadinessServiceError, ValueError):
    """The requested range or cut is outside the preregistered contract."""


class ReadinessAssetUnsupportedError(ReadinessServiceError):
    """The requested asset is outside the experiment's supported profile."""


class ReadinessIntegrityError(ReadinessServiceError):
    """Persisted evidence contradicts the projected readiness aggregates."""


class ReadinessService:
    """Read evidence once, project aggregates and release the transient dataset."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        provider_resolver: ProviderAssetContextResolver,
        catalog: AssetCatalogService,
    ) -> None:
        storage.require_open()
        if not storage.read_only:
            raise ReadinessServiceError("readiness requires read-only storage")
        self._storage = storage
        self._provider_resolver = provider_resolver
        self._catalog = catalog

    def query(self, request: PredictiveDatasetRequest) -> ResearchPitReadinessView:
        """Return the readiness view for one explicit range and cut."""
        dataset = self._build(request)
        try:
            view = self._project(dataset)
        finally:
            del dataset
        self._enforce_budget(view)
        return view

    def _build(self, request: PredictiveDatasetRequest) -> PredictiveDataset:
        builder = PredictiveDatasetBuilder(
            self._storage,
            provider_resolver=self._provider_resolver,
            catalog=self._catalog,
        )
        try:
            return builder.build(request)
        except (UnsupportedPredictiveAssetError, AssetNotFoundError) as error:
            raise ReadinessAssetUnsupportedError(str(error)) from error
        except PredictiveDatasetRequestError as error:
            raise ReadinessRequestError(str(error)) from error
        except PredictiveDatasetIntegrityError as error:
            raise ReadinessIntegrityError(str(error)) from error
        except PredictiveDatasetError as error:
            raise ReadinessServiceError(str(error)) from error

    @staticmethod
    def _project(dataset: PredictiveDataset) -> ResearchPitReadinessView:
        samples = dataset.samples
        summary = dataset.summary
        if len(samples) != summary.counts.requested:
            raise ReadinessIntegrityError("dataset samples differ from the summary count")
        ReadinessService._verify_sample_states(samples, summary.counts)
        columns = tuple(
            ReadinessService._column(index, samples=samples, requested=len(samples))
            for index in range(len(FEATURE_COLUMNS))
        )
        return ResearchPitReadinessView(
            workspace_uuid=dataset.manifest.workspace_uuid,
            summary=summary,
            feature_columns=columns,
            limitations=(
                *summary.limitations,
                "Los conteos por columna particionan las decisiones solicitadas; "
                "las incidencias de razones pueden solaparse dentro de una misma decisión.",
                "Una lectura 'research_ready' describe suficiencia de muestra PIT, "
                "no un predictor validado ni una recomendación.",
            ),
        )

    @staticmethod
    def _column(
        index: int,
        *,
        samples: tuple[PredictiveSample, ...],
        requested: int,
    ) -> ReadinessFeatureColumn:
        spec = PREREGISTERED_EXPERIMENT.feature_columns[index]
        expected_version = (
            spec.algorithm_version
            if spec.algorithm_version is not None
            else CLOSE_RETURN_ALGORITHM_VERSION
        )
        counts = {state: 0 for state in _FEATURE_STATE_ORDER}
        occurrences: dict[ReasonCode, int] = {}
        for sample in samples:
            evidence = sample.features[index]
            if evidence.column_name != spec.column_name:
                raise ReadinessIntegrityError("feature columns are not in the declared order")
            if evidence.algorithm_version != expected_version:
                raise ReadinessIntegrityError("feature algorithm version differs from the contract")
            if evidence.unit != FEATURE_RATIO_UNIT:
                raise ReadinessIntegrityError("feature unit differs from the declared contract")
            counts[evidence.state] += 1
            for reason in evidence.reasons:
                occurrences[reason.code] = occurrences.get(reason.code, 0) + 1
        return ReadinessFeatureColumn(
            column_name=spec.column_name,
            algorithm_version=expected_version,
            unit=FEATURE_RATIO_UNIT,
            requested=requested,
            states=tuple(
                ReadinessColumnState(state=state, samples=counts[state])
                for state in _FEATURE_STATE_ORDER
            ),
            reasons=tuple(
                ReadinessReasonCount(code=code, occurrences=occurrences[code])
                for code in sorted(occurrences, key=lambda item: item.value)
            ),
        )

    @staticmethod
    def _verify_sample_states(
        samples: tuple[PredictiveSample, ...],
        counts: PredictiveDatasetCounts,
    ) -> None:
        feature_counts = {state: 0 for state in _FEATURE_STATE_ORDER}
        label_counts = {state: 0 for state in _LABEL_STATE_ORDER}
        for sample in samples:
            feature_counts[sample.feature_state] += 1
            label_counts[sample.label_state] += 1
        expected_feature = (
            counts.feature_available,
            counts.feature_missing,
            counts.feature_not_yet_available,
            counts.feature_incompatible,
            counts.feature_ambiguous,
        )
        expected_label = (
            counts.label_available,
            counts.label_missing,
            counts.label_not_yet_available,
            counts.label_incompatible,
            counts.label_ambiguous,
        )
        if tuple(feature_counts[state] for state in _FEATURE_STATE_ORDER) != expected_feature:
            raise ReadinessIntegrityError("sample feature states differ from the summary")
        if tuple(label_counts[state] for state in _LABEL_STATE_ORDER) != expected_label:
            raise ReadinessIntegrityError("sample label states differ from the summary")

    @staticmethod
    def _enforce_budget(view: ResearchPitReadinessView) -> None:
        encoded = json.dumps(view.to_json_dict(), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_READINESS_VIEW_BYTES:
            raise ReadinessServiceError("readiness projection exceeds the response budget")


__all__ = [
    "ReadinessAssetUnsupportedError",
    "ReadinessIntegrityError",
    "ReadinessRequestError",
    "ReadinessService",
    "ReadinessServiceError",
]
