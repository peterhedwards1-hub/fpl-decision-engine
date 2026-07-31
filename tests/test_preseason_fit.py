from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.preseason_fit import (
    EXHAUSTED_SEASONS,
    PRESEASON_PARAMETER_NAMES,
    _config_from_preseason_parameters,
    _require_prior_seasons,
    _robust_config,
    _suggest_preseason_config,
    fit_preseason_priors,
)
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    PRESEASON_V5_MODEL_CONFIG,
)

RULES = {
    "2022-23": load_season_rules(Path("config/seasons/2022-23.json")),
    "2023-24": load_season_rules(Path("config/seasons/2023-24.json")),
}


class _StubTrial:
    """Records the search space without needing optuna in the test."""

    def __init__(self, position: float) -> None:
        self.position = position
        self.suggested: dict[str, float] = {}

    def suggest_float(
        self, name: str, low: float, high: float, *, log: bool = False
    ) -> float:
        value = low + (high - low) * self.position
        self.suggested[name] = value
        return value


def _database(tmp_path, seasons: tuple[str, ...]):
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    for code in seasons:
        database.connection.execute(
            "INSERT INTO seasons (code, name) VALUES (?, ?)",
            (code, code),
        )
    database.connection.commit()
    return database


def test_exhausted_seasons_cannot_be_fitted_on(tmp_path) -> None:
    database = _database(tmp_path, ("2023-24", "2024-25"))
    try:
        with pytest.raises(ValueError, match="design-exhausted"):
            fit_preseason_priors(
                database,
                {"2024-25": RULES["2023-24"]},
                target_seasons=("2024-25",),
            )
    finally:
        database.__exit__(None, None, None)

    assert set(EXHAUSTED_SEASONS) == {"2024-25", "2025-26"}


def test_forward_seasons_cannot_be_fitted_on(tmp_path) -> None:
    database = _database(tmp_path, ("2025-26", "2026-27"))
    try:
        with pytest.raises(ValueError, match="reserved for qualification"):
            fit_preseason_priors(
                database,
                {"2026-27": RULES["2023-24"]},
                target_seasons=("2026-27",),
            )
    finally:
        database.__exit__(None, None, None)


def test_a_target_season_needs_an_earlier_season_to_carry_forward(
    tmp_path,
) -> None:
    database = _database(tmp_path, ("2022-23", "2023-24"))
    try:
        # 2022-23 is the earliest season present, so carry-forward reads nothing.
        with pytest.raises(ValueError, match="no earlier season"):
            _require_prior_seasons(database, ("2022-23",))
        _require_prior_seasons(database, ("2023-24",))
    finally:
        database.__exit__(None, None, None)


def test_a_base_configuration_without_stage_3a_is_refused(tmp_path) -> None:
    database = _database(tmp_path, ("2022-23", "2023-24"))
    try:
        with pytest.raises(ValueError, match="carry_forward"):
            fit_preseason_priors(
                database,
                {"2023-24": RULES["2023-24"]},
                target_seasons=("2023-24",),
                base_config=CORRECTED_V4_MODEL_CONFIG,
            )
        with pytest.raises(ValueError, match="cold_start_prior"):
            fit_preseason_priors(
                database,
                {"2023-24": RULES["2023-24"]},
                target_seasons=("2023-24",),
                base_config=replace(
                    PRESEASON_V5_MODEL_CONFIG,
                    cold_start_prior="position",
                ),
            )
    finally:
        database.__exit__(None, None, None)


def test_rules_must_cover_exactly_the_target_seasons(tmp_path) -> None:
    database = _database(tmp_path, ("2021-22", "2022-23", "2023-24"))
    try:
        with pytest.raises(ValueError, match="Rules must match"):
            fit_preseason_priors(
                database,
                {"2022-23": RULES["2022-23"]},
                target_seasons=("2022-23", "2023-24"),
            )
    finally:
        database.__exit__(None, None, None)


def test_the_search_space_only_moves_stage_3a_fields() -> None:
    config = _suggest_preseason_config(_StubTrial(0.5), PRESEASON_V5_MODEL_CONFIG)

    changed = {
        name
        for name in PRESEASON_V5_MODEL_CONFIG.__dataclass_fields__
        if getattr(config, name) != getattr(PRESEASON_V5_MODEL_CONFIG, name)
    }
    assert changed <= set(PRESEASON_PARAMETER_NAMES)
    assert config.team_strength_carry_forward is True
    assert config.cold_start_prior == "position_price"


@pytest.mark.parametrize("position", (0.0, 0.25, 0.5, 0.75, 1.0))
def test_cold_start_bounds_stay_ordered_across_the_search_space(
    position: float,
) -> None:
    trial = _StubTrial(position)

    config = _suggest_preseason_config(trial, PRESEASON_V5_MODEL_CONFIG)

    # The span parameterisation is what keeps minimum <= maximum legal, so no
    # trial is ever wasted on a configuration the dataclass rejects.
    assert 0 < config.cold_start_minimum_factor <= 1
    assert config.cold_start_maximum_factor >= config.cold_start_minimum_factor
    assert "cold_start_factor_span" in trial.suggested


