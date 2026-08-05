"""Evidence for the preseason team-strength decision.

The claim under test is narrow: before GW1 the production model has no opinion
about any club, and a regressed previous season is a better opinion than none.
Every structural property that claim rests on is checked here on synthetic
leagues whose right answer is known in advance, because in a real season a
club's schedule and its quality are entangled and nothing can be isolated.

The league builder is shared with the opponent-adjusted team-strength tests. A
second copy would let the two drift, and then two suites would be proving
things about two different leagues.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_team_strength import (
    CLUBS,
    _league,
    _source,
    _strong_weak_goals,
)

from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.optimisation import mean_appearance
from fpl_engine.preseason_strength import (
    CARRY_FORWARD_LABEL,
    CARRY_FORWARD_PRESEASON_CONFIG,
    FLAT_LABEL,
    FLAT_PRESEASON_CONFIG,
    FOCUS_PLAYER_NAMES,
    MATERIAL_BRIER_TOLERANCE,
    NEUTRAL_REALISED_POINTS_TOLERANCE,
    PRESEASON_CARRY_FORWARD_MODEL_VERSION,
    _team_metrics,
    apply_decision_gate,
    build_revised_squad,
    compare_preseason_squads,
    discover_season_transitions,
    evaluate_team_goal_forecasts,
    player_component_explanations,
    preseason_model_is_validated,
    preseason_strength_separation,
    render_preseason_validation_markdown,
    run_robustness_checks,
    squad_comparison_artifact,
    validate_preseason_strength,
)
from fpl_engine.production import (
    PRESEASON_MODEL_VERSION,
    select_decision_projection_run,
    select_preseason_projection_run,
    select_production_projection_run,
)
from fpl_engine.projections import MODEL_VERSION, RatesProjectionModel
from fpl_engine.readiness import build_preseason_readiness_report
from fpl_engine.squad_comparison import opening_candidates

RULES = load_season_rules(Path("config/seasons/2026-27.json"))

#: A promoted side replaces the weakest club in the target season, so the
#: promoted prior has somewhere to apply.
PROMOTED_CLUBS = (*CLUBS[:-1], "Newly Promoted")


def _seasons(tmp_path, *, seasons: tuple[str, ...], target_promoted: bool = True):
    """Completed strong-and-weak seasons, then an unplayed target season."""

    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    for code in seasons[:-1]:
        database.ingest_bundle(
            _source(),
            _league(code, goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})),
        )
    database.ingest_bundle(
        _source(),
        _league(
            seasons[-1],
            finished=False,
            club_names=PROMOTED_CLUBS if target_promoted else CLUBS,
        ),
    )
    return database


def _strengths(database, config, season_code="2026-27", gameweek=1):
    return RatesProjectionModel(database, RULES, config=config)._team_strengths(
        season_code, gameweek, ()
    )


def _team_ids(database, season_code="2026-27") -> dict[str, str]:
    """Source club number to the database's own team id.

    The strength maps are keyed on the internal id, and the synthetic league
    names its clubs by source id, so the two have to be joined explicitly.
    """

    return {
        str(row["source_team_id"]): str(row["id"])
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.source_team_id FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
    }


# --------------------------------------------------------------------------
# 1-2. What the two models actually believe before a ball is kicked
# --------------------------------------------------------------------------


def test_flat_preseason_strengths_are_identical_for_every_club(tmp_path) -> None:
    """The defect, stated as a measurement rather than an assertion."""

    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        strengths = _strengths(database, FLAT_PRESEASON_CONFIG)

    attacks = {round(float(v["attack"]), 9) for v in strengths.values()}
    defences = {round(float(v["defence"]), 9) for v in strengths.values()}
    assert len(strengths) == len(CLUBS)
    assert attacks == {1.0}
    assert defences == {1.0}


def test_carry_forward_separates_established_clubs_at_gameweek_one(
    tmp_path,
) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        strengths = _strengths(database, CARRY_FORWARD_PRESEASON_CONFIG)
        ids = _team_ids(database)

    # Clubs 1-4 beat clubs 5-8 four-nil all season; they must not come out
    # equal to the sides they beat.
    strong = [float(strengths[ids[str(club)]]["attack"]) for club in (1, 2, 3, 4)]
    weak = [float(strengths[ids[str(club)]]["attack"]) for club in (5, 6, 7)]
    assert min(strong) > max(weak)
    assert len({round(float(v["attack"]), 9) for v in strengths.values()}) > 1


def test_the_separation_measure_reports_the_structural_difference(
    tmp_path,
) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        flat = preseason_strength_separation(
            database, RULES, config=FLAT_PRESEASON_CONFIG, season_code="2026-27"
        )
        carry = preseason_strength_separation(
            database,
            RULES,
            config=CARRY_FORWARD_PRESEASON_CONFIG,
            season_code="2026-27",
        )

    assert flat["distinct_attack_multipliers"] == 1
    assert flat["attack_spread"] == 0
    assert flat["separates_established_from_promoted"] is False
    assert carry["distinct_attack_multipliers"] > 1
    assert carry["separates_established_from_promoted"] is True
    assert carry["established_minus_promoted_attack"] > 0


# --------------------------------------------------------------------------
# 3. Promoted clubs take the declared prior, not a measurement
# --------------------------------------------------------------------------


def test_promoted_clubs_receive_the_declared_promoted_priors(tmp_path) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        model = RatesProjectionModel(
            database, RULES, config=CARRY_FORWARD_PRESEASON_CONFIG
        )
        rates, league_average = model._carry_forward_team_rates(
            "2026-27", maximum_ingestion_run_id=None
        )
        promoted_team_id = str(
            database.connection.execute(
                """
                SELECT teams.id FROM teams
                JOIN seasons ON seasons.id = teams.season_id
                WHERE seasons.code = '2026-27' AND teams.name = 'Newly Promoted'
                """
            ).fetchone()["id"]
        )

    attack, defence = rates[promoted_team_id]
    config = CARRY_FORWARD_PRESEASON_CONFIG
    assert attack == pytest.approx(
        league_average * config.promoted_team_attack_multiplier
    )
    assert defence == pytest.approx(
        league_average * config.promoted_team_defence_multiplier
    )
    # A declared conservative prior: worse than average at both ends.
    assert config.promoted_team_attack_multiplier < 1.0
    assert config.promoted_team_defence_multiplier > 1.0


# --------------------------------------------------------------------------
# 4-5. Point-in-time safety
# --------------------------------------------------------------------------


def test_carry_forward_reads_only_the_immediately_previous_season(
    tmp_path,
) -> None:
    """A third season back must not change the GW1 prior at all.

    Built so the two histories disagree loudly: in one, clubs 1-4 dominate
    the season before last and everyone draws last season. If anything from
    two seasons back leaked, the ratings would separate.
    """

    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    with database:
        database.initialise()
        database.ingest_bundle(
            _source(),
            _league(
                "2024-25", goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
            ),
        )
        database.ingest_bundle(_source(), _league("2025-26"))
        database.ingest_bundle(_source(), _league("2026-27", finished=False))
        strengths = _strengths(database, CARRY_FORWARD_PRESEASON_CONFIG)

    # Last season was an eight-club league of one-all draws, so every club's
    # carried prior is identical however lopsided the season before it was.
    assert len({round(float(v["attack"]), 9) for v in strengths.values()}) == 1


def test_no_target_season_result_reaches_the_gameweek_one_prior(
    tmp_path,
) -> None:
    """Playing the target season out must not move a GW1 forecast."""

    unplayed = HistoricalDatabase(tmp_path / "unplayed.sqlite3")
    with unplayed:
        unplayed.initialise()
        unplayed.ingest_bundle(
            _source(),
            _league(
                "2025-26", goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
            ),
        )
        unplayed.ingest_bundle(_source(), _league("2026-27", finished=False))
        before = _strengths(unplayed, CARRY_FORWARD_PRESEASON_CONFIG)

    played = HistoricalDatabase(tmp_path / "played.sqlite3")
    with played:
        played.initialise()
        played.ingest_bundle(
            _source(),
            _league(
                "2025-26", goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
            ),
        )
        # The target season is played out with the table completely inverted.
        played.ingest_bundle(
            _source(),
            _league(
                "2026-27",
                goals=_strong_weak_goals({5, 6, 7, 8}, {1, 2, 3, 4}),
            ),
        )
        after = _strengths(played, CARRY_FORWARD_PRESEASON_CONFIG)

    for team_id, values in before.items():
        assert float(after[team_id]["attack"]) == pytest.approx(
            float(values["attack"])
        )
        assert float(after[team_id]["defence"]) == pytest.approx(
            float(values["defence"])
        )


# --------------------------------------------------------------------------
# 6-7. Regression toward the league average
# --------------------------------------------------------------------------


def test_the_carried_prior_is_regressed_toward_the_league_average(
    tmp_path,
) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        model = RatesProjectionModel(
            database, RULES, config=CARRY_FORWARD_PRESEASON_CONFIG
        )
        rates, league_average = model._carry_forward_team_rates(
            "2026-27", maximum_ingestion_run_id=None
        )
        raw = database.connection.execute(
            """
            WITH results AS (
                SELECT home_team_id AS team_id, home_score AS goals_for
                FROM fixtures JOIN seasons ON seasons.id = fixtures.season_id
                WHERE seasons.code = '2025-26'
                UNION ALL
                SELECT away_team_id, away_score
                FROM fixtures JOIN seasons ON seasons.id = fixtures.season_id
                WHERE seasons.code = '2025-26'
            )
            SELECT teams.name, COUNT(*) AS matches,
                   SUM(results.goals_for) AS goals_for
            FROM results JOIN teams ON teams.id = results.team_id
            GROUP BY teams.id
            """
        ).fetchall()
        names = {
            str(row["id"]): str(row["name"])
            for row in database.connection.execute(
                """
                SELECT teams.id, teams.name FROM teams
                JOIN seasons ON seasons.id = teams.season_id
                WHERE seasons.code = '2026-27'
                """
            )
        }

    unregressed = {
        str(row["name"]): float(row["goals_for"]) / int(row["matches"])
        for row in raw
    }
    moved = 0
    for team_id, (attack, _) in rates.items():
        name = names[team_id]
        if name not in unregressed:
            continue
        raw_rate = unregressed[name]
        # Every club's carried rate must sit strictly between what it actually
        # did and the league average — that is what regression means.
        assert min(raw_rate, league_average) <= attack <= max(
            raw_rate, league_average
        )
        if abs(raw_rate - league_average) > 1e-9:
            assert abs(attack - league_average) < abs(raw_rate - league_average)
            moved += 1
    assert moved > 0


def test_stronger_regression_pulls_multipliers_closer_to_average(
    tmp_path,
) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        weak = _strengths(
            database,
            replace(
                CARRY_FORWARD_PRESEASON_CONFIG,
                carry_forward_regression_matches=8.0,
            ),
        )
        strong = _strengths(
            database,
            replace(
                CARRY_FORWARD_PRESEASON_CONFIG,
                carry_forward_regression_matches=16.0,
            ),
        )

    def spread(values):
        attacks = [float(v["attack"]) for v in values.values()]
        return max(attacks) - min(attacks)

    assert spread(strong) < spread(weak)
    for team_id, values in weak.items():
        assert abs(float(strong[team_id]["attack"]) - 1.0) <= abs(
            float(values["attack"]) - 1.0
        ) + 1e-9


# --------------------------------------------------------------------------
# 8. Venue is not team quality
# --------------------------------------------------------------------------


def test_venue_effects_stay_separate_from_team_strength(tmp_path) -> None:
    """Doubling the home factor must move fixtures, never a club's rating."""

    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        base = CARRY_FORWARD_PRESEASON_CONFIG
        louder = replace(
            base, home_attack_multiplier=1.30, away_attack_multiplier=0.70
        )
        assert _strengths(database, base).keys() == _strengths(
            database, louder
        ).keys()
        for team_id, values in _strengths(database, base).items():
            assert float(_strengths(database, louder)[team_id]["attack"]) == (
                pytest.approx(float(values["attack"]))
            )

        quiet_fixtures = RatesProjectionModel(
            database, RULES, config=base
        ).fixture_expected_goals(season_code="2026-27", gameweek_number=1)
        loud_fixtures = RatesProjectionModel(
            database, RULES, config=louder
        ).fixture_expected_goals(season_code="2026-27", gameweek_number=1)

    quiet = {row["fixture_id"]: row for row in quiet_fixtures}
    loud = {row["fixture_id"]: row for row in loud_fixtures}
    assert quiet
    for fixture_id, row in quiet.items():
        assert (
            loud[fixture_id]["home_expected_goals"] > row["home_expected_goals"]
        )
        assert (
            loud[fixture_id]["away_expected_goals"] < row["away_expected_goals"]
        )


