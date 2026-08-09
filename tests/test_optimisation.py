from __future__ import annotations

from collections import Counter
from dataclasses import replace
from itertools import combinations, permutations
from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    OptimisationError,
    _expected_weekly_score,
    _optimal_captaincy,
    _used_outfield_bench_indexes,
    appearance_qualified_candidates,
    goalkeeper_pair_orientation,
    optimise_full_squad,
    optimise_opening_squads,
    optimise_starting_xi,
    squad_ranking_key,
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


def test_a_goalkeeper_can_be_captain_when_their_points_dominate() -> None:
    """FPL permits captaining a goalkeeper and the exact captaincy enumeration
    must too. The MILP guide once excluded goalkeepers from the linear captain
    term, but the reported captaincy is resolved here, over every starter — so
    a goalkeeper who out-scores the outfield starters is captained."""

    starters = tuple(
        CandidatePlayer(
            source_player_id=identifier,
            web_name=identifier,
            team_id=identifier,
            team_short_name=identifier,
            position=position,
            price_tenths=50,
            expected_points=points,
            gameweek_expected_points=points,
            appearance_probability=1.0,
        )
        for identifier, position, points in (
            ("keeper", Position.GK, 9.0),
            ("mid", Position.MID, 6.0),
            ("fwd", Position.FWD, 5.0),
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

    captain, _vice = _optimal_captaincy(
        starters, points=points, appearance=appearance
    )

    assert captain == "keeper"


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


# --------------------------------------------------------------------------
# Goalkeepers are a pair, not two players
# --------------------------------------------------------------------------


def _pair_squad_candidates(
    *,
    first_points: float,
    first_appearance: float,
    second_points: float,
    second_appearance: float,
    third_points: float = 1.0,
    gameweeks: int = 2,
) -> tuple[CandidatePlayer, ...]:
    """Eleven forced outfield players and three goalkeepers to choose between.

    The outfield squad is fully determined — exactly the required counts at
    identical prices — so the only decision left is which two goalkeepers are
    owned and which one starts. Anything the optimiser does differently is a
    goalkeeper decision.
    """

    def player(
        identifier: str,
        position: Position,
        price_tenths: int,
        points: float,
        appearance_probability: float,
    ) -> CandidatePlayer:
        values = tuple(
            GameweekPlayerValue(
                gameweek,
                points,
                appearance_probability,
                appearance_probability,
            )
            for gameweek in range(1, gameweeks + 1)
        )
        return CandidatePlayer(
            source_player_id=identifier,
            web_name=f"Player {identifier}",
            team_id=identifier,
            team_short_name=f"T{identifier}",
            position=position,
            price_tenths=price_tenths,
            expected_points=points * gameweeks,
            gameweek_expected_points=points,
            appearance_probability=appearance_probability,
            gameweek_values=values,
        )

    outfield_shape = (
        *(Position.DEF for _ in range(5)),
        *(Position.MID for _ in range(5)),
        *(Position.FWD for _ in range(3)),
    )
    outfield = tuple(
        player(f"out{index:02d}", position, 40, 4.0, 0.9)
        for index, position in enumerate(outfield_shape)
    )
    keepers = (
        player("gk_first", Position.GK, 40, first_points, first_appearance),
        player("gk_second", Position.GK, 40, second_points, second_appearance),
        player("gk_third", Position.GK, 40, third_points, 0.9),
    )
    return outfield + keepers


def test_the_pair_orientation_prefers_protection_over_a_bigger_standalone() -> None:
    """The claim the whole treatment rests on, as arithmetic.

    A goalkeeper with the smaller unconditional projection can be the correct
    nomination: if their own appearance is doubtful, the reserve behind them
    collects in exactly the states they miss, and the pair is worth more than
    the safer goalkeeper's projection alone.
    """

    orientation = goalkeeper_pair_orientation(
        "safe",
        "doubtful",
        gameweek_number=1,
        points={"safe": 4.0, "doubtful": 3.9},
        appearance={"safe": 0.99, "doubtful": 0.50},
    )

    assert orientation.starter_id == "doubtful"
    assert orientation.prefers_lower_standalone
    # 3.9 + 0.5 x 4.0 against 4.0 + 0.01 x 3.9.
    assert orientation.value == pytest.approx(5.9)
    assert orientation.alternative_value == pytest.approx(4.039)


def test_the_lower_standalone_goalkeeper_can_be_selected_and_started() -> None:
    """The same claim, but decided by the optimiser rather than by arithmetic."""

    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.99,
        second_points=3.9,
        second_appearance=0.50,
        third_points=0.2,
    )

    result = optimise_full_squad(
        candidates, budget_tenths=1000, rules=RULES
    )

    assert set(result.goalkeeper_pair) == {"gk_first", "gk_second"}
    nominated = {
        orientation.gameweek_number: orientation.starter_id
        for orientation in result.goalkeeper_orientations
    }
    assert set(nominated.values()) == {"gk_second"}
    started = {
        plan.gameweek_number: next(
            player_id
            for player_id in plan.starting_player_ids
            if player_id.startswith("gk_")
        )
        for plan in result.gameweek_plans
    }
    # The nomination is the lineup, not a label attached after selection.
    assert started == nominated


def test_pair_value_decides_which_two_goalkeepers_are_owned() -> None:
    """Selection, not only nomination.

    The reserve never starts, so a model that values goalkeepers one at a time
    is indifferent between reserves and picks arbitrarily. Pair valuation makes
    the reserve's quality a reason to own them.
    """

    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.5,
        second_points=1.0,
        second_appearance=0.9,
        third_points=3.0,
    )

    result = optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)

    # gk_third is the better partner for the doubtful gk_first, even though
    # gk_second and gk_third cost the same and neither is expected to start.
    assert set(result.goalkeeper_pair) == {"gk_first", "gk_third"}


