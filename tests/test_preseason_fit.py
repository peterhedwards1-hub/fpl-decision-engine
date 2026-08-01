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
    PRESEASON_SEARCH_SPACE,
    _require_prior_seasons,
    _robust_config,
    config_from_point,
    design_points,
    fit_preseason_priors,
    halton,
    profile_preseason_prior,
)
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    PRESEASON_V5_MODEL_CONFIG,
)

RULES = {
    "2022-23": load_season_rules(Path("config/seasons/2022-23.json")),
    "2023-24": load_season_rules(Path("config/seasons/2023-24.json")),
}


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


def test_profile_refuses_an_unknown_parameter(tmp_path) -> None:
    database = _database(tmp_path, ("2022-23", "2023-24"))
    try:
        with pytest.raises(ValueError, match="Unknown preseason parameter"):
            profile_preseason_prior(
                database,
                {"2023-24": RULES["2023-24"]},
                parameter="recent_gameweeks",
                target_seasons=("2023-24",),
                low=1.0,
                high=2.0,
            )
    finally:
        database.__exit__(None, None, None)


def test_halton_reproduces_its_known_leading_terms() -> None:
    assert [halton(index, 2) for index in range(1, 8)] == [
        0.5,
        0.25,
        0.75,
        0.125,
        0.625,
        0.375,
        0.875,
    ]
    assert halton(1, 3) == pytest.approx(1 / 3)
    assert halton(2, 3) == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="start at 1"):
        halton(0, 2)


def test_the_design_is_fixed_space_filling_and_within_range() -> None:
    first = design_points(48)
    second = design_points(48)

    # The design must not depend on anything measured, or leave-one-season-out
    # would be scoring points the withheld season helped choose.
    assert first == second
    assert len(first) == 48
    assert len({tuple(sorted(point.items())) for point in first}) == 48
    for point in first:
        assert set(point) == set(PRESEASON_SEARCH_SPACE)
        for name, value in point.items():
            low, high, _ = PRESEASON_SEARCH_SPACE[name]
            assert low <= value <= high


def test_every_design_point_builds_a_legal_configuration() -> None:
    for point in design_points(48):
        config = config_from_point(point, PRESEASON_V5_MODEL_CONFIG)
        assert config.cold_start_minimum_factor <= config.cold_start_maximum_factor
        changed = {
            name
            for name in PRESEASON_V5_MODEL_CONFIG.__dataclass_fields__
            if getattr(config, name)
            != getattr(PRESEASON_V5_MODEL_CONFIG, name)
        }
        assert changed <= set(PRESEASON_PARAMETER_NAMES)


def _matrix_for(points, chooser):
    return [{"2022-23": chooser(point), "2023-24": chooser(point)} for point in points]


def test_a_parameter_the_leading_points_agree_on_replaces_the_declared_value() -> None:
    points = design_points(48)
    # Score improves the closer the promoted attack multiplier is to 1.0, well
    # inside the search range, so the leading points cluster there.
    matrix = _matrix_for(
        points,
        lambda point: abs(point["promoted_team_attack_multiplier"] - 1.0),
    )

    config, evidence = _robust_config(
        points,
        matrix,
        ("2022-23", "2023-24"),
        PRESEASON_V5_MODEL_CONFIG,
        PRESEASON_V5_MODEL_CONFIG,
    )

    by_name = {item.name: item for item in evidence}
    assert by_name["promoted_team_attack_multiplier"].identified is True
    assert by_name["promoted_team_attack_multiplier"].at_boundary is False
    assert config.promoted_team_attack_multiplier == pytest.approx(1.0, abs=0.05)


def test_a_parameter_pressed_against_a_search_bound_is_censored() -> None:
    points = design_points(48)
    # Monotone in the multiplier, so the best points pile up at the low bound.
    matrix = _matrix_for(
        points,
        lambda point: point["promoted_team_attack_multiplier"],
    )

    config, evidence = _robust_config(
        points,
        matrix,
        ("2022-23", "2023-24"),
        PRESEASON_V5_MODEL_CONFIG,
        PRESEASON_V5_MODEL_CONFIG,
    )

    by_name = {item.name: item for item in evidence}
    # The bound chose that value, not the data, so the declared value stands.
    assert by_name["promoted_team_attack_multiplier"].at_boundary is True
    assert by_name["promoted_team_attack_multiplier"].identified is False
    assert by_name["promoted_team_attack_multiplier"].retained == (
        "declared (censored)"
    )
    assert (
        config.promoted_team_attack_multiplier
        == PRESEASON_V5_MODEL_CONFIG.promoted_team_attack_multiplier
    )


def test_a_parameter_the_leading_points_scatter_over_keeps_its_declared_value() -> None:
    points = design_points(48)
    # The score ignores elasticity entirely, so its leading values scatter.
    matrix = _matrix_for(
        points,
        lambda point: abs(point["promoted_team_attack_multiplier"] - 1.0),
    )

    config, evidence = _robust_config(
        points,
        matrix,
        ("2022-23", "2023-24"),
        PRESEASON_V5_MODEL_CONFIG,
        PRESEASON_V5_MODEL_CONFIG,
    )

    by_name = {item.name: item for item in evidence}
    assert by_name["cold_start_price_elasticity"].identified is False
    assert (
        config.cold_start_price_elasticity
        == PRESEASON_V5_MODEL_CONFIG.cold_start_price_elasticity
    )


def test_folds_select_from_the_same_outcome_independent_design() -> None:
    points = design_points(48)
    # 2022-23 prefers a high multiplier, 2023-24 a low one. A fold that trains
    # on one season must therefore pick a different configuration to the other.
    matrix = [
        {
            "2022-23": abs(point["promoted_team_attack_multiplier"] - 1.2),
            "2023-24": abs(point["promoted_team_attack_multiplier"] - 0.8),
        }
        for point in points
    ]

    first, _ = _robust_config(
        points, matrix, ("2022-23",), PRESEASON_V5_MODEL_CONFIG, PRESEASON_V5_MODEL_CONFIG
    )
    second, _ = _robust_config(
        points, matrix, ("2023-24",), PRESEASON_V5_MODEL_CONFIG, PRESEASON_V5_MODEL_CONFIG
    )

    assert (
        first.promoted_team_attack_multiplier
        != second.promoted_team_attack_multiplier
    )
    assert first.promoted_team_attack_multiplier > 1.0
    assert second.promoted_team_attack_multiplier < 1.0


def test_the_shipped_fit_only_moved_identified_parameters() -> None:
    report = json.loads(Path("data/preseason-priors-fit.json").read_text())
    shipped = json.loads(
        Path("config/model_candidates/preseason-priors-v2.json").read_text()
    )
    declared = asdict(PRESEASON_V5_MODEL_CONFIG)

    moved = {name for name in declared if declared[name] != shipped[name]}
    assert moved == set(report["identified_parameters"])
    assert moved <= set(PRESEASON_PARAMETER_NAMES)
    assert not moved & set(report["censored_parameters"])
    assert report["objective_version"] == "preseason-rmse-bias-v2"