# --------------------------------------------------------------------------
# 9. One modelling difference, and only one
# --------------------------------------------------------------------------


def test_the_control_and_the_candidate_differ_in_exactly_one_field() -> None:
    flat = asdict(FLAT_PRESEASON_CONFIG)
    carry = asdict(CARRY_FORWARD_PRESEASON_CONFIG)
    differing = {key for key in flat if flat[key] != carry[key]}
    assert differing == {"team_strength_carry_forward"}
    assert flat["team_strength_carry_forward"] is False
    assert carry["team_strength_carry_forward"] is True
    assert flat["team_strength_model"] == "raw_goals"
    assert carry["team_strength_model"] == "raw_goals"


# --------------------------------------------------------------------------
# 10. Transition discovery
# --------------------------------------------------------------------------


def test_transition_discovery_excludes_incomplete_transitions_safely(
    tmp_path,
) -> None:
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    with database:
        database.initialise()
        database.ingest_bundle(
            _source(),
            _league(
                "2024-25", goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})
            ),
        )
        # An unplayed middle season is unusable as a prior *and* unusable as a
        # target, and must be reported twice rather than silently dropped.
        database.ingest_bundle(_source(), _league("2025-26", finished=False))
        database.ingest_bundle(_source(), _league("2026-27"))
        transitions = discover_season_transitions(database)

    by_pair = {
        (entry.previous_season, entry.target_season): entry
        for entry in transitions
    }
    assert set(by_pair) == {("2024-25", "2025-26"), ("2025-26", "2026-27")}
    assert by_pair[("2024-25", "2025-26")].usable is False
    assert "GW1-GW8" in by_pair[("2024-25", "2025-26")].reason
    assert by_pair[("2025-26", "2026-27")].usable is False
    assert "completion" in by_pair[("2025-26", "2026-27")].reason
    # Nothing is dropped: an excluded transition is still returned with a
    # reason, so "we excluded it" can be told from "it was never there".
    assert len(transitions) == 2


