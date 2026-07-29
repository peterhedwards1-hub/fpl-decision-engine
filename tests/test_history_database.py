from datetime import UTC, datetime
from dataclasses import replace

import pytest

from fpl_engine.domain import Position
from fpl_engine.history.csv_bundle import load_csv_bundle
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

        assert database.schema_version == 9
        table_names = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "player_fixture_stats" in table_names
        assert "player_gameweek_observations" in table_names
        assert "fixture_observations" in table_names
        assert "manager_snapshots" in table_names
        assert "manager_squad_entries" in table_names
        assert "projection_runs" in table_names
        assert "player_gameweek_projections" in table_names
        assert "news_evidence" in table_names
        assert "weekly_decision_runs" in table_names
        assert "actual_actions" in table_names
        assert "weekly_evaluations" in table_names
        assert "projection_backtest_runs" in table_names
        assert "projection_backtest_predictions" in table_names
        view_names = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            )
        }
        assert "player_gameweek_snapshots" in view_names


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
            "season_stats_observations": 0,
            "gameweek_snapshots": 1,
        }
        assert totals is not None
        assert totals["minutes"] == 115
        assert totals["total_points"] == 10
        assert totals["expected_goals"] == pytest.approx(0.8)
        assert totals["price_tenths"] == 75
        assert run["status"] == "completed"
        assert run["row_count"] == 10
        assert run["content_sha256"] == "abc123"


def test_fixture_reschedules_preserve_each_ingested_state(tmp_path) -> None:
    original = make_bundle()
    rescheduled = replace(
        original,
        fixtures=(
            replace(
                original.fixtures[0],
                kickoff_time="2025-09-20T14:00:00+00:00",
            ),
            original.fixtures[1],
        ),
    )

    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, original)
        database.ingest_bundle(SOURCE, rescheduled)

        rows = database.connection.execute(
            """
            SELECT observations.kickoff_time
            FROM fixture_observations observations
            JOIN fixtures ON fixtures.id = observations.fixture_id
            WHERE fixtures.source_fixture_id = '5001'
            ORDER BY observations.provenance_run_id
            """
        ).fetchall()

        assert [row["kickoff_time"] for row in rows] == [
            None,
            "2025-09-20T14:00:00+00:00",
        ]


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


def test_csv_loader_rejects_missing_bundle_files(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_csv_bundle(tmp_path / "missing", SeasonRecord("2025-26", "2025/26"))

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    with pytest.raises(ValueError, match="missing required files"):
        load_csv_bundle(bundle_dir, SeasonRecord("2025-26", "2025/26"))


def test_import_refreshes_same_namespace_id_through_another_delivery_source(tmp_path) -> None:
    other_source = IngestionSource(
        name="other-source",
        url=SOURCE.url,
        retrieved_at=SOURCE.retrieved_at,
        content_sha256=SOURCE.content_sha256,
        identifier_namespace="official-fpl",
        source_revision="commit-123",
        adapter_version="historical-v1",
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, make_bundle())

        database.ingest_bundle(other_source, make_bundle())

        row = database.connection.execute(
            """
            SELECT ingestion_runs.source_name
            FROM teams
            JOIN ingestion_runs ON ingestion_runs.id = teams.provenance_run_id
            WHERE teams.source_team_id = '1'
            """
        ).fetchone()
        assert row["source_name"] == "other-source"
        provenance = database.connection.execute(
            """
            SELECT identifier_namespace, source_url, content_sha256,
                   source_revision, adapter_version
            FROM ingestion_runs WHERE source_name = 'other-source'
            """
        ).fetchone()
        assert dict(provenance) == {
            "identifier_namespace": "official-fpl",
            "source_url": SOURCE.url,
            "content_sha256": SOURCE.content_sha256,
            "source_revision": "commit-123",
            "adapter_version": "historical-v1",
        }


def test_same_element_id_in_different_seasons_creates_distinct_identities(tmp_path) -> None:
    first = make_bundle()
    second = replace(
        make_bundle(),
        season=SeasonRecord("2026-27", "2026/27"),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, first)
        database.ingest_bundle(SOURCE, second)

        assert database.connection.execute(
            "SELECT COUNT(*) FROM players"
        ).fetchone()[0] == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_seasons"
        ).fetchone()[0] == 2


def test_stable_identifier_links_real_player_across_seasons(tmp_path) -> None:
    first_player = replace(make_bundle().players[0], official_fpl_code="9001")
    second_player = replace(make_bundle().players[0], official_fpl_code="9001")
    first = replace(
        make_bundle(),
        players=(first_player,),
    )
    second = replace(
        make_bundle(),
        season=SeasonRecord("2026-27", "2026/27"),
        players=(second_player,),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, first)
        database.ingest_bundle(SOURCE, second)

        assert database.connection.execute(
            "SELECT COUNT(*) FROM players"
        ).fetchone()[0] == 1
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_seasons"
        ).fetchone()[0] == 2
        identifier = database.connection.execute(
            "SELECT identifier_value FROM player_identifiers"
        ).fetchone()
        assert identifier["identifier_value"] == "9001"


def test_same_name_does_not_merge_players_without_stable_identifier(tmp_path) -> None:
    base = make_bundle()
    players = (
        base.players[0],
        PlayerRecord("102", "Example", "Alex", "Example"),
    )
    player_seasons = (
        base.player_seasons[0],
        PlayerSeasonRecord("102", "2", Position.MID, 70),
    )
    bundle = replace(base, players=players, player_seasons=player_seasons)
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, bundle)

        assert database.connection.execute(
            "SELECT COUNT(*) FROM players"
        ).fetchone()[0] == 2


def test_stable_identity_reconciles_migrated_unidentified_season_data(tmp_path) -> None:
    unidentified = make_bundle()
    identified_other_season = replace(
        make_bundle(),
        season=SeasonRecord("2026-27", "2026/27"),
        players=(replace(make_bundle().players[0], official_fpl_code="9001"),),
    )
    identified_original_season = replace(
        make_bundle(),
        players=(replace(make_bundle().players[0], official_fpl_code="9001"),),
    )

    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, unidentified)
        database.ingest_bundle(replace(SOURCE, content_sha256="other"), identified_other_season)
        database.ingest_bundle(replace(SOURCE, content_sha256="third"), identified_original_season)

        assert database.connection.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1
        assert database.connection.execute("SELECT COUNT(*) FROM player_seasons").fetchone()[0] == 2
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_fixture_stats"
        ).fetchone()[0] == 4
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_gameweek_observations"
        ).fetchone()[0] == 3
        assert database.connection.execute(
            "SELECT identifier_value FROM player_identifiers"
        ).fetchone()[0] == "9001"


def test_reconciliation_rejects_contradictory_stable_identifiers(tmp_path) -> None:
    first = replace(
        make_bundle(), players=(replace(make_bundle().players[0], official_fpl_code="9001"),)
    )
    second = replace(
        make_bundle(),
        season=SeasonRecord("2026-27", "2026/27"),
        players=(replace(make_bundle().players[0], official_fpl_code="9002"),),
    )
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, first)
        database.ingest_bundle(replace(SOURCE, content_sha256="other"), second)
        stable_id = database.connection.execute(
            "SELECT player_id FROM player_identifiers WHERE identifier_value = '9001'"
        ).fetchone()[0]
        duplicate_id = database.connection.execute(
            "SELECT player_id FROM player_identifiers WHERE identifier_value = '9002'"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="contradictory"):
            database.reconcile_player_identities(
                stable_player_id=stable_id, duplicate_player_id=duplicate_id
            )
