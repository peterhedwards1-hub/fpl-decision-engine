"""Stage-one forecast diagnostics with paired, time-aware comparisons."""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .evaluation import load_backtest_benchmark_rows
from .history.database import HistoricalDatabase

SUPPORTED_BASELINES = (
    "season_points_per_fixture",
    "recent_4_points_per_fixture",
    "season_points_per_90_model_minutes",
    "position_points_per_fixture",
)


def build_stage_one_diagnostics(
    database: HistoricalDatabase,
    backtest_run_ids: tuple[int, ...],
    *,
    baseline_method: str = "season_points_per_fixture",
    bootstrap_samples: int = 2_000,
    moving_block_gameweeks: int = 3,
    minimum_slice_samples: int = 100,
    seed: int = 20260730,
) -> dict[str, Any]:
    """Build paired uncertainty, calibration, slicing and oracle diagnostics."""

    if not backtest_run_ids:
        raise ValueError("At least one completed backtest run is required")
    if baseline_method not in SUPPORTED_BASELINES:
        raise ValueError(f"Unsupported baseline method {baseline_method!r}")
    if bootstrap_samples <= 0:
        raise ValueError("Bootstrap samples must be positive")
    if moving_block_gameweeks <= 0:
        raise ValueError("Moving block length must be positive")
    if minimum_slice_samples <= 0:
        raise ValueError("Minimum slice samples must be positive")

    rows: list[dict[str, Any]] = []
    run_summaries = []
    for run_id in dict.fromkeys(backtest_run_ids):
        run, forecasts = load_backtest_benchmark_rows(database, run_id)
        model_rows = forecasts["model"]
        baseline_rows = forecasts[baseline_method]
        if len(model_rows) != len(baseline_rows):
            raise RuntimeError("Model and baseline rows are not aligned")
        for model, baseline in zip(model_rows, baseline_rows, strict=True):
            identity = (
                "origin_gameweek",
                "target_gameweek",
                "player_season_id",
            )
            if any(model[name] != baseline[name] for name in identity):
                raise RuntimeError("Model and baseline row identities differ")
            rows.append(
                {
                    **model,
                    "model_expected_points": float(
                        model["benchmark_expected_points"]
                    ),
                    "baseline_expected_points": float(
                        baseline["benchmark_expected_points"]
                    ),
                }
            )
        run_summaries.append(
            {
                "backtest_run_id": run_id,
                "season_code": run["season_code"],
                "model_version": run["model_version"],
                "horizon_gameweeks": run["horizon_gameweeks"],
                "samples": len(model_rows),
            }
        )

    _assign_forecast_rank_bands(rows)
    _attach_actual_event_outcomes(database, rows)
    return {
        "schema_version": 1,
        "purpose": (
            "Historical design diagnostics only; this report is not a new "
            "promotion holdout."
        ),
        "runs": run_summaries,
        "baseline_method": baseline_method,
        "paired_moving_block_bootstrap": _paired_bootstrap(
            rows,
            samples=bootstrap_samples,
            moving_block_gameweeks=moving_block_gameweeks,
            seed=seed,
        ),
        "calibration": {
            "appearance": _probability_calibration(
                rows,
                probability_name="appearance_probability",
                outcome=lambda row: float(int(row["actual_minutes"]) > 0),
            ),
            "sixty_minutes": _probability_calibration(
                rows,
                probability_name="sixty_probability",
                outcome=lambda row: float(int(row["actual_minutes"]) >= 60),
            ),
            "points": _points_calibration(rows),
        },
        "residual_slices": _residual_slices(
            rows,
            minimum_samples=minimum_slice_samples,
        ),
        "oracle_sensitivity": _oracle_sensitivity(rows),
        "limitations": [
            "Bootstrap resamples seasons and consecutive target-Gameweek blocks; "
            "five seasons still provide weak between-season uncertainty.",
            "Probability calibration excludes double Gameweeks because persisted "
            "appearance probabilities describe at-least-one appearance there.",
            "Global top-one regret is a forecast-ranking diagnostic, not a captain "
            "choice from a manager's owned squad.",
            "Residual slices omit fixture difficulty, promoted-club and role-change "
            "labels until those origin-time features are persisted.",
            "Oracle estimates are sensitivity bounds and are not additive error "
            "attributions.",
        ],
    }