def test_transition_discovery_accepts_a_complete_pair(tmp_path) -> None:
    with _seasons(tmp_path, seasons=("2024-25", "2025-26", "2026-27")) as db:
        transitions = discover_season_transitions(db, exclude_seasons=("2026-27",))

    assert [entry.usable for entry in transitions] == [True]
    assert transitions[0].previous_season == "2024-25"
    assert transitions[0].target_season == "2025-26"


# --------------------------------------------------------------------------
# 11. Early-season metrics
# --------------------------------------------------------------------------


def test_early_season_metrics_are_calculated_correctly() -> None:
    rows = [
        {
            "expected": 2.0,
            "actual": 1.0,
            "clean_sheet_probability": 0.25,
            "clean_sheet": 0.0,
        },
        {
            "expected": 1.0,
            "actual": 2.0,
            "clean_sheet_probability": 0.50,
            "clean_sheet": 1.0,
        },
        {
            "expected": 1.0,
            "actual": 0.0,
            "clean_sheet_probability": 0.75,
            "clean_sheet": 1.0,
        },
    ]
    metrics = _team_metrics(rows)
    assert metrics["observations"] == 3
    # Errors are +1, -1, +1.
    assert metrics["goals_mae"] == pytest.approx(1.0)
    assert metrics["goals_rmse"] == pytest.approx(1.0)
    assert metrics["goals_bias"] == pytest.approx(1.0 / 3.0, abs=1e-4)
    assert metrics["clean_sheet_brier"] == pytest.approx(
        (0.0625 + 0.25 + 0.0625) / 3, abs=1e-4
    )
    assert _team_metrics([]) == {"observations": 0}


