from pathlib import Path

import pytest

from fpl_engine.chips import (
    _reserve_from_doubles,
    recommend_chip,
    recommend_chip_timing,
)
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Chip, Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    optimise_full_squad,
)

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


def test_chip_timing_computes_future_opportunity_cost() -> None:
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
    candidates = tuple(
        CandidatePlayer(
            **{
                **player.__dict__,
                "expected_points": 12.0,
                "gameweek_expected_points": 4.0,
                "gameweek_values": (
                    GameweekPlayerValue(2, 4.0, 0.9),
                    GameweekPlayerValue(
                        3,
                        9.0
                        if player.source_player_id == "22"
                        else 5.0,
                        0.9,
                    ),
                ),
            }
        )
        for player in _candidates()
    )

    timing = recommend_chip_timing(
        Chip.TRIPLE_CAPTAIN,
        candidates,
        candidate_gameweeks=(2, 3),
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )

    assert timing.recommended_gameweek == 3
    assert not timing.horizon_reaches_set_expiry
    current = timing.options[0]
    assert current.future_opportunity_cost > 0
    assert current.net_value_versus_best_later < 0


def test_double_gameweek_reserve_picks_biggest_double_and_caps_clubs() -> None:
    # One season, two doubles: a small one (GW24) and a bigger one (GW33) that
    # should be chosen. In GW33 club "A" has four high doublers, but only three
    # can be owned, so the fourth (11.0) is excluded from the squad.
    doubles = {
        ("2024-25", 24): [("A", 20.0), ("B", 18.0)],
        ("2024-25", 33): [
            ("A", 15.0), ("A", 14.0), ("A", 13.0), ("A", 11.0),  # 11.0 capped out
            ("B", 9.0), ("C", 8.0), ("D", 7.0), ("E", 6.0), ("F", 5.0),
            ("G", 4.0), ("H", 3.0), ("I", 2.0), ("J", 1.0), ("K", 0.5), ("L", 0.4),
            ("M", 0.3),
        ],
    }
    reserve = _reserve_from_doubles(
        doubles, maximum_per_club=3, squad_size=15, bench_size=4
    )
    # Squad = top 15 after the club cap drops A's 11.0: ...,1.0,0.5,0.4,0.3.
    # Triple Captain = best pick (15.0); Bench Boost = last four (0.5+0.4+0.3 ... )
    assert reserve[Chip.TRIPLE_CAPTAIN] == 15.0
    # bench = ranks 12-15 of the capped squad: 1.0, 0.5, 0.4, 0.3
    assert reserve[Chip.BENCH_BOOST] == round(1.0 + 0.5 + 0.4 + 0.3, 3)


def test_chip_timing_holds_for_an_expected_but_unscheduled_double() -> None:
    current_ids = frozenset(
        {"1", "2", "4", "5", "6", "7", "8", "11", "12", "13", "14", "15", "18", "19", "20"}
    )
    candidates = tuple(
        CandidatePlayer(
            **{
                **player.__dict__,
                "expected_points": 12.0,
                "gameweek_expected_points": 4.0,
                "gameweek_values": (
                    GameweekPlayerValue(24, 4.0, 0.9),
                    GameweekPlayerValue(25, 4.0, 0.9),
                ),
            }
        )
        for player in _candidates()
    )
    kwargs = dict(
        candidate_gameweeks=(24, 25),  # second half, stops well before GW38
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )

    # No reserve: the horizon does not reach expiry, but nothing stops a play.
    without = recommend_chip_timing(Chip.BENCH_BOOST, candidates, **kwargs)
    assert not without.horizon_reaches_set_expiry
    assert not without.hold_for_expected_double

    # A large expected double reserve makes every visible week lose to it, so the
    # recommendation is to hold rather than waste the chip on an ordinary week.
    held = recommend_chip_timing(
        Chip.BENCH_BOOST, candidates,
        expected_double_reserve=999.0, reserve_until_gameweek=37, **kwargs,
    )
    assert held.hold_for_expected_double
    assert held.expected_double_reserve == 999.0
    assert all(
        option.future_opportunity_cost >= 999.0 for option in held.options
    )
    assert "Hold" in held.explanation


def test_chip_timing_skips_illegal_weeks_and_does_not_cross_chip_sets() -> None:
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

    def at_gameweeks(first: int, second: int):
        return tuple(
            CandidatePlayer(
                **{
                    **player.__dict__,
                    "expected_points": 10.0,
                    "gameweek_values": (
                        GameweekPlayerValue(first, 4.0, 0.9),
                        GameweekPlayerValue(second, 8.0, 0.9),
                    ),
                }
            )
            for player in _candidates()
        )

    free_hit = recommend_chip_timing(
        Chip.FREE_HIT,
        at_gameweeks(1, 2),
        candidate_gameweeks=(1, 2),
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )
    assert tuple(option.gameweek_number for option in free_hit.options) == (2,)

    triple_captain = recommend_chip_timing(
        Chip.TRIPLE_CAPTAIN,
        at_gameweeks(19, 20),
        candidate_gameweeks=(19, 20),
        previous_chip_gameweeks=(),
        budget_tenths=1000,
        rules=RULES,
        current_player_ids=current_ids,
    )
    assert tuple(option.gameweek_number for option in triple_captain.options) == (19,)
    assert triple_captain.horizon_reaches_set_expiry
