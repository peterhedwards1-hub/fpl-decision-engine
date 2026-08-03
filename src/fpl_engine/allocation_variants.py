"""Separate the team-strength change from the player-allocation change.

The opponent-adjusted candidate changes two things at once: how much a club is
expected to score, and how that expectation is handed to individual players.
A squad comparison against the incumbent therefore cannot say which of the two
moved the squad, and FPL points depend heavily on the second.

Four variants isolate it:

    A  existing team strength       existing rate allocation   (the incumbent)
    B  opponent-adjusted strength   existing rate allocation
    C  existing team strength       share allocation
    D  opponent-adjusted strength   share allocation           (the candidate)

**B is structurally unsound and is measured anyway.** The rate path multiplies
a player's historical per-90 rate — which already embeds the strength of the
club they earned it at — by that club's strength multiplier. Feeding it a
better team rating makes the double-count larger, not smaller. It is included
because "we could not run it" and "we ran it and it is worse" are different
claims, and only the second is evidence. Read B against A as the cost of the
double-count, and C against D as the marginal contribution of opponent
adjustment inside the coherent route.

Every variant is scored on the same origins with the same rules, through the
same backtester and regret evaluators the promotion gate uses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .backtest import ProjectionBacktester, load_backtest_report
from .config import SeasonRules
from .evaluation import (
    evaluate_legal_squad_regret,
    evaluate_owned_captain_regret,
    evaluate_transfer_regret,
)
from .history.database import HistoricalDatabase
from .projections import (
    CORRECTED_V4_MODEL_CONFIG,
    OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
    ProjectionModelConfig,
)

#: Each variant, and what makes it what it is.
ALLOCATION_VARIANTS: dict[str, dict[str, Any]] = {
    "A_existing_strength_rate_allocation": {
        "config": CORRECTED_V4_MODEL_CONFIG,
        "team_strength": "existing",
        "allocation": "player_rate",
        "sound": True,
        "note": "The production incumbent.",
    },
    "B_opponent_adjusted_rate_allocation": {
        "config": replace(
            CORRECTED_V4_MODEL_CONFIG, team_strength_model="opponent_adjusted"
        ),
        "team_strength": "opponent_adjusted",
        "allocation": "player_rate",
        "sound": False,
        "note": (
            "Structurally unsound: the rate path multiplies a club-influenced "
            "per-90 rate by club strength again, so a better team rating "
            "enlarges the double-count. Measured, not recommended."
        ),
    },
    "C_existing_strength_share_allocation": {
        "config": replace(
            CORRECTED_V4_MODEL_CONFIG,
            scoring_event_source="team_share_expected",
            cold_start_prior="position_price",
        ),
        "team_strength": "existing",
        "allocation": "team_share",
        "sound": True,
        "note": (
            "The coherent allocation on the existing decayed expected-goal "
            "team strength, which has no opponent adjustment and no preseason "
            "prior. The control for D."
        ),
    },
    "D_opponent_adjusted_share_allocation": {
        "config": OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
        "team_strength": "opponent_adjusted",
        "allocation": "team_share",
        "sound": True,
        "note": "The candidate.",
    },
}

#: The comparisons worth reading, and what each one isolates.
VARIANT_CONTRASTS: tuple[tuple[str, str, str], ...] = (
    (
        "D_opponent_adjusted_share_allocation",
        "C_existing_strength_share_allocation",
        "The marginal contribution of opponent adjustment, with allocation "
        "held fixed at the coherent share route. This is the contrast that "
        "answers whether the team-strength model earns its place.",
    ),
    (
        "C_existing_strength_share_allocation",
        "A_existing_strength_rate_allocation",
        "The marginal contribution of share allocation, with team strength "
        "held fixed.",
    ),
    (
        "D_opponent_adjusted_share_allocation",
        "A_existing_strength_rate_allocation",
        "The combined candidate against the production incumbent. Not "
        "attributable to either component on its own.",
    ),
    (
        "B_opponent_adjusted_rate_allocation",
        "A_existing_strength_rate_allocation",
        "The cost of feeding a better team rating into the double-counting "
        "rate path. Expected to be worse, and reported so that claim rests on "
        "a measurement.",
    ),
)


def evaluate_allocation_variants(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    origin_gameweek_start: int = 2,
    origin_gameweek_end: int = 38,
    horizon_gameweeks: int = 1,
    variants: dict[str, dict[str, Any]] | None = None,
    max_transfers_per_week: int = 1,
    include_transfer_regret: bool = True,
) -> dict[str, Any]:
    """Run every variant over identical origins and score the same measures."""

    chosen = variants or ALLOCATION_VARIANTS
    results: dict[str, Any] = {}
    for name, spec in chosen.items():
        config: ProjectionModelConfig = spec["config"]
        report = ProjectionBacktester(
            database,
            rules,
            config=config,
            model_version=f"variant-{name}",
        ).run(
            season_code=season_code,
            origin_gameweek_start=origin_gameweek_start,
            origin_gameweek_end=origin_gameweek_end,
            horizon_gameweeks=horizon_gameweeks,
        )
        results[name] = {
            **{
                key: value
                for key, value in spec.items()
                if key != "config"
            },
            "backtest_run_id": report.backtest_run_id,
            "configuration": {
                "team_strength_model": config.team_strength_model,
                "scoring_event_source": config.scoring_event_source,
                "team_strength_carry_forward": (
                    config.team_strength_carry_forward
                ),
                "cold_start_prior": config.cold_start_prior,
            },
            **_score_run(
                database,
                rules,
                run_id=report.backtest_run_id,
                max_transfers_per_week=max_transfers_per_week,
                include_transfer_regret=include_transfer_regret,
            ),
        }
    return {
        "season_code": season_code,
        "origin_gameweek_start": origin_gameweek_start,
        "origin_gameweek_end": origin_gameweek_end,
        "horizon_gameweeks": horizon_gameweeks,
        "generated_at": datetime.now(UTC).isoformat(),
        "variants": results,
        "contrasts": [
            {
                "treatment": treatment,
                "control": control,
                "isolates": isolates,
                "differences": _difference(
                    results.get(treatment), results.get(control)
                ),
            }
            for treatment, control, isolates in VARIANT_CONTRASTS
            if treatment in results and control in results
        ],
        "limitations": (
            "Variant B is structurally unsound and is reported so the claim "
            "rests on a measurement rather than an assertion. Do not read it "
            "as a candidate.",
            "Player-points RMSE and bias are computed over every projected "
            "player-Gameweek; top-player calibration over the model's own "
            "highest-ranked players, which is the population a manager "
            "actually picks from.",
            "Regret measures replay one squad forward under a single transfer "
            "policy. They are sensitive to that policy, so read the direction "
            "of a difference rather than its exact size.",
            "Historical seasons are design evidence only. Forward 2026/27 "
            "captures are the qualification.",
        ),
    }


def _score_run(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    run_id: int,
    max_transfers_per_week: int,
    include_transfer_regret: bool,
) -> dict[str, Any]:
    report = load_backtest_report(database, run_id)
    scores: dict[str, Any] = {
        "player_points": {
            "observations": report.prediction_count,
            "rmse": round(report.overall.points_rmse, 4),
            "mae": round(report.overall.points_mae, 4),
            "bias": round(report.overall.points_bias, 4),
        },
        # The population a manager actually picks from: the model's own
        # highest-ranked players, not the whole pool.
        "top_player_calibration": {
            str(metric.value): {
                "rmse": round(metric.points_rmse, 4),
                "mae": round(metric.points_mae, 4),
                "bias": round(metric.points_bias, 4),
                "observations": metric.samples,
            }
            for metric in report.top_n
        },
    }
    squad = evaluate_legal_squad_regret(database, run_id, rules, methods=("model",))
    scores["legal_squad_regret"] = {
        "mean": round(
            sum(origin.regret for origin in squad.origins) / len(squad.origins), 4
        )
        if squad.origins
        else None,
        "origins": len(squad.origins),
        "realised_points": round(
            sum(origin.realised_points for origin in squad.origins), 2
        ),
    }
    captain = evaluate_owned_captain_regret(database, run_id, rules)
    scores["owned_captain_regret"] = {
        "mean": round(captain.mean_regret, 4),
        "total": round(captain.total_regret, 4),
        "samples": captain.samples,
        "gameweeks": len(captain.gameweeks),
    }
    if include_transfer_regret:
        transfer = evaluate_transfer_regret(
            database,
            run_id,
            rules,
            max_transfers_per_week=max_transfers_per_week,
        )
        scores["transfer_regret"] = {
            "mean": round(transfer.same_state_mean_regret, 4),
            "total": round(transfer.same_state_total_regret, 4),
            "decisions": transfer.decisions,
            "continuous_policy_points": round(
                transfer.continuous_policy_points, 2
            ),
            "continuous_hindsight_points": round(
                transfer.continuous_hindsight_points, 2
            ),
        }
    return scores


def _difference(
    treatment: dict[str, Any] | None,
    control: dict[str, Any] | None,
) -> dict[str, Any]:
    """Treatment minus control, so a negative error or regret is an improvement."""

    if treatment is None or control is None:
        return {}
    differences: dict[str, Any] = {}
    for section, keys in (
        ("player_points", ("rmse", "mae", "bias")),
        ("legal_squad_regret", ("mean",)),
        ("owned_captain_regret", ("mean",)),
        ("transfer_regret", ("mean", "continuous_policy_points")),
    ):
        for key in keys:
            first = (treatment.get(section) or {}).get(key)
            second = (control.get(section) or {}).get(key)
            if first is None or second is None:
                continue
            differences[f"{section}_{key}"] = round(first - second, 4)
    for label in set(treatment.get("top_player_calibration", {})) & set(
        control.get("top_player_calibration", {})
    ):
        differences[f"top_{label}_mae"] = round(
            treatment["top_player_calibration"][label]["mae"]
            - control["top_player_calibration"][label]["mae"],
            4,
        )
        differences[f"top_{label}_bias"] = round(
            treatment["top_player_calibration"][label]["bias"]
            - control["top_player_calibration"][label]["bias"],
            4,
        )
    return differences