def test_early_and_full_season_windows_are_reported_separately(
    tmp_path,
) -> None:
    with _seasons(
        tmp_path,
        seasons=("2024-25", "2025-26", "2026-27"),
        target_promoted=False,
    ) as database:
        report = evaluate_team_goal_forecasts(
            database,
            RULES,
            season_code="2025-26",
            config=CARRY_FORWARD_PRESEASON_CONFIG,
            early_gameweeks=3,
        )

    assert report["early_season"]["observations"] > 0
    assert report["full_season"]["observations"] >= (
        report["early_season"]["observations"]
    )
    assert set(report["breakdowns"]) == {"venue", "promotion", "phase"}
    assert set(report["breakdowns"]["venue"]) == {"home", "away"}
    assert set(report["breakdowns"]["phase"]) == {"gw1_gw4", "gw5_gw8"}


# --------------------------------------------------------------------------
# 12. The decision gate
# --------------------------------------------------------------------------


def _aggregate(
    *,
    flat_rmse=1.30,
    carry_rmse=1.20,
    flat_brier=0.19,
    carry_brier=0.18,
    flat_points=300.0,
    carry_points=301.0,
    carry_bias=0.05,
):
    return {
        FLAT_LABEL: {
            "early_team_goals": {
                "goals_rmse": flat_rmse,
                "goals_mae": flat_rmse - 0.3,
                "goals_bias": -0.05,
                "clean_sheet_brier": flat_brier,
            },
            "mean_opening_squad_realised_points": flat_points,
            "mean_squad_regret": 40.0,
        },
        CARRY_FORWARD_LABEL: {
            "early_team_goals": {
                "goals_rmse": carry_rmse,
                "goals_mae": carry_rmse - 0.3,
                "goals_bias": carry_bias,
                "clean_sheet_brier": carry_brier,
            },
            "mean_opening_squad_realised_points": carry_points,
            "mean_squad_regret": 39.0,
        },
    }


def _historical(count=3, rmse_gain=0.1, points_change=1.0):
    return [
        {
            "transition": {"target_season": f"20{20 + index}-{21 + index}"},
            "models": {
                FLAT_LABEL: {
                    "team_goals": {"early_season": {"goals_rmse": 1.30}}
                },
                CARRY_FORWARD_LABEL: {
                    "team_goals": {
                        "early_season": {"goals_rmse": 1.30 - rmse_gain}
                    }
                },
            },
            "differences": {"opening_squad_realised_points": points_change},
        }
        for index in range(count)
    ]


_SEPARATION = {
    FLAT_LABEL: {
        "distinct_attack_multipliers": 1,
        "separates_established_from_promoted": False,
    },
    CARRY_FORWARD_LABEL: {
        "distinct_attack_multipliers": 20,
        "separates_established_from_promoted": True,
        "established_minus_promoted_attack": 0.14,
    },
}


def test_the_decision_gate_passes_a_clearly_better_candidate() -> None:
    gate = apply_decision_gate(
        _aggregate(), _historical(), live_separation=_SEPARATION
    )
    assert gate["passed"] is True
    assert gate["failed_criteria"] == []
    assert gate["neutral_tolerance"] == NEUTRAL_REALISED_POINTS_TOLERANCE
    assert {entry["criterion"] for entry in gate["criteria"]} == {
        "improves_early_team_goal_error",
        "clean_sheet_brier_not_materially_worse",
        "opening_squad_decision_not_worse",
        "acceptable_across_multiple_transitions",
        "separates_established_from_promoted",
        "no_severe_new_calibration_defect",
    }


def test_the_decision_gate_fails_on_worse_early_accuracy() -> None:
    gate = apply_decision_gate(
        _aggregate(carry_rmse=1.40), _historical(), live_separation=_SEPARATION
    )
    assert gate["passed"] is False
    assert "improves_early_team_goal_error" in gate["failed_criteria"]


def test_the_decision_gate_fails_on_a_materially_worse_brier_score() -> None:
    gate = apply_decision_gate(
        _aggregate(carry_brier=0.19 + 10 * MATERIAL_BRIER_TOLERANCE),
        _historical(),
        live_separation=_SEPARATION,
    )
    assert gate["passed"] is False
    assert "clean_sheet_brier_not_materially_worse" in gate["failed_criteria"]
    assert "no_severe_new_calibration_defect" in gate["failed_criteria"]


