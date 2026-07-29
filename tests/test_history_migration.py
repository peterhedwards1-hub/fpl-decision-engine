from __future__ import annotations

import sqlite3
from pathlib import Path

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.schema import MIGRATE_V2_TO_V3_SQL

V2_SCHEMA = Path(__file__).parent / "fixtures" / "schema_v2.sql"


def _create_v3_database(
    path: Path, *, timing_quality: str = "date_only", observed_at: str | None = None
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(V2_SCHEMA.read_text())
    connection.executescript(
        """
        INSERT INTO ingestion_runs
            (id, source_name, retrieved_at, status)
        VALUES (1, 'official-fpl-api', '2025-08-15T12:00:00+00:00', 'completed');
        INSERT INTO seasons (id, code, name) VALUES (1, '2025-26', '2025/26');
        INSERT INTO teams
            (id, season_id, source_team_id, name, short_name, provenance_run_id)
        VALUES (1, 1, '1', 'North Town', 'NTH', 1),
               (2, 1, '2', 'South City', 'STH', 1);
        INSERT INTO players
            (id, source_name, source_player_id, first_name, second_name, web_name)
        VALUES (1, 'official-fpl-api', '101', 'Ada', 'Striker', 'Ada');
        INSERT INTO player_seasons
            (id, season_id, player_id, team_id, position, provenance_run_id)
        VALUES (1, 1, 1, 1, 'FWD', 1);
        INSERT INTO gameweeks
            (id, season_id, number, is_finished, provenance_run_id)
        VALUES (1, 1, 1, 1, 1);
        INSERT INTO fixtures
            (id, season_id, source_fixture_id, gameweek_id, home_team_id,
             away_team_id, finished, provenance_run_id)
        VALUES (1, 1, '501', 1, 1, 2, 1, 1);
        INSERT INTO player_fixture_stats
            (id, player_season_id, fixture_id, minutes, total_points, provenance_run_id)
        VALUES (1, 1, 1, 90, 8, 1);
        INSERT INTO player_gameweek_snapshots
            (id, player_season_id, gameweek_id, team_id, price_tenths,
             captured_at, provenance_run_id)
        VALUES (1, 1, 1, 1, 75, '2025-08-15T12:00:00+00:00', 1);
        PRAGMA user_version = 2;
        """
    )
    connection.commit()
    connection.executescript(MIGRATE_V2_TO_V3_SQL)
    connection.executescript(
        """
        INSERT INTO ingestion_runs
            (id, source_name, retrieved_at, status)
        VALUES (2, 'historical-source', '2025-08-16T02:00:00+02:00', 'completed');
        INSERT INTO ingestion_runs
            (id, source_name, retrieved_at, status)
        VALUES (3, 'historical-source', '2025-08-17T00:00:00+00:00', 'completed');
        INSERT INTO player_gameweek_observations (
            id, player_season_id, gameweek_id, observation_kind, observed_at,
            timing_quality, price_tenths, source_observation_key, provenance_run_id
        ) VALUES (3, 1, 1, 'historical_reconstruction', NULL, 'unknown', 77,
                  'unknown', 3);
        """,
    )
    connection.execute(
        """
        INSERT INTO player_gameweek_observations (
            id, player_season_id, gameweek_id, observation_kind, observed_at,
            timing_quality, price_tenths, source_observation_key, provenance_run_id
        ) VALUES (2, 1, 1, 'post_gameweek', ?, ?, 76, 'date-only', 2)
        """,
        (observed_at, timing_quality),
    )
    connection.execute(
        "UPDATE player_gameweek_observations SET observed_at = ?, timing_quality = ? WHERE id = 2",
        (observed_at, timing_quality),
    )
    connection.commit()
    connection.close()


def test_populated_v2_database_migrates_in_place_without_data_loss(tmp_path) -> None:
    database_path = tmp_path / "history.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(V2_SCHEMA.read_text())
    connection.executescript(
        """
        INSERT INTO ingestion_runs
            (id, source_name, source_url, retrieved_at, content_sha256, status, row_count)
        VALUES
            (1, 'official-fpl-api', 'https://example.invalid',
             '2025-08-15T12:00:00+00:00', 'digest', 'completed', 9);
        INSERT INTO seasons (id, code, name) VALUES (1, '2025-26', '2025/26');
        INSERT INTO teams
            (id, season_id, source_team_id, name, short_name, provenance_run_id)
        VALUES
            (1, 1, '1', 'North Town', 'NTH', 1),
            (2, 1, '2', 'South City', 'STH', 1);
        INSERT INTO players
            (id, source_name, source_player_id, first_name, second_name, web_name)
        VALUES (1, 'official-fpl-api', '101', 'Ada', 'Striker', 'Ada');
        INSERT INTO player_seasons
            (id, season_id, player_id, team_id, position, provenance_run_id)
        VALUES (1, 1, 1, 1, 'FWD', 1);
        INSERT INTO gameweeks
            (id, season_id, number, is_finished, provenance_run_id)
        VALUES (1, 1, 1, 1, 1);
        INSERT INTO fixtures
            (id, season_id, source_fixture_id, gameweek_id, home_team_id,
             away_team_id, finished, provenance_run_id)
        VALUES (1, 1, '501', 1, 1, 2, 1, 1);
        INSERT INTO player_fixture_stats
            (id, player_season_id, fixture_id, minutes, total_points, provenance_run_id)
        VALUES (1, 1, 1, 90, 8, 1);
        INSERT INTO player_gameweek_snapshots
            (id, player_season_id, gameweek_id, team_id, price_tenths,
             selected_by_percent, captured_at, provenance_run_id)
        VALUES (1, 1, 1, 1, 75, 12.3, '2025-08-15T12:00:00+00:00', 1);
        PRAGMA user_version = 2;
        """
    )
    connection.commit()
    connection.close()

    with HistoricalDatabase(database_path) as database:
        database.initialise()

        assert database.schema_version == 10
        assert database.connection.execute(
            "SELECT COUNT(*) FROM seasons"
        ).fetchone()[0] == 1
        assert database.connection.execute(
            "SELECT COUNT(*) FROM players"
        ).fetchone()[0] == 1
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_seasons"
        ).fetchone()[0] == 1
        assert database.connection.execute(
            "SELECT COUNT(*) FROM fixtures"
        ).fetchone()[0] == 1
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_fixture_stats"
        ).fetchone()[0] == 1
        observation = database.connection.execute(
            """
            SELECT observation_kind, timing_quality, observed_at, observed_on,
                   selected_by_percent, source_observation_key
            FROM player_gameweek_observations
            """
        ).fetchone()
        assert observation["observation_kind"] == "live_pre_deadline"
        assert observation["timing_quality"] == "exact"
        assert observation["observed_at"] == "2025-08-15T12:00:00+00:00"
        assert observation["observed_on"] is None
        assert observation["selected_by_percent"] == 12.3
        assert observation["source_observation_key"] == "legacy-v2-1"
        assert database.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not database.connection.execute("PRAGMA foreign_key_check").fetchall()

        database.initialise()
        assert database.schema_version == 10
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_gameweek_observations"
        ).fetchone()[0] == 1


