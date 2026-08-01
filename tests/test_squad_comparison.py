from __future__ import annotations

from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    optimise_full_squad,
)
from fpl_engine.squad_comparison import (
    _summarise,
    compare_opening_squads,
    value_squad_under,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
SHAPE = (
    *((Position.GK, index) for index in range(3)),
    *((Position.DEF, index) for index in range(6)),
    *((Position.MID, index) for index in range(6)),
    *((Position.FWD, index) for index in range(4)),
)


def _candidates(*, favour: str) -> tuple[CandidatePlayer, ...]:
    """Nineteen players; `favour` decides which half looks strong."""

    players = []
    for order, (position, slot) in enumerate(SHAPE):
        identifier = f"{position.value}{slot}"
        strong = identifier.endswith(("0", "1", "2")) == (favour == "low")
        weekly = 6.0 if strong else 2.0
        players.append(
            CandidatePlayer(
                source_player_id=identifier,
                web_name=identifier,
                team_id=str(order % 8),
                team_short_name=f"T{order % 8}",
                position=position,
                price_tenths=55,
                expected_points=weekly * 2,
                gameweek_expected_points=weekly,
                appearance_probability=0.9,
                gameweek_values=(
                    GameweekPlayerValue(1, weekly, 0.9, 0.8),
                    GameweekPlayerValue(2, weekly, 0.9, 0.8),
                ),
            )
        )
    return tuple(players)


def test_valuing_a_fixed_squad_uses_the_other_configurations_numbers() -> None:
    optimistic = _candidates(favour="low")
    pessimistic = _candidates(favour="high")
    chosen = optimise_full_squad(
        optimistic, budget_tenths=1000, rules=RULES
    )
    squad_ids = frozenset(
        player.source_player_id for player in chosen.players
    )

    own = value_squad_under(squad_ids, optimistic, RULES)
    other = value_squad_under(squad_ids, pessimistic, RULES)

    # Same fifteen players, two sets of beliefs: the configuration that rates
    # them highly must value the squad above the one that does not.
    assert own > other
    assert own == pytest.approx(chosen.horizon_expected_points)


def test_valuing_a_squad_the_other_side_cannot_price_is_refused() -> None:
    optimistic = _candidates(favour="low")
    chosen = optimise_full_squad(optimistic, budget_tenths=1000, rules=RULES)
    squad_ids = frozenset(
        player.source_player_id for player in chosen.players
    )
    truncated = tuple(
        player
        for player in optimistic
        if player.source_player_id != next(iter(sorted(squad_ids)))
    )

    with pytest.raises(ValueError, match="absent from the other"):
        value_squad_under(squad_ids, truncated, RULES)


def test_summary_labels_starters_and_bench_positions() -> None:
    result = optimise_full_squad(
        _candidates(favour="low"), budget_tenths=1000, rules=RULES
    )

    summary = _summarise(result, "v1", "2026-27", 1, 2)

    roles = [player.role for player in summary.players]
    assert roles[:11] == ["starter"] * 11
    assert roles[11:] == ["bench0", "bench1", "bench2", "bench3"]
    assert summary.starting_ids == result.starting_player_ids
    assert len(summary.player_ids) == 15
    assert summary.total_cost_tenths == result.total_cost_tenths


def test_comparison_requires_exactly_two_configurations(tmp_path) -> None:
    from fpl_engine.history.database import HistoricalDatabase
    from fpl_engine.projections import PRESEASON_V5_MODEL_CONFIG

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        with pytest.raises(ValueError, match="Exactly two configurations"):
            compare_opening_squads(
                database,
                RULES,
                {"only": PRESEASON_V5_MODEL_CONFIG},
                season_code="2026-27",
            )
        with pytest.raises(ValueError, match="Sensitivity fraction"):
            compare_opening_squads(
                database,
                RULES,
                {
                    "a": PRESEASON_V5_MODEL_CONFIG,
                    "b": PRESEASON_V5_MODEL_CONFIG,
                },
                season_code="2026-27",
                sensitivity_fraction=1.5,
            )