def test_the_decision_gate_tolerates_a_neutral_decision_difference() -> None:
    """Half a point given up over eight Gameweeks is inside the noise."""

    inside = apply_decision_gate(
        _aggregate(carry_points=300.0 - NEUTRAL_REALISED_POINTS_TOLERANCE),
        _historical(points_change=-NEUTRAL_REALISED_POINTS_TOLERANCE),
        live_separation=_SEPARATION,
    )
    assert inside["passed"] is True

    outside = _aggregate(carry_points=290.0)
    outside[CARRY_FORWARD_LABEL]["mean_squad_regret"] = 41.0
    beyond = apply_decision_gate(
        outside,
        _historical(points_change=-10.0),
        live_separation=_SEPARATION,
    )
    assert beyond["passed"] is False
    assert "opening_squad_decision_not_worse" in beyond["failed_criteria"]


def test_the_decision_gate_requires_more_than_one_usable_transition() -> None:
    gate = apply_decision_gate(
        _aggregate(), _historical(count=1), live_separation=_SEPARATION
    )
    assert gate["passed"] is False
    assert "acceptable_across_multiple_transitions" in gate["failed_criteria"]


def test_the_decision_gate_requires_the_structural_fix() -> None:
    """A candidate that still cannot separate clubs has not fixed anything."""

    gate = apply_decision_gate(
        _aggregate(),
        _historical(),
        live_separation={
            FLAT_LABEL: {"separates_established_from_promoted": False},
            CARRY_FORWARD_LABEL: {"separates_established_from_promoted": False},
        },
    )
    assert gate["passed"] is False
    assert "separates_established_from_promoted" in gate["failed_criteria"]


def test_the_decision_gate_fails_a_severe_calibration_defect() -> None:
    gate = apply_decision_gate(
        _aggregate(carry_bias=0.9), _historical(), live_separation=_SEPARATION
    )
    assert gate["passed"] is False
    assert "no_severe_new_calibration_defect" in gate["failed_criteria"]


# --------------------------------------------------------------------------
# 13-14. Production selection
# --------------------------------------------------------------------------


def _projection_run(database, *, model_version, horizon=8, generated_at, gameweek=1):
    season = database.connection.execute(
        "SELECT id FROM seasons WHERE code = '2026-27'"
    ).fetchone()
    cursor = database.connection.execute(
        """
        INSERT INTO projection_runs (
            season_id, model_version, generated_at, start_gameweek,
            horizon_gameweeks, observation_mode, assumptions_json
        ) VALUES (?, ?, ?, ?, ?, 'latest_available', '{}')
        """,
        (
            int(season["id"]),
            model_version,
            generated_at,
            gameweek,
            horizon,
        ),
    )
    return int(cursor.lastrowid)


def _database_with_runs(tmp_path):
    database = HistoricalDatabase(tmp_path / "runs.sqlite3")
    database.__enter__()
    database.initialise()
    database.connection.execute(
        "INSERT INTO seasons (code, name) VALUES ('2026-27', '2026/27')"
    )
    return database


def test_a_validated_candidate_becomes_the_preseason_selection(
    tmp_path,
) -> None:
    with _database_with_runs(tmp_path) as database:
        incumbent = _projection_run(
            database,
            model_version=MODEL_VERSION,
            generated_at="2026-08-04T09:00:00+00:00",
        )
        preseason = _projection_run(
            database,
            model_version=PRESEASON_MODEL_VERSION,
            generated_at="2026-08-04T08:00:00+00:00",
        )

        run, context = select_decision_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=1,
            minimum_horizon_gameweeks=8,
            preseason_model_validated=True,
        )
        assert run is not None
        # The incumbent run is *newer*. Recency must not win here.
        assert run.run_id == preseason
        assert run.run_id != incumbent
        assert context == "preseason_opening_squad"

        unvalidated, unvalidated_context = select_decision_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=1,
            minimum_horizon_gameweeks=8,
            preseason_model_validated=False,
        )
        assert unvalidated is not None
        assert unvalidated.run_id == incumbent
        assert unvalidated_context == "in_season_live_projection"


def test_an_in_season_decision_never_uses_the_preseason_model(tmp_path) -> None:
    with _database_with_runs(tmp_path) as database:
        _projection_run(
            database,
            model_version=PRESEASON_MODEL_VERSION,
            generated_at="2026-11-01T08:00:00+00:00",
            gameweek=12,
        )
        incumbent = _projection_run(
            database,
            model_version=MODEL_VERSION,
            generated_at="2026-10-01T08:00:00+00:00",
            gameweek=12,
        )

        # The preseason selector refuses outright away from GW1.
        assert (
            select_preseason_projection_run(
                database,
                season_code="2026-27",
                start_gameweek=12,
                minimum_horizon_gameweeks=5,
            )
            is None
        )
        # And the incumbent selector never had the preseason version in its
        # allow-list, however new that run is.
        production = select_production_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=12,
            minimum_horizon_gameweeks=5,
        )
        assert production is not None
        assert production.run_id == incumbent

        run, context = select_decision_projection_run(
            database,
            season_code="2026-27",
            start_gameweek=12,
            minimum_horizon_gameweeks=5,
            preseason_model_validated=True,
        )
        assert run is not None
        assert run.run_id == incumbent
        assert context == "in_season_live_projection"


