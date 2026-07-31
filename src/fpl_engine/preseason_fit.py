"""Fit the Stage 3a preseason priors on development folds only.

`preseason-priors-v1` declared its carry-forward and cold-start parameters
rather than estimating them. This module estimates them from the seasons the
project may still use for design, and reports how well the estimate transfers
between them. It never promotes anything: the output is a configuration to
declare and put through the forward gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .backtest import ProjectionBacktester
from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import PRESEASON_V5_MODEL_CONFIG, ProjectionModelConfig
from .tuning import ModelingDependencyError, tuning_objective

FIT_MODEL_VERSION = "rates-preseason-priors-fit"
FIT_OBJECTIVE_VERSION = "preseason-early-window-v1"

#: The Stage 3a parameters this module estimates. Every other field is held at
#: the base configuration so the fit cannot quietly re-tune the incumbent.
PRESEASON_PARAMETER_NAMES = (
    "carry_forward_regression_matches",
    "promoted_team_attack_multiplier",
    "promoted_team_defence_multiplier",
    "cold_start_price_elasticity",
    "cold_start_minimum_factor",
    "cold_start_maximum_factor",
)

#: Seasons whose outcomes have already been queried by earlier evaluation work.
#: They are design-exhausted, so fitting on them would recycle inspected data.
EXHAUSTED_SEASONS = frozenset({"2024-25", "2025-26"})


@dataclass(frozen=True)
class ParameterEvidence:
    """What the search actually established about one parameter.

    A single winning trial reports a value for every parameter whether or not
    the data constrained it. Comparing the leading trials' spread to the whole
    search's spread separates the two cases.
    """

    name: str
    declared: float
    best_trial: float
    leading_mean: float
    leading_deviation: float
    search_deviation: float
    identified: bool
    retained: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeaveOneSeasonOutFold:
    """One season withheld while the parameters are chosen on the others."""

    held_out_season: str
    selected_parameters: dict[str, float]
    held_out_score: float
    declared_held_out_score: float
    score_change: float


@dataclass(frozen=True)
class PreseasonPriorFitResult:
    study_name: str
    target_seasons: tuple[str, ...]
    origin_gameweek_start: int
    origin_gameweek_end: int
    horizon_gameweeks: int
    trials_requested: int
    trials_completed: int
    best_trial_number: int
    best_score: float
    best_trial_config: ProjectionModelConfig
    robust_config: ProjectionModelConfig
    parameter_evidence: tuple[ParameterEvidence, ...]
    declared_parameters: dict[str, float]
    robust_season_scores: dict[str, float]
    declared_season_scores: dict[str, float]
    best_trial_season_scores: dict[str, float]
    declared_backtest_run_ids: dict[str, int]
    robust_backtest_run_ids: dict[str, int]
    leave_one_season_out: tuple[LeaveOneSeasonOutFold, ...]
    limitations: tuple[str, ...]

    @property
    def best_config(self) -> ProjectionModelConfig:
        """The configuration worth declaring: robust, not the raw winner."""

        return self.robust_config

    @property
    def transfers_across_seasons(self) -> bool:
        """True when every withheld season preferred the fitted parameters."""

        return bool(self.leave_one_season_out) and all(
            fold.score_change < 0 for fold in self.leave_one_season_out
        )

    @property
    def identified_parameters(self) -> tuple[str, ...]:
        return tuple(
            evidence.name
            for evidence in self.parameter_evidence
            if evidence.identified
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "study_name": self.study_name,
            "objective_version": FIT_OBJECTIVE_VERSION,
            "target_seasons": list(self.target_seasons),
            "origin_gameweek_start": self.origin_gameweek_start,
            "origin_gameweek_end": self.origin_gameweek_end,
            "horizon_gameweeks": self.horizon_gameweeks,
            "trials_requested": self.trials_requested,
            "trials_completed": self.trials_completed,
            "best_trial_number": self.best_trial_number,
            "best_score": self.best_score,
            "best_trial_config": asdict(self.best_trial_config),
            "robust_config": asdict(self.robust_config),
            "parameter_evidence": [
                evidence.as_dict() for evidence in self.parameter_evidence
            ],
            "identified_parameters": list(self.identified_parameters),
            "declared_parameters": self.declared_parameters,
            "robust_season_scores": self.robust_season_scores,
            "best_trial_season_scores": self.best_trial_season_scores,
            "declared_season_scores": self.declared_season_scores,
            "declared_backtest_run_ids": self.declared_backtest_run_ids,
            "robust_backtest_run_ids": self.robust_backtest_run_ids,
            "leave_one_season_out": [
                asdict(fold) for fold in self.leave_one_season_out
            ],
            "transfers_across_seasons": self.transfers_across_seasons,
            "limitations": list(self.limitations),
            "promotion_status": (
                "Design evidence only. The robust configuration must be "
                "registered as a forward candidate and pass both gate tiers "
                "on 2026/27 before it may replace anything."
            ),
        }


def fit_preseason_priors(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    *,
    target_seasons: tuple[str, ...],
    origin_gameweek_start: int = 1,
    origin_gameweek_end: int = 8,
    horizon_gameweeks: int = 1,
    trials: int = 40,
    study_name: str = "fpl-preseason-priors-fit-v1",
    storage_url: str | None = None,
    seed: int = 20260731,
    base_config: ProjectionModelConfig = PRESEASON_V5_MODEL_CONFIG,
) -> PreseasonPriorFitResult:
    """Estimate the Stage 3a priors over each season's opening Gameweeks."""

    if not target_seasons:
        raise ValueError("At least one target season is required")
    if len(set(target_seasons)) != len(target_seasons):
        raise ValueError("Target seasons must be unique")
    exhausted = sorted(set(target_seasons) & EXHAUSTED_SEASONS)
    if exhausted:
        raise ValueError(
            "Preseason priors cannot be fitted on design-exhausted seasons: "
            + ", ".join(exhausted)
        )
    forward = sorted(season for season in target_seasons if season > "2025-26")
    if forward:
        raise ValueError(
            "Forward seasons are reserved for qualification, not fitting: "
            + ", ".join(forward)
        )
    if set(rules_by_season) != set(target_seasons):
        missing = set(target_seasons) - set(rules_by_season)
        extra = set(rules_by_season) - set(target_seasons)
        raise ValueError(
            f"Rules must match the target seasons; missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    if not 1 <= origin_gameweek_start <= origin_gameweek_end <= 38:
        raise ValueError("Fitting origins must be within 1–38")
    if horizon_gameweeks <= 0:
        raise ValueError("Fitting horizon must be positive")
    if trials <= 0:
        raise ValueError("Fitting trials must be positive")
    if not base_config.team_strength_carry_forward:
        raise ValueError(
            "Fitting carry-forward parameters requires a base configuration "
            "with team_strength_carry_forward enabled"
        )
    if base_config.cold_start_prior != "position_price":
        raise ValueError(
            "Fitting cold-start parameters requires a base configuration with "
            "cold_start_prior set to 'position_price'"
        )
    _require_prior_seasons(database, target_seasons)

    try:
        import optuna
    except ImportError as error:
        raise ModelingDependencyError(
            "Preseason prior fitting requires the 'modeling' project dependency"
        ) from error

    ordered_seasons = tuple(sorted(target_seasons))
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=seed),
        study_name=study_name,
        storage=storage_url,
        load_if_exists=True,
    )
    scope = {
        "target_seasons": list(ordered_seasons),
        "origin_gameweek_start": origin_gameweek_start,
        "origin_gameweek_end": origin_gameweek_end,
        "horizon_gameweeks": horizon_gameweeks,
        "objective_version": FIT_OBJECTIVE_VERSION,
        "base_config": asdict(base_config),
    }
    prior_scope = study.user_attrs.get("fit_scope")
    if prior_scope is not None and prior_scope != scope:
        raise ValueError(
            "Existing study was fitted over a different scope or base "
            "configuration; use a new study name"
        )
    study.set_user_attr("fit_scope", scope)

    def objective(trial: Any) -> float:
        config = _suggest_preseason_config(trial, base_config)
        scores = {}
        for season_code in ordered_seasons:
            report = ProjectionBacktester(
                database,
                rules_by_season[season_code],
                config=config,
                model_version=f"{FIT_MODEL_VERSION}-trial-{trial.number}",
            ).run(
                season_code=season_code,
                origin_gameweek_start=origin_gameweek_start,
                origin_gameweek_end=origin_gameweek_end,
                horizon_gameweeks=horizon_gameweeks,
                evidence_policy="performance_only",
            )
            scores[season_code] = tuning_objective(report)
        trial.set_user_attr("season_scores", scores)
        return sum(scores.values()) / len(scores)

    completed = sum(trial.state.name == "COMPLETE" for trial in study.trials)
    if completed < trials:
        study.optimize(objective, n_trials=trials - completed)
    finished = [trial for trial in study.trials if trial.state.name == "COMPLETE"]
    if not finished:
        raise ValueError("Preseason fitting produced no completed trials")

    declared_scores = {}
    declared_run_ids = {}
    for season_code in ordered_seasons:
        report = ProjectionBacktester(
            database,
            rules_by_season[season_code],
            config=base_config,
            model_version=f"{FIT_MODEL_VERSION}-declared",
        ).run(
            season_code=season_code,
            origin_gameweek_start=origin_gameweek_start,
            origin_gameweek_end=origin_gameweek_end,
            horizon_gameweeks=horizon_gameweeks,
            evidence_policy="performance_only",
        )
        declared_scores[season_code] = round(tuning_objective(report), 6)
        declared_run_ids[season_code] = report.backtest_run_id

    best_trial = study.best_trial
    best_trial_config = _config_from_preseason_parameters(
        best_trial.params,
        base_config,
    )
    robust_config, evidence = _robust_config(
        finished,
        ordered_seasons,
        base_config,
        best_trial_config,
    )
    robust_scores = {}
    robust_run_ids = {}
    for season_code in ordered_seasons:
        report = ProjectionBacktester(
            database,
            rules_by_season[season_code],
            config=robust_config,
            model_version=f"{FIT_MODEL_VERSION}-robust",
        ).run(
            season_code=season_code,
            origin_gameweek_start=origin_gameweek_start,
            origin_gameweek_end=origin_gameweek_end,
            horizon_gameweeks=horizon_gameweeks,
            evidence_policy="performance_only",
        )
        robust_scores[season_code] = round(tuning_objective(report), 6)
        robust_run_ids[season_code] = report.backtest_run_id

    folds = _leave_one_season_out(
        database,
        rules_by_season,
        finished,
        ordered_seasons,
        declared_scores,
        base_config,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
    )
    return PreseasonPriorFitResult(
        study_name=study.study_name,
        target_seasons=ordered_seasons,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        trials_requested=trials,
        trials_completed=len(finished),
        best_trial_number=best_trial.number,
        best_score=round(float(best_trial.value), 6),
        best_trial_config=best_trial_config,
        robust_config=robust_config,
        parameter_evidence=evidence,
        declared_parameters={
            name: float(getattr(base_config, name))
            for name in PRESEASON_PARAMETER_NAMES
        },
        robust_season_scores=robust_scores,
        best_trial_season_scores={
            season_code: round(float(score), 6)
            for season_code, score in best_trial.user_attrs["season_scores"].items()
        },
        declared_season_scores=declared_scores,
        declared_backtest_run_ids=declared_run_ids,
        robust_backtest_run_ids=robust_run_ids,
        leave_one_season_out=folds,
        limitations=_limitations(ordered_seasons, origin_gameweek_end),
    )


