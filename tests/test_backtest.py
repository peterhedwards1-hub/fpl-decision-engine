from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_engine.assumption_audit import run_assumption_audit
from fpl_engine.backtest import ProjectionBacktester, load_backtest_report
from fpl_engine.config import load_season_rules
from fpl_engine.diagnostics import build_stage_one_diagnostics
from fpl_engine.domain import Position
from fpl_engine.evaluation import (
    build_evaluation_suite,
    compare_backtest_to_baselines,
)
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)
from fpl_engine.learned_challenger import (
    train_and_evaluate_learned_challenger,
)
from fpl_engine.projections import (
    DEFENSIVE_EMPIRICAL_V5_MODEL_CONFIG,
    ProjectionModelConfig,
    RatesProjectionModel,
)
from fpl_engine.tuning import (
    tune_projection_model,
    tune_projection_model_rolling,
    tuning_objective,
)

RULES = load_season_rules(Path("config/seasons/2025-26.json"))
SOURCE = IngestionSource(
    name="historical-test",
    retrieved_at=datetime(2026, 7, 1, tzinfo=UTC),
    identifier_namespace="official-fpl",
)
SCHEDULE_SOURCE = replace(
    SOURCE,
    retrieved_at=datetime(2025, 8, 1, tzinfo=UTC),
    content_sha256="pre-season-schedule",
)


def _historical_bundle() -> HistoricalBundle:
    return HistoricalBundle(
        season=SeasonRecord("2025-26", "2025/26"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=(
            PlayerRecord(
                "101",
                "Ada",
                "Ada",
                "Striker",
                official_fpl_code="9001",
            ),
        ),
        player_seasons=(
            PlayerSeasonRecord("101", "1", Position.FWD, 75, 77),
        ),
        gameweeks=(
            GameweekRecord(1, "2025-08-15T17:30:00Z", True),
            GameweekRecord(2, "2025-08-22T17:30:00Z", True),
            GameweekRecord(3, "2025-08-29T17:30:00Z", True),
        ),
        fixtures=(
            FixtureRecord("501", "1", "2", 1, finished=True, home_score=2, away_score=0),
            FixtureRecord("502", "2", "1", 2, finished=True, home_score=1, away_score=1),
            FixtureRecord("503", "1", "2", 3, finished=True, home_score=0, away_score=1),
        ),
        fixture_stats=(
            PlayerFixtureStatsRecord(
                "101", "501", minutes=90, starts=True, goals=1, total_points=8
            ),
            PlayerFixtureStatsRecord(
                "101", "502", minutes=60, starts=True, assists=1, total_points=5
            ),
            PlayerFixtureStatsRecord(
                "101", "503", minutes=20, total_points=1
            ),
        ),
        gameweek_snapshots=tuple(
            PlayerGameweekSnapshotRecord(
                "101",
                gameweek,
                74 + gameweek,
                None,
                source_team_id="1",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key=f"historical-gw-{gameweek}",
            )
            for gameweek in (1, 2, 3)
        ),
    )


def _future_bundle() -> HistoricalBundle:
    return HistoricalBundle(
        season=SeasonRecord("2026-27", "2026/27"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=(
            PlayerRecord(
                "301",
                "Ada",
                "Ada",
                "Striker",
                official_fpl_code="9001",
            ),
        ),
        player_seasons=(
            PlayerSeasonRecord("301", "1", Position.FWD, 80, 82),
        ),
        gameweeks=(GameweekRecord(1, "2026-08-14T17:30:00Z", True),),
        fixtures=(
            FixtureRecord(
                "601",
                "1",
                "2",
                1,
                finished=True,
                home_score=10,
                away_score=0,
            ),
        ),
        fixture_stats=(
            PlayerFixtureStatsRecord(
                "301",
                "601",
                minutes=90,
                starts=True,
                goals=10,
                total_points=50,
            ),
        ),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                "301",
                1,
                80,
                None,
                source_team_id="1",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key="future-gw-1",
            ),
        ),
    )


def _schedule_bundle(bundle: HistoricalBundle) -> HistoricalBundle:
    return replace(
        bundle,
        gameweeks=tuple(
            replace(gameweek, is_finished=False)
            for gameweek in bundle.gameweeks
        ),
        fixtures=tuple(
            replace(
                fixture,
                finished=False,
                home_score=None,
                away_score=None,
            )
            for fixture in bundle.fixtures
        ),
        fixture_stats=(),
        gameweek_snapshots=(),
    )


