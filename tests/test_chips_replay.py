from __future__ import annotations

from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.backtest import ProjectionBacktester
from fpl_engine.chip_state import (
    SCORING_CHIPS,
    ChipLedger,
    ScoringChipPolicy,
)
from fpl_engine.config import load_season_rules
from fpl_engine.decision_evaluation import (
    RealisedPlayerOutcome,
    TransferReplayWeek,
    replay_transfer_continuity,
    resolve_squad_gameweek,
)
from fpl_engine.domain import Chip, Position
from fpl_engine.evaluation import evaluate_chip_regret
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    optimise_full_squad,
)
from fpl_engine.transfers import CurrentSquad

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
SHAPE = (
    *((Position.GK, index) for index in range(2)),
    *((Position.DEF, index) for index in range(5)),
    *((Position.MID, index) for index in range(5)),
    *((Position.FWD, index) for index in range(3)),
)


def test_a_chip_is_spent_once_per_set_and_blocks_its_own_gameweek() -> None:
    ledger = ChipLedger()

    # Bench Boost is legal in GW2 and, once played, gone for that half.
    assert Chip.BENCH_BOOST in ledger.available(2, RULES)
    after = ledger.after_playing(Chip.BENCH_BOOST, 2, RULES)
    assert Chip.BENCH_BOOST not in after.available(5, RULES)
    # The second set restores it from the configured start Gameweek.
    assert Chip.BENCH_BOOST in after.available(25, RULES)
    with pytest.raises(ValueError, match="already been used"):
        after.after_playing(Chip.BENCH_BOOST, 5, RULES)


def test_only_one_chip_may_be_active_in_a_gameweek() -> None:
    ledger = ChipLedger().after_playing(Chip.BENCH_BOOST, 4, RULES)

    assert ledger.chip_for(4) == Chip.BENCH_BOOST
    assert ledger.available(4, RULES) == ()
    with pytest.raises(ValueError, match="already has a chip active"):
        ledger.after_playing(Chip.TRIPLE_CAPTAIN, 4, RULES)
    # A different Gameweek is unaffected.
    assert Chip.TRIPLE_CAPTAIN in ledger.available(5, RULES)


def test_the_default_policy_never_plays_anything() -> None:
    policy = ScoringChipPolicy()

    assert policy.plays_anything is False
    assert (
        policy.choose(
            {chip: 1000.0 for chip in SCORING_CHIPS},
            SCORING_CHIPS,
        )
        is None
    )


def test_a_policy_plays_the_largest_gain_that_clears_its_threshold() -> None:
    policy = ScoringChipPolicy(
        bench_boost_threshold=5.0,
        triple_captain_threshold=5.0,
    )

    chosen = policy.choose(
        {Chip.BENCH_BOOST: 6.0, Chip.TRIPLE_CAPTAIN: 9.0},
        SCORING_CHIPS,
    )
    assert chosen == Chip.TRIPLE_CAPTAIN
    # A chip that is not legal this week cannot be chosen however large.
    assert (
        policy.choose(
            {Chip.BENCH_BOOST: 6.0, Chip.TRIPLE_CAPTAIN: 99.0},
            (Chip.BENCH_BOOST,),
        )
        == Chip.BENCH_BOOST
    )
    # Below threshold, nothing is played.
    assert (
        policy.choose({Chip.BENCH_BOOST: 4.9}, (Chip.BENCH_BOOST,)) is None
    )


def _forced_squad():
    candidates = tuple(
        CandidatePlayer(
            source_player_id=f"{position.value}{slot}",
            web_name=f"{position.value}{slot}",
            team_id=str(index % 8),
            team_short_name=f"T{index % 8}",
            position=position,
            price_tenths=60,
            expected_points=10.0 - index * 0.1,
            gameweek_expected_points=10.0 - index * 0.1,
            appearance_probability=0.9,
            gameweek_values=(
                GameweekPlayerValue(1, 10.0 - index * 0.1, 0.9, 0.8),
            ),
        )
        for index, (position, slot) in enumerate(SHAPE)
    )
    return optimise_full_squad(candidates, budget_tenths=1000, rules=RULES)


def _outcomes(points: int = 2):
    return {
        f"{position.value}{slot}": RealisedPlayerOutcome(
            source_player_id=f"{position.value}{slot}",
            points=points,
            minutes=90,
        )
        for position, slot in SHAPE
    }


def test_bench_boost_scores_the_bench_that_would_not_have_counted() -> None:
    squad = _forced_squad()
    outcomes = _outcomes()

    plain = resolve_squad_gameweek(squad, outcomes, RULES, 1)
    boosted = resolve_squad_gameweek(
        squad, outcomes, RULES, 1, active_chip=Chip.BENCH_BOOST
    )

    # Fifteen players count instead of eleven, at two points each.
    assert len(plain.scoring_player_ids) == 11
    assert len(boosted.scoring_player_ids) == 15
    assert boosted.total_points - plain.total_points == 8
    assert boosted.active_chip == "bench_boost"


