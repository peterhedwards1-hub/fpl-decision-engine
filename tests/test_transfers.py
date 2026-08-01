from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.decision_evaluation import (
    RealisedPlayerOutcome,
    TransferReplayWeek,
    replay_transfer_continuity,
)
from fpl_engine.domain import Position
from fpl_engine.optimisation import CandidatePlayer
from fpl_engine.pricing import PurchaseLedger
from fpl_engine.transfers import (
    CurrentSquad,
    free_transfer_option_value,
    recommend_transfers,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def test_transfer_recommender_searches_the_configured_move_cap_by_default() -> None:
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
            price_tenths=50,
            expected_points=(100.0 if index == 22 else 20.0 + index),
            gameweek_expected_points=3.0 + index / 10,
            appearance_probability=1.0,
        )
        for index, position in enumerate(positions, start=1)
    )
    current_ids = frozenset(
        (
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
        )
    )
    recommendation = recommend_transfers(
        candidates,
        CurrentSquad(
            player_ids=current_ids,
            selling_prices_tenths={player_id: 50 for player_id in current_ids},
            bank_tenths=10,
            free_transfers=1,
        ),
        rules=RULES,
    )

    assert {route.transfer_count for route in recommendation.routes} == {
        0,
        1,
        2,
        3,
        4,
        5,
    }
    assert recommendation.primary.transfer_count in {1, 2, 3, 4, 5}
    assert any(
        player.source_player_id == "22"
        for player in recommendation.primary.transfers_in
    )
    assert recommendation.primary.horizon_points_gain > 0
    assert recommendation.primary.next_free_transfers >= 1
    roll = next(
        route for route in recommendation.routes if route.transfer_count == 0
    )
    assert roll.points_hit == 0
    assert roll.horizon_points_gain == 0
    assert "exact best legal routes" in recommendation.search_scope


def test_free_transfer_value_is_state_dependent_expected_avoided_hits() -> None:
    needs = {0: 0.1, 1: 0.4, 2: 0.3, 3: 0.2}

    one = free_transfer_option_value(1, needs, RULES)
    two = free_transfer_option_value(2, needs, RULES)
    five = free_transfer_option_value(5, needs, RULES)

    assert one == 0
    assert 0 < two < five


def test_transfer_replay_preserves_squad_and_free_transfer_state() -> None:
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
            price_tenths=50,
            expected_points=20.0 + index,
            gameweek_expected_points=2.0 + index / 10,
            appearance_probability=0.9,
        )
        for index, position in enumerate(positions, start=1)
    )
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
    outcomes = tuple(
        RealisedPlayerOutcome(
            player.source_player_id,
            int(player.expected_points) % 10,
            90,
        )
        for player in candidates
    )

    report = replay_transfer_continuity(
        (
            TransferReplayWeek(2, candidates, outcomes),
            TransferReplayWeek(3, candidates, outcomes),
        ),
        CurrentSquad(
            player_ids=current_ids,
            selling_prices_tenths={
                player_id: 50 for player_id in current_ids
            },
            bank_tenths=0,
            free_transfers=1,
        ),
        rules=RULES,
        max_transfers_per_week=1,
    )

    assert len(report.weeks) == 2
    assert report.total_hits == 0
    assert 1 <= report.final_free_transfers <= 5
    assert report.total_regret >= 0


def _priced_candidates(prices: dict[str, int], points: dict[str, float]):
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
            price_tenths=prices.get(str(index), 50),
            expected_points=points.get(str(index), 20.0 + index),
            gameweek_expected_points=2.0 + index / 10,
            appearance_probability=0.9,
        )
        for index, position in enumerate(positions, start=1)
    )


OWNED = frozenset(
    {"1", "2", "4", "5", "6", "7", "8", "11", "12", "13", "14", "15", "18", "19", "20"}
)


def test_a_price_rise_only_half_funds_the_next_transfer() -> None:
    # Player "1" is bought at 50 and rises to 60 by the second Gameweek.
    first = _priced_candidates({}, {})
    second = _priced_candidates({"1": 60}, {})
    outcomes = tuple(
        RealisedPlayerOutcome(player.source_player_id, 3, 90) for player in first
    )

    report = replay_transfer_continuity(
        (
            TransferReplayWeek(2, first, outcomes),
            TransferReplayWeek(3, second, outcomes),
        ),
        CurrentSquad(
            player_ids=OWNED,
            selling_prices_tenths={player_id: 50 for player_id in OWNED},
            bank_tenths=0,
            free_transfers=1,
        ),
        rules=RULES,
        max_transfers_per_week=1,
    )

    # Fourteen players held at 50, plus "1" bought at 50 and now worth 60. Its
    # sale value is 55, not 60, so the squad is worth 755 rather than 760: the
    # rise funds only half of itself.
    second_week = report.weeks[1]
    assert second_week.squad_selling_value_tenths == 755
    assert report.final_purchase_prices_tenths["1"] == 50
    assert any(
        "half-profit" in limitation for limitation in report.limitations
    )


def test_the_opening_ledger_must_cover_the_opening_squad() -> None:
    candidates = _priced_candidates({}, {})
    outcomes = tuple(
        RealisedPlayerOutcome(player.source_player_id, 3, 90) for player in candidates
    )

    with pytest.raises(ValueError, match="must cover exactly the opening squad"):
        replay_transfer_continuity(
            (TransferReplayWeek(2, candidates, outcomes),),
            CurrentSquad(
                player_ids=OWNED,
                selling_prices_tenths={player_id: 50 for player_id in OWNED},
                bank_tenths=0,
                free_transfers=1,
            ),
            rules=RULES,
            initial_ledger=PurchaseLedger.from_entry_prices({"1": 50}),
        )