def test_walk_forward_backtest_persists_predictions_and_metrics(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SCHEDULE_SOURCE, _schedule_bundle(_historical_bundle()))
        database.ingest_bundle(SOURCE, _historical_bundle())

        report = ProjectionBacktester(database, RULES).run(
            season_code="2025-26",
            origin_gameweek_start=2,
            origin_gameweek_end=3,
            horizon_gameweeks=1,
        )

        rows = database.connection.execute(
            """
            SELECT expected_points, actual_points, expected_minutes,
                   actual_minutes
            FROM projection_backtest_predictions
            WHERE backtest_run_id = ?
            ORDER BY origin_gameweek
            """,
            (report.backtest_run_id,),
        ).fetchall()
        point_errors = [
            row["actual_points"] - row["expected_points"] for row in rows
        ]
        assert report.prediction_count == 2
        assert report.generated_prediction_count == 2
        assert report.missing_outcome_count == 0
        assert report.source_ingestion_run_id == 2
        assert len(report.data_fingerprint or "") == 64
        assert report.overall.samples == 2
        assert report.overall.points_mae == pytest.approx(
            round(sum(abs(error) for error in point_errors) / 2, 4)
        )
        assert report.overall.points_rmse == pytest.approx(
            round(math.sqrt(sum(error**2 for error in point_errors) / 2), 4)
        )
        assert report.by_position[0].value == "FWD"
        assert report.by_horizon[0].value == "1"
        assert {metric.value for metric in report.by_participation} == {
            "played"
        }
        assert report.by_fixture_count[0].value == "1"
        assert {metric.value for metric in report.top_n} == {
            "15",
            "50",
            "100",
        }
        assert report.regulation_minutes_per_match == 1980
        assert tuning_objective(report) > 0
        assert [row["actual_points"] for row in rows] == [5, 1]
        assert [row["actual_minutes"] for row in rows] == [60, 20]
        assert database.connection.execute(
            "SELECT COUNT(*) FROM projection_runs"
        ).fetchone()[0] == 0
        assert load_backtest_report(
            database, report.backtest_run_id
        ).as_dict() == report.as_dict()
        comparison = compare_backtest_to_baselines(
            database,
            report.backtest_run_id,
        )
        assert {
            metric.name for metric in comparison.methods
        } == {
            "model",
            "season_points_per_fixture",
            "recent_4_points_per_fixture",
            "season_points_per_90_model_minutes",
            "position_points_per_fixture",
        }
        assert {
            metric.horizon_step for metric in comparison.by_horizon
        } == {1}
        assert all(metric.samples == 2 for metric in comparison.methods)
        suite = build_evaluation_suite(
            database,
            (report.backtest_run_id,),
        )
        assert suite["incumbent_runs"][0]["season_code"] == "2025-26"
        assert suite["challenger_comparisons"] == []
        diagnostics = build_stage_one_diagnostics(
            database,
            (report.backtest_run_id,),
            bootstrap_samples=20,
            moving_block_gameweeks=1,
            minimum_slice_samples=1,
            seed=1,
        )
        bootstrap = diagnostics["paired_moving_block_bootstrap"]
        assert bootstrap["bootstrap_samples"] == 20
        assert "points_rmse" in bootstrap["metrics"]
        assert diagnostics["calibration"]["appearance"]["samples"] == 2
        assert diagnostics["residual_slices"]
        assert diagnostics["oracle_sensitivity"]["status"] == "available"
        component_json = database.connection.execute(
            """
            SELECT component_points_json
            FROM projection_backtest_predictions
            WHERE backtest_run_id = ?
            LIMIT 1
            """,
            (report.backtest_run_id,),
        ).fetchone()[0]
        assert component_json is not None


def test_future_season_results_do_not_leak_into_historical_projection(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, _historical_bundle())
        before = RatesProjectionModel(database, RULES).project(
            season_code="2025-26",
            start_gameweek=2,
            horizon_gameweeks=1,
            persist=False,
        ).projections[0]

        database.ingest_bundle(
            replace(SOURCE, content_sha256="future"),
            _future_bundle(),
        )
        after = RatesProjectionModel(database, RULES).project(
            season_code="2025-26",
            start_gameweek=2,
            horizon_gameweeks=1,
            persist=False,
        ).projections[0]

        assert after.expected_minutes == before.expected_minutes
        assert after.goal_points == before.goal_points
        assert after.expected_points == before.expected_points


