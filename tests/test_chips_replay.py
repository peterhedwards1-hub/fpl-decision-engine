from __future__ import annotations

from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.backtest import ProjectionBacktester
from fpl_engine.chip_state import (
    SCORING_CHIPS,
    ChipDecisionContext,
    ChipLedger,
    LookaheadChipPolicy,
    ReserveChipPolicy,
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


def _context(values: dict, legal: tuple) -> ChipDecisionContext:
    return ChipDecisionContext(
        gameweek_number=2,
        values_by_gameweek={2: values},
        legal_by_gameweek={2: legal},
        expiry_gameweek=19,
    )


def test_the_default_policy_never_plays_anything() -> None:
    policy = ScoringChipPolicy()

    assert policy.plays_anything is False
    assert (
        policy.choose(
            _context({chip: 1000.0 for chip in SCORING_CHIPS}, SCORING_CHIPS)
        )
        is None
    )


def test_a_policy_plays_the_largest_gain_that_clears_its_threshold() -> None:
    policy = ScoringChipPolicy(
        bench_boost_threshold=5.0,
        triple_captain_threshold=5.0,
    )

    chosen = policy.choose(
        _context(
            {Chip.BENCH_BOOST: 6.0, Chip.TRIPLE_CAPTAIN: 9.0}, SCORING_CHIPS
        )
    )
    assert chosen == Chip.TRIPLE_CAPTAIN
    # A chip that is not legal this week cannot be chosen however large.
    assert (
        policy.choose(
            _context(
                {Chip.BENCH_BOOST: 6.0, Chip.TRIPLE_CAPTAIN: 99.0},
                (Chip.BENCH_BOOST,),
            )
        )
        == Chip.BENCH_BOOST
    )
    # Below threshold, nothing is played.
    assert (
        policy.choose(_context({Chip.BENCH_BOOST: 4.9}, (Chip.BENCH_BOOST,)))
        is None
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


def _double_gameweek_candidates():
    """GW2 is ordinary; GW4 is a double, worth roughly twice as much."""

    positions = (
        *(Position.GK for _ in range(3)),
        *(Position.DEF for _ in range(7)),
        *(Position.MID for _ in range(7)),
        *(Position.FWD for _ in range(5)),
    )
    weekly = {2: 1.0, 3: 1.0, 4: 2.0, 5: 1.0}
    return tuple(
        CandidatePlayer(
            source_player_id=str(index),
            web_name=f"Player {index}",
            team_id=str((index - 1) % 8 + 1),
            team_short_name=f"T{(index - 1) % 8 + 1}",
            position=position,
            price_tenths=50,
            expected_points=sum(
                (5.0 - index / 100) * multiplier for multiplier in weekly.values()
            ),
            gameweek_expected_points=5.0 - index / 100,
            appearance_probability=0.9,
            gameweek_values=tuple(
                GameweekPlayerValue(
                    gameweek,
                    (5.0 - index / 100) * multiplier,
                    0.9,
                    0.8,
                )
                for gameweek, multiplier in weekly.items()
            ),
        )
        for index, position in enumerate(positions, start=1)
    )


def _double_replay(policy):
    candidates = _double_gameweek_candidates()
    outcomes = tuple(
        RealisedPlayerOutcome(player.source_player_id, 3, 90)
        for player in candidates
    )
    return replay_transfer_continuity(
        tuple(
            TransferReplayWeek(gameweek, candidates, outcomes)
            for gameweek in (2, 3, 4, 5)
        ),
        CurrentSquad(
            player_ids=OWNED,
            selling_prices_tenths={player_id: 50 for player_id in OWNED},
            bank_tenths=0,
            free_transfers=1,
            available_chips=("bench_boost", "triple_captain"),
        ),
        rules=RULES,
        max_transfers_per_week=1,
        chip_policy=policy,
    )


def test_a_chip_value_rises_with_a_double_gameweek() -> None:
    report = _double_replay(None)

    first = report.weeks[0]
    # The look-ahead saw GW4 worth roughly twice GW2, without playing anything.
    assert first.chip_lookahead_gameweeks == (3, 4, 5)
    later = first.chip_best_later_forecast
    now = report.chip_counterfactual["bench_boost"][0]["realised_gain"]
    assert later["bench_boost"] > 0
    assert now >= 0


def test_a_threshold_policy_spends_the_chip_before_the_double() -> None:
    report = _double_replay(ScoringChipPolicy(bench_boost_threshold=1.0))

    # Myopic: the first week clearing the threshold takes it, and GW4 is gone.
    assert [play["gameweek_number"] for play in report.chip_plays] == [2]


def test_a_lookahead_policy_waits_for_the_double_gameweek() -> None:
    report = _double_replay(LookaheadChipPolicy(enabled=True))

    played = {play["chip"]: play["gameweek_number"] for play in report.chip_plays}
    # Holding the chip while a better Gameweek remains is the whole point.
    assert played["bench_boost"] == 4
    assert report.weeks[0].active_chip is None
    assert report.weeks[0].chip_best_later_forecast["bench_boost"] > 0


def test_a_lookahead_policy_records_how_far_it_could_see() -> None:
    report = _double_replay(LookaheadChipPolicy(enabled=True))

    first = report.weeks[0]
    # The window stops at the projection horizon, well short of the GW19 set
    # expiry, and that shortfall is reported rather than assumed away.
    assert first.chip_lookahead_reaches_expiry is False
    assert first.chip_lookahead_gameweeks == (3, 4, 5)


def test_a_disabled_lookahead_policy_plays_nothing() -> None:
    report = _double_replay(LookaheadChipPolicy())

    assert report.chip_plays == ()


def test_a_margin_makes_the_policy_wait_when_weeks_are_close() -> None:
    context = ChipDecisionContext(
        gameweek_number=2,
        values_by_gameweek={
            2: {Chip.BENCH_BOOST: 6.0, Chip.TRIPLE_CAPTAIN: 0.0},
            3: {Chip.BENCH_BOOST: 5.5, Chip.TRIPLE_CAPTAIN: 0.0},
        },
        legal_by_gameweek={
            2: (Chip.BENCH_BOOST,),
            3: (Chip.BENCH_BOOST,),
        },
        expiry_gameweek=19,
    )

    # Without a margin the marginally better week wins.
    assert LookaheadChipPolicy(enabled=True).choose(context) == Chip.BENCH_BOOST
    # With one, a close call defers to the less certain later week.
    assert (
        LookaheadChipPolicy(enabled=True, margin=1.0).choose(context) is None
    )
    # A minimum gain refuses trivial plays outright.
    assert (
        LookaheadChipPolicy(enabled=True, minimum_gain=10.0).choose(context)
        is None
    )


def _second_half_context(now_bench: float, later_bench: float, *, top: int) -> ChipDecisionContext:
    """A second-half context whose look-ahead stops before the set expires."""

    return ChipDecisionContext(
        gameweek_number=25,
        values_by_gameweek={
            gw: {Chip.BENCH_BOOST: (now_bench if gw == 25 else later_bench)}
            for gw in range(25, top + 1)
        },
        legal_by_gameweek={gw: (Chip.BENCH_BOOST,) for gw in range(25, top + 1)},
        expiry_gameweek=38,
    )


def test_reserve_policy_holds_a_chip_for_an_expected_unscheduled_double() -> None:
    # An ordinary week that beats every *visible* later week but not the
    # expected double. The look-ahead would spend it; the reserve holds it.
    context = _second_half_context(6.0, 5.0, top=30)
    assert not context.reaches_expiry  # GW30 < GW38, the double is still hidden

    reserve = ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0},
        reserve_until_gameweek=37,
        enabled=True,
    )
    assert LookaheadChipPolicy(enabled=True).choose(context) == Chip.BENCH_BOOST
    assert reserve.choose(context) is None
    assert reserve.reserve_for(Chip.BENCH_BOOST, context) == 12.0