def write_stage_one_diagnostics(
    report: dict[str, Any],
    output_path: str | Path,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _paired_bootstrap(
    rows: list[dict[str, Any]],
    *,
    samples: int,
    moving_block_gameweeks: int,
    seed: int,
) -> dict[str, Any]:
    blocks = _comparison_blocks(rows)
    by_season: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        by_season[str(block["season_code"])].append(block)
    for season_blocks in by_season.values():
        season_blocks.sort(key=lambda block: int(block["target_gameweek"]))
    seasons = sorted(by_season)
    rng = random.Random(seed)
    metrics = {
        "points_rmse": _rmse_difference,
        "absolute_points_bias": _absolute_bias_difference,
        "global_top_1_regret": _top_one_difference,
        "unconstrained_top_15_regret": _top_fifteen_difference,
    }
    draws: dict[str, list[float]] = {name: [] for name in metrics}
    for _ in range(samples):
        sampled = _moving_block_sample(
            by_season,
            seasons,
            block_length=moving_block_gameweeks,
            rng=rng,
        )
        for name, metric in metrics.items():
            draws[name].append(metric(sampled))

    results = {}
    for name, metric in metrics.items():
        block_differences = [
            _block_metric_difference(block, name)
            for block in blocks
            if _block_has_metric(block, name)
        ]
        season_differences = {
            season: metric(season_blocks)
            for season, season_blocks in by_season.items()
        }
        worst_season = max(
            season_differences,
            key=season_differences.__getitem__,
        )
        values = sorted(draws[name])
        results[name] = {
            "difference_model_minus_baseline": round(metric(blocks), 4),
            "mean_paired_block_difference": round(
                statistics.fmean(block_differences),
                4,
            ),
            "median_paired_block_difference": round(
                statistics.median(block_differences),
                4,
            ),
            "interval_80": [
                round(_quantile(values, 0.10), 4),
                round(_quantile(values, 0.90), 4),
            ],
            "interval_95": [
                round(_quantile(values, 0.025), 4),
                round(_quantile(values, 0.975), 4),
            ],
            "percentage_target_gameweek_blocks_won": round(
                100.0
                * sum(value < 0 for value in block_differences)
                / len(block_differences),
                1,
            ),
            "worst_season": {
                "season_code": worst_season,
                "difference": round(
                    season_differences[worst_season],
                    4,
                ),
            },
            "season_differences": {
                season: round(value, 4)
                for season, value in season_differences.items()
            },
            "lower_is_better": True,
        }
    results["points_rmse"]["by_position"] = _position_rmse_differences(rows)
    return {
        "resampling_unit": (
            f"season plus circular moving blocks of "
            f"{moving_block_gameweeks} target Gameweeks"
        ),
        "bootstrap_samples": samples,
        "seed": seed,
        "target_gameweek_blocks": len(blocks),
        "metrics": results,
    }


def _comparison_blocks(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (str(row["season_code"]), int(row["target_gameweek"]))
        ].append(row)
    blocks = []
    for (season, target), block_rows in sorted(grouped.items()):
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in block_rows:
            groups[
                (
                    int(row["origin_gameweek"]),
                    int(row["target_gameweek"]),
                )
            ].append(row)
        top_one_model = []
        top_one_baseline = []
        top_fifteen_model = []
        top_fifteen_baseline = []
        for group_rows in groups.values():
            actual_best = max(float(row["actual_points"]) for row in group_rows)
            model_sorted = sorted(
                group_rows,
                key=lambda row: (
                    -float(row["model_expected_points"]),
                    int(row["player_season_id"]),
                ),
            )
            baseline_sorted = sorted(
                group_rows,
                key=lambda row: (
                    -float(row["baseline_expected_points"]),
                    int(row["player_season_id"]),
                ),
            )
            actual_sorted = sorted(
                group_rows,
                key=lambda row: (
                    -float(row["actual_points"]),
                    int(row["player_season_id"]),
                ),
            )
            top_one_model.append(
                actual_best - float(model_sorted[0]["actual_points"])
            )
            top_one_baseline.append(
                actual_best - float(baseline_sorted[0]["actual_points"])
            )
            oracle_top_fifteen = sum(
                float(row["actual_points"]) for row in actual_sorted[:15]
            )
            top_fifteen_model.append(
                oracle_top_fifteen
                - sum(
                    float(row["actual_points"])
                    for row in model_sorted[:15]
                )
            )
            top_fifteen_baseline.append(
                oracle_top_fifteen
                - sum(
                    float(row["actual_points"])
                    for row in baseline_sorted[:15]
                )
            )
        blocks.append(
            {
                "season_code": season,
                "target_gameweek": target,
                "count": len(block_rows),
                "model_squared_error": sum(
                    (
                        float(row["actual_points"])
                        - float(row["model_expected_points"])
                    )
                    ** 2
                    for row in block_rows
                ),
                "baseline_squared_error": sum(
                    (
                        float(row["actual_points"])
                        - float(row["baseline_expected_points"])
                    )
                    ** 2
                    for row in block_rows
                ),
                "model_error": sum(
                    float(row["actual_points"])
                    - float(row["model_expected_points"])
                    for row in block_rows
                ),
                "baseline_error": sum(
                    float(row["actual_points"])
                    - float(row["baseline_expected_points"])
                    for row in block_rows
                ),
                "top_one_model": top_one_model,
                "top_one_baseline": top_one_baseline,
                "top_fifteen_model": top_fifteen_model,
                "top_fifteen_baseline": top_fifteen_baseline,
            }
        )
    return blocks


def _moving_block_sample(
    by_season: dict[str, list[dict[str, Any]]],
    seasons: list[str],
    *,
    block_length: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    sampled = []
    for _ in seasons:
        season = rng.choice(seasons)
        source = by_season[season]
        needed = len(source)
        while needed > 0:
            start = rng.randrange(len(source))
            take = min(block_length, needed)
            sampled.extend(
                source[(start + offset) % len(source)]
                for offset in range(take)
            )
            needed -= take
    return sampled


def _rmse_difference(blocks: list[dict[str, Any]]) -> float:
    count = sum(int(block["count"]) for block in blocks)
    model = math.sqrt(
        sum(float(block["model_squared_error"]) for block in blocks) / count
    )
    baseline = math.sqrt(
        sum(float(block["baseline_squared_error"]) for block in blocks)
        / count
    )
    return model - baseline


def _absolute_bias_difference(blocks: list[dict[str, Any]]) -> float:
    count = sum(int(block["count"]) for block in blocks)
    model = abs(sum(float(block["model_error"]) for block in blocks) / count)
    baseline = abs(
        sum(float(block["baseline_error"]) for block in blocks) / count
    )
    return model - baseline


def _regret_difference(
    blocks: list[dict[str, Any]],
    model_name: str,
    baseline_name: str,
) -> float:
    model_values = [
        float(value)
        for block in blocks
        for value in block[model_name]
    ]
    baseline_values = [
        float(value)
        for block in blocks
        for value in block[baseline_name]
    ]
    return statistics.fmean(model_values) - statistics.fmean(
        baseline_values
    )


def _top_one_difference(blocks: list[dict[str, Any]]) -> float:
    return _regret_difference(
        blocks,
        "top_one_model",
        "top_one_baseline",
    )


def _top_fifteen_difference(blocks: list[dict[str, Any]]) -> float:
    return _regret_difference(
        blocks,
        "top_fifteen_model",
        "top_fifteen_baseline",
    )


def _block_metric_difference(
    block: dict[str, Any],
    name: str,
) -> float:
    if name == "points_rmse":
        return _rmse_difference([block])
    if name == "absolute_points_bias":
        return _absolute_bias_difference([block])
    if name == "global_top_1_regret":
        return _top_one_difference([block])
    return _top_fifteen_difference([block])


def _block_has_metric(block: dict[str, Any], name: str) -> bool:
    if name == "global_top_1_regret":
        return bool(block["top_one_model"])
    if name == "unconstrained_top_15_regret":
        return bool(block["top_fifteen_model"])
    return int(block["count"]) > 0


def _position_rmse_differences(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["position"])].append(row)
    result = []
    for position, position_rows in sorted(grouped.items()):
        model = _rmse(position_rows, "model_expected_points")
        baseline = _rmse(position_rows, "baseline_expected_points")
        result.append(
            {
                "position": position,
                "samples": len(position_rows),
                "model_rmse": round(model, 4),
                "baseline_rmse": round(baseline, 4),
                "difference_model_minus_baseline": round(
                    model - baseline,
                    4,
                ),
            }
        )
    return result


def _probability_calibration(
    rows: list[dict[str, Any]],
    *,
    probability_name: str,
    outcome: Callable[[dict[str, Any]], float],
) -> dict[str, Any]:
    eligible = [row for row in rows if int(row["fixture_count"]) == 1]
    bins: dict[int, list[tuple[float, float]]] = defaultdict(list)
    brier = 0.0
    log_loss = 0.0
    for row in eligible:
        probability = min(1.0, max(0.0, float(row[probability_name])))
        actual = outcome(row)
        bins[min(9, int(probability * 10))].append((probability, actual))
        brier += (probability - actual) ** 2
        clipped = min(1.0 - 1e-15, max(1e-15, probability))
        log_loss -= (
            actual * math.log(clipped)
            + (1.0 - actual) * math.log(1.0 - clipped)
        )
    if not eligible:
        return {
            "samples": 0,
            "brier_score": None,
            "log_loss": None,
            "bins": [],
        }
    return {
        "samples": len(eligible),
        "excluded_multi_fixture_rows": len(rows) - len(eligible),
        "brier_score": round(brier / len(eligible), 6),
        "log_loss": round(log_loss / len(eligible), 6),
        "bins": [
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "samples": len(values),
                "mean_predicted": round(
                    statistics.fmean(value[0] for value in values),
                    4,
                ),
                "observed_frequency": round(
                    statistics.fmean(value[1] for value in values),
                    4,
                ),
            }
            for index, values in sorted(bins.items())
        ],
    }