def test_a_trial_round_trips_from_its_recorded_parameters() -> None:
    trial = _StubTrial(0.3)
    config = _suggest_preseason_config(trial, PRESEASON_V5_MODEL_CONFIG)

    rebuilt = _config_from_preseason_parameters(
        trial.suggested,
        PRESEASON_V5_MODEL_CONFIG,
    )

    # Leave-one-season-out rebuilds a fold's winner from its stored params, so
    # the rebuild has to reproduce the configuration the trial actually ran.
    assert rebuilt == config


class _StubCompletedTrial:
    def __init__(self, number: int, params: dict, season_scores: dict) -> None:
        self.number = number
        self.params = params
        self.user_attrs = {"season_scores": season_scores}


def _trial(number: int, *, attack: float, elasticity: float, score: float):
    """A trial that varies the promoted attack multiplier and nothing else."""

    return _StubCompletedTrial(
        number,
        {
            "carry_forward_regression_matches": 12.0,
            "promoted_team_attack_multiplier": attack,
            "promoted_team_defence_multiplier": 1.20,
            "cold_start_price_elasticity": elasticity,
            "cold_start_minimum_factor": 0.35,
            "cold_start_factor_span": 3.0 / 0.35,
        },
        {"2022-23": score, "2023-24": score},
    )


def test_a_parameter_the_leading_trials_agree_on_replaces_the_declared_value() -> None:
    # The best trials all sit near 1.05 while the search ranged over 0.6-1.05.
    trials = [
        _trial(0, attack=1.05, elasticity=1.5, score=1.0),
        _trial(1, attack=1.04, elasticity=1.5, score=1.1),
        _trial(2, attack=1.06, elasticity=1.5, score=1.2),
        _trial(3, attack=0.60, elasticity=1.5, score=3.0),
        _trial(4, attack=0.70, elasticity=1.5, score=3.1),
        _trial(5, attack=0.80, elasticity=1.5, score=3.2),
    ]

    config, evidence = _robust_config(
        trials,
        ("2022-23", "2023-24"),
        PRESEASON_V5_MODEL_CONFIG,
        PRESEASON_V5_MODEL_CONFIG,
    )

    by_name = {item.name: item for item in evidence}
    assert by_name["promoted_team_attack_multiplier"].identified is True
    assert by_name["promoted_team_attack_multiplier"].retained == "fitted"
    assert config.promoted_team_attack_multiplier == pytest.approx(1.05, abs=0.02)


def test_a_parameter_the_leading_trials_scatter_over_keeps_its_declared_value() -> None:
    # Elasticity is uncorrelated with the score here, so the leading trials
    # scatter across the range and nothing about it has been established.
    trials = [
        _trial(0, attack=1.05, elasticity=0.1, score=1.0),
        _trial(1, attack=1.04, elasticity=2.9, score=1.1),
        _trial(2, attack=1.06, elasticity=1.4, score=1.2),
        _trial(3, attack=0.60, elasticity=0.5, score=3.0),
        _trial(4, attack=0.70, elasticity=2.2, score=3.1),
        _trial(5, attack=0.80, elasticity=1.9, score=3.2),
    ]

    config, evidence = _robust_config(
        trials,
        ("2022-23", "2023-24"),
        PRESEASON_V5_MODEL_CONFIG,
        PRESEASON_V5_MODEL_CONFIG,
    )

    by_name = {item.name: item for item in evidence}
    assert by_name["cold_start_price_elasticity"].identified is False
    assert by_name["cold_start_price_elasticity"].retained == "declared"
    assert (
        config.cold_start_price_elasticity
        == PRESEASON_V5_MODEL_CONFIG.cold_start_price_elasticity
    )


def test_an_untouched_parameter_never_moves() -> None:
    trials = [
        _trial(index, attack=1.05, elasticity=1.5, score=1.0 + index / 10)
        for index in range(6)
    ]

    config, evidence = _robust_config(
        trials,
        ("2022-23",),
        PRESEASON_V5_MODEL_CONFIG,
        PRESEASON_V5_MODEL_CONFIG,
    )

    # Every trial declared the same defence multiplier, so its leading spread is
    # zero and it can only be "identified" if it differs from the declared value.
    by_name = {item.name: item for item in evidence}
    assert by_name["promoted_team_defence_multiplier"].identified is False
    assert config.cold_start_minimum_factor <= config.cold_start_maximum_factor


def test_the_shipped_fit_only_moved_identified_parameters() -> None:
    report = json.loads(Path("data/preseason-priors-fit.json").read_text())
    shipped = json.loads(
        Path("config/model_candidates/preseason-priors-v2.json").read_text()
    )
    declared = asdict(PRESEASON_V5_MODEL_CONFIG)

    moved = {name for name in declared if declared[name] != shipped[name]}
    assert moved == set(report["identified_parameters"])
    assert moved <= set(PRESEASON_PARAMETER_NAMES)
    # Both withheld seasons preferred the shipped rule to the declared values.
    assert report["transfers_across_seasons"] is True