def test_newer_schema_versions_are_rejected(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.connection.execute("PRAGMA user_version = 11")
        database.connection.commit()

        try:
            database.initialise()
        except RuntimeError as error:
            assert "newer than supported" in str(error)
        else:
            raise AssertionError("Newer schema version should be rejected")


def test_version_3_migrates_populated_timing_rows_to_version_4(tmp_path) -> None:
    database_path = tmp_path / "history.sqlite3"
    _create_v3_database(
        database_path,
        timing_quality="date_only",
        observed_at="2025-08-16T23:30:00+02:00",
    )

    with HistoricalDatabase(database_path) as database:
        assert database.schema_version == 3
        database.initialise()
        assert database.schema_version == 10
        rows = database.connection.execute(
            """
            SELECT id, timing_quality, observed_at, observed_on
            FROM player_gameweek_observations ORDER BY id
            """
        ).fetchall()
        assert rows[0]["timing_quality"] == "exact"
        assert rows[0]["observed_at"] == "2025-08-15T12:00:00+00:00"
        assert rows[0]["observed_on"] is None
        assert rows[1]["timing_quality"] == "date_only"
        assert rows[1]["observed_at"] is None
        assert rows[1]["observed_on"] == "2025-08-16"
        assert rows[2]["timing_quality"] == "unknown"
        assert rows[2]["observed_at"] is None
        assert rows[2]["observed_on"] is None
        assert database.connection.execute(
            "SELECT retrieved_at FROM ingestion_runs WHERE id = 2"
        ).fetchone()[0] == "2025-08-16T00:00:00+00:00"
        assert not database.connection.execute("PRAGMA foreign_key_check").fetchall()
        database.initialise()
        assert database.schema_version == 10


def test_version_3_migration_rejects_ambiguous_timing_rows(tmp_path) -> None:
    cases = (
        ("unknown", "2025-08-16T12:00:00+00:00", "has an observed_at timestamp"),
        ("exact", None, "requires an observed_at timestamp"),
        ("date_only", None, "requires an observed_at timestamp"),
    )
    for index, (quality, observed_at, message) in enumerate(cases):
        database_path = tmp_path / f"inconsistent-{index}.sqlite3"
        _create_v3_database(
            database_path, timing_quality=quality, observed_at=observed_at
        )
        with HistoricalDatabase(database_path) as database:
            try:
                database.initialise()
            except RuntimeError as error:
                assert message in str(error)
                assert "row 2" in str(error)
                assert database.schema_version == 3
                assert database.connection.execute(
                    "SELECT COUNT(*) FROM player_gameweek_observations"
                ).fetchone()[0] == 3
            else:
                raise AssertionError("Ambiguous v3 timing row should fail migration")
