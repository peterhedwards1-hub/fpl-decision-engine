"""The last four questions between a validated preseason model and a squad.

The carry-forward preseason team-strength model is already validated and is not
revisited here. What remains are four defects that survive it, and one live run
that turns the result into a team.

**Promoted clubs are still identical to each other.** Carry-forward fixed the
"every club is average" problem for the seventeen clubs with a top-flight
record. The three that came up still share one declared prior, so a side that
scored ninety-seven Championship goals and a side that scraped through the
play-offs are the same club to the model. Section one varies that prior by what
each club actually did in the division it left, and only adopts the change if
promoted-club forecasts improve without costing anything overall.

**Players promoted with their clubs are still nameless.** They have no Premier
League record, so each takes the positional cold-start prior. Previous-division
minutes could separate a forty-six-match starter from a fringe player without
claiming anything about scoring. Section two gates that on coverage.

**Goalkeepers were valued one at a time.** They cannot be: the substitute
automatically replaces a starter who records no minutes, so what a manager owns
is a pair, and the nomination should follow the pair value rather than the
larger standalone projection. Section three makes the pair the unit of both
selection and weekly nomination.

**The squad search was eight candidates wide and spent the budget.** Section
four widens it to forty complete squads, rescores every one exactly, and asks
three specific questions of the result: what a squad that keeps money in the
bank costs, why no Arsenal defender is selected, and which selections survive
single-factor stress.

Nothing here re-tunes the projection engine, and no combination of stress
factors is crossed with another: a factorial sweep over a single season's
opening decision would find whatever it went looking for.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .championship import (
    BASE_PROMOTED_ATTACK,
    BASE_PROMOTED_DEFENCE,
    TESTED_PROMOTED_WEIGHTS,
    championship_coverage,
    cohort_mean_multipliers,
    promoted_club_priors,
)
from .config import SeasonRules
from .history.database import HistoricalDatabase
from .optimisation import (
    DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    CandidatePlayer,
    FullSquadResult,
    OptimisationError,
    mean_appearance,
    optimise_full_squad,
    squad_ranking_key,
)
from .preseason_strength import (
    CARRY_FORWARD_PRESEASON_CONFIG,
    EARLY_SEASON_GAMEWEEKS,
    PRESEASON_CARRY_FORWARD_MODEL_VERSION,
    SeasonTransition,
    _normalised,
    _promoted_team_ids,
    discover_season_transitions,
    evaluate_player_and_decision,
    generate_preseason_projection,
    squad_as_dict,
)
from .projections import ProjectionModelConfig, RatesProjectionModel, TeamStrengthOverride
from .promoted_roles import (
    MINIMUM_ROLE_COVERAGE,
    match_roles,
    role_coverage,
    stored_roles,
)
from .squad_comparison import opening_candidates

#: How many distinct complete legal squads the frontier aims for.
DEFAULT_FRONTIER_SIZE = 40

#: Bank levels the frontier is asked to hold back, in tenths of a million.
BANK_THRESHOLDS_TENTHS: tuple[int, ...] = (0, 5, 10)

#: Two squads within this many expected points over the horizon are treated as
#: interchangeable on the model's evidence. It is roughly the value of a single
#: autosub, and well inside what a projection can resolve. A bank-preserving
#: squad inside the band is reported as flexibility-equivalent; it does not
#: replace the maximum-value squad, because "equivalent within noise" is not
#: "better" and the primary recommendation should not move on a rounding.
FLEXIBILITY_EQUIVALENCE_POINTS = 0.25

#: The club whose absence from the recommended squad has to be explained.
ARSENAL_SHORT_NAME = "ARS"

#: Single-factor stress tests. Run one at a time, never crossed.
CONCENTRATION_ATTACK_SCALE = 0.90

#: The promoted-prior gate. Promoted forecasts must improve on more than one
#: transition, and overall forecasting must not get materially worse.
MINIMUM_IMPROVED_TRANSITIONS = 2
MATERIAL_OVERALL_RMSE_TOLERANCE = 0.01
MATERIAL_BRIER_TOLERANCE = 0.005

FIXED_PROMOTED_LABEL = "fixed"


def differentiated_label(weight: float) -> str:
    return f"championship_relative_w{weight:g}"


def promoted_prior_configs(
    base: ProjectionModelConfig = CARRY_FORWARD_PRESEASON_CONFIG,
    weights: tuple[float, ...] = TESTED_PROMOTED_WEIGHTS,
) -> dict[str, ProjectionModelConfig]:
    """The control and one candidate per tested weight.

    Exactly two fields separate any candidate from the control, and one of
    them is the weight itself, so a difference cannot come from anywhere else.
    """

    configs = {FIXED_PROMOTED_LABEL: base}
    for weight in weights:
        configs[differentiated_label(weight)] = replace(
            base,
            promoted_prior_mode="championship_relative",
            promoted_prior_weight=weight,
        )
    return configs


# --------------------------------------------------------------------------
# 1. Promoted-team forecast accuracy
# --------------------------------------------------------------------------


def _metrics(rows: list[dict[str, float]]) -> dict[str, Any]:
    if not rows:
        return {"observations": 0}
    errors = [row["expected"] - row["actual"] for row in rows]
    count = len(errors)
    return {
        "observations": count,
        "goals_rmse": round(math.sqrt(sum(e * e for e in errors) / count), 4),
        "goals_mae": round(sum(abs(e) for e in errors) / count, 4),
        "goals_bias": round(sum(errors) / count, 4),
        "clean_sheet_brier": round(
            sum(
                (row["clean_sheet_probability"] - row["clean_sheet"]) ** 2
                for row in rows
            )
            / count,
            4,
        ),
    }


def evaluate_promoted_forecasts(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    config: ProjectionModelConfig,
    early_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
) -> dict[str, Any]:
    """Score a model's GW1-origin expectations, promoted clubs called out.

    Three groups are reported, because the promoted prior has two sides and
    they land in different rows. ``promoted_attack`` is goals *scored by* a
    promoted club, which the attack multiplier drives. ``promoted_defence`` is
    goals *scored against* a promoted club, which the defensive-vulnerability
    multiplier drives and which would otherwise be filed under whichever
    established club happened to be playing them. ``promoted_clean_sheet``
    scores the promoted club's own clean-sheet probability.

    Team strength is estimated once, at the GW1 origin, from evidence that
    predates the target season, and held fixed for the whole horizon.
    """

    model = RatesProjectionModel(database, rules, config=config)
    promoted = _promoted_team_ids(database, season_code)
    promoted_attack: list[dict[str, float]] = []
    promoted_defence: list[dict[str, float]] = []
    overall: list[dict[str, float]] = []
    for target in range(1, early_gameweeks + 1):
        for fixture in model.fixture_expected_goals(
            season_code=season_code, gameweek_number=1, target_gameweek=target
        ):
            if fixture["home_score"] is None or fixture["away_score"] is None:
                continue
            home = str(fixture["home_team_id"])
            away = str(fixture["away_team_id"])
            for team, opponent, expected, actual, opponent_expected, opponent_actual in (
                (
                    home,
                    away,
                    float(fixture["home_expected_goals"]),
                    float(fixture["home_score"]),
                    float(fixture["away_expected_goals"]),
                    float(fixture["away_score"]),
                ),
                (
                    away,
                    home,
                    float(fixture["away_expected_goals"]),
                    float(fixture["away_score"]),
                    float(fixture["home_expected_goals"]),
                    float(fixture["home_score"]),
                ),
            ):
                row = {
                    "expected": expected,
                    "actual": actual,
                    # This club's clean sheet is decided by the opponent's
                    # goals, so the probability comes from the other lambda.
                    "clean_sheet_probability": math.exp(-opponent_expected),
                    "clean_sheet": 1.0 if opponent_actual == 0 else 0.0,
                }
                overall.append(row)
                if team in promoted:
                    promoted_attack.append(row)
                if opponent in promoted:
                    promoted_defence.append(row)
    return {
        "overall": _metrics(overall),
        "promoted_attack": _metrics(promoted_attack),
        "promoted_defence": _metrics(promoted_defence),
        "promoted_involved": _metrics(promoted_attack + promoted_defence),
        "promoted_clean_sheet_brier": (
            _metrics(promoted_attack).get("clean_sheet_brier")
        ),
    }


def promoted_prior_summary(
    database: HistoricalDatabase,
    *,
    target_season: str,
    previous_season: str,
    weight: float,
) -> dict[str, Any]:
    """The priors a weight produces for one season's promoted cohort."""

    names = tuple(_promoted_names(database, target_season))
    priors = promoted_club_priors(
        database,
        championship_season_code=previous_season,
        promoted_fpl_names=names,
        weight=weight,
    )
    mean_attack, mean_defence = cohort_mean_multipliers(priors)
    return {
        "target_season": target_season,
        "championship_season": previous_season,
        "weight": weight,
        "clubs": [prior.as_dict() for prior in priors.values()],
        "cohort_mean_attack": None if mean_attack is None else round(mean_attack, 4),
        "cohort_mean_defence": (
            None if mean_defence is None else round(mean_defence, 4)
        ),
        "declared_base_attack": BASE_PROMOTED_ATTACK,
        "declared_base_defence": BASE_PROMOTED_DEFENCE,
        "all_matched": all(prior.matched for prior in priors.values()),
    }


def _promoted_names(
    database: HistoricalDatabase, season_code: str
) -> tuple[str, ...]:
    previous = database.connection.execute(
        "SELECT code FROM seasons WHERE code < ? ORDER BY code DESC LIMIT 1",
        (season_code,),
    ).fetchone()
    if previous is None:
        return ()
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
    return tuple(
        sorted(
            str(row["name"])
            for row in database.connection.execute(
                """
                SELECT teams.name FROM teams
                JOIN seasons ON seasons.id = teams.season_id
                WHERE seasons.code = ?
                """,
                (season_code,),
            )
            if _normalised(str(row["name"])) not in prior_names
        )
    )


