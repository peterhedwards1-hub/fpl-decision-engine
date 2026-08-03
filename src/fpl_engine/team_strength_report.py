"""Reporting and historical evaluation for the team-strength models.

Two jobs. The report explains one origin: every club's rating, what produced
it, and how the rivals would have ranked it. The evaluation scores the rating
itself — how close its expected goals came to the goals actually scored, and
whether the clean-sheet probabilities it implies are calibrated — broken down
the ways that matter, because an average over a whole season hides exactly the
cases the new model exists to fix.

Historical seasons are design evidence. Nothing here qualifies anything: the
2026/27 forward captures do that.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .history.database import HistoricalDatabase
from .projections import (
    CORRECTED_V4_MODEL_CONFIG,
    OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
    PRESEASON_V5_MODEL_CONFIG,
    TEAM_SHARE_XG_V5_MODEL_CONFIG,
    ProjectionModelConfig,
    RatesProjectionModel,
)
from .team_strength import (
    ContextualAdjustment,
    TeamStrengthSettings,
    TeamStrengthState,
    estimate_team_strength,
)

#: The three rivals the challenger has to be explained against.
COMPARISON_MODELS: dict[str, ProjectionModelConfig] = {
    # The production incumbent: raw goals for and against, shrunk toward the
    # league average, with no carry-forward. Before the season every club is
    # identical, which is the defect this whole exercise exists to address.
    "flat_preseason": CORRECTED_V4_MODEL_CONFIG,
    # Raw previous-season goal rates carried across by club name.
    "raw_goals_carry_forward": PRESEASON_V5_MODEL_CONFIG,
    # The isolated expected-goal path: decayed team xG, but with no opponent
    # adjustment and no preseason prior of its own.
    "team_share_expected_goals": TEAM_SHARE_XG_V5_MODEL_CONFIG,
}


def build_team_strength_report(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    gameweek_number: int,
    settings: TeamStrengthSettings | None = None,
    adjustments: tuple[ContextualAdjustment, ...] = (),
    as_of: datetime | None = None,
    maximum_ingestion_run_id: int | None = None,
) -> dict[str, Any]:
    """Explain every club's rating at one forecast origin."""

    state = estimate_team_strength(
        database,
        season_code=season_code,
        gameweek_number=gameweek_number,
        settings=settings,
        adjustments=adjustments,
        as_of=as_of,
        maximum_ingestion_run_id=maximum_ingestion_run_id,
    )
    rivals = {
        name: _rival_strengths(
            database,
            rules,
            config=config,
            season_code=season_code,
            gameweek_number=gameweek_number,
            as_of=as_of,
            maximum_ingestion_run_id=maximum_ingestion_run_id,
        )
        for name, config in COMPARISON_MODELS.items()
    }
    rival_ranks = {
        name: _ranks(
            {team_id: float(entry["attack"]) for team_id, entry in values.items()}
        )
        for name, values in rivals.items()
    }
    report = state.as_dict()
    for entry in report["teams"]:
        entry["comparison"] = {
            name: _rival_entry(
                entry["team_id"], rivals[name], rival_ranks[name]
            )
            for name in rivals
        }
    report["comparison_agreement"] = {
        name: _rank_agreement(state, values) for name, values in rivals.items()
    }
    report["comparison_models"] = {
        name: {
            "team_strength_model": config.team_strength_model,
            "team_strength_carry_forward": config.team_strength_carry_forward,
            "scoring_event_source": config.scoring_event_source,
        }
        for name, config in COMPARISON_MODELS.items()
    }
    return report


def _rival_strengths(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    config: ProjectionModelConfig,
    season_code: str,
    gameweek_number: int,
    as_of: datetime | None,
    maximum_ingestion_run_id: int | None,
) -> dict[str, dict[str, float]]:
    model = RatesProjectionModel(database, rules, config=config)
    return model._team_strengths(
        season_code,
        gameweek_number,
        (),
        as_of=as_of,
        maximum_ingestion_run_id=maximum_ingestion_run_id,
    )


def _rival_entry(
    team_id: str,
    rival: dict[str, dict[str, float]],
    ranks: dict[str, int],
) -> dict[str, Any] | None:
    values = rival.get(team_id)
    if values is None:
        return None
    return {
        "attack": round(float(values["attack"]), 6),
        "defence": round(float(values["defence"]), 6),
        "attack_rank": ranks[team_id],
    }