def test_empirical_defensive_contribution_model_is_forward_only(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, _historical_bundle())
        with pytest.raises(ValueError, match="restricted to genuinely forward"):
            ProjectionBacktester(
                database,
                RULES,
                config=DEFENSIVE_EMPIRICAL_V5_MODEL_CONFIG,
            ).run(
                season_code="2025-26",
                origin_gameweek_start=2,
                origin_gameweek_end=2,
            )


def test_assumption_variants_change_recent_and_threshold_scoring(
    tmp_path,
) -> None:
    historical = _historical_bundle()
    historical = replace(
        historical,
        player_seasons=(
            replace(historical.player_seasons[0], position=Position.DEF),
        ),
        fixture_stats=(
            replace(
                historical.fixture_stats[0],
                defensive_contributions=10,
                penalties_missed=1,
            ),
            *historical.fixture_stats[1:],
        ),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, historical)
        reference = RatesProjectionModel(
            database,
            RULES,
            config=ProjectionModelConfig(
                player_rate_prior_minutes=90,
                scoring_recent_evidence_weight=1,
            ),
        ).project(
            season_code="2025-26",
            start_gameweek=2,
            horizon_gameweeks=1,
            persist=False,
        ).projections[0]
        corrected = RatesProjectionModel(
            database,
            RULES,
            config=ProjectionModelConfig(
                player_rate_prior_minutes=90,
                scoring_recent_evidence_weight=3,
                defensive_contribution_model="threshold_poisson",
                include_penalty_events=True,
            ),
        ).project(
            season_code="2025-26",
            start_gameweek=2,
            horizon_gameweeks=1,
            persist=False,
        ).projections[0]

        assert corrected.goal_points > reference.goal_points
        assert 0 <= corrected.defensive_contribution_points <= 2
        assert corrected.defensive_contribution_points < (
            reference.defensive_contribution_points
        )
        assert corrected.deduction_points < reference.deduction_points


def test_later_same_season_results_do_not_leak_into_projection(
    tmp_path,
) -> None:
    historical = _historical_bundle()
    through_gameweek_one = replace(
        historical,
        fixture_stats=tuple(
            stats
            for stats in historical.fixture_stats
            if stats.source_fixture_id == "501"
        ),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            replace(SOURCE, content_sha256="through-gw1"),
            through_gameweek_one,
        )
        before = RatesProjectionModel(database, RULES).project(
            season_code="2025-26",
            start_gameweek=2,
            horizon_gameweeks=1,
            persist=False,
        ).projections[0]

        database.ingest_bundle(
            replace(SOURCE, content_sha256="through-gw3"),
            historical,
        )
        after = RatesProjectionModel(database, RULES).project(
            season_code="2025-26",
            start_gameweek=2,
            horizon_gameweeks=1,
            persist=False,
        ).projections[0]

        assert after.expected_minutes == before.expected_minutes
        assert after.goal_points == before.goal_points
        assert after.expected_points == before.expected_points


