"""Reproducible hyperparameter search for walk-forward projection models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .backtest import BacktestReport, ProjectionBacktester
from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import DEFAULT_MODEL_CONFIG, ProjectionModelConfig

TUNED_MODEL_VERSION = "rates-two-stage-v2"


class ModelingDependencyError(RuntimeError):
    """Raised when the optional modelling toolchain is unavailable."""


@dataclass(frozen=True)
class ProjectionTuningResult:
    study_name: str
    trials_requested: int
    best_trial_number: int
    best_score: float
    best_config: ProjectionModelConfig
    development_backtest_run_id: int
    baseline_validation_report: BacktestReport
    validation_report: BacktestReport

    def as_dict(self) -> dict[str, object]:
        return {
            "study_name": self.study_name,
            "trials_requested": self.trials_requested,
            "best_trial_number": self.best_trial_number,
            "best_score": self.best_score,
            "best_config": asdict(self.best_config),
            "development_backtest_run_id": (
                self.development_backtest_run_id
            ),
            "baseline_validation": (
                self.baseline_validation_report.as_dict()
            ),
            "validation": self.validation_report.as_dict(),
            "validation_change": _validation_change(
                self.baseline_validation_report,
                self.validation_report,
            ),
            "objective": {
                "primary": "top-100 points MAE",
                "penalties": (
                    "top-100 absolute bias, overall points MAE, "
                    "overall minutes MAE and team-minute inconsistency"
                ),
            },
        }


def tune_projection_model(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    development_start: int = 2,
    development_end: int = 25,
    validation_start: int = 26,
    validation_end: int = 38,
    horizon_gameweeks: int = 1,
    trials: int = 30,
    study_name: str = "fpl-rates-two-stage-v2",
    storage_url: str | None = None,
    seed: int = 20260729,
) -> ProjectionTuningResult:
    """Tune on the development window and evaluate the winner once on validation."""

    if not 1 <= development_start <= development_end < validation_start:
        raise ValueError(
            "Development Gameweeks must precede the validation window"
        )
    if not validation_start <= validation_end <= 38:
        raise ValueError("Validation Gameweeks must be within 1–38")
    if trials <= 0:
        raise ValueError("Tuning trials must be positive")
    try:
        import optuna
    except ImportError as error:
        raise ModelingDependencyError(
            "Projection tuning requires the 'modeling' project dependency"
        ) from error

    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(
        direction="minimize",
        sampler=sampler,
        study_name=study_name,
        storage=storage_url,
        load_if_exists=True,
    )

    def objective(trial: Any) -> float:
        config = _suggest_config(trial)
        report = ProjectionBacktester(
            database,
            rules,
            config=config,
            model_version=f"{TUNED_MODEL_VERSION}-trial-{trial.number}",
        ).run(
            season_code=season_code,
            origin_gameweek_start=development_start,
            origin_gameweek_end=development_end,
            horizon_gameweeks=horizon_gameweeks,
            evidence_policy="performance_only",
        )
        score = tuning_objective(report)
        trial.set_user_attr(
            "development_backtest_run_id", report.backtest_run_id
        )
        trial.set_user_attr("points_mae", report.overall.points_mae)
        trial.set_user_attr("points_bias", report.overall.points_bias)
        trial.set_user_attr(
            "expected_minutes_per_match",
            report.expected_minutes_per_match,
        )
        return score

    study.optimize(objective, n_trials=trials)
    best_trial = study.best_trial
    best_config = _config_from_parameters(best_trial.params)
    baseline_validation_report = ProjectionBacktester(
        database,
        rules,
        config=DEFAULT_MODEL_CONFIG,
        model_version=f"{TUNED_MODEL_VERSION}-validation-baseline",
    ).run(
        season_code=season_code,
        origin_gameweek_start=validation_start,
        origin_gameweek_end=validation_end,
        horizon_gameweeks=horizon_gameweeks,
        evidence_policy="performance_only",
    )
    validation_report = ProjectionBacktester(
        database,
        rules,
        config=best_config,
        model_version=(
            f"{TUNED_MODEL_VERSION}-selected-{best_trial.number}"
        ),
    ).run(
        season_code=season_code,
        origin_gameweek_start=validation_start,
        origin_gameweek_end=validation_end,
        horizon_gameweeks=horizon_gameweeks,
        evidence_policy="performance_only",
    )
    development_run_id = int(
        best_trial.user_attrs["development_backtest_run_id"]
    )
    return ProjectionTuningResult(
        study_name=study.study_name,
        trials_requested=trials,
        best_trial_number=best_trial.number,
        best_score=round(float(best_trial.value), 6),
        best_config=best_config,
        development_backtest_run_id=development_run_id,
        baseline_validation_report=baseline_validation_report,
        validation_report=validation_report,
    )


def tuning_objective(report: BacktestReport) -> float:
    """Decision-focused scalar used to order otherwise multi-metric trials."""

    top_100 = next(
        metric for metric in report.top_n if metric.value == "100"
    )
    physical_error = abs(
        report.expected_minutes_per_match
        - report.regulation_minutes_per_match
    ) / report.regulation_minutes_per_match
    return (
        top_100.points_mae
        + 0.35 * abs(top_100.points_bias)
        + 0.15 * report.overall.points_mae
        + 0.005 * report.overall.minutes_mae
        + 2.0 * physical_error
    )


def _validation_change(
    baseline: BacktestReport,
    selected: BacktestReport,
) -> dict[str, float]:
    baseline_top_100 = next(
        metric for metric in baseline.top_n if metric.value == "100"
    )
    selected_top_100 = next(
        metric for metric in selected.top_n if metric.value == "100"
    )
    return {
        "overall_points_mae": round(
            selected.overall.points_mae - baseline.overall.points_mae,
            4,
        ),
        "absolute_points_bias": round(
            abs(selected.overall.points_bias)
            - abs(baseline.overall.points_bias),
            4,
        ),
        "minutes_mae": round(
            selected.overall.minutes_mae - baseline.overall.minutes_mae,
            4,
        ),
        "top_100_points_mae": round(
            selected_top_100.points_mae - baseline_top_100.points_mae,
            4,
        ),
        "top_100_absolute_bias": round(
            abs(selected_top_100.points_bias)
            - abs(baseline_top_100.points_bias),
            4,
        ),
    }


def _suggest_config(trial: Any) -> ProjectionModelConfig:
    parameters = {
        "player_rate_prior_minutes": trial.suggest_float(
            "player_rate_prior_minutes", 300.0, 1800.0, log=True
        ),
        "minutes_prior_matches": DEFAULT_MODEL_CONFIG.minutes_prior_matches,
        "team_prior_matches": trial.suggest_float(
            "team_prior_matches", 2.0, 12.0
        ),
        "home_attack_multiplier": trial.suggest_float(
            "home_attack_multiplier", 1.02, 1.15
        ),
        "away_attack_multiplier": trial.suggest_float(
            "away_attack_multiplier", 0.85, 0.98
        ),
        "recent_gameweeks": trial.suggest_int(
            "recent_gameweeks", 2, 6
        ),
        "recent_evidence_weight": trial.suggest_float(
            "recent_evidence_weight", 1.5, 10.0, log=True
        ),
        "appearance_prior_matches": trial.suggest_float(
            "appearance_prior_matches", 0.25, 4.0, log=True
        ),
        "appearance_prior_probability": trial.suggest_float(
            "appearance_prior_probability", 0.15, 0.55
        ),
        "conditional_minutes_prior_appearances": trial.suggest_float(
            "conditional_minutes_prior_appearances", 0.5, 6.0, log=True
        ),
    }
    return _config_from_parameters(parameters)


def _config_from_parameters(
    parameters: dict[str, Any],
) -> ProjectionModelConfig:
    return replace(
        DEFAULT_MODEL_CONFIG,
        minutes_model="two_stage",
        enforce_team_minutes=True,
        **parameters,
    )