def test_a_failed_validation_artifact_does_not_authorise_the_preseason_model() -> (
    None
):
    passing = {
        "season_code": "2026-27",
        "validation": {"decision_gate": {"passed": True}},
        "selected_model": {
            "label": CARRY_FORWARD_LABEL,
            "model_version": PRESEASON_CARRY_FORWARD_MODEL_VERSION,
        },
    }
    assert preseason_model_is_validated(passing, season_code="2026-27") is True
    assert preseason_model_is_validated(passing, season_code="2025-26") is False
    assert preseason_model_is_validated(None, season_code="2026-27") is False

    failed = json.loads(json.dumps(passing))
    failed["validation"]["decision_gate"]["passed"] = False
    assert preseason_model_is_validated(failed, season_code="2026-27") is False

    wrong_model = json.loads(json.dumps(passing))
    wrong_model["selected_model"]["label"] = FLAT_LABEL
    assert preseason_model_is_validated(wrong_model, season_code="2026-27") is (
        False
    )


def test_readiness_warns_when_the_preseason_model_is_not_validated(
    tmp_path,
) -> None:
    with _database_with_runs(tmp_path) as database:
        report = build_preseason_readiness_report(
            database,
            RULES,
            season_code="2026-27",
            preseason_validation={},
        )

    assert report["decision_context"] == "in_season_live_projection"
    assert report["preseason_team_strength"]["validated"] is False
    assert report["preseason_team_strength"]["status"] == "missing"


# --------------------------------------------------------------------------
# 15-17. The revised squad
# --------------------------------------------------------------------------


def _candidates(database, config, season_code="2026-27"):
    return opening_candidates(
        database,
        RULES,
        config,
        season_code=season_code,
        gameweek=1,
        horizon_gameweeks=3,
        generated_at=datetime(2026, 8, 4, tzinfo=UTC),
    )


def test_the_revised_squad_is_legal(tmp_path) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        candidates = _candidates(database, CARRY_FORWARD_PRESEASON_CONFIG)
        recommendation = build_revised_squad(
            candidates, RULES, candidate_pool_size=3, alternative_count=2
        )

    squad = recommendation.primary
    assert len(squad.players) == RULES.squad.squad_size
    positions: dict[str, int] = {}
    for player in squad.players:
        positions[player.position.value] = positions.get(player.position.value, 0) + 1
    assert positions == RULES.squad.position_counts
    assert len(squad.starting_player_ids) == RULES.squad.starting_size
    assert len(squad.bench_player_ids) == (
        RULES.squad.squad_size - RULES.squad.starting_size
    )
    assert squad.captain_id in squad.starting_player_ids
    assert squad.vice_captain_id in squad.starting_player_ids
    assert squad.captain_id != squad.vice_captain_id
    assert len({player.source_player_id for player in squad.players}) == (
        RULES.squad.squad_size
    )
    assert len(recommendation.alternatives) == 2
    for alternative in recommendation.alternatives:
        assert alternative.starting_player_ids != squad.starting_player_ids


def test_budget_and_club_limits_are_enforced(tmp_path) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        candidates = _candidates(database, CARRY_FORWARD_PRESEASON_CONFIG)
        recommendation = build_revised_squad(
            candidates, RULES, candidate_pool_size=2, alternative_count=1
        )

    for squad in (recommendation.primary, *recommendation.alternatives):
        assert squad.total_cost_tenths <= RULES.squad.budget_tenths
        assert squad.total_cost_tenths == sum(
            player.price_tenths for player in squad.players
        )
        per_club: dict[str, int] = {}
        for player in squad.players:
            per_club[player.team_id] = per_club.get(player.team_id, 0) + 1
        assert max(per_club.values()) <= RULES.squad.max_players_per_team


def test_the_appearance_floor_is_applied_before_selection(tmp_path) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        candidates = _candidates(database, CARRY_FORWARD_PRESEASON_CONFIG)
        recommendation = build_revised_squad(
            candidates,
            RULES,
            candidate_pool_size=1,
            alternative_count=0,
            minimum_mean_appearance=0.5,
        )

    for player in recommendation.primary.players:
        assert mean_appearance(player) >= 0.5


