from prediction_bot.config import CalibrationSettings
from prediction_bot.core.calibration import ProbabilityCalibrator


def test_calibrator_shrinks_extreme_probabilities_when_low_confidence() -> None:
    calibrator = ProbabilityCalibrator(CalibrationSettings(enabled=True, base_shrink=0.3, confidence_weight=0.4, evidence_weight=0.03))
    result = calibrator.calibrate(raw_probability=0.9, confidence=0.1, evidence_count=0, spread=0.08)
    assert result.calibrated_probability < 0.9
    assert result.calibrated_probability > 0.5


def test_calibrator_keeps_probability_close_when_high_confidence() -> None:
    calibrator = ProbabilityCalibrator(CalibrationSettings(enabled=True, base_shrink=0.45, confidence_weight=0.45, evidence_weight=0.05))
    result = calibrator.calibrate(raw_probability=0.7, confidence=1.0, evidence_count=12, spread=0.01)
    assert result.calibrated_probability >= 0.65
