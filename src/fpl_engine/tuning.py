"""Reproducible hyperparameter search for walk-forward projection models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .backtest import BacktestReport, ProjectionBacktester
from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import (
    BASELINE_V2_MODEL_CONFIG,
    DEFAULT_MODEL_CONFIG,
    MODEL_VERSION,
    ProjectionModelConfig,
)

TUNED_MODEL_VERSION = "rates-two-stage-v2"
ROLLING_STABILITY_WEIGHT = 0.25
ROLLING_OBJECTIVE_VERSION = "top100-accuracy-cross-season-v2"


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


@dataclass(frozen=True)
class RollingProjectionTuningResult:
    study_name: str
    trials_requested: int
    development_seasons: tuple[str, ...]
    validation_season: str
    best_trial_number: int
    best_score: float
    best_config: ProjectionModelConfig
    development_backtest_run_ids: dict[str, int]
    development_season_scores: dict[str, float]
    development_weighted_mean_score: float
    development_worst_season_score: float
    cross_season_stability_penalty: float
    incumbent_validation_report: BacktestReport
    challenger_validation_report: BacktestReport

    def as_dict(self) -> dict[str, object]:
        return {
            "study_name": self.study_name,
            "trials_requested": self.trials_requested,
            "development_seasons": self.development_seasons,
            "validation_season": self.validation_season,
            "best_trial_number": self.best_trial_number,
            "best_score": self.best_score,
            "best_config": asdict(self.best_config),
            "development_backtest_run_ids": (
                self.development_backtest_run_ids
            ),
            "development_season_scores": self.development_season_scores,
            "development_weighted_mean_score": (
                self.development_weighted_mean_score
            ),
            "development_worst_season_score": (
                self.development_worst_season_score
            ),
            "cross_season_stability_penalty": (
                self.cross_season_stability_penalty
            ),
            "incumbent_validation": (
                self.incumbent_validation_report.as_dict()
            ),
            "challenger_validation": (
                self.challenger_validation_report.as_dict()
            ),
            "validation_change": _validation_change(
                self.incumbent_validation_report,
                self.challenger_validation_report,
            ),
            "objective": {
                "version": ROLLING_OBJECTIVE_VERSION,
                "primary": "sample-weighted development-season score",
                "cross_season_stability_weight": ROLLING_STABILITY_WEIGHT,
                "formula": (
                    "weighted_mean + stability_weight * "
                    "(worst_season - weighted_mean)"
                ),
            },
            "holdout_selection_locked_before_evaluation": True,
            "holdout_locked": True,
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
        config=BASELINE_V2_MODEL_CONFIG,
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


def tune_projection_model_rolling(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    *,
    development_seasons: tuple[str, ...],
    validation_season: str,
    origin_gameweek_start: int = 2,
    origin_gameweek_end: int = 38,
    horizon_gameweeks: int = 1,
    trials: int = 50,
    study_name: str = "fpl-rates-rolling-v3",
    storage_url: str | None = None,
    seed: int = 20260729,
) -> RollingProjectionTuningResult:
    """Tune across seasons, then inspect one holdout exactly once."""

    if not development_seasons:
        raise ValueError("At least one development season is required")
    if validation_season in development_seasons:
        raise ValueError("Validation season cannot also be a development season")
    required_rules = {*development_seasons, validation_season}
    if set(rules_by_season) != required_rules:
        missing = required_rules - set(rules_by_season)
        extra = set(rules_by_season) - required_rules
        raise ValueError(
            f"Rules must match the requested seasons; missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    if not 1 <= origin_gameweek_start <= origin_gameweek_end <= 38:
        raise ValueError("Rolling backtest Gameweeks must be within 1–38")
    if trials <= 0:
        raise ValueError("Tuning trials must be positive")
    try:
        import optuna
    except ImportError as error:
        raise ModelingDependencyError(
            "Projection tuning requires the 'modeling' project dependency"
        ) from error

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=seed),
        study_name=study_name,
        storage=storage_url,
        load_if_exists=True,
    )
    expected_scope = {
        "development_seasons": list(development_seasons),
        "validation_season": validation_season,
        "origin_gameweek_start": origin_gameweek_start,
        "origin_gameweek_end": origin_gameweek_end,
        "horizon_gameweeks": horizon_gameweeks,
        "objective_version": ROLLING_OBJECTIVE_VERSION,
        "cross_season_stability_weight": ROLLING_STABILITY_WEIGHT,
    }
    prior_scope = study.user_attrs.get("evaluation_scope")
    if prior_scope is not None and prior_scope != expected_scope:
        raise ValueError(
            "Existing rolling study has a different season or Gameweek scope"
        )
    if study.user_attrs.get("holdout_locked"):
        raise ValueError(
            "This study's validation holdout has already been inspected; "
            "create a new study with a genuinely unseen holdout"
        )
    study.set_user_attr("evaluation_scope", expected_scope)

    def objective(trial: Any) -> float:
        config = _suggest_rolling_config(trial)
        reports = {}
        for season_code in development_seasons:
            reports[season_code] = ProjectionBacktester(
                database,
                rules_by_season[season_code],
                config=config,
                model_version=(
                    f"rates-rolling-v3-trial-{trial.number}"
                ),
            ).run(
                season_code=season_code,
                origin_gameweek_start=origin_gameweek_start,
                origin_gameweek_end=origin_gameweek_end,
                horizon_gameweeks=horizon_gameweeks,
                evidence_policy="performance_only",
            )
        weights = {
            season_code: next(
                metric.samples
                for metric in report.top_n
                if metric.value == "100"
            )
            for season_code, report in reports.items()
        }
        season_scores = {
            season_code: tuning_objective(report)
            for season_code, report in reports.items()
        }
        weighted_mean_score = sum(
            season_scores[season_code] * weights[season_code]
            for season_code in development_seasons
        ) / sum(weights.values())
        worst_season_score = max(season_scores.values())
        stability_penalty = ROLLING_STABILITY_WEIGHT * (
            worst_season_score - weighted_mean_score
        )
        score = weighted_mean_score + stability_penalty
        trial.set_user_attr(
            "development_backtest_run_ids",
            {
                season_code: report.backtest_run_id
                for season_code, report in reports.items()
            },
        )
        trial.set_user_attr(
            "season_scores",
            {
                season_code: round(season_score, 6)
                for season_code, season_score in season_scores.items()
            },
        )
        trial.set_user_attr(
            "development_weighted_mean_score",
            round(weighted_mean_score, 6),
        )
        trial.set_user_attr(
            "development_worst_season_score",
            round(worst_season_score, 6),
        )
        trial.set_user_attr(
            "cross_season_stability_penalty",
            round(stability_penalty, 6),
        )
        return score

    holdout_evaluation_started = bool(
        study.user_attrs.get("holdout_evaluation_started")
    )
    if not holdout_evaluation_started:
        completed_trials = sum(
            trial.state.name == "COMPLETE" for trial in study.trials
        )
        remaining_trials = max(0, trials - completed_trials)
        if remaining_trials:
            study.optimize(objective, n_trials=remaining_trials)
    if not any(trial.state.name == "COMPLETE" for trial in study.trials):
        raise ValueError("Rolling study has no completed trials")
    if holdout_evaluation_started:
        selected_trial_number = int(
            study.user_attrs["selected_trial_before_holdout"]
        )
        best_trial = next(
            trial
            for trial in study.trials
            if trial.number == selected_trial_number
        )
    else:
        best_trial = study.best_trial
        study.set_user_attr("holdout_evaluation_started", True)
        study.set_user_attr(
            "selected_trial_before_holdout", best_trial.number
        )
    best_config = _config_from_parameters(best_trial.params)
    incumbent = ProjectionBacktester(
        database,
        rules_by_season[validation_season],
        config=DEFAULT_MODEL_CONFIG,
        model_version=f"{MODEL_VERSION}-rolling-validation-incumbent",
    ).run(
        season_code=validation_season,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        evidence_policy="performance_only",
    )
    challenger = ProjectionBacktester(
        database,
        rules_by_season[validation_season],
        config=best_config,
        model_version=(
            f"rates-rolling-v3-selected-{best_trial.number}"
        ),
    ).run(
        season_code=validation_season,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        evidence_policy="performance_only",
    )
    study.set_user_attr("holdout_locked", True)
    study.set_user_attr(
        "validation_backtest_run_ids",
        {
            "incumbent": incumbent.backtest_run_id,
            "challenger": challenger.backtest_run_id,
        },
    )
    return RollingProjectionTuningResult(
        study_name=study.study_name,
        trials_requested=trials,
        development_seasons=development_seasons,
        validation_season=validation_season,
        best_trial_number=best_trial.number,
        best_score=round(float(best_trial.value), 6),
        best_config=best_config,
        development_backtest_run_ids={
            key: int(value)
            for key, value in best_trial.user_attrs[
                "development_backtest_run_ids"
            ].items()
        },
        development_season_scores={
            key: float(value)
            for key, value in best_trial.user_attrs["season_scores"].items()
        },
        development_weighted_mean_score=float(
            best_trial.user_attrs["development_weighted_mean_score"]
        ),
        development_worst_season_score=float(
            best_trial.user_attrs["development_worst_season_score"]
        ),
        cross_season_stability_penalty=float(
            best_trial.user_attrs["cross_season_stability_penalty"]
        ),
        incumbent_validation_report=incumbent,
        challenger_validation_report=challenger,
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
        "minutes_prior_matches": BASELINE_V2_MODEL_CONFIG.minutes_prior_matches,
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


def _suggest_rolling_config(trial: Any) -> ProjectionModelConfig:
    """Expanded range for a new study; do not alter the completed v2 study."""

    parameters = {
        "player_rate_prior_minutes": trial.suggest_float(
            "player_rate_prior_minutes", 600.0, 3600.0, log=True
        ),
        "minutes_prior_matches": BASELINE_V2_MODEL_CONFIG.minutes_prior_matches,
        "team_prior_matches": trial.suggest_float(
            "team_prior_matches", 2.0, 20.0
        ),
        "home_attack_multiplier": trial.suggest_float(
            "home_attack_multiplier", 0.98, 1.16
        ),
        "away_attack_multiplier": trial.suggest_float(
            "away_attack_multiplier", 0.75, 1.0
        ),
        "recent_gameweeks": trial.suggest_int(
            "recent_gameweeks", 2, 8
        ),
        "recent_evidence_weight": trial.suggest_float(
            "recent_evidence_weight", 1.0, 10.0, log=True
        ),
        "appearance_prior_matches": trial.suggest_float(
            "appearance_prior_matches", 0.25, 8.0, log=True
        ),
        "appearance_prior_probability": trial.suggest_float(
            "appearance_prior_probability", 0.15, 0.65
        ),
        "conditional_minutes_prior_appearances": trial.suggest_float(
            "conditional_minutes_prior_appearances", 0.25, 8.0, log=True
        ),
    }
    return _config_from_parameters(parameters)


def _config_from_parameters(
    parameters: dict[str, Any],
) -> ProjectionModelConfig:
    return replace(
        BASELINE_V2_MODEL_CONFIG,
        minutes_model="two_stage",
        enforce_team_minutes=True,
        **parameters,
    )