def test_triple_captain_adds_one_more_captain_multiple() -> None:
    squad = _forced_squad()
    outcomes = _outcomes()

    plain = resolve_squad_gameweek(squad, outcomes, RULES, 1)
    tripled = resolve_squad_gameweek(
        squad, outcomes, RULES, 1, active_chip=Chip.TRIPLE_CAPTAIN
    )

    # The captain already counts twice; the chip makes it three times.
    assert tripled.total_points - plain.total_points == 2
    assert tripled.effective_captain_id == plain.effective_captain_id


def _replay_candidates():
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
            expected_points=20.0 + index,
            gameweek_expected_points=2.0 + index / 10,
            appearance_probability=0.9,
        )
        for index, position in enumerate(positions, start=1)
    )


OWNED = frozenset(
    {"1", "2", "4", "5", "6", "7", "8", "11", "12", "13", "14", "15", "18", "19", "20"}
)


def _replay(policy: ScoringChipPolicy | None):
    candidates = _replay_candidates()
    outcomes = tuple(
        RealisedPlayerOutcome(player.source_player_id, 3, 90)
        for player in candidates
    )
    return replay_transfer_continuity(
        (
            TransferReplayWeek(2, candidates, outcomes),
            TransferReplayWeek(3, candidates, outcomes),
        ),
        CurrentSquad(
            player_ids=OWNED,
            selling_prices_tenths={player_id: 50 for player_id in OWNED},
            bank_tenths=0,
            free_transfers=1,
            available_chips=("wildcard", "bench_boost", "triple_captain"),
        ),
        rules=RULES,
        max_transfers_per_week=1,
        chip_policy=policy,
    )


def test_a_replay_plays_no_chip_unless_a_policy_is_declared() -> None:
    report = _replay(None)

    assert report.chip_plays == ()
    assert all(week.active_chip is None for week in report.weeks)
    # Availability is untouched when nothing is spent.
    assert report.weeks[1].state.available_chips == (
        "wildcard",
        "bench_boost",
        "triple_captain",
    )


def test_a_declared_policy_spends_a_chip_and_removes_it_from_the_state() -> None:
    report = _replay(ScoringChipPolicy(bench_boost_threshold=0.0))

    assert len(report.chip_plays) == 1
    play = report.chip_plays[0]
    assert play["chip"] == "bench_boost"
    assert play["gameweek_number"] == 2
    first = report.weeks[0]
    assert first.active_chip == "bench_boost"
    assert first.chip_realised_gain is not None
    # Wildcard is untouched: the replay cannot value it, so it must not drop it.
    assert "wildcard" in report.weeks[1].state.available_chips
    assert "bench_boost" not in report.weeks[1].state.available_chips


def test_every_week_records_what_each_chip_would_have_gained() -> None:
    report = _replay(None)

    counterfactual = report.chip_counterfactual
    assert set(counterfactual) == {"bench_boost", "triple_captain"}
    for entries in counterfactual.values():
        assert [entry["gameweek_number"] for entry in entries] == [2, 3]
        # Chip timing can only be scored if every alternative week is valued.
        assert all(entry["legal"] for entry in entries)
        assert all(entry["realised_gain"] >= 0 for entry in entries)


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


def test_an_unplayed_chip_is_charged_the_best_week_it_missed(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        report = evaluate_chip_regret(database, run_id, RULES)
    finally:
        database.__exit__(None, None, None)

    assert set(report.by_chip) == {"bench_boost", "triple_captain"}
    assert report.played == ()
    for chip, summary in report.by_chip.items():
        assert summary["played_gameweek"] is None
        assert summary["realised_gain"] == 0
        # Not playing is itself a timing decision, so it is charged in full.
        assert summary["regret"] == summary["best_legal_gain"]
        assert chip in {"bench_boost", "triple_captain"}
    assert report.total_regret == sum(
        summary["regret"] for summary in report.by_chip.values()
    )
    assert any(
        "Wildcard and " in limitation for limitation in report.limitations
    )


def test_playing_a_chip_reduces_its_regret_to_the_timing_difference(
    tmp_path,
) -> None:
    database, run_id = _database(tmp_path)
    try:
        never = evaluate_chip_regret(database, run_id, RULES)
        played = evaluate_chip_regret(
            database,
            run_id,
            RULES,
            chip_policy=ScoringChipPolicy(bench_boost_threshold=0.0),
        )
    finally:
        database.__exit__(None, None, None)

    boost = played.by_chip["bench_boost"]
    assert boost["played_gameweek"] is not None
    # Regret is now only the gap to the best week, not the whole chip.
    assert boost["regret"] == max(
        0, boost["best_legal_gain"] - boost["realised_gain"]
    )
    assert boost["regret"] <= never.by_chip["bench_boost"]["regret"]
