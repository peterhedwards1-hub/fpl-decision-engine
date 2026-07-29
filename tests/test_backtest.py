from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_engine.backtest import ProjectionBacktester, load_backtest_report
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
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
from fpl_engine.projections import RatesProjectionModel

RULES = load_season_rules(Path("config/seasons/2025-26.json"))
SOURCE = IngestionSource(
    name="historical-test",
    retrieved_at=datetime(2026, 7, 1, tzinfo=UTC),
    identifier_namespace="official-fpl",
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


def test_walk_forward_backtest_persists_predictions_and_metrics(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
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
        assert report.overall.samples == 2
        assert report.overall.points_mae == pytest.approx(
            round(sum(abs(error) for error in point_errors) / 2, 4)
        )
        assert report.overall.points_rmse == pytest.approx(
            round(math.sqrt(sum(error**2 for error in point_errors) / 2), 4)
        )
        assert report.by_position[0].value == "FWD"
        assert report.by_horizon[0].value == "1"
        assert [row["actual_points"] for row in rows] == [5, 1]
        assert [row["actual_minutes"] for row in rows] == [60, 20]
        assert database.connection.execute(
            "SELECT COUNT(*) FROM projection_runs"
        ).fetchone()[0] == 0
        assert load_backtest_report(
            database, report.backtest_run_id
        ).as_dict() == report.as_dict()


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


def test_strict_pre_deadline_policy_rejects_reconstructed_snapshots(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
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