def test_reserve_policy_plays_a_week_that_beats_the_expected_double() -> None:
    # A genuine big week (a real double, visible now) clears the reserve bar.
    context = _second_half_context(15.0, 5.0, top=30)
    reserve = ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0},
        reserve_until_gameweek=37,
        enabled=True,
    )
    assert reserve.choose(context) == Chip.BENCH_BOOST


def test_reserve_drops_once_the_projection_reaches_the_set_expiry() -> None:
    # When the look-ahead reaches expiry, any real double is already visible, so
    # the reserve must not block: the policy falls back to plain look-ahead.
    context = _second_half_context(6.0, 5.0, top=38)
    assert context.reaches_expiry
    reserve = ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0},
        reserve_until_gameweek=37,
        enabled=True,
    )
    assert reserve.reserve_for(Chip.BENCH_BOOST, context) == 0.0
    assert reserve.choose(context) == Chip.BENCH_BOOST


def test_reserve_drops_after_its_window_so_the_last_week_is_play_or_lose() -> None:
    context = ChipDecisionContext(
        gameweek_number=37,
        values_by_gameweek={37: {Chip.BENCH_BOOST: 6.0}},
        legal_by_gameweek={37: (Chip.BENCH_BOOST,)},
        expiry_gameweek=38,
    )
    reserve = ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0},
        reserve_until_gameweek=37,
        enabled=True,
    )
    assert reserve.reserve_for(Chip.BENCH_BOOST, context) == 0.0
    assert reserve.choose(context) == Chip.BENCH_BOOST


def test_reserve_discount_trades_the_expected_double_against_a_sure_week() -> None:
    context = _second_half_context(7.0, 5.0, top=30)
    full = ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0},
        reserve_until_gameweek=37,
        enabled=True,
    )
    # A half-discounted reserve (bar 6.0) lets a 7.0 week through; the full
    # reserve (bar 12.0) still holds.
    discounted = ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0},
        reserve_until_gameweek=37,
        reserve_discount=0.5,
        enabled=True,
    )
    assert full.choose(context) is None
    assert discounted.choose(context) == Chip.BENCH_BOOST


def test_a_disabled_reserve_policy_plays_nothing() -> None:
    context = _second_half_context(15.0, 5.0, top=30)
    assert ReserveChipPolicy(
        reserve_by_chip={Chip.BENCH_BOOST: 12.0}, reserve_until_gameweek=37
    ).choose(context) is None


def test_lookahead_stops_at_the_set_expiry() -> None:
    context = ChipDecisionContext(
        gameweek_number=18,
        values_by_gameweek={
            18: {Chip.BENCH_BOOST: 4.0},
            # A far better week, but in the second half where a first-half
            # chip cannot be played.
            22: {Chip.BENCH_BOOST: 40.0},
        },
        legal_by_gameweek={18: (Chip.BENCH_BOOST,), 22: (Chip.BENCH_BOOST,)},
        expiry_gameweek=19,
    )

    assert context.lookahead_gameweeks == ()
    assert context.best_later_value(Chip.BENCH_BOOST) == 0.0
    # Use it or lose it: waiting for GW22 would waste the chip.
    assert LookaheadChipPolicy(enabled=True).choose(context) == Chip.BENCH_BOOST
