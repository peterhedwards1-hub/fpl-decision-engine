"""Fit the Stage 3a preseason priors on development folds only.

`preseason-priors-v1` declared its carry-forward and cold-start parameters
rather than estimating them. This module estimates them from the seasons the
project may still use for design, and reports how well the estimate transfers
between them. It never promotes anything: the output is a configuration to
declare and put through the forward gate.

Three properties are deliberate.

The search design is a fixed Halton sequence, generated before any outcome is
seen. An adaptive sampler proposes later points in response to results from
*every* season, so a fold that withheld one season would still be scored on
parameter combinations that season helped choose. A fixed design makes
leave-one-season-out genuinely out-of-season.

The objective is RMSE plus a bias penalty, not MAE. The project's own
conclusion is that MAE is the wrong gate for forecasts that feed an expected
points optimiser: it rewards the conditional median, while the optimiser needs
calibrated means. Decision metrics are reported beside the objective rather
than driving selection.

A parameter whose leading estimate sits against a search bound is reported as
censored, not identified. There the bound chose the value, not the data.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any

from .backtest import BacktestReport, ProjectionBacktester
from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import PRESEASON_V5_MODEL_CONFIG, ProjectionModelConfig

FIT_MODEL_VERSION = "rates-preseason-priors-fit"
FIT_OBJECTIVE_VERSION = "preseason-rmse-bias-v2"

#: Weight on absolute forecast bias, in RMSE units. Bias is the failure the
#: optimiser is least able to absorb, because it shifts every comparison.
BIAS_PENALTY_WEIGHT = 0.5

#: The Stage 3a configuration fields this module estimates. Every other field
#: is held at the base configuration so the fit cannot re-tune the incumbent.
PRESEASON_PARAMETER_NAMES = (
    "carry_forward_regression_matches",
    "promoted_team_attack_multiplier",
    "promoted_team_defence_multiplier",
    "cold_start_price_elasticity",
    "cold_start_minimum_factor",
    "cold_start_maximum_factor",
)

#: Search dimensions. The cold-start maximum is searched as a multiple of the
#: minimum so the configuration's minimum <= maximum invariant holds by
#: construction. Ranges are wide enough that an interior optimum is meaningful.
PRESEASON_SEARCH_SPACE: dict[str, tuple[float, float, str]] = {
    "carry_forward_regression_matches": (2.0, 80.0, "log"),
    "promoted_team_attack_multiplier": (0.55, 1.35, "linear"),
    "promoted_team_defence_multiplier": (0.80, 1.80, "linear"),
    "cold_start_price_elasticity": (0.0, 3.0, "linear"),
    "cold_start_minimum_factor": (0.10, 0.90, "linear"),
    "cold_start_factor_span": (1.5, 14.0, "log"),
}

#: Halton bases, one per search dimension, in the order above.
_HALTON_BASES = (2, 3, 5, 7, 11, 13)

#: A leading estimate within this fraction of a search bound is censored.
_BOUNDARY_TOLERANCE = 0.05

#: Seasons whose outcomes have already been queried by earlier evaluation work.
EXHAUSTED_SEASONS = frozenset({"2024-25", "2025-26"})


@dataclass(frozen=True)
class ParameterEvidence:
    """What the search actually established about one parameter."""

    name: str
    declared: float
    best_point: float
    leading_mean: float
    leading_deviation: float
    search_deviation: float
    identified: bool
    at_boundary: bool
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
class HorizonCheck:
    """The shipped configuration scored at a horizon it was not fitted on."""

    horizon_gameweeks: int
    season_code: str
    declared_score: float
    fitted_score: float
    score_change: float


@dataclass(frozen=True)
class PreseasonPriorFitResult:
    target_seasons: tuple[str, ...]
    origin_gameweek_start: int
    origin_gameweek_end: int
    horizon_gameweeks: int
    design_points: int
    best_point_index: int
    best_score: float
    best_point_config: ProjectionModelConfig
    robust_config: ProjectionModelConfig
    parameter_evidence: tuple[ParameterEvidence, ...]
    declared_parameters: dict[str, float]
    robust_season_scores: dict[str, float]
    declared_season_scores: dict[str, float]
    declared_diagnostics: dict[str, dict[str, float]]
    robust_diagnostics: dict[str, dict[str, float]]
    leave_one_season_out: tuple[LeaveOneSeasonOutFold, ...]
    horizon_checks: tuple[HorizonCheck, ...]
    limitations: tuple[str, ...]

    @property
    def best_config(self) -> ProjectionModelConfig:
        """The configuration worth declaring: robust, not the raw winner."""

        return self.robust_config

    @property
    def transfers_out_of_season(self) -> bool:
        """True when every withheld season preferred the fitted parameters.

        The design is outcome-independent, so a fold's training seasons chose
        the ranking only — not the points being ranked. That makes this a real
        out-of-season comparison, on as many folds as there are seasons.
        """

        return bool(self.leave_one_season_out) and all(
            fold.score_change < 0 for fold in self.leave_one_season_out
        )

    @property
    def holds_at_other_horizons(self) -> bool:
        return bool(self.horizon_checks) and all(
            check.score_change < 0 for check in self.horizon_checks
        )

    @property
    def identified_parameters(self) -> tuple[str, ...]:
        return tuple(
            evidence.name
            for evidence in self.parameter_evidence
            if evidence.identified
        )

    @property
    def censored_parameters(self) -> tuple[str, ...]:
        return tuple(
            evidence.name
            for evidence in self.parameter_evidence
            if evidence.at_boundary
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective_version": FIT_OBJECTIVE_VERSION,
            "objective": (
                "overall points RMSE + "
                f"{BIAS_PENALTY_WEIGHT} * |overall points bias|; "
                "decision metrics are reported, not optimised"
            ),
            "search_design": "fixed Halton sequence, generated before scoring",
            "search_space": {
                name: {"low": low, "high": high, "scale": scale}
                for name, (low, high, scale) in PRESEASON_SEARCH_SPACE.items()
            },
            "target_seasons": list(self.target_seasons),
            "origin_gameweek_start": self.origin_gameweek_start,
            "origin_gameweek_end": self.origin_gameweek_end,
            "horizon_gameweeks": self.horizon_gameweeks,
            "design_points": self.design_points,
            "best_point_index": self.best_point_index,
            "best_score": self.best_score,
            "best_point_config": asdict(self.best_point_config),
            "robust_config": asdict(self.robust_config),
            "parameter_evidence": [
                evidence.as_dict() for evidence in self.parameter_evidence
            ],
            "identified_parameters": list(self.identified_parameters),
            "censored_parameters": list(self.censored_parameters),
            "declared_parameters": self.declared_parameters,
            "robust_season_scores": self.robust_season_scores,
            "declared_season_scores": self.declared_season_scores,
            "declared_diagnostics": self.declared_diagnostics,
            "robust_diagnostics": self.robust_diagnostics,
            "leave_one_season_out": [
                asdict(fold) for fold in self.leave_one_season_out
            ],
            "transfers_out_of_season": self.transfers_out_of_season,
            "horizon_checks": [asdict(check) for check in self.horizon_checks],
            "holds_at_other_horizons": self.holds_at_other_horizons,
            "limitations": list(self.limitations),
            "promotion_status": (
                "Design evidence only. The robust configuration must be "
                "registered as a forward candidate and pass both gate tiers "
                "on 2026/27 before it may replace anything."
            ),
        }


def preseason_objective(report: BacktestReport) -> float:
    """RMSE with a bias penalty: what an expected-points optimiser needs.

    Deliberately not `tuning_objective`, whose dominant term is top-100 MAE.
    MAE rewards something closer to the conditional median, so a configuration
    can improve it while worsening the calibrated means the squad optimiser
    integrates over.
    """

    return report.overall.points_rmse + BIAS_PENALTY_WEIGHT * abs(
        report.overall.points_bias
    )


def _diagnostics(report: BacktestReport) -> dict[str, float]:
    """Decision-relevant metrics, reported beside the objective."""

    top_100 = next(metric for metric in report.top_n if metric.value == "100")
    return {
        "points_rmse": round(report.overall.points_rmse, 6),
        "points_bias": round(report.overall.points_bias, 6),
        "points_mae": round(report.overall.points_mae, 6),
        "minutes_mae": round(report.overall.minutes_mae, 6),
        "top_100_points_rmse": round(top_100.points_rmse, 6),
        "top_100_points_mae": round(top_100.points_mae, 6),
        "top_100_points_bias": round(top_100.points_bias, 6),
        "samples": report.overall.samples,
    }


def halton(index: int, base: int) -> float:
    """The `index`-th point of the Halton sequence in the given base."""

    if index < 1:
        raise ValueError("Halton indices start at 1")
    if base < 2:
        raise ValueError("Halton bases must be at least 2")
    fraction = 1.0
    result = 0.0
    remaining = index
    while remaining > 0:
        fraction /= base
        result += fraction * (remaining % base)
        remaining //= base
    return result


def design_points(count: int) -> tuple[dict[str, float], ...]:
    """A fixed, outcome-independent space-filling design over the search space."""

    if count < 4:
        raise ValueError("A usable design needs at least four points")
    names = tuple(PRESEASON_SEARCH_SPACE)
    points = []
    for index in range(1, count + 1):
        point = {}
        for dimension, name in enumerate(names):
            low, high, scale = PRESEASON_SEARCH_SPACE[name]
            unit = halton(index, _HALTON_BASES[dimension])
            if scale == "log":
                point[name] = low * (high / low) ** unit
            else:
                point[name] = low + (high - low) * unit
        points.append(point)
    return tuple(points)


def config_from_point(
    point: dict[str, float],
    base_config: ProjectionModelConfig,
) -> ProjectionModelConfig:
    minimum_factor = float(point["cold_start_minimum_factor"])
    return replace(
        base_config,
        carry_forward_regression_matches=float(
            point["carry_forward_regression_matches"]
        ),
        promoted_team_attack_multiplier=float(
            point["promoted_team_attack_multiplier"]
        ),
        promoted_team_defence_multiplier=float(
            point["promoted_team_defence_multiplier"]
        ),
        cold_start_price_elasticity=float(point["cold_start_price_elasticity"]),
        cold_start_minimum_factor=minimum_factor,
        cold_start_maximum_factor=minimum_factor
        * float(point["cold_start_factor_span"]),
    )


def fit_preseason_priors(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    *,
    target_seasons: tuple[str, ...],
    origin_gameweek_start: int = 1,
    origin_gameweek_end: int = 8,
    horizon_gameweeks: int = 1,
    design_size: int = 48,
    confirmation_horizons: tuple[int, ...] = (8,),
    base_config: ProjectionModelConfig = PRESEASON_V5_MODEL_CONFIG,
) -> PreseasonPriorFitResult:
    """Estimate the Stage 3a priors over each season's opening Gameweeks."""

    _validate_request(
        database,
        rules_by_season,
        target_seasons=target_seasons,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        base_config=base_config,
    )
    ordered_seasons = tuple(sorted(target_seasons))
    points = design_points(design_size)

    def score(
        config: ProjectionModelConfig,
        season_code: str,
        horizon: int,
        label: str,
    ) -> tuple[float, BacktestReport]:
        report = ProjectionBacktester(
            database,
            rules_by_season[season_code],
            config=config,
            model_version=f"{FIT_MODEL_VERSION}-{label}",
        ).run(
            season_code=season_code,
            origin_gameweek_start=origin_gameweek_start,
            origin_gameweek_end=origin_gameweek_end,
            horizon_gameweeks=horizon,
            evidence_policy="performance_only",
        )
        return preseason_objective(report), report

    # One score per design point per season, computed once and reused by both
    # the overall selection and every fold.
    matrix: list[dict[str, float]] = []
    for index, point in enumerate(points):
        config = config_from_point(point, base_config)
        matrix.append(
            {
                season_code: score(
                    config, season_code, horizon_gameweeks, f"design-{index}"
                )[0]
                for season_code in ordered_seasons
            }
        )

    declared_scores = {}
    declared_diagnostics = {}
    for season_code in ordered_seasons:
        value, report = score(
            base_config, season_code, horizon_gameweeks, "declared"
        )
        declared_scores[season_code] = round(value, 6)
        declared_diagnostics[season_code] = _diagnostics(report)

    best_index = min(
        range(len(points)),
        key=lambda index: _mean(
            [matrix[index][season] for season in ordered_seasons]
        ),
    )
    best_point_config = config_from_point(points[best_index], base_config)
    robust_config, evidence = _robust_config(
        points,
        matrix,
        ordered_seasons,
        base_config,
        best_point_config,
    )

    robust_scores = {}
    robust_diagnostics = {}
    for season_code in ordered_seasons:
        value, report = score(
            robust_config, season_code, horizon_gameweeks, "robust"
        )
        robust_scores[season_code] = round(value, 6)
        robust_diagnostics[season_code] = _diagnostics(report)

    folds = []
    if len(ordered_seasons) >= 2:
        for held_out in ordered_seasons:
            others = tuple(
                season for season in ordered_seasons if season != held_out
            )
            fold_config, _ = _robust_config(
                points,
                matrix,
                others,
                base_config,
                base_config,
            )
            value, _ = score(
                fold_config, held_out, horizon_gameweeks, f"loso-{held_out}"
            )
            folds.append(
                LeaveOneSeasonOutFold(
                    held_out_season=held_out,
                    selected_parameters=_parameter_values(fold_config),
                    held_out_score=round(value, 6),
                    declared_held_out_score=declared_scores[held_out],
                    score_change=round(value - declared_scores[held_out], 6),
                )
            )

    # The opening-squad use case is a multi-Gameweek horizon, so the fitted
    # configuration is checked at one it was not selected on.
    checks = []
    for horizon in confirmation_horizons:
        if horizon == horizon_gameweeks:
            continue
        for season_code in ordered_seasons:
            declared_value, _ = score(
                base_config, season_code, horizon, f"declared-h{horizon}"
            )
            fitted_value, _ = score(
                robust_config, season_code, horizon, f"robust-h{horizon}"
            )
            checks.append(
                HorizonCheck(
                    horizon_gameweeks=horizon,
                    season_code=season_code,
                    declared_score=round(declared_value, 6),
                    fitted_score=round(fitted_value, 6),
                    score_change=round(fitted_value - declared_value, 6),
                )
            )

    return PreseasonPriorFitResult(
        target_seasons=ordered_seasons,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        design_points=len(points),
        best_point_index=best_index,
        best_score=round(
            _mean([matrix[best_index][season] for season in ordered_seasons]), 6
        ),
        best_point_config=best_point_config,
        robust_config=robust_config,
        parameter_evidence=evidence,
        declared_parameters=_parameter_values(base_config),
        robust_season_scores=robust_scores,
        declared_season_scores=declared_scores,
        declared_diagnostics=declared_diagnostics,
        robust_diagnostics=robust_diagnostics,
        leave_one_season_out=tuple(folds),
        horizon_checks=tuple(checks),
        limitations=_limitations(ordered_seasons, origin_gameweek_end),
    )


