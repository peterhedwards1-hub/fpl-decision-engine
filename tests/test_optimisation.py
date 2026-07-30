from __future__ import annotations

from collections import Counter
from itertools import combinations, permutations
from pathlib import Path

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    _expected_weekly_score,
    _optimal_captaincy,
    _used_outfield_bench_indexes,
    optimise_full_squad,
    optimise_opening_squads,
    optimise_starting_xi,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))

# The legal-XI-plus-captain objective ignores bench composition entirely, so
# every legal bench ties at the optimum. These candidates make that degeneracy
# explicit: eleven clearly superior starters, then a matched cheap and strong
# option for each of the four bench slots, with the budget to afford either.
_STAR_SHAPE = (
    Position.GK,
    *(Position.DEF for _ in range(3)),
    *(Position.MID for _ in range(4)),
    *(Position.FWD for _ in range(3)),
)
_BENCH_SHAPE = (Position.GK, Position.DEF, Position.DEF, Position.MID)


def _degenerate_bench_candidates() -> tuple[CandidatePlayer, ...]:
    def player(
        identifier: str,
        position: Position,
        price_tenths: int,
        points: float,
        appearance_probability: float,
    ) -> CandidatePlayer:
        return CandidatePlayer(
            source_player_id=identifier,
            web_name=f"Player {identifier}",
            team_id=identifier,
            team_short_name=f"T{identifier}",
            position=position,
            price_tenths=price_tenths,
            expected_points=points,
            gameweek_expected_points=points,
            appearance_probability=appearance_probability,
        )

    candidates = [
        player(
            f"star{index:02d}",
            position,
            70,
            6.0 + index * 0.1,
            0.90 - index * 0.01,
        )
        for index, position in enumerate(_STAR_SHAPE)
    ]
    for index, position in enumerate(_BENCH_SHAPE):
        candidates.append(
            player(f"cheap{index:02d}", position, 40, 0.1, 1.0)
        )
        candidates.append(
            player(f"strong{index:02d}", position, 55, 3.0, 1.0)
        )
    # A second strong option per outfield bench slot, so the solver has a real
    # choice rather than a single forced completion.
    for index, position in enumerate(_BENCH_SHAPE):
        candidates.append(
            player(f"spare{index:02d}", position, 55, 2.9, 1.0)
        )
    return tuple(candidates)


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
    assert len(result.gameweek_plans) == 1
    assert result.gameweek_plans[0].starting_player_ids == (
        result.starting_player_ids
    )
    selected = {player.source_player_id: player for player in result.players}
    assert selected[result.bench_player_ids[0]].position == Position.GK
    assert result.total_cost_tenths <= 1000
    assert result.gameweek_expected_points > 0
    assert result.expected_bench_contribution >= 0
    assert "exactly integrates" in result.proof

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
    assert "legal-XI" in opening.objective
    assert opening.transfer_triggers


def test_full_squad_uses_the_callers_budget_above_initial_team_value() -> None:
    positions = (
        *(Position.GK for _ in range(2)),
        *(Position.DEF for _ in range(5)),
        *(Position.MID for _ in range(5)),
        *(Position.FWD for _ in range(3)),
    )
    candidates = tuple(
        CandidatePlayer(
            source_player_id=str(index),
            web_name=f"Player {index}",
            team_id=str((index - 1) % 5 + 1),
            team_short_name=f"T{(index - 1) % 5 + 1}",
            position=position,
            price_tenths=77 if index == 1 else 66,
            expected_points=30 + index,
            gameweek_expected_points=2 + index / 10,
            appearance_probability=1.0,
        )
        for index, position in enumerate(positions, start=1)
    )

    result = optimise_full_squad(
        candidates,
        budget_tenths=1001,
        rules=RULES,
    )

    assert result.total_cost_tenths == 1001


