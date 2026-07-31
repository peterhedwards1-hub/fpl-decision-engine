from __future__ import annotations

from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.backtest import ProjectionBacktester
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.evaluation import (
    _replayed_squad_points,
    evaluate_legal_squad_regret,
    replay_backtest_transfer_continuity,
)
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.optimisation import (
    CandidatePlayer,
    FullSquadResult,
    GameweekLineupPlan,
    GameweekPlayerValue,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
SQUAD_SHAPE = (
    (Position.GK, 2),
    (Position.DEF, 5),
    (Position.MID, 5),
    (Position.FWD, 3),
)
# A 4-4-2 XI leaves a bench of one keeper, one defender, one midfielder and one
# forward, so every outfield blank has a same-position replacement available.
STARTING_IDS = frozenset(
    {
        "GK0",
        "DEF0",
        "DEF1",
        "DEF2",
        "DEF3",
        "MID0",
        "MID1",
        "MID2",
        "MID3",
        "FWD0",
        "FWD1",
    }
)
BENCH_ORDER = ("GK1", "DEF4", "MID4", "FWD2")


def _squad() -> FullSquadResult:
    players = tuple(
        CandidatePlayer(
            source_player_id=f"{position.value}{slot}",
            web_name=f"{position.value}{slot}",
            team_id=str(index % 8 + 1),
            team_short_name=f"T{index % 8 + 1}",
            position=position,
            price_tenths=60,
            expected_points=4.0,
            gameweek_expected_points=4.0,
        )
        for index, (position, slot) in enumerate(
            (position, slot)
            for position, count in SQUAD_SHAPE
            for slot in range(count)
        )
    )
    return FullSquadResult(
        players=players,
        starting_player_ids=STARTING_IDS,
        bench_player_ids=BENCH_ORDER,
        captain_id="MID0",
        vice_captain_id="MID1",
        total_cost_tenths=900,
        horizon_expected_points=0.0,
        gameweek_expected_points=0.0,
        expected_bench_contribution=0.0,
        expected_captain_contribution=0.0,
        gameweek_plans=(
            GameweekLineupPlan(
                gameweek_number=1,
                starting_player_ids=STARTING_IDS,
                captain_id="MID0",
                vice_captain_id="MID1",
            ),
        ),
        solver_status="Optimal",
        proof="fixture",
    )


def _lookup(squad: FullSquadResult, *, blanking: frozenset[str] = frozenset()):
    return {
        (player.source_player_id, 1): GameweekPlayerValue(
            gameweek_number=1,
            expected_points=0.0 if player.source_player_id in blanking else 2.0,
            appearance_probability=(
                0.0 if player.source_player_id in blanking else 1.0
            ),
        )
        for player in squad.players
    }


def test_full_xi_scores_every_starter_plus_the_captain() -> None:
    squad = _squad()

    points = _replayed_squad_points(squad, _lookup(squad), (1,), RULES)

    # Eleven starters on two points each, and the captain counted twice.
    assert points == 24.0


def test_a_blanking_starter_is_replaced_by_the_bench() -> None:
    squad = _squad()

    points = _replayed_squad_points(
        squad,
        _lookup(squad, blanking=frozenset({"DEF0"})),
        (1,),
        RULES,
    )

    # The old measure charged the squad the full two points for the absence.
    # The bench defender comes on, so the week is worth the same as a clean one.
    assert points == 24.0


def test_captain_falls_back_to_the_vice_when_the_captain_blanks() -> None:
    squad = _squad()

    points = _replayed_squad_points(
        squad,
        _lookup(squad, blanking=frozenset({"MID0"})),
        (1,),
        RULES,
    )

    # Ten starters plus the substituted midfielder, and the vice captained.
    assert points == 24.0


def test_more_blanks_than_the_bench_can_cover_lose_points() -> None:
    squad = _squad()

    points = _replayed_squad_points(
        squad,
        _lookup(squad, blanking=frozenset({"DEF0", "DEF1", "MID2", "FWD0"})),
        (1,),
        RULES,
    )

    # Only three outfield bench players exist, so one absence stays uncovered.
    assert points == 22.0


def _backtest_database(tmp_path):
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
    report = ProjectionBacktester(database, RULES).run(
        season_code="2026-27",
        origin_gameweek_start=1,
        origin_gameweek_end=3,
        horizon_gameweeks=1,
    )
    return database, report.backtest_run_id


def test_legal_squad_regret_reports_autosub_replayed_points(tmp_path) -> None:
    database, run_id = _backtest_database(tmp_path)
    try:
        report = evaluate_legal_squad_regret(
            database,
            run_id,
            RULES,
            methods=("model",),
        )
    finally:
        database.__exit__(None, None, None)

    assert report.origins
    for origin in report.origins:
        assert origin.regret >= 0
        assert origin.realised_points >= 0
    assert any(
        "exact autosubs" in limitation for limitation in report.limitations
    )
    assert any(
        "replay_backtest_transfer_continuity" in limitation
        for limitation in report.limitations
    )


def test_continuity_replay_carries_one_squad_across_gameweeks(tmp_path) -> None:
    database, run_id = _backtest_database(tmp_path)
    try:
        replay = replay_backtest_transfer_continuity(
            database,
            run_id,
            RULES,
            max_transfers_per_week=1,
        )
    finally:
        database.__exit__(None, None, None)

    assert replay["gameweeks"] == [1, 2, 3]
    assert replay["opening_gameweek"] == 1
    # The opening squad is picked, not transferred into, so only the later
    # Gameweeks carry a transfer decision.
    assert [week["gameweek_number"] for week in replay["weeks"]] == [2, 3]
    assert replay["opening_squad_cost_tenths"] <= RULES.squad.budget_tenths
    assert replay["season_points"] == (
        replay["opening_squad_points"] + replay["total_net_points"]
    )
    for week in replay["weeks"]:
        assert week["transfers_made"] <= 1
        assert week["points_hit"] >= 0
        assert week["regret"] >= 0
    assert any(
        "Chips are not played" in limitation for limitation in replay["limitations"]
    )


def test_continuity_replay_needs_two_gameweeks(tmp_path) -> None:
    database, run_id = _backtest_database(tmp_path)
    try:
        with pytest.raises(ValueError, match="at least two Gameweeks"):
            replay_backtest_transfer_continuity(
                database,
                run_id,
                RULES,
                first_gameweek=3,
            )
    finally:
        database.__exit__(None, None, None)


def test_continuity_replay_rejects_rules_from_another_season(tmp_path) -> None:
    database, run_id = _backtest_database(tmp_path)
    try:
        with pytest.raises(ValueError, match="must match the backtest season"):
            replay_backtest_transfer_continuity(
                database,
                run_id,
                load_season_rules(Path("config/seasons/2025-26.json")),
            )
    finally:
        database.__exit__(None, None, None)
