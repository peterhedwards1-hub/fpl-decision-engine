from __future__ import annotations

from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.projections import (
    ProjectionModelConfig,
    RatesProjectionModel,
    _bounded_minutes_reconciliation,
    _config_hash,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def _player(source_id: str, *, minutes: float = 900.0, starts: float = 10.0) -> dict:
    return {
        "source_player_id": source_id,
        "player_season_id": int(source_id),
        "web_name": source_id,
        "team_id": 1,
        "team_short_name": "NTH",
        "position": "FWD",
        "status": "a",
        "chance_of_playing_next_round": None,
        "matches": 10,
        "starts": starts,
        "zero_minute_records": 0,
        "appearances": 10 if minutes else 0,
        "sixty_appearances": 10 if minutes >= 600 else 0,
        "minutes": minutes,
        "recent_matches": 0,
        "recent_starts": 0,
        "recent_zero_minute_records": 0,
        "recent_appearances": 0,
        "recent_sixty_appearances": 0,
        "recent_minutes": 0,
        "expected_goals": 10.0,
        "expected_assists": 5.0,
        "recent_expected_goals": 0.0,
        "recent_expected_assists": 0.0,
    }


def test_config_hash_includes_challenger_parameters() -> None:
    base = ProjectionModelConfig()
    challenger = ProjectionModelConfig(scoring_event_source="coherent_team_allocation")
    assert _config_hash(base) != _config_hash(challenger)
    assert len(_config_hash(challenger)) == 64


def test_bounded_reconciliation_preserves_order_and_limits() -> None:
    players = [
        {"_expected_minutes_per_fixture": 80.0},
        {"_expected_minutes_per_fixture": 10.0},
    ]
    result = _bounded_minutes_reconciliation(
        players,
        target=90.0,
        max_relative=0.10,
        max_absolute=5.0,
    )
    assert result[0] > result[1]
    assert result[0] <= 85.0
    assert result[1] <= 15.0


def test_coherent_allocation_reconciles_goals_and_assists() -> None:
    model = RatesProjectionModel(
        database=None,  # type: ignore[arg-type]
        rules=RULES,
        config=ProjectionModelConfig(
            scoring_event_source="coherent_team_allocation",
            enforce_team_minutes=False,
        ),
    )
    first = _player("1")
    second = _player("2", minutes=450.0, starts=5.0)
    players = [first, second]
    fixtures = {1: [{"home_team_id": 1, "away_team_id": 2}]}
    strengths = {
        "1": {
            "attack": 1.0,
            "defence": 1.0,
            "league_average_goals": 1.5,
        },
        "2": {
            "attack": 1.0,
            "defence": 1.0,
            "league_average_goals": 1.5,
        },
    }
    model._prepare_minutes(
        players,
        season_code="unused",
        start_gameweek=1,
        use_availability=True,
    )
    model._prepare_coherent_event_allocations(
        players, fixtures, strengths, start_gameweek=1, horizon_gameweeks=1
    )
    allocations = [player["_coherent_by_gameweek"][1] for player in players]
    team_goals = 1.5 * model.config.home_attack_multiplier
    assert sum(row["goals"] for row in allocations) == pytest.approx(team_goals)
    assert sum(row["assists"] for row in allocations) == pytest.approx(
        team_goals * (1.0 - model.config.coherent_assist_unassisted_goal_fraction)
    )


def test_zero_participation_gets_no_events_and_penalty_share_does_not_change_team_goals() -> None:
    model = RatesProjectionModel(
        database=None,  # type: ignore[arg-type]
        rules=RULES,
        config=ProjectionModelConfig(
            scoring_event_source="coherent_team_allocation",
            enforce_team_minutes=False,
        ),
    )
    available = _player("1")
    unavailable = _player("2")
    unavailable["status"] = "i"
    players = [available, unavailable]
    fixtures = {1: [{"home_team_id": 1, "away_team_id": 2}]}
    strengths = {
        "1": {"attack": 1.0, "defence": 1.0, "league_average_goals": 1.5},
        "2": {"attack": 1.0, "defence": 1.0, "league_average_goals": 1.5},
    }
    model._prepare_minutes(players, season_code="unused", start_gameweek=1, use_availability=True)
    model._prepare_coherent_event_allocations(
        players,
        fixtures,
        strengths,
        start_gameweek=1,
        horizon_gameweeks=1,
    )
    row = unavailable["_coherent_by_gameweek"][1]
    assert row["goals"] == 0
    original = sum(p["_coherent_by_gameweek"][1]["goals"] for p in players)
    assert original == pytest.approx(1.5 * model.config.home_attack_multiplier)
    assert sum(p["_coherent_by_gameweek"][1]["penalty_share"] for p in players) == pytest.approx(
        model.config.coherent_penalty_goal_fraction
    )


def test_participation_probabilities_are_consistent_and_unknown_is_not_certain() -> None:
    model = RatesProjectionModel(
        database=None,  # type: ignore[arg-type]
        rules=RULES,
        config=ProjectionModelConfig(
            minutes_model="participation_v1",
            enforce_team_minutes=False,
        ),
    )
    player = _player("1", minutes=0, starts=0)
    player["matches"] = 0
    player["appearances"] = 0
    model._prepare_minutes([player], season_code="unused", start_gameweek=1, use_availability=True)
    assert player["_role_unknown"] is True
    assert player["_expected_minutes_per_fixture"] < 90
    start = player["_start_probability"]
    sub = player["_substitute_probability"]
    appearance = player["_appearance_probability"]
    assert 0 <= start <= 1
    assert 0 <= sub <= 1
    assert appearance == pytest.approx(start + (1 - start) * sub)
    assert player["_sixty_probability"] <= appearance