def test_goalkeeper_points_are_never_counted_twice() -> None:
    """The solver objective must contain each goalkeeper exactly once.

    The pair term already carries the nominated goalkeeper's own expectation
    plus the reserve's, conditioned on absence. If the ordinary starter term
    still carried the goalkeeper, the objective would pay for the same points
    twice and the optimiser would overbuy goalkeepers.
    """

    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.8,
        second_points=2.0,
        second_appearance=0.8,
        third_points=0.1,
    )

    result = optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)

    by_id = {player.source_player_id: player for player in result.players}
    outfield_lineup = sum(
        value.expected_points
        for plan in result.gameweek_plans
        for player_id in plan.starting_player_ids
        if by_id[player_id].position != Position.GK
        for value in by_id[player_id].gameweek_values
        if value.gameweek_number == plan.gameweek_number
    )
    captain = sum(
        next(
            value.expected_points
            for value in by_id[plan.captain_id].gameweek_values
            if value.gameweek_number == plan.gameweek_number
        )
        for plan in result.gameweek_plans
    )
    expected_objective = (
        outfield_lineup + captain + result.goalkeeper_pair_value
    )

    assert result.solver_objective == pytest.approx(expected_objective, abs=1e-3)
    # And the pair value is exactly the two-goalkeeper quantity, not a sum.
    assert result.goalkeeper_pair_value == pytest.approx(
        sum(
            orientation.value
            for orientation in result.goalkeeper_orientations
        ),
        abs=1e-6,
    )


def test_the_exact_revaluation_agrees_with_the_pair_value() -> None:
    """The objective and the exact rescoring must price the pair identically.

    The exact weekly score already replaces an absent goalkeeper with the
    substitute. If that disagreed with the pair term, the optimiser would be
    selecting against one number and reporting another.
    """

    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.75,
        second_points=2.5,
        second_appearance=0.85,
        third_points=0.1,
    )

    result = optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)

    for orientation in result.goalkeeper_orientations:
        starter = next(
            player
            for player in result.players
            if player.source_player_id == orientation.starter_id
        )
        substitute = next(
            player
            for player in result.players
            if player.source_player_id == orientation.substitute_id
        )
        starter_value = next(
            value
            for value in starter.gameweek_values
            if value.gameweek_number == orientation.gameweek_number
        )
        substitute_value = next(
            value
            for value in substitute.gameweek_values
            if value.gameweek_number == orientation.gameweek_number
        )
        assert orientation.value == pytest.approx(
            starter_value.expected_points
            + (1.0 - starter_value.appearance_probability)
            * substitute_value.expected_points
        )


