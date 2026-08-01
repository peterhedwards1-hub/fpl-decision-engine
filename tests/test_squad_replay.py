from __future__ import annotations

from dataclasses import replace
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
    optimise_full_squad,
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


def _rotating_result():
    """A two-Gameweek squad whose XI and bench swap between the weeks."""

    shape = (
        *((Position.GK, f"gk{index}") for index in range(2)),
        *((Position.DEF, f"def{index}") for index in range(5)),
        *((Position.MID, f"mid{index}") for index in range(5)),
        *((Position.FWD, f"fwd{index}") for index in range(3)),
    )
    rotating = {"mid3": (9.0, 0.0), "mid4": (0.0, 9.0)}
    candidates = tuple(
        CandidatePlayer(
            source_player_id=identifier,
            web_name=identifier,
            team_id=str(index % 8),
            team_short_name=f"T{index % 8}",
            position=position,
            price_tenths=60,
            expected_points=sum(rotating.get(identifier, (5.0, 5.0))),
            gameweek_expected_points=rotating.get(identifier, (5.0, 5.0))[0],
            appearance_probability=0.9,
            gameweek_values=(
                GameweekPlayerValue(1, rotating.get(identifier, (5.0, 5.0))[0], 0.9, 0.8),
                GameweekPlayerValue(2, rotating.get(identifier, (5.0, 5.0))[1], 0.9, 0.8),
            ),
        )
        for index, (position, identifier) in enumerate(shape)
    )
    return optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)


def _rotating_lookup(result, *, blanking: frozenset[str] = frozenset()):
    return {
        (player.source_player_id, gameweek): GameweekPlayerValue(
            gameweek_number=gameweek,
            expected_points=0.0 if player.source_player_id in blanking else 2.0,
            appearance_probability=(
                0.0 if player.source_player_id in blanking else 1.0
            ),
        )
        for player in result.players
        for gameweek in (1, 2)
    }


def test_a_rotated_gameweek_substitutes_from_that_weeks_bench() -> None:
    result = _rotating_result()
    first, second = result.gameweek_plans
    # mid3 starts in GW1 and is benched in GW2; mid4 does the reverse.
    assert "mid4" in second.starting_player_ids
    assert "mid3" in second.bench_player_ids

    clean = _replayed_squad_points(result, _rotating_lookup(result), (2,), RULES)
    blanked = _replayed_squad_points(
        result,
        _rotating_lookup(result, blanking=frozenset({"mid4"})),
        (2,),
        RULES,
    )

    # Scoring GW2 against the opening Gameweek's bench would list mid4 as both a
    # starter and a substitute, and omit mid3 entirely.
    assert clean == 24.0
    assert blanked == 24.0


def test_replay_rejects_a_plan_whose_bench_overlaps_its_starters() -> None:
    result = _rotating_result()
    second = result.gameweek_plans[1]
    corrupted = replace(
        result,
        gameweek_plans=(
            result.gameweek_plans[0],
            replace(
                second,
                bench_player_ids=(second.bench_player_ids[0],)
                + tuple(sorted(second.starting_player_ids))[:3],
            ),
        ),
    )

    with pytest.raises(ValueError, match="lists a starter on its bench"):
        _replayed_squad_points(corrupted, _rotating_lookup(result), (2,), RULES)