def test_strict_pre_deadline_policy_rejects_reconstructed_snapshots(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SCHEDULE_SOURCE, _schedule_bundle(_historical_bundle()))
        database.ingest_bundle(SOURCE, _historical_bundle())

        with pytest.raises(ValueError, match="no scorable predictions"):
            ProjectionBacktester(database, RULES).run(
                season_code="2025-26",
                origin_gameweek_start=2,
                origin_gameweek_end=2,
                evidence_policy="pre_deadline_only",
            )

        failed = database.connection.execute(
            """
            SELECT status, error_message
            FROM projection_backtest_runs
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        assert failed["status"] == "failed"
        assert "no scorable predictions" in failed["error_message"]


def test_failed_backtest_removes_predictions_from_earlier_origins(
    tmp_path,
    monkeypatch,
) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            SCHEDULE_SOURCE,
            _schedule_bundle(_historical_bundle()),
        )
        database.ingest_bundle(SOURCE, _historical_bundle())
        original_project = RatesProjectionModel.project
        calls = 0

        def fail_on_second_origin(model, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic projection failure")
            return original_project(model, **kwargs)

        monkeypatch.setattr(
            RatesProjectionModel,
            "project",
            fail_on_second_origin,
        )

        with pytest.raises(RuntimeError, match="synthetic projection failure"):
            ProjectionBacktester(database, RULES).run(
                season_code="2025-26",
                origin_gameweek_start=2,
                origin_gameweek_end=3,
            )

        failed = database.connection.execute(
            """
            SELECT id, status, prediction_count
            FROM projection_backtest_runs
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        stored_predictions = database.connection.execute(
            """
            SELECT COUNT(*)
            FROM projection_backtest_predictions
            WHERE backtest_run_id = ?
            """,
            (failed["id"],),
        ).fetchone()[0]
        assert failed["status"] == "failed"
        assert failed["prediction_count"] == 0
        assert stored_predictions == 0


def test_fixture_slate_is_replayed_from_observations_known_at_origin(
    tmp_path,
) -> None:
    initial = HistoricalBundle(
        season=SeasonRecord("2025-26", "2025/26"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=(PlayerRecord("101", "Ada", "Ada", "Striker"),),
        player_seasons=(
            PlayerSeasonRecord("101", "1", Position.FWD, 75, 75),
        ),
        gameweeks=(
            GameweekRecord(10, "2025-10-31T18:30:00Z"),
            GameweekRecord(12, "2025-11-14T18:30:00Z"),
        ),
        fixtures=(FixtureRecord("501", "1", "2", 10),),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                "101",
                10,
                75,
                None,
                source_team_id="1",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key="gw10",
            ),
        ),
    )
    revised = replace(
        initial,
        fixtures=(FixtureRecord("501", "1", "2", 12),),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                "101",
                12,
                75,
                None,
                source_team_id="1",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key="gw12",
            ),
        ),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            replace(
                SOURCE,
                retrieved_at=datetime(2025, 10, 1, tzinfo=UTC),
                content_sha256="fixture-gw10",
            ),
            initial,
        )
        database.ingest_bundle(
            replace(
                SOURCE,
                retrieved_at=datetime(2025, 11, 5, tzinfo=UTC),
                content_sha256="fixture-gw12",
            ),
            revised,
        )
        model = RatesProjectionModel(database, RULES)

        gw10 = model.project(
            season_code="2025-26",
            start_gameweek=10,
            horizon_gameweeks=1,
            generated_at=datetime(2025, 10, 31, 18, 29, tzinfo=UTC),
            fixture_as_of=datetime(2025, 10, 31, 18, 29, tzinfo=UTC),
            persist=False,
        )
        gw12 = model.project(
            season_code="2025-26",
            start_gameweek=12,
            horizon_gameweeks=1,
            generated_at=datetime(2025, 11, 14, 18, 29, tzinfo=UTC),
            fixture_as_of=datetime(2025, 11, 14, 18, 29, tzinfo=UTC),
            persist=False,
        )

        assert gw10.projections[0].fixture_count == 1
        assert gw12.projections[0].fixture_count == 1


def test_performance_only_prefers_newer_gameweek_team_membership(
    tmp_path,
) -> None:
    bundle = HistoricalBundle(
        season=SeasonRecord("2025-26", "2025/26"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
            TeamRecord("3", "West United", "WST"),
        ),
        players=(PlayerRecord("101", "Ada", "Ada", "Striker"),),
        player_seasons=(
            PlayerSeasonRecord("101", "1", Position.FWD, 75, 75),
        ),
        gameweeks=(
            GameweekRecord(5, "2025-09-12T18:30:00Z"),
            GameweekRecord(15, "2025-12-05T18:30:00Z"),
            GameweekRecord(16, "2025-12-12T18:30:00Z"),
        ),
        fixtures=(FixtureRecord("501", "2", "3", 16),),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                "101",
                5,
                70,
                datetime(2025, 9, 12, 12, tzinfo=UTC),
                source_team_id="1",
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                source_observation_key="exact-gw5",
            ),
            PlayerGameweekSnapshotRecord(
                "101",
                15,
                75,
                None,
                source_team_id="2",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key="reconstructed-gw15",
            ),
        ),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, bundle)

        result = RatesProjectionModel(database, RULES).project(
            season_code="2025-26",
            start_gameweek=16,
            horizon_gameweeks=1,
            observation_mode="performance_only",
            use_availability=False,
            persist=False,
        )

        assert result.projections[0].team_short_name == "STH"
        assert result.projections[0].fixture_count == 1