def profile_preseason_prior(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    *,
    parameter: str,
    target_seasons: tuple[str, ...],
    low: float,
    high: float,
    steps: int = 7,
    origin_gameweek_start: int = 1,
    origin_gameweek_end: int = 8,
    horizon_gameweeks: int = 1,
    base_config: ProjectionModelConfig = PRESEASON_V5_MODEL_CONFIG,
) -> dict[str, Any]:
    """Sweep one parameter with the others fixed, and report the curve.

    A joint search cannot say whether an optimum sits against a bound because
    the objective wanted to keep going, or because that is simply where the
    leading points happened to land. Holding everything else still and walking
    one axis can.
    """

    if parameter not in PRESEASON_PARAMETER_NAMES:
        raise ValueError(
            f"Unknown preseason parameter {parameter!r}; expected one of "
            + ", ".join(PRESEASON_PARAMETER_NAMES)
        )
    if steps < 3:
        raise ValueError("A profile needs at least three steps")
    if not low < high:
        raise ValueError("Profile range must be increasing")
    _validate_request(
        database,
        rules_by_season,
        target_seasons=target_seasons,
        origin_gameweek_start=origin_gameweek_start,
        origin_gameweek_end=origin_gameweek_end,
        horizon_gameweeks=horizon_gameweeks,
        base_config=base_config,
    )
    ordered_seasons = tuple(sorted(target_seasons))
    curve = []
    for step in range(steps):
        value = low + (high - low) * step / (steps - 1)
        config = replace(base_config, **{parameter: value})
        if config.cold_start_maximum_factor < config.cold_start_minimum_factor:
            continue
        scores = {}
        for season_code in ordered_seasons:
            report = ProjectionBacktester(
                database,
                rules_by_season[season_code],
                config=config,
                model_version=f"{FIT_MODEL_VERSION}-profile",
            ).run(
                season_code=season_code,
                origin_gameweek_start=origin_gameweek_start,
                origin_gameweek_end=origin_gameweek_end,
                horizon_gameweeks=horizon_gameweeks,
                evidence_policy="performance_only",
            )
            scores[season_code] = round(preseason_objective(report), 6)
        curve.append(
            {
                "value": round(value, 6),
                "season_scores": scores,
                "mean_score": round(_mean(list(scores.values())), 6),
            }
        )
    best = min(curve, key=lambda entry: entry["mean_score"])
    span = high - low
    return {
        "parameter": parameter,
        "objective_version": FIT_OBJECTIVE_VERSION,
        "target_seasons": list(ordered_seasons),
        "horizon_gameweeks": horizon_gameweeks,
        "held_at": {
            name: float(getattr(base_config, name))
            for name in PRESEASON_PARAMETER_NAMES
            if name != parameter
        },
        "low": low,
        "high": high,
        "curve": curve,
        "best_value": best["value"],
        "best_mean_score": best["mean_score"],
        "at_low_boundary": best["value"] - low <= span * _BOUNDARY_TOLERANCE,
        "at_high_boundary": high - best["value"] <= span * _BOUNDARY_TOLERANCE,
        "note": (
            "A minimum at either boundary means the range is too narrow to "
            "identify this parameter, not that the boundary is the estimate."
        ),
    }


