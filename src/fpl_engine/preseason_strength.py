"""Is a regressed previous season better than pretending every club is equal?

Before a ball is kicked the production team-strength path has nothing in the
target season to read, so every club shrinks to the same league average.
Manchester City, Bournemouth and a newly promoted side come out identical apart
from venue, and an opening-squad optimiser handed those beliefs will happily
buy a Bournemouth defender for a trip to the Etihad because, as far as the
model is concerned, that is a neutral fixture.

The alternative measured here is the smallest one that could work: carry the
previous season's raw goals for and against across by club name, regressed
toward the league average, and give promoted clubs a declared conservative
prior. That is one modelling change. Nothing else moves — same player rates,
same minutes model, same scoring source, same defensive-contribution model,
same optimiser, same horizon.

The two are separated by exactly one field:

    flat            team_strength_carry_forward = False
    carry_forward   team_strength_carry_forward = True

Both are `raw_goals`. Both are the corrected-v4 incumbent otherwise. The
existing opponent-adjusted challenger is scored alongside as a reference and
is deliberately not eligible for the preseason production switch: it changes
three things at once and answering that question is not this module's job.

Scoring is point-in-time safe by construction. Team strength is estimated once
at the GW1 origin, before any target-season fixture has been played, and then
held fixed across GW1-GW8 — which is what a preseason forecast actually is.
Re-estimating each week would score a different model from the one a manager
would have used at the deadline.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backtest import ProjectionBacktester, load_backtest_report
from .config import SeasonRules, load_season_rules
from .evaluation import (
    SquadConstructionPolicy,
    evaluate_legal_squad_regret,
    evaluate_owned_captain_regret,
    evaluate_squad_construction_policies,
)
from .history.database import HistoricalDatabase
from .optimisation import (
    DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    CandidatePlayer,
    mean_appearance,
    optimise_opening_squads,
)
from .projections import (
    CORRECTED_V4_MODEL_CONFIG,
    MODEL_VERSION,
    OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
    ProjectionModelConfig,
    RatesProjectionModel,
)
from .reviewed_modifiers import apply_reviewed_modifiers
from .squad_comparison import opening_candidates, value_squad_under

#: The window a preseason decision actually lives or dies in. An opening squad
#: is normally held roughly this long before the first meaningful rebuild.
EARLY_SEASON_GAMEWEEKS = 8

#: The control. The production incumbent, with the flat preseason prior.
FLAT_PRESEASON_CONFIG = CORRECTED_V4_MODEL_CONFIG

#: The candidate. Exactly one field differs from the control.
CARRY_FORWARD_PRESEASON_CONFIG = replace(
    CORRECTED_V4_MODEL_CONFIG, team_strength_carry_forward=True
)

#: The model version a passing candidate's live preseason run is labelled with.
#: Deliberately outside the incumbent family the in-season selector accepts, so
#: an in-season decision cannot pick it up by being the newest run.
PRESEASON_CARRY_FORWARD_MODEL_VERSION = f"{MODEL_VERSION}-preseason-carry-forward"

#: Labels used throughout the artifact. `opponent_adjusted` is a reference
#: only: it is never selected for production by this module.
FLAT_LABEL = "flat"
CARRY_FORWARD_LABEL = "carry_forward"
OPPONENT_ADJUSTED_LABEL = "opponent_adjusted"

COMPARED_MODELS: dict[str, ProjectionModelConfig] = {
    FLAT_LABEL: FLAT_PRESEASON_CONFIG,
    CARRY_FORWARD_LABEL: CARRY_FORWARD_PRESEASON_CONFIG,
}

#: Scored when asked for, reported, and never promoted from here.
REFERENCE_MODELS: dict[str, ProjectionModelConfig] = {
    OPPONENT_ADJUSTED_LABEL: OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
}

#: What "effectively neutral" means on the decision measure, declared before
#: any decision-level result was read: the candidate may give up this many
#: realised GW1-GW8 points per historical season and still count as neutral.
#: Half a point over eight Gameweeks is inside the noise of a single autosub.
NEUTRAL_REALISED_POINTS_TOLERANCE = 0.5

#: How much a clean-sheet Brier score may worsen before criterion 2 fails.
#: Brier on a roughly 0.25-base-rate event moves by about this much between
#: genuinely different models, so anything smaller is not a material change.
MATERIAL_BRIER_TOLERANCE = 0.005

#: A bias this large means the model expects a goal every three fixtures that
#: nobody scores, which is a calibration defect rather than a trade-off.
SEVERE_BIAS_THRESHOLD = 0.35

#: The declared regression settings a bounded sensitivity check may use. Not a
#: search space: three declared values, chosen only on historical evidence.
ROBUSTNESS_REGRESSION_MATCHES: tuple[float, ...] = (8.0, 12.0, 16.0)

#: Preseason appearance probabilities are optimistic — nobody is injured in
#: August yet — so a cap is a plausible perturbation, not a tuning knob.
PRESEASON_APPEARANCE_CAP = 0.95

#: A previous season below this share of a full fixture list cannot support a
#: carry-forward prior, and a target season below this many completed early
#: fixtures cannot score one.
MINIMUM_PREVIOUS_SEASON_COMPLETION = 0.90
MINIMUM_TARGET_EARLY_FIXTURES = 20


# --------------------------------------------------------------------------
# Transition discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonTransition:
    """One previous-season-to-target-season pair, and whether it can be used."""

    previous_season: str
    target_season: str
    previous_finished_fixtures: int
    previous_expected_fixtures: int
    target_early_fixtures: int
    usable: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_season_transitions(
    database: HistoricalDatabase,
    *,
    early_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    exclude_seasons: tuple[str, ...] = (),
) -> tuple[SeasonTransition, ...]:
    """Every consecutive season pair in the database, with a usability verdict.

    A transition is usable when the previous season is complete enough for a
    prior to mean anything and the target season has enough finished early
    fixtures to score one. Both thresholds are declared above. Unusable
    transitions are returned rather than dropped, because "we excluded it" and
    "it was never there" are different claims.
    """

    codes = [
        str(row["code"])
        for row in database.connection.execute(
            "SELECT code FROM seasons ORDER BY code"
        )
    ]
    transitions: list[SeasonTransition] = []
    for previous, target in zip(codes, codes[1:], strict=False):
        if target in exclude_seasons:
            continue
        previous_finished, previous_total = _fixture_counts(database, previous)
        target_early = _early_finished_fixtures(database, target, early_gameweeks)
        reasons = []
        if previous_total <= 0:
            reasons.append(f"{previous} has no fixtures recorded")
        elif previous_finished < previous_total * MINIMUM_PREVIOUS_SEASON_COMPLETION:
            reasons.append(
                f"{previous} finished only {previous_finished} of "
                f"{previous_total} fixtures, below the "
                f"{MINIMUM_PREVIOUS_SEASON_COMPLETION:.0%} completion the "
                "carry-forward prior needs"
            )
        if target_early < MINIMUM_TARGET_EARLY_FIXTURES:
            reasons.append(
                f"{target} has only {target_early} finished fixtures in "
                f"GW1-GW{early_gameweeks}, below the "
                f"{MINIMUM_TARGET_EARLY_FIXTURES} needed to score a preseason "
                "forecast"
            )
        transitions.append(
            SeasonTransition(
                previous_season=previous,
                target_season=target,
                previous_finished_fixtures=previous_finished,
                previous_expected_fixtures=previous_total,
                target_early_fixtures=target_early,
                usable=not reasons,
                reason="; ".join(reasons) or "Usable",
            )
        )
    return tuple(transitions)


def _fixture_counts(
    database: HistoricalDatabase, season_code: str
) -> tuple[int, int]:
    row = database.connection.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(
                   CASE WHEN fixtures.finished = 1
                             AND fixtures.home_score IS NOT NULL
                             AND fixtures.away_score IS NOT NULL
                        THEN 1 ELSE 0 END
               ) AS finished
        FROM fixtures
        JOIN seasons ON seasons.id = fixtures.season_id
        WHERE seasons.code = ?
        """,
        (season_code,),
    ).fetchone()
    return int(row["finished"] or 0), int(row["total"] or 0)


def _early_finished_fixtures(
    database: HistoricalDatabase, season_code: str, early_gameweeks: int
) -> int:
    row = database.connection.execute(
        """
        SELECT COUNT(*) AS finished
        FROM fixtures
        JOIN seasons ON seasons.id = fixtures.season_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE seasons.code = ?
          AND gameweeks.number <= ?
          AND fixtures.finished = 1
          AND fixtures.home_score IS NOT NULL
          AND fixtures.away_score IS NOT NULL
        """,
        (season_code, early_gameweeks),
    ).fetchone()
    return int(row["finished"] or 0)


