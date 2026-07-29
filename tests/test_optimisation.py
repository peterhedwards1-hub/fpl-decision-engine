from __future__ import annotations

from collections import Counter
from itertools import combinations
from pathlib import Path

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    optimise_full_squad,
    optimise_opening_squads,
    optimise_starting_xi,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def _candidates() -> tuple[CandidatePlayer, ...]:
    positions = (
        Position.GK,
        Position.GK,
        Position.DEF,
        Position.DEF,
        Position.DEF,
        Position.DEF,
        Position.DEF,
        Position.MID,
        Position.MID,
        Position.MID,
        Position.MID,
        Position.MID,
        Position.FWD,
        Position.FWD,
        Position.FWD,
        Position.FWD,
    )
    return tuple(
        CandidatePlayer(
            source_player_id=str(index),
            web_name=f"Player {index}",
            team_id=str((index - 1) % 6 + 1),
            team_short_name=f"T{(index - 1) % 6 + 1}",
            position=position,
            price_tenths=40 + (index % 5) * 5,
            expected_points=30.0 + index * 1.7,
        )
        for index, position in enumerate(positions, start=1)
    )


def test_starting_xi_solver_matches_exhaustive_optimum() -> None:
    candidates = _candidates()
    budget = 600
    result = optimise_starting_xi(candidates, budget_tenths=budget, rules=RULES)

    feasible = []
    for players in combinations(candidates, RULES.squad.starting_size):
        positions = Counter(player.position.value for player in players)
        teams = Counter(player.team_id for player in players)
        if sum(player.price_tenths for player in players) > budget:
            continue
        if any(
            not RULES.squad.formation_min[position]
            <= positions[position]
            <= RULES.squad.formation_max[position]
            for position in RULES.squad.formation_min
        ):
            continue
        if max(teams.values()) > RULES.squad.max_players_per_team:
            continue
        feasible.append(sum(player.expected_points for player in players))

    assert result.solver_status == "Optimal"
    assert result.expected_points == round(max(feasible), 3)
    assert len(result.players) == 11
    assert result.total_cost_tenths <= budget
    assert "CBC returned Optimal" in result.proof
    repeated = optimise_starting_xi(
        candidates, budget_tenths=budget, rules=RULES
    )
    assert repeated.players == result.players


def test_full_squad_returns_legal_lineup_bench_and_captaincy() -> None:
    positions = (
        *(Position.GK for _ in range(3)),
        *(Position.DEF for _ in range(7)),
        *(Position.MID for _ in range(7)),
        *(Position.FWD for _ in range(5)),
    )
    candidates = tuple(
        CandidatePlayer(
            source_player_id=str(index),
            web_name=f"Player {index}",
            team_id=str((index - 1) % 8 + 1),
            team_short_name=f"T{(index - 1) % 8 + 1}",
            position=position,
            price_tenths=40 + (index % 7) * 5,
            expected_points=35 + index,
            gameweek_expected_points=3 + index / 10,
            appearance_probability=0.85 + (index % 3) * 0.05,
        )
        for index, position in enumerate(positions, start=1)
    )

    result = optimise_full_squad(
        candidates,
        budget_tenths=1000,
        rules=RULES,
    )

    assert result.solver_status == "Optimal"
    assert len(result.players) == 15
    assert len(result.starting_player_ids) == 11
    assert len(result.bench_player_ids) == 4
    assert result.captain_id in result.starting_player_ids
    assert result.vice_captain_id in result.starting_player_ids
    assert result.captain_id != result.vice_captain_id
    selected = {player.source_player_id: player for player in result.players}
    assert selected[result.bench_player_ids[0]].position == Position.GK
    assert result.total_cost_tenths <= 1000
    assert result.gameweek_expected_points > 0
    assert result.expected_bench_contribution >= 0
    assert "2^15" in result.proof

    opening = optimise_opening_squads(
        candidates,
        budget_tenths=1000,
        rules=RULES,
        alternative_count=1,
    )
    primary_ids = {
        player.source_player_id for player in opening.primary.players
    }
    alternative_ids = {
        player.source_player_id for player in opening.alternatives[0].players
    }
    assert primary_ids != alternative_ids
    assert "uncertainty" in opening.objective
    assert opening.transfer_triggers
