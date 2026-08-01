from __future__ import annotations

from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.backtest import ProjectionBacktester
from fpl_engine.config import load_season_rules
from fpl_engine.decision_evaluation import (
    RealisedPlayerOutcome,
    resolve_squad_gameweek,
)
from fpl_engine.domain import Position
from fpl_engine.evaluation import evaluate_owned_captain_regret
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    optimise_full_squad,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
SHAPE = (
    *((Position.GK, index) for index in range(2)),
    *((Position.DEF, index) for index in range(5)),
    *((Position.MID, index) for index in range(5)),
    *((Position.FWD, index) for index in range(3)),
)


def _forced_squad():
    """Exactly fifteen legal players, so the squad is determined."""

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


def _outcomes(points: dict[str, int], minutes: dict[str, int]):
    return {
        f"{position.value}{slot}": RealisedPlayerOutcome(
            source_player_id=f"{position.value}{slot}",
            points=points.get(f"{position.value}{slot}", 2),
            minutes=minutes.get(f"{position.value}{slot}", 90),
        )
        for position, slot in SHAPE
    }


def test_the_scoring_lineup_is_the_set_a_captain_could_have_come_from() -> None:
    squad = _forced_squad()
    resolved = resolve_squad_gameweek(squad, _outcomes({}, {}), RULES, 1)

    # Eleven players count when everyone plays, and the armband can only have
    # been on one of them.
    assert len(resolved.scoring_player_ids) == 11
    assert resolved.effective_captain_id in resolved.scoring_player_ids
    assert resolved.substitution_count == 0


def test_a_blanking_captain_hands_the_armband_to_the_vice() -> None:
    squad = _forced_squad()
    captain = squad.captain_id
    vice = squad.vice_captain_id

    resolved = resolve_squad_gameweek(
        squad,
        _outcomes({captain: 0}, {captain: 0}),
        RULES,
        1,
    )

    assert resolved.effective_captain_id == vice
    assert captain not in resolved.scoring_player_ids


def test_neither_playing_leaves_no_captain_at_all() -> None:
    squad = _forced_squad()
    absent = {squad.captain_id: 0, squad.vice_captain_id: 0}

    resolved = resolve_squad_gameweek(squad, _outcomes(absent, absent), RULES, 1)

    assert resolved.effective_captain_id is None


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


def test_captain_regret_is_measured_within_the_owned_squad(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        report = evaluate_owned_captain_regret(database, run_id, RULES)
    finally:
        database.__exit__(None, None, None)

    assert report.samples == 3
    assert report.method == "model"
    for entry in report.gameweeks:
        assert entry.regret >= 0
        # The comparator must be attainable: a player the squad owned and who
        # actually counted, never the best scorer in the game.
        assert entry.best_available_points >= entry.effective_captain_points
        if entry.best_available_id is not None:
            assert entry.effective_captain_id is not None
    assert report.mean_regret == round(report.total_regret / report.samples, 4)
    assert any(
        "not the best player in the game" in limitation
        for limitation in report.limitations
    )


def test_captain_regret_refuses_an_unknown_method(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="Unknown owned-captain regret"):
            evaluate_owned_captain_regret(
                database, run_id, RULES, method="invented"
            )
    finally:
        database.__exit__(None, None, None)


def test_captain_regret_rejects_rules_from_another_season(tmp_path) -> None:
    database, run_id = _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="must match the backtest season"):
            evaluate_owned_captain_regret(
                database,
                run_id,
                load_season_rules(Path("config/seasons/2025-26.json")),
            )
    finally:
        database.__exit__(None, None, None)
