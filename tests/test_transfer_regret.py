from __future__ import annotations

from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.backtest import ProjectionBacktester
from fpl_engine.config import load_season_rules
from fpl_engine.decision_evaluation import (
    RealisedPlayerOutcome,
    TransferReplayWeek,
    replay_transfer_continuity,
)
from fpl_engine.domain import Position
from fpl_engine.evaluation import evaluate_transfer_regret
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.optimisation import CandidatePlayer
from fpl_engine.transfers import CurrentSquad

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
OWNED = frozenset(
    {"1", "2", "4", "5", "6", "7", "8", "11", "12", "13", "14", "15", "18", "19", "20"}
)


def _candidates(points: dict[str, float] | None = None):
    positions = (
        *(Position.GK for _ in range(3)),
        *(Position.DEF for _ in range(7)),
        *(Position.MID for _ in range(7)),
        *(Position.FWD for _ in range(5)),
    )
    scores = points or {}
    return tuple(
        CandidatePlayer(
            source_player_id=str(index),
            web_name=f"Player {index}",
            team_id=str((index - 1) % 8 + 1),
            team_short_name=f"T{(index - 1) % 8 + 1}",
            position=position,
            price_tenths=50,
            expected_points=scores.get(str(index), 20.0 + index),
            gameweek_expected_points=2.0 + index / 10,
            appearance_probability=0.9,
        )
        for index, position in enumerate(positions, start=1)
    )


def _squad(chips: tuple[str, ...] = ()) -> CurrentSquad:
    return CurrentSquad(
        player_ids=OWNED,
        selling_prices_tenths={player_id: 50 for player_id in OWNED},
        bank_tenths=0,
        free_transfers=1,
        available_chips=chips,
    )


def test_the_state_each_decision_was_taken_from_is_recorded() -> None:
    candidates = _candidates()
    outcomes = tuple(
        RealisedPlayerOutcome(player.source_player_id, 3, 90) for player in candidates
    )

    report = replay_transfer_continuity(
        (
            TransferReplayWeek(2, candidates, outcomes),
            TransferReplayWeek(3, candidates, outcomes),
        ),
        _squad(chips=("wildcard", "bench_boost")),
        rules=RULES,
        max_transfers_per_week=1,
    )

    first = report.weeks[0].state
    assert first.gameweek_number == 2
    assert set(first.player_ids) == OWNED
    assert first.free_transfers == 1
    assert first.bank_tenths == 0
    assert first.max_transfers == 1
    # Purchase prices are part of the state, since they set spending power.
    assert set(first.purchase_prices_tenths) == OWNED
    # Chips are carried but never spent, so both branches stay equally
    # constrained and no chip policy leaks into the comparison.
    assert first.available_chips == ("wildcard", "bench_boost")
    assert report.weeks[1].state.available_chips == ("wildcard", "bench_boost")


def test_both_branches_decide_from_the_identical_prior_state() -> None:
    candidates = _candidates()
    # Player "3" is unowned and scores heavily; only hindsight can know that.
    outcomes = tuple(
        RealisedPlayerOutcome(
            player.source_player_id,
            30 if player.source_player_id == "3" else 1,
            90,
        )
        for player in candidates
    )

    report = replay_transfer_continuity(
        (TransferReplayWeek(2, candidates, outcomes),),
        _squad(),
        rules=RULES,
        max_transfers_per_week=1,
    )

    week = report.weeks[0]
    # Hindsight can only act from the same squad, bank and free transfers, so
    # its advantage is information, not a different starting position.
    assert set(week.state.player_ids) == OWNED
    assert week.same_state_hindsight_net_points >= week.net_points
    assert week.regret == max(
        0, week.same_state_hindsight_net_points - week.net_points
    )


def _database(tmp_path):
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


def test_the_two_metrics_are_reported_separately(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        report = evaluate_transfer_regret(database, run_id, RULES)
    finally:
        database.__exit__(None, None, None)

    assert report.decisions == 2
    assert report.gameweeks == (2, 3)
    # The gate metric does not compound; the season metric does.
    assert report.same_state_mean_regret == round(
        report.same_state_total_regret / report.decisions, 4
    )
    assert report.same_state_total_regret >= 0
    assert report.continuous_policy_points > 0
    assert any(
        "No chip is played" in limitation for limitation in report.limitations
    )
    assert any(
        "positive regret by construction" in limitation
        for limitation in report.limitations
    )


def test_transfer_regret_needs_decisions_to_score(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="at least two Gameweeks"):
            evaluate_transfer_regret(database, run_id, RULES, first_gameweek=3)
    finally:
        database.__exit__(None, None, None)


def test_transfer_regret_rejects_rules_from_another_season(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="must match the backtest season"):
            evaluate_transfer_regret(
                database,
                run_id,
                load_season_rules(Path("config/seasons/2025-26.json")),
            )
    finally:
        database.__exit__(None, None, None)