def _validate_request(
    database: HistoricalDatabase,
    rules_by_season: dict[str, SeasonRules],
    *,
    target_seasons: tuple[str, ...],
    origin_gameweek_start: int,
    origin_gameweek_end: int,
    horizon_gameweeks: int,
    base_config: ProjectionModelConfig,
) -> None:
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


def _parameter_values(config: ProjectionModelConfig) -> dict[str, float]:
    return {
        name: round(float(getattr(config, name)), 6)
        for name in PRESEASON_PARAMETER_NAMES
    }


def _configuration_bounds() -> dict[str, tuple[float, float]]:
    """Reachable range of each configuration field under the search space."""

    bounds = {}
    for name in PRESEASON_PARAMETER_NAMES:
        if name in PRESEASON_SEARCH_SPACE:
            low, high, _ = PRESEASON_SEARCH_SPACE[name]
            bounds[name] = (low, high)
    minimum_low, minimum_high, _ = PRESEASON_SEARCH_SPACE[
        "cold_start_minimum_factor"
    ]
    span_low, span_high, _ = PRESEASON_SEARCH_SPACE["cold_start_factor_span"]
    bounds["cold_start_maximum_factor"] = (
        minimum_low * span_low,
        minimum_high * span_high,
    )
    return bounds


def _robust_config(
    points: tuple[dict[str, float], ...],
    matrix: list[dict[str, float]],
    seasons: tuple[str, ...],
    base_config: ProjectionModelConfig,
    best_point_config: ProjectionModelConfig,
) -> tuple[ProjectionModelConfig, tuple[ParameterEvidence, ...]]:
    """Move a parameter only where the leading points disagree with the declared value.

    Six parameters against a handful of season starts will always produce six
    numbers, most of them noise. A parameter moves only when the declared value
    falls outside one deviation of the leading points' mean, and never when
    that mean sits against a search bound.
    """

    order = sorted(
        range(len(points)),
        key=lambda index: _mean([matrix[index][season] for season in seasons]),
    )
    leading = order[: max(3, len(order) // 10)]
    bounds = _configuration_bounds()
    updates: dict[str, float] = {}
    evidence = []
    for name in PRESEASON_PARAMETER_NAMES:
        leading_values = [
            float(getattr(config_from_point(points[index], base_config), name))
            for index in leading
        ]
        search_values = [
            float(getattr(config_from_point(points[index], base_config), name))
            for index in order
        ]
        leading_mean = _mean(leading_values)
        leading_deviation = _deviation(leading_values)
        declared = float(getattr(base_config, name))
        low, high = bounds[name]
        span = high - low
        at_boundary = (
            leading_mean - low <= span * _BOUNDARY_TOLERANCE
            or high - leading_mean <= span * _BOUNDARY_TOLERANCE
        )
        identified = (
            abs(leading_mean - declared) > leading_deviation and not at_boundary
        )
        if identified:
            updates[name] = leading_mean
        evidence.append(
            ParameterEvidence(
                name=name,
                declared=declared,
                best_point=round(float(getattr(best_point_config, name)), 6),
                leading_mean=round(leading_mean, 6),
                leading_deviation=round(leading_deviation, 6),
                search_deviation=round(_deviation(search_values), 6),
                identified=identified,
                at_boundary=at_boundary,
                retained=(
                    "fitted"
                    if identified
                    else "declared (censored)"
                    if at_boundary
                    else "declared"
                ),
            )
        )
    candidate = replace(base_config, **updates)
    if candidate.cold_start_maximum_factor < candidate.cold_start_minimum_factor:
        candidate = replace(
            candidate,
            cold_start_maximum_factor=candidate.cold_start_minimum_factor,
        )
    return candidate, tuple(evidence)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(
        sum((value - average) ** 2 for value in values) / (len(values) - 1)
    )


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
        "The shipped configuration moves only parameters whose leading points "
        "disagree with the declared value by more than their own spread and "
        "do not sit against a search bound.",
        f"Only origins up to GW{origin_gameweek_end} are scored, because "
        "carry-forward decays as real fixtures arrive and cold starts stop "
        "binding once a player has played.",
        "The earliest imported season cannot be a target: carry-forward needs "
        "a previous season to read.",
        "Every non-Stage-3a field is held at the base configuration, so this "
        "is not a re-tune of the incumbent.",
        "Promoted-club parameters rest on three clubs per season start, which "
        "is far thinner than the row counts suggest.",
        "Design evidence only. No historical result may promote a model; the "
        "fitted configuration still needs the forward 2026/27 gate.",
    )
