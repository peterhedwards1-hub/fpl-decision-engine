from pathlib import Path

import pytest

from fpl_engine.chips import recommend_chip
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Chip, Position
from fpl_engine.optimisation import CandidatePlayer

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
    current_ids = frozenset(player.source_player_id for player in candidates[:15])
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

    free_hit = recommend_chip(
        Chip.FREE_HIT,
        candidates,
        gameweek_number=2,
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
    )
    assert free_hit.squad is not None
    assert len(free_hit.squad.players) == 15

    with pytest.raises(ValueError, match="unavailable"):
        recommend_chip(
            Chip.FREE_HIT,
            candidates,
            gameweek_number=1,
            previous_chip_gameweeks=(),
            budget_tenths=1000,
            rules=RULES,
        )