# --------------------------------------------------------------------------
# Team-level forecast accuracy
# --------------------------------------------------------------------------


def evaluate_team_goal_forecasts(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    config: ProjectionModelConfig,
    early_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    include_full_season: bool = True,
) -> dict[str, Any]:
    """Score one model's GW1-origin fixture expectations against the results.

    The rating is estimated once, at GW1, and applied unchanged to every
    target Gameweek. That is the preseason question. Full-season numbers use
    the same frozen GW1 beliefs and are reported separately, because a
    preseason rating is not supposed to still be the best available view in
    March and reading the two together would hide that.
    """

    model = RatesProjectionModel(database, rules, config=config)
    promoted = _promoted_team_ids(database, season_code)
    rows: list[dict[str, Any]] = []
    last = 38 if include_full_season else early_gameweeks
    for target in range(1, last + 1):
        for fixture in model.fixture_expected_goals(
            season_code=season_code, gameweek_number=1, target_gameweek=target
        ):
            if fixture["home_score"] is None or fixture["away_score"] is None:
                continue
            for team, venue, expected, actual in (
                (
                    str(fixture["home_team_id"]),
                    "home",
                    float(fixture["home_expected_goals"]),
                    float(fixture["home_score"]),
                ),
                (
                    str(fixture["away_team_id"]),
                    "away",
                    float(fixture["away_expected_goals"]),
                    float(fixture["away_score"]),
                ),
            ):
                rows.append(
                    {
                        "expected": expected,
                        "actual": actual,
                        # The opponent's clean sheet is decided by these goals,
                        # so one expectation drives both sides of the ledger.
                        "clean_sheet_probability": math.exp(-expected),
                        "clean_sheet": 1.0 if actual == 0 else 0.0,
                        "gameweek": target,
                        "venue": venue,
                        "promotion": (
                            "promoted" if team in promoted else "established"
                        ),
                    }
                )
    early = [row for row in rows if row["gameweek"] <= early_gameweeks]
    result: dict[str, Any] = {
        "early_season": _team_metrics(early),
        "breakdowns": {
            "venue": {
                venue: _team_metrics([r for r in early if r["venue"] == venue])
                for venue in ("home", "away")
            },
            "promotion": {
                group: _team_metrics(
                    [r for r in early if r["promotion"] == group]
                )
                for group in ("established", "promoted")
            },
            "phase": {
                "gw1_gw4": _team_metrics(
                    [r for r in early if r["gameweek"] <= 4]
                ),
                "gw5_gw8": _team_metrics([r for r in early if r["gameweek"] > 4]),
            },
        },
    }
    if include_full_season:
        result["full_season"] = _team_metrics(rows)
    return result


def _team_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"observations": 0}
    errors = [row["expected"] - row["actual"] for row in rows]
    count = len(errors)
    return {
        "observations": count,
        "goals_rmse": round(math.sqrt(sum(e * e for e in errors) / count), 4),
        "goals_mae": round(sum(abs(e) for e in errors) / count, 4),
        # Positive bias means the model expects more goals than were scored.
        "goals_bias": round(sum(errors) / count, 4),
        "clean_sheet_brier": round(
            sum(
                (row["clean_sheet_probability"] - row["clean_sheet"]) ** 2
                for row in rows
            )
            / count,
            4,
        ),
        "mean_expected": round(sum(r["expected"] for r in rows) / count, 4),
        "mean_actual": round(sum(r["actual"] for r in rows) / count, 4),
    }


