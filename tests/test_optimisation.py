from __future__ import annotations

from collections import Counter
from dataclasses import replace
from itertools import combinations, permutations
from pathlib import Path

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    _expected_weekly_score,
    _optimal_captaincy,
    _used_outfield_bench_indexes,
    appearance_qualified_candidates,
    optimise_full_squad,
    optimise_opening_squads,
    optimise_starting_xi,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def test_opening_appearance_guardrail_uses_the_whole_horizon() -> None:
    reliable = CandidatePlayer(
        "reliable",
        "Reliable",
        "1",
        "T1",
        Position.MID,
        50,
        10.0,
        gameweek_values=(
            GameweekPlayerValue(1, 5.0, 0.8),
            GameweekPlayerValue(2, 5.0, 0.6),
        ),
    )
    risky = replace(
        reliable,
        source_player_id="risky",
        web_name="Risky",
        gameweek_values=(
            GameweekPlayerValue(1, 5.0, 1.0),
            GameweekPlayerValue(2, 5.0, 0.0),
        ),
    )

    qualified = appearance_qualified_candidates((reliable, risky))

    assert qualified == (reliable,)

# These candidates make reserve selection explicit: eleven clearly superior
# starters, then a matched cheap and strong option for every bench slot, with
# the budget to afford either.
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


def test_captaincy_can_prefer_lower_raw_points_for_fallback_value() -> None:
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
            ("highest", 5.0, 0.99),
            ("fallback", 4.9, 0.50),
            ("third", 2.0, 1.0),
        )
    )

    captain, vice = _optimal_captaincy(
        starters,
        points={player.source_player_id: player.expected_points for player in starters},
        appearance={
            player.source_player_id: player.appearance_probability
            for player in starters
        },
    )

    assert (captain, vice) == ("fallback", "highest")


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


def test_bench_quality_is_valued_across_the_full_horizon() -> None:
    candidates = []
    for player in _degenerate_bench_candidates():
        if player.source_player_id.startswith("star"):
            points = (player.expected_points, player.expected_points)
        elif player.source_player_id.startswith("strong"):
            points = (0.1, 3.0)
        elif player.source_player_id.startswith("spare"):
            points = (0.1, 2.9)
        else:
            points = (0.1, 0.1)
        candidates.append(
            replace(
                player,
                expected_points=sum(points),
                gameweek_expected_points=points[0],
                gameweek_values=(
                    GameweekPlayerValue(1, points[0], player.appearance_probability),
                    GameweekPlayerValue(2, points[1], player.appearance_probability),
                ),
            )
        )

    result = optimise_full_squad(
        tuple(candidates),
        budget_tenths=1000,
        rules=RULES,
    )

    selected = {player.source_player_id: player for player in result.players}
    future_bench = [
        selected[player_id]
        for player_id in result.gameweek_plans[1].bench_player_ids
    ]
    assert all(player.source_player_id.startswith("strong") for player in future_bench)
    assert result.horizon_expected_bench_contribution > 0
    assert result.horizon_expected_points > result.lineup_expected_points


def test_bench_order_beats_every_other_permutation() -> None:
    candidates = _degenerate_bench_candidates()

    result = optimise_full_squad(
        candidates, budget_tenths=1000, rules=RULES
    )

    points = {
        player.source_player_id: (
            player.expected_points
            if player.gameweek_expected_points is None
            else player.gameweek_expected_points
        )
        for player in result.players
    }
    appearance = {
        player.source_player_id: player.appearance_probability
        for player in result.players
    }
    outfield_bench = result.bench_player_ids[1:]
    for ordering in permutations(outfield_bench):
        expected, _, _ = _expected_weekly_score(
            result.players,
            result.starting_player_ids,
            (result.bench_player_ids[0], *ordering),
            result.captain_id,
            result.vice_captain_id,
            RULES,
            points=points,
            appearance=appearance,
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


def _rotating_candidates() -> tuple[CandidatePlayer, ...]:
    """Exactly fifteen legal players whose best XI differs between Gameweeks.

    The squad is forced, so the only decision left is who starts each week. Two
    midfielders swap: one is worthless in GW2, the other worthless in GW1.
    """

    shape = (
        *((Position.GK, f"gk{index}") for index in range(2)),
        *((Position.DEF, f"def{index}") for index in range(5)),
        *((Position.MID, f"mid{index}") for index in range(5)),
        *((Position.FWD, f"fwd{index}") for index in range(3)),
    )
    rotating = {"mid3": (9.0, 0.0), "mid4": (0.0, 9.0)}
    candidates = []
    for index, (position, identifier) in enumerate(shape):
        first, second = rotating.get(identifier, (5.0 + index * 0.01, 5.0 + index * 0.01))
        candidates.append(
            CandidatePlayer(
                source_player_id=identifier,
                web_name=identifier,
                team_id=str(index % 8),
                team_short_name=f"T{index % 8}",
                position=position,
                price_tenths=60,
                expected_points=first + second,
                gameweek_expected_points=first,
                appearance_probability=0.9,
                gameweek_values=(
                    GameweekPlayerValue(1, first, 0.9, 0.8),
                    GameweekPlayerValue(2, second, 0.9, 0.8),
                ),
            )
        )
    return tuple(candidates)


def test_every_gameweek_plan_carries_its_own_legal_bench() -> None:
    result = optimise_full_squad(
        _rotating_candidates(),
        budget_tenths=1000,
        rules=RULES,
    )

    squad_ids = {player.source_player_id for player in result.players}
    assert len(result.gameweek_plans) == 2
    for plan in result.gameweek_plans:
        assert len(plan.bench_player_ids) == 4
        # A player is either starting or benched in a given Gameweek, never both
        # and never neither.
        assert not set(plan.bench_player_ids) & plan.starting_player_ids
        assert set(plan.bench_player_ids) | set(plan.starting_player_ids) == squad_ids
        assert len(set(plan.bench_player_ids)) == 4


def test_a_rotated_lineup_gets_a_rotated_bench() -> None:
    result = optimise_full_squad(
        _rotating_candidates(),
        budget_tenths=1000,
        rules=RULES,
    )

    first, second = result.gameweek_plans
    assert first.starting_player_ids != second.starting_player_ids
    # mid3 is worthless in GW2 and mid4 worthless in GW1, so they swap.
    assert "mid3" in first.starting_player_ids
    assert "mid3" in second.bench_player_ids
    assert "mid4" in first.bench_player_ids
    assert "mid4" in second.starting_player_ids


def test_declared_terminal_value_is_added_once_beyond_the_horizon() -> None:
    candidates = list(_rotating_candidates())
    candidates[0] = replace(candidates[0], residual_value=2.5)

    result = optimise_full_squad(
        tuple(candidates),
        budget_tenths=1000,
        rules=RULES,
    )

    assert result.terminal_value == 2.5
    assert result.decision_value == result.horizon_expected_points + 2.5