def validate_promoted_priors(
    database: HistoricalDatabase,
    transitions: tuple[SeasonTransition, ...],
    *,
    rules_for: dict[str, SeasonRules],
    base_config: ProjectionModelConfig = CARRY_FORWARD_PRESEASON_CONFIG,
    weights: tuple[float, ...] = TESTED_PROMOTED_WEIGHTS,
    early_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
) -> dict[str, Any]:
    """Score every weight on every usable transition, then choose one.

    Selection is leave-one-transition-out and point-in-time safe in both
    directions. For each held-out transition the weight is chosen using only
    the *other* transitions, and every model reads only evidence that predates
    the season it is forecasting. A weight that wins because it was chosen on
    the season it is then scored on has proved nothing, and the held-out
    column is the only column entitled to be read as evidence.
    """

    configs = promoted_prior_configs(base_config, weights)
    per_transition: list[dict[str, Any]] = []
    for transition in transitions:
        entry: dict[str, Any] = {
            "transition": transition.as_dict(),
            "models": {},
            "priors": {},
        }
        for label, config in configs.items():
            entry["models"][label] = evaluate_promoted_forecasts(
                database,
                rules_for[transition.target_season],
                season_code=transition.target_season,
                config=config,
                early_gameweeks=early_gameweeks,
            )
        for weight in weights:
            entry["priors"][differentiated_label(weight)] = promoted_prior_summary(
                database,
                target_season=transition.target_season,
                previous_season=transition.previous_season,
                weight=weight,
            )
        per_transition.append(entry)

    def score(entry: dict[str, Any], label: str) -> float:
        # One number per model per season: promoted-involved goal RMSE. The
        # gate reads the components separately; the selector needs an order.
        # A transition with no promoted club contributes no evidence, and is
        # scored as a tie rather than being silently dropped or crashing.
        metrics = entry["models"][label]["promoted_involved"]
        value = metrics.get("goals_rmse")
        return math.inf if value is None else float(value)

    labels = list(configs)
    held_out: list[dict[str, Any]] = []
    for index, entry in enumerate(per_transition):
        others = [
            other for position, other in enumerate(per_transition) if position != index
        ]
        if not others:
            continue
        means = {
            label: sum(score(other, label) for other in others) / len(others)
            for label in labels
        }
        chosen = min(labels, key=lambda label: (means[label], label))
        held_out.append(
            {
                "target_season": entry["transition"]["target_season"],
                "chosen_on_other_transitions": chosen,
                "training_mean_promoted_rmse": {
                    label: round(value, 4) for label, value in means.items()
                },
                "held_out_promoted_rmse": {
                    label: round(score(entry, label), 4) for label in labels
                },
                "held_out_improvement_over_fixed": round(
                    score(entry, FIXED_PROMOTED_LABEL) - score(entry, chosen), 4
                ),
                "beats_fixed_out_of_sample": (
                    score(entry, chosen) < score(entry, FIXED_PROMOTED_LABEL)
                ),
            }
        )

    pooled = {
        label: _pooled_metrics([entry["models"][label] for entry in per_transition])
        for label in labels
    }
    # The weight to adopt, chosen on the pooled evidence and only used when the
    # gate below passes.
    differentiated_labels = [
        label for label in labels if label != FIXED_PROMOTED_LABEL
    ]

    def pooled_rmse(label: str, group: str = "promoted_involved") -> float:
        value = pooled[label][group].get("goals_rmse")
        return math.inf if value is None else float(value)

    def pooled_value(label: str, group: str, key: str) -> float | None:
        return pooled[label][group].get(key)

    best_label = (
        min(differentiated_labels, key=lambda label: (pooled_rmse(label), label))
        if differentiated_labels
        else FIXED_PROMOTED_LABEL
    )
    promoted_observations = pooled[FIXED_PROMOTED_LABEL]["promoted_involved"][
        "observations"
    ]

    improved = [
        entry["transition"]["target_season"]
        for entry in per_transition
        if score(entry, best_label) < score(entry, FIXED_PROMOTED_LABEL)
    ]
    def improves(group: str, key: str) -> bool:
        candidate = pooled_value(best_label, group, key)
        control = pooled_value(FIXED_PROMOTED_LABEL, group, key)
        return (
            candidate is not None and control is not None and candidate < control
        )

    def not_worse_by(group: str, key: str, tolerance: float) -> bool:
        candidate = pooled_value(best_label, group, key)
        control = pooled_value(FIXED_PROMOTED_LABEL, group, key)
        if candidate is None or control is None:
            return False
        return candidate - control <= tolerance

    criteria = [
        {
            "criterion": "promoted_evidence_exists",
            "description": (
                "At least one usable transition contains a promoted club, so "
                "there is something to differentiate."
            ),
            "promoted_observations": promoted_observations,
            "passed": promoted_observations > 0,
        },
        {
            "criterion": "promoted_goal_error_improves",
            "description": (
                "Pooled promoted-club goal RMSE or MAE improves on the fixed "
                "prior."
            ),
            "fixed_rmse": pooled_value(
                FIXED_PROMOTED_LABEL, "promoted_involved", "goals_rmse"
            ),
            "candidate_rmse": pooled_value(
                best_label, "promoted_involved", "goals_rmse"
            ),
            "fixed_mae": pooled_value(
                FIXED_PROMOTED_LABEL, "promoted_involved", "goals_mae"
            ),
            "candidate_mae": pooled_value(
                best_label, "promoted_involved", "goals_mae"
            ),
            "passed": improves("promoted_involved", "goals_rmse")
            or improves("promoted_involved", "goals_mae"),
        },
        {
            "criterion": "improves_on_multiple_transitions",
            "description": (
                "Promoted-club goal RMSE improves on at least "
                f"{MINIMUM_IMPROVED_TRANSITIONS} usable transitions."
            ),
            "transitions_improved": improved,
            "transitions_evaluated": len(per_transition),
            "passed": len(improved) >= MINIMUM_IMPROVED_TRANSITIONS,
        },
        {
            "criterion": "overall_forecasting_not_materially_worse",
            "description": (
                "Pooled overall team-goal RMSE does not worsen by more than "
                f"{MATERIAL_OVERALL_RMSE_TOLERANCE}, and the clean-sheet Brier "
                f"score by no more than {MATERIAL_BRIER_TOLERANCE}."
            ),
            "fixed_overall_rmse": pooled_value(
                FIXED_PROMOTED_LABEL, "overall", "goals_rmse"
            ),
            "candidate_overall_rmse": pooled_value(
                best_label, "overall", "goals_rmse"
            ),
            "fixed_overall_brier": pooled_value(
                FIXED_PROMOTED_LABEL, "overall", "clean_sheet_brier"
            ),
            "candidate_overall_brier": pooled_value(
                best_label, "overall", "clean_sheet_brier"
            ),
            "passed": not_worse_by(
                "overall", "goals_rmse", MATERIAL_OVERALL_RMSE_TOLERANCE
            )
            and not_worse_by(
                "overall", "clean_sheet_brier", MATERIAL_BRIER_TOLERANCE
            ),
        },
        {
            "criterion": "survives_leave_one_transition_out",
            "description": (
                "A weight chosen without seeing a transition still beats the "
                "fixed prior on it, on more than one transition."
            ),
            "held_out": held_out,
            "held_out_wins": sum(
                1 for entry in held_out if entry["beats_fixed_out_of_sample"]
            ),
            "passed": (
                sum(1 for entry in held_out if entry["beats_fixed_out_of_sample"])
                >= MINIMUM_IMPROVED_TRANSITIONS
            ),
        },
    ]
    passed = all(entry["passed"] for entry in criteria)
    return {
        "compared_models": {
            label: {
                "promoted_prior_mode": config.promoted_prior_mode,
                "promoted_prior_weight": config.promoted_prior_weight,
                "promoted_team_attack_multiplier": (
                    config.promoted_team_attack_multiplier
                ),
                "promoted_team_defence_multiplier": (
                    config.promoted_team_defence_multiplier
                ),
            }
            for label, config in configs.items()
        },
        "per_transition": per_transition,
        "pooled": pooled,
        "leave_one_transition_out": held_out,
        "candidate_label": best_label,
        "gate": {"passed": passed, "criteria": criteria},
        "selected_label": best_label if passed else FIXED_PROMOTED_LABEL,
        "selected_weight": (
            configs[best_label].promoted_prior_weight if passed else 0.0
        ),
        "selected_mode": (
            "championship_relative" if passed else "fixed"
        ),
        "rationale": (
            f"{best_label} improved promoted-club forecasts on "
            f"{len(improved)} of {len(per_transition)} transitions without "
            "materially worsening overall forecasting."
            if passed
            else (
                "No tested weight improved promoted-club forecasting reliably "
                "enough to displace the declared fixed prior, so the fixed "
                "prior is retained."
            )
        ),
    }


def _pooled_metrics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool by observation count; RMSE pools as a mean square, not a mean."""

    pooled: dict[str, Any] = {}
    for group in ("overall", "promoted_attack", "promoted_defence", "promoted_involved"):
        total = 0
        sums: dict[str, float] = {}
        for entry in entries:
            metrics = entry[group]
            count = int(metrics.get("observations", 0))
            if not count:
                continue
            total += count
            for key in ("goals_rmse", "goals_mae", "goals_bias", "clean_sheet_brier"):
                value = metrics.get(key)
                if value is None:
                    continue
                sums[key] = sums.get(key, 0.0) + count * (
                    value**2 if key == "goals_rmse" else value
                )
        pooled[group] = {
            "observations": total,
            **{
                key: round(
                    math.sqrt(value / total) if key == "goals_rmse" else value / total,
                    4,
                )
                for key, value in sums.items()
            },
        }
    return pooled


# --------------------------------------------------------------------------
# 2. Promoted-player role evidence
# --------------------------------------------------------------------------


def audit_promoted_player_roles(
    database: HistoricalDatabase,
    *,
    season_code: str,
    previous_season_code: str,
    candidates: tuple[CandidatePlayer, ...],
) -> dict[str, Any]:
    """Report what role evidence exists, and whether it can be used.

    With no usable evidence this returns a refusal, not an empty adjustment.
    The distinction matters: "we applied the treatment and it changed nothing"
    and "there was nothing to apply" are different claims, and only the second
    one is true here unless a role file has been imported.
    """

    promoted_names = set(_promoted_names(database, season_code))
    promoted_short_names = {
        str(row["short_name"])
        for row in database.connection.execute(
            """
            SELECT teams.short_name, teams.name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
        if str(row["name"]) in promoted_names
    }
    promoted_candidates = tuple(
        player
        for player in candidates
        if player.team_short_name in promoted_short_names
    )
    roles = stored_roles(database, championship_season_code=previous_season_code)
    matches, unresolved = match_roles(
        roles,
        tuple(
            {
                "source_player_id": player.source_player_id,
                "club_name": player.team_short_name,
                "name": player.web_name,
                "official_fpl_code": None,
            }
            for player in promoted_candidates
        ),
    )
    coverage = role_coverage(
        eligible_promoted_players=len(promoted_candidates),
        matched_players=len(matches),
        unresolved=unresolved,
    )
    return {
        "championship_season": previous_season_code,
        "promoted_clubs": sorted(promoted_short_names),
        "eligible_promoted_candidates": len(promoted_candidates),
        "stored_role_rows": len(roles),
        "coverage": coverage,
        "adopted": bool(coverage["sufficient"]),
        "treatment": (
            "shrunk_championship_role" if coverage["sufficient"] else "none"
        ),
        "audit_rows": [
            {
                "source_player_id": match.source_player_id,
                "club": match.role.club_name,
                "player": match.role.player_name,
                "appearances": match.role.appearances,
                "starts": match.role.starts,
                "substitute_appearances": match.role.substitute_appearances,
                "minutes": match.role.minutes,
                "share_of_team_minutes": round(match.role.minutes_share, 4),
                "match_method": match.method,
            }
            for match in sorted(matches.values(), key=lambda m: m.source_player_id)
        ],
        "scoring_fields_imported": [],
        "note": (
            "Championship appearances, starts, minutes and share of team "
            "minutes are the only fields this path can read. No Championship "
            "goal, assist, clean-sheet or fantasy-point total enters a Premier "
            "League scoring projection, and the storage table has no column "
            "for one."
        ),
        "minimum_coverage": MINIMUM_ROLE_COVERAGE,
    }


# --------------------------------------------------------------------------
# 3 and 4. The exact squad frontier
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontierEntry:
    rank: int
    linear_rank: int
    result: FullSquadResult

    @property
    def player_ids(self) -> frozenset[str]:
        return frozenset(
            player.source_player_id for player in self.result.players
        )


