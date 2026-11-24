"""Recursive least squares with forgetting, bounds and outlier damping.

One building block for both gain estimators: the night regression of
heat-loss factor and steady gain, and the hourly regression of the gain
profile and the sun terms. Plain Python: at most about 30 parameters per
room, updated once an hour or once a night.

P is the normalised covariance of classic RLS; the parameter covariance is
P times the noise variance, which is estimated from the residuals.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import inf, sqrt

# Residuals are clipped only once the noise estimate rests on a few samples.
MIN_SAMPLES_FOR_CLIPPING = 5


@dataclass(frozen=True)
class RlsUpdateResult:
    residual: float
    clipped: bool


class RecursiveLeastSquares:
    def __init__(  # noqa: PLR0913 -- every tuning knob is keyword-only
        self,
        theta0: list[float],
        p0_diag: list[float],
        forgetting: float,
        *,
        lower_bounds: list[float] | None = None,
        upper_bounds: list[float] | None = None,
        huber_k: float | None = 2.0,
        p_max_diag: list[float] | None = None,
        noise_floor_variance: float = 0.0,
    ) -> None:
        n = len(theta0)
        if len(p0_diag) != n:
            raise ValueError("p0_diag must match theta0")
        if not 0 < forgetting <= 1:
            raise ValueError("forgetting must be in (0, 1]")
        self._n = n
        self._theta0 = list(theta0)
        self._p0_diag = list(p0_diag)
        self.forgetting = forgetting
        self.lower_bounds = list(lower_bounds) if lower_bounds else [-inf] * n
        self.upper_bounds = list(upper_bounds) if upper_bounds else [inf] * n
        self.huber_k = huber_k
        if p_max_diag is not None and len(p_max_diag) != n:
            raise ValueError("p_max_diag must match theta0")
        self.p_max_diag = list(p_max_diag) if p_max_diag is not None else None
        # The first samples fit almost exactly and estimate the noise near
        # zero; without a floor a parameter the data never excited would
        # count as known.
        self.noise_floor_variance = noise_floor_variance
        self.reset()

    def reset(self) -> None:
        self.theta = list(self._theta0)
        self.p = [
            [self._p0_diag[i] if i == j else 0.0 for j in range(self._n)]
            for i in range(self._n)
        ]
        self.noise_variance: float | None = None
        self.samples = 0

    @property
    def size(self) -> int:
        return self._n

    def predict(self, x: list[float]) -> float:
        return sum(t * v for t, v in zip(self.theta, x, strict=True))

    def std(self, index: int) -> float:
        if self.noise_variance is None:
            return inf
        variance = max(self.noise_variance, self.noise_floor_variance)
        return sqrt(max(0.0, self.p[index][index]) * variance)

    def update(self, x: list[float], y: float, weight: float = 1.0) -> RlsUpdateResult:
        if len(x) != self._n:
            raise ValueError("regressor size mismatch")
        if weight <= 0:
            raise ValueError("weight must be positive")
        lam = self.forgetting
        residual = y - self.predict(x)
        px = [sum(self.p[i][j] * x[j] for j in range(self._n)) for i in range(self._n)]
        denominator = lam / weight + sum(x[i] * px[i] for i in range(self._n))
        error = self._clipped_error(residual, denominator * weight / lam)
        gain = [v / denominator for v in px]
        for i in range(self._n):
            self.theta[i] = min(
                self.upper_bounds[i],
                max(self.lower_bounds[i], self.theta[i] + gain[i] * error),
            )
        self._update_covariance(gain, px)
        # A priori times a posteriori residual estimates the noise variance
        # without the parameter error of the first samples.
        posterior_residual = error * (lam / weight) / denominator
        self._update_noise(max(0.0, error * posterior_residual))
        return RlsUpdateResult(residual=residual, clipped=error != residual)

    def _clipped_error(self, residual: float, spread: float) -> float:
        """The residual, clipped to huber_k times its expected spread."""
        if (
            self.huber_k is None
            or self.noise_variance is None
            or self.samples < MIN_SAMPLES_FOR_CLIPPING
        ):
            return residual
        # The expected spread of the residual includes the parameter
        # uncertainty: early on a large residual is the model's error,
        # not an outlier.
        variance = max(self.noise_variance, self.noise_floor_variance)
        limit = self.huber_k * sqrt(variance * spread)
        if abs(residual) <= limit:
            return residual
        return limit if residual > 0 else -limit

    def _update_covariance(self, gain: list[float], px: list[float]) -> None:
        lam = self.forgetting
        self.p = [
            [(self.p[i][j] - gain[i] * px[j]) / lam for j in range(self._n)]
            for i in range(self._n)
        ]
        # Keep P symmetric against rounding.
        for i in range(self._n):
            for j in range(i + 1, self._n):
                mean = (self.p[i][j] + self.p[j][i]) / 2
                self.p[i][j] = mean
                self.p[j][i] = mean
        if self.p_max_diag is not None:
            self._cap_variance(self.p_max_diag)

    def _cap_variance(self, p_max_diag: list[float]) -> None:
        """Cap each parameter's variance by scaling its row and column.

        Forgetting inflates the variance of a parameter the data do not
        excite (a block of days that never come); capped per parameter, the
        others keep learning.
        """
        for i in range(self._n):
            if self.p[i][i] > p_max_diag[i]:
                scale = sqrt(p_max_diag[i] / self.p[i][i])
                for j in range(self._n):
                    self.p[i][j] *= scale
                    self.p[j][i] *= scale

    def _update_noise(self, sample_variance: float) -> None:
        self.samples += 1
        if self.noise_variance is None:
            self.noise_variance = sample_variance
            return
        lam = self.forgetting
        effective = 1 / (1 - lam) if lam < 1 else inf
        count = min(self.samples, effective)
        self.noise_variance += (sample_variance - self.noise_variance) / count

    def to_dict(self) -> dict:
        return {
            "theta": list(self.theta),
            "p": [list(row) for row in self.p],
            "noise_variance": self.noise_variance,
            "samples": self.samples,
        }

    def restore(self, data: dict) -> bool:
        """Restore a saved state; False (and nothing changed) if it does not fit."""
        theta = data.get("theta")
        p = data.get("p")
        if (
            not isinstance(theta, list)
            or not isinstance(p, list)
            or len(theta) != self._n
            or len(p) != self._n
            or any(not isinstance(row, list) or len(row) != self._n for row in p)
        ):
            return False
        self.theta = [float(v) for v in theta]
        self.p = [[float(v) for v in row] for row in p]
        noise = data.get("noise_variance")
        self.noise_variance = float(noise) if noise is not None else None
        self.samples = int(data.get("samples", 0))
        return True
