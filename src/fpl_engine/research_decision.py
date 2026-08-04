"""Research-to-decision projection reruns and auditable comparisons."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .domain import Position
from .history.database import HistoricalDatabase
from .optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    _optimal_captaincy,
    optimise_full_squad,
    optimise_opening_squads,
    optimise_starting_xi,
)
from .projections import (
    ProjectionModelConfig,
    RatesProjectionModel,
    _config_hash,
)
from .reviewed_modifiers import modifier_overrides
from .transfers import CurrentSquad, recommend_transfers


@dataclass(frozen=True)
class ResearchProjectionRun:
    baseline_projection_run_id: int
    revised_projection_run_id: int
    modifier_ids: tuple[int, ...]
    horizon_gameweeks: int


@dataclass(frozen=True)
class DecisionComparison:
    comparison_id: int
    decision_type: str
    baseline_projection_run_id: int
    revised_projection_run_id: int
    baseline_objective: float
    baseline_revalued_objective: float
    revised_objective: float
    decision_improvement: float
    projection_impact: float
    changed_players: dict[str, list[str]]
    explanations: tuple[dict[str, Any], ...]
    robustness: str


def _run_context(database: HistoricalDatabase, projection_run_id: int) -> Any:
    row = database.connection.execute(
        """
        SELECT projection_runs.*, seasons.code AS season_code
        FROM projection_runs
        JOIN seasons ON seasons.id = projection_runs.season_id
        WHERE projection_runs.id = ?
        """,
        (projection_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Projection run {projection_run_id} is unavailable")
    return row


def _config_for_run(row: Any) -> ProjectionModelConfig:
    assumptions = json.loads(row["assumptions_json"])
    config = assumptions.get("model_config")
    if not isinstance(config, dict):
        raise ValueError("Projection run does not contain its model configuration")
    return ProjectionModelConfig(**config)


def generate_revised_projection(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    baseline_projection_run_id: int,
    decision_type: str,
    input_package_id: str | None = None,
    research_run_id: str | None = None,
    generated_at: datetime | None = None,
) -> ResearchProjectionRun:
    """Rerun a baseline with current accepted modifiers and identical inputs."""

    if decision_type not in {"opening_squad", "transfers", "weekly_xi"}:
        raise ValueError("Unknown research decision type")
    baseline = _run_context(database, baseline_projection_run_id)
    generated = generated_at or datetime.now(UTC)
    if generated.tzinfo is None:
        raise ValueError("Research projection time must be timezone-aware")
    config = _config_for_run(baseline)
    overrides, provenance = modifier_overrides(
        database,
        season_code=baseline["season_code"],
        start_gameweek=int(baseline["start_gameweek"]),
        horizon_gameweeks=int(baseline["horizon_gameweeks"]),
        now=generated,
    )
    result = RatesProjectionModel(
        database,
        rules,
        config=config,
        model_version=f"{baseline['model_version']}-post-research",
    ).project(
        season_code=baseline["season_code"],
        start_gameweek=int(baseline["start_gameweek"]),
        horizon_gameweeks=int(baseline["horizon_gameweeks"]),
        overrides=overrides,
        generated_at=generated,
        observation_mode=baseline["observation_mode"],
        fixture_max_ingestion_run_id=baseline["source_ingestion_run_id"],
    )
    if result.projection_run_id is None:
        raise RuntimeError("Revised projection was not persisted")
    modifier_ids = sorted({item[1] for item in provenance})
    effective_by_modifier: dict[int, list[dict[str, Any]]] = {}
    for gameweek, modifier_id, effective in provenance:
        effective_by_modifier.setdefault(modifier_id, []).append(
            {"gameweek": gameweek, "effective": json.loads(effective)}
        )
    with database.transaction():
        database.connection.execute(
            """
            INSERT INTO research_projection_runs (
                revised_projection_run_id, baseline_projection_run_id, decision_type,
                input_package_id, research_run_id, source_ingestion_run_id,
                model_config_hash, horizon_gameweeks, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.projection_run_id,
                baseline_projection_run_id,
                decision_type,
                input_package_id,
                research_run_id,
                baseline["source_ingestion_run_id"],
                _config_hash(config),
                baseline["horizon_gameweeks"],
                generated.astimezone(UTC).isoformat(),
            ),
        )
        database.connection.executemany(
            """
            INSERT INTO projection_run_modifier_links (
                projection_run_id, modifier_id, effective_value_json
            ) VALUES (?, ?, ?)
            """,
            [
                (
                    result.projection_run_id,
                    modifier_id,
                    json.dumps(effective_by_modifier[modifier_id], sort_keys=True),
                )
                for modifier_id in modifier_ids
            ]
        )
    return ResearchProjectionRun(
        baseline_projection_run_id=baseline_projection_run_id,
        revised_projection_run_id=result.projection_run_id,
        modifier_ids=tuple(modifier_ids),
        horizon_gameweeks=int(baseline["horizon_gameweeks"]),
    )


