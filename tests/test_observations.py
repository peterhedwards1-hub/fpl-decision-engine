from dataclasses import replace
from datetime import UTC, datetime

from test_history_database import SOURCE, make_bundle

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
    IngestionSource,
    PlayerGameweekSnapshotRecord,
)


def test_identical_observation_ingestion_is_idempotent(tmp_path) -> None:
    bundle = make_bundle()
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        first_run = database.ingest_bundle(SOURCE, bundle)
        second_run = database.ingest_bundle(SOURCE, bundle)

        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_gameweek_observations"
        ).fetchone()[0] == 1
        observation = database.connection.execute(
            "SELECT provenance_run_id FROM player_gameweek_observations"
        ).fetchone()
        assert observation["provenance_run_id"] == second_run
        assert second_run != first_run


def test_observation_history_preserves_team_and_nullable_ownership(tmp_path) -> None:
    base = make_bundle()
    observations = (
        replace(
            base.gameweek_snapshots[0],
            captured_at=datetime(2025, 8, 15, 17, 0, tzinfo=UTC),
            source_team_id="1",
            selected_by_percent=12.3,
            selected_count=None,
            source_observation_key="before-transfer",
        ),
        PlayerGameweekSnapshotRecord(
            source_player_id="101",
            gameweek_number=1,
            price_tenths=76,
            captured_at=None,
            source_team_id="2",
            selected_count=123456,
            selected_by_percent=None,
            observation_kind="historical_reconstruction",
            timing_quality="unknown",
            source_observation_key="historical-count",
        ),
    )
    bundle = replace(base, gameweek_snapshots=observations)
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, bundle)

        rows = database.connection.execute(
            """
            SELECT observations.team_id, observations.selected_count,
                   observations.selected_by_percent, observations.observed_at
            FROM player_gameweek_observations observations
            ORDER BY observations.id
            """
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["team_id"] != rows[1]["team_id"]
        assert rows[0]["selected_count"] is None
        assert rows[0]["selected_by_percent"] == 12.3
        assert rows[1]["selected_count"] == 123456
        assert rows[1]["selected_by_percent"] is None
        assert rows[1]["observed_at"] is None


def test_contradictory_team_id_is_rejected(tmp_path) -> None:
    base = make_bundle()
    conflicting_team = replace(base.teams[0], name="Unrelated Club")
    conflicting = replace(base, teams=(conflicting_team, base.teams[1]))
    with HistoricalDatabase(tmp_path / "history.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(SOURCE, base)

        try:
            database.ingest_bundle(
                IngestionSource(
                    name="historical-dataset",
                    retrieved_at=SOURCE.retrieved_at,
                    content_sha256="other",
                    source_revision="abc123",
                    adapter_version="vaastav-v1",
                ),
                conflicting,
            )
        except ValueError as error:
            assert "Contradictory team identity" in str(error)
        else:
            raise AssertionError("Contradictory team identity should fail")

        assert database.connection.execute(
            "SELECT COUNT(*) FROM teams WHERE name = 'Unrelated Club'"
        ).fetchone()[0] == 0