def test_the_revised_squad_is_attached_to_its_projection_run(tmp_path) -> None:
    """The squad in the artifact names the run that produced it."""

    from fpl_engine.preseason_strength import (
        generate_preseason_projection,
        squad_as_dict,
    )

    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        live = generate_preseason_projection(
            database,
            RULES,
            season_code="2026-27",
            config=CARRY_FORWARD_PRESEASON_CONFIG,
            horizon_gameweeks=3,
            generated_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
        candidates = _candidates(database, CARRY_FORWARD_PRESEASON_CONFIG)
        recommendation = build_revised_squad(
            candidates, RULES, candidate_pool_size=1, alternative_count=0
        )
        stored = database.connection.execute(
            "SELECT model_version, horizon_gameweeks FROM projection_runs WHERE id = ?",
            (live["projection_run_id"],),
        ).fetchone()

    assert live["projection_run_id"] is not None
    assert live["model_version"] == PRESEASON_CARRY_FORWARD_MODEL_VERSION
    assert str(stored["model_version"]) == PRESEASON_CARRY_FORWARD_MODEL_VERSION
    assert int(stored["horizon_gameweeks"]) == 3
    assert live["configuration"]["team_strength_carry_forward"] is True

    squad = {
        **squad_as_dict(recommendation.primary, label=CARRY_FORWARD_LABEL),
        "projection_run_id": live["projection_run_id"],
    }
    assert squad["projection_run_id"] == live["projection_run_id"]
    assert len(squad["players"]) == RULES.squad.squad_size


# --------------------------------------------------------------------------
# 18. The three named players
# --------------------------------------------------------------------------


def test_the_report_compares_the_three_named_players(tmp_path) -> None:
    """Explanations are keyed to the named players and carry every component.

    The synthetic league is given squads containing players with exactly those
    names, so the selection rule is tested rather than the coincidence that a
    real feed happens to contain them.
    """

    named = {
        1: ["Truffert", *[f"p1-{slot}" for slot in range(14)]],
        2: ["O'Shea", *[f"p2-{slot}" for slot in range(14)]],
        3: ["Muñoz", *[f"p3-{slot}" for slot in range(14)]],
    }
    database = HistoricalDatabase(tmp_path / "named.sqlite3")
    with database:
        database.initialise()
        database.ingest_bundle(
            _source(),
            _league(
                "2025-26",
                goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8}),
                squads=named,
            ),
        )
        database.ingest_bundle(
            _source(), _league("2026-27", finished=False, squads=named)
        )
        configs = {
            FLAT_LABEL: FLAT_PRESEASON_CONFIG,
            CARRY_FORWARD_LABEL: CARRY_FORWARD_PRESEASON_CONFIG,
        }
        candidates = {
            label: _candidates(database, config)
            for label, config in configs.items()
        }
        focus = frozenset(
            player.source_player_id
            for pool in candidates.values()
            for player in pool
            if any(name in player.web_name for name in FOCUS_PLAYER_NAMES)
        )
        explanations = player_component_explanations(
            database,
            RULES,
            season_code="2026-27",
            configs=configs,
            squads={label: frozenset() for label in configs},
            source_player_ids=focus,
            horizon_gameweeks=3,
            generated_at=datetime(2026, 8, 4, tzinfo=UTC),
        )

    names = {entry["web_name"] for entry in explanations}
    assert names == set(FOCUS_PLAYER_NAMES)
    for entry in explanations:
        flat = entry["models"][FLAT_LABEL]
        carry = entry["models"][CARRY_FORWARD_LABEL]
        for side in (flat, carry):
            assert side["opponent"]
            assert side["venue"] in {"home", "away"}
            assert side["opponent_expected_goals"] > 0
            assert 0.0 < side["clean_sheet_probability"] < 1.0
            assert side["expected_minutes"] >= 0
            assert 0.0 <= side["appearance_probability"] <= 1.0
            for key in (
                "attacking_points",
                "clean_sheet_points",
                "defensive_contribution_points",
                "horizon_expected_points",
                "gameweek_expected_points",
            ):
                assert key in side
        assert entry["horizon_points_change"] == pytest.approx(
            carry["horizon_expected_points"] - flat["horizon_expected_points"],
            abs=1e-3,
        )
        assert entry["change_attributed_to"] in {
            "team_strength",
            "squad_budget_interaction",
            "unchanged",
            "unknown",
        }

    # Clubs 1-3 all won last season, so a model that carried anything forward
    # must make their opponents' clean sheets less likely than a flat one.
    moved = [
        entry
        for entry in explanations
        if entry["models"][FLAT_LABEL]["clean_sheet_probability"]
        != entry["models"][CARRY_FORWARD_LABEL]["clean_sheet_probability"]
    ]
    assert moved


# --------------------------------------------------------------------------
# 19. Cross-valuation
# --------------------------------------------------------------------------


def test_cross_valuation_uses_the_right_projection_for_each_score(
    tmp_path,
) -> None:
    """Each row is one model's opinion of every squad, on its own scale.

    The check that matters: a model must not rate a rival squad above the one
    it chose itself. If it did, the optimiser was not solving the objective
    the valuation is reading, and the whole comparison would be meaningless.
    """

    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        configs = {
            FLAT_LABEL: FLAT_PRESEASON_CONFIG,
            CARRY_FORWARD_LABEL: CARRY_FORWARD_PRESEASON_CONFIG,
        }
        candidate_sets = {
            label: _candidates(database, config)
            for label, config in configs.items()
        }
        squads = {
            label: frozenset(
                player.source_player_id
                for player in build_revised_squad(
                    pool, RULES, candidate_pool_size=1, alternative_count=0
                ).primary.players
            )
            for label, pool in candidate_sets.items()
        }
        comparison = compare_preseason_squads(
            database,
            RULES,
            season_code="2026-27",
            squads=squads,
            candidate_sets=candidate_sets,
        )

    cross = comparison["cross_valuation"]
    assert set(cross) == {FLAT_LABEL, CARRY_FORWARD_LABEL}
    for holder in (FLAT_LABEL, CARRY_FORWARD_LABEL):
        own = cross[holder][holder]
        other = cross[holder][
            CARRY_FORWARD_LABEL if holder == FLAT_LABEL else FLAT_LABEL
        ]
        assert own is not None and other is not None
        assert own >= other - 1e-6
    key = f"{CARRY_FORWARD_LABEL}_vs_{FLAT_LABEL}"
    assert key in comparison["overlap"]
    overlap = comparison["overlap"][key]
    assert overlap["common_count"] == len(
        squads[FLAT_LABEL] & squads[CARRY_FORWARD_LABEL]
    )


