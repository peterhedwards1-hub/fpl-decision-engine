"""Transparent rates-based player projections."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .domain import Position
from .history.database import HistoricalDatabase

MODEL_VERSION = "rates-baseline-v1"
POSITION_PRIORS: dict[Position, dict[str, float]] = {
    Position.GK: {
        "minutes": 78.0,
        "goals": 0.001,
        "assists": 0.005,
        "clean_sheets": 0.28,
        "saves": 3.0,
        "bonus": 0.10,
        "defensive_contributions": 0.0,
        "yellow_cards": 0.04,
        "red_cards": 0.002,
        "own_goals": 0.002,
    },
    Position.DEF: {
        "minutes": 68.0,
        "goals": 0.045,
        "assists": 0.070,
        "clean_sheets": 0.28,
        "saves": 0.0,
        "bonus": 0.12,
        "defensive_contributions": 0.30,
        "yellow_cards": 0.16,
        "red_cards": 0.008,
        "own_goals": 0.010,
    },
    Position.MID: {
        "minutes": 66.0,
        "goals": 0.24,
        "assists": 0.18,
        "clean_sheets": 0.28,
        "saves": 0.0,
        "bonus": 0.22,
        "defensive_contributions": 0.16,
        "yellow_cards": 0.14,
        "red_cards": 0.006,
        "own_goals": 0.003,
    },
    Position.FWD: {
        "minutes": 64.0,
        "goals": 0.34,
        "assists": 0.15,
        "clean_sheets": 0.0,
        "saves": 0.0,
        "bonus": 0.28,
        "defensive_contributions": 0.10,
        "yellow_cards": 0.13,
        "red_cards": 0.006,
        "own_goals": 0.002,
    },
}


@dataclass(frozen=True)
class ProjectionModelConfig:
    player_rate_prior_minutes: float = 900.0
    minutes_prior_matches: float = 6.0
    team_prior_matches: float = 6.0
    home_attack_multiplier: float = 1.08
    away_attack_multiplier: float = 0.92
    minimum_team_multiplier: float = 0.60
    maximum_team_multiplier: float = 1.50

    def __post_init__(self) -> None:
        if self.player_rate_prior_minutes <= 0:
            raise ValueError("Player-rate prior minutes must be positive")
        if self.minutes_prior_matches <= 0:
            raise ValueError("Minutes prior matches must be positive")
        if self.team_prior_matches <= 0:
            raise ValueError("Team prior matches must be positive")
        if self.home_attack_multiplier <= 0:
            raise ValueError("Home attack multiplier must be positive")
        if self.away_attack_multiplier <= 0:
            raise ValueError("Away attack multiplier must be positive")
        if self.minimum_team_multiplier <= 0:
            raise ValueError("Minimum team multiplier must be positive")
        if self.maximum_team_multiplier < self.minimum_team_multiplier:
            raise ValueError(
                "Maximum team multiplier cannot be below the minimum"
            )


DEFAULT_MODEL_CONFIG = ProjectionModelConfig()


@dataclass(frozen=True)
class ProjectionOverride:
    source_player_id: str
    gameweek_number: int
    expected_minutes: float
    rationale: str


@dataclass(frozen=True)
class TeamStrengthOverride:
    source_team_id: str
    attack_multiplier: float
    defence_susceptibility: float
    rationale: str


@dataclass(frozen=True)
class PlayerGameweekProjection:
    source_player_id: str
    web_name: str
    team_short_name: str
    position: Position
    gameweek_number: int
    fixture_count: int
    expected_minutes: float
    appearance_points: float
    goal_points: float
    assist_points: float
    clean_sheet_points: float
    save_points: float
    defensive_contribution_points: float
    bonus_points: float
    deduction_points: float
    expected_points: float
    uncertainty: float
    assumptions: tuple[str, ...]
    override_rationale: str | None = None


@dataclass(frozen=True)
class ProjectionResult:
    projection_run_id: int | None
    model_version: str
    generated_at: datetime
    start_gameweek: int
    horizon_gameweeks: int
    projections: tuple[PlayerGameweekProjection, ...]
    team_strengths: dict[str, dict[str, float]]
    model_config: ProjectionModelConfig


class RatesProjectionModel:
    """Bayesian-shrunk per-90 baseline with explicit fixture adjustments."""

    def __init__(
        self,
        database: HistoricalDatabase,
        rules: SeasonRules,
        *,
        config: ProjectionModelConfig = DEFAULT_MODEL_CONFIG,
        model_version: str = MODEL_VERSION,
    ) -> None:
        self.database = database
        self.rules = rules
        self.config = config
        self.model_version = model_version

    def project(
        self,
        *,
        season_code: str,
        start_gameweek: int,
        horizon_gameweeks: int = 8,
        overrides: tuple[ProjectionOverride, ...] = (),
        team_overrides: tuple[TeamStrengthOverride, ...] = (),
        generated_at: datetime | None = None,
        observation_mode: str = "latest_available",
        use_availability: bool = True,
        fixture_as_of: datetime | None = None,
        fixture_max_ingestion_run_id: int | None = None,
        persist: bool = True,
    ) -> ProjectionResult:
        if horizon_gameweeks <= 0:
            raise ValueError("Projection horizon must be positive")
        generated = generated_at or datetime.now(UTC)
        if generated.tzinfo is None:
            raise ValueError("Projection generation time must be timezone-aware")
        if fixture_as_of is not None and fixture_as_of.tzinfo is None:
            raise ValueError("Fixture cutoff time must be timezone-aware")

        season = self.database.connection.execute(
            "SELECT id FROM seasons WHERE code = ?", (season_code,)
        ).fetchone()
        if season is None:
            raise ValueError(f"Season {season_code!r} is not available")
        players = self._players(
            season_code,
            start_gameweek,
            observation_mode=observation_mode,
        )
        fixtures = self._fixtures(
            season_code,
            start_gameweek,
            horizon_gameweeks,
            as_of=fixture_as_of,
            maximum_ingestion_run_id=fixture_max_ingestion_run_id,
        )
        strengths = self._team_strengths(
            season_code,
            start_gameweek,
            team_overrides,
            as_of=fixture_as_of,
            maximum_ingestion_run_id=fixture_max_ingestion_run_id,
        )
        override_lookup = {
            (override.source_player_id, override.gameweek_number): override
            for override in overrides
        }
        projections = tuple(
            projection
            for player in players
            for projection in self._project_player(
                player,
                fixtures,
                strengths,
                start_gameweek,
                horizon_gameweeks,
                override_lookup,
                use_availability,
            )
        )
        run_id = (
            self._persist(
                season_id=int(season["id"]),
                generated_at=generated,
                start_gameweek=start_gameweek,
                horizon_gameweeks=horizon_gameweeks,
                projections=projections,
                strengths=strengths,
                observation_mode=observation_mode,
            )
            if persist
            else None
        )
        return ProjectionResult(
            projection_run_id=run_id,
            model_version=self.model_version,
            generated_at=generated,
            start_gameweek=start_gameweek,
            horizon_gameweeks=horizon_gameweeks,
            projections=projections,
            team_strengths=strengths,
            model_config=self.config,
        )

    def _players(
        self,
        season_code: str,
        start_gameweek: int,
        *,
        observation_mode: str,
    ) -> list[dict[str, Any]]:
        observation_filter = {
            "latest_available": "1 = 1",
            "latest_pre_deadline": (
                "observations.observation_kind = 'live_pre_deadline' "
                "AND observations.timing_quality = 'exact' "
                "AND datetime(observations.observed_at) "
                "< datetime(gameweeks.deadline_time)"
            ),
            "pre_deadline_only": (
                "observations.observation_kind = 'live_pre_deadline' "
                "AND observations.timing_quality = 'exact' "
                "AND datetime(observations.observed_at) "
                "< datetime(gameweeks.deadline_time)"
            ),
            "performance_only": "1 = 1",
        }.get(observation_mode)
        if observation_filter is None:
            raise ValueError(f"Unknown projection observation mode {observation_mode!r}")
        rows = self.database.connection.execute(
            f"""
            WITH ranked_observations AS (
                SELECT observations.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY observations.player_season_id
                           ORDER BY
                               gameweeks.number DESC,
                               CASE observations.timing_quality
                                   WHEN 'exact' THEN 0
                                   WHEN 'date_only' THEN 1
                                   ELSE 2
                               END,
                               observations.observed_at DESC,
                               observations.observed_on DESC,
                               observations.id DESC
                       ) AS observation_rank
                FROM player_gameweek_observations observations
                JOIN gameweeks ON gameweeks.id = observations.gameweek_id
                JOIN seasons ON seasons.id = gameweeks.season_id
                WHERE seasons.code = ? AND gameweeks.number <= ?
                  AND {observation_filter}
            ),
            career AS (
                SELECT current_ps.id AS current_player_season_id,
                       COUNT(stats.id) AS matches,
                       COALESCE(SUM(stats.minutes), 0) AS minutes,
                       COALESCE(SUM(stats.goals), 0) AS goals,
                       COALESCE(SUM(stats.assists), 0) AS assists,
                       COALESCE(SUM(stats.clean_sheet), 0) AS clean_sheets,
                       COALESCE(SUM(stats.saves), 0) AS saves,
                       COALESCE(SUM(stats.bonus), 0) AS bonus,
                       COALESCE(SUM(stats.defensive_contributions), 0)
                           AS defensive_contributions,
                       COALESCE(SUM(stats.yellow_cards), 0) AS yellow_cards,
                       COALESCE(SUM(stats.red_cards), 0) AS red_cards,
                       COALESCE(SUM(stats.own_goals), 0) AS own_goals
                FROM player_seasons current_ps
                JOIN player_seasons history_ps
                  ON history_ps.player_id = current_ps.player_id
                LEFT JOIN player_fixture_stats stats
                  ON stats.player_season_id = history_ps.id
                 AND stats.fixture_id IN (
                     SELECT historical_fixtures.id
                     FROM fixtures historical_fixtures
                     JOIN seasons historical_seasons
                       ON historical_seasons.id = historical_fixtures.season_id
                     LEFT JOIN gameweeks historical_gameweeks
                       ON historical_gameweeks.id = historical_fixtures.gameweek_id
                     WHERE historical_seasons.code < ?
                        OR (
                            historical_seasons.code = ?
                            AND historical_gameweeks.number < ?
                        )
                 )
                GROUP BY current_ps.id
            )
            SELECT ps.id AS player_season_id, ps.source_player_id,
                   players.web_name, ps.position, teams.id AS team_id,
                   teams.source_team_id, teams.short_name AS team_short_name,
                   observations.price_tenths, observations.status,
                   observations.chance_of_playing_next_round,
                   career.matches, career.minutes, career.goals, career.assists,
                   career.clean_sheets, career.saves, career.bonus,
                   career.defensive_contributions, career.yellow_cards,
                   career.red_cards, career.own_goals
            FROM player_seasons ps
            JOIN seasons ON seasons.id = ps.season_id
            JOIN players ON players.id = ps.player_id
            JOIN ranked_observations observations
              ON observations.player_season_id = ps.id
             AND observations.observation_rank = 1
            JOIN teams ON teams.id = COALESCE(observations.team_id, ps.team_id)
            JOIN career ON career.current_player_season_id = ps.id
            WHERE seasons.code = ?
              AND ps.identifier_namespace = 'official-fpl'
            ORDER BY ps.source_player_id
            """,
            (
                season_code,
                start_gameweek,
                season_code,
                season_code,
                start_gameweek,
                season_code,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def _fixtures(
        self,
        season_code: str,
        start_gameweek: int,
        horizon: int,
        *,
        as_of: datetime | None = None,
        maximum_ingestion_run_id: int | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        if as_of is None:
            rows = self.database.connection.execute(
                """
                SELECT gameweeks.number AS gameweek_number,
                       home.id AS home_team_id, away.id AS away_team_id
                FROM fixtures
                JOIN seasons ON seasons.id = fixtures.season_id
                JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                JOIN teams home ON home.id = fixtures.home_team_id
                JOIN teams away ON away.id = fixtures.away_team_id
                WHERE seasons.code = ?
                  AND gameweeks.number BETWEEN ? AND ?
                ORDER BY gameweeks.number, fixtures.kickoff_time, fixtures.id
                """,
                (season_code, start_gameweek, start_gameweek + horizon - 1),
            ).fetchall()
        else:
            rows = self.database.connection.execute(
                """
                WITH ranked_observations AS (
                    SELECT observations.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY observations.fixture_id
                               ORDER BY datetime(ingestion_runs.retrieved_at) DESC,
                                        ingestion_runs.id DESC,
                                        observations.id DESC
                           ) AS observation_rank
                    FROM fixture_observations observations
                    JOIN ingestion_runs
                      ON ingestion_runs.id = observations.provenance_run_id
                    JOIN fixtures
                      ON fixtures.id = observations.fixture_id
                    JOIN seasons ON seasons.id = fixtures.season_id
                    WHERE seasons.code = ?
                      AND ingestion_runs.status = 'completed'
                      AND datetime(ingestion_runs.retrieved_at) <= datetime(?)
                      AND (? IS NULL OR ingestion_runs.id <= ?)
                )
                SELECT gameweeks.number AS gameweek_number,
                       home.id AS home_team_id, away.id AS away_team_id
                FROM ranked_observations observations
                JOIN fixtures ON fixtures.id = observations.fixture_id
                JOIN gameweeks ON gameweeks.id = observations.gameweek_id
                JOIN teams home ON home.id = fixtures.home_team_id
                JOIN teams away ON away.id = fixtures.away_team_id
                WHERE observations.observation_rank = 1
                  AND gameweeks.number BETWEEN ? AND ?
                ORDER BY gameweeks.number, observations.kickoff_time,
                         observations.fixture_id
                """,
                (
                    season_code,
                    as_of.astimezone(UTC).isoformat(),
                    maximum_ingestion_run_id,
                    maximum_ingestion_run_id,
                    start_gameweek,
                    start_gameweek + horizon - 1,
                ),
            ).fetchall()
        result = {gameweek: [] for gameweek in range(start_gameweek, start_gameweek + horizon)}
        for row in rows:
            result[int(row["gameweek_number"])].append(dict(row))
        return result

    def _team_strengths(
        self,
        season_code: str,
        start_gameweek: int,
        overrides: tuple[TeamStrengthOverride, ...],
        *,
        as_of: datetime | None = None,
        maximum_ingestion_run_id: int | None = None,
    ) -> dict[str, dict[str, float]]:
        if as_of is None:
            aggregate = self.database.connection.execute(
                """
                WITH results AS (
                    SELECT home_team_id AS team_id, home_score AS goals_for,
                           away_score AS goals_against
                    FROM fixtures
                    JOIN seasons ON seasons.id = fixtures.season_id
                    JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                    WHERE seasons.code = ? AND fixtures.finished = 1
                      AND gameweeks.number < ?
                      AND home_score IS NOT NULL AND away_score IS NOT NULL
                    UNION ALL
                    SELECT away_team_id, away_score, home_score
                    FROM fixtures
                    JOIN seasons ON seasons.id = fixtures.season_id
                    JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                    WHERE seasons.code = ? AND fixtures.finished = 1
                      AND gameweeks.number < ?
                      AND home_score IS NOT NULL AND away_score IS NOT NULL
                )
                SELECT teams.id AS team_id, teams.source_team_id,
                       COUNT(results.team_id) AS matches,
                       COALESCE(SUM(results.goals_for), 0) AS goals_for,
                       COALESCE(SUM(results.goals_against), 0) AS goals_against
                FROM teams
                JOIN seasons ON seasons.id = teams.season_id
                LEFT JOIN results ON results.team_id = teams.id
                WHERE seasons.code = ?
                GROUP BY teams.id
                """,
                (
                    season_code,
                    start_gameweek,
                    season_code,
                    start_gameweek,
                    season_code,
                ),
            ).fetchall()
        else:
            cutoff = as_of.astimezone(UTC).isoformat()
            aggregate = self.database.connection.execute(
                """
                WITH ranked_observations AS (
                    SELECT observations.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY observations.fixture_id
                               ORDER BY datetime(ingestion_runs.retrieved_at) DESC,
                                        ingestion_runs.id DESC,
                                        observations.id DESC
                           ) AS observation_rank
                    FROM fixture_observations observations
                    JOIN ingestion_runs
                      ON ingestion_runs.id = observations.provenance_run_id
                    JOIN fixtures
                      ON fixtures.id = observations.fixture_id
                    JOIN seasons ON seasons.id = fixtures.season_id
                    WHERE seasons.code = ?
                      AND ingestion_runs.status = 'completed'
                      AND datetime(ingestion_runs.retrieved_at) <= datetime(?)
                      AND (? IS NULL OR ingestion_runs.id <= ?)
                ),
                results AS (
                    SELECT fixtures.home_team_id AS team_id,
                           observations.home_score AS goals_for,
                           observations.away_score AS goals_against
                    FROM ranked_observations observations
                    JOIN fixtures ON fixtures.id = observations.fixture_id
                    JOIN gameweeks ON gameweeks.id = observations.gameweek_id
                    WHERE observations.observation_rank = 1
                      AND observations.finished = 1
                      AND gameweeks.number < ?
                      AND observations.home_score IS NOT NULL
                      AND observations.away_score IS NOT NULL
                    UNION ALL
                    SELECT fixtures.away_team_id, observations.away_score,
                           observations.home_score
                    FROM ranked_observations observations
                    JOIN fixtures ON fixtures.id = observations.fixture_id
                    JOIN gameweeks ON gameweeks.id = observations.gameweek_id
                    WHERE observations.observation_rank = 1
                      AND observations.finished = 1
                      AND gameweeks.number < ?
                      AND observations.home_score IS NOT NULL
                      AND observations.away_score IS NOT NULL
                )
                SELECT teams.id AS team_id, teams.source_team_id,
                       COUNT(results.team_id) AS matches,
                       COALESCE(SUM(results.goals_for), 0) AS goals_for,
                       COALESCE(SUM(results.goals_against), 0) AS goals_against
                FROM teams
                JOIN seasons ON seasons.id = teams.season_id
                LEFT JOIN results ON results.team_id = teams.id
                WHERE seasons.code = ?
                GROUP BY teams.id
                """,
                (
                    season_code,
                    cutoff,
                    maximum_ingestion_run_id,
                    maximum_ingestion_run_id,
                    start_gameweek,
                    start_gameweek,
                    season_code,
                ),
            ).fetchall()
        total_matches = sum(int(row["matches"]) for row in aggregate)
        total_goals = sum(int(row["goals_for"]) for row in aggregate)
        league_average = total_goals / total_matches if total_matches else 1.4
        prior_matches = self.config.team_prior_matches
        result: dict[str, dict[str, float]] = {}
        for row in aggregate:
            matches = int(row["matches"])
            attack_rate = (
                float(row["goals_for"]) + prior_matches * league_average
            ) / (matches + prior_matches)
            defence_rate = (
                float(row["goals_against"]) + prior_matches * league_average
            ) / (matches + prior_matches)
            result[str(row["team_id"])] = {
                "attack": _clamp(
                    attack_rate / league_average,
                    self.config.minimum_team_multiplier,
                    self.config.maximum_team_multiplier,
                ),
                "defence": _clamp(
                    defence_rate / league_average,
                    self.config.minimum_team_multiplier,
                    self.config.maximum_team_multiplier,
                ),
                "matches": float(matches),
                "league_average_goals": league_average,
            }
        by_source = {
            str(row["source_team_id"]): str(row["team_id"]) for row in aggregate
        }
        for override in overrides:
            team_id = by_source.get(override.source_team_id)
            if team_id is None:
                raise ValueError(
                    f"Unknown team override {override.source_team_id!r}"
                )
            result[team_id]["attack"] = override.attack_multiplier
            result[team_id]["defence"] = override.defence_susceptibility
            result[team_id]["overridden"] = 1.0
        return result

    def _project_player(
        self,
        player: dict[str, Any],
        fixtures: dict[int, list[dict[str, Any]]],
        strengths: dict[str, dict[str, float]],
        start_gameweek: int,
        horizon: int,
        overrides: dict[tuple[str, int], ProjectionOverride],
        use_availability: bool,
    ) -> tuple[PlayerGameweekProjection, ...]:
        position = Position(player["position"])
        prior = POSITION_PRIORS[position]
        sample_minutes = float(player["minutes"])
        sample_matches = int(player["matches"])
        prior_minutes = self.config.player_rate_prior_minutes
        prior_matches = self.config.minutes_prior_matches
        minutes_per_fixture = (
            float(player["minutes"]) + prior["minutes"] * prior_matches
        ) / (sample_matches + prior_matches)
        availability = (
            _availability_multiplier(
                player["status"], player["chance_of_playing_next_round"]
            )
            if use_availability
            else 1.0
        )
        minutes_per_fixture = _clamp(minutes_per_fixture * availability, 0.0, 90.0)
        rate_names = (
            "goals",
            "assists",
            "clean_sheets",
            "saves",
            "bonus",
            "defensive_contributions",
            "yellow_cards",
            "red_cards",
            "own_goals",
        )
        rates = {
            name: (
                float(player[name]) * 90.0
                + prior[name] * prior_minutes
            )
            / (sample_minutes + prior_minutes)
            for name in rate_names
        }
        projections = []
        for offset, gameweek in enumerate(range(start_gameweek, start_gameweek + horizon)):
            player_fixtures = [
                fixture
                for fixture in fixtures[gameweek]
                if player["team_id"]
                in (fixture["home_team_id"], fixture["away_team_id"])
            ]
            fixture_count = len(player_fixtures)
            override = overrides.get((player["source_player_id"], gameweek))
            expected_minutes = minutes_per_fixture * fixture_count
            if override is not None:
                expected_minutes = _clamp(
                    override.expected_minutes, 0.0, 90.0 * fixture_count
                )
            per_fixture_minutes = (
                0.0 if fixture_count == 0 else expected_minutes / fixture_count
            )
            components = {
                "appearance": 0.0,
                "goal": 0.0,
                "assist": 0.0,
                "clean": 0.0,
                "save": 0.0,
                "defensive": 0.0,
                "bonus": 0.0,
                "deduction": 0.0,
            }
            fixture_notes = []
            for fixture in player_fixtures:
                is_home = player["team_id"] == fixture["home_team_id"]
                opponent_id = str(
                    fixture["away_team_id"] if is_home else fixture["home_team_id"]
                )
                team_strength = strengths[str(player["team_id"])]
                opponent_strength = strengths[opponent_id]
                venue_attack = (
                    self.config.home_attack_multiplier
                    if is_home
                    else self.config.away_attack_multiplier
                )
                scoring_factor = (
                    team_strength["attack"]
                    * opponent_strength["defence"]
                    * venue_attack
                )
                opponent_lambda = (
                    team_strength["league_average_goals"]
                    * opponent_strength["attack"]
                    * team_strength["defence"]
                    * (
                        self.config.away_attack_multiplier
                        if is_home
                        else self.config.home_attack_multiplier
                    )
                )
                minute_factor = per_fixture_minutes / 90.0
                sixty_factor = _clamp(per_fixture_minutes / 60.0, 0.0, 1.0)
                components["appearance"] += min(per_fixture_minutes / 30.0, 2.0)
                components["goal"] += (
                    rates["goals"]
                    * minute_factor
                    * scoring_factor
                    * self.rules.scoring.goals[position.value]
                )
                components["assist"] += (
                    rates["assists"]
                    * minute_factor
                    * scoring_factor
                    * self.rules.scoring.assists
                )
                components["clean"] += (
                    math.exp(-opponent_lambda)
                    * sixty_factor
                    * self.rules.scoring.clean_sheets[position.value]
                )
                components["save"] += (
                    rates["saves"]
                    * minute_factor
                    / self.rules.scoring.saves_per_point
                )
                components["defensive"] += (
                    rates["defensive_contributions"] * minute_factor
                )
                components["bonus"] += rates["bonus"] * minute_factor
                components["deduction"] -= (
                    rates["yellow_cards"] * minute_factor
                    + 3.0 * rates["red_cards"] * minute_factor
                    + 2.0 * rates["own_goals"] * minute_factor
                )
                if position in (Position.GK, Position.DEF):
                    components["deduction"] -= (
                        opponent_lambda / 2.0
                    ) * sixty_factor
                fixture_notes.append(
                    f"{'home' if is_home else 'away'} fixture factor "
                    f"{scoring_factor:.2f}"
                )
            expected_points = sum(components.values())
            uncertainty = (
                1.25
                + 3.5 / math.sqrt(sample_matches + 1)
                + offset * 0.12
                + (0.75 if fixture_count == 0 else 0.0)
            )
            assumptions = (
                f"Rates shrunk by {prior_minutes:.0f} prior minutes",
                f"Expected minutes per fixture {minutes_per_fixture:.1f}",
                f"Availability multiplier {availability:.2f}",
                *fixture_notes,
            )
            projections.append(
                PlayerGameweekProjection(
                    source_player_id=player["source_player_id"],
                    web_name=player["web_name"],
                    team_short_name=player["team_short_name"],
                    position=position,
                    gameweek_number=gameweek,
                    fixture_count=fixture_count,
                    expected_minutes=round(expected_minutes, 2),
                    appearance_points=round(components["appearance"], 3),
                    goal_points=round(components["goal"], 3),
                    assist_points=round(components["assist"], 3),
                    clean_sheet_points=round(components["clean"], 3),
                    save_points=round(components["save"], 3),
                    defensive_contribution_points=round(
                        components["defensive"], 3
                    ),
                    bonus_points=round(components["bonus"], 3),
                    deduction_points=round(components["deduction"], 3),
                    expected_points=round(expected_points, 3),
                    uncertainty=round(uncertainty, 3),
                    assumptions=assumptions,
                    override_rationale=None if override is None else override.rationale,
                )
            )
        return tuple(projections)

    def _persist(
        self,
        *,
        season_id: int,
        generated_at: datetime,
        start_gameweek: int,
        horizon_gameweeks: int,
        projections: tuple[PlayerGameweekProjection, ...],
        strengths: dict[str, dict[str, float]],
        observation_mode: str,
    ) -> int:
        source_run = self.database.connection.execute(
            """
            SELECT id FROM ingestion_runs WHERE status = 'completed'
            ORDER BY retrieved_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        assumptions = {
            "position_priors": POSITION_PRIORS,
            "team_strengths": strengths,
            "model_config": self.config.__dict__,
        }
        with self.database.transaction():
            cursor = self.database.connection.execute(
                """
                INSERT INTO projection_runs (
                    season_id, generated_at, start_gameweek, horizon_gameweeks,
                    model_version, observation_mode, assumptions_json,
                    source_ingestion_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    season_id,
                    generated_at.astimezone(UTC).isoformat(),
                    start_gameweek,
                    horizon_gameweeks,
                    self.model_version,
                    observation_mode,
                    json.dumps(assumptions, sort_keys=True, default=str),
                    None if source_run is None else source_run["id"],
                ),
            )
            run_id = int(cursor.fetchone()[0])
            player_season_ids = {
                row["source_player_id"]: row["id"]
                for row in self.database.connection.execute(
                    """
                    SELECT id, source_player_id FROM player_seasons
                    WHERE season_id = ? AND identifier_namespace = 'official-fpl'
                    """,
                    (season_id,),
                )
            }
            self.database.connection.executemany(
                """
                INSERT INTO player_gameweek_projections (
                    projection_run_id, player_season_id, gameweek_number,
                    expected_minutes, appearance_points, goal_points,
                    assist_points, clean_sheet_points, save_points,
                    defensive_contribution_points, bonus_points,
                    deduction_points, expected_points, uncertainty,
                    assumptions_json, override_rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        player_season_ids[projection.source_player_id],
                        projection.gameweek_number,
                        projection.expected_minutes,
                        projection.appearance_points,
                        projection.goal_points,
                        projection.assist_points,
                        projection.clean_sheet_points,
                        projection.save_points,
                        projection.defensive_contribution_points,
                        projection.bonus_points,
                        projection.deduction_points,
                        projection.expected_points,
                        projection.uncertainty,
                        json.dumps(projection.assumptions),
                        projection.override_rationale,
                    )
                    for projection in projections
                ),
            )
        return run_id