def build_frontier(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    budget_tenths: int,
    size: int = DEFAULT_FRONTIER_SIZE,
    required_player_ids: frozenset[str] = frozenset(),
    excluded_squads: tuple[frozenset[str], ...] = (),
) -> tuple[list[FullSquadResult], dict[str, Any]]:
    """Enumerate distinct complete legal squads and rescore every one exactly.

    Each solve excludes every squad already produced — the complete fifteen,
    not the starting eleven — so two squads that field the same XI behind
    different benches are both allowed onto the frontier. They are genuinely
    different propositions once autosubs and rotation are priced.

    The solver ranks by its linear objective. Every candidate is then rescored
    exactly, integrating independent appearance outcomes with legal autosubs,
    bench order, goalkeeper-pair orientation and captain fallback, and ranked
    again. Whether those two orders differ is reported rather than assumed.
    """

    if size < 1:
        raise ValueError("Frontier size must be at least one")
    results: list[FullSquadResult] = []
    excluded = list(excluded_squads)
    started = time.monotonic()
    exhausted = False
    for _ in range(size):
        try:
            result = optimise_full_squad(
                candidates,
                budget_tenths=budget_tenths,
                rules=rules,
                excluded_squads=tuple(excluded),
                required_player_ids=required_player_ids,
            )
        except OptimisationError:
            exhausted = True
            break
        results.append(result)
        excluded.append(
            frozenset(player.source_player_id for player in result.players)
        )
    elapsed = time.monotonic() - started
    if not results:
        raise OptimisationError(
            "No legal squad could be produced from the candidate set"
        )
    linear_order = [
        id(result)
        for result in sorted(
            results,
            key=lambda result: (
                -result.solver_objective,
                tuple(sorted(p.source_player_id for p in result.players)),
            ),
        )
    ]
    exact_order = sorted(results, key=squad_ranking_key)
    linear_rank = {value: index + 1 for index, value in enumerate(linear_order)}
    diagnostics = {
        "first_solve": results[0],
        "requested": size,
        "produced": len(results),
        "exhausted_before_target": exhausted,
        "runtime_seconds": round(elapsed, 2),
        "distinct_squads": len({frozenset(
            player.source_player_id for player in result.players
        ) for result in results}),
        "distinct_starting_xis": len({result.starting_player_ids for result in results}),
        "exact_versus_linear": _rank_comparison(results, exact_order, linear_rank),
    }
    return exact_order, diagnostics


def _rank_comparison(
    results: list[FullSquadResult],
    exact_order: list[FullSquadResult],
    linear_rank: dict[int, int],
) -> dict[str, Any]:
    moves = [
        {
            "exact_rank": index + 1,
            "linear_rank": linear_rank[id(result)],
            "decision_value": result.decision_value,
            "solver_objective": result.solver_objective,
        }
        for index, result in enumerate(exact_order)
    ]
    reordered = [entry for entry in moves if entry["exact_rank"] != entry["linear_rank"]]
    top_changed = bool(moves and moves[0]["linear_rank"] != 1)
    return {
        "reordered_candidates": len(reordered),
        "candidates": len(results),
        "changes_the_order": bool(reordered),
        "changes_the_winner": top_changed,
        "largest_rank_move": (
            max(abs(entry["exact_rank"] - entry["linear_rank"]) for entry in moves)
            if moves
            else 0
        ),
        "ranks": moves,
        "note": (
            "The linear rank is CBC's own proven objective, which prices a "
            "legal XI and its captain but not autosub activation, bench order, "
            "the vice-captain or goalkeeper-pair orientation. The exact rank "
            "prices all of them."
        ),
    }


def squad_summary(
    result: FullSquadResult,
    *,
    label: str,
    budget_tenths: int,
    candidates: tuple[CandidatePlayer, ...],
) -> dict[str, Any]:
    summary = squad_as_dict(result, label=label, candidates=candidates)
    by_id = {player.source_player_id: player for player in result.players}
    summary.update(
        {
            "bank_tenths": budget_tenths - result.total_cost_tenths,
            "solver_objective": result.solver_objective,
            "goalkeeper_pair": [
                {
                    "source_player_id": player_id,
                    "web_name": by_id[player_id].web_name
                    if player_id in by_id
                    else player_id,
                    "team": by_id[player_id].team_short_name
                    if player_id in by_id
                    else None,
                    "price_tenths": by_id[player_id].price_tenths
                    if player_id in by_id
                    else None,
                    "standalone_horizon_xp": round(
                        by_id[player_id].expected_points, 3
                    )
                    if player_id in by_id
                    else None,
                }
                for player_id in result.goalkeeper_pair
            ],
            "goalkeeper_pair_value": result.goalkeeper_pair_value,
            "goalkeeper_orientations": [
                {
                    "gameweek_number": orientation.gameweek_number,
                    "starter": by_id[orientation.starter_id].web_name
                    if orientation.starter_id in by_id
                    else orientation.starter_id,
                    "starter_id": orientation.starter_id,
                    "substitute_id": orientation.substitute_id,
                    "pair_value": round(orientation.value, 3),
                    "alternative_orientation_value": round(
                        orientation.alternative_value, 3
                    ),
                    "uplift_over_starter_alone": round(orientation.uplift, 4),
                    "starts_despite_lower_standalone": (
                        orientation.prefers_lower_standalone
                    ),
                }
                for orientation in result.goalkeeper_orientations
            ],
            "team_counts": _team_counts(result.players),
        }
    )
    return summary