def test_missing_outcomes_are_excluded_but_explicit_zero_is_scored(
    tmp_path,
) -> None:
    base = _historical_bundle()
    complete = replace(
        base,
        players=(
            *base.players,
            PlayerRecord("102", "Bea", "Bea", "Forward"),
            PlayerRecord("103", "Cia", "Cia", "Forward"),
        ),
        player_seasons=(
            *base.player_seasons,
            PlayerSeasonRecord("102", "1", Position.FWD, 70, 70),
            PlayerSeasonRecord("103", "1", Position.FWD, 65, 65),
        ),
        fixture_stats=(
            *base.fixture_stats,
            PlayerFixtureStatsRecord("102", "502", minutes=0, total_points=0),
        ),
        gameweek_snapshots=(
            *base.gameweek_snapshots,
            PlayerGameweekSnapshotRecord(
                "102",
                2,
                70,
                None,
                source_team_id="1",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key="bea-gw2",
            ),
            PlayerGameweekSnapshotRecord(
                "103",
                2,
                65,
                None,
                source_team_id="1",
                observation_kind="historical_reconstruction",
                timing_quality="unknown",
                source_observation_key="cia-gw2",
            ),
        ),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SCHEDULE_SOURCE, _schedule_bundle(complete))
        database.ingest_bundle(SOURCE, complete)

        report = ProjectionBacktester(database, RULES).run(
            season_code="2025-26",
            origin_gameweek_start=2,
            origin_gameweek_end=2,
        )
        actuals = database.connection.execute(
            """
            SELECT player_seasons.source_player_id, predictions.actual_minutes,
                   predictions.actual_points
            FROM projection_backtest_predictions predictions
            JOIN player_seasons
              ON player_seasons.id = predictions.player_season_id
            WHERE predictions.backtest_run_id = ?
            ORDER BY player_seasons.source_player_id
            """,
            (report.backtest_run_id,),
        ).fetchall()

        assert report.generated_prediction_count == 3
        assert report.prediction_count == 2
        assert report.missing_outcome_count == 1
        assert [tuple(row) for row in actuals] == [
            ("101", 60, 5),
            ("102", 0, 0),
        ]


def test_tuning_uses_development_then_separate_validation_window(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            SCHEDULE_SOURCE,
            _schedule_bundle(_historical_bundle()),
        )
        database.ingest_bundle(SOURCE, _historical_bundle())

        result = tune_projection_model(
            database,
            RULES,
            season_code="2025-26",
            development_start=2,
            development_end=2,
            validation_start=3,
            validation_end=3,
            trials=1,
            study_name="test-two-stage",
            storage_url=None,
            seed=1,
        )

        assert result.best_trial_number == 0
        assert result.best_score > 0
        assert result.best_config.minutes_model == "two_stage"
        assert result.development_backtest_run_id != (
            result.validation_report.backtest_run_id
        )
        assert result.baseline_validation_report.backtest_run_id != (
            result.validation_report.backtest_run_id
        )
        assert result.validation_report.origin_gameweek_start == 3
        assert "validation_change" in result.as_dict()


