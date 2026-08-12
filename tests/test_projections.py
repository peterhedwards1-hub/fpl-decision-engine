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
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)
from fpl_engine.projections import (
    BASELINE_V2_MODEL_CONFIG,
    DEFAULT_MODEL_CONFIG,
    DEFENSIVE_CONTRIBUTION_HIT_RATES_2025,
    DEFENSIVE_EMPIRICAL_V5_MODEL_CONFIG,
    EXPECTED_EVENTS_V4_MODEL_CONFIG,
    MODEL_VERSION,
    PRESEASON_V5_MODEL_CONFIG,
    TEAM_SHARE_XG_V5_MODEL_CONFIG,
    ProjectionModelConfig,
    ProjectionOverride,
    RatesProjectionModel,
    projection_totals,
)
from fpl_engine.simulation import simulation_inputs_from_projection

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
CAPTURED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_corrected_rules_model_is_the_versioned_default() -> None:
    assert MODEL_VERSION == "rates-rules-corrected-v4"
    assert DEFAULT_MODEL_CONFIG.player_rate_prior_minutes == 1776.650037050099
    assert DEFAULT_MODEL_CONFIG.recent_gameweeks == 4
    assert DEFAULT_MODEL_CONFIG.defensive_contribution_model == "threshold_poisson"
    assert DEFAULT_MODEL_CONFIG != BASELINE_V2_MODEL_CONFIG
    assert (
        DEFENSIVE_EMPIRICAL_V5_MODEL_CONFIG
        .defensive_contribution_model
        == "empirical_2025_minutes_band"
    )


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