def _team_counts(players: tuple[CandidatePlayer, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for player in players:
        counts[player.team_short_name] = counts.get(player.team_short_name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def squad_difference(
    baseline: FullSquadResult,
    other: FullSquadResult,
) -> dict[str, Any]:
    """Who came in, who went out, and what it cost."""

    baseline_by_id = {p.source_player_id: p for p in baseline.players}
    other_by_id = {p.source_player_id: p for p in other.players}
    out = sorted(set(baseline_by_id) - set(other_by_id))
    into = sorted(set(other_by_id) - set(baseline_by_id))
    return {
        "changes": len(out),
        "out": [
            {
                "source_player_id": player_id,
                "web_name": baseline_by_id[player_id].web_name,
                "team": baseline_by_id[player_id].team_short_name,
                "position": baseline_by_id[player_id].position.value,
                "price_tenths": baseline_by_id[player_id].price_tenths,
                "horizon_xp": round(baseline_by_id[player_id].expected_points, 3),
            }
            for player_id in out
        ],
        "in": [
            {
                "source_player_id": player_id,
                "web_name": other_by_id[player_id].web_name,
                "team": other_by_id[player_id].team_short_name,
                "position": other_by_id[player_id].position.value,
                "price_tenths": other_by_id[player_id].price_tenths,
                "horizon_xp": round(other_by_id[player_id].expected_points, 3),
            }
            for player_id in into
        ],
        "cost_change_tenths": other.total_cost_tenths - baseline.total_cost_tenths,
        "exact_value_change": round(
            other.decision_value - baseline.decision_value, 3
        ),
    }


def solve_bank_levels(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    budget_tenths: int,
    thresholds: tuple[int, ...] = BANK_THRESHOLDS_TENTHS,
) -> list[dict[str, Any]]:
    """Solve the best squad at each level of money deliberately left unspent."""

    solved: list[dict[str, Any]] = []
    for threshold in thresholds:
        try:
            result = optimise_full_squad(
                candidates,
                budget_tenths=budget_tenths - threshold,
                rules=rules,
            )
        except OptimisationError as error:
            solved.append(
                {
                    "minimum_bank_tenths": threshold,
                    "feasible": False,
                    "reason": str(error),
                    "result": None,
                }
            )
            continue
        solved.append(
            {
                "minimum_bank_tenths": threshold,
                "feasible": True,
                "result": result,
            }
        )
    return solved


def report_bank_levels(
    solved: list[dict[str, Any]],
    *,
    primary: FullSquadResult,
    budget_tenths: int,
    pool: tuple[FullSquadResult, ...] = (),
) -> dict[str, Any]:
    """Report the best squad available at each level of unspent money.

    Each level is the best *exactly valued* squad, over every complete squad
    this run enumerated, that leaves at least that much in the bank — not
    merely the one the solver returned from a budget-reduced solve. The solver
    maximises a linear objective, so its budget-reduced answer is not
    necessarily the exact optimum at that bank level, and reporting it as the
    price of flexibility would overstate the cost.

    Money in the bank is not converted into points. There is no defensible
    exchange rate: what it buys is a transfer that has not happened yet, at a
    price that has not moved yet. So each level carries its own exact value and
    the value it gives up, and the reader decides whether that is worth paying
    for the flexibility.
    """

    entries: list[dict[str, Any]] = []
    for solution in solved:
        threshold = solution["minimum_bank_tenths"]
        qualifying = [
            candidate
            for candidate in pool
            if budget_tenths - candidate.total_cost_tenths >= threshold
        ]
        if solution["feasible"]:
            qualifying.append(solution["result"])
        if not qualifying:
            entries.append(
                {
                    "minimum_bank_tenths": threshold,
                    "feasible": False,
                    "reason": solution.get(
                        "reason",
                        "No enumerated squad leaves this much in the bank",
                    ),
                }
            )
            continue
        result = min(qualifying, key=squad_ranking_key)
        sacrificed = round(primary.decision_value - result.decision_value, 3)
        entries.append(
            {
                "minimum_bank_tenths": threshold,
                "feasible": True,
                "exact_horizon_value": result.decision_value,
                "gameweek_value": result.gameweek_expected_points,
                "total_cost_tenths": result.total_cost_tenths,
                "bank_tenths": budget_tenths - result.total_cost_tenths,
                "value_sacrificed": sacrificed,
                "changes_from_unrestricted": squad_difference(primary, result),
                "flexibility_equivalent": (
                    sacrificed <= FLEXIBILITY_EQUIVALENCE_POINTS
                ),
                "player_ids": sorted(
                    player.source_player_id for player in result.players
                ),
            }
        )
    return {
        "thresholds_tenths": [
            solution["minimum_bank_tenths"] for solution in solved
        ],
        "equivalence_band_points": FLEXIBILITY_EQUIVALENCE_POINTS,
        "entries": entries,
        "policy": (
            "Money in the bank is never given a points value. A bank-preserving "
            "squad within "
            f"{FLEXIBILITY_EQUIVALENCE_POINTS} expected points over the horizon "
            "is reported as flexibility-equivalent, and does not replace the "
            "maximum-value primary squad: equivalent within noise is not better."
        ),
    }


def solve_club_defender_counterfactuals(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    budget_tenths: int,
    club_short_name: str = ARSENAL_SHORT_NAME,
) -> list[dict[str, Any]]:
    """Force each of a club's defenders in and rebuild the squad around them.

    Substituting a player into a finished squad answers a different and easier
    question. Forcing them in and re-solving lets the optimiser pay for them
    however it likes, so the reported gap is the true cost of owning them and
    the displacement chain shows who paid.
    """

    eligible = tuple(
        player
        for player in candidates
        if player.team_short_name == club_short_name
        and player.position.value == "DEF"
    )
    solved: list[dict[str, Any]] = []
    for player in sorted(eligible, key=lambda value: value.source_player_id):
        try:
            result = optimise_full_squad(
                candidates,
                budget_tenths=budget_tenths,
                rules=rules,
                required_player_ids=frozenset({player.source_player_id}),
            )
        except OptimisationError as error:
            solved.append(
                {"player": player, "feasible": False, "reason": str(error), "result": None}
            )
            continue
        solved.append({"player": player, "feasible": True, "result": result})
    return solved


def report_club_defender_counterfactuals(
    solved: list[dict[str, Any]],
    *,
    primary: FullSquadResult,
    budget_tenths: int,
    candidates: tuple[CandidatePlayer, ...],
    all_candidates: tuple[CandidatePlayer, ...],
    components: dict[str, dict[str, Any]] | None = None,
    club_short_name: str = ARSENAL_SHORT_NAME,
) -> dict[str, Any]:
    eligible_ids = {player.source_player_id for player in candidates}
    excluded_by_availability = tuple(
        player
        for player in all_candidates
        if player.team_short_name == club_short_name
        and player.position.value == "DEF"
        and player.source_player_id not in eligible_ids
    )
    forced: list[dict[str, Any]] = []
    for solution in solved:
        player = solution["player"]
        if not solution["feasible"]:
            forced.append(
                {
                    "source_player_id": player.source_player_id,
                    "web_name": player.web_name,
                    "price_tenths": player.price_tenths,
                    "feasible": False,
                    "reason": solution["reason"],
                }
            )
            continue
        result = solution["result"]
        opening = player.gameweek_values[0] if player.gameweek_values else None
        breakdown = _component_breakdown(
            player, (components or {}).get(player.source_player_id)
        )
        forced.append(
            {
                "source_player_id": player.source_player_id,
                "web_name": player.web_name,
                "price_tenths": player.price_tenths,
                "feasible": True,
                "mean_appearance": round(mean_appearance(player), 4),
                "expected_minutes_proxy_appearance": round(
                    player.appearance_probability, 4
                ),
                "gameweek_1_xp": round(
                    opening.expected_points if opening else 0.0, 3
                ),
                "horizon_xp": round(player.expected_points, 3),
                "components": breakdown,
                "in_primary_squad": player.source_player_id
                in {member.source_player_id for member in primary.players},
                "starts_gameweek_1": (
                    player.source_player_id in result.starting_player_ids
                ),
                "exact_horizon_value": result.decision_value,
                "value_gap": round(
                    primary.decision_value - result.decision_value, 3
                ),
                "total_cost_tenths": result.total_cost_tenths,
                "bank_tenths": budget_tenths - result.total_cost_tenths,
                "displacement": squad_difference(primary, result),
            }
        )
    feasible = [entry for entry in forced if entry.get("feasible")]
    best = min(feasible, key=lambda entry: entry["value_gap"], default=None)
    return {
        "club": club_short_name,
        "eligible_defenders": len(solved),
        "excluded_by_availability": [
            {
                "source_player_id": player.source_player_id,
                "web_name": player.web_name,
                "price_tenths": player.price_tenths,
                "mean_appearance": round(mean_appearance(player), 4),
                "reason": "below the minimum mean appearance guardrail",
            }
            for player in excluded_by_availability
        ],
        "forced": forced,
        "best_squad_containing_one": (
            None
            if best is None
            else {
                "source_player_id": best["source_player_id"],
                "web_name": best["web_name"],
                "exact_horizon_value": best["exact_horizon_value"],
                "value_gap": best["value_gap"],
            }
        ),
    }


def player_component_sums(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    config: ProjectionModelConfig,
    gameweek: int,
    horizon_gameweeks: int,
    generated_at: datetime,
) -> dict[str, dict[str, Any]]:
    """Each player's expected points split into the components that made them.

    Read from the projection itself rather than inferred, so a counterfactual
    can say whether a defender is being valued for clean sheets, for attacking
    return or for defensive contribution, instead of only that the total is
    lower than somebody else's.
    """

    result = RatesProjectionModel(
        database, rules, config=config, model_version="component-explain"
    ).project(
        season_code=season_code,
        start_gameweek=gameweek,
        horizon_gameweeks=horizon_gameweeks,
        generated_at=generated_at,
        persist=False,
    )
    sums: dict[str, dict[str, float]] = {}
    opening: dict[str, dict[str, float]] = {}
    for projection in result.projections:
        entry = sums.setdefault(
            projection.source_player_id,
            {
                "appearance_points": 0.0,
                "attacking_points": 0.0,
                "clean_sheet_points": 0.0,
                "defensive_contribution_points": 0.0,
                "save_points": 0.0,
                "bonus_points": 0.0,
                "expected_minutes": 0.0,
            },
        )
        entry["appearance_points"] += projection.appearance_points
        entry["attacking_points"] += (
            projection.goal_points + projection.assist_points
        )
        entry["clean_sheet_points"] += projection.clean_sheet_points
        entry["defensive_contribution_points"] += (
            projection.defensive_contribution_points
        )
        entry["save_points"] += projection.save_points
        entry["bonus_points"] += projection.bonus_points
        entry["expected_minutes"] += projection.expected_minutes
        if projection.gameweek_number == gameweek:
            opening[projection.source_player_id] = {
                "gameweek_expected_minutes": round(projection.expected_minutes, 2),
                "gameweek_clean_sheet_points": round(
                    projection.clean_sheet_points, 3
                ),
                "gameweek_attacking_points": round(
                    projection.goal_points + projection.assist_points, 3
                ),
                "gameweek_defensive_contribution_points": round(
                    projection.defensive_contribution_points, 3
                ),
            }
    return {
        player_id: {
            **{key: round(value, 3) for key, value in entry.items()},
            **opening.get(player_id, {}),
        }
        for player_id, entry in sums.items()
    }


def _component_breakdown(
    player: CandidatePlayer, components: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A candidate's horizon shape, plus the projection's own decomposition.

    The optimiser's candidate carries a total per Gameweek and no split. The
    clean-sheet, attacking and defensive-contribution components come from the
    projection rows themselves and are merged in when supplied, rather than
    being reconstructed from the total.
    """

    return {
        **(components or {}),
        "horizon_expected_points": round(player.expected_points, 3),
        "per_gameweek_expected_points": [
            round(value.expected_points, 3) for value in player.gameweek_values
        ],
        "per_gameweek_appearance": [
            round(value.appearance_probability, 4)
            for value in player.gameweek_values
        ],
        "per_gameweek_sixty": [
            round(value.sixty_probability, 4) for value in player.gameweek_values
        ],
        "note": (
            "Clean-sheet, attacking and defensive-contribution components are "
            "persisted on the projection run's player rows; the optimiser "
            "candidate carries only the total."
        ),
    }

def explain_absence(
    counterfactuals: dict[str, Any],
    *,
    candidates: tuple[CandidatePlayer, ...],
    primary: FullSquadResult | None = None,
    club_short_name: str = ARSENAL_SHORT_NAME,
) -> dict[str, Any]:
    """Which of the five possible reasons actually accounts for the absence.

    Price, individual projection, minutes or eligibility, fixtures and budget
    allocation are separable, and answering "the model just prefers others" is
    not an answer. Each is checked against the numbers that would have to be
    true for it to be the cause.
    """

    selected = (
        sorted(
            player.web_name
            for player in (primary.players if primary else ())
            if player.team_short_name == club_short_name
        )
    )
    if selected:
        return {
            "conclusion": "not_absent",
            "selected_players": selected,
            "detail": (
                f"The recommended squad contains {len(selected)} "
                f"{club_short_name} player(s) — {', '.join(selected)} — so "
                "there is no absence to explain. The counterfactual table "
                "below still reports what forcing each defender in costs."
            ),
        }
    defenders = tuple(
        player
        for player in candidates
        if player.team_short_name == club_short_name
        and player.position.value == "DEF"
    )
    all_defenders = tuple(
        player for player in candidates if player.position.value == "DEF"
    )
    if not defenders or not all_defenders:
        return {
            "conclusion": "no_eligible_defender",
            "detail": (
                f"No {club_short_name} defender survives the eligibility "
                "guardrail, so the absence is an availability outcome, not a "
                "valuation one."
            ),
        }
    prices = sorted(player.price_tenths for player in all_defenders)
    median_price = prices[len(prices) // 2]
    club_prices = sorted(player.price_tenths for player in defenders)
    best_club = max(defenders, key=lambda player: player.expected_points)
    selected_band = [
        player
        for player in all_defenders
        if abs(player.price_tenths - best_club.price_tenths) <= 5
    ]
    better_in_band = [
        player
        for player in selected_band
        if player.expected_points > best_club.expected_points
    ]
    minimum_gap = min(
        (entry["value_gap"] for entry in counterfactuals["forced"] if entry.get("feasible")),
        default=None,
    )
    reasons = {
        "price": {
            "club_defender_prices_tenths": club_prices,
            "league_median_defender_price_tenths": median_price,
            "cheapest_club_defender_is_above_median": club_prices[0] > median_price,
        },
        "individual_projection": {
            "best_club_defender": best_club.web_name,
            "best_club_defender_horizon_xp": round(best_club.expected_points, 3),
            "defenders_within_0_5m_scoring_higher": len(better_in_band),
            "examples": sorted(
                (
                    {
                        "web_name": player.web_name,
                        "team": player.team_short_name,
                        "price_tenths": player.price_tenths,
                        "horizon_xp": round(player.expected_points, 3),
                    }
                    for player in better_in_band
                ),
                key=lambda entry: -entry["horizon_xp"],
            )[:5],
        },
        "minutes_or_eligibility": {
            "excluded_by_availability": counterfactuals["excluded_by_availability"],
            "lowest_mean_appearance_among_eligible": round(
                min(mean_appearance(player) for player in defenders), 4
            ),
        },
        "budget_allocation": {
            "minimum_value_gap": minimum_gap,
            "displacement_of_best_case": (
                counterfactuals["forced"][0]["displacement"]
                if counterfactuals["forced"]
                else None
            ),
        },
    }
    if club_prices[0] > median_price and not better_in_band:
        conclusion = "price"
    elif better_in_band:
        conclusion = "individual_projection"
    elif counterfactuals["excluded_by_availability"]:
        conclusion = "minutes_or_eligibility"
    else:
        conclusion = "budget_allocation"
    return {
        "conclusion": conclusion,
        "reasons": reasons,
        "note": (
            "Fixture difficulty is not reported as a separate cause here: over "
            "an eight-Gameweek horizon every club faces a materially different "
            "run, and the fixture effect is already inside each defender's "
            "horizon projection rather than being separable from it."
        ),
    }


# --------------------------------------------------------------------------
# Concentration tests
# --------------------------------------------------------------------------


def _team_id_for(
    database: HistoricalDatabase, season_code: str, short_name: str
) -> str | None:
    row = database.connection.execute(
        """
        SELECT teams.source_team_id FROM teams
        JOIN seasons ON seasons.id = teams.season_id
        WHERE seasons.code = ? AND teams.short_name = ?
        """,
        (season_code, short_name),
    ).fetchone()
    return None if row is None else str(row["source_team_id"])


def attack_scale_override(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    config: ProjectionModelConfig,
    short_name: str,
    scale: float,
) -> tuple[TeamStrengthOverride, ...]:
    """Scale one club's attack multiplier, leaving everything else alone.

    The perturbation is applied to the club's *rating*, so it travels through
    expected goals into their own attackers, their opponents' clean sheets,
    bonus and defensive contribution — which is what a concentration test is
    supposed to test. Scaling a finished points total would move the players
    and leave the rest of the league believing something else.
    """

    model = RatesProjectionModel(database, rules, config=config)
    strengths = model._team_strengths(season_code, 1, ())
    names = {
        str(row["id"]): (str(row["source_team_id"]), str(row["short_name"]))
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.source_team_id, teams.short_name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
    }
    for team_id, values in strengths.items():
        source_id, short = names.get(team_id, ("", ""))
        if short != short_name:
            continue
        return (
            TeamStrengthOverride(
                source_team_id=source_id,
                attack_multiplier=float(values["attack"]) * scale,
                defence_susceptibility=float(values["defence"]),
                rationale=(
                    f"Concentration test: {short_name} attack scaled by "
                    f"{scale:g}, defence unchanged."
                ),
            ),
        )
    return ()


def structural_claims(
    result: FullSquadResult,
    *,
    promoted_short_names: frozenset[str],
) -> dict[str, Any]:
    """The named claims a stress test is supposed to be able to break."""

    counts = _team_counts(result.players)
    by_id = {player.source_player_id: player for player in result.players}
    starters = result.starting_player_ids
    return {
        "triple_manchester_united_attack": sum(
            1
            for player in result.players
            if player.team_short_name == "MUN"
            and player.position.value in {"MID", "FWD"}
        )
        >= 3,
        "manchester_united_players": counts.get("MUN", 0),
        "bournemouth_attacking_double_up": sum(
            1
            for player in result.players
            if player.team_short_name == "BOU"
            and player.position.value in {"MID", "FWD"}
        )
        >= 2,
        "no_arsenal": counts.get(ARSENAL_SHORT_NAME, 0) == 0,
        "promoted_defenders": sorted(
            player.web_name
            for player in result.players
            if player.team_short_name in promoted_short_names
            and player.position.value == "DEF"
        ),
        "goalkeeper_pair": sorted(
            by_id[player_id].web_name
            for player_id in result.goalkeeper_pair
            if player_id in by_id
        ),
        "starting_goalkeeper": (
            by_id[result.goalkeeper_orientations[0].starter_id].web_name
            if result.goalkeeper_orientations
            and result.goalkeeper_orientations[0].starter_id in by_id
            else None
        ),
        "captain": (
            by_id[result.captain_id].web_name
            if result.captain_id in by_id
            else result.captain_id
        ),
        "vice_captain": (
            by_id[result.vice_captain_id].web_name
            if result.vice_captain_id in by_id
            else result.vice_captain_id
        ),
        "starting_xi": sorted(
            by_id[player_id].web_name
            for player_id in starters
            if player_id in by_id
        ),
        "team_counts": counts,
    }


def run_concentration_tests(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    baseline_config: ProjectionModelConfig,
    baseline_result: FullSquadResult,
    fixed_prior_config: ProjectionModelConfig,
    promoted_short_names: frozenset[str],
    gameweek: int,
    horizon_gameweeks: int,
    minimum_mean_appearance: float,
    generated_at: datetime,
    role_treatment_validated: bool,
    budget_tenths: int,
) -> dict[str, Any]:
    """Four single-factor stress tests, run one at a time.

    Each perturbs exactly one thing and re-solves from scratch. Nothing is
    crossed with anything else: a factorial sweep over one season's opening
    decision produces a number for every story and evidence for none.
    """

    baseline_claims = structural_claims(
        baseline_result, promoted_short_names=promoted_short_names
    )
    runs: list[dict[str, Any]] = []

    def run_one(
        name: str,
        description: str,
        config: ProjectionModelConfig,
        team_overrides: tuple[TeamStrengthOverride, ...],
    ) -> None:
        candidates = opening_candidates(
            database,
            rules,
            config,
            season_code=season_code,
            gameweek=gameweek,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated_at,
            team_overrides=team_overrides,
            model_version=f"concentration-{name}",
        )
        eligible = tuple(
            player
            for player in candidates
            if mean_appearance(player) >= minimum_mean_appearance
        )
        result = optimise_full_squad(
            eligible, budget_tenths=budget_tenths, rules=rules
        )
        claims = structural_claims(
            result, promoted_short_names=promoted_short_names
        )
        runs.append(
            {
                "name": name,
                "description": description,
                "exact_horizon_value": result.decision_value,
                "total_cost_tenths": result.total_cost_tenths,
                "player_ids": sorted(
                    player.source_player_id for player in result.players
                ),
                "player_names": {
                    player.source_player_id: player.web_name
                    for player in result.players
                },
                "changes_from_baseline": squad_difference(baseline_result, result),
                "claims": claims,
                "claims_that_survive": {
                    key: (baseline_claims[key] == claims[key])
                    for key in (
                        "triple_manchester_united_attack",
                        "bournemouth_attacking_double_up",
                        "no_arsenal",
                        "promoted_defenders",
                        "goalkeeper_pair",
                        "captain",
                    )
                },
            }
        )

    for short_name, label in (("MUN", "manchester_united"), ("BOU", "bournemouth")):
        overrides = attack_scale_override(
            database,
            rules,
            season_code=season_code,
            config=baseline_config,
            short_name=short_name,
            scale=CONCENTRATION_ATTACK_SCALE,
        )
        if not overrides:
            runs.append(
                {
                    "name": f"{label}_attack_minus_10_percent",
                    "description": (
                        f"{short_name} is not in the {season_code} team list, "
                        "so this test could not be run."
                    ),
                    "skipped": True,
                }
            )
            continue
        run_one(
            f"{label}_attack_minus_10_percent",
            f"{short_name} attack multiplier scaled to "
            f"{CONCENTRATION_ATTACK_SCALE:g}, everything else unchanged.",
            baseline_config,
            overrides,
        )

    if fixed_prior_config != baseline_config:
        run_one(
            "fixed_promoted_prior",
            "The declared fixed promoted prior instead of the differentiated "
            "one, with no other change.",
            fixed_prior_config,
            (),
        )
    else:
        runs.append(
            {
                "name": "differentiated_promoted_prior",
                "description": (
                    "The differentiated promoted prior was not adopted, so the "
                    "live model already uses the fixed prior. Running the two "
                    "against each other here would compare a model with "
                    "itself; the historical comparison in the promoted-prior "
                    "section is the evidence."
                ),
                "skipped": True,
            }
        )

    runs.append(
        {
            "name": "promoted_player_role_treatment",
            "description": (
                "Incumbent player model against the promoted-player role "
                "treatment."
            ),
            "skipped": not role_treatment_validated,
            "reason": (
                None
                if role_treatment_validated
                else (
                    "The role treatment was not validated — no usable "
                    "Championship player-level role evidence is available — so "
                    "there is no second model to run. Reporting a null result "
                    "here would imply the treatment was tested and found "
                    "neutral, which is not what happened."
                )
            ),
        }
    )

    return {
        "baseline_claims": baseline_claims,
        "baseline_exact_value": baseline_result.decision_value,
        "baseline_note": (
            "The baseline is the unconstrained single solve under the live "
            "model, because every perturbed run is one too. The recommended "
            "squad is chosen from a wider exactly ranked pool and may differ "
            "from it."
        ),
        "attack_scale": CONCENTRATION_ATTACK_SCALE,
        "runs": runs,
        "survival": {
            claim: {
                "baseline": baseline_claims[claim],
                "runs_agreeing": sum(
                    1
                    for run in runs
                    if not run.get("skipped")
                    and run["claims_that_survive"][claim]
                ),
                "runs": sum(1 for run in runs if not run.get("skipped")),
            }
            for claim in (
                "triple_manchester_united_attack",
                "bournemouth_attacking_double_up",
                "no_arsenal",
                "promoted_defenders",
                "goalkeeper_pair",
                "captain",
            )
        },
        "policy": (
            "Single factors only. No combination of two perturbations is run: "
            "with one opening decision per season there is no way to tell a "
            "real interaction from a coincidence."
        ),
    }


def eligibility_audit(
    all_candidates: tuple[CandidatePlayer, ...],
    eligible: tuple[CandidatePlayer, ...],
    *,
    minimum_mean_appearance: float,
) -> dict[str, Any]:
    """Who the optimiser was allowed to pick from, and who was excluded and why."""

    eligible_ids = {player.source_player_id for player in eligible}
    excluded = [
        player
        for player in all_candidates
        if player.source_player_id not in eligible_ids
    ]
    by_position: dict[str, dict[str, int]] = {}
    for player in all_candidates:
        entry = by_position.setdefault(
            player.position.value, {"candidates": 0, "eligible": 0}
        )
        entry["candidates"] += 1
        if player.source_player_id in eligible_ids:
            entry["eligible"] += 1
    return {
        "priced_candidates": len(all_candidates),
        "eligible_candidates": len(eligible),
        "minimum_mean_appearance": minimum_mean_appearance,
        "by_position": dict(sorted(by_position.items())),
        "excluded_count": len(excluded),
        "notable_exclusions": [
            {
                "web_name": player.web_name,
                "team": player.team_short_name,
                "position": player.position.value,
                "price_tenths": player.price_tenths,
                "mean_appearance": round(mean_appearance(player), 4),
                "horizon_xp": round(player.expected_points, 3),
            }
            for player in sorted(
                excluded, key=lambda value: -value.expected_points
            )[:20]
        ],
        "note": (
            "Exclusion is on the mean appearance probability across the "
            "horizon only. A player excluded here is not judged poor; the "
            "model simply does not expect them to play enough for an opening "
            "squad slot to be worth spending on them."
        ),
    }


def _pair_effect(
    with_pairs: FullSquadResult,
    without_pairs: FullSquadResult,
    candidates: tuple[CandidatePlayer, ...],
) -> dict[str, Any]:
    """What the pair treatment changed, measured against valuing them singly.

    Reported even when it is nothing. A correction that happens not to bite on
    one particular squad is still a correction; claiming an effect it did not
    have would be worse than reporting zero.
    """

    by_id = {player.source_player_id: player for player in candidates}
    uplifts = [
        orientation.uplift for orientation in with_pairs.goalkeeper_orientations
    ]
    return {
        "goalkeepers_with_pair_valuation": sorted(
            by_id[player_id].web_name
            for player_id in with_pairs.goalkeeper_pair
            if player_id in by_id
        ),
        "goalkeepers_without_pair_valuation": sorted(
            player.web_name
            for player in without_pairs.players
            if player.position.value == "GK"
        ),
        "squad_changed": squad_difference(without_pairs, with_pairs)["changes"] > 0,
        "exact_value_with_pair_valuation": with_pairs.decision_value,
        "exact_value_without_pair_valuation": without_pairs.decision_value,
        "total_substitution_protection_points": round(sum(uplifts), 4),
        "largest_gameweek_uplift": round(max(uplifts), 4) if uplifts else None,
        "any_gameweek_nominates_lower_standalone": any(
            orientation.prefers_lower_standalone
            for orientation in with_pairs.goalkeeper_orientations
        ),
        "note": (
            "Substitution protection is worth "
            "P(nominated goalkeeper records no minutes) x the reserve's "
            "expected points. When the minutes model puts a first-choice "
            "goalkeeper's appearance probability at one, that product is zero "
            "and the pair treatment cannot move the selection however it is "
            "implemented. The number above says whether that is the case here."
        ),
    }


def snapshot_provenance(
    database: HistoricalDatabase, *, season_code: str
) -> dict[str, Any]:
    """Where the live player, price and fixture data actually came from.

    Reported rather than assumed, because a squad built on a mirrored snapshot
    and a squad built on a direct official capture are not equally defensible
    and the difference must not be invisible in the artifact.
    """

    rows = database.connection.execute(
        """
        SELECT DISTINCT runs.id, runs.source_name, runs.retrieved_at,
               runs.source_url, runs.content_sha256, runs.adapter_version,
               runs.status
        FROM ingestion_runs runs
        JOIN player_gameweek_observations observations
          ON observations.provenance_run_id = runs.id
        JOIN player_seasons ps ON ps.id = observations.player_season_id
        JOIN seasons ON seasons.id = ps.season_id
        WHERE seasons.code = ? AND runs.status = 'completed'
        ORDER BY runs.id DESC
        LIMIT 5
        """,
        (season_code,),
    ).fetchall()
    entries = [
        {
            "ingestion_run_id": int(row["id"]),
            "source_name": str(row["source_name"]),
            "retrieved_at": str(row["retrieved_at"]),
            "source_url": row["source_url"],
            "content_sha256": row["content_sha256"],
            "adapter_version": row["adapter_version"],
            "is_official_api": str(row["source_name"]) == "official-fpl-api",
        }
        for row in rows
    ]
    latest = entries[0] if entries else None
    return {
        "season_code": season_code,
        "runs": entries,
        "latest": latest,
        "official_direct_capture": bool(latest and latest["is_official_api"]),
        "warning": (
            None
            if latest and latest["is_official_api"]
            else (
                "The live snapshot did not come from a direct official FPL API "
                "capture. Prices, availability and fixtures are as good as the "
                "mirror behind them and no better."
            )
        ),
    }


def historical_decision_evidence(
    database: HistoricalDatabase,
    transitions: tuple[SeasonTransition, ...],
    *,
    rules_for: dict[str, SeasonRules],
    configs: dict[str, ProjectionModelConfig],
    horizon_gameweeks: int,
    candidate_pool_size: int = 2,
) -> dict[str, Any]:
    """Secondary evidence: what each prior would have done to an opening squad.

    Secondary on purpose. One opening squad per season is four observations in
    total, each a single draw from a wide distribution, and it cannot carry a
    decision on its own. The forecast evidence is what the gate reads.
    """

    seasons: list[dict[str, Any]] = []
    for transition in transitions:
        entry: dict[str, Any] = {"target_season": transition.target_season}
        for label, config in configs.items():
            scored = evaluate_player_and_decision(
                database,
                rules_for[transition.target_season],
                season_code=transition.target_season,
                label=f"promoted-prior-{transition.target_season}-{label}",
                config=config,
                horizon_gameweeks=horizon_gameweeks,
                candidate_pool_size=candidate_pool_size,
                # The opening-squad effect is what this section is for. The
                # hindsight regret measures solve a mixed-integer program per
                # origin across a whole season and would dominate the run
                # without answering the question being asked.
                include_regret=False,
            )
            entry[label] = {
                "realised_points": (scored.get("opening_squad") or {}).get(
                    "realised_points"
                ),
                "predicted_horizon_points": (
                    scored.get("opening_squad") or {}
                ).get("predicted_horizon_points"),
                "player_points_rmse": (scored.get("player_points") or {}).get(
                    "rmse"
                ),
                "squad_regret_mean": (scored.get("squad_regret") or {}).get(
                    "mean"
                ),
            }
        seasons.append(entry)
    means: dict[str, Any] = {}
    for label in configs:
        realised = [
            season[label]["realised_points"]
            for season in seasons
            if season[label]["realised_points"] is not None
        ]
        means[label] = {
            "mean_realised_points": (
                round(sum(realised) / len(realised), 3) if realised else None
            ),
            "seasons": len(realised),
        }
    difference = None
    labels = list(configs)
    if len(labels) == 2:
        first, second = labels
        if (
            means[first]["mean_realised_points"] is not None
            and means[second]["mean_realised_points"] is not None
        ):
            difference = {
                "comparison": f"{second} minus {first}",
                "mean_realised_points": round(
                    means[second]["mean_realised_points"]
                    - means[first]["mean_realised_points"],
                    3,
                ),
            }
    return {
        "seasons": seasons,
        "means": means,
        "difference": difference,
        "status": "secondary evidence; four observations, one per season",
        "hindsight_regret_measures": (
            "not run: a legal-squad and owned-captain regret replay solves a "
            "mixed-integer program per origin across a whole season, and the "
            "question here is the opening-squad effect"
        ),
    }


def finalise_preseason_squad(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    horizon_gameweeks: int = EARLY_SEASON_GAMEWEEKS,
    gameweek_number: int = 1,
    frontier_size: int = DEFAULT_FRONTIER_SIZE,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    generated_at: datetime | None = None,
    include_decision_evidence: bool = False,
    alternative_count: int = 3,
    apply_modifiers: bool = True,
) -> dict[str, Any]:
    """Validate, then project, then search, then explain — in that order.

    The order is the point. Every gate is applied to evidence that predates the
    live run, so the live run cannot influence the decision that authorised it,
    and the squad at the end is the consequence of the validation rather than
    the reason for it.
    """

    generated = generated_at or datetime.now(UTC)
    warnings: list[str] = []
    started = time.monotonic()

    # -- coverage and provenance ----------------------------------------
    provenance = snapshot_provenance(database, season_code=season_code)
    if provenance["warning"]:
        warnings.append(provenance["warning"])
    coverage = championship_coverage(database)

    transitions = discover_season_transitions(
        database,
        early_gameweeks=horizon_gameweeks,
        exclude_seasons=(season_code,),
    )
    usable = tuple(entry for entry in transitions if entry.usable)
    for entry in transitions:
        if not entry.usable:
            warnings.append(
                f"Excluded transition {entry.previous_season}->"
                f"{entry.target_season}: {entry.reason}"
            )
    rules_for = {
        transition.target_season: _rules_for_season(transition.target_season)
        for transition in usable
    }

    # -- 1. promoted-club priors ----------------------------------------
    if usable:
        promoted_validation = validate_promoted_priors(
            database,
            usable,
            rules_for=rules_for,
            early_gameweeks=horizon_gameweeks,
        )
    else:
        promoted_validation = {
            "gate": {"passed": False, "criteria": []},
            "selected_label": FIXED_PROMOTED_LABEL,
            "selected_weight": 0.0,
            "selected_mode": "fixed",
            "rationale": (
                "No usable season transition exists, so no weight can be "
                "validated and the declared fixed prior is retained."
            ),
        }
        warnings.append(
            "No usable historical transition was available to validate the "
            "differentiated promoted prior."
        )

    selected_config = (
        replace(
            CARRY_FORWARD_PRESEASON_CONFIG,
            promoted_prior_mode="championship_relative",
            promoted_prior_weight=promoted_validation["selected_weight"],
        )
        if promoted_validation["selected_mode"] == "championship_relative"
        else CARRY_FORWARD_PRESEASON_CONFIG
    )
    model_version = PRESEASON_CARRY_FORWARD_MODEL_VERSION + (
        f"-promoted-w{promoted_validation['selected_weight']:g}"
        if promoted_validation["selected_mode"] == "championship_relative"
        else "-promoted-fixed"
    )

    previous_season_row = database.connection.execute(
        "SELECT code FROM seasons WHERE code < ? ORDER BY code DESC LIMIT 1",
        (season_code,),
    ).fetchone()
    previous_season = (
        None if previous_season_row is None else str(previous_season_row["code"])
    )
    live_priors = (
        promoted_prior_summary(
            database,
            target_season=season_code,
            previous_season=previous_season,
            weight=promoted_validation["selected_weight"],
        )
        if previous_season
        else {}
    )
    if live_priors and not live_priors.get("all_matched", True):
        warnings.append(
            "At least one promoted club could not be matched to a Championship "
            "record and kept the declared fixed prior."
        )

    # -- live projection -------------------------------------------------
    live_projection = generate_preseason_projection(
        database,
        rules,
        season_code=season_code,
        config=selected_config,
        model_version=model_version,
        gameweek_number=gameweek_number,
        horizon_gameweeks=horizon_gameweeks,
        generated_at=generated,
        apply_modifiers=apply_modifiers,
    )

    all_candidates = opening_candidates(
        database,
        rules,
        selected_config,
        season_code=season_code,
        gameweek=gameweek_number,
        horizon_gameweeks=horizon_gameweeks,
        generated_at=generated,
        model_version=model_version,
    )
    eligible = tuple(
        player
        for player in all_candidates
        if mean_appearance(player) >= minimum_mean_appearance
    )
    audit = eligibility_audit(
        all_candidates, eligible, minimum_mean_appearance=minimum_mean_appearance
    )

    # -- 2. promoted-player role evidence --------------------------------
    role_audit = (
        audit_promoted_player_roles(
            database,
            season_code=season_code,
            previous_season_code=previous_season,
            candidates=all_candidates,
        )
        if previous_season
        else {"adopted": False, "coverage": {"sufficient": False}}
    )
    if not role_audit.get("adopted"):
        warnings.append(
            "The promoted-player role treatment was not adopted: "
            + str((role_audit.get("coverage") or {}).get("verdict", "no evidence"))
        )

    # -- 4. the frontier -------------------------------------------------
    frontier, frontier_diagnostics = build_frontier(
        eligible,
        rules,
        budget_tenths=rules.squad.budget_tenths,
        size=frontier_size,
    )
    # What the pair treatment actually changed, measured rather than asserted.
    without_pairs = optimise_full_squad(
        eligible,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
        goalkeeper_pair_valuation=False,
    )
    bank_solutions = solve_bank_levels(
        eligible, rules, budget_tenths=rules.squad.budget_tenths
    )
    counterfactual_solutions = solve_club_defender_counterfactuals(
        eligible, rules, budget_tenths=rules.squad.budget_tenths
    )

    # Every complete legal squad this run produced, ranked by exact value.
    # The solver maximises a linear objective that prices neither autosubs nor
    # bench order, so a squad it reached only under a side constraint — a bank
    # floor, a forced inclusion — can exactly beat the one it reached without
    # any. Leaving those out of the ranking would mean recommending a squad
    # known to be worse than one already computed, which is not defensible.
    # ``build_frontier``'s first solve carries no exclusions, so it is the
    # unconstrained linear optimum and the right baseline for a stress test.
    unconstrained = frontier_diagnostics["first_solve"]
    pool: list[FullSquadResult] = list(frontier)
    pool_sources = {id(result): "frontier" for result in frontier}
    for solution in bank_solutions:
        if solution["feasible"]:
            pool.append(solution["result"])
            pool_sources.setdefault(
                id(solution["result"]),
                f"bank_{solution['minimum_bank_tenths']}",
            )
    for solution in counterfactual_solutions:
        if solution["feasible"]:
            pool.append(solution["result"])
            pool_sources.setdefault(
                id(solution["result"]),
                f"forced_{solution['player'].web_name}",
            )
    seen: set[frozenset[str]] = set()
    unique_pool: list[FullSquadResult] = []
    for result in pool:
        key = frozenset(player.source_player_id for player in result.players)
        if key in seen:
            continue
        seen.add(key)
        unique_pool.append(result)
    ranked_pool = sorted(unique_pool, key=squad_ranking_key)
    primary = ranked_pool[0]
    primary_source = pool_sources.get(id(primary), "frontier")

    banks = report_bank_levels(
        bank_solutions,
        primary=primary,
        budget_tenths=rules.squad.budget_tenths,
        pool=tuple(ranked_pool),
    )
    components = player_component_sums(
        database,
        rules,
        season_code=season_code,
        config=selected_config,
        gameweek=gameweek_number,
        horizon_gameweeks=horizon_gameweeks,
        generated_at=generated,
    )
    counterfactuals = report_club_defender_counterfactuals(
        counterfactual_solutions,
        primary=primary,
        budget_tenths=rules.squad.budget_tenths,
        candidates=eligible,
        all_candidates=all_candidates,
        components=components,
    )
    absence = explain_absence(
        counterfactuals, candidates=eligible, primary=primary
    )

    promoted_short_names = frozenset(
        str(row["short_name"])
        for row in database.connection.execute(
            """
            SELECT teams.short_name, teams.name FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
        if str(row["name"]) in set(_promoted_names(database, season_code))
    )
    # The concentration baseline is the *unconstrained single solve*, because
    # every perturbed run is one too. Comparing a forced-inclusion squad with
    # unforced ones would report the forcing as a stress-test effect.
    concentration = run_concentration_tests(
        database,
        rules,
        season_code=season_code,
        baseline_config=selected_config,
        baseline_result=unconstrained,
        fixed_prior_config=CARRY_FORWARD_PRESEASON_CONFIG,
        promoted_short_names=promoted_short_names,
        gameweek=gameweek_number,
        horizon_gameweeks=horizon_gameweeks,
        minimum_mean_appearance=minimum_mean_appearance,
        generated_at=generated,
        role_treatment_validated=bool(role_audit.get("adopted")),
        budget_tenths=rules.squad.budget_tenths,
    )

    decision_evidence: dict[str, Any] = {}
    if include_decision_evidence and usable:
        # Both priors are replayed whatever the gate decided. A single column
        # of realised points says what one model would have scored; it takes
        # two to say anything about the choice between them, and the losing
        # candidate's decision record is exactly what a reader wants when the
        # gate rejected it on forecast accuracy.
        candidate_label = promoted_validation.get("candidate_label")
        evidence_configs = {FIXED_PROMOTED_LABEL: CARRY_FORWARD_PRESEASON_CONFIG}
        if candidate_label and candidate_label != FIXED_PROMOTED_LABEL:
            evidence_configs[candidate_label] = promoted_prior_configs()[
                candidate_label
            ]
        decision_evidence = historical_decision_evidence(
            database,
            usable,
            rules_for=rules_for,
            configs=evidence_configs,
            horizon_gameweeks=horizon_gameweeks,
        )

    # -- robust and model-sensitive selections ---------------------------
    stability = _selection_stability(
        primary,
        concentration,
        banks,
        names={
            player.source_player_id: player.web_name for player in eligible
        },
    )

    alternatives = [
        squad_summary(
            entry,
            label=f"alternative_{index}",
            budget_tenths=rules.squad.budget_tenths,
            candidates=eligible,
        )
        | {
            "exact_value_gap": round(primary.decision_value - entry.decision_value, 3),
            "changes_from_primary": squad_difference(primary, entry),
        }
        for index, entry in enumerate(
            [entry for entry in ranked_pool[1:] if entry is not primary][
                :alternative_count
            ],
            start=1,
        )
    ]

    runtime = round(time.monotonic() - started, 2)
    return {
        "season_code": season_code,
        "generated_at": generated.isoformat(),
        "gameweek_number": gameweek_number,
        "horizon_gameweeks": horizon_gameweeks,
        "frontier_size_requested": frontier_size,
        "minimum_mean_appearance": minimum_mean_appearance,
        "runtime_seconds": runtime,
        "data_coverage": {
            "snapshot_provenance": provenance,
            "championship": coverage,
            "premier_league_seasons": [
                str(row["code"])
                for row in database.connection.execute(
                    "SELECT code FROM seasons ORDER BY code"
                )
            ],
            "transitions": [entry.as_dict() for entry in transitions],
            "usable_transitions": [
                f"{entry.previous_season}->{entry.target_season}"
                for entry in usable
            ],
        },
        "promoted_team_priors": {
            "validation": promoted_validation,
            "live": live_priors,
            "selected_mode": promoted_validation["selected_mode"],
            "selected_weight": promoted_validation["selected_weight"],
            "historical_decision_evidence": decision_evidence,
        },
        "promoted_player_roles": role_audit,
        "goalkeeper_pair": {
            "implementation": (
                "Every eligible goalkeeper pair is enumerated, each pair's "
                "best weekly orientation and exact value computed, and that "
                "value carried into the same objective the outfield players "
                "are selected under. Goalkeepers appear in the objective once "
                "and are excluded from the starter, captain and bench-quality "
                "terms, so no goalkeeper's points are counted twice. The "
                "nomination is pinned inside the solve, not swapped afterwards."
            ),
            "independence_assumption": (
                "Appearance states are independent, as everywhere else in the "
                "optimiser. Two goalkeepers at the same club, or a first "
                "choice and their own understudy, would violate it and the "
                "protection would be overstated."
            ),
            "selected_pair": [
                entry
                for entry in squad_summary(
                    primary,
                    label="primary",
                    budget_tenths=rules.squad.budget_tenths,
                    candidates=eligible,
                )["goalkeeper_pair"]
            ],
            "orientations": squad_summary(
                primary,
                label="primary",
                budget_tenths=rules.squad.budget_tenths,
                candidates=eligible,
            )["goalkeeper_orientations"],
            "pair_value": primary.goalkeeper_pair_value,
            "effect_on_this_run": _pair_effect(
                unconstrained, without_pairs, eligible
            ),
        },
        "eligibility_audit": audit,
        "frontier": {
            **{
                key: value
                for key, value in frontier_diagnostics.items()
                if key != "first_solve"
            },
            "ranking_pool_size": len(ranked_pool),
            "primary_source": primary_source,
            "ranking_pool_note": (
                "The frontier, every feasible bank level and every forced "
                "inclusion are ranked together on exact value. A squad the "
                "linear solver only reached under a side constraint can still "
                "be the exact optimum among everything enumerated."
            ),
            "squads": [
                squad_summary(
                    entry,
                    label=f"frontier_{index}",
                    budget_tenths=rules.squad.budget_tenths,
                    candidates=eligible,
                )
                for index, entry in enumerate(frontier, start=1)
            ],
        },
        "bank_frontier": {
            **{key: value for key, value in banks.items() if key != "entries"},
            "entries": [
                {key: value for key, value in entry.items() if key != "result"}
                for entry in banks["entries"]
            ],
        },
        "arsenal_counterfactuals": {
            **{
                key: value
                for key, value in counterfactuals.items()
                if key != "forced"
            },
            "forced": [
                {key: value for key, value in entry.items() if key != "result"}
                for entry in counterfactuals["forced"]
            ],
            "conclusion": absence,
        },
        "concentration_tests": concentration,
        "live_projection": live_projection,
        "final_squad": squad_summary(
            primary,
            label="final",
            budget_tenths=rules.squad.budget_tenths,
            candidates=eligible,
        )
        | {
            "projection_run_id": live_projection.get("projection_run_id"),
            "model_version": model_version,
            "provisional": True,
        },
        "alternatives": alternatives,
        "selection_stability": stability,
        "warnings": warnings,
        "status": (
            "provisional until the final pre-deadline team-news rerun"
        ),
    }


def _rules_for_season(season_code: str) -> SeasonRules:
    from .config import load_season_rules

    return load_season_rules(Path(f"config/seasons/{season_code}.json"))


def _selection_stability(
    primary: FullSquadResult,
    concentration: dict[str, Any],
    banks: dict[str, Any],
    names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Which players survive every stress test, and which appear in only one.

    Membership is counted across the primary squad, every completed
    concentration run and every feasible bank level. A player present in all
    of them is a selection the evidence supports; a player present only in the
    primary squad is a selection one model produced.
    """

    memberships: list[frozenset[str]] = [
        frozenset(player.source_player_id for player in primary.players)
    ]
    resolved = dict(names or {})
    resolved.update(
        {player.source_player_id: player.web_name for player in primary.players}
    )
    for run in concentration.get("runs", []):
        if run.get("skipped"):
            continue
        resolved.update(run.get("player_names") or {})
        memberships.append(frozenset(run["player_ids"]))
    for entry in banks.get("entries", []):
        if not entry.get("feasible"):
            continue
        memberships.append(frozenset(entry["player_ids"]))
    counts: dict[str, int] = {}
    for membership in memberships:
        for player_id in membership:
            counts[player_id] = counts.get(player_id, 0) + 1
    total = len(memberships)
    classification = {
        player_id: (
            "robust"
            if count == total
            else "moderate"
            if count > 1
            else "model_sensitive"
        )
        for player_id, count in counts.items()
    }
    return {
        "runs_compared": total,
        "robust": sorted(
            resolved.get(player_id, player_id)
            for player_id, value in classification.items()
            if value == "robust"
        ),
        "moderate": sorted(
            resolved.get(player_id, player_id)
            for player_id, value in classification.items()
            if value == "moderate"
        ),
        "model_sensitive": sorted(
            resolved.get(player_id, player_id)
            for player_id, value in classification.items()
            if value == "model_sensitive"
        ),
        "selection_counts": {
            resolved.get(player_id, player_id): count
            for player_id, count in sorted(counts.items())
        },
    }


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


def squad_artifact(result: dict[str, Any]) -> dict[str, Any]:
    """The squad document, separated from the validation that authorised it."""

    return {
        "season_code": result["season_code"],
        "generated_at": result["generated_at"],
        "gameweek_number": result["gameweek_number"],
        "horizon_gameweeks": result["horizon_gameweeks"],
        "projection_run_id": (result.get("live_projection") or {}).get(
            "projection_run_id"
        ),
        "model_version": (result.get("live_projection") or {}).get("model_version"),
        "promoted_prior": {
            "mode": result["promoted_team_priors"]["selected_mode"],
            "weight": result["promoted_team_priors"]["selected_weight"],
            "clubs": (result["promoted_team_priors"].get("live") or {}).get(
                "clubs", []
            ),
        },
        "promoted_player_role_treatment": result["promoted_player_roles"].get(
            "treatment", "none"
        ),
        "final_squad": result["final_squad"],
        "alternatives": result["alternatives"],
        "bank_frontier": result["bank_frontier"],
        "goalkeeper_pair": result["goalkeeper_pair"],
        "selection_stability": result["selection_stability"],
        "warnings": result["warnings"],
        "status": result["status"],
    }


def _money(tenths: int | None) -> str:
    return "—" if tenths is None else f"£{tenths / 10:.1f}m"


def render_markdown(result: dict[str, Any]) -> str:
    """The report a person reads before committing a squad."""

    lines: list[str] = []
    squad = result["final_squad"]
    priors = result["promoted_team_priors"]
    gate = priors["validation"].get("gate", {})
    provenance = result["data_coverage"]["snapshot_provenance"]

    lines += [
        f"# Preseason final squad — {result['season_code']}",
        "",
        f"Generated {result['generated_at']}. "
        f"Horizon GW{result['gameweek_number']}–"
        f"GW{result['gameweek_number'] + result['horizon_gameweeks'] - 1}. "
        f"Total runtime {result['runtime_seconds']}s.",
        "",
        "**This squad is provisional.** It stands until the final reliable "
        "pre-deadline team-news rerun, and no further.",
        "",
        "## 1. Data coverage and provenance",
        "",
        f"- Premier League seasons imported: "
        f"{', '.join(result['data_coverage']['premier_league_seasons'])}",
        f"- Usable season transitions: "
        f"{', '.join(result['data_coverage']['usable_transitions']) or 'none'}",
        f"- Live snapshot source: "
        f"`{(provenance.get('latest') or {}).get('source_name', 'none')}` "
        f"retrieved {(provenance.get('latest') or {}).get('retrieved_at', '—')}",
        f"- Direct official API capture: "
        f"{'yes' if provenance.get('official_direct_capture') else 'no'}",
        "",
        "| Championship season | clubs | matches | mean goals per club-match | source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in result["data_coverage"]["championship"]["seasons"]:
        lines.append(
            f"| {entry['season_code']} | {entry['clubs']} | {entry['matches']} "
            f"| {entry['average_goals_per_team_match']} | {entry['source_name']} |"
        )

    lines += [
        "",
        "## 2. Promoted-club priors",
        "",
        f"- Gate: **{'PASS' if gate.get('passed') else 'FAIL'}**",
        f"- Selected: **{priors['selected_mode']}**"
        + (
            f", weight {priors['selected_weight']:g}"
            if priors["selected_mode"] == "championship_relative"
            else ""
        ),
        f"- {priors['validation'].get('rationale', '')}",
        "",
        "| criterion | passed |",
        "| --- | --- |",
    ]
    for entry in gate.get("criteria", []):
        lines.append(
            f"| {entry['criterion']} | {'yes' if entry['passed'] else 'no'} |"
        )
    pooled = priors["validation"].get("pooled", {})
    if pooled:
        lines += [
            "",
            "| model | promoted goals RMSE | promoted goals MAE | promoted bias "
            "| overall RMSE | overall CS Brier |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for label, metrics in pooled.items():
            involved = metrics["promoted_involved"]
            overall = metrics["overall"]
            lines.append(
                f"| {label} | {involved.get('goals_rmse')} "
                f"| {involved.get('goals_mae')} | {involved.get('goals_bias')} "
                f"| {overall.get('goals_rmse')} "
                f"| {overall.get('clean_sheet_brier')} |"
            )
    evidence = priors.get("historical_decision_evidence") or {}
    if evidence.get("means"):
        lines += [
            "",
            "Secondary evidence — what each prior's opening squad actually "
            "scored, one observation per season:",
            "",
            "| model | mean realised GW1–8 points | seasons |",
            "| --- | --- | --- |",
        ]
        for label, entry in evidence["means"].items():
            lines.append(
                f"| {label} | {entry['mean_realised_points']} "
                f"| {entry['seasons']} |"
            )
        if evidence.get("difference"):
            lines += [
                "",
                f"Difference ({evidence['difference']['comparison']}): "
                f"{evidence['difference']['mean_realised_points']} points per "
                "season. Four observations; this is not what the gate reads.",
            ]

    live = priors.get("live") or {}
    if live.get("clubs"):
        lines += [
            "",
            f"Live priors for {result['season_code']} "
            f"(Championship {live.get('championship_season')}, cohort mean "
            f"attack {live.get('cohort_mean_attack')} against a declared "
            f"{live.get('declared_base_attack')}):",
            "",
            "| club | Championship attack relative | defence relative "
            "| attack multiplier | defence multiplier |",
            "| --- | --- | --- | --- | --- |",
        ]
        for club in live["clubs"]:
            lines.append(
                f"| {club['fpl_name']} | {club['attack_relative']} "
                f"| {club['defence_relative']} | {club['attack_multiplier']} "
                f"| {club['defence_multiplier']} |"
            )

    roles = result["promoted_player_roles"]
    lines += [
        "",
        "## 3. Promoted-player role evidence",
        "",
        f"- Treatment applied: **{roles.get('treatment', 'none')}**",
        f"- Stored Championship role rows: {roles.get('stored_role_rows', 0)}",
        f"- Eligible promoted candidates: "
        f"{roles.get('eligible_promoted_candidates', 0)}",
        f"- Coverage: {(roles.get('coverage') or {}).get('coverage')} "
        f"(minimum {(roles.get('coverage') or {}).get('minimum_coverage')})",
        "",
        str((roles.get("coverage") or {}).get("verdict", "")),
        "",
        "## 4. Goalkeeper pair",
        "",
        result["goalkeeper_pair"]["implementation"],
        "",
        result["goalkeeper_pair"]["independence_assumption"],
        "",
        "| GW | nominated | pair value | other orientation | uplift over "
        "starter alone | lower standalone starts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for orientation in result["goalkeeper_pair"]["orientations"]:
        lines.append(
            f"| {orientation['gameweek_number']} | {orientation['starter']} "
            f"| {orientation['pair_value']} "
            f"| {orientation['alternative_orientation_value']} "
            f"| {orientation['uplift_over_starter_alone']} "
            f"| {'yes' if orientation['starts_despite_lower_standalone'] else 'no'} |"
        )

    effect = result["goalkeeper_pair"].get("effect_on_this_run") or {}
    if effect:
        lines += [
            "",
            f"- Pair chosen: {', '.join(effect['goalkeepers_with_pair_valuation'])}",
            "- Chosen when goalkeepers are valued singly: "
            f"{', '.join(effect['goalkeepers_without_pair_valuation'])}",
            "- Squad changed by the pair treatment: "
            f"{'yes' if effect['squad_changed'] else 'no'}",
            "- Total substitution-protection value over the horizon: "
            f"{effect['total_substitution_protection_points']} points",
            "- A Gameweek nominates the lower standalone goalkeeper: "
            f"{'yes' if effect['any_gameweek_nominates_lower_standalone'] else 'no'}",
            "",
            effect["note"],
        ]

    audit = result["eligibility_audit"]
    frontier = result["frontier"]
    lines += [
        "",
        "## 5. Eligibility audit",
        "",
        f"- Priced candidates: {audit['priced_candidates']}",
        f"- Eligible after the mean-appearance guardrail "
        f"({audit['minimum_mean_appearance']}): {audit['eligible_candidates']}",
        f"- Excluded: {audit['excluded_count']}",
        "",
        "## 6. Candidate frontier",
        "",
        f"- Requested {frontier['requested']}, produced {frontier['produced']} "
        f"distinct complete squads in {frontier['runtime_seconds']}s",
        "- Ranked together with the bank levels and forced inclusions: "
        f"{frontier['ranking_pool_size']} squads; the recommendation came from "
        f"`{frontier['primary_source']}`",
        f"- Distinct starting elevens among them: "
        f"{frontier['distinct_starting_xis']}",
        f"- Exact rescoring reorders the solver's ranking: "
        f"**{'yes' if frontier['exact_versus_linear']['changes_the_order'] else 'no'}**"
        f" ({frontier['exact_versus_linear']['reordered_candidates']} of "
        f"{frontier['exact_versus_linear']['candidates']} moved; largest move "
        f"{frontier['exact_versus_linear']['largest_rank_move']})",
        f"- Exact rescoring changes the winner: "
        f"**{'yes' if frontier['exact_versus_linear']['changes_the_winner'] else 'no'}**",
        "",
        "## 7. Bank frontier",
        "",
        result["bank_frontier"]["policy"],
        "",
        "| minimum bank | exact GW1–8 value | GW1 value | cost | bank "
        "| changes | value sacrificed | flexibility-equivalent |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in result["bank_frontier"]["entries"]:
        if not entry.get("feasible"):
            lines.append(
                f"| {_money(entry['minimum_bank_tenths'])} | infeasible | | | "
                "| | | |"
            )
            continue
        lines.append(
            f"| {_money(entry['minimum_bank_tenths'])} "
            f"| {entry['exact_horizon_value']} | {entry['gameweek_value']} "
            f"| {_money(entry['total_cost_tenths'])} "
            f"| {_money(entry['bank_tenths'])} "
            f"| {entry['changes_from_unrestricted']['changes']} "
            f"| {entry['value_sacrificed']} "
            f"| {'yes' if entry['flexibility_equivalent'] else 'no'} |"
        )

    counterfactuals = result["arsenal_counterfactuals"]
    lines += [
        "",
        "## 8. Arsenal defender counterfactuals",
        "",
        f"- Eligible Arsenal defenders: {counterfactuals['eligible_defenders']}",
        f"- Excluded by the availability guardrail: "
        f"{len(counterfactuals['excluded_by_availability'])}",
        "",
        "| defender | price | mean appearance | GW1 xP | GW1–8 xP "
        "| forced squad value | value gap | squad changes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in counterfactuals["forced"]:
        if not entry.get("feasible"):
            lines.append(
                f"| {entry['web_name']} | | | | | infeasible | | |"
            )
            continue
        lines.append(
            f"| {entry['web_name']} | {_money(entry['price_tenths'])} "
            f"| {entry['mean_appearance']} | {entry['gameweek_1_xp']} "
            f"| {entry['horizon_xp']} | {entry['exact_horizon_value']} "
            f"| {entry['value_gap']} | {entry['displacement']['changes']} |"
        )
    best = counterfactuals.get("best_squad_containing_one")
    conclusion = counterfactuals["conclusion"]
    lines += [
        "",
        "Best squad containing any Arsenal defender: "
        + (
            f"{best['web_name']} at {best['exact_horizon_value']} "
            + (
                f"({abs(best['value_gap'])} ahead of the frontier's linear "
                "winner — the exact ranking pool promoted it)"
                if best["value_gap"] < 0
                else f"({best['value_gap']} behind the recommendation)"
            )
            if best
            else "none was feasible"
        ),
        "",
        f"**Verdict: {conclusion['conclusion']}.** "
        + str(conclusion.get("detail") or conclusion.get("note", "")),
        "",
        "## 9. Concentration tests",
        "",
        result["concentration_tests"]["policy"],
        "",
        result["concentration_tests"]["baseline_note"],
        "",
        "| test | exact value | squad changes | Man Utd triple | Bournemouth "
        "double | no Arsenal | goalkeeper pair | captain |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    baseline_claims = result["concentration_tests"]["baseline_claims"]
    lines.append(
        f"| baseline | {result['concentration_tests']['baseline_exact_value']} | — "
        f"| {'yes' if baseline_claims['triple_manchester_united_attack'] else 'no'} "
        f"| {'yes' if baseline_claims['bournemouth_attacking_double_up'] else 'no'} "
        f"| {'yes' if baseline_claims['no_arsenal'] else 'no'} "
        f"| {', '.join(baseline_claims['goalkeeper_pair'])} "
        f"| {baseline_claims['captain']} |"
    )
    for run in result["concentration_tests"]["runs"]:
        if run.get("skipped"):
            lines.append(
                f"| {run['name']} | not run | {run.get('reason') or run['description']} "
                "| | | | | |"
            )
            continue
        claims = run["claims"]
        lines.append(
            f"| {run['name']} | {run['exact_horizon_value']} "
            f"| {run['changes_from_baseline']['changes']} "
            f"| {'yes' if claims['triple_manchester_united_attack'] else 'no'} "
            f"| {'yes' if claims['bournemouth_attacking_double_up'] else 'no'} "
            f"| {'yes' if claims['no_arsenal'] else 'no'} "
            f"| {', '.join(claims['goalkeeper_pair'])} | {claims['captain']} |"
        )

    lines += [
        "",
        "## 10. Final squad",
        "",
        f"Projection run {result['final_squad'].get('projection_run_id')} "
        f"under `{result['final_squad'].get('model_version')}`. "
        f"Cost {_money(squad['total_cost_tenths'])}, bank "
        f"{_money(squad['bank_tenths'])}. "
        f"GW1 expected {squad['gameweek_expected_points']}, "
        f"exact GW1–8 decision value {squad['decision_value']}, "
        f"linear objective {squad['solver_objective']}.",
        "",
        "| player | club | pos | price | GW1–8 xP | role |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for player in squad["players"]:
        role = "XI" if player["starts_gameweek"] else f"bench {player['bench_rank']}"
        if player["captain"]:
            role += " (C)"
        if player["vice_captain"]:
            role += " (V)"
        lines.append(
            f"| {player['web_name']} | {player['team']} | {player['position']} "
            f"| {_money(player['price_tenths'])} "
            f"| {player['horizon_expected_points']} | {role} |"
        )

    lines += ["", "## 11. Meaningful alternatives", ""]
    if result["alternatives"]:
        lines += [
            "| alternative | exact value | gap | cost | bank | changes |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for alternative in result["alternatives"]:
            lines.append(
                f"| {alternative['label']} | {alternative['decision_value']} "
                f"| {alternative['exact_value_gap']} "
                f"| {_money(alternative['total_cost_tenths'])} "
                f"| {_money(alternative['bank_tenths'])} "
                f"| {alternative['changes_from_primary']['changes']} |"
            )
    else:
        lines.append("No distinct alternative was produced.")

    stability = result["selection_stability"]
    lines += [
        "",
        "## 12. Robust and model-sensitive selections",
        "",
        f"Compared across {stability['runs_compared']} squads: the "
        "recommendation, every completed concentration run and every feasible "
        "bank level.",
        "",
        f"- Robust (in every one): {', '.join(stability['robust']) or 'none'}",
        f"- Moderate: {', '.join(stability['moderate']) or 'none'}",
        f"- Model-sensitive (in exactly one): "
        f"{', '.join(stability['model_sensitive']) or 'none'}",
        "",
        "## 13. Warnings and unresolved limitations",
        "",
    ]
    lines.extend(f"- {warning}" for warning in result["warnings"])
    lines.append("")
    return "\n".join(lines)


def write_artifacts(
    result: dict[str, Any],
    *,
    validation_path: str | Path,
    squad_path: str | Path,
    markdown_path: str | Path,
) -> tuple[Path, Path, Path]:
    paths = []
    for path, payload in (
        (Path(validation_path), result),
        (Path(squad_path), squad_artifact(result)),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    markdown = Path(markdown_path)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_markdown(result), encoding="utf-8")
    paths.append(markdown)
    return tuple(paths)


def load_final_validation(
    season_code: str, *, directory: str | Path = "data/models"
) -> dict[str, Any] | None:
    """Read the written validation artifact, or None when there is not one."""

    path = Path(directory) / f"preseason-final-validation-{season_code}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def load_final_squad(
    season_code: str, *, directory: str | Path = "data/models"
) -> dict[str, Any] | None:
    path = Path(directory) / f"preseason-final-squad-{season_code}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None
