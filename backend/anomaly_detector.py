"""
anomaly_detector.py — Isolation Forest wrapper for AML anomaly scoring.

Design decisions
----------------
* Reproducible seed for all random operations.
* Separate training buffer (warm-up) vs live scoring.
* score_samples() returns negative values; we invert and clip to [0, 1].
* Retraining is triggered every N new samples after warm-up.
* train() and evaluate() operate on SEPARATE data — no leakage.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from backend.config import IF_CFG, IFConfig

log = logging.getLogger(__name__)

N_FEATURES = 22  # must match feature_engineering.N_FEATURES


class AMLAnomalyDetector:
    """
    Thread-safe Isolation Forest anomaly detector.

    Usage
    -----
    detector = AMLAnomalyDetector()

    # During warm-up:
    detector.add_sample(feature_vector)   # returns 0.0 until trained

    # After warm-up:
    score = detector.score(feature_vector)  # [0, 1], higher = more anomalous

    # Periodic retraining (called internally):
    detector.maybe_retrain()
    """

    def __init__(self, cfg: IFConfig | None = None) -> None:
        self.cfg     = cfg or IF_CFG
        self._lock   = threading.Lock()
        self._model: Optional[IsolationForest] = None
        self._scaler = RobustScaler()
        self._scaler_fitted = False

        # Rolling buffer for (re)training
        self._buffer: deque[np.ndarray] = deque(maxlen=10_000)
        self._samples_seen = 0
        self._generation   = 0

    # -----------------------------------------------------------------------
    def add_sample(self, vec: np.ndarray) -> float:
        """
        Add a feature vector to the training buffer.
        Returns current anomaly score (0.0 if model not warm).
        """
        with self._lock:
            self._buffer.append(vec.copy())
            self._samples_seen += 1

        self.maybe_retrain()
        return self.score(vec)

    # -----------------------------------------------------------------------
    def score(self, vec: np.ndarray) -> float:
        """
        Return anomaly score in [0, 1].
        0 = definitely normal; 1 = maximally anomalous.
        """
        with self._lock:
            if self._model is None or not self._scaler_fitted:
                return 0.0
            X = vec.reshape(1, -1)
            try:
                X_scaled = self._scaler.transform(X)
                raw = self._model.score_samples(X_scaled)[0]
                # score_samples: more negative = more anomalous
                # Typical range for trained IF: roughly -0.6 to 0
                score = float(np.clip((-raw - 0.1) * 2.0, 0.0, 1.0))
                return round(score, 6)
            except Exception as exc:
                log.warning("IF scoring error: %s", exc)
                return 0.0

    # -----------------------------------------------------------------------
    def score_batch(self, X: np.ndarray) -> np.ndarray:
        """Score an (N, F) matrix; returns (N,) array of float scores."""
        with self._lock:
            if self._model is None or not self._scaler_fitted:
                return np.zeros(len(X))
            try:
                X_scaled = self._scaler.transform(X)
                raw = self._model.score_samples(X_scaled)
                scores = np.clip((-raw - 0.1) * 2.0, 0.0, 1.0)
                return scores
            except Exception as exc:
                log.warning("IF batch scoring error: %s", exc)
                return np.zeros(len(X))

    # -----------------------------------------------------------------------
    def maybe_retrain(self) -> bool:
        """Retrain if we have enough data and interval has elapsed."""
        with self._lock:
            n = len(self._buffer)
            if n < self.cfg.min_train_samples:
                return False
            if (self._samples_seen - self.cfg.min_train_samples) \
                    % self.cfg.retrain_interval != 0:
                return False

        self._retrain()
        return True

    def _retrain(self) -> None:
        """Fit Isolation Forest + scaler on the current buffer."""
        with self._lock:
            X = np.array(self._buffer)

        if len(X) < self.cfg.min_train_samples:
            return

        log.info("Retraining Isolation Forest on %d samples…", len(X))
        try:
            scaler = RobustScaler()
            X_scaled = scaler.fit_transform(X)

            model = IsolationForest(
                n_estimators=self.cfg.n_estimators,
                contamination=self.cfg.contamination,
                random_state=self.cfg.random_state,
                n_jobs=-1,
            )
            model.fit(X_scaled)

            with self._lock:
                self._scaler = scaler
                self._scaler_fitted = True
                self._model = model
                self._generation += 1

            log.info("IF generation %d trained.", self._generation)
        except Exception as exc:
            log.error("Retraining failed: %s", exc)

    # -----------------------------------------------------------------------
    # Batch training for evaluation / scripts
    # -----------------------------------------------------------------------
    def train(self, X: np.ndarray) -> None:
        """Train on a provided matrix (evaluation pipeline)."""
        log.info("Training IF on %d samples…", len(X))
        scaler = RobustScaler()
        X_scaled = scaler.fit_transform(X)
        model = IsolationForest(
            n_estimators=self.cfg.n_estimators,
            contamination=self.cfg.contamination,
            random_state=self.cfg.random_state,
            n_jobs=-1,
        )
        model.fit(X_scaled)
        with self._lock:
            self._scaler = scaler
            self._scaler_fitted = True
            self._model = model
            self._generation += 1
        log.info("IF trained (generation %d).", self._generation)

    @property
    def is_warm(self) -> bool:
        with self._lock:
            return self._model is not None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def samples_seen(self) -> int:
        with self._lock:
            return self._samples_seen


# ---------------------------------------------------------------------------
# Module-level singleton (used by streaming engine)
# ---------------------------------------------------------------------------
_global_detector: Optional[AMLAnomalyDetector] = None


def get_detector() -> AMLAnomalyDetector:
    global _global_detector
    if _global_detector is None:
        _global_detector = AMLAnomalyDetector()
    return _global_detector