def _promoted_team_ids(
    database: HistoricalDatabase, season_code: str
) -> frozenset[str]:
    """Clubs with no same-named entry in the immediately preceding season.

    Name matching is what the carry-forward path itself uses, because the
    source reassigns team numbering every year. Using the same rule here keeps
    the breakdown labels consistent with the model being scored.
    """

    previous = database.connection.execute(
        "SELECT code FROM seasons WHERE code < ? ORDER BY code DESC LIMIT 1",
        (season_code,),
    ).fetchone()
    if previous is None:
        return frozenset()
    prior_names = {
        _normalised(str(row["name"]))
        for row in database.connection.execute(
            """
            SELECT teams.name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (str(previous["code"]),),
        )
    }
    return frozenset(
        str(row["id"])
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
        if _normalised(str(row["name"])) not in prior_names
    )


def _normalised(name: str) -> str:
    return " ".join(name.strip().lower().split())


# --------------------------------------------------------------------------
# Player-level and decision-level accuracy
# --------------------------------------------------------------------------


def evaluate_player_and_decision(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    label: str,
    config: ProjectionModelConfig,
    horizon_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    candidate_pool_size: int = 8,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
) -> dict[str, Any]:
    """Backtest one model from the GW1 origin and score the squad it builds.

    One origin only. A preseason decision is made once, so averaging it with
    thirty in-season origins would answer a different question.
    """

    report = ProjectionBacktester(
        database,
        rules,
        config=config,
        model_version=f"preseason-strength-{label}",
    ).run(
        season_code=season_code,
        origin_gameweek_start=1,
        origin_gameweek_end=1,
        horizon_gameweeks=horizon_gameweeks,
    )
    run_id = report.backtest_run_id
    loaded = load_backtest_report(database, run_id)
    by_position = {metric.value: metric for metric in loaded.by_position}
    top_n = {metric.value: metric for metric in loaded.top_n}

    policy = SquadConstructionPolicy(
        name="preseason_opening",
        minimum_mean_appearance=minimum_mean_appearance,
        candidate_pool_size=candidate_pool_size,
    )
    squads = evaluate_squad_construction_policies(
        database, run_id, rules, (policy,), origin_gameweeks=(1,)
    )
    origin = squads.origins[0]
    regret = evaluate_legal_squad_regret(database, run_id, rules, methods=("model",))
    captain = evaluate_owned_captain_regret(database, run_id, rules)
    return {
        "backtest_run_id": run_id,
        "player_points": {
            "observations": loaded.prediction_count,
            "rmse": round(loaded.overall.points_rmse, 4),
            "mae": round(loaded.overall.points_mae, 4),
            "bias": round(loaded.overall.points_bias, 4),
        },
        # For defenders and goalkeepers the points bias is dominated by clean
        # sheets and goals conceded, which is exactly the channel a team
        # rating moves. It is a position-level points bias, not a decomposed
        # clean-sheet residual, and is labelled as such.
        "clean_sheet_position_bias": {
            position: (
                None
                if position not in by_position
                else {
                    "points_bias": round(by_position[position].points_bias, 4),
                    "points_mae": round(by_position[position].points_mae, 4),
                    "samples": by_position[position].samples,
                }
            )
            for position in ("GK", "DEF")
        },
        "top_player_accuracy": {
            cutoff: (
                None
                if cutoff not in top_n
                else {
                    "rmse": round(top_n[cutoff].points_rmse, 4),
                    "mae": round(top_n[cutoff].points_mae, 4),
                    "bias": round(top_n[cutoff].points_bias, 4),
                    "samples": top_n[cutoff].samples,
                }
            )
            for cutoff in ("15", "50")
        },
        "opening_squad": {
            "status": origin.status,
            "failure_reason": origin.failure_reason,
            "target_gameweeks": list(origin.target_gameweeks),
            "eligible_players": origin.eligible_players,
            "predicted_horizon_points": origin.predicted_horizon_points,
            "realised_points": origin.realised_points,
            "realised_autosub_points": origin.realised_autosub_points,
            "squad_cost_tenths": origin.squad_cost_tenths,
            "selected_player_ids": list(origin.selected_player_ids),
            "bench_player_ids": list(origin.bench_player_ids),
        },
        "squad_regret": {
            "mean": regret.mean_regret_by_method.get("model"),
            "realised_points": round(
                sum(entry.realised_points for entry in regret.origins), 3
            ),
            "hindsight_points": round(
                sum(entry.hindsight_optimal_points for entry in regret.origins), 3
            ),
            "origins": len(regret.origins),
        },
        "captain_regret": {
            "mean": round(captain.mean_regret, 4),
            "total": round(captain.total_regret, 4),
            "samples": captain.samples,
        },
    }


# --------------------------------------------------------------------------
# One transition end to end
# --------------------------------------------------------------------------


def evaluate_transition(
    database: HistoricalDatabase,
    transition: SeasonTransition,
    *,
    rules: SeasonRules | None = None,
    models: dict[str, ProjectionModelConfig] | None = None,
    early_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    candidate_pool_size: int = 8,
    include_decision_metrics: bool = True,
) -> dict[str, Any]:
    """Score every compared model on one target season from its GW1 origin."""

    if not transition.usable:
        raise ValueError(
            f"Transition {transition.previous_season}->"
            f"{transition.target_season} is not usable: {transition.reason}"
        )
    season_rules = rules or load_season_rules(
        Path(f"config/seasons/{transition.target_season}.json")
    )
    chosen = models or COMPARED_MODELS
    results: dict[str, Any] = {}
    for label, config in chosen.items():
        entry: dict[str, Any] = {
            "configuration": _configuration_summary(config),
            "team_goals": evaluate_team_goal_forecasts(
                database,
                season_rules,
                season_code=transition.target_season,
                config=config,
                early_gameweeks=early_gameweeks,
            ),
        }
        if include_decision_metrics:
            entry.update(
                evaluate_player_and_decision(
                    database,
                    season_rules,
                    season_code=transition.target_season,
                    label=f"{transition.target_season}-{label}",
                    config=config,
                    horizon_gameweeks=early_gameweeks,
                    candidate_pool_size=candidate_pool_size,
                )
            )
        results[label] = entry
    return {
        "transition": transition.as_dict(),
        "models": results,
        "differences": _model_differences(results),
        "squad_overlap": _squad_overlap(results),
        "team_strength_separation": {
            label: preseason_strength_separation(
                database,
                season_rules,
                season_code=transition.target_season,
                config=config,
            )
            for label, config in chosen.items()
        },
    }


def _configuration_summary(config: ProjectionModelConfig) -> dict[str, Any]:
    return {
        "team_strength_model": config.team_strength_model,
        "team_strength_carry_forward": config.team_strength_carry_forward,
        "carry_forward_regression_matches": (
            config.carry_forward_regression_matches
        ),
        "promoted_team_attack_multiplier": (
            config.promoted_team_attack_multiplier
        ),
        "promoted_team_defence_multiplier": (
            config.promoted_team_defence_multiplier
        ),
        "home_attack_multiplier": config.home_attack_multiplier,
        "away_attack_multiplier": config.away_attack_multiplier,
        "minimum_team_multiplier": config.minimum_team_multiplier,
        "maximum_team_multiplier": config.maximum_team_multiplier,
        "scoring_event_source": config.scoring_event_source,
        "minutes_model": config.minutes_model,
        "defensive_contribution_model": config.defensive_contribution_model,
        "cold_start_prior": config.cold_start_prior,
    }


def _model_differences(results: dict[str, Any]) -> dict[str, Any]:
    """Candidate minus control, so a negative error is an improvement."""

    control = results.get(FLAT_LABEL)
    candidate = results.get(CARRY_FORWARD_LABEL)
    if control is None or candidate is None:
        return {}
    differences: dict[str, Any] = {}
    for key in ("goals_rmse", "goals_mae", "goals_bias", "clean_sheet_brier"):
        first = candidate["team_goals"]["early_season"].get(key)
        second = control["team_goals"]["early_season"].get(key)
        if first is not None and second is not None:
            differences[f"early_{key}"] = round(first - second, 4)
    for section, keys in (
        ("player_points", ("rmse", "mae", "bias")),
        ("captain_regret", ("mean",)),
    ):
        for key in keys:
            first = (candidate.get(section) or {}).get(key)
            second = (control.get(section) or {}).get(key)
            if first is not None and second is not None:
                differences[f"{section}_{key}"] = round(first - second, 4)
    first = (candidate.get("squad_regret") or {}).get("mean")
    second = (control.get("squad_regret") or {}).get("mean")
    if first is not None and second is not None:
        differences["squad_regret_mean"] = round(first - second, 4)
    first = (candidate.get("opening_squad") or {}).get("realised_points")
    second = (control.get("opening_squad") or {}).get("realised_points")
    if first is not None and second is not None:
        differences["opening_squad_realised_points"] = round(first - second, 3)
    return differences


def _squad_overlap(results: dict[str, Any]) -> dict[str, Any]:
    control = ((results.get(FLAT_LABEL) or {}).get("opening_squad") or {}).get(
        "selected_player_ids"
    )
    candidate = (
        (results.get(CARRY_FORWARD_LABEL) or {}).get("opening_squad") or {}
    ).get("selected_player_ids")
    if not control or not candidate:
        return {}
    control_ids = frozenset(control)
    candidate_ids = frozenset(candidate)
    return {
        "common": sorted(control_ids & candidate_ids),
        "only_flat": sorted(control_ids - candidate_ids),
        "only_carry_forward": sorted(candidate_ids - control_ids),
        "common_count": len(control_ids & candidate_ids),
    }


# --------------------------------------------------------------------------
# The structural defect the whole exercise exists to fix
# --------------------------------------------------------------------------


def preseason_strength_separation(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    config: ProjectionModelConfig,
    season_code: str,
) -> dict[str, Any]:
    """How far apart the strongest and weakest clubs sit before GW1.

    The flat model's defect is not that it is inaccurate. It is that it has no
    opinion at all: every attack multiplier is the same number, so the spread
    is zero and the gap between an established title contender and a promoted
    side is zero. That is measurable directly and does not need an outcome.
    """

    model = RatesProjectionModel(database, rules, config=config)
    strengths = model._team_strengths(season_code, 1, ())
    names = {
        str(row["id"]): str(row["name"])
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
    }
    promoted = _promoted_team_ids(database, season_code)
    attacks = {team_id: float(value["attack"]) for team_id, value in strengths.items()}
    defences = {
        team_id: float(value["defence"]) for team_id, value in strengths.items()
    }
    if not attacks:
        return {"teams": 0}
    established_attack = [
        value for team_id, value in attacks.items() if team_id not in promoted
    ]
    promoted_attack = [
        value for team_id, value in attacks.items() if team_id in promoted
    ]
    ordered = sorted(attacks.items(), key=lambda item: -item[1])
    return {
        "teams": len(attacks),
        "distinct_attack_multipliers": len({round(v, 9) for v in attacks.values()}),
        "attack_spread": round(max(attacks.values()) - min(attacks.values()), 6),
        "defence_spread": round(max(defences.values()) - min(defences.values()), 6),
        "established_mean_attack": (
            round(sum(established_attack) / len(established_attack), 6)
            if established_attack
            else None
        ),
        "promoted_mean_attack": (
            round(sum(promoted_attack) / len(promoted_attack), 6)
            if promoted_attack
            else None
        ),
        "established_minus_promoted_attack": (
            round(
                sum(established_attack) / len(established_attack)
                - sum(promoted_attack) / len(promoted_attack),
                6,
            )
            if established_attack and promoted_attack
            else None
        ),
        "separates_established_from_promoted": bool(
            len({round(v, 9) for v in attacks.values()}) > 1
        ),
        "strongest": [
            {"name": names.get(team_id, team_id), "attack": round(value, 4)}
            for team_id, value in ordered[:5]
        ],
        "weakest": [
            {"name": names.get(team_id, team_id), "attack": round(value, 4)}
            for team_id, value in ordered[-5:]
        ],
    }


# --------------------------------------------------------------------------
# Aggregation and the decision gate
# --------------------------------------------------------------------------


def aggregate_historical_results(
    historical: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pool the per-season numbers the gate reads.

    Team metrics are pooled by observation count, because a season with fewer
    completed early fixtures should carry less weight. Decision metrics are a
    plain mean over seasons, because each season contributes exactly one
    opening-squad decision.
    """

    aggregate: dict[str, Any] = {"seasons": len(historical)}
    for label in (FLAT_LABEL, CARRY_FORWARD_LABEL, OPPONENT_ADJUSTED_LABEL):
        entries = [
            entry["models"][label]
            for entry in historical
            if label in entry.get("models", {})
        ]
        if not entries:
            continue
        weighted: dict[str, float] = {}
        total = 0
        for entry in entries:
            early = entry["team_goals"]["early_season"]
            count = int(early.get("observations", 0))
            if not count:
                continue
            total += count
            for key in ("goals_rmse", "goals_mae", "goals_bias", "clean_sheet_brier"):
                value = early.get(key)
                if value is None:
                    continue
                # RMSE pools as a mean square, not a mean.
                weighted[key] = weighted.get(key, 0.0) + count * (
                    value**2 if key == "goals_rmse" else value
                )
        pooled = {
            key: round(
                math.sqrt(value / total) if key == "goals_rmse" else value / total,
                4,
            )
            for key, value in weighted.items()
        }
        pooled["observations"] = total
        realised = [
            (entry.get("opening_squad") or {}).get("realised_points")
            for entry in entries
        ]
        realised = [value for value in realised if value is not None]
        regrets = [
            (entry.get("squad_regret") or {}).get("mean") for entry in entries
        ]
        regrets = [value for value in regrets if value is not None]
        captains = [
            (entry.get("captain_regret") or {}).get("mean") for entry in entries
        ]
        captains = [value for value in captains if value is not None]
        points = [
            (entry.get("player_points") or {}).get(key)
            for entry in entries
            for key in ("rmse",)
        ]
        points = [value for value in points if value is not None]
        aggregate[label] = {
            "early_team_goals": pooled,
            "mean_opening_squad_realised_points": (
                round(sum(realised) / len(realised), 3) if realised else None
            ),
            "total_opening_squad_realised_points": (
                round(sum(realised), 3) if realised else None
            ),
            "mean_squad_regret": (
                round(sum(regrets) / len(regrets), 4) if regrets else None
            ),
            "mean_captain_regret": (
                round(sum(captains) / len(captains), 4) if captains else None
            ),
            "mean_player_points_rmse": (
                round(sum(points) / len(points), 4) if points else None
            ),
        }
    return aggregate


def apply_decision_gate(
    aggregate: dict[str, Any],
    historical: list[dict[str, Any]],
    *,
    live_separation: dict[str, Any] | None = None,
    neutral_tolerance: float = NEUTRAL_REALISED_POINTS_TOLERANCE,
) -> dict[str, Any]:
    """The six declared criteria, each with the number that decided it.

    Every criterion has to pass. Nothing here is weighted or traded off,
    because a gate that can be argued around is not a gate.
    """

    control = aggregate.get(FLAT_LABEL) or {}
    candidate = aggregate.get(CARRY_FORWARD_LABEL) or {}
    control_goals = control.get("early_team_goals") or {}
    candidate_goals = candidate.get("early_team_goals") or {}

    criteria: list[dict[str, Any]] = []

    rmse_gain = _gap(control_goals.get("goals_rmse"), candidate_goals.get("goals_rmse"))
    mae_gain = _gap(control_goals.get("goals_mae"), candidate_goals.get("goals_mae"))
    criteria.append(
        {
            "criterion": "improves_early_team_goal_error",
            "description": (
                "Aggregate GW1-GW8 team-goal RMSE or MAE improves on the flat "
                "control."
            ),
            "flat_rmse": control_goals.get("goals_rmse"),
            "carry_forward_rmse": candidate_goals.get("goals_rmse"),
            "rmse_improvement": rmse_gain,
            "flat_mae": control_goals.get("goals_mae"),
            "carry_forward_mae": candidate_goals.get("goals_mae"),
            "mae_improvement": mae_gain,
            "passed": bool(
                (rmse_gain is not None and rmse_gain > 0)
                or (mae_gain is not None and mae_gain > 0)
            ),
        }
    )

    brier_change = _difference(
        candidate_goals.get("clean_sheet_brier"),
        control_goals.get("clean_sheet_brier"),
    )
    criteria.append(
        {
            "criterion": "clean_sheet_brier_not_materially_worse",
            "description": (
                "Clean-sheet Brier score does not worsen by more than "
                f"{MATERIAL_BRIER_TOLERANCE}."
            ),
            "flat_brier": control_goals.get("clean_sheet_brier"),
            "carry_forward_brier": candidate_goals.get("clean_sheet_brier"),
            "change": brier_change,
            "tolerance": MATERIAL_BRIER_TOLERANCE,
            "passed": bool(
                brier_change is not None
                and brier_change <= MATERIAL_BRIER_TOLERANCE
            ),
        }
    )

    points_change = _difference(
        candidate.get("mean_opening_squad_realised_points"),
        control.get("mean_opening_squad_realised_points"),
    )
    regret_change = _difference(
        candidate.get("mean_squad_regret"), control.get("mean_squad_regret")
    )
    criteria.append(
        {
            "criterion": "opening_squad_decision_not_worse",
            "description": (
                "Mean realised GW1-GW8 opening-squad points improve, or fall "
                f"by no more than {neutral_tolerance} per season; or squad "
                "regret improves."
            ),
            "flat_mean_realised_points": control.get(
                "mean_opening_squad_realised_points"
            ),
            "carry_forward_mean_realised_points": candidate.get(
                "mean_opening_squad_realised_points"
            ),
            "realised_points_change": points_change,
            "neutral_tolerance": neutral_tolerance,
            "flat_mean_squad_regret": control.get("mean_squad_regret"),
            "carry_forward_mean_squad_regret": candidate.get("mean_squad_regret"),
            "squad_regret_change": regret_change,
            "passed": bool(
                (points_change is not None and points_change >= -neutral_tolerance)
                or (regret_change is not None and regret_change < 0)
            ),
        }
    )

    per_season = [
        {
            "target_season": entry["transition"]["target_season"],
            "early_rmse_improvement": _gap(
                entry["models"][FLAT_LABEL]["team_goals"]["early_season"].get(
                    "goals_rmse"
                ),
                entry["models"][CARRY_FORWARD_LABEL]["team_goals"][
                    "early_season"
                ].get("goals_rmse"),
            ),
            "realised_points_change": (entry.get("differences") or {}).get(
                "opening_squad_realised_points"
            ),
        }
        for entry in historical
        if FLAT_LABEL in entry.get("models", {})
        and CARRY_FORWARD_LABEL in entry.get("models", {})
    ]
    acceptable = [
        season
        for season in per_season
        if (season["early_rmse_improvement"] or 0) > 0
        or (season["realised_points_change"] or 0) >= -neutral_tolerance
    ]
    criteria.append(
        {
            "criterion": "acceptable_across_multiple_transitions",
            "description": (
                "The candidate performs acceptably on more than one usable "
                "season transition."
            ),
            "seasons_evaluated": len(per_season),
            "seasons_acceptable": len(acceptable),
            "per_season": per_season,
            "passed": len(acceptable) > 1,
        }
    )

    separation = live_separation or {}
    control_separation = separation.get(FLAT_LABEL) or {}
    candidate_separation = separation.get(CARRY_FORWARD_LABEL) or {}
    criteria.append(
        {
            "criterion": "separates_established_from_promoted",
            "description": (
                "The candidate gives established and promoted clubs different "
                "preseason strengths, which the flat control structurally "
                "cannot."
            ),
            "flat_distinct_attack_multipliers": control_separation.get(
                "distinct_attack_multipliers"
            ),
            "carry_forward_distinct_attack_multipliers": (
                candidate_separation.get("distinct_attack_multipliers")
            ),
            "carry_forward_established_minus_promoted_attack": (
                candidate_separation.get("established_minus_promoted_attack")
            ),
            "passed": bool(
                candidate_separation.get("separates_established_from_promoted")
                and not control_separation.get(
                    "separates_established_from_promoted", False
                )
            ),
        }
    )

    candidate_bias = candidate_goals.get("goals_bias")
    control_bias = control_goals.get("goals_bias")
    bias_growth = (
        None
        if candidate_bias is None or control_bias is None
        else round(abs(candidate_bias) - abs(control_bias), 4)
    )
    criteria.append(
        {
            "criterion": "no_severe_new_calibration_defect",
            "description": (
                "Absolute goal bias stays below "
                f"{SEVERE_BIAS_THRESHOLD}, and the clean-sheet Brier score "
                "does not worsen materially."
            ),
            "flat_goals_bias": control_bias,
            "carry_forward_goals_bias": candidate_bias,
            "absolute_bias_growth": bias_growth,
            "severe_bias_threshold": SEVERE_BIAS_THRESHOLD,
            "passed": bool(
                candidate_bias is not None
                and abs(candidate_bias) < SEVERE_BIAS_THRESHOLD
                and brier_change is not None
                and brier_change <= MATERIAL_BRIER_TOLERANCE
            ),
        }
    )

    passed = all(entry["passed"] for entry in criteria)
    return {
        "passed": passed,
        "neutral_tolerance": neutral_tolerance,
        "neutral_definition": (
            "The candidate counts as effectively neutral on the decision "
            f"measure when it gives up no more than {neutral_tolerance} "
            "realised points over GW1-GW8 per historical season. This "
            "threshold was declared before any decision-level result was "
            "read."
        ),
        "criteria": criteria,
        "failed_criteria": [
            entry["criterion"] for entry in criteria if not entry["passed"]
        ],
    }


def _gap(control: float | None, candidate: float | None) -> float | None:
    """How much the candidate improves on the control. Positive is better."""

    if control is None or candidate is None:
        return None
    return round(control - candidate, 4)


def _difference(candidate: float | None, control: float | None) -> float | None:
    if control is None or candidate is None:
        return None
    return round(candidate - control, 4)


# --------------------------------------------------------------------------
# Live projection, revised squad and cross-model comparison
# --------------------------------------------------------------------------


def generate_preseason_projection(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    config: ProjectionModelConfig,
    model_version: str = PRESEASON_CARRY_FORWARD_MODEL_VERSION,
    gameweek_number: int = 1,
    horizon_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    generated_at: datetime | None = None,
    apply_modifiers: bool = True,
) -> dict[str, Any]:
    """Persist one live preseason projection run under its own model version.

    Reviewed research modifiers that are currently accepted and unexpired are
    applied; expired and informational-only findings are excluded by
    `active_modifiers`, so nothing here has to decide which is which.
    """

    generated = generated_at or datetime.now(UTC)
    overrides: tuple[Any, ...] = ()
    provenance: tuple[tuple[int, int, str], ...] = ()
    if apply_modifiers:
        overrides, provenance = apply_reviewed_modifiers(
            database,
            season_code=season_code,
            start_gameweek=gameweek_number,
            horizon_gameweeks=horizon_gameweeks,
            now=generated,
        )
    result = RatesProjectionModel(
        database,
        rules,
        config=config,
        model_version=model_version,
    ).project(
        season_code=season_code,
        start_gameweek=gameweek_number,
        horizon_gameweeks=horizon_gameweeks,
        overrides=overrides,
        generated_at=generated,
        persist=True,
    )
    return {
        "projection_run_id": result.projection_run_id,
        "model_version": model_version,
        "season_code": season_code,
        "start_gameweek": gameweek_number,
        "horizon_gameweeks": horizon_gameweeks,
        "generated_at": generated.isoformat(),
        "configuration": _configuration_summary(config),
        "applied_reviewed_modifier_ids": sorted(
            {int(entry[1]) for entry in provenance}
        ),
    }


def build_revised_squad(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    candidate_pool_size: int = 8,
    alternative_count: int = 2,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
) -> Any:
    """Solve the opening squad over a pool of distinct solver-proven squads."""

    eligible = tuple(
        player
        for player in candidates
        if mean_appearance(player) >= minimum_mean_appearance
    )
    return optimise_opening_squads(
        eligible,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
        alternative_count=alternative_count,
        candidate_pool_size=candidate_pool_size,
    )


def squad_as_dict(
    squad: Any,
    *,
    label: str,
    candidates: tuple[CandidatePlayer, ...] = (),
) -> dict[str, Any]:
    """One squad, in the shape the artifact and the app both read."""

    bench_rank = {
        player_id: rank
        for rank, player_id in enumerate(squad.bench_player_ids, start=1)
    }
    by_id = {player.source_player_id: player for player in candidates}
    return {
        "label": label,
        "total_cost_tenths": squad.total_cost_tenths,
        "gameweek_expected_points": round(squad.gameweek_expected_points, 3),
        "lineup_expected_points": round(squad.lineup_expected_points, 3),
        "horizon_expected_points": round(squad.horizon_expected_points, 3),
        "horizon_expected_bench_contribution": round(
            squad.horizon_expected_bench_contribution, 3
        ),
        "decision_value": round(squad.decision_value, 3),
        "captain_id": squad.captain_id,
        "vice_captain_id": squad.vice_captain_id,
        "starting_player_ids": sorted(squad.starting_player_ids),
        "bench_player_ids": list(squad.bench_player_ids),
        "players": [
            {
                "source_player_id": player.source_player_id,
                "web_name": player.web_name,
                "team": player.team_short_name,
                "position": player.position.value,
                "price_tenths": player.price_tenths,
                "horizon_expected_points": round(player.expected_points, 3),
                "gameweek_expected_points": round(
                    player.gameweek_expected_points or 0.0, 3
                ),
                "mean_appearance": round(mean_appearance(player), 4),
                "starts_gameweek": (
                    player.source_player_id in squad.starting_player_ids
                ),
                "bench_rank": bench_rank.get(player.source_player_id),
                "captain": player.source_player_id == squad.captain_id,
                "vice_captain": player.source_player_id == squad.vice_captain_id,
            }
            for player in sorted(
                squad.players,
                key=lambda value: (
                    value.source_player_id not in squad.starting_player_ids,
                    bench_rank.get(value.source_player_id, 0),
                    value.position.value,
                    value.web_name,
                ),
            )
        ],
        "gameweek_plans": [
            {
                "gameweek_number": plan.gameweek_number,
                "starting_player_ids": sorted(plan.starting_player_ids),
                "starting_names": sorted(
                    by_id[player_id].web_name
                    for player_id in plan.starting_player_ids
                    if player_id in by_id
                ),
                "bench_player_ids": list(plan.bench_player_ids),
                "captain_id": plan.captain_id,
                "vice_captain_id": plan.vice_captain_id,
            }
            for plan in squad.gameweek_plans
        ],
        "proof": squad.proof,
    }


def compare_preseason_squads(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    squads: dict[str, frozenset[str]],
    candidate_sets: dict[str, tuple[CandidatePlayer, ...]],
) -> dict[str, Any]:
    """Value every squad under every model's beliefs.

    Two configurations produce expected points on their own scales, so a raw
    number from one cannot be compared with a raw number from the other. What
    can be compared is what a single model thinks of two squads.
    """

    cross: dict[str, dict[str, Any]] = {}
    for holder, holder_candidates in candidate_sets.items():
        cross[holder] = {}
        for owner, squad_ids in squads.items():
            try:
                cross[holder][owner] = round(
                    value_squad_under(squad_ids, holder_candidates, rules), 3
                )
            except ValueError as error:
                # A player one model priced or projected and the other did not.
                # Reporting why is more useful than an absent cell.
                cross[holder][owner] = None
                cross[holder][f"{owner}_note"] = str(error)
    labels = sorted(squads)
    overlap = {}
    for index, first in enumerate(labels):
        for second in labels[index + 1 :]:
            shared = squads[first] & squads[second]
            overlap[f"{first}_vs_{second}"] = {
                "common": sorted(shared),
                "common_count": len(shared),
                f"only_{first}": sorted(squads[first] - shared),
                f"only_{second}": sorted(squads[second] - shared),
            }
    return {
        "season_code": season_code,
        "cross_valuation": cross,
        "overlap": overlap,
        "interpretation": (
            "Read each row: one model's opinion of every squad. Comparing "
            "across rows compares two different scales and means nothing.",
        ),
    }


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def run_robustness_checks(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    base_config: ProjectionModelConfig,
    gameweek: int = 1,
    horizon_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    candidate_pool_size: int = 8,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """A small, bounded, declared set of stress tests. Not a search.

    Four runs: the three declared regression strengths at the declared
    promoted priors, plus the preseason appearance cap toggled on at the
    declared regression strength. Crossing every combination would be twelve
    runs and the broad parameter search this work explicitly rules out.

    Every run is deterministic — same inputs, same solver, no seeded
    randomness — so a rerun reproduces the classification exactly.
    """

    runs: list[dict[str, Any]] = []
    for regression in ROBUSTNESS_REGRESSION_MATCHES:
        for cap in (None, PRESEASON_APPEARANCE_CAP):
            if regression != base_config.carry_forward_regression_matches and cap:
                # Four runs, not twelve. The cap is varied at the declared
                # regression strength; the regression strength is varied
                # without a cap.
                continue
            config = replace(
                base_config, carry_forward_regression_matches=regression
            )
            candidates = opening_candidates(
                database,
                rules,
                config,
                season_code=season_code,
                gameweek=gameweek,
                horizon_gameweeks=horizon_gameweeks,
                generated_at=generated_at,
            )
            if cap is not None:
                candidates = _cap_appearance(candidates, cap)
            recommendation = build_revised_squad(
                candidates,
                rules,
                candidate_pool_size=candidate_pool_size,
                alternative_count=0,
                minimum_mean_appearance=minimum_mean_appearance,
            )
            primary = recommendation.primary
            by_id = {p.source_player_id: p for p in primary.players}
            runs.append(
                {
                    "name": (
                        f"regression_{regression:g}"
                        + ("" if cap is None else f"_appearance_cap_{cap:g}")
                    ),
                    "carry_forward_regression_matches": regression,
                    "appearance_cap": cap,
                    "promoted_team_attack_multiplier": (
                        config.promoted_team_attack_multiplier
                    ),
                    "promoted_team_defence_multiplier": (
                        config.promoted_team_defence_multiplier
                    ),
                    "objective": round(primary.decision_value, 3),
                    "horizon_expected_points": round(
                        primary.horizon_expected_points, 3
                    ),
                    "total_cost_tenths": primary.total_cost_tenths,
                    "captain": (
                        by_id[primary.captain_id].web_name
                        if primary.captain_id in by_id
                        else primary.captain_id
                    ),
                    "captain_id": primary.captain_id,
                    "player_ids": sorted(by_id),
                    "player_names": sorted(
                        f"{p.web_name} ({p.team_short_name})"
                        for p in primary.players
                    ),
                    "starting_player_ids": sorted(primary.starting_player_ids),
                    "team_counts": _team_counts(primary.players),
                }
            )

    memberships = [frozenset(run["player_ids"]) for run in runs]
    everywhere = frozenset.intersection(*memberships) if memberships else frozenset()
    anywhere = frozenset.union(*memberships) if memberships else frozenset()
    counts = {
        player_id: sum(player_id in members for members in memberships)
        for player_id in anywhere
    }
    classification = {
        player_id: (
            "robust"
            if count == len(memberships)
            else "moderate"
            if count > 1
            else "model_sensitive"
        )
        for player_id, count in counts.items()
    }
    objectives = [run["objective"] for run in runs]
    return {
        "runs": runs,
        "core_player_ids": sorted(everywhere),
        "single_run_player_ids": sorted(
            player_id for player_id, count in counts.items() if count == 1
        ),
        "selection_counts": dict(sorted(counts.items())),
        "classification": dict(sorted(classification.items())),
        "captains": sorted({run["captain"] for run in runs}),
        "captaincy_changes": len({run["captain_id"] for run in runs}) - 1,
        "objective_spread": (
            round(max(objectives) - min(objectives), 3) if objectives else None
        ),
        "deterministic": True,
        "note": (
            "Four declared runs, no combinatorial search. A player selected "
            "in every run is robust; in more than one but not all, moderate; "
            "in exactly one, model_sensitive."
        ),
    }


def _cap_appearance(
    candidates: tuple[CandidatePlayer, ...], cap: float
) -> tuple[CandidatePlayer, ...]:
    """Cap appearance and sixty probabilities without touching expected points.

    Deliberately does not rescale points. The cap tests whether a selection
    survives a stricter availability filter, not whether a different scoring
    model would pick differently.
    """

    capped = []
    for player in candidates:
        values = tuple(
            replace(
                value,
                appearance_probability=min(value.appearance_probability, cap),
                sixty_probability=min(value.sixty_probability, cap),
            )
            for value in player.gameweek_values
        )
        capped.append(
            replace(
                player,
                appearance_probability=min(player.appearance_probability, cap),
                gameweek_values=values,
            )
        )
    return tuple(capped)


def _team_counts(players: tuple[CandidatePlayer, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for player in players:
        counts[player.team_short_name] = counts.get(player.team_short_name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


def load_preseason_validation(
    season_code: str,
    *,
    directory: str | Path = "data/models",
) -> dict[str, Any] | None:
    """Read a written validation artifact, or None when there is not one."""

    path = Path(directory) / f"preseason-strength-validation-{season_code}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


# --------------------------------------------------------------------------
# Player-level explanation
# --------------------------------------------------------------------------

#: The three selections the flat model's blindness shows up in most clearly,
#: and which every report on this change is expected to explain by name.
FOCUS_PLAYER_NAMES: tuple[str, ...] = ("Truffert", "O'Shea", "Muñoz")


def _fixture_context(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    gameweek: int,
    config: ProjectionModelConfig,
) -> dict[str, dict[str, Any]]:
    """Each club's opponent, venue and goal expectations in one Gameweek."""

    model = RatesProjectionModel(database, rules, config=config)
    short_names = {
        str(row["id"]): str(row["short_name"])
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.short_name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
    }
    context: dict[str, dict[str, Any]] = {}
    for fixture in model.fixture_expected_goals(
        season_code=season_code, gameweek_number=gameweek
    ):
        home = str(fixture["home_team_id"])
        away = str(fixture["away_team_id"])
        home_lambda = float(fixture["home_expected_goals"])
        away_lambda = float(fixture["away_expected_goals"])
        context[short_names.get(home, home)] = {
            "opponent": short_names.get(away, away),
            "venue": "home",
            "team_expected_goals": round(home_lambda, 4),
            "opponent_expected_goals": round(away_lambda, 4),
            "clean_sheet_probability": round(math.exp(-away_lambda), 4),
        }
        context[short_names.get(away, away)] = {
            "opponent": short_names.get(home, home),
            "venue": "away",
            "team_expected_goals": round(away_lambda, 4),
            "opponent_expected_goals": round(home_lambda, 4),
            "clean_sheet_probability": round(math.exp(-home_lambda), 4),
        }
    return context


def player_component_explanations(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    configs: dict[str, ProjectionModelConfig],
    squads: dict[str, frozenset[str]],
    source_player_ids: frozenset[str],
    gameweek: int = 1,
    horizon_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    generated_at: datetime | None = None,
    prices: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Break every named player's Gameweek-1 points down, model by model.

    Two models can move a player for two very different reasons: the fixture
    got harder, or the squad simply could not afford them any more. Only the
    first is a team-strength finding, so the two are told apart explicitly
    rather than left for a reader to guess.
    """

    per_model: dict[str, dict[str, Any]] = {}
    fixtures: dict[str, dict[str, dict[str, Any]]] = {}
    for label, config in configs.items():
        result = RatesProjectionModel(
            database, rules, config=config, model_version=f"explain-{label}"
        ).project(
            season_code=season_code,
            start_gameweek=gameweek,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated_at or datetime.now(UTC),
            persist=False,
        )
        grouped: dict[str, list[Any]] = {}
        for projection in result.projections:
            grouped.setdefault(projection.source_player_id, []).append(projection)
        per_model[label] = grouped
        fixtures[label] = _fixture_context(
            database,
            rules,
            season_code=season_code,
            gameweek=gameweek,
            config=config,
        )

    explanations: list[dict[str, Any]] = []
    for player_id in sorted(source_player_ids):
        rows: dict[str, Any] = {}
        identity: dict[str, Any] | None = None
        for label, grouped in per_model.items():
            entries = sorted(
                grouped.get(player_id, []), key=lambda row: row.gameweek_number
            )
            if not entries:
                rows[label] = None
                continue
            opening = entries[0]
            if identity is None:
                identity = {
                    "source_player_id": player_id,
                    "web_name": opening.web_name,
                    "club": opening.team_short_name,
                    "position": opening.position.value,
                    "price_tenths": (prices or {}).get(player_id),
                }
            fixture = fixtures[label].get(opening.team_short_name, {})
            rows[label] = {
                "horizon_expected_points": round(
                    sum(entry.expected_points for entry in entries), 3
                ),
                "gameweek_expected_points": round(opening.expected_points, 3),
                "expected_minutes": round(opening.expected_minutes, 2),
                "appearance_probability": round(opening.appearance_probability, 4),
                "sixty_probability": round(opening.sixty_probability, 4),
                "opponent": fixture.get("opponent"),
                "venue": fixture.get("venue"),
                "team_expected_goals": fixture.get("team_expected_goals"),
                "opponent_expected_goals": fixture.get("opponent_expected_goals"),
                "clean_sheet_probability": fixture.get("clean_sheet_probability"),
                "attacking_points": round(
                    opening.goal_points + opening.assist_points, 3
                ),
                "clean_sheet_points": round(opening.clean_sheet_points, 3),
                "defensive_contribution_points": round(
                    opening.defensive_contribution_points, 3
                ),
                "save_points": round(opening.save_points, 3),
                "bonus_points": round(opening.bonus_points, 3),
                "in_squad": player_id in squads.get(label, frozenset()),
            }
        if identity is None:
            continue
        flat = rows.get(FLAT_LABEL)
        carry = rows.get(CARRY_FORWARD_LABEL)
        change = (
            None
            if flat is None or carry is None
            else round(
                carry["horizon_expected_points"] - flat["horizon_expected_points"],
                3,
            )
        )
        membership_changed = (
            None
            if flat is None or carry is None
            else flat["in_squad"] != carry["in_squad"]
        )
        # A player whose own projection barely moved but whose place did was
        # displaced by what the rest of the squad could now afford, not by a
        # new opinion about their club.
        if change is None:
            source = "unknown"
        elif abs(change) >= 0.5:
            source = "team_strength"
        elif membership_changed:
            source = "squad_budget_interaction"
        else:
            source = "unchanged"
        explanations.append(
            {
                **identity,
                "models": rows,
                "horizon_points_change": change,
                "gameweek_points_change": (
                    None
                    if flat is None or carry is None
                    else round(
                        carry["gameweek_expected_points"]
                        - flat["gameweek_expected_points"],
                        3,
                    )
                ),
                "membership_changed": membership_changed,
                "change_attributed_to": source,
            }
        )
    return explanations


def preseason_model_is_validated(
    validation: dict[str, Any] | None, *, season_code: str
) -> bool:
    """Whether an artifact authorises the carry-forward preseason selection."""

    if not validation:
        return False
    if str(validation.get("season_code")) != season_code:
        return False
    gate = (validation.get("validation") or {}).get("decision_gate") or {}
    selected = validation.get("selected_model") or {}
    return bool(
        gate.get("passed")
        and selected.get("label") == CARRY_FORWARD_LABEL
        and selected.get("model_version") == PRESEASON_CARRY_FORWARD_MODEL_VERSION
    )


# --------------------------------------------------------------------------
# The whole thing, in one call
# --------------------------------------------------------------------------


def validate_preseason_strength(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    horizon_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    candidate_pool_size: int = 8,
    gameweek_number: int = 1,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    include_reference_model: bool = True,
    include_decision_metrics: bool = True,
    include_robustness: bool = True,
    generate_live_projection: bool = True,
    generated_at: datetime | None = None,
    neutral_tolerance: float = NEUTRAL_REALISED_POINTS_TOLERANCE,
) -> dict[str, Any]:
    """Discover, evaluate, gate, project, optimise and compare, in that order.

    The order matters. The gate is applied to historical evidence before the
    live projection is generated, so the live run cannot influence the
    decision that authorised it. If the gate fails, the flat control is
    selected instead, the failure is reported, and the squad that comes out is
    explicitly not labelled validated.
    """

    generated = generated_at or datetime.now(UTC)
    warnings: list[str] = []

    transitions = discover_season_transitions(
        database, early_gameweeks=horizon_gameweeks, exclude_seasons=(season_code,)
    )
    usable = [entry for entry in transitions if entry.usable]
    excluded = [entry for entry in transitions if not entry.usable]
    for entry in excluded:
        warnings.append(
            f"Excluded transition {entry.previous_season}->"
            f"{entry.target_season}: {entry.reason}"
        )
    if len(usable) < 2:
        warnings.append(
            f"Only {len(usable)} usable season transition(s) were found. The "
            "decision gate requires more than one, so it cannot pass."
        )

    models = dict(COMPARED_MODELS)
    if include_reference_model:
        models.update(REFERENCE_MODELS)

    historical: list[dict[str, Any]] = []
    for transition in usable:
        historical.append(
            evaluate_transition(
                database,
                transition,
                models=models,
                early_gameweeks=horizon_gameweeks,
                candidate_pool_size=candidate_pool_size,
                include_decision_metrics=include_decision_metrics,
            )
        )
    aggregate = aggregate_historical_results(historical)

    live_separation = {
        label: preseason_strength_separation(
            database, rules, season_code=season_code, config=config
        )
        for label, config in models.items()
    }
    gate = apply_decision_gate(
        aggregate,
        historical,
        live_separation=live_separation,
        neutral_tolerance=neutral_tolerance,
    )
    if not gate["passed"]:
        warnings.append(
            "The decision gate failed on: "
            + ", ".join(gate["failed_criteria"])
            + ". The flat preseason model is retained and the squad below is "
            "a robustness comparison, not a validated recommendation."
        )

    selected_label = CARRY_FORWARD_LABEL if gate["passed"] else FLAT_LABEL
    selected_config = models[selected_label]
    selected = {
        "label": selected_label,
        "model_version": (
            PRESEASON_CARRY_FORWARD_MODEL_VERSION
            if selected_label == CARRY_FORWARD_LABEL
            else MODEL_VERSION
        ),
        "validated": gate["passed"],
        "scope": "preseason opening-squad decision only",
        "configuration": _configuration_summary(selected_config),
        "rationale": (
            "The regressed previous-season carry-forward passed every declared "
            "gate criterion and is used for the GW1 opening-squad decision "
            "only; in-season decisions keep the incumbent selector."
            if gate["passed"]
            else "The candidate failed the declared gate, so the flat "
            "preseason model is retained."
        ),
    }

    # ---- live projection and the squads -------------------------------
    live_projection: dict[str, Any] = {}
    candidate_sets: dict[str, tuple[CandidatePlayer, ...]] = {}
    squads: dict[str, Any] = {}
    revised_squad: dict[str, Any] = {}
    alternatives: list[dict[str, Any]] = []
    flat_comparison: dict[str, Any] = {}
    robustness: dict[str, Any] = {}

    if generate_live_projection:
        live_projection = generate_preseason_projection(
            database,
            rules,
            season_code=season_code,
            config=selected_config,
            model_version=selected["model_version"],
            gameweek_number=gameweek_number,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated,
        )

    for label, config in models.items():
        try:
            candidate_sets[label] = opening_candidates(
                database,
                rules,
                config,
                season_code=season_code,
                gameweek=gameweek_number,
                horizon_gameweeks=horizon_gameweeks,
                generated_at=generated,
            )
        except ValueError as error:
            warnings.append(f"Could not build {label} candidates: {error}")

    recommendations: dict[str, Any] = {}
    for label, candidates in candidate_sets.items():
        recommendations[label] = build_revised_squad(
            candidates,
            rules,
            candidate_pool_size=candidate_pool_size,
            alternative_count=2,
            minimum_mean_appearance=minimum_mean_appearance,
        )
        squads[label] = frozenset(
            player.source_player_id
            for player in recommendations[label].primary.players
        )

    if selected_label in recommendations:
        chosen = recommendations[selected_label]
        revised_squad = {
            **squad_as_dict(
                chosen.primary,
                label=selected_label,
                candidates=candidate_sets[selected_label],
            ),
            "validated": gate["passed"],
            "projection_run_id": live_projection.get("projection_run_id"),
            "objective": chosen.objective,
            "assumptions": list(chosen.assumptions),
            "transfer_triggers": list(chosen.transfer_triggers),
        }
        alternatives = [
            squad_as_dict(
                alternative,
                label=f"{selected_label}_alternative_{index}",
                candidates=candidate_sets[selected_label],
            )
            for index, alternative in enumerate(chosen.alternatives, start=1)
        ]
        for alternative in alternatives:
            alternative["decision_value_gap"] = round(
                revised_squad["decision_value"] - alternative["decision_value"], 3
            )

    if len(squads) > 1:
        flat_comparison = compare_preseason_squads(
            database,
            rules,
            season_code=season_code,
            squads=squads,
            candidate_sets=candidate_sets,
        )
        prices = {
            player.source_player_id: player.price_tenths
            for candidates in candidate_sets.values()
            for player in candidates
        }
        changed = frozenset.union(*squads.values()) - frozenset.intersection(
            *squads.values()
        )
        focus = frozenset(
            player.source_player_id
            for candidates in candidate_sets.values()
            for player in candidates
            if any(name in player.web_name for name in FOCUS_PLAYER_NAMES)
        )
        flat_comparison["changed_players"] = player_component_explanations(
            database,
            rules,
            season_code=season_code,
            configs={
                label: config
                for label, config in models.items()
                if label in candidate_sets
            },
            squads=squads,
            source_player_ids=changed,
            gameweek=gameweek_number,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated,
            prices=prices,
        )
        flat_comparison["focus_players"] = player_component_explanations(
            database,
            rules,
            season_code=season_code,
            configs={
                label: config
                for label, config in models.items()
                if label in candidate_sets
            },
            squads=squads,
            source_player_ids=focus,
            gameweek=gameweek_number,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated,
            prices=prices,
        )
        flat_comparison["squads"] = {
            label: squad_as_dict(
                recommendations[label].primary,
                label=label,
                candidates=candidate_sets[label],
            )
            for label in recommendations
        }
        flat_comparison["team_strength_separation"] = live_separation

    if include_robustness and CARRY_FORWARD_LABEL in candidate_sets:
        robustness = run_robustness_checks(
            database,
            rules,
            season_code=season_code,
            base_config=CARRY_FORWARD_PRESEASON_CONFIG,
            gameweek=gameweek_number,
            horizon_gameweeks=horizon_gameweeks,
            candidate_pool_size=candidate_pool_size,
            minimum_mean_appearance=minimum_mean_appearance,
            generated_at=generated,
        )
        robustness["structural_questions"] = _structural_questions(
            robustness["runs"], candidate_sets.get(CARRY_FORWARD_LABEL, ())
        )

    return {
        "season_code": season_code,
        "generated_at": generated.isoformat(),
        "horizon_gameweeks": horizon_gameweeks,
        "gameweek_number": gameweek_number,
        "candidate_pool_size": candidate_pool_size,
        "minimum_mean_appearance": minimum_mean_appearance,
        "validation": {
            "transitions": [entry.as_dict() for entry in transitions],
            "usable_transitions": [
                f"{entry.previous_season}->{entry.target_season}"
                for entry in usable
            ],
            "excluded_transitions": [
                {
                    "transition": (
                        f"{entry.previous_season}->{entry.target_season}"
                    ),
                    "reason": entry.reason,
                }
                for entry in excluded
            ],
            "compared_models": {
                label: _configuration_summary(config)
                for label, config in models.items()
            },
            "reference_only_models": sorted(REFERENCE_MODELS),
            "aggregate": aggregate,
            "decision_gate": gate,
            "point_in_time_policy": (
                "Team strength is estimated once at the GW1 origin from the "
                "previous season only and held fixed across GW1-GW8. No "
                "target-season outcome, later ingestion run or end-of-season "
                "player record enters a GW1 forecast."
            ),
        },
        "selected_model": selected,
        "historical_results": historical,
        "live_projection": live_projection,
        "revised_squad": revised_squad,
        "alternatives": alternatives,
        "flat_comparison": flat_comparison,
        "robustness": robustness,
        "warnings": warnings,
    }


def _structural_questions(
    runs: list[dict[str, Any]],
    candidates: tuple[CandidatePlayer, ...],
) -> dict[str, Any]:
    """The three named claims this change was expected to move, checked."""

    by_id = {player.source_player_id: player for player in candidates}
    triple_ups = []
    arsenal = []
    truffert_starts = []
    for run in runs:
        counts = run["team_counts"]
        triple_ups.append(max(counts.values()) >= 3 if counts else False)
        arsenal.append(counts.get("ARS", 0) > 0)
        starters = set(run["starting_player_ids"])
        truffert = [
            player_id
            for player_id in run["player_ids"]
            if "Truffert" in by_id.get(player_id, _Missing).web_name
        ]
        truffert_starts.append(
            bool(truffert) and all(pid in starters for pid in truffert)
        )
    return {
        "bournemouth_style_triple_up_survives": {
            "runs_with_a_triple_up": sum(triple_ups),
            "runs": len(runs),
            "survives_everywhere": all(triple_ups) if runs else None,
            "note": (
                "Measured as any club contributing three or more players, "
                "which is the structural pattern, not the specific club."
            ),
        },
        "no_arsenal_survives": {
            "runs_selecting_arsenal": sum(arsenal),
            "runs": len(runs),
            "no_arsenal_everywhere": (not any(arsenal)) if runs else None,
        },
        "truffert_still_starts": {
            "runs_starting_truffert": sum(truffert_starts),
            "runs": len(runs),
            "starts_everywhere": all(truffert_starts) if runs else None,
        },
    }


class _Missing:
    """Stand-in so a name lookup on an absent candidate cannot raise."""

    web_name = ""
    team_short_name = ""


# --------------------------------------------------------------------------
# Human-readable report
# --------------------------------------------------------------------------


def render_preseason_validation_markdown(result: dict[str, Any]) -> str:
    """A short readable summary of the artifact, for someone deciding."""

    validation = result.get("validation") or {}
    gate = validation.get("decision_gate") or {}
    aggregate = validation.get("aggregate") or {}
    selected = result.get("selected_model") or {}
    lines: list[str] = [
        f"# Preseason team strength — {result.get('season_code')}",
        "",
        f"Generated {result.get('generated_at')}. "
        f"Horizon {result.get('horizon_gameweeks')} Gameweeks.",
        "",
        "## Verdict",
        "",
        f"- Decision gate: **{'PASS' if gate.get('passed') else 'FAIL'}**",
        f"- Selected preseason model: **{selected.get('label')}** "
        f"(`{selected.get('model_version')}`)",
        f"- Scope: {selected.get('scope')}",
        f"- Usable transitions: "
        f"{', '.join(validation.get('usable_transitions') or []) or 'none'}",
    ]
    excluded = validation.get("excluded_transitions") or []
    if excluded:
        lines.append("- Excluded transitions:")
        lines.extend(
            f"  - {entry['transition']}: {entry['reason']}" for entry in excluded
        )
    lines += ["", "## Aggregate early-season accuracy (GW1–GW8)", ""]
    lines.append("| model | goals RMSE | goals MAE | goals bias | CS Brier |")
    lines.append("| --- | --- | --- | --- | --- |")
    for label in (FLAT_LABEL, CARRY_FORWARD_LABEL, OPPONENT_ADJUSTED_LABEL):
        entry = (aggregate.get(label) or {}).get("early_team_goals")
        if not entry:
            continue
        lines.append(
            f"| {label} | {entry.get('goals_rmse')} | {entry.get('goals_mae')} "
            f"| {entry.get('goals_bias')} | {entry.get('clean_sheet_brier')} |"
        )
    lines += ["", "## Decision gate", ""]
    lines.append("| criterion | passed | detail |")
    lines.append("| --- | --- | --- |")
    for entry in gate.get("criteria") or []:
        lines.append(
            f"| {entry['criterion']} | {'yes' if entry['passed'] else 'no'} "
            f"| {entry['description']} |"
        )
    lines += ["", gate.get("neutral_definition", ""), ""]

    squad = result.get("revised_squad") or {}
    if squad:
        lines += [
            "## Revised opening squad",
            "",
            f"Cost {squad.get('total_cost_tenths', 0) / 10:.1f}m. "
            f"GW1 expected {squad.get('gameweek_expected_points')}. "
            f"Eight-Gameweek decision value {squad.get('decision_value')}. "
            f"Validated: {squad.get('validated')}.",
            "",
            "| player | club | pos | price | GW1–8 xP | role |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for player in squad.get("players", []):
            role = (
                "XI"
                if player["starts_gameweek"]
                else f"bench {player['bench_rank']}"
            )
            if player["captain"]:
                role += " (C)"
            if player["vice_captain"]:
                role += " (V)"
            lines.append(
                f"| {player['web_name']} | {player['team']} | "
                f"{player['position']} | {player['price_tenths'] / 10:.1f} | "
                f"{player['horizon_expected_points']} | {role} |"
            )
        lines.append("")

    focus = (result.get("flat_comparison") or {}).get("focus_players") or []
    if focus:
        lines += ["## Truffert, O'Shea and Muñoz", ""]
        for entry in focus:
            flat = (entry.get("models") or {}).get(FLAT_LABEL) or {}
            carry = (entry.get("models") or {}).get(CARRY_FORWARD_LABEL) or {}
            lines += [
                f"### {entry['web_name']} ({entry['club']}, {entry['position']})",
                "",
                f"- GW1 fixture: "
                f"{'at home to' if carry.get('venue') == 'home' else 'away at'} "
                f"{carry.get('opponent')}",
                f"- Opponent expected goals: flat "
                f"{flat.get('opponent_expected_goals')} → carry-forward "
                f"{carry.get('opponent_expected_goals')}",
                f"- Clean-sheet probability: flat "
                f"{flat.get('clean_sheet_probability')} → carry-forward "
                f"{carry.get('clean_sheet_probability')}",
                f"- GW1–8 projection: flat "
                f"{flat.get('horizon_expected_points')} → carry-forward "
                f"{carry.get('horizon_expected_points')} "
                f"({entry.get('horizon_points_change')})",
                f"- In squad: flat {flat.get('in_squad')} → carry-forward "
                f"{carry.get('in_squad')}; change attributed to "
                f"{entry.get('change_attributed_to')}",
                "",
            ]

    robustness = result.get("robustness") or {}
    if robustness:
        classification = robustness.get("classification") or {}
        counts: dict[str, int] = {}
        for value in classification.values():
            counts[value] = counts.get(value, 0) + 1
        lines += [
            "## Robustness",
            "",
            f"- Runs: {len(robustness.get('runs') or [])}",
            f"- Objective spread: {robustness.get('objective_spread')}",
            f"- Captaincy changes across runs: "
            f"{robustness.get('captaincy_changes')}",
            f"- Classification: {counts}",
            "",
        ]

    warnings = result.get("warnings") or []
    if warnings:
        lines += ["## Warnings", ""]
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    return "\n".join(lines)


def write_preseason_validation_markdown(
    result: dict[str, Any], output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_preseason_validation_markdown(result), encoding="utf-8")
    return path


def squad_comparison_artifact(result: dict[str, Any]) -> dict[str, Any]:
    """The cross-model squad comparison, extracted as its own document."""

    comparison = result.get("flat_comparison") or {}
    return {
        "season_code": result.get("season_code"),
        "generated_at": result.get("generated_at"),
        "gameweek_number": result.get("gameweek_number"),
        "horizon_gameweeks": result.get("horizon_gameweeks"),
        "selected_model": result.get("selected_model"),
        "projection_run_id": (result.get("live_projection") or {}).get(
            "projection_run_id"
        ),
        "squads": comparison.get("squads", {}),
        "alternatives": result.get("alternatives", []),
        "cross_valuation": comparison.get("cross_valuation", {}),
        "overlap": comparison.get("overlap", {}),
        "changed_players": comparison.get("changed_players", []),
        "focus_players": comparison.get("focus_players", []),
        "team_strength_separation": comparison.get("team_strength_separation", {}),
        "robustness": result.get("robustness", {}),
        "warnings": result.get("warnings", []),
        "interpretation": comparison.get("interpretation", ()),
    }