def test_vice_captain_maximises_the_captain_fallback() -> None:
    # The vice-captain contributes only when the captain is absent, and that
    # term never reaches the solver's objective. Left to CBC it was an
    # arbitrary starter.
    candidates = _degenerate_bench_candidates()

    result = optimise_full_squad(
        candidates, budget_tenths=1000, rules=RULES
    )

    selected = {player.source_player_id: player for player in result.players}
    starters = [selected[player_id] for player_id in result.starting_player_ids]
    captain = selected[result.captain_id]
    assert captain.appearance_probability < 1.0
    assert captain.gameweek_expected_points == max(
        player.gameweek_expected_points for player in starters
    )
    best_available_vice = max(
        player.gameweek_expected_points
        for player in starters
        if player.source_player_id != result.captain_id
    )
    assert (
        selected[result.vice_captain_id].gameweek_expected_points
        == best_available_vice
    )


def test_vice_captain_is_chosen_jointly_with_the_captain() -> None:
    starters = tuple(
        CandidatePlayer(
            source_player_id=identifier,
            web_name=identifier,
            team_id=identifier,
            team_short_name=identifier,
            position=Position.MID,
            price_tenths=50,
            expected_points=points,
            gameweek_expected_points=points,
            appearance_probability=appearance,
        )
        for identifier, points, appearance in (
            ("a", 5.0, 0.5),
            ("b", 5.0, 0.99),
            ("c", 4.0, 1.0),
        )
    )
    points = {
        player.source_player_id: player.gameweek_expected_points
        for player in starters
    }
    appearance = {
        player.source_player_id: player.appearance_probability
        for player in starters
    }

    captain, vice = _optimal_captaincy(
        starters, points=points, appearance=appearance
    )

    # Both top scorers tie on captain points, so the fallback decides: the
    # less certain captain puts more weight on the vice-captain.
    assert (captain, vice) == ("a", "b")


def test_bench_is_optimised_rather_than_filled_with_the_cheapest_legal_players() -> (
    None
):
    candidates = _degenerate_bench_candidates()

    result = optimise_full_squad(
        candidates, budget_tenths=1000, rules=RULES
    )

    selected = {player.source_player_id: player for player in result.players}
    bench = [selected[player_id] for player_id in result.bench_player_ids]
    assert all(
        player.source_player_id.startswith("strong") for player in bench
    ), [player.source_player_id for player in bench]
    # 11 starters at 70 plus four bench players at 55.
    assert result.total_cost_tenths == 990


def test_bench_order_beats_every_other_permutation() -> None:
    candidates = _degenerate_bench_candidates()

    result = optimise_full_squad(
        candidates, budget_tenths=1000, rules=RULES
    )

    outfield_bench = result.bench_player_ids[1:]
    for ordering in permutations(outfield_bench):
        expected, _, _ = _expected_weekly_score(
            result.players,
            result.starting_player_ids,
            (result.bench_player_ids[0], *ordering),
            result.captain_id,
            result.vice_captain_id,
            RULES,
        )
        assert result.gameweek_expected_points >= round(expected, 3)


def test_full_squad_selection_is_deterministic_across_runs() -> None:
    candidates = _degenerate_bench_candidates()

    first = optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)
    second = optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)

    assert first.players == second.players
    assert first.starting_player_ids == second.starting_player_ids
    assert first.bench_player_ids == second.bench_player_ids
    assert first.captain_id == second.captain_id
    assert first.vice_captain_id == second.vice_captain_id


def test_opening_alternatives_must_change_the_starting_xi() -> None:
    candidates = _degenerate_bench_candidates()

    opening = optimise_opening_squads(
        candidates,
        budget_tenths=1000,
        rules=RULES,
        alternative_count=2,
    )

    starting_xis = [opening.primary.starting_player_ids] + [
        alternative.starting_player_ids
        for alternative in opening.alternatives
    ]
    assert len(set(starting_xis)) == len(starting_xis)


def test_exact_bench_evaluator_skips_an_illegal_higher_priority_substitute() -> None:
    starter_positions = (
        *(Position.DEF for _ in range(3)),
        *(Position.MID for _ in range(4)),
        *(Position.FWD for _ in range(3)),
    )
    bench_positions = (Position.FWD, Position.DEF, Position.MID)
    # One starting defender is absent and all three substitutes play. The
    # first bench player cannot enter because that would leave only two
    # defenders, so the second substitute must be used.
    outcomes = (
        False,
        *(True for _ in range(9)),
        True,
        True,
        True,
    )

    assert _used_outfield_bench_indexes(
        starter_positions,
        bench_positions,
        outcomes,
        RULES,
    ) == (1,)
