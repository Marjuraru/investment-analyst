"""Public API for the deterministic simulated pipeline."""

from investment_analyst.simulation.pipeline import (
    SimulatedPipeline,
    SimulationCounts,
    SimulationRunSummary,
)
from investment_analyst.simulation.scoring import (
    RETURN_WEIGHT,
    VOLUME_WEIGHT,
    clamp,
    final_score,
    score_return,
    score_volume,
    verdict_for_score,
)

__all__ = [
    "RETURN_WEIGHT",
    "VOLUME_WEIGHT",
    "SimulatedPipeline",
    "SimulationCounts",
    "SimulationRunSummary",
    "clamp",
    "final_score",
    "score_return",
    "score_volume",
    "verdict_for_score",
]