def load_projection_candidates(
    database: HistoricalDatabase,
    projection_run_id: int,
) -> tuple[CandidatePlayer, ...]:
    """Load optimizer candidates from one exact persisted projection run."""

    run = _run_context(database, projection_run_id)
    rows = database.connection.execute(
        """
        SELECT ps.source_player_id, players.web_name, teams.source_team_id,
               teams.short_name, ps.position, observations.price_tenths,
               projections.gameweek_number, projections.expected_points,
               projections.appearance_probability, projections.sixty_probability,
               projections.uncertainty
        FROM player_gameweek_projections projections
        JOIN player_seasons ps ON ps.id = projections.player_season_id
        JOIN players ON players.id = ps.player_id
        LEFT JOIN player_gameweek_observations observations
          ON observations.player_season_id = ps.id
         AND observations.gameweek_id = (
             SELECT id FROM gameweeks
             WHERE season_id = ps.season_id AND number = projections.gameweek_number
         )
         AND observations.provenance_run_id = ?
        JOIN teams ON teams.id = ps.team_id
        WHERE projections.projection_run_id = ?
        ORDER BY ps.source_player_id, projections.gameweek_number
        """,
        (run["source_ingestion_run_id"], projection_run_id),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        player_id = str(row["source_player_id"])
        item = grouped.setdefault(
            player_id,
            {
                "web_name": row["web_name"],
                "team_id": str(row["source_team_id"]),
                "team_short_name": row["short_name"],
                "position": Position(row["position"]),
                "price_tenths": int(row["price_tenths"] or 0),
                "values": [],
                "uncertainty": 0.0,
            },
        )
        item["values"].append(
            GameweekPlayerValue(
                gameweek_number=int(row["gameweek_number"]),
                expected_points=float(row["expected_points"]),
                appearance_probability=float(row["appearance_probability"]),
                sixty_probability=float(row["sixty_probability"]),
            )
        )
        item["uncertainty"] += float(row["uncertainty"])
    return tuple(
        CandidatePlayer(
            source_player_id=player_id,
            web_name=str(item["web_name"]),
            team_id=str(item["team_id"]),
            team_short_name=str(item["team_short_name"]),
            position=item["position"],
            price_tenths=int(item["price_tenths"]),
            expected_points=sum(value.expected_points for value in item["values"]),
            gameweek_expected_points=item["values"][0].expected_points,
            appearance_probability=item["values"][0].appearance_probability,
            uncertainty=float(item["uncertainty"]),
            gameweek_values=tuple(item["values"]),
        )
        for player_id, item in sorted(grouped.items())
    )


def _recommendation_json(result: Any) -> dict[str, Any]:
    return {
        "players": [player.source_player_id for player in result.players],
        "starting_player_ids": sorted(result.starting_player_ids),
        "bench_player_ids": list(result.bench_player_ids),
        "captain_id": result.captain_id,
        "vice_captain_id": result.vice_captain_id,
        "horizon_expected_points": result.horizon_expected_points,
        "gameweek_expected_points": result.gameweek_expected_points,
        "total_cost_tenths": result.total_cost_tenths,
    }


def compare_opening_squad_decision(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    baseline_projection_run_id: int,
    revised_projection_run_id: int,
    modifier_ids: tuple[int, ...] = (),
) -> DecisionComparison:
    """Rerun opening-squad optimisation and persist a before/after comparison."""

    baseline_candidates = load_projection_candidates(database, baseline_projection_run_id)
    revised_candidates = load_projection_candidates(database, revised_projection_run_id)
    baseline = optimise_opening_squads(
        baseline_candidates,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
        alternative_count=2,
    ).primary
    revised = optimise_opening_squads(
        revised_candidates,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
        alternative_count=2,
    ).primary
    revised_by_id = {player.source_player_id: player for player in revised_candidates}
    baseline_revalued = 0.0
    for plan in baseline.gameweek_plans:
        values = {
            player_id: next(
                value.expected_points
                for value in revised_by_id[player_id].gameweek_values
                if value.gameweek_number == plan.gameweek_number
            )
            for player_id in plan.starting_player_ids
        }
        baseline_revalued += sum(values.values())
        baseline_revalued += next(
            value.expected_points
            for value in revised_by_id[plan.captain_id].gameweek_values
            if value.gameweek_number == plan.gameweek_number
        )
    baseline_ids = {player.source_player_id for player in baseline.players}
    revised_ids = {player.source_player_id for player in revised.players}
    added = sorted(revised_ids - baseline_ids)
    removed = sorted(baseline_ids - revised_ids)
    decision_improvement = round(revised.horizon_expected_points - baseline_revalued, 6)
    projection_impact = round(baseline_revalued - baseline.horizon_expected_points, 6)
    robustness = (
        "near_tie" if abs(decision_improvement) < 0.5 else
        "moderate" if abs(decision_improvement) < 2.0 else "robust"
    )
    explanations = tuple(
        {
            "source_player_id": player_id,
            "change": "added" if player_id in added else "removed",
            "baseline_expected_points": next(
                player.expected_points
                for player in baseline_candidates
                if player.source_player_id == player_id
            ),
            "revised_expected_points": next(
                player.expected_points
                for player in revised_candidates
                if player.source_player_id == player_id
            ),
            "modifier_ids": list(modifier_ids),
        }
        for player_id in [*added, *removed]
    )
    cursor = database.connection.execute(
        """
        INSERT INTO research_decision_comparisons (
            decision_type, baseline_projection_run_id, revised_projection_run_id,
            baseline_recommendation_json, revised_recommendation_json,
            baseline_objective, baseline_revalued_objective, revised_objective,
            decision_improvement, projection_impact, changed_players_json,
            explanations_json, modifier_ids_json, robustness, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            "opening_squad",
            baseline_projection_run_id,
            revised_projection_run_id,
            json.dumps(_recommendation_json(baseline), sort_keys=True),
            json.dumps(_recommendation_json(revised), sort_keys=True),
            baseline.horizon_expected_points,
            baseline_revalued,
            revised.horizon_expected_points,
            decision_improvement,
            projection_impact,
            json.dumps({"added": added, "removed": removed}),
            json.dumps(explanations),
            json.dumps(modifier_ids),
            robustness,
            datetime.now(UTC).isoformat(),
        ),
    )
    comparison_id = int(cursor.fetchone()[0])
    database.connection.commit()
    return DecisionComparison(
        comparison_id=comparison_id,
        decision_type="opening_squad",
        baseline_projection_run_id=baseline_projection_run_id,
        revised_projection_run_id=revised_projection_run_id,
        baseline_objective=baseline.horizon_expected_points,
        baseline_revalued_objective=baseline_revalued,
        revised_objective=revised.horizon_expected_points,
        decision_improvement=decision_improvement,
        projection_impact=projection_impact,
        changed_players={"added": added, "removed": removed},
        explanations=explanations,
        robustness=robustness,
    )


def _persist_comparison(
    database: HistoricalDatabase,
    *,
    decision_type: str,
    baseline_projection_run_id: int,
    revised_projection_run_id: int,
    baseline_recommendation: dict[str, Any],
    revised_recommendation: dict[str, Any],
    baseline_objective: float,
    baseline_revalued: float,
    revised_objective: float,
    changed_players: dict[str, list[str]],
    explanations: tuple[dict[str, Any], ...],
    modifier_ids: tuple[int, ...],
) -> DecisionComparison:
    decision_improvement = round(revised_objective - baseline_revalued, 6)
    projection_impact = round(baseline_revalued - baseline_objective, 6)
    robustness = (
        "near_tie"
        if abs(decision_improvement) < 0.5
        else "moderate"
        if abs(decision_improvement) < 2.0
        else "robust"
    )
    cursor = database.connection.execute(
        """
        INSERT INTO research_decision_comparisons (
            decision_type, baseline_projection_run_id, revised_projection_run_id,
            baseline_recommendation_json, revised_recommendation_json,
            baseline_objective, baseline_revalued_objective, revised_objective,
            decision_improvement, projection_impact, changed_players_json,
            explanations_json, modifier_ids_json, robustness, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            decision_type,
            baseline_projection_run_id,
            revised_projection_run_id,
            json.dumps(baseline_recommendation, sort_keys=True),
            json.dumps(revised_recommendation, sort_keys=True),
            baseline_objective,
            baseline_revalued,
            revised_objective,
            decision_improvement,
            projection_impact,
            json.dumps(changed_players, sort_keys=True),
            json.dumps(explanations, sort_keys=True),
            json.dumps(modifier_ids),
            robustness,
            datetime.now(UTC).isoformat(),
        ),
    )
    comparison_id = int(cursor.fetchone()[0])
    database.connection.commit()
    return DecisionComparison(
        comparison_id=comparison_id,
        decision_type=decision_type,
        baseline_projection_run_id=baseline_projection_run_id,
        revised_projection_run_id=revised_projection_run_id,
        baseline_objective=round(baseline_objective, 6),
        baseline_revalued_objective=round(baseline_revalued, 6),
        revised_objective=round(revised_objective, 6),
        decision_improvement=decision_improvement,
        projection_impact=projection_impact,
        changed_players=changed_players,
        explanations=explanations,
        robustness=robustness,
    )


def _weekly_candidates(
    candidates: tuple[CandidatePlayer, ...], gameweek_number: int
) -> tuple[CandidatePlayer, ...]:
    result = []
    for candidate in candidates:
        value = next(
            (
                value
                for value in candidate.gameweek_values
                if value.gameweek_number == gameweek_number
            ),
            None,
        )
        if value is None:
            continue
        result.append(
            CandidatePlayer(
                source_player_id=candidate.source_player_id,
                web_name=candidate.web_name,
                team_id=candidate.team_id,
                team_short_name=candidate.team_short_name,
                position=candidate.position,
                price_tenths=candidate.price_tenths,
                expected_points=value.expected_points,
                gameweek_expected_points=value.expected_points,
                appearance_probability=value.appearance_probability,
                uncertainty=candidate.uncertainty,
                residual_value=candidate.residual_value,
                gameweek_values=(value,),
            )
        )
    return tuple(result)


def compare_weekly_xi_decision(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    baseline_projection_run_id: int,
    revised_projection_run_id: int,
    gameweek_number: int | None = None,
    modifier_ids: tuple[int, ...] = (),
) -> DecisionComparison:
    """Compare one exact XI/bench/captain decision for the target Gameweek."""

    baseline_run = _run_context(database, baseline_projection_run_id)
    target_gameweek = int(gameweek_number or baseline_run["start_gameweek"])
    baseline_candidates = _weekly_candidates(
        load_projection_candidates(database, baseline_projection_run_id), target_gameweek
    )
    revised_candidates = _weekly_candidates(
        load_projection_candidates(database, revised_projection_run_id), target_gameweek
    )
    baseline_xi = optimise_starting_xi(
        baseline_candidates, budget_tenths=rules.squad.budget_tenths, rules=rules
    )
    revised_xi = optimise_starting_xi(
        revised_candidates, budget_tenths=rules.squad.budget_tenths, rules=rules
    )
    baseline_points = {p.source_player_id: p.expected_points for p in baseline_xi.players}
    revised_by_id = {p.source_player_id: p for p in revised_candidates}
    revised_points = {p.source_player_id: p.expected_points for p in revised_xi.players}
    baseline_appearance = {
        p.source_player_id: p.appearance_probability for p in baseline_xi.players
    }
    revised_appearance = {
        p.source_player_id: p.appearance_probability for p in revised_xi.players
    }
    baseline_captain, baseline_vice = _optimal_captaincy(
        baseline_xi.players, points=baseline_points, appearance=baseline_appearance
    )
    revised_captain, revised_vice = _optimal_captaincy(
        revised_xi.players, points=revised_points, appearance=revised_appearance
    )
    baseline_objective = baseline_xi.expected_points + baseline_points[baseline_captain]
    baseline_revalued = sum(
        revised_by_id[player_id].expected_points
        for player_id in (p.source_player_id for p in baseline_xi.players)
    ) + revised_by_id[baseline_captain].expected_points
    revised_objective = revised_xi.expected_points + revised_points[revised_captain]
    baseline_ids = {p.source_player_id for p in baseline_xi.players}
    revised_ids = {p.source_player_id for p in revised_xi.players}
    added = sorted(revised_ids - baseline_ids)
    removed = sorted(baseline_ids - revised_ids)
    changed = {"added": added, "removed": removed}
    explanations = tuple(
        {
            "source_player_id": player_id,
            "change": "added" if player_id in added else "removed",
            "baseline_expected_points": next(
                (p.expected_points for p in baseline_candidates if p.source_player_id == player_id),
                None,
            ),
            "revised_expected_points": next(
                (p.expected_points for p in revised_candidates if p.source_player_id == player_id),
                None,
            ),
            "modifier_ids": list(modifier_ids),
        }
        for player_id in [*added, *removed]
    )
    if baseline_captain != revised_captain or baseline_vice != revised_vice:
        changed["captaincy"] = [baseline_captain, baseline_vice, revised_captain, revised_vice]
    return _persist_comparison(
        database,
        decision_type="weekly_xi",
        baseline_projection_run_id=baseline_projection_run_id,
        revised_projection_run_id=revised_projection_run_id,
        baseline_recommendation={
            "players": sorted(baseline_ids),
            "captain_id": baseline_captain,
            "vice_captain_id": baseline_vice,
        },
        revised_recommendation={
            "players": sorted(revised_ids),
            "captain_id": revised_captain,
            "vice_captain_id": revised_vice,
        },
        baseline_objective=baseline_objective,
        baseline_revalued=baseline_revalued,
        revised_objective=revised_objective,
        changed_players=changed,
        explanations=explanations,
        modifier_ids=modifier_ids,
    )


def compare_transfer_decision(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    baseline_projection_run_id: int,
    revised_projection_run_id: int,
    current_squad: CurrentSquad,
    modifier_ids: tuple[int, ...] = (),
) -> DecisionComparison:
    """Compare transfer routes while preserving the exact manager state."""

    baseline_candidates = load_projection_candidates(database, baseline_projection_run_id)
    revised_candidates = load_projection_candidates(database, revised_projection_run_id)
    baseline = recommend_transfers(baseline_candidates, current_squad, rules=rules)
    revised = recommend_transfers(revised_candidates, current_squad, rules=rules)
    baseline_ids = {p.source_player_id for p in baseline.primary.resulting_squad.players}
    revised_ids = {p.source_player_id for p in revised.primary.resulting_squad.players}
    revised_by_id = {p.source_player_id: p for p in revised_candidates}
    baseline_selected_revised = tuple(
        revised_by_id[player_id] for player_id in baseline_ids if player_id in revised_by_id
    )
    baseline_revalued_result = optimise_full_squad(
        baseline_selected_revised,
        budget_tenths=sum(p.price_tenths for p in baseline_selected_revised),
        rules=rules,
    )
    added = sorted(revised_ids - baseline_ids)
    removed = sorted(baseline_ids - revised_ids)
    explanations = tuple(
        {
            "source_player_id": player_id,
            "change": "added" if player_id in added else "removed",
            "baseline_expected_points": next(
                p.expected_points for p in baseline_candidates if p.source_player_id == player_id
            ),
            "revised_expected_points": next(
                p.expected_points for p in revised_candidates if p.source_player_id == player_id
            ),
            "modifier_ids": list(modifier_ids),
        }
        for player_id in [*added, *removed]
    )
    return _persist_comparison(
        database,
        decision_type="transfers",
        baseline_projection_run_id=baseline_projection_run_id,
        revised_projection_run_id=revised_projection_run_id,
        baseline_recommendation={
            "transfers_in": [p.source_player_id for p in baseline.primary.transfers_in],
            "transfers_out": [p.source_player_id for p in baseline.primary.transfers_out],
            "route_score": baseline.primary.route_score,
        },
        revised_recommendation={
            "transfers_in": [p.source_player_id for p in revised.primary.transfers_in],
            "transfers_out": [p.source_player_id for p in revised.primary.transfers_out],
            "route_score": revised.primary.route_score,
        },
        baseline_objective=baseline.primary.resulting_squad.horizon_expected_points,
        baseline_revalued=baseline_revalued_result.horizon_expected_points,
        revised_objective=revised.primary.resulting_squad.horizon_expected_points,
        changed_players={"added": added, "removed": removed},
        explanations=explanations,
        modifier_ids=modifier_ids,
    )
