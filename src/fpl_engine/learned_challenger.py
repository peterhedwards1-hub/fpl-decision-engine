"""Leakage-checked gradient-boosting challenger for rate-model projections."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .history.database import HistoricalDatabase

FEATURE_NAMES = (
    "base_expected_points",
    "base_expected_minutes",
    "uncertainty",
    "fixture_count",
    "horizon_step",
    "origin_gameweek_fraction",
    "position_gk",
    "position_def",
    "position_mid",
    "position_fwd",
)


class LearnedModelDependencyError(RuntimeError):
    """Raised when the optional learned-model toolchain is unavailable."""


@dataclass(frozen=True)
class ChallengerMetrics:
    samples: int
    points_mae: float
    points_bias: float
    top_100_samples: int
    top_100_points_mae: float
    top_100_points_bias: float
    top_100_actual_points_mean: float
    captain_regret: float
    unconstrained_top_15_regret: float


@dataclass(frozen=True)
class LearnedChallengerReport:
    artifact_path: str
    metadata_path: str
    training_run_ids: tuple[int, ...]
    training_seasons: tuple[str, ...]
    validation_run_id: int
    validation_season: str
    loss: str
    baseline: ChallengerMetrics
    challenger: ChallengerMetrics

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_path": self.artifact_path,
            "metadata_path": self.metadata_path,
            "training_run_ids": self.training_run_ids,
            "training_seasons": self.training_seasons,
            "validation_run_id": self.validation_run_id,
            "validation_season": self.validation_season,
            "loss": self.loss,
            "baseline": asdict(self.baseline),
            "challenger": asdict(self.challenger),
            "change": {
                "points_mae": round(
                    self.challenger.points_mae - self.baseline.points_mae,
                    4,
                ),
                "absolute_points_bias": round(
                    abs(self.challenger.points_bias)
                    - abs(self.baseline.points_bias),
                    4,
                ),
                "top_100_points_mae": round(
                    self.challenger.top_100_points_mae
                    - self.baseline.top_100_points_mae,
                    4,
                ),
                "top_100_absolute_bias": round(
                    abs(self.challenger.top_100_points_bias)
                    - abs(self.baseline.top_100_points_bias),
                    4,
                ),
            },
        }


def train_and_evaluate_learned_challenger(
    database: HistoricalDatabase,
    *,
    training_run_ids: tuple[int, ...],
    validation_run_id: int,
    artifact_path: str | Path,
    seed: int = 20260729,
    loss: str = "absolute_error",
) -> LearnedChallengerReport:
    """Fit on earlier seasons and evaluate once on a later persisted run."""

    if not training_run_ids:
        raise ValueError("At least one training backtest run is required")
    if validation_run_id in training_run_ids:
        raise ValueError("Validation run cannot also be a training run")
    if loss not in {"absolute_error", "squared_error", "poisson"}:
        raise ValueError(
            "Learned challenger loss must be absolute_error, "
            "squared_error or poisson"
        )
    run_scope = _run_scope(
        database,
        (*training_run_ids, validation_run_id),
    )
    training_seasons = tuple(
        run_scope[run_id]["season_code"] for run_id in training_run_ids
    )
    validation_season = run_scope[validation_run_id]["season_code"]
    if any(season >= validation_season for season in training_seasons):
        raise ValueError(
            "Every training season must chronologically precede validation"
        )
    validation_config = run_scope[validation_run_id]["model_config_json"]
    if any(
        run_scope[run_id]["model_config_json"] != validation_config
        for run_id in training_run_ids
    ):
        raise ValueError(
            "Training and validation runs must use the same base configuration"
        )
    training_rows = _prediction_rows(database, training_run_ids)
    validation_rows = _prediction_rows(database, (validation_run_id,))
    if not training_rows or not validation_rows:
        raise ValueError("Training and validation runs need prediction rows")
    try:
        import joblib
        from sklearn.ensemble import HistGradientBoostingRegressor
    except ImportError as error:
        raise LearnedModelDependencyError(
            "Learned challengers require the 'modeling' project dependency"
        ) from error

    training_actual = [
        float(row["actual_points"]) for row in training_rows
    ]
    target_offset = (
        max(0.0, -min(training_actual)) if loss == "poisson" else 0.0
    )
    training_target = [
        outcome + target_offset for outcome in training_actual
    ]
    estimator = HistGradientBoostingRegressor(
        loss=loss,
        learning_rate=0.05,
        max_iter=300,
        max_leaf_nodes=31,
        min_samples_leaf=50,
        l2_regularization=1.0,
        random_state=seed,
    )
    estimator.fit(
        [_features(row) for row in training_rows],
        training_target,
    )
    raw_predictions = [
        float(value) - target_offset
        for value in estimator.predict(
            [_features(row) for row in validation_rows]
        )
    ]
    calibrated = (
        [max(0.0, value) for value in raw_predictions]
        if loss == "absolute_error"
        else raw_predictions
    )
    baseline_predictions = [
        float(row["expected_points"]) for row in validation_rows
    ]
    actual = [float(row["actual_points"]) for row in validation_rows]
    baseline_metrics = _metrics(
        validation_rows,
        baseline_predictions,
        actual,
    )
    challenger_metrics = _metrics(
        validation_rows,
        calibrated,
        actual,
    )

    artifact = Path(artifact_path)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = artifact.with_suffix(".json")
    metadata = {
        "model_type": "HistGradientBoostingRegressor",
        "created_at": datetime.now(UTC).isoformat(),
        "feature_names": FEATURE_NAMES,
        "training_run_ids": training_run_ids,
        "training_seasons": training_seasons,
        "validation_run_id": validation_run_id,
        "validation_season": validation_season,
        "base_model_config": json.loads(validation_config),
        "seed": seed,
        "loss": loss,
        "target_offset": target_offset,
        "estimand": (
            "conditional_median"
            if loss == "absolute_error"
            else "conditional_mean"
        ),
        "selection_warning": (
            "Do not retrain or tune this artifact after inspecting its "
            "validation report; use a new holdout."
        ),
    }
    joblib.dump(
        {
            "estimator": estimator,
            "feature_names": FEATURE_NAMES,
            "metadata": metadata,
        },
        artifact,
    )
    metadata_path.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )
    return LearnedChallengerReport(
        artifact_path=str(artifact),
        metadata_path=str(metadata_path),
        training_run_ids=training_run_ids,
        training_seasons=training_seasons,
        validation_run_id=validation_run_id,
        validation_season=validation_season,
        loss=loss,
        baseline=baseline_metrics,
        challenger=challenger_metrics,
    )


def _run_scope(
    database: HistoricalDatabase,
    run_ids: tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in run_ids)
    rows = database.connection.execute(
        f"""
        SELECT runs.id, seasons.code AS season_code, runs.status,
               runs.model_config_json
        FROM projection_backtest_runs runs
        JOIN seasons ON seasons.id = runs.season_id
        WHERE runs.id IN ({placeholders})
        """,
        run_ids,
    ).fetchall()
    result = {int(row["id"]): dict(row) for row in rows}
    missing = set(run_ids) - set(result)
    if missing:
        raise ValueError(f"Unknown backtest run IDs: {sorted(missing)}")
    incomplete = [
        run_id for run_id, row in result.items() if row["status"] != "completed"
    ]
    if incomplete:
        raise ValueError(
            f"Backtest runs must be completed: {sorted(incomplete)}"
        )
    return result


def _prediction_rows(
    database: HistoricalDatabase,
    run_ids: tuple[int, ...],
) -> list:
    placeholders = ",".join("?" for _ in run_ids)
    return database.connection.execute(
        f"""
        SELECT predictions.origin_gameweek, predictions.horizon_step,
               predictions.fixture_count, predictions.expected_minutes,
               predictions.expected_points, predictions.uncertainty,
               predictions.actual_points, player_seasons.position
        FROM projection_backtest_predictions predictions
        JOIN player_seasons
          ON player_seasons.id = predictions.player_season_id
        WHERE predictions.backtest_run_id IN ({placeholders})
        ORDER BY predictions.backtest_run_id,
                 predictions.origin_gameweek,
                 predictions.player_season_id
        """,
        run_ids,
    ).fetchall()


def _features(row: Any) -> list[float]:
    position = row["position"]
    return [
        float(row["expected_points"]),
        float(row["expected_minutes"]),
        float(row["uncertainty"]),
        float(row["fixture_count"]),
        float(row["horizon_step"]),
        float(row["origin_gameweek"]) / 38.0,
        float(position == "GK"),
        float(position == "DEF"),
        float(position == "MID"),
        float(position == "FWD"),
    ]


def _metrics(
    rows: list,
    predictions: list[float],
    actual: list[float],
) -> ChallengerMetrics:
    errors = [
        outcome - prediction
        for prediction, outcome in zip(predictions, actual, strict=True)
    ]
    top_indices: list[int] = []
    by_origin: dict[tuple[int, int], list[int]] = {}
    for index, row in enumerate(rows):
        by_origin.setdefault(
            (int(row["origin_gameweek"]), int(row["horizon_step"])),
            [],
        ).append(index)
    captain_regrets = []
    top_15_regrets = []
    for indices in by_origin.values():
        predicted_order = sorted(
            indices,
            key=lambda index: predictions[index],
            reverse=True,
        )
        actual_order = sorted(
            indices,
            key=lambda index: actual[index],
            reverse=True,
        )
        top_indices.extend(predicted_order[:100])
        captain_regrets.append(
            actual[actual_order[0]] - actual[predicted_order[0]]
        )
        top_15_regrets.append(
            sum(actual[index] for index in actual_order[:15])
            - sum(actual[index] for index in predicted_order[:15])
        )
    top_errors = [errors[index] for index in top_indices]
    return ChallengerMetrics(
        samples=len(errors),
        points_mae=round(
            sum(abs(error) for error in errors) / len(errors),
            4,
        ),
        points_bias=round(sum(errors) / len(errors), 4),
        top_100_samples=len(top_errors),
        top_100_points_mae=round(
            sum(abs(error) for error in top_errors) / len(top_errors),
            4,
        ),
        top_100_points_bias=round(
            sum(top_errors) / len(top_errors),
            4,
        ),
        top_100_actual_points_mean=round(
            sum(actual[index] for index in top_indices) / len(top_indices),
            4,
        ),
        captain_regret=round(
            sum(captain_regrets) / len(captain_regrets),
            4,
        ),
        unconstrained_top_15_regret=round(
            sum(top_15_regrets) / len(top_15_regrets),
            4,
        ),
    )
