from __future__ import annotations

from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import CandidatePlayer, GameweekPlayerValue
from fpl_engine.squad_lab import (
    ClubLimit,
    SquadLabRequest,
    run_squad_lab_search,
    seed_squad,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def _player(
    identifier: int,
    position: Position,
    team: str,
    price_tenths: int,
    points: float,
) -> CandidatePlayer:
    values = tuple(
        GameweekPlayerValue(
            gameweek_number=gameweek,
            expected_points=points,
            appearance_probability=0.85,
            sixty_probability=0.8,
        )
        for gameweek in (1, 2)
    )
    return CandidatePlayer(
        source_player_id=str(identifier),
        web_name=f"P{identifier}",
        team_id=team,
        team_short_name=team,
        position=position,
        price_tenths=price_tenths,
        expected_points=points * len(values),
        gameweek_expected_points=points,
        appearance_probability=0.85,
        gameweek_values=values,
    )


def _pool() -> tuple[CandidatePlayer, ...]:
    """Six clubs deep enough that constraints have somewhere to go."""

    players: list[CandidatePlayer] = []
    identifier = 1
    shape = (
        (Position.GK, 6),
        (Position.DEF, 14),
        (Position.MID, 14),
        (Position.FWD, 10),
    )
    clubs = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
    for position, count in shape:
        for index in range(count):
            players.append(
                _player(
                    identifier,
                    position,
                    clubs[index % len(clubs)],
                    40 + (index % 5) * 5,
                    2.0 + (index % 7) * 0.4,
                )
            )
            identifier += 1
    return tuple(players)


def test_a_required_player_is_never_searched_away() -> None:
    pool = _pool()
    keeper = next(p for p in pool if p.position is Position.GK)
    request = SquadLabRequest(
        budget_tenths=RULES.squad.budget_tenths,
        required_player_ids=frozenset({keeper.source_player_id}),
        time_budget_seconds=30.0,
        kicks=1,
    )
    result = run_squad_lab_search({"only": pool}, RULES, request, workers=2)
    assert keeper.source_player_id in result.player_ids


def test_an_excluded_player_never_appears() -> None:
    pool = _pool()
    banned = max(pool, key=lambda p: p.expected_points)
    request = SquadLabRequest(
        budget_tenths=RULES.squad.budget_tenths,
        forbidden_player_ids=frozenset({banned.source_player_id}),
        time_budget_seconds=30.0,
        kicks=1,
    )
    result = run_squad_lab_search({"only": pool}, RULES, request, workers=2)
    assert banned.source_player_id not in result.player_ids


def test_a_tighter_club_limit_is_honoured() -> None:
    pool = _pool()
    by_id = {player.source_player_id: player for player in pool}
    request = SquadLabRequest(
        budget_tenths=RULES.squad.budget_tenths,
        club_limits=(ClubLimit("AAA", 1),),
        time_budget_seconds=30.0,
        kicks=1,
    )
    result = run_squad_lab_search({"only": pool}, RULES, request, workers=2)
    picked = [by_id[player_id] for player_id in result.player_ids]
    assert sum(1 for p in picked if p.team_short_name == "AAA") <= 1


def test_the_squad_is_legal_and_within_budget() -> None:
    pool = _pool()
    by_id = {player.source_player_id: player for player in pool}
    request = SquadLabRequest(
        budget_tenths=RULES.squad.budget_tenths,
        time_budget_seconds=30.0,
        kicks=0,
    )
    result = run_squad_lab_search({"only": pool}, RULES, request, workers=2)
    picked = [by_id[player_id] for player_id in result.player_ids]
    assert len(picked) == RULES.squad.squad_size
    assert sum(p.price_tenths for p in picked) <= RULES.squad.budget_tenths
    shape: dict[str, int] = {}
    for player in picked:
        shape[player.position.value] = shape.get(player.position.value, 0) + 1
    assert shape == dict(RULES.squad.position_counts)


def test_the_worst_case_objective_is_the_lower_of_the_pools() -> None:
    """Two pools that disagree must be judged on the pessimistic one."""

    generous = _pool()
    mean = tuple(
        CandidatePlayer(
            source_player_id=p.source_player_id,
            web_name=p.web_name,
            team_id=p.team_id,
            team_short_name=p.team_short_name,
            position=p.position,
            price_tenths=p.price_tenths,
            expected_points=p.expected_points * 0.5,
            gameweek_expected_points=p.gameweek_expected_points * 0.5,
            appearance_probability=p.appearance_probability,
            gameweek_values=tuple(
                GameweekPlayerValue(
                    gameweek_number=v.gameweek_number,
                    expected_points=v.expected_points * 0.5,
                    appearance_probability=v.appearance_probability,
                    sixty_probability=v.sixty_probability,
                )
                for v in p.gameweek_values
            ),
        )
        for p in generous
    )
    request = SquadLabRequest(
        budget_tenths=RULES.squad.budget_tenths,
        time_budget_seconds=30.0,
        kicks=0,
    )
    result = run_squad_lab_search(
        {"generous": generous, "mean": mean}, RULES, request, workers=2
    )
    assert set(result.per_model) == {"generous", "mean"}
    assert result.objective == pytest.approx(min(result.per_model.values()))
    assert result.per_model["mean"] < result.per_model["generous"]


def test_contradictory_constraints_are_rejected_before_any_work() -> None:
    with pytest.raises(ValueError, match="both required and excluded"):
        SquadLabRequest(
            budget_tenths=1000,
            required_player_ids=frozenset({"1"}),
            forbidden_player_ids=frozenset({"1"}),
        )


def test_pools_covering_different_players_are_refused() -> None:
    """A worst case across mismatched pools would compare different squads."""

    pool = _pool()
    request = SquadLabRequest(budget_tenths=RULES.squad.budget_tenths)
    with pytest.raises(ValueError, match="same players"):
        run_squad_lab_search(
            {"full": pool, "short": pool[:-1]}, RULES, request, workers=2
        )


def test_the_seed_squad_already_satisfies_the_constraints() -> None:
    pool = _pool()
    by_id = {player.source_player_id: player for player in pool}
    forward = next(p for p in pool if p.position is Position.FWD)
    request = SquadLabRequest(
        budget_tenths=RULES.squad.budget_tenths,
        required_player_ids=frozenset({forward.source_player_id}),
        club_limits=(ClubLimit("BBB", 2),),
    )
    squad = seed_squad(pool, request, RULES)
    picked = [by_id[player_id] for player_id in squad]
    assert forward.source_player_id in squad
    assert sum(1 for p in picked if p.team_short_name == "BBB") <= 2