def _require_prior_seasons(
    database: HistoricalDatabase,
    target_seasons: tuple[str, ...],
) -> None:
    """Carry-forward reads the previous season, so it must be present."""

    available = {
        str(row["code"])
        for row in database.connection.execute("SELECT code FROM seasons")
    }
    for season_code in sorted(target_seasons):
        if season_code not in available:
            raise ValueError(f"Season {season_code!r} is unavailable")
        earlier = sorted(code for code in available if code < season_code)
        if not earlier:
            raise ValueError(
                f"Season {season_code!r} has no earlier season in the database, "
                "so carry-forward has nothing to read and its parameters "
                "cannot be estimated from it"
            )


def _suggest_preseason_config(
    trial: Any,
    base_config: ProjectionModelConfig,
) -> ProjectionModelConfig:
    minimum_factor = trial.suggest_float("cold_start_minimum_factor", 0.10, 0.80)
    parameters = {
        "carry_forward_regression_matches": trial.suggest_float(
            "carry_forward_regression_matches", 2.0, 30.0, log=True
        ),
        "promoted_team_attack_multiplier": trial.suggest_float(
            "promoted_team_attack_multiplier", 0.60, 1.05
        ),
        "promoted_team_defence_multiplier": trial.suggest_float(
            "promoted_team_defence_multiplier", 0.95, 1.60
        ),
        "cold_start_price_elasticity": trial.suggest_float(
            "cold_start_price_elasticity", 0.0, 3.0
        ),
        "cold_start_minimum_factor": minimum_factor,
        # Suggested as a multiple so the configuration's minimum <= maximum
        # invariant holds by construction instead of pruning trials.
        "cold_start_maximum_factor": minimum_factor
        * trial.suggest_float("cold_start_factor_span", 2.0, 12.0, log=True),
    }
    return replace(base_config, **parameters)


