from pathlib import Path

import pytest

from fpl_engine.chips import recommend_chip
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Chip, Position
from fpl_engine.optimisation import CandidatePlayer, optimise_full_squad

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def _candidates() -> tuple[CandidatePlayer, ...]:
    positions = (
        *(Position.GK for _ in range(3)),
        *(Position.DEF for _ in range(7)),
        *(Position.MID for _ in range(7)),
        *(Position.FWD for _ in range(5)),
    )
    return tuple(
        CandidatePlayer(
            source_player_id=str(index),
            web_name=f"Player {index}",
            team_id=str((index - 1) % 8 + 1),
            team_short_name=f"T{(index - 1) % 8 + 1}",
            position=position,
            price_tenths=50,
            expected_points=30 + index,
            gameweek_expected_points=2 + index / 5,
            appearance_probability=0.9,
        )
        for index, position in enumerate(positions, start=1)
    )


def test_chip_recommendations_share_season_rules_and_solver() -> None:
    candidates = _candidates()
    current_ids = frozenset(
        {
            "1",
            "2",
            "4",
            "5",
            "6",
            "7",
            "8",
            "11",
            "12",
            "13",
            "14",
            "15",
            "18",
            "19",
            "20",
        }
    )
    current = tuple(
        player
        for player in candidates
        if player.source_player_id in current_ids
    )
    baseline = optimise_full_squad(
        current,
        budget_tenths=sum(player.price_tenths for player in current),
        rules=RULES,
    )
    triple_captain = recommend_chip(
        Chip.TRIPLE_CAPTAIN,
        candidates,
        gameweek_number=2,
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )
    assert triple_captain.captain_id in current_ids
    assert triple_captain.expected_incremental_points > 0
    assert triple_captain.expected_incremental_points == (
        baseline.expected_captain_contribution
    )

    free_hit = recommend_chip(
        Chip.FREE_HIT,
        candidates,
        gameweek_number=2,
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )
    assert free_hit.squad is not None
    assert len(free_hit.squad.players) == 15
    assert free_hit.expected_incremental_points == round(
        free_hit.squad.gameweek_expected_points
        - baseline.gameweek_expected_points,
        3,
    )

    bench_boost = recommend_chip(
        Chip.BENCH_BOOST,
        candidates,
        gameweek_number=20,
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )
    assert bench_boost.expected_incremental_points == round(
        sum(
            player.gameweek_expected_points or player.expected_points
            for player in current
        )
        + baseline.expected_captain_contribution
        - baseline.gameweek_expected_points,
        3,
    )

    with pytest.raises(ValueError, match="unavailable"):
        recommend_chip(
            Chip.FREE_HIT,
            candidates,
            gameweek_number=1,
            previous_chip_gameweeks=(),
            budget_tenths=1000,
            rules=RULES,
        )
