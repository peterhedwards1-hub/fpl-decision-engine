from datetime import UTC, datetime

import pytest

from fpl_engine.domain import Position
from fpl_engine.history import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    HistoricalDatabase,
    IngestionSource,
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)


SOURCE = IngestionSource(
    name="test-source",
    url="https://example.invalid/history",
    retrieved_at=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
    content_sha256="abc123",
)


def make_bundle(*, points: int = 8) -> HistoricalBundle:
    return HistoricalBundle(
        season=SeasonRecord("2025-26", "2025/26"),
        teams=(
            TeamRecord("1", "North London", "NTH"),
            TeamRecord("2", "South Coast", "STH"),
        ),
        players=(PlayerRecord("101", "Example", "Alex", "Example"),),
        player_seasons=(
            PlayerSeasonRecord("101", "1", Position.MID, 75, 78),
        ),
        gameweeks=(
            GameweekRecord(1, "2025-08-15T18:30:00+00:00", True),
        ),
        fixtures=(
            FixtureRecord("5001", "1", "2", 1, finished=True),
            FixtureRecord("5002", "2", "1", 1, finished=True),
        ),
        fixture_stats=(
            PlayerFixtureStatsRecord(
                "101",
                "5001",
                minutes=90,
                starts=True,
                goals=1,
                expected_goals=0.65,
                total_points=points,
            ),
            PlayerFixtureStatsRecord(
                "101",
                "5002",
                minutes=25,
                expected_goals=0.15,
                total_points=2,
            ),
        ),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                "101",
                1,
                75,
                datetime(2025, 8, 15, 17, 0, tzinfo=UTC),
                selected_by_percent=12.4,
                status="a",
            ),
        ),
    )


def test_initialise_creates_versioned_schema(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()

        assert database.schema_version == 1
        table_names = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "player_fixture_stats" in table_names
        assert "player_gameweek_snapshots" in table_names


def test_bundle_ingestion_preserves_double_gameweek_fixtures(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        run_id = database.ingest_bundle(SOURCE, make_bundle())

        summary = database.season_summary("2025-26")
        totals = database.player_gameweek_totals("2025-26", "101", 1)
        run = database.connection.execute(
            "SELECT status, row_count, content_sha256 FROM ingestion_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        assert summary == {
            "teams": 2,
            "players": 1,
            "gameweeks": 1,
            "fixtures": 2,
            "fixture_stats": 2,
            "gameweek_snapshots": 1,
        }
        assert totals is not None
        assert totals["minutes"] == 115
        assert totals["total_points"] == 10
        assert totals["expected_goals"] == pytest.approx(0.8)
        assert totals["price_tenths"] == 75
        assert run["status"] == "completed"
        assert run["row_count"] == 9
        assert run["content_sha256"] == "abc123"


def test_reingestion_is_idempotent_and_updates_values(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, make_bundle(points=8))
        database.ingest_bundle(SOURCE, make_bundle(points=9))

        summary = database.season_summary("2025-26")
        totals = database.player_gameweek_totals("2025-26", "101", 1)

        assert summary["fixture_stats"] == 2
        assert totals is not None
        assert totals["total_points"] == 11
        assert database.connection.execute(
            "SELECT COUNT(*) FROM ingestion_runs"
        ).fetchone()[0] == 2


def test_failed_bundle_rolls_back_domain_rows_and_records_failure(tmp_path) -> None:
    invalid_bundle = HistoricalBundle(
        season=SeasonRecord("2025-26", "2025/26"),
        players=(PlayerRecord("101", "Example"),),
        player_seasons=(
            PlayerSeasonRecord("101", "missing-team", Position.MID, 75),
        ),
    )

    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()

        with pytest.raises(ValueError, match="team"):
            database.ingest_bundle(SOURCE, invalid_bundle)

        run = database.connection.execute(
            "SELECT status, row_count, error_message FROM ingestion_runs"
        ).fetchone()
        assert run["status"] == "failed"
        assert run["row_count"] == 0
        assert "team" in run["error_message"]
        assert database.connection.execute(
            "SELECT COUNT(*) FROM seasons"
        ).fetchone()[0] == 0
        assert database.connection.execute(
            "SELECT COUNT(*) FROM players"
        ).fetchone()[0] == 0