def _points_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "overall": _point_deciles(rows),
        "by_position": {
            position: _point_deciles(
                [row for row in rows if str(row["position"]) == position]
            )
            for position in sorted({str(row["position"]) for row in rows})
        },
        "by_horizon": {
            str(horizon): _point_deciles(
                [
                    row
                    for row in rows
                    if int(row["horizon_step"]) == horizon
                ]
            )
            for horizon in sorted(
                {int(row["horizon_step"]) for row in rows}
            )
        },
    }


def _point_deciles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            float(row["model_expected_points"]),
            int(row["player_season_id"]),
        ),
    )
    bins: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(ordered):
        bins[min(9, index * 10 // len(ordered))].append(row)
    return [
        {
            "decile": index + 1,
            "samples": len(values),
            "mean_predicted": round(
                statistics.fmean(
                    float(row["model_expected_points"]) for row in values
                ),
                4,
            ),
            "mean_actual": round(
                statistics.fmean(
                    float(row["actual_points"]) for row in values
                ),
                4,
            ),
            "bias_actual_minus_predicted": round(
                statistics.fmean(
                    float(row["actual_points"])
                    - float(row["model_expected_points"])
                    for row in values
                ),
                4,
            ),
        }
        for index, values in sorted(bins.items())
    ]


def _assign_forecast_rank_bands(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(
        list
    )
    for row in rows:
        groups[
            (
                str(row["season_code"]),
                int(row["origin_gameweek"]),
                int(row["target_gameweek"]),
            )
        ].append(row)
    for group_rows in groups.values():
        ordered = sorted(
            group_rows,
            key=lambda row: (
                -float(row["model_expected_points"]),
                int(row["player_season_id"]),
            ),
        )
        for rank, row in enumerate(ordered, start=1):
            row["_rank_band"] = (
                "top_15"
                if rank <= 15
                else (
                    "16_to_50"
                    if rank <= 50
                    else "51_to_100" if rank <= 100 else "outside_100"
                )
            )


def _residual_slices(
    rows: list[dict[str, Any]],
    *,
    minimum_samples: int,
) -> list[dict[str, Any]]:
    dimensions: dict[str, Callable[[dict[str, Any]], str]] = {
        "position": lambda row: str(row["position"]),
        "predicted_minutes": lambda row: _minutes_band(
            float(row["expected_minutes"])
        ),
        "participation": lambda row: (
            "played" if int(row["actual_minutes"]) > 0 else "dnp"
        ),
        "fixture_count": lambda row: (
            "single" if int(row["fixture_count"]) == 1 else "double_or_more"
        ),
        "season_phase": lambda row: _season_phase(
            int(row["target_gameweek"])
        ),
        "forecast_rank": lambda row: str(row["_rank_band"]),
        "horizon": lambda row: f"GW+{int(row['horizon_step'])}",
    }
    slices = []
    for dimension, classifier in dimensions.items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[classifier(row)].append(row)
        for value, values in sorted(grouped.items()):
            if len(values) < minimum_samples:
                continue
            summary = _forecast_summary(values, "model_expected_points")
            slices.append(
                {
                    "dimension": dimension,
                    "value": value,
                    **summary,
                }
            )
    return slices


def _oracle_sensitivity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if int(row["fixture_count"]) == 1
        and row.get("component_points_json")
    ]
    if not eligible:
        return {
            "status": "requires_component_backtest",
            "samples": 0,
            "message": (
                "Schema 14 persists component points for new backtests; "
                "existing rows must be regenerated before oracle sensitivity "
                "can be computed."
            ),
        }
    forecasts = {
        "model": [],
        "actual_appearance": [],
        "actual_appearance_and_minutes": [],
        "actual_team_goals": [],
        "actual_player_goals_and_assists": [],
    }
    latent_samples = 0
    for row in eligible:
        components = json.loads(str(row["component_points_json"]))
        model = float(row["model_expected_points"])
        appearance_probability = float(row["appearance_probability"])
        actual_minutes = int(row["actual_minutes"])
        expected_minutes = float(row["expected_minutes"])
        if actual_minutes == 0 or appearance_probability <= 0:
            appearance_oracle = 0.0
        else:
            appearance_oracle = model / appearance_probability
        minute_oracle = _minutes_oracle(
            components,
            actual_minutes=actual_minutes,
            expected_minutes=expected_minutes,
            sixty_probability=float(row["sixty_probability"]),
        )
        forecasts["model"].append((model, float(row["actual_points"])))
        forecasts["actual_appearance"].append(
            (appearance_oracle, float(row["actual_points"]))
        )
        forecasts["actual_appearance_and_minutes"].append(
            (minute_oracle, float(row["actual_points"]))
        )
        latent = components.get("_latent_expectations")
        if (
            isinstance(latent, dict)
            and float(latent.get("team_expected_goals", 0.0)) > 0
            and row.get("_actual_team_goals") is not None
        ):
            latent_samples += 1
            team_lambda = float(latent["team_expected_goals"])
            scoring_components = (
                float(components["goal"]) + float(components["assist"])
            )
            team_oracle = (
                model
                - scoring_components
                + scoring_components
                * float(row["_actual_team_goals"])
                / team_lambda
            )
            player_event_oracle = (
                model
                - scoring_components
                + float(row["_actual_goals"])
                * float(components["_goal_rule"])
                + float(row["_actual_assists"])
                * float(components["_assist_rule"])
            )
            forecasts["actual_team_goals"].append(
                (team_oracle, float(row["actual_points"]))
            )
            forecasts["actual_player_goals_and_assists"].append(
                (player_event_oracle, float(row["actual_points"]))
            )
    return {
        "status": "available",
        "samples": len(eligible),
        "excluded_multi_fixture_or_legacy_rows": len(rows) - len(eligible),
        "metrics": {
            name: _forecast_pairs_summary(values)
            for name, values in forecasts.items()
            if values
        },
        "latent_component_samples": latent_samples,
        "interpretation": [
            "actual_appearance conditions the existing forecast on whether "
            "the player appeared.",
            "actual_appearance_and_minutes also substitutes realised minutes "
            "into linear components and the clean-sheet 60-minute gate.",
            "actual_team_goals rescales projected goal and assist components "
            "by realised team goals where team-share latent expectations exist.",
            "actual_player_goals_and_assists replaces only the player's "
            "scoring-event components with realised events.",
            "These interacting counterfactuals are sensitivity bounds, not "
            "additive attribution.",
        ],
    }


def _attach_actual_event_outcomes(
    database: HistoricalDatabase,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    player_ids = tuple(
        sorted({int(row["player_season_id"]) for row in rows})
    )
    placeholders = ",".join("?" for _ in player_ids)
    outcomes = database.connection.execute(
        f"""
        SELECT stats.player_season_id,
               gameweeks.number AS gameweek_number,
               SUM(stats.goals) AS actual_goals,
               SUM(stats.assists) AS actual_assists,
               SUM(
                   CASE
                       WHEN player_seasons.team_id = fixtures.home_team_id
                       THEN fixtures.home_score
                       ELSE fixtures.away_score
                   END
               ) AS actual_team_goals
        FROM player_fixture_stats stats
        JOIN player_seasons
          ON player_seasons.id = stats.player_season_id
        JOIN fixtures ON fixtures.id = stats.fixture_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE stats.player_season_id IN ({placeholders})
          AND fixtures.home_score IS NOT NULL
          AND fixtures.away_score IS NOT NULL
        GROUP BY stats.player_season_id, gameweeks.number
        """,
        player_ids,
    ).fetchall()
    lookup = {
        (int(row["player_season_id"]), int(row["gameweek_number"])): row
        for row in outcomes
    }
    for row in rows:
        outcome = lookup.get(
            (
                int(row["player_season_id"]),
                int(row["target_gameweek"]),
            )
        )
        if outcome is None:
            continue
        row["_actual_goals"] = int(outcome["actual_goals"])
        row["_actual_assists"] = int(outcome["actual_assists"])
        row["_actual_team_goals"] = int(outcome["actual_team_goals"])


def _minutes_oracle(
    components: dict[str, float],
    *,
    actual_minutes: int,
    expected_minutes: float,
    sixty_probability: float,
) -> float:
    if actual_minutes <= 0:
        return 0.0
    under_rule = float(components["_appearance_under_60_rule"])
    over_rule = float(components["_appearance_60_or_more_rule"])
    appearance = over_rule if actual_minutes >= 60 else under_rule
    linear_names = (
        "goal",
        "assist",
        "save",
        "defensive_contribution",
        "bonus",
        "deduction",
    )
    linear = (
        0.0
        if expected_minutes <= 0
        else sum(float(components[name]) for name in linear_names)
        * actual_minutes
        / expected_minutes
    )
    clean_sheet = (
        float(components["clean_sheet"]) / sixty_probability
        if actual_minutes >= 60 and sixty_probability > 0
        else 0.0
    )
    return appearance + linear + clean_sheet


def _forecast_pairs_summary(
    values: list[tuple[float, float]],
) -> dict[str, float | int]:
    errors = [actual - predicted for predicted, actual in values]
    return {
        "samples": len(values),
        "rmse": round(
            math.sqrt(statistics.fmean(error**2 for error in errors)),
            4,
        ),
        "mae": round(statistics.fmean(abs(error) for error in errors), 4),
        "bias_actual_minus_predicted": round(
            statistics.fmean(errors),
            4,
        ),
    }


def _forecast_summary(
    rows: list[dict[str, Any]],
    prediction_name: str,
) -> dict[str, Any]:
    errors = [
        float(row["actual_points"]) - float(row[prediction_name])
        for row in rows
    ]
    return {
        "samples": len(rows),
        "mean_predicted": round(
            statistics.fmean(float(row[prediction_name]) for row in rows),
            4,
        ),
        "mean_actual": round(
            statistics.fmean(float(row["actual_points"]) for row in rows),
            4,
        ),
        "rmse": round(
            math.sqrt(statistics.fmean(error**2 for error in errors)),
            4,
        ),
        "mae": round(statistics.fmean(abs(error) for error in errors), 4),
        "bias_actual_minus_predicted": round(
            statistics.fmean(errors),
            4,
        ),
    }


def _rmse(rows: list[dict[str, Any]], prediction_name: str) -> float:
    return math.sqrt(
        statistics.fmean(
            (
                float(row["actual_points"])
                - float(row[prediction_name])
            )
            ** 2
            for row in rows
        )
    )


def _minutes_band(minutes: float) -> str:
    if minutes <= 0:
        return "0"
    if minutes < 30:
        return "0_to_29"
    if minutes < 60:
        return "30_to_59"
    if minutes < 75:
        return "60_to_74"
    return "75_plus"


def _season_phase(gameweek: int) -> str:
    if gameweek <= 12:
        return "early"
    if gameweek <= 25:
        return "middle"
    return "late"


def _quantile(sorted_values: list[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = probability * (len(sorted_values) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[lower]
    weight = index - lower
    return (
        sorted_values[lower] * (1.0 - weight)
        + sorted_values[upper] * weight
    )
