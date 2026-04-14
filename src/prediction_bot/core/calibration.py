from __future__ import annotations

from dataclasses import dataclass

from prediction_bot.config import CalibrationSettings


@dataclass(frozen=True)
class CalibrationResult:
    raw_probability: float
    calibrated_probability: float
    shrink_factor: float


def _clamp_probability(value: float) -> float:
    return max(0.01, min(0.99, value))


class ProbabilityCalibrator:
    """Simple confidence-aware shrinkage calibrator around 0.5.

    This is a robust interim approach before full out-of-sample model calibration.
    """

    def __init__(self, settings: CalibrationSettings) -> None:
        self.settings = settings

    def calibrate(
        self,
        raw_probability: float,
        confidence: float,
        evidence_count: int,
        spread: float | None,
    ) -> CalibrationResult:
        raw = _clamp_probability(raw_probability)

        if not self.settings.enabled:
            return CalibrationResult(raw_probability=raw, calibrated_probability=raw, shrink_factor=1.0)

        conf = max(0.0, min(1.0, confidence))
        evidence_norm = min(max(evidence_count, 0), self.settings.max_evidence_for_weight) / max(
            self.settings.max_evidence_for_weight, 1
        )

        shrink = (
            self.settings.base_shrink
            + (self.settings.confidence_weight * conf)
            + (self.settings.evidence_weight * evidence_norm)
        )

        if spread is not None and spread > 0.0:
            spread_penalty = min(0.35, spread * self.settings.spread_penalty_weight)
            shrink -= spread_penalty

        shrink = max(0.08, min(1.0, shrink))

        calibrated = 0.5 + ((raw - 0.5) * shrink)
        calibrated = _clamp_probability(calibrated)

        return CalibrationResult(
            raw_probability=round(raw, 6),
            calibrated_probability=round(calibrated, 6),
            shrink_factor=round(shrink, 6),
        )


def brier_score(predictions: list[float], outcomes: list[float]) -> float | None:
    if not predictions or len(predictions) != len(outcomes):
        return None
    total = 0.0
    for p, o in zip(predictions, outcomes):
        total += (p - o) ** 2
    return total / len(predictions)
