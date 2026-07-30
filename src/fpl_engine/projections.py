"""Transparent rates-based player projections."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .domain import Position
from .history.database import HistoricalDatabase

MODEL_VERSION = "rates-rules-corrected-v4"
TUNED_V3_MODEL_VERSION = "rates-two-stage-v3"
BASELINE_V2_MODEL_VERSION = "rates-two-stage-v2"
LEGACY_MODEL_VERSION = "rates-baseline-v1"
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
        "penalties_saved": 0.01,
        "penalties_missed": 0.0,
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
        "penalties_saved": 0.0,
        "penalties_missed": 0.002,
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
        "penalties_saved": 0.0,
        "penalties_missed": 0.01,
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
        "penalties_saved": 0.0,
        "penalties_missed": 0.015,
    },
}

DEFENSIVE_CONTRIBUTION_COUNT_PRIORS: dict[Position, float] = {
    Position.GK: 0.0,
    Position.DEF: 7.0,
    Position.MID: 5.0,
    Position.FWD: 3.0,
}

MINUTES_PRIORS: dict[Position, dict[str, float]] = {
    Position.GK: {
        "conditional_minutes": 88.0,
        "sixty_probability_given_appearance": 0.96,
    },
    Position.DEF: {
        "conditional_minutes": 72.0,
        "sixty_probability_given_appearance": 0.72,
    },
    Position.MID: {
        "conditional_minutes": 69.0,
        "sixty_probability_given_appearance": 0.68,
    },
    Position.FWD: {
        "conditional_minutes": 67.0,
        "sixty_probability_given_appearance": 0.64,
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
    minutes_model: str = "two_stage"
    recent_gameweeks: int = 3
    recent_evidence_weight: float = 4.0
    appearance_prior_matches: float = 1.0
    appearance_prior_probability: float = 0.40
    conditional_minutes_prior_appearances: float = 2.0
    team_minutes_per_fixture: float = 990.0
    enforce_team_minutes: bool = True
    minutes_allocation: str = "team_total"
    scoring_recent_evidence_weight: float = 1.0
    scoring_event_source: str = "actual"
    defensive_contribution_model: str = "legacy_linear"
    include_penalty_events: bool = False

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
        if self.minutes_model not in {"legacy", "two_stage"}:
            raise ValueError("Minutes model must be 'legacy' or 'two_stage'")
        if self.recent_gameweeks <= 0:
            raise ValueError("Recent Gameweeks must be positive")
        if self.recent_evidence_weight < 1:
            raise ValueError("Recent evidence weight cannot be below one")
        if self.appearance_prior_matches <= 0:
            raise ValueError("Appearance prior matches must be positive")
        if not 0 < self.appearance_prior_probability < 1:
            raise ValueError("Appearance prior probability must be between zero and one")
        if self.conditional_minutes_prior_appearances <= 0:
            raise ValueError(
                "Conditional-minutes prior appearances must be positive"
            )
        if self.team_minutes_per_fixture <= 0:
            raise ValueError("Team minutes per fixture must be positive")
        if self.minutes_allocation not in {
            "team_total",
            "position_aware",
        }:
            raise ValueError(
                "Minutes allocation must be 'team_total' or "
                "'position_aware'"
            )
        if self.scoring_recent_evidence_weight < 1:
            raise ValueError(
                "Recent scoring evidence weight cannot be below one"
            )
        if self.scoring_event_source not in {
            "actual",
            "expected_with_actual_fallback",
        }:
            raise ValueError(
                "Scoring event source must be 'actual' or "
                "'expected_with_actual_fallback'"
            )
        if self.defensive_contribution_model not in {
            "legacy_linear",
            "threshold_poisson",
        }:
            raise ValueError(
                "Defensive contribution model must be 'legacy_linear' "
                "or 'threshold_poisson'"
            )


BASELINE_V2_MODEL_CONFIG = ProjectionModelConfig()
TUNED_V3_MODEL_CONFIG = ProjectionModelConfig(
    player_rate_prior_minutes=1776.650037050099,
    minutes_prior_matches=6.0,
    team_prior_matches=11.870184562035677,
    home_attack_multiplier=1.0680722197944925,
    away_attack_multiplier=0.8512934695622035,
    minimum_team_multiplier=0.6,
    maximum_team_multiplier=1.5,
    minutes_model="two_stage",
    recent_gameweeks=4,
    recent_evidence_weight=1.845760710001814,
    appearance_prior_matches=3.2516466478759654,
    appearance_prior_probability=0.4044943812940328,
    conditional_minutes_prior_appearances=1.248676119370052,
    team_minutes_per_fixture=990.0,
    enforce_team_minutes=True,
)
CORRECTED_V4_MODEL_CONFIG = replace(
    TUNED_V3_MODEL_CONFIG,
    defensive_contribution_model="threshold_poisson",
    include_penalty_events=True,
)
EXPECTED_EVENTS_V4_MODEL_CONFIG = replace(
    CORRECTED_V4_MODEL_CONFIG,
    scoring_event_source="expected_with_actual_fallback",
)
DEFAULT_MODEL_CONFIG = CORRECTED_V4_MODEL_CONFIG

ROBUST_V4_MODEL_CONFIG = ProjectionModelConfig(
    player_rate_prior_minutes=2761.331925036367,
    minutes_prior_matches=6.0,
    team_prior_matches=19.355921510454394,
    home_attack_multiplier=1.1440792800906638,
    away_attack_multiplier=0.9884699819036712,
    minimum_team_multiplier=0.6,
    maximum_team_multiplier=1.5,
    minutes_model="two_stage",
    recent_gameweeks=6,
    recent_evidence_weight=3.4024319691135294,
    appearance_prior_matches=3.2594648781387656,
    appearance_prior_probability=0.5517557096047864,
    conditional_minutes_prior_appearances=1.352415949352844,
    team_minutes_per_fixture=990.0,
    enforce_team_minutes=True,
)


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
    appearance_probability: float
    sixty_probability: float
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
        source_ingestion_run_id = self._resolve_source_ingestion_run_id(
            generated,
            fixture_max_ingestion_run_id,
        )
        players = self._players(
            season_code,
            start_gameweek,
            observation_mode=observation_mode,
            maximum_ingestion_run_id=source_ingestion_run_id,
        )
        self._prepare_minutes(players, use_availability=use_availability)
        fixtures = self._fixtures(
            season_code,
            start_gameweek,
            horizon_gameweeks,
            as_of=fixture_as_of,
            maximum_ingestion_run_id=source_ingestion_run_id,
        )
        strengths = self._team_strengths(
            season_code,
            start_gameweek,
            team_overrides,
            as_of=fixture_as_of,
            maximum_ingestion_run_id=source_ingestion_run_id,
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
                source_ingestion_run_id=source_ingestion_run_id,
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

    def _resolve_source_ingestion_run_id(
        self,
        generated_at: datetime,
        requested_run_id: int | None,
    ) -> int:
        if requested_run_id is not None:
            row = self.database.connection.execute(
                """
                SELECT id FROM ingestion_runs
                WHERE id = ? AND status = 'completed'
                """,
                (requested_run_id,),
            ).fetchone()
        else:
            row = self.database.connection.execute(
                """
                SELECT id FROM ingestion_runs
                WHERE status = 'completed'
                  AND datetime(retrieved_at) <= datetime(?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (generated_at.astimezone(UTC).isoformat(),),
            ).fetchone()
        if row is None:
            qualifier = (
                f" {requested_run_id}"
                if requested_run_id is not None
                else f" as of {generated_at.astimezone(UTC).isoformat()}"
            )
            raise ValueError(
                f"No completed projection source ingestion run{qualifier}"
            )
        return int(row["id"])

    def _players(
        self,
        season_code: str,
        start_gameweek: int,
        *,
        observation_mode: str,
        maximum_ingestion_run_id: int | None,
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
                JOIN ingestion_runs observation_runs
                  ON observation_runs.id = observations.provenance_run_id
                JOIN gameweeks ON gameweeks.id = observations.gameweek_id
                JOIN seasons ON seasons.id = gameweeks.season_id
                WHERE seasons.code = ? AND gameweeks.number <= ?
                  AND (
                      ? IS NULL
                      OR observation_runs.id <= ?
                  )
                  AND {observation_filter}
            ),
            career AS (
                SELECT current_ps.id AS current_player_season_id,
                       COUNT(stats.id) AS matches,
                       COALESCE(SUM(stats.minutes > 0), 0) AS appearances,
                       COALESCE(SUM(stats.minutes >= 60), 0)
                           AS sixty_appearances,
                       COALESCE(SUM(stats.minutes), 0) AS minutes,
                       COALESCE(SUM(stats.goals), 0) AS goals,
                       COALESCE(SUM(stats.assists), 0) AS assists,
                       COALESCE(
                           SUM(COALESCE(stats.expected_goals, stats.goals)),
                           0
                       ) AS expected_goals,
                       COALESCE(
                           SUM(COALESCE(stats.expected_assists, stats.assists)),
                           0
                       ) AS expected_assists,
                       COALESCE(SUM(stats.clean_sheet), 0) AS clean_sheets,
                       COALESCE(SUM(stats.saves), 0) AS saves,
                       COALESCE(SUM(stats.bonus), 0) AS bonus,
                       COALESCE(SUM(stats.defensive_contributions), 0)
                           AS defensive_contributions,
                       COALESCE(SUM(stats.yellow_cards), 0) AS yellow_cards,
                       COALESCE(SUM(stats.red_cards), 0) AS red_cards,
                       COALESCE(SUM(stats.own_goals), 0) AS own_goals,
                       COALESCE(SUM(stats.penalties_saved), 0)
                           AS penalties_saved,
                       COALESCE(SUM(stats.penalties_missed), 0)
                           AS penalties_missed
                FROM player_seasons current_ps
                JOIN player_seasons history_ps
                  ON history_ps.player_id = current_ps.player_id
                 AND history_ps.identifier_namespace =
                     current_ps.identifier_namespace
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
            ),
            recent AS (
                SELECT current_ps.id AS current_player_season_id,
                       COUNT(stats.id) AS recent_matches,
                       COALESCE(SUM(stats.minutes > 0), 0)
                           AS recent_appearances,
                       COALESCE(SUM(stats.minutes >= 60), 0)
                           AS recent_sixty_appearances,
                       COALESCE(SUM(stats.minutes), 0) AS recent_minutes,
                       COALESCE(SUM(stats.goals), 0) AS recent_goals,
                       COALESCE(SUM(stats.assists), 0) AS recent_assists,
                       COALESCE(
                           SUM(COALESCE(stats.expected_goals, stats.goals)),
                           0
                       ) AS recent_expected_goals,
                       COALESCE(
                           SUM(COALESCE(stats.expected_assists, stats.assists)),
                           0
                       ) AS recent_expected_assists,
                       COALESCE(SUM(stats.clean_sheet), 0)
                           AS recent_clean_sheets,
                       COALESCE(SUM(stats.saves), 0) AS recent_saves,
                       COALESCE(SUM(stats.bonus), 0) AS recent_bonus,
                       COALESCE(SUM(stats.defensive_contributions), 0)
                           AS recent_defensive_contributions,
                       COALESCE(SUM(stats.yellow_cards), 0)
                           AS recent_yellow_cards,
                       COALESCE(SUM(stats.red_cards), 0)
                           AS recent_red_cards,
                       COALESCE(SUM(stats.own_goals), 0)
                           AS recent_own_goals,
                       COALESCE(SUM(stats.penalties_saved), 0)
                           AS recent_penalties_saved,
                       COALESCE(SUM(stats.penalties_missed), 0)
                           AS recent_penalties_missed
                FROM player_seasons current_ps
                JOIN seasons current_seasons
                  ON current_seasons.id = current_ps.season_id
                LEFT JOIN player_fixture_stats stats
                  ON stats.player_season_id = current_ps.id
                 AND stats.fixture_id IN (
                     SELECT recent_fixtures.id
                     FROM fixtures recent_fixtures
                     JOIN gameweeks recent_gameweeks
                       ON recent_gameweeks.id = recent_fixtures.gameweek_id
                     WHERE recent_fixtures.season_id = current_ps.season_id
                       AND recent_gameweeks.number BETWEEN ? AND ?
                 )
                WHERE current_seasons.code = ?
                GROUP BY current_ps.id
            )
            SELECT ps.id AS player_season_id, ps.source_player_id,
                   players.web_name, ps.position, teams.id AS team_id,
                   teams.source_team_id, teams.short_name AS team_short_name,
                   observations.price_tenths, observations.status,
                   observations.chance_of_playing_next_round,
                   career.matches, career.appearances,
                   career.sixty_appearances, career.minutes,
                   recent.recent_matches, recent.recent_appearances,
                   recent.recent_sixty_appearances, recent.recent_minutes,
                   recent.recent_goals, recent.recent_assists,
                   recent.recent_expected_goals,
                   recent.recent_expected_assists,
                   recent.recent_clean_sheets, recent.recent_saves,
                   recent.recent_bonus,
                   recent.recent_defensive_contributions,
                   recent.recent_yellow_cards, recent.recent_red_cards,
                   recent.recent_own_goals,
                   recent.recent_penalties_saved,
                   recent.recent_penalties_missed,
                   career.goals, career.assists,
                   career.expected_goals, career.expected_assists,
                   career.clean_sheets, career.saves, career.bonus,
                   career.defensive_contributions, career.yellow_cards,
                   career.red_cards, career.own_goals,
                   career.penalties_saved, career.penalties_missed
            FROM player_seasons ps
            JOIN seasons ON seasons.id = ps.season_id
            JOIN players ON players.id = ps.player_id
            JOIN ranked_observations observations
              ON observations.player_season_id = ps.id
             AND observations.observation_rank = 1
            JOIN teams ON teams.id = COALESCE(observations.team_id, ps.team_id)
            JOIN career ON career.current_player_season_id = ps.id
            JOIN recent ON recent.current_player_season_id = ps.id
            WHERE seasons.code = ?
              AND ps.identifier_namespace = 'official-fpl'
            ORDER BY ps.source_player_id
            """,
            (
                season_code,
                start_gameweek,
                maximum_ingestion_run_id,
                maximum_ingestion_run_id,
                season_code,
                season_code,
                start_gameweek,
                max(1, start_gameweek - self.config.recent_gameweeks),
                start_gameweek - 1,
                season_code,
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

    def _prepare_minutes(
        self,
        players: list[dict[str, Any]],
        *,
        use_availability: bool,
    ) -> None:
        """Estimate appearance and conditional minutes before team reconciliation."""

        for player in players:
            position = Position(player["position"])
            availability = (
                _availability_multiplier(
                    player["status"],
                    player["chance_of_playing_next_round"],
                )
                if use_availability
                else 1.0
            )
            if self.config.minutes_model == "legacy":
                prior_matches = self.config.minutes_prior_matches
                expected_minutes = (
                    float(player["minutes"])
                    + POSITION_PRIORS[position]["minutes"] * prior_matches
                ) / (int(player["matches"]) + prior_matches)
                expected_minutes = _clamp(
                    expected_minutes * availability, 0.0, 90.0
                )
                conditional_minutes = max(
                    expected_minutes,
                    MINUTES_PRIORS[position]["conditional_minutes"],
                )
                appearance_probability = _clamp(
                    expected_minutes / max(conditional_minutes, 1.0),
                    0.0,
                    1.0,
                )
                sixty_given_appearance = _clamp(
                    MINUTES_PRIORS[position][
                        "sixty_probability_given_appearance"
                    ],
                    0.0,
                    1.0,
                )
            else:
                extra_recent_weight = (
                    self.config.recent_evidence_weight - 1.0
                )
                weighted_matches = (
                    float(player["matches"])
                    + extra_recent_weight
                    * float(player["recent_matches"])
                )
                weighted_appearances = (
                    float(player["appearances"])
                    + extra_recent_weight
                    * float(player["recent_appearances"])
                )
                weighted_sixty = (
                    float(player["sixty_appearances"])
                    + extra_recent_weight
                    * float(player["recent_sixty_appearances"])
                )
                weighted_minutes = (
                    float(player["minutes"])
                    + extra_recent_weight
                    * float(player["recent_minutes"])
                )
                appearance_probability = (
                    weighted_appearances
                    + self.config.appearance_prior_matches
                    * self.config.appearance_prior_probability
                ) / (
                    weighted_matches
                    + self.config.appearance_prior_matches
                )
                appearance_probability = _clamp(
                    appearance_probability * availability, 0.0, 1.0
                )
                conditional_prior = (
                    self.config.conditional_minutes_prior_appearances
                )
                conditional_minutes = (
                    weighted_minutes
                    + conditional_prior
                    * MINUTES_PRIORS[position]["conditional_minutes"]
                ) / (weighted_appearances + conditional_prior)
                conditional_minutes = _clamp(
                    conditional_minutes, 1.0, 90.0
                )
                sixty_given_appearance = (
                    weighted_sixty
                    + conditional_prior
                    * MINUTES_PRIORS[position][
                        "sixty_probability_given_appearance"
                    ]
                ) / (weighted_appearances + conditional_prior)
                sixty_given_appearance = _clamp(
                    sixty_given_appearance, 0.0, 1.0
                )
                expected_minutes = (
                    appearance_probability * conditional_minutes
                )

            player["_availability"] = availability
            player["_conditional_minutes"] = conditional_minutes
            player["_appearance_probability"] = appearance_probability
            player["_sixty_given_appearance"] = sixty_given_appearance
            player["_sixty_probability"] = (
                appearance_probability * sixty_given_appearance
            )
            player["_expected_minutes_per_fixture"] = expected_minutes

        if (
            self.config.minutes_model != "two_stage"
            or not self.config.enforce_team_minutes
        ):
            return

        players_by_team: dict[str, list[dict[str, Any]]] = {}
        for player in players:
            players_by_team.setdefault(str(player["team_id"]), []).append(
                player
            )
        for team_players in players_by_team.values():
            allocation_groups = (
                (
                    (
                        player
                        for player in team_players
                        if player["position"] == Position.GK.value
                    ),
                    90.0,
                ),
                (
                    (
                        player
                        for player in team_players
                        if player["position"] != Position.GK.value
                    ),
                    self.config.team_minutes_per_fixture - 90.0,
                ),
            ) if self.config.minutes_allocation == "position_aware" else (
                (iter(team_players), self.config.team_minutes_per_fixture),
            )
            reconciled: list[tuple[dict[str, Any], float]] = []
            for group, target in allocation_groups:
                group_players = list(group)
                allocations = _allocate_capped_minutes(
                    [
                        float(player["_expected_minutes_per_fixture"])
                        for player in group_players
                    ],
                    target=target,
                    cap=90.0,
                )
                reconciled.extend(
                    zip(group_players, allocations, strict=True)
                )
            for player, expected_minutes in reconciled:
                conditional_minutes = float(
                    player["_conditional_minutes"]
                )
                appearance_probability = _clamp(
                    expected_minutes / max(conditional_minutes, 1.0),
                    0.0,
                    1.0,
                )
                player["_expected_minutes_per_fixture"] = expected_minutes
                player["_appearance_probability"] = appearance_probability
                player["_sixty_probability"] = min(
                    appearance_probability,
                    appearance_probability
                    * float(player["_sixty_given_appearance"]),
                )

    def _project_player(
        self,
        player: dict[str, Any],
        fixtures: dict[int, list[dict[str, Any]]],
        strengths: dict[str, dict[str, float]],
        start_gameweek: int,
        horizon: int,
        overrides: dict[tuple[str, int], ProjectionOverride],
    ) -> tuple[PlayerGameweekProjection, ...]:
        position = Position(player["position"])
        prior = POSITION_PRIORS[position]
        sample_minutes = float(player["minutes"])
        sample_matches = int(player["matches"])
        prior_minutes = self.config.player_rate_prior_minutes
        minutes_per_fixture = float(player["_expected_minutes_per_fixture"])
        appearance_probability = float(player["_appearance_probability"])
        sixty_probability = float(player["_sixty_probability"])
        conditional_minutes = float(player["_conditional_minutes"])
        availability = float(player["_availability"])
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
            "penalties_saved",
            "penalties_missed",
        )
        recent_scoring_extra = (
            self.config.scoring_recent_evidence_weight - 1.0
        )
        scoring_sample_minutes = (
            sample_minutes
            + recent_scoring_extra * float(player["recent_minutes"])
        )
        rate_source_names = {
            "goals": "expected_goals",
            "assists": "expected_assists",
        } if (
            self.config.scoring_event_source
            == "expected_with_actual_fallback"
        ) else {}
        rates = {
            name: (
                (
                    float(player[rate_source_names.get(name, name)])
                    + recent_scoring_extra
                    * float(
                        player[
                            f"recent_{rate_source_names.get(name, name)}"
                        ]
                    )
                )
                * 90.0
                + (
                    DEFENSIVE_CONTRIBUTION_COUNT_PRIORS[position]
                    if (
                        name == "defensive_contributions"
                        and self.config.defensive_contribution_model
                        == "threshold_poisson"
                    )
                    else prior[name]
                )
                * prior_minutes
            )
            / (scoring_sample_minutes + prior_minutes)
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
            fixture_appearance_probability = appearance_probability
            fixture_sixty_probability = sixty_probability
            if override is not None and self.config.minutes_model == "two_stage":
                fixture_appearance_probability = _clamp(
                    per_fixture_minutes / max(conditional_minutes, 1.0),
                    0.0,
                    1.0,
                )
                fixture_sixty_probability = min(
                    fixture_appearance_probability,
                    fixture_appearance_probability
                    * float(player["_sixty_given_appearance"]),
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
                if self.config.minutes_model == "legacy":
                    sixty_factor = _clamp(
                        per_fixture_minutes / 60.0, 0.0, 1.0
                    )
                    components["appearance"] += min(
                        per_fixture_minutes / 30.0, 2.0
                    )
                else:
                    sixty_factor = fixture_sixty_probability
                    components["appearance"] += (
                        fixture_appearance_probability
                        + fixture_sixty_probability
                    )
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
                if (
                    self.config.defensive_contribution_model
                    == "threshold_poisson"
                ):
                    contribution_threshold = (
                        self.rules.scoring
                        .defensive_contribution_thresholds[position.value]
                    )
                    contribution_lambda = (
                        rates["defensive_contributions"]
                        * conditional_minutes
                        / 90.0
                    )
                    components["defensive"] += (
                        fixture_appearance_probability
                        * _poisson_at_least(
                            contribution_lambda,
                            contribution_threshold,
                        )
                        * self.rules.scoring.defensive_contribution_points
                    )
                else:
                    components["defensive"] += (
                        rates["defensive_contributions"] * minute_factor
                    )
                if self.config.include_penalty_events:
                    components["save"] += (
                        rates["penalties_saved"]
                        * minute_factor
                        * self.rules.scoring.penalty_save
                    )
                    components["deduction"] += (
                        rates["penalties_missed"]
                        * minute_factor
                        * self.rules.scoring.penalty_miss
                    )
                components["bonus"] += rates["bonus"] * minute_factor
                components["deduction"] -= (
                    rates["yellow_cards"] * minute_factor
                    + 3.0 * rates["red_cards"] * minute_factor
                    + 2.0 * rates["own_goals"] * minute_factor
                )
                if position in (Position.GK, Position.DEF):
                    if self.config.minutes_model == "two_stage":
                        conceded_lambda = (
                            opponent_lambda * conditional_minutes / 90.0
                        )
                        expected_conceded_pairs = (
                            fixture_appearance_probability
                            * _poisson_expected_complete_pairs(
                                conceded_lambda
                            )
                        )
                    else:
                        expected_conceded_pairs = (
                            _poisson_expected_complete_pairs(
                                opponent_lambda * minute_factor
                            )
                        )
                    components["deduction"] += (
                        expected_conceded_pairs
                        * self.rules.scoring.goals_conceded_per_two[
                            position.value
                        ]
                    )
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
                f"Appearance probability {fixture_appearance_probability:.2f}",
                f"60-minute probability {fixture_sixty_probability:.2f}",
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
                    appearance_probability=round(
                        1.0
                        - (
                            1.0 - fixture_appearance_probability
                        )
                        ** fixture_count,
                        6,
                    ),
                    sixty_probability=round(
                        1.0
                        - (1.0 - fixture_sixty_probability)
                        ** fixture_count,
                        6,
                    ),
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
        source_ingestion_run_id: int | None,
    ) -> int:
        source_run = (
            self.database.connection.execute(
                "SELECT id FROM ingestion_runs WHERE id = ? AND status = 'completed'",
                (source_ingestion_run_id,),
            ).fetchone()
            if source_ingestion_run_id is not None
            else self.database.connection.execute(
                """
                SELECT id FROM ingestion_runs
                WHERE status = 'completed' AND datetime(retrieved_at) <= datetime(?)
                ORDER BY datetime(retrieved_at) DESC, id DESC LIMIT 1
                """,
                (generated_at.astimezone(UTC).isoformat(),),
            ).fetchone()
        )
        if source_ingestion_run_id is not None and source_run is None:
            raise ValueError(
                "Projection source ingestion run is missing or incomplete"
            )
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
                    expected_minutes, appearance_probability,
                    sixty_probability, appearance_points, goal_points,
                    assist_points, clean_sheet_points, save_points,
                    defensive_contribution_points, bonus_points,
                    deduction_points, expected_points, uncertainty,
                    assumptions_json, override_rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        player_season_ids[projection.source_player_id],
                        projection.gameweek_number,
                        projection.expected_minutes,
                        projection.appearance_probability,
                        projection.sixty_probability,
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
        # Horizon errors for one player are persistent and correlated. Adding
        # the disclosed per-Gameweek uncertainty avoids the unjustified
        # independence assumption implicit in root-sum-of-squares.
        row["uncertainty"] += projection.uncertainty
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


def _allocate_capped_minutes(
    raw_values: list[float],
    *,
    target: float,
    cap: float,
) -> list[float]:
    """Scale positive estimates to a team budget without exceeding player caps."""

    if not raw_values:
        return []
    feasible_target = min(target, cap * len(raw_values))
    allocations = [0.0] * len(raw_values)
    active = set(range(len(raw_values)))
    remaining = feasible_target
    while active and remaining > 1e-9:
        total_weight = sum(max(raw_values[index], 1e-9) for index in active)
        scale = remaining / total_weight
        capped = {
            index
            for index in active
            if max(raw_values[index], 1e-9) * scale >= cap
        }
        if not capped:
            for index in active:
                allocations[index] = (
                    max(raw_values[index], 1e-9) * scale
                )
            remaining = 0.0
            break
        for index in capped:
            allocations[index] = cap
            remaining -= cap
        active -= capped
    return allocations


def _poisson_at_least(rate: float, threshold: int) -> float:
    """Return P(X >= threshold) for a Poisson count without SciPy."""

    if threshold <= 0:
        return 1.0
    if rate <= 0 or threshold >= 1000:
        return 0.0
    probability = math.exp(-rate)
    cumulative = probability
    for count in range(1, threshold):
        probability *= rate / count
        cumulative += probability
    return _clamp(1.0 - cumulative, 0.0, 1.0)


def _poisson_expected_complete_pairs(rate: float) -> float:
    """Return E[floor(X / 2)] for a Poisson-distributed goal count."""

    if rate <= 0:
        return 0.0
    return rate / 2.0 - (1.0 - math.exp(-2.0 * rate)) / 4.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