def test_pair_valuation_can_be_switched_off_for_a_controlled_comparison() -> None:
    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.5,
        second_points=1.0,
        second_appearance=0.9,
        third_points=3.0,
    )

    without = optimise_full_squad(
        candidates,
        budget_tenths=1000,
        rules=RULES,
        goalkeeper_pair_valuation=False,
    )

    assert without.goalkeeper_pair == ()
    assert without.goalkeeper_pair_value == 0.0


# --------------------------------------------------------------------------
# Budget, forced inclusion and the frontier
# --------------------------------------------------------------------------


def test_the_budget_is_an_upper_bound_not_a_target() -> None:
    """Nothing requires the squad to spend every penny.

    A candidate set whose only legal squad is cheap has to be reachable; an
    optimiser that treats the budget as an equality would report it infeasible.
    """

    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.9,
        second_points=3.0,
        second_appearance=0.9,
    )

    result = optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)

    assert result.total_cost_tenths <= 1000
    assert result.total_cost_tenths == 600


def test_a_cheaper_squad_wins_a_tie_before_the_identifier_tie_break() -> None:
    """Money left over is never points, but it does break a tie.

    Two squads the model cannot separate on expected points are not equal in
    every respect: one leaves money for a transfer that has not happened yet.
    """

    expensive = optimise_full_squad(
        _pair_squad_candidates(
            first_points=4.0, first_appearance=0.9,
            second_points=3.0, second_appearance=0.9,
        ),
        budget_tenths=1000,
        rules=RULES,
    )
    cheaper = replace(expensive, total_cost_tenths=expensive.total_cost_tenths - 10)

    ordered = sorted([expensive, cheaper], key=squad_ranking_key)

    assert ordered[0] is cheaper


def test_a_forced_player_is_in_the_squad_and_the_rest_is_rebuilt() -> None:
    candidates = _pair_squad_candidates(
        first_points=4.0,
        first_appearance=0.9,
        second_points=3.0,
        second_appearance=0.9,
        third_points=3.5,
    )

    forced = optimise_full_squad(
        candidates,
        budget_tenths=1000,
        rules=RULES,
        required_player_ids=frozenset({"gk_second"}),
    )

    assert "gk_second" in {
        player.source_player_id for player in forced.players
    }


def test_forcing_a_player_who_is_not_a_candidate_is_refused() -> None:
    candidates = _pair_squad_candidates(
        first_points=4.0, first_appearance=0.9,
        second_points=3.0, second_appearance=0.9,
    )

    with pytest.raises(OptimisationError, match="not eligible candidates"):
        optimise_full_squad(
            candidates,
            budget_tenths=1000,
            rules=RULES,
            required_player_ids=frozenset({"nobody"}),
        )


def test_a_frontier_may_repeat_a_starting_eleven_when_asked_to() -> None:
    """Two squads with the same XI and different benches are different squads.

    They make different autosub and rotation propositions, and excluding one of
    them narrows the frontier for no reason.
    """

    candidates = _degenerate_bench_candidates()

    frontier = optimise_opening_squads(
        candidates,
        budget_tenths=1000,
        rules=RULES,
        alternative_count=3,
        candidate_pool_size=4,
        require_distinct_starting_xi=False,
    )

    squads = [frontier.primary, *frontier.alternatives]
    memberships = [
        frozenset(player.source_player_id for player in squad.players)
        for squad in squads
    ]
    assert len(set(memberships)) == len(memberships)


def test_a_frontier_larger_than_the_candidate_set_stops_rather_than_failing() -> None:
    """Running out of legal squads is a fact, not an error."""

    candidates = _pair_squad_candidates(
        first_points=4.0, first_appearance=0.9,
        second_points=3.0, second_appearance=0.9,
    )

    frontier = optimise_opening_squads(
        candidates,
        budget_tenths=1000,
        rules=RULES,
        alternative_count=0,
        candidate_pool_size=25,
        require_distinct_starting_xi=False,
    )

    assert frontier.primary.total_cost_tenths <= 1000
