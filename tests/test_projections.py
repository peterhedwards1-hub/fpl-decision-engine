from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)
from fpl_engine.projections import (
    MODEL_VERSION,
    ProjectionModelConfig,
    ProjectionOverride,
    RatesProjectionModel,
    projection_totals,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
CAPTURED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _bundle() -> HistoricalBundle:
    return HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=(PlayerRecord("101", "Ada", "Striker", "Ada"),),
        player_seasons=(
            PlayerSeasonRecord("101", "1", Position.FWD),
        ),
        gameweeks=(
            GameweekRecord(1, "2026-08-14T17:30:00Z", False),
            GameweekRecord(2, "2026-08-21T17:30:00Z", False),
        ),
        fixtures=(
            FixtureRecord("501", "1", "2", 1, "2026-08-15T14:00:00Z"),
            FixtureRecord("502", "2", "1", 2, "2026-08-22T14:00:00Z"),
        ),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                source_player_id="101",
                gameweek_number=1,
                price_tenths=75,
                captured_at=CAPTURED_AT,
                source_team_id="1",
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                status="a",
                source_observation_key="pre-gw1",
            ),
        ),
    )


def test_rates_model_projects_components_and_persists_versioned_run(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="official-fpl-api",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            _bundle(),
        )
        result = RatesProjectionModel(database, RULES).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=2,
            overrides=(
                ProjectionOverride("101", 1, 30.0, "Managed preseason minutes"),
            ),
            generated_at=CAPTURED_AT,
        )

        assert result.model_version == MODEL_VERSION
        assert result.projection_run_id == 1
        assert len(result.projections) == 2
        assert result.projections[0].expected_minutes == 30
        assert result.projections[0].override_rationale == "Managed preseason minutes"
        assert result.projections[0].goal_points > 0
        assert result.projections[0].expected_points > 0
        assert result.projections[1].expected_minutes > 30
        assert len(projection_totals(result.projections)) == 1
        run = database.connection.execute(
            "SELECT model_version, horizon_gameweeks FROM projection_runs"
        ).fetchone()
        assert dict(run) == {
            "model_version": MODEL_VERSION,
            "horizon_gameweeks": 2,
        }
        assert database.connection.execute(
            "SELECT COUNT(*) FROM player_gameweek_projections"
        ).fetchone()[0] == 2


def test_two_stage_minutes_respect_team_fixture_budget(tmp_path) -> None:
    players = tuple(
        PlayerRecord(str(player_id), f"Player {player_id}")
        for player_id in range(1, 13)
    )
    bundle = HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=players,
        player_seasons=tuple(
            PlayerSeasonRecord(
                str(player_id),
                "1",
                Position.MID,
            )
            for player_id in range(1, 13)
        ),
        gameweeks=(
            GameweekRecord(1, "2026-08-14T17:30:00Z", False),
        ),
        fixtures=(
            FixtureRecord("501", "1", "2", 1, "2026-08-15T14:00:00Z"),
        ),
        gameweek_snapshots=tuple(
            PlayerGameweekSnapshotRecord(
                source_player_id=str(player_id),
                gameweek_number=1,
                price_tenths=50,
                captured_at=CAPTURED_AT,
                source_team_id="1",
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                source_observation_key=f"pre-gw1-{player_id}",
            )
            for player_id in range(1, 13)
        ),
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="official-fpl-api",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            bundle,
        )

        result = RatesProjectionModel(
            database,
            RULES,
            config=ProjectionModelConfig(minutes_model="two_stage"),
        ).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        )

        assert sum(
            projection.expected_minutes
            for projection in result.projections
        ) == 990
        assert all(
            0 <= projection.expected_minutes <= 90
            for projection in result.projections
        )
        assert all(
            0 <= projection.appearance_points <= 2
            for projection in result.projections
        )