def _rank_agreement(
    state: TeamStrengthState,
    rival: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Spearman rank correlation on attack, plus the largest disagreement.

    A correlation near one means the new model is re-describing what the rival
    already said; a low one means the two would pick different squads, which
    is the case worth reading the rationale for.
    """

    shared = [team_id for team_id in state.teams if team_id in rival]
    if len(shared) < 3:
        return {"teams": len(shared), "attack_rank_correlation": None}
    spread = {round(float(rival[team_id]["attack"]), 9) for team_id in shared}
    if len(spread) < 2:
        # Before the season the flat model puts every club on the same
        # multiplier. Ranking those is an alphabetical tie-break, and
        # correlating against it would report a number that means nothing.
        return {
            "teams": len(shared),
            "attack_rank_correlation": None,
            "note": (
                "The comparison model gives every club the same attack "
                "multiplier at this origin, so it has no ranking to compare."
            ),
        }
    ours = _ranks({team_id: state.teams[team_id].attack for team_id in shared})
    theirs = _ranks({team_id: float(rival[team_id]["attack"]) for team_id in shared})
    count = len(shared)
    squared = sum((ours[team_id] - theirs[team_id]) ** 2 for team_id in shared)
    correlation = 1.0 - (6.0 * squared) / (count * (count**2 - 1))
    worst = max(shared, key=lambda team_id: abs(ours[team_id] - theirs[team_id]))
    return {
        "teams": count,
        "attack_rank_correlation": round(correlation, 4),
        "largest_disagreement": {
            "name": state.teams[worst].name,
            "our_rank": ours[worst],
            "their_rank": theirs[worst],
            "rationale": list(state.teams[worst].rationale),
        },
    }


def _ranks(values: dict[str, float]) -> dict[str, int]:
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    return {team_id: index for index, (team_id, _) in enumerate(ordered, start=1)}


# --------------------------------------------------------------------------
# Historical evaluation
# --------------------------------------------------------------------------


#: The challenger, alongside the three rivals, all scored the same way.
EVALUATED_MODELS: dict[str, ProjectionModelConfig] = {
    "opponent_adjusted": OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
    **COMPARISON_MODELS,
}


def evaluate_team_strength_models(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    origin_gameweek_start: int = 1,
    origin_gameweek_end: int = 38,
    settings: TeamStrengthSettings | None = None,
    models: dict[str, ProjectionModelConfig] | None = None,
) -> dict[str, Any]:
    """Score the rating against what the fixtures actually produced.

    Every model — the challenger included — is asked through
    `RatesProjectionModel.fixture_expected_goals`, the same public component
    the live forecast uses. That matters more than it sounds: recreating the
    fixture expectation here with a parallel copy of the venue constants would
    mean scoring a model nothing ever runs, and the away factor alone differs
    by eight per cent between the two constant sets.

    For every origin the model answers one question with a checkable answer:
    how many goals will the home side score, and the away side? Each fixture
    contributes two predictions and two outcomes. Clean-sheet calibration comes
    free — the probability of conceding none is `exp(-lambda)` under the same
    Poisson the projection uses — so the defensive side of the rating is tested
    without touching the player model at all.

    That is what isolates the question the spec insists on separating: whether
    a gain comes from better team totals or merely from a different player
    allocation. Nothing here reads a player projection.
    """

    fixtures = _fixture_outcomes(database, season_code)
    if not fixtures:
        raise ValueError(f"Season {season_code!r} has no completed fixtures")
    origins = sorted(
        {
            gameweek
            for gameweek in (fixture["gameweek_number"] for fixture in fixtures)
            if origin_gameweek_start <= gameweek <= origin_gameweek_end
        }
    )
    if not origins:
        raise ValueError("No completed Gameweeks fall inside the origin range")
    last_origin = max(origins)
    evaluated = models or EVALUATED_MODELS

    predictions: dict[str, list[dict[str, Any]]] = {name: [] for name in evaluated}
    context: dict[int, dict[str, Any]] = {}
    for origin in origins:
        state = estimate_team_strength(
            database,
            season_code=season_code,
            gameweek_number=origin,
            settings=settings,
        )
        # Slice labels come from one estimator for every model, so a club is
        # in the same bucket whichever model is being scored.
        context[origin] = {
            team_id: {
                "is_promoted": team.is_promoted,
                "schedule_strength": team.schedule_strength,
                "continuity": (
                    None
                    if team.squad_continuity is None
                    else team.squad_continuity.continuity_score
                ),
            }
            for team_id, team in state.teams.items()
        }
        for name, config in evaluated.items():
            model = RatesProjectionModel(
                database,
                rules,
                config=config,
                team_strength_settings=settings,
            )
            predictions[name].extend(
                _fixture_predictions(
                    model.fixture_expected_goals(
                        season_code=season_code,
                        gameweek_number=origin,
                    ),
                    origin=origin,
                    last_origin=last_origin,
                    context=context[origin],
                )
            )
    return {
        "season_code": season_code,
        "origins": origins,
        "generated_at": datetime.now(UTC).isoformat(),
        "models": {
            name: _score(rows) for name, rows in predictions.items() if rows
        },
        "model_configurations": {
            name: {
                "team_strength_model": config.team_strength_model,
                "team_strength_carry_forward": config.team_strength_carry_forward,
                "scoring_event_source": config.scoring_event_source,
                "home_attack_multiplier": config.home_attack_multiplier,
                "away_attack_multiplier": config.away_attack_multiplier,
            }
            for name, config in evaluated.items()
        },
        "limitations": (
            "Every model is scored through the same public projection "
            "component the live forecast uses, so these expectations are the "
            "ones a projection at that origin would have applied.",
            "Team-goal accuracy is measured against realised goals, which are "
            "a noisy draw from the expectation being scored. A better model "
            "shows a smaller RMSE only in aggregate; single fixtures prove "
            "nothing.",
            "Clean-sheet calibration assumes goals conceded are Poisson and "
            "independent of goals scored, which is the same assumption the "
            "projection makes. It tests the rating, not that assumption.",
            "Player-points accuracy, squad regret and captain regret are not "
            "computed here. Run evaluate-allocation-variants for those; "
            "keeping them apart is what lets a gain in team totals be told "
            "apart from a change in allocation.",
            "Historical seasons are design evidence only. Forward 2026/27 "
            "captures are the qualification.",
        ),
    }


def _fixture_outcomes(
    database: HistoricalDatabase, season_code: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in database.connection.execute(
            """
            SELECT gameweeks.number AS gameweek_number,
                   fixtures.home_team_id, fixtures.away_team_id,
                   fixtures.home_score, fixtures.away_score
            FROM fixtures
            JOIN seasons ON seasons.id = fixtures.season_id
            JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
            WHERE seasons.code = ?
              AND fixtures.finished = 1
              AND fixtures.home_score IS NOT NULL
              AND fixtures.away_score IS NOT NULL
            ORDER BY gameweeks.number, fixtures.id
            """,
            (season_code,),
        )
    ]


def _fixture_predictions(
    fixtures: list[dict[str, Any]],
    *,
    origin: int,
    last_origin: int,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn one model's fixture expectations into scored, labelled rows."""

    rows = []
    for fixture in fixtures:
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
            entry = context.get(team, {})
            continuity = entry.get("continuity")
            rows.append(
                {
                    "expected": expected,
                    "actual": actual,
                    # The opponent's clean sheet is decided by these goals, so
                    # one expectation drives both sides of the ledger.
                    "clean_sheet_probability": math.exp(-expected),
                    "clean_sheet": 1.0 if actual == 0 else 0.0,
                    "phase": _phase(origin, last_origin),
                    "promotion": (
                        "promoted" if entry.get("is_promoted") else "established"
                    ),
                    # How hard a run the club has had so far, not how hard this
                    # one fixture is: the question is whether a favourable
                    # early schedule has left the rating overstated.
                    "schedule": (
                        "hard"
                        if float(entry.get("schedule_strength", 1.0)) >= 1.0
                        else "easy"
                    ),
                    "turnover": (
                        "unknown"
                        if continuity is None
                        else "small" if continuity >= 0.7 else "large"
                    ),
                    "venue": venue,
                    "origin": origin,
                }
            )
    return rows


def _phase(origin: int, last_origin: int) -> str:
    if last_origin <= 2:
        return "early"
    third = last_origin / 3.0
    if origin <= third:
        return "early"
    return "middle" if origin <= 2 * third else "late"


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    breakdowns = {
        name: {
            key: _metrics([row for row in rows if row[name] == key])
            for key in sorted({row[name] for row in rows})
        }
        for name in ("phase", "promotion", "schedule", "turnover", "venue")
    }
    # Horizon is one Gameweek by construction here: the rating is re-estimated
    # at every origin. The breakdown is kept so the shape matches the rest of
    # the evaluation suite and so the limitation is stated rather than implied.
    breakdowns["horizon"] = {"1": _metrics(rows)}
    return {
        "overall": _metrics(rows),
        "clean_sheet_calibration": _calibration(rows),
        "breakdowns": breakdowns,
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"observations": 0}
    errors = [row["expected"] - row["actual"] for row in rows]
    count = len(errors)
    return {
        "observations": count,
        "rmse": round(math.sqrt(sum(value**2 for value in errors) / count), 4),
        "mae": round(sum(abs(value) for value in errors) / count, 4),
        # Positive bias means the model expects more goals than were scored.
        "bias": round(sum(errors) / count, 4),
        "mean_expected": round(
            sum(row["expected"] for row in rows) / count, 4
        ),
        "mean_actual": round(sum(row["actual"] for row in rows) / count, 4),
    }


def _calibration(rows: list[dict[str, Any]], buckets: int = 5) -> dict[str, Any]:
    """Reliability of the implied clean-sheet probabilities."""

    if not rows:
        return {"observations": 0, "buckets": []}
    table = []
    brier = 0.0
    for index in range(buckets):
        low = index / buckets
        high = (index + 1) / buckets
        band = [
            row
            for row in rows
            if low <= row["clean_sheet_probability"] < high
            or (index == buckets - 1 and row["clean_sheet_probability"] == 1.0)
        ]
        if not band:
            continue
        table.append(
            {
                "band": f"{low:.1f}-{high:.1f}",
                "observations": len(band),
                "predicted": round(
                    sum(row["clean_sheet_probability"] for row in band)
                    / len(band),
                    4,
                ),
                "observed": round(
                    sum(row["clean_sheet"] for row in band) / len(band), 4
                ),
            }
        )
    for row in rows:
        brier += (row["clean_sheet_probability"] - row["clean_sheet"]) ** 2
    return {
        "observations": len(rows),
        "brier_score": round(brier / len(rows), 4),
        "buckets": table,
    }
