from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    BASELINE_V2_MODEL_CONFIG,
    DEFAULT_MODEL_CONFIG,
    MODEL_VERSION,
    ProjectionModelConfig,
    ProjectionOverride,
    RatesProjectionModel,
    projection_totals,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
CAPTURED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_corrected_rules_model_is_the_versioned_default() -> None:
    assert MODEL_VERSION == "rates-rules-corrected-v4"
    assert DEFAULT_MODEL_CONFIG.player_rate_prior_minutes == 1776.650037050099
    assert DEFAULT_MODEL_CONFIG.recent_gameweeks == 4
    assert DEFAULT_MODEL_CONFIG.defensive_contribution_model == "threshold_poisson"
    assert DEFAULT_MODEL_CONFIG != BASELINE_V2_MODEL_CONFIG


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
        assert 0 < result.projections[0].appearance_probability < 1
        assert 0 <= result.projections[0].sixty_probability <= 1
        assert result.projections[0].override_rationale == "Managed preseason minutes"
        assert result.projections[0].goal_points > 0
        assert result.projections[0].expected_points > 0
        assert result.projections[1].expected_minutes > 30
        totals = projection_totals(result.projections)
        assert len(totals) == 1
        assert totals[0]["uncertainty"] == round(
            sum(projection.uncertainty for projection in result.projections),
            2,
        )
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
        persisted_probability = database.connection.execute(
            """
            SELECT appearance_probability, sixty_probability
            FROM player_gameweek_projections
            ORDER BY gameweek_number
            LIMIT 1
            """
        ).fetchone()
        assert persisted_probability["appearance_probability"] == (
            result.projections[0].appearance_probability
        )


def test_generated_at_resolves_and_enforces_one_ingestion_cutoff(tmp_path) -> None:
    later_at = CAPTURED_AT + timedelta(days=2)
    later_bundle = replace(
        _bundle(),
        gameweek_snapshots=(
            replace(
                _bundle().gameweek_snapshots[0],
                captured_at=later_at,
                status="i",
                chance_of_playing_next_round=0,
                source_observation_key="post-cutoff",
            ),
        ),
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="initial",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            _bundle(),
        )
        database.ingest_bundle(
            IngestionSource(
                name="later",
                retrieved_at=later_at,
                identifier_namespace="official-fpl",
            ),
            later_bundle,
        )

        result = RatesProjectionModel(database, RULES).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT + timedelta(days=1),
        )

        assert result.projections[0].expected_minutes > 0
        source_run_id = database.connection.execute(
            "SELECT source_ingestion_run_id FROM projection_runs"
        ).fetchone()[0]
        assert source_run_id == 1


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


def test_position_aware_minutes_separate_goalkeeper_budget(tmp_path) -> None:
    positions = (
        Position.GK,
        Position.GK,
        *(Position.MID for _ in range(11)),
    )
    players = tuple(
        PlayerRecord(str(index), f"Player {index}")
        for index in range(1, len(positions) + 1)
    )
    bundle = HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=players,
        player_seasons=tuple(
            PlayerSeasonRecord(str(index), "1", position)
            for index, position in enumerate(positions, start=1)
        ),
        gameweeks=(
            GameweekRecord(1, "2026-08-14T17:30:00Z", False),
        ),
        fixtures=(
            FixtureRecord("501", "1", "2", 1, "2026-08-15T14:00:00Z"),
        ),
        gameweek_snapshots=tuple(
            PlayerGameweekSnapshotRecord(
                source_player_id=str(index),
                gameweek_number=1,
                price_tenths=50,
                captured_at=CAPTURED_AT,
                source_team_id="1",
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                source_observation_key=f"pre-gw1-{index}",
            )
            for index in range(1, len(positions) + 1)
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
            config=ProjectionModelConfig(
                minutes_model="two_stage",
                minutes_allocation="position_aware",
            ),
        ).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        )

        goalkeeper_minutes = sum(
            projection.expected_minutes
            for projection in result.projections
            if projection.position == Position.GK
        )
        outfield_minutes = sum(
            projection.expected_minutes
            for projection in result.projections
            if projection.position != Position.GK
        )
        assert goalkeeper_minutes == 90
        assert outfield_minutes == pytest.approx(900, abs=0.05)
