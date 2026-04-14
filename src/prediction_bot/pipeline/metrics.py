from __future__ import annotations

from prediction_bot.storage.prediction_store import BrierMetrics, PredictionStore


def get_metrics(store: PredictionStore) -> BrierMetrics:
    return store.brier_metrics()
