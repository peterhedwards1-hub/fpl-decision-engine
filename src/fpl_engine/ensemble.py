"""Small non-negative, sum-to-one ensemble for chronological OOF forecasts."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OOFEnsembleRow:
    season_code: str
    gameweek: int
    actual: float
    predictions: dict[str, float]
    trained_through: dict[str, tuple[str, int]]


@dataclass(frozen=True)
class ConstrainedEnsemble:
    model_names: tuple[str, ...]
    weights: tuple[float, ...]
    training_samples: int
    training_rmse: float
    individual_rmse: dict[str, float]
    iterations: int

    def predict(self, predictions: dict[str, float]) -> float:
        missing = set(self.model_names) - set(predictions)
        if missing:
            raise ValueError(f"Ensemble prediction is missing models: {sorted(missing)}")
        return sum(
            weight * float(predictions[name])
            for name, weight in zip(self.model_names, self.weights, strict=True)
        )


def fit_constrained_ensemble(
    rows: tuple[OOFEnsembleRow, ...],
    *,
    maximum_iterations: int = 5_000,
    tolerance: float = 1e-10,
) -> ConstrainedEnsemble:
    """Fit least-squares weights projected onto the probability simplex."""

    if not rows:
        raise ValueError("At least one OOF row is required")
    names = tuple(sorted(rows[0].predictions))
    if len(names) < 2:
        raise ValueError("At least two component models are required")
    for row in rows:
        if tuple(sorted(row.predictions)) != names:
            raise ValueError("Every ensemble row must contain the same models")
        if set(row.trained_through) != set(names):
            raise ValueError("Training cutoffs are required for every model")
        target_period = (row.season_code, row.gameweek)
        leaking = [name for name in names if row.trained_through[name] >= target_period]
        if leaking:
            raise ValueError(
                "Ensemble inputs must be strictly chronological; leakage in " + ", ".join(leaking)
            )
    features = [[float(row.predictions[name]) for name in names] for row in rows]
    actual = [float(row.actual) for row in rows]
    weights = [1.0 / len(names)] * len(names)
    maximum_column_energy = max(
        sum(feature[index] ** 2 for feature in features) for index in range(len(names))
    )
    step = 1.0 / max(1.0, 2.0 * maximum_column_energy)
    completed = 0
    for iteration in range(maximum_iterations):
        errors = [
            sum(weight * value for weight, value in zip(weights, feature, strict=True)) - outcome
            for feature, outcome in zip(features, actual, strict=True)
        ]
        gradient = [
            2.0
            * sum(error * feature[index] for error, feature in zip(errors, features, strict=True))
            for index in range(len(names))
        ]
        candidate = _project_simplex(
            [weight - step * value for weight, value in zip(weights, gradient, strict=True)]
        )
        completed = iteration + 1
        if (
            max(abs(left - right) for left, right in zip(candidate, weights, strict=True))
            <= tolerance
        ):
            weights = candidate
            break
        weights = candidate
    blended = [
        sum(weight * value for weight, value in zip(weights, feature, strict=True))
        for feature in features
    ]
    return ConstrainedEnsemble(
        model_names=names,
        weights=tuple(round(value, 10) for value in weights),
        training_samples=len(rows),
        training_rmse=_rmse(blended, actual),
        individual_rmse={
            name: _rmse(
                [float(row.predictions[name]) for row in rows],
                actual,
            )
            for name in names
        },
        iterations=completed,
    )


def _project_simplex(values: list[float]) -> list[float]:
    """Euclidean projection onto {w >= 0, sum(w) = 1}."""

    ordered = sorted(values, reverse=True)
    cumulative = 0.0
    rho = 0
    for index, value in enumerate(ordered, start=1):
        cumulative += value
        if value - (cumulative - 1.0) / index > 0:
            rho = index
    threshold = (sum(ordered[:rho]) - 1.0) / rho
    projected = [max(value - threshold, 0.0) for value in values]
    total = sum(projected)
    return [value / total for value in projected]


def _rmse(predicted: list[float], actual: list[float]) -> float:
    return round(
        math.sqrt(
            sum(
                (prediction - outcome) ** 2
                for prediction, outcome in zip(predicted, actual, strict=True)
            )
            / len(actual)
        ),
        6,
    )