def projection_totals(
    projections: tuple[PlayerGameweekProjection, ...],
) -> list[dict[str, Any]]:
    """Aggregate a run over its configured horizon for explorers/optimisers."""

    totals: dict[str, dict[str, Any]] = {}
    for projection in projections:
        row = totals.setdefault(
            projection.source_player_id,
            {
                "source_player_id": projection.source_player_id,
                "web_name": projection.web_name,
                "team_short_name": projection.team_short_name,
                "position": projection.position.value,
                "expected_minutes": 0.0,
                "expected_points": 0.0,
                "uncertainty": 0.0,
            },
        )
        row["expected_minutes"] += projection.expected_minutes
        row["expected_points"] += projection.expected_points
        row["uncertainty"] = math.sqrt(
            row["uncertainty"] ** 2 + projection.uncertainty**2
        )
    for row in totals.values():
        row["expected_minutes"] = round(row["expected_minutes"], 1)
        row["expected_points"] = round(row["expected_points"], 2)
        row["uncertainty"] = round(row["uncertainty"], 2)
    return sorted(
        totals.values(), key=lambda row: row["expected_points"], reverse=True
    )


def _availability_multiplier(status: str | None, chance: int | None) -> float:
    if chance is not None:
        return _clamp(chance / 100.0, 0.0, 1.0)
    return {
        "a": 1.0,
        "d": 0.75,
        "i": 0.0,
        "n": 0.0,
        "s": 0.0,
        "u": 0.0,
    }.get(status or "a", 1.0)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