# --------------------------------------------------------------------------
# 20. Determinism of the stress tests
# --------------------------------------------------------------------------


def test_stress_tests_are_deterministic_and_bounded(tmp_path) -> None:
    with _seasons(tmp_path, seasons=("2025-26", "2026-27")) as database:
        kwargs = {
            "season_code": "2026-27",
            "base_config": CARRY_FORWARD_PRESEASON_CONFIG,
            "horizon_gameweeks": 3,
            "candidate_pool_size": 1,
            "generated_at": datetime(2026, 8, 4, tzinfo=UTC),
        }
        first = run_robustness_checks(database, RULES, **kwargs)
        second = run_robustness_checks(database, RULES, **kwargs)

    assert first["runs"] == second["runs"]
    assert first["classification"] == second["classification"]
    assert first["objective_spread"] == second["objective_spread"]
    # A declared, bounded set: three regression strengths plus the cap toggle.
    assert len(first["runs"]) == 4
    assert {run["carry_forward_regression_matches"] for run in first["runs"]} == {
        8.0,
        12.0,
        16.0,
    }
    assert {run["appearance_cap"] for run in first["runs"]} == {None, 0.95}
    assert set(first["classification"].values()) <= {
        "robust",
        "moderate",
        "model_sensitive",
    }
    for player_id in first["core_player_ids"]:
        assert first["classification"][player_id] == "robust"


# --------------------------------------------------------------------------
# The artifact, and what happens when the gate fails
# --------------------------------------------------------------------------


def test_a_failing_gate_reports_the_failure_and_keeps_the_flat_model(
    tmp_path,
) -> None:
    """Failure must be loud, and must still produce a comparison.

    One prior season gives one transition, and the gate requires more than
    one. Nothing is silently retained: the flat model is named as the
    selection, the squad is marked unvalidated, and the robustness comparison
    is produced anyway so the failure is informative rather than a dead end.
    """

    with _seasons(
        tmp_path, seasons=("2024-25", "2025-26", "2026-27")
    ) as database:
        result = validate_preseason_strength(
            database,
            RULES,
            season_code="2026-27",
            horizon_gameweeks=3,
            candidate_pool_size=3,
            include_reference_model=False,
            include_decision_metrics=False,
        )

    gate = result["validation"]["decision_gate"]
    assert gate["passed"] is False
    assert "acceptable_across_multiple_transitions" in gate["failed_criteria"]
    assert result["selected_model"]["label"] == FLAT_LABEL
    assert result["selected_model"]["model_version"] == MODEL_VERSION
    assert result["selected_model"]["validated"] is False
    assert result["revised_squad"]["validated"] is False
    assert result["live_projection"]["model_version"] == MODEL_VERSION
    assert any("decision gate failed" in warning for warning in result["warnings"])
    assert any("usable season transition" in w for w in result["warnings"])
    # A failure is not a dead end: the comparison and stress tests still run.
    assert result["robustness"]["runs"]
    assert result["flat_comparison"]["cross_valuation"]
    # And an artifact that failed cannot authorise the preseason selector.
    assert preseason_model_is_validated(result, season_code="2026-27") is False


def test_the_artifact_carries_every_documented_section(tmp_path) -> None:
    with _seasons(
        tmp_path, seasons=("2024-25", "2025-26", "2026-27")
    ) as database:
        result = validate_preseason_strength(
            database,
            RULES,
            season_code="2026-27",
            horizon_gameweeks=3,
            candidate_pool_size=3,
            include_reference_model=False,
            include_decision_metrics=False,
            include_robustness=False,
            generate_live_projection=False,
        )

    assert {
        "validation",
        "selected_model",
        "historical_results",
        "live_projection",
        "revised_squad",
        "alternatives",
        "flat_comparison",
        "robustness",
        "warnings",
    } <= set(result)
    validation = result["validation"]
    assert {
        "transitions",
        "usable_transitions",
        "excluded_transitions",
        "compared_models",
        "aggregate",
        "decision_gate",
        "point_in_time_policy",
    } <= set(validation)
    # Every discovered transition is reported, usable or not.
    assert validation["transitions"]
    comparison = squad_comparison_artifact(result)
    assert comparison["season_code"] == "2026-27"
    assert set(comparison["squads"]) == {FLAT_LABEL, CARRY_FORWARD_LABEL}
    markdown = render_preseason_validation_markdown(result)
    assert "# Preseason team strength" in markdown
    assert "## Decision gate" in markdown
    assert "## Revised opening squad" in markdown
    # The artifact must be JSON-serialisable exactly as written.
    assert json.loads(json.dumps(result))["season_code"] == "2026-27"


def test_the_command_line_accepts_the_documented_invocation(
    monkeypatch, capsys
) -> None:
    """The published command must parse, including every declared flag."""

    from fpl_engine.history import cli

    monkeypatch.setattr(
        "sys.argv",
        [
            "fpl-history",
            "validate-preseason-strength",
            "2026-27",
            "--horizon",
            "8",
            "--candidate-pool-size",
            "8",
            "--output",
            "data/models/preseason-strength-validation.json",
            "--help",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    printed = capsys.readouterr().out
    for flag in (
        "--horizon",
        "--candidate-pool-size",
        "--output",
        "--comparison-output",
        "--markdown-output",
    ):
        assert flag in printed