def _config_from_preseason_parameters(
    parameters: dict[str, Any],
    base_config: ProjectionModelConfig,
) -> ProjectionModelConfig:
    minimum_factor = float(parameters["cold_start_minimum_factor"])
    return replace(
        base_config,
        carry_forward_regression_matches=float(
            parameters["carry_forward_regression_matches"]
        ),
        promoted_team_attack_multiplier=float(
            parameters["promoted_team_attack_multiplier"]
        ),
        promoted_team_defence_multiplier=float(
            parameters["promoted_team_defence_multiplier"]
        ),
        cold_start_price_elasticity=float(
            parameters["cold_start_price_elasticity"]
        ),
        cold_start_minimum_factor=minimum_factor,
        cold_start_maximum_factor=(
            minimum_factor * float(parameters["cold_start_factor_span"])
        ),
    )


def _leading_statistics(
    trials: list[Any],
    seasons: tuple[str, ...],
    base_config: ProjectionModelConfig,
) -> dict[str, tuple[float, float, float]]:
    """Return (leading mean, leading deviation, whole-search deviation) per parameter.

    "Leading" is the best decile of trials, floored at three, scored on the
    supplied seasons only so this can also be called inside a fold.
    """

    def score(trial: Any) -> float:
        return sum(
            float(trial.user_attrs["season_scores"][season]) for season in seasons
        ) / len(seasons)

    ordered = sorted(trials, key=score)
    leading_count = max(3, len(ordered) // 10)
    leading = ordered[:leading_count]
    statistics: dict[str, tuple[float, float, float]] = {}
    for name in PRESEASON_PARAMETER_NAMES:
        leading_values = [
            float(getattr(_config_from_preseason_parameters(trial.params, base_config), name))
            for trial in leading
        ]
        search_values = [
            float(getattr(_config_from_preseason_parameters(trial.params, base_config), name))
            for trial in ordered
        ]
        statistics[name] = (
            _mean(leading_values),
            _deviation(leading_values),
            _deviation(search_values),
        )
    return statistics


def _robust_config(
    trials: list[Any],
    seasons: tuple[str, ...],
    base_config: ProjectionModelConfig,
    best_trial_config: ProjectionModelConfig,
) -> tuple[ProjectionModelConfig, tuple[ParameterEvidence, ...]]:
    """Move a parameter only where the leading trials disagree with the declared value.

    Six parameters against a handful of season starts will always produce six
    numbers, most of them noise. A parameter is treated as identified only when
    the declared value falls outside one deviation of the leading trials' mean;
    otherwise the declared value stands. The estimate itself is the leading
    mean rather than the single winning trial, which is one draw from that
    spread.
    """

    statistics = _leading_statistics(trials, seasons, base_config)
    updates: dict[str, float] = {}
    evidence = []
    for name in PRESEASON_PARAMETER_NAMES:
        leading_mean, leading_deviation, search_deviation = statistics[name]
        declared = float(getattr(base_config, name))
        identified = abs(leading_mean - declared) > leading_deviation
        if identified:
            updates[name] = leading_mean
        evidence.append(
            ParameterEvidence(
                name=name,
                declared=declared,
                best_trial=round(float(getattr(best_trial_config, name)), 6),
                leading_mean=round(leading_mean, 6),
                leading_deviation=round(leading_deviation, 6),
                search_deviation=round(search_deviation, 6),
                identified=identified,
                retained="fitted" if identified else "declared",
            )
        )
    # Applied together so the cold-start bounds are validated as a pair.
    candidate = replace(base_config, **updates)
    if candidate.cold_start_maximum_factor < candidate.cold_start_minimum_factor:
        candidate = replace(
            candidate,
            cold_start_maximum_factor=candidate.cold_start_minimum_factor,
        )
    return candidate, tuple(evidence)


def _leave_one_season_out(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    trials: list[Any],
    target_seasons: tuple[str, ...],
    declared_scores: dict[str, float],
    base_config: ProjectionModelConfig,
    *,
    origin_gameweek_start: int,
    origin_gameweek_end: int,
    horizon_gameweeks: int,
) -> tuple[LeaveOneSeasonOutFold, ...]:
    """Rebuild the shipped configuration without each season, then score it there.

    The fold repeats the whole selection rule, not just the trial ranking, so it
    measures the configuration that would actually be declared.
    """

    if len(target_seasons) < 2:
        return ()
    folds = []
    for held_out in target_seasons:
        others = tuple(season for season in target_seasons if season != held_out)
        fold_config, _ = _robust_config(trials, others, base_config, base_config)
        report = ProjectionBacktester(
            database,
            rules_by_season[held_out],
            config=fold_config,
            model_version=f"{FIT_MODEL_VERSION}-loso-{held_out}",
        ).run(
            season_code=held_out,
            origin_gameweek_start=origin_gameweek_start,
            origin_gameweek_end=origin_gameweek_end,
            horizon_gameweeks=horizon_gameweeks,
            evidence_policy="performance_only",
        )
        held_out_score = tuning_objective(report)
        declared = declared_scores[held_out]
        folds.append(
            LeaveOneSeasonOutFold(
                held_out_season=held_out,
                selected_parameters={
                    name: round(float(getattr(fold_config, name)), 6)
                    for name in PRESEASON_PARAMETER_NAMES
                },
                held_out_score=round(held_out_score, 6),
                declared_held_out_score=round(declared, 6),
                score_change=round(held_out_score - declared, 6),
            )
        )
    return tuple(folds)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return (sum((value - average) ** 2 for value in values) / (len(values) - 1)) ** 0.5


def _limitations(
    target_seasons: tuple[str, ...],
    origin_gameweek_end: int,
) -> tuple[str, ...]:
    return (
        f"Fitted on {len(target_seasons)} season "
        f"{'start' if len(target_seasons) == 1 else 'starts'} "
        f"({', '.join(target_seasons)}); six parameters against that little "
        "evidence can absorb noise, so read parameter_evidence and the "
        "leave-one-season-out folds before believing any point estimate.",
        "The shipped configuration moves only parameters whose leading trials "
        "disagree with the declared value by more than their own spread. "
        "Everything else keeps its declared value, so an unidentified "
        "parameter is not silently replaced by one draw from noise.",
        f"Only origins up to GW{origin_gameweek_end} are scored, because "
        "carry-forward decays as real fixtures arrive and cold starts stop "
        "binding once a player has played.",
        "The earliest imported season cannot be a target: carry-forward needs "
        "a previous season to read.",
        "Every non-Stage-3a field is held at the base configuration, so this "
        "is not a re-tune of the incumbent.",
        "Design evidence only. No historical result may promote a model; the "
        "fitted configuration still needs the forward 2026/27 gate.",
    )
