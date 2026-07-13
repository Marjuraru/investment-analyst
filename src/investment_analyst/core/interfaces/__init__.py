"""Public repository interfaces."""

from investment_analyst.core.interfaces.repositories import (
    AssetRepository,
    DiagnosticResultRepository,
    MetricDefinitionRepository,
    MetricResultRepository,
    ObservationRepository,
    RawRecordRepository,
    SourceDefinitionRepository,
)

__all__ = [
    "AssetRepository",
    "DiagnosticResultRepository",
    "MetricDefinitionRepository",
    "MetricResultRepository",
    "ObservationRepository",
    "RawRecordRepository",
    "SourceDefinitionRepository",
]