def test_rolling_tuning_locks_validation_after_first_inspection(
    tmp_path,
) -> None:
    development = replace(
        _historical_bundle(),
        season=SeasonRecord("2024-25", "2024/25"),
    )
    storage = f"sqlite:///{(tmp_path / 'tuning.sqlite3').as_posix()}"
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            replace(SCHEDULE_SOURCE, content_sha256="2024-schedule"),
            _schedule_bundle(development),
        )
        database.ingest_bundle(
            replace(SOURCE, content_sha256="2024-results"),
            development,
        )
        database.ingest_bundle(
            replace(SCHEDULE_SOURCE, content_sha256="2025-schedule"),
            _schedule_bundle(_historical_bundle()),
        )
        database.ingest_bundle(
            replace(SOURCE, content_sha256="2025-results"),
            _historical_bundle(),
        )
        result = tune_projection_model_rolling(
            database,
            {
                "2024-25": replace(RULES, season="2024-25"),
                "2025-26": RULES,
            },
            development_seasons=("2024-25",),
            validation_season="2025-26",
            origin_gameweek_start=2,
            origin_gameweek_end=3,
            trials=1,
            study_name="rolling-lock-test",
            storage_url=storage,
            seed=1,
        )

        assert result.development_seasons == ("2024-25",)
        assert result.validation_season == "2025-26"
        assert result.development_backtest_run_ids["2024-25"] > 0
        result_dict = result.as_dict()
        assert result_dict["holdout_locked"] is True
        assert (
            result_dict["holdout_selection_locked_before_evaluation"] is True
        )
        assert result.development_season_scores["2024-25"] > 0
        assert result.development_weighted_mean_score == (
            result.development_worst_season_score
        )
        assert result.cross_season_stability_penalty == 0
        learned = train_and_evaluate_learned_challenger(
            database,
            training_run_ids=(
                result.development_backtest_run_ids["2024-25"],
            ),
            validation_run_id=(
                result.challenger_validation_report.backtest_run_id
            ),
            artifact_path=tmp_path / "challenger.joblib",
            seed=1,
        )
        assert learned.baseline.samples > 0
        assert learned.challenger.samples == learned.baseline.samples
        assert learned.loss == "absolute_error"
        assert learned.challenger.captain_regret >= 0
        assert (tmp_path / "challenger.joblib").exists()
        assert (tmp_path / "challenger.json").exists()

        import optuna

        interrupted_study = optuna.load_study(
            study_name="rolling-lock-test",
            storage=storage,
        )
        interrupted_study.set_user_attr("holdout_locked", False)
        recovered = tune_projection_model_rolling(
            database,
            {
                "2024-25": replace(RULES, season="2024-25"),
                "2025-26": RULES,
            },
            development_seasons=("2024-25",),
            validation_season="2025-26",
            origin_gameweek_start=2,
            origin_gameweek_end=3,
            trials=2,
            study_name="rolling-lock-test",
            storage_url=storage,
            seed=1,
        )
        assert recovered.best_trial_number == result.best_trial_number
        recovered_study = optuna.load_study(
            study_name="rolling-lock-test",
            storage=storage,
        )
        assert sum(
            trial.state.name == "COMPLETE"
            for trial in recovered_study.trials
        ) == 1

        with pytest.raises(ValueError, match="already been inspected"):
            tune_projection_model_rolling(
                database,
                {
                    "2024-25": replace(RULES, season="2024-25"),
                    "2025-26": RULES,
                },
                development_seasons=("2024-25",),
                validation_season="2025-26",
                origin_gameweek_start=2,
                origin_gameweek_end=3,
                trials=1,
                study_name="rolling-lock-test",
                storage_url=storage,
                seed=1,
            )


def test_assumption_audit_uses_development_folds_only(tmp_path) -> None:
    first = replace(
        _historical_bundle(),
        season=SeasonRecord("2024-25", "2024/25"),
        fixture_stats=(
            _historical_bundle().fixture_stats[0],
            replace(
                _historical_bundle().fixture_stats[1],
                total_points=-1,
            ),
            _historical_bundle().fixture_stats[2],
        ),
    )
    second = _historical_bundle()
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        for season_code, bundle in (
            ("2024-25", first),
            ("2025-26", second),
        ):
            database.ingest_bundle(
                replace(
                    SCHEDULE_SOURCE,
                    content_sha256=f"{season_code}-schedule",
                ),
                _schedule_bundle(bundle),
            )
            database.ingest_bundle(
                replace(
                    SOURCE,
                    content_sha256=f"{season_code}-results",
                ),
                bundle,
            )

        report = run_assumption_audit(
            database,
            {
                "2024-25": replace(RULES, season="2024-25"),
                "2025-26": RULES,
            },
            development_seasons=("2024-25", "2025-26"),
            origin_gameweek_start=2,
            origin_gameweek_end=3,
            output_path=tmp_path / "audit.json",
            artifact_directory=tmp_path / "models",
            seed=1,
        )

        assert len(report.variants) == 5
        assert len(report.learned_losses) == 3
        assert {
            result.estimand for result in report.learned_losses
        } == {"conditional_median", "conditional_mean"}
        assert all(
            len(result.seasons) == 2 for result in report.variants
        )
        assert (tmp_path / "audit.json").exists()
        assert not any(
            "validation" in limitation.lower()
            and "previously inspected" not in limitation.lower()
            for limitation in report.limitations
        )
        backtest_count = database.connection.execute(
            "SELECT COUNT(*) FROM projection_backtest_runs"
        ).fetchone()[0]
        rerun = run_assumption_audit(
            database,
            {
                "2024-25": replace(RULES, season="2024-25"),
                "2025-26": RULES,
            },
            development_seasons=("2024-25", "2025-26"),
            origin_gameweek_start=2,
            origin_gameweek_end=3,
            output_path=tmp_path / "audit-rerun.json",
            artifact_directory=tmp_path / "models-rerun",
            seed=1,
        )
        assert {
            result.seasons[0].backtest_run_id
            for result in rerun.variants
        } == {
            result.seasons[0].backtest_run_id
            for result in report.variants
        }
        assert database.connection.execute(
            "SELECT COUNT(*) FROM projection_backtest_runs"
        ).fetchone()[0] == backtest_count
