"""One-command evidence and recommendation report for a season deadline."""

from __future__ import annotations

from typing import Any

from .config import SeasonRules
from .history.database import HistoricalDatabase
from .optimisation import (
    DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    appearance_qualified_candidates,
    mean_appearance,
    optimise_opening_squads,
)
from .production import select_production_projection_run
from .prospective import build_prospective_capture_status
from .research_decision import load_projection_candidates


def build_preseason_readiness_report(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    gameweek_number: int = 1,
    horizon_gameweeks: int = 8,
    candidate_pool_size: int = 8,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
) -> dict[str, Any]:
    """Return the qualified run, robust squad frontier and deadline blockers."""

    if rules.season != season_code:
        raise ValueError("Readiness rules must match the requested season")
    if not 0.0 <= minimum_mean_appearance <= 1.0:
        raise ValueError("Minimum mean appearance must be between zero and one")
    production = select_production_projection_run(
        database,
        season_code=season_code,
        start_gameweek=gameweek_number,
        minimum_horizon_gameweeks=horizon_gameweeks,
    )
    capture = build_prospective_capture_status(database, season_code)
    candidate_rows = database.connection.execute(
        """
        SELECT registrations.candidate_key, registrations.model_version,
               registrations.status, registrations.registered_at,
               registrations.evaluated_at
        FROM model_candidate_registrations registrations
        JOIN seasons ON seasons.id = registrations.season_id
        WHERE seasons.code = ?
        ORDER BY registrations.id
        """,
        (season_code,),
    ).fetchall()
    final_decision = database.connection.execute(
        """
        SELECT decisions.id, decisions.created_at,
               decisions.projection_run_id
        FROM weekly_decision_runs decisions
        JOIN seasons ON seasons.id = decisions.season_id
        JOIN gameweeks ON gameweeks.id = decisions.gameweek_id
        WHERE seasons.code = ? AND gameweeks.number = ?
          AND decisions.mode = 'final'
        ORDER BY decisions.id DESC
        LIMIT 1
        """,
        (season_code, gameweek_number),
    ).fetchone()
    base = {
        "season_code": season_code,
        "gameweek_number": gameweek_number,
        "requested_horizon_gameweeks": horizon_gameweeks,
        "candidate_pool_size": candidate_pool_size,
        "minimum_mean_appearance": minimum_mean_appearance,
        "forward_candidates": [dict(row) for row in candidate_rows],
        "prospective_capture_summary": capture["summary"],
        "final_decision": None if final_decision is None else dict(final_decision),
    }
    if production is None:
        return {
            **base,
            "production_projection": None,
            "ready_for_provisional_selection": False,
            "ready_to_submit": False,
            "blockers": [
                "No qualified incumbent projection covers the requested horizon."
            ],
            "recommendation": None,
        }

    all_candidates = load_projection_candidates(database, production.run_id)
    candidates = appearance_qualified_candidates(
        all_candidates,
        minimum_mean_appearance=minimum_mean_appearance,
    )
    recommendation = optimise_opening_squads(
        candidates,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
        alternative_count=2,
        candidate_pool_size=candidate_pool_size,
    )
    blockers = []
    if final_decision is None:
        blockers.append(
            "No final two-pass decision is frozen yet; re-run after the last "
            "reliable team news before submitting."
        )
    elif int(final_decision["projection_run_id"]) != production.run_id:
        blockers.append(
            "The latest qualified production projection is not the projection "
            "attached to the frozen final decision. Re-run and freeze it again."
        )
    if not capture["summary"]["no_missed_required_evidence_to_date"]:
        blockers.append("Required prospective evidence has already been missed.")
    primary = recommendation.primary
    return {
        **base,
        "production_projection": {
            "run_id": production.run_id,
            "model_version": production.model_version,
            "generated_at": production.generated_at,
            "horizon_gameweeks": production.horizon_gameweeks,
        },
        "eligible_players": len(candidates),
        "ready_for_provisional_selection": True,
        "ready_to_submit": not blockers,
        "blockers": blockers,
        "recommendation": {
            "objective": recommendation.objective,
            "assumptions": list(recommendation.assumptions),
            "transfer_triggers": list(recommendation.transfer_triggers),
            "primary": _squad_dict(primary),
            "alternatives": [
                _squad_dict(squad) for squad in recommendation.alternatives
            ],
        },
        "interpretation": [
            "This maximises the model's expected value; it cannot guarantee "
            "the highest realised points.",
            "The incumbent remains in production until a declared challenger "
            "passes its immutable forward gates.",
            "A provisional squad is useful for structure and monitoring, but the "
            "final squad must be regenerated after late injuries, roles and transfers.",
        ],
    }


def _squad_dict(squad) -> dict[str, Any]:
    bench_rank = {
        player_id: rank
        for rank, player_id in enumerate(squad.bench_player_ids, start=1)
    }
    return {
        "total_cost_tenths": squad.total_cost_tenths,
        "lineup_expected_points": squad.lineup_expected_points,
        "horizon_expected_points": squad.horizon_expected_points,
        "horizon_expected_bench_contribution": (
            squad.horizon_expected_bench_contribution
        ),
        "terminal_value": squad.terminal_value,
        "decision_value": squad.decision_value,
        "captain_id": squad.captain_id,
        "vice_captain_id": squad.vice_captain_id,
        "players": [
            {
                "source_player_id": player.source_player_id,
                "web_name": player.web_name,
                "team": player.team_short_name,
                "position": player.position.value,
                "price_tenths": player.price_tenths,
                "horizon_expected_points": round(player.expected_points, 3),
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
                    value.position.value,
                    value.web_name,
                    value.source_player_id,
                ),
            )
        ],
        "gameweek_plans": [
            {
                "gameweek_number": plan.gameweek_number,
                "starting_player_ids": sorted(plan.starting_player_ids),
                "bench_player_ids": list(plan.bench_player_ids),
                "captain_id": plan.captain_id,
                "vice_captain_id": plan.vice_captain_id,
            }
            for plan in squad.gameweek_plans
        ],
        "proof": squad.proof,
    }