def _previous_season_bundle() -> HistoricalBundle:
    """A completed 2025/26 in which North Town dominate and South City do not."""

    return HistoricalBundle(
        season=SeasonRecord(code="2025-26", name="2025/26"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=(PlayerRecord("101", "Ada", "Striker", "Ada"),),
        player_seasons=(PlayerSeasonRecord("101", "1", Position.FWD),),
        gameweeks=(
            GameweekRecord(1, "2025-08-15T17:30:00Z", True),
            GameweekRecord(2, "2025-08-22T17:30:00Z", True),
        ),
        fixtures=(
            FixtureRecord(
                "401", "1", "2", 1, "2025-08-16T14:00:00Z", 4, 0, True
            ),
            FixtureRecord(
                "402", "2", "1", 2, "2025-08-23T14:00:00Z", 0, 3, True
            ),
        ),
        gameweek_snapshots=(
            PlayerGameweekSnapshotRecord(
                source_player_id="101",
                gameweek_number=1,
                price_tenths=75,
                captured_at=datetime(2025, 8, 14, 12, 0, tzinfo=UTC),
                source_team_id="1",
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                status="a",
                source_observation_key="prev-pre-gw1",
            ),
        ),
    )


def _promoted_season_bundle() -> HistoricalBundle:
    """A 2026/27 whose third club has no previous top-flight season."""

    base = _bundle()
    return replace(
        base,
        teams=(*base.teams, TeamRecord("3", "New Town", "NEW")),
    )


def _preseason_database(database: HistoricalDatabase) -> None:
    for index, bundle in enumerate(
        (_previous_season_bundle(), _promoted_season_bundle())
    ):
        database.ingest_bundle(
            IngestionSource(
                name="official-fpl-api",
                retrieved_at=CAPTURED_AT + timedelta(seconds=index),
                identifier_namespace="official-fpl",
            ),
            bundle,
        )


def _strengths_by_short_name(
    database: HistoricalDatabase,
    config: ProjectionModelConfig,
) -> dict[str, dict[str, float]]:
    result = RatesProjectionModel(database, RULES, config=config).project(
        season_code="2026-27",
        start_gameweek=1,
        horizon_gameweeks=1,
        generated_at=CAPTURED_AT,
        persist=False,
    )
    names = {
        str(row["id"]): str(row["short_name"])
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.short_name
            FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = '2026-27'
            """
        )
    }
    return {
        names[team_id]: strength
        for team_id, strength in result.team_strengths.items()
    }


def test_incumbent_cannot_separate_clubs_before_the_season_starts(
    tmp_path,
) -> None:
    # The defect the carry-forward option exists to fix. Guarded so the
    # incumbent's behaviour cannot drift while it remains the default.
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        _preseason_database(database)
        strengths = _strengths_by_short_name(database, DEFAULT_MODEL_CONFIG)

    assert set(strengths) == {"NTH", "STH", "NEW"}
    for value in strengths.values():
        assert value["attack"] == pytest.approx(1.0)
        assert value["defence"] == pytest.approx(1.0)


def test_carry_forward_separates_clubs_and_prices_promoted_sides(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        _preseason_database(database)
        strengths = _strengths_by_short_name(
            database, PRESEASON_V5_MODEL_CONFIG
        )

    assert strengths["NTH"]["attack"] > strengths["STH"]["attack"]
    assert strengths["NTH"]["defence"] < strengths["STH"]["defence"]
    # New Town played no previous top-flight football, so they take the
    # declared promoted prior exactly rather than the league average.
    assert strengths["NEW"]["attack"] == pytest.approx(
        PRESEASON_V5_MODEL_CONFIG.promoted_team_attack_multiplier
    )
    assert strengths["NEW"]["defence"] == pytest.approx(
        PRESEASON_V5_MODEL_CONFIG.promoted_team_defence_multiplier
    )
    # The previous season produced seven goals across four team-matches.
    assert strengths["NTH"]["league_average_goals"] == pytest.approx(1.75)


def test_carry_forward_is_regressed_toward_the_league_average(
    tmp_path,
) -> None:
    # A two-match previous season is thin evidence, so the carried prior must
    # sit well inside the raw ratio rather than reproducing it.
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        _preseason_database(database)
        strengths = _strengths_by_short_name(
            database, PRESEASON_V5_MODEL_CONFIG
        )
        heavier = _strengths_by_short_name(
            database,
            replace(
                PRESEASON_V5_MODEL_CONFIG,
                carry_forward_regression_matches=1000.0,
            ),
        )

    raw_ratio = 3.5 / 1.75
    assert 1.0 < strengths["NTH"]["attack"] < raw_ratio
    # Heavier regression must collapse the carried prior back toward parity.
    assert heavier["NTH"]["attack"] < strengths["NTH"]["attack"]
    assert heavier["NTH"]["attack"] == pytest.approx(1.0, abs=0.01)


def test_cold_start_prior_separates_new_players_by_price(tmp_path) -> None:
    base = _bundle()
    expensive, cheap = "101", "102"
    bundle = replace(
        base,
        players=(
            base.players[0],
            PlayerRecord(cheap, "Bud", "Reserve", "Bud"),
        ),
        player_seasons=(
            base.player_seasons[0],
            PlayerSeasonRecord(cheap, "1", Position.FWD),
        ),
        gameweek_snapshots=(
            base.gameweek_snapshots[0],
            replace(
                base.gameweek_snapshots[0],
                source_player_id=cheap,
                price_tenths=45,
                source_observation_key="pre-gw1-cheap",
            ),
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

        def goal_points(config: ProjectionModelConfig) -> dict[str, float]:
            result = RatesProjectionModel(
                database, RULES, config=config
            ).project(
                season_code="2026-27",
                start_gameweek=1,
                horizon_gameweeks=1,
                generated_at=CAPTURED_AT,
                persist=False,
            )
            return {
                projection.source_player_id: projection.goal_points
                for projection in result.projections
            }

        incumbent = goal_points(DEFAULT_MODEL_CONFIG)
        challenger = goal_points(PRESEASON_V5_MODEL_CONFIG)

    # Neither player has any history, so the incumbent gives a £7.5m signing
    # and a £4.5m reserve the same attacking prior.
    assert incumbent[expensive] == pytest.approx(incumbent[cheap])
    assert challenger[expensive] > challenger[cheap]


def test_cold_start_price_factor_fades_as_minutes_accumulate() -> None:
    config = replace(PRESEASON_V5_MODEL_CONFIG, player_rate_prior_minutes=900.0)
    model = RatesProjectionModel.__new__(RatesProjectionModel)
    model.config = config
    players = [
        {"position": "FWD", "price_tenths": 120},
        {"position": "FWD", "price_tenths": 60},
        {"position": "FWD", "price_tenths": 40},
    ]

    model._prepare_cold_start_priors(players)

    factors = [player["_cold_start_price_factor"] for player in players]
    assert factors[0] > factors[1] > factors[2]
    # The median-priced player is the reference and is left untouched.
    assert factors[1] == pytest.approx(1.0)


def test_appearance_projection_uses_configured_scoring_values(tmp_path) -> None:
    scoring = replace(
        RULES.scoring,
        appearance_under_60=3,
        appearance_60_or_more=7,
    )
    rules = replace(RULES, scoring=scoring)
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
        projection = RatesProjectionModel(database, rules).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        ).projections[0]

    expected = (
        (
            projection.appearance_probability
            - projection.sixty_probability
        )
        * 3
        + projection.sixty_probability * 7
    )
    assert projection.appearance_points == pytest.approx(
        round(expected, 3),
        abs=0.001,
    )


def test_empirical_defensive_contribution_challenger_uses_minutes_bands(
    tmp_path,
) -> None:
    defender_bundle = replace(
        _bundle(),
        player_seasons=(
            replace(_bundle().player_seasons[0], position=Position.DEF),
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
            defender_bundle,
        )
        projection = RatesProjectionModel(
            database,
            RULES,
            config=DEFENSIVE_EMPIRICAL_V5_MODEL_CONFIG,
        ).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        ).projections[0]

    under_rate, sixty_rate = (
        DEFENSIVE_CONTRIBUTION_HIT_RATES_2025[Position.DEF]
    )
    expected = (
        (
            projection.appearance_probability
            - projection.sixty_probability
        )
        * under_rate
        + projection.sixty_probability * sixty_rate
    ) * RULES.scoring.defensive_contribution_points
    assert projection.defensive_contribution_points == pytest.approx(
        round(expected, 3),
        abs=0.001,
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


def test_expected_event_challenger_uses_xg_and_xa_with_actual_fallback(
    tmp_path,
) -> None:
    evidence = replace(
        _bundle(),
        fixture_stats=(
            PlayerFixtureStatsRecord(
                "101",
                "501",
                minutes=90,
                goals=0,
                assists=0,
                expected_goals=2.0,
                expected_assists=1.0,
            ),
        ),
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="expected-events",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            evidence,
        )
        actual_events = RatesProjectionModel(
            database,
            RULES,
            config=DEFAULT_MODEL_CONFIG,
        ).project(
            season_code="2026-27",
            start_gameweek=2,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        )
        expected_events = RatesProjectionModel(
            database,
            RULES,
            config=EXPECTED_EVENTS_V4_MODEL_CONFIG,
        ).project(
            season_code="2026-27",
            start_gameweek=2,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        )

        assert expected_events.projections[0].goal_points > (
            actual_events.projections[0].goal_points
        )
        assert expected_events.projections[0].assist_points > (
            actual_events.projections[0].assist_points
        )


def test_team_share_xg_challenger_is_coherent_with_team_expectation(
    tmp_path,
) -> None:
    players = tuple(
        PlayerRecord(str(player_id), f"Player {player_id}")
        for player_id in range(1, 5)
    )
    evidence = HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=players,
        player_seasons=tuple(
            PlayerSeasonRecord(
                str(player_id),
                "1" if player_id <= 2 else "2",
                Position.FWD,
            )
            for player_id in range(1, 5)
        ),
        gameweeks=(
            GameweekRecord(1, "2026-08-14T17:30:00Z", True),
            GameweekRecord(2, "2026-08-21T17:30:00Z", False),
        ),
        fixtures=(
            FixtureRecord(
                "501",
                "1",
                "2",
                1,
                "2026-08-15T14:00:00Z",
                2,
                1,
                True,
            ),
            FixtureRecord(
                "502",
                "2",
                "1",
                2,
                "2026-08-22T14:00:00Z",
            ),
        ),
        fixture_stats=tuple(
            PlayerFixtureStatsRecord(
                str(player_id),
                "501",
                minutes=90,
                expected_goals=(0.8, 0.4, 0.5, 0.3)[player_id - 1],
                expected_assists=(0.2, 0.3, 0.1, 0.2)[player_id - 1],
            )
            for player_id in range(1, 5)
        ),
        gameweek_snapshots=tuple(
            PlayerGameweekSnapshotRecord(
                source_player_id=str(player_id),
                gameweek_number=1,
                price_tenths=60,
                captured_at=CAPTURED_AT,
                source_team_id="1" if player_id <= 2 else "2",
                observation_kind="live_pre_deadline",
                timing_quality="exact",
                source_observation_key=f"p{player_id}",
            )
            for player_id in range(1, 5)
        ),
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="expected-events",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            evidence,
        )
        result = RatesProjectionModel(
            database,
            RULES,
            config=TEAM_SHARE_XG_V5_MODEL_CONFIG,
        ).project(
            season_code="2026-27",
            start_gameweek=2,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        )
        simulation_fixtures, simulation_players = (
            simulation_inputs_from_projection(
                database,
                result,
                season_code="2026-27",
                gameweek_number=2,
                rules=RULES,
            )
        )

    for team in ("NTH", "STH"):
        projections = tuple(
            projection
            for projection in result.projections
            if projection.team_short_name == team
        )
        team_lambda = projections[0].latent_expectations[
            "team_expected_goals"
        ]
        assert sum(
            projection.latent_expectations["goal_share"]
            for projection in projections
        ) == pytest.approx(1.0)
        assert sum(
            projection.goal_points for projection in projections
        ) / RULES.scoring.goals["FWD"] == pytest.approx(
            team_lambda,
            abs=0.001,
        )
    assert len(simulation_fixtures) == 1
    assert len(simulation_players) == 4


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


def _budget_bundle(count: int = 24) -> HistoricalBundle:
    """One club with more squad players than a fixture has minutes for."""

    return HistoricalBundle(
        season=SeasonRecord(code="2026-27", name="2026/27"),
        teams=(
            TeamRecord("1", "North Town", "NTH"),
            TeamRecord("2", "South City", "STH"),
        ),
        players=tuple(
            PlayerRecord(str(player_id), f"Player {player_id}")
            for player_id in range(1, count + 1)
        ),
        player_seasons=tuple(
            PlayerSeasonRecord(str(player_id), "1", Position.MID)
            for player_id in range(1, count + 1)
        ),
        gameweeks=(GameweekRecord(1, "2026-08-14T17:30:00Z", False),),
        fixtures=(FixtureRecord("501", "1", "2", 1, "2026-08-15T14:00:00Z"),),
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
            for player_id in range(1, count + 1)
        ),
    )


def _appearance_by_player(database, config) -> dict[str, float]:
    model = RatesProjectionModel(database, RULES, config=config)
    players = model._players(
        "2026-27", 1, observation_mode="latest_available", maximum_ingestion_run_id=None
    )
    model._prepare_cold_start_priors(players)
    model._prepare_minutes(
        players, season_code="2026-27", start_gameweek=1, use_availability=True
    )
    return {
        str(player["source_player_id"]): float(player["_appearance_probability"])
        for player in players
    }


def test_reconciliation_preserving_appearance_keeps_the_estimated_probability(
    tmp_path,
) -> None:
    """The team-minutes budget must correct minutes, not availability.

    Left off, a club's allocated share back-derives every player's appearance
    probability, so availability moves with squad depth. Switched on, the
    estimate survives untouched and the correction lands on conditional
    minutes instead.
    """

    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="official-fpl-api",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            _budget_bundle(),
        )

        base = ProjectionModelConfig(minutes_model="two_stage")
        unreconciled = _appearance_by_player(
            database, replace(base, enforce_team_minutes=False)
        )
        incumbent = _appearance_by_player(database, base)
        preserving = _appearance_by_player(
            database, replace(base, minutes_reconciliation_preserves_appearance=True)
        )

        # The incumbent rewrites availability; the new path does not.
        assert incumbent != unreconciled
        assert preserving == unreconciled

        result = RatesProjectionModel(
            database,
            RULES,
            config=replace(base, minutes_reconciliation_preserves_appearance=True),
        ).project(
            season_code="2026-27",
            start_gameweek=1,
            horizon_gameweeks=1,
            generated_at=CAPTURED_AT,
            persist=False,
        )
        minutes = [projection.expected_minutes for projection in result.projections]
        # Minutes stay physical: never negative, never above a full match, and
        # never inventing more than the fixture actually contains.
        assert all(0 <= value <= 90 for value in minutes)
        assert sum(minutes) <= 990 + 1e-6


def test_reconciliation_default_is_unchanged() -> None:
    """The incumbent must not move because the new path exists."""

    assert ProjectionModelConfig().minutes_reconciliation_preserves_appearance is False
