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
from .team_strength import (
    ContextualAdjustment,
    TeamStrengthSettings,
    TeamStrengthState,
    estimate_team_strength,
)

MODEL_VERSION = "rates-rules-corrected-v4"
OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_VERSION = (
    "opponent-adjusted-team-strength-v1"
)
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

# Price signals attacking role. It says nothing about disciplinary record,
# goalkeeping workload or own goals, so those priors stay flat. Clean sheets
# are excluded because they are supplied by team strength, which now has its
# own preseason prior.
COLD_START_SCALED_RATES: frozenset[str] = frozenset(
    {"goals", "assists", "bonus"}
)

DEFENSIVE_CONTRIBUTION_COUNT_PRIORS: dict[Position, float] = {
    Position.GK: 0.0,
    Position.DEF: 7.0,
    Position.MID: 5.0,
    Position.FWD: 3.0,
}

# Forward-only calibration from 2025/26. Each pair is the threshold hit rate
# conditional on an appearance below 60 minutes and at least 60 minutes:
# DEF 5/924 and 816/3026; MID 2/2082 and 585/3265; FWD 0/663 and 9/765.
DEFENSIVE_CONTRIBUTION_HIT_RATES_2025: dict[Position, tuple[float, float]] = {
    Position.GK: (0.0, 0.0),
    Position.DEF: (5 / 924, 816 / 3026),
    Position.MID: (2 / 2082, 585 / 3265),
    Position.FWD: (0.0, 9 / 765),
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
    playing_time_artifact: str | None = None
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
    team_form_half_life_gameweeks: float = 8.0
    team_assist_per_goal_prior: float = 0.72
    defensive_contribution_model: str = "legacy_linear"
    include_penalty_events: bool = False
    # Preseason capability. Team strength reads only completed fixtures in the
    # target season, so before a ball is kicked every club sits on the league
    # prior and the model cannot tell Manchester City from a promoted side.
    # These options seed that prior instead. All defaults are declared, not
    # fitted: no inspected season was used to choose them.
    team_strength_carry_forward: bool = False
    carry_forward_regression_matches: float = 12.0
    promoted_team_attack_multiplier: float = 0.85
    promoted_team_defence_multiplier: float = 1.20
    cold_start_prior: str = "position"
    cold_start_price_elasticity: float = 1.5
    cold_start_minimum_factor: float = 0.35
    cold_start_maximum_factor: float = 3.0
    # Which team-strength estimator produces the attack and defence
    # multipliers. "raw_goals" is the incumbent: goals for and against shrunk
    # toward the league average, with no adjustment for who they came against.
    # "opponent_adjusted" uses fpl_engine.team_strength, which rates every club
    # relative to the opponents it actually faced, seeds a preseason prior from
    # the previous season's opponent-adjusted expected goals, and reports its
    # own derivation. It supersedes team_strength_carry_forward, which is
    # ignored when it is selected.
    team_strength_model: str = "raw_goals"

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
            raise ValueError("Maximum team multiplier cannot be below the minimum")
        if self.minutes_model not in {
            "legacy",
            "two_stage",
            "learned_hurdle",
        }:
            raise ValueError(
                "Minutes model must be 'legacy', 'two_stage' or "
                "'learned_hurdle'"
            )
        if (
            self.minutes_model == "learned_hurdle"
            and not self.playing_time_artifact
        ):
            raise ValueError(
                "Learned hurdle minutes require an artifact path"
            )
        if self.recent_gameweeks <= 0:
            raise ValueError("Recent Gameweeks must be positive")
        if self.recent_evidence_weight < 1:
            raise ValueError("Recent evidence weight cannot be below one")
        if self.appearance_prior_matches <= 0:
            raise ValueError("Appearance prior matches must be positive")
        if not 0 < self.appearance_prior_probability < 1:
            raise ValueError("Appearance prior probability must be between zero and one")
        if self.conditional_minutes_prior_appearances <= 0:
            raise ValueError("Conditional-minutes prior appearances must be positive")
        if self.team_minutes_per_fixture <= 0:
            raise ValueError("Team minutes per fixture must be positive")
        if self.minutes_allocation not in {
            "team_total",
            "position_aware",
        }:
            raise ValueError("Minutes allocation must be 'team_total' or 'position_aware'")
        if self.scoring_recent_evidence_weight < 1:
            raise ValueError("Recent scoring evidence weight cannot be below one")
        if self.scoring_event_source not in {
            "actual",
            "expected_with_actual_fallback",
            "team_share_expected",
        }:
            raise ValueError(
                "Scoring event source must be 'actual' or "
                "'expected_with_actual_fallback', or "
                "'team_share_expected'"
            )
        if self.team_form_half_life_gameweeks <= 0:
            raise ValueError("Team form half-life must be positive")
        if not 0 <= self.team_assist_per_goal_prior <= 1:
            raise ValueError("Team assist-per-goal prior must be between zero and one")
        if self.defensive_contribution_model not in {
            "legacy_linear",
            "threshold_poisson",
            "empirical_2025_minutes_band",
        }:
            raise ValueError(
                "Defensive contribution model must be 'legacy_linear', "
                "'threshold_poisson', or 'empirical_2025_minutes_band'"
            )
        if self.carry_forward_regression_matches <= 0:
            raise ValueError("Carry-forward regression matches must be positive")
        if self.promoted_team_attack_multiplier <= 0:
            raise ValueError("Promoted attack multiplier must be positive")
        if self.promoted_team_defence_multiplier <= 0:
            raise ValueError("Promoted defence multiplier must be positive")
        if self.cold_start_prior not in {"position", "position_price"}:
            raise ValueError(
                "Cold-start prior must be 'position' or 'position_price'"
            )
        if self.cold_start_price_elasticity < 0:
            raise ValueError("Cold-start price elasticity cannot be negative")
        if not 0 < self.cold_start_minimum_factor <= 1:
            raise ValueError(
                "Cold-start minimum factor must be within (0, 1]"
            )
        if self.cold_start_maximum_factor < self.cold_start_minimum_factor:
            raise ValueError(
                "Cold-start maximum factor cannot be below the minimum"
            )
        if self.team_strength_model not in {"raw_goals", "opponent_adjusted"}:
            raise ValueError(
                "Team strength model must be 'raw_goals' or "
                "'opponent_adjusted'"
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
TEAM_SHARE_XG_V5_MODEL_CONFIG = replace(
    CORRECTED_V4_MODEL_CONFIG,
    scoring_event_source="team_share_expected",
)
DEFENSIVE_EMPIRICAL_V5_MODEL_CONFIG = replace(
    CORRECTED_V4_MODEL_CONFIG,
    defensive_contribution_model="empirical_2025_minutes_band",
)
PRESEASON_V5_MODEL_CONFIG = replace(
    CORRECTED_V4_MODEL_CONFIG,
    team_strength_carry_forward=True,
    cold_start_prior="position_price",
)
#: The opponent-adjusted challenger. Three changes from the corrected v4
#: incumbent, each fixing a named structural defect:
#:
#: * `team_strength_model` replaces raw goals with an opponent-adjusted
#:   Poisson rating and a previous-season expected-goal prior, so clubs are
#:   separated before a ball is kicked and an easy early schedule does not
#:   make a mediocre club look elite;
#: * `scoring_event_source` allocates the team's goal expectation to players
#:   by share, rather than multiplying a player's already club-influenced
#:   per-90 rate by their club's strength a second time;
#: * `cold_start_prior` gives a player with no history a price-scaled prior,
#:   which matters far more once shares, not raw rates, drive scoring.
OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG = replace(
    CORRECTED_V4_MODEL_CONFIG,
    team_strength_model="opponent_adjusted",
    scoring_event_source="team_share_expected",
    cold_start_prior="position_price",
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
    latent_expectations: dict[str, float] | None = None


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
    #: Full derivation of the team ratings when the opponent-adjusted model
    #: produced them: priors, weights, continuity, adjustments and rationale.
    #: `None` for the incumbent, which has no such record to give.
    team_strength_state: TeamStrengthState | None = None


class RatesProjectionModel:
    """Bayesian-shrunk per-90 baseline with explicit fixture adjustments."""

    def __init__(
        self,
        database: HistoricalDatabase,
        rules: SeasonRules,
        *,
        config: ProjectionModelConfig = DEFAULT_MODEL_CONFIG,
        model_version: str = MODEL_VERSION,
        team_strength_settings: TeamStrengthSettings | None = None,
        team_strength_adjustments: tuple[ContextualAdjustment, ...] = (),
    ) -> None:
        self.database = database
        self.rules = rules
        self.config = config
        self.model_version = model_version
        # The projection config owns every constant the two share, so a
        # declared team-strength setting can never disagree with the venue
        # multipliers and bounds the rest of the projection uses.
        self.team_strength_settings = (
            team_strength_settings or TeamStrengthSettings()
        ).for_projection_config(config)
        self.team_strength_adjustments = team_strength_adjustments
        self._last_team_strength_state: TeamStrengthState | None = None

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
        self._prepare_cold_start_priors(players)
        self._prepare_minutes(
            players,
            season_code=season_code,
            start_gameweek=start_gameweek,
            use_availability=use_availability,
        )
        if self.config.scoring_event_source == "team_share_expected":
            self._prepare_team_event_shares(players)
        fixtures = self._fixtures(
            season_code,
            start_gameweek,
            horizon_gameweeks,
            as_of=fixture_as_of,
            maximum_ingestion_run_id=source_ingestion_run_id,
        )
        # Cleared first so a raw-goals run can never report the derivation of
        # an opponent-adjusted run that happened to share this engine.
        self._last_team_strength_state = None
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
            team_strength_state=self._last_team_strength_state,
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
            raise ValueError(f"No completed projection source ingestion run{qualifier}")
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
        if self.config.team_strength_model == "opponent_adjusted":
            return self._opponent_adjusted_team_strengths(
                season_code,
                start_gameweek,
                overrides,
                as_of=as_of,
                maximum_ingestion_run_id=maximum_ingestion_run_id,
            )
        if self.config.scoring_event_source == "team_share_expected":
            return self._expected_goal_team_strengths(
                season_code,
                start_gameweek,
                overrides,
            )
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
        carry_forward, previous_league_average = (
            self._carry_forward_team_rates(
                season_code,
                maximum_ingestion_run_id=maximum_ingestion_run_id,
            )
            if self.config.team_strength_carry_forward
            else ({}, None)
        )
        if total_matches:
            league_average = total_goals / total_matches
        elif previous_league_average is not None:
            # Before the season starts there is nothing to average, so the
            # previous season's scoring rate beats a hardcoded constant.
            league_average = previous_league_average
        else:
            league_average = 1.4
        prior_matches = self.config.team_prior_matches
        result: dict[str, dict[str, float]] = {}
        for row in aggregate:
            matches = int(row["matches"])
            # Without carry-forward every club shrinks toward the same league
            # average, which is why a preseason forecast cannot separate clubs.
            prior_attack, prior_defence = carry_forward.get(
                str(row["team_id"]),
                (league_average, league_average),
            )
            attack_rate = (float(row["goals_for"]) + prior_matches * prior_attack) / (
                matches + prior_matches
            )
            defence_rate = (float(row["goals_against"]) + prior_matches * prior_defence) / (
                matches + prior_matches
            )
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
        by_source = {str(row["source_team_id"]): str(row["team_id"]) for row in aggregate}
        for override in overrides:
            team_id = by_source.get(override.source_team_id)
            if team_id is None:
                raise ValueError(f"Unknown team override {override.source_team_id!r}")
            result[team_id]["attack"] = override.attack_multiplier
            result[team_id]["defence"] = override.defence_susceptibility
            result[team_id]["overridden"] = 1.0
        return result

    def _carry_forward_team_rates(
        self,
        season_code: str,
        *,
        maximum_ingestion_run_id: int | None,
    ) -> tuple[dict[str, tuple[float, float]], float | None]:
        """Seed each club's prior from the previous season's goal rates.

        Returns per-team expected goals for and against per match, keyed by
        the *current* season's team id, plus the previous season's league
        average. Clubs are matched across seasons by name because the source's
        team numbering is reassigned every year.

        This reads only a season that finished before the target season began,
        so it cannot leak into any origin within the target season. Promoted
        clubs have nothing to carry and take the declared promoted prior
        instead.
        """

        previous = self.database.connection.execute(
            """
            SELECT code FROM seasons
            WHERE code < ?
            ORDER BY code DESC
            LIMIT 1
            """,
            (season_code,),
        ).fetchone()
        if previous is None:
            return {}, None
        rows = self.database.connection.execute(
            """
            WITH results AS (
                SELECT home_team_id AS team_id, home_score AS goals_for,
                       away_score AS goals_against
                FROM fixtures
                JOIN seasons ON seasons.id = fixtures.season_id
                WHERE seasons.code = ? AND fixtures.finished = 1
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND (? IS NULL OR fixtures.provenance_run_id <= ?)
                UNION ALL
                SELECT away_team_id, away_score, home_score
                FROM fixtures
                JOIN seasons ON seasons.id = fixtures.season_id
                WHERE seasons.code = ? AND fixtures.finished = 1
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND (? IS NULL OR fixtures.provenance_run_id <= ?)
            )
            SELECT teams.name AS name,
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
                previous["code"],
                maximum_ingestion_run_id,
                maximum_ingestion_run_id,
                previous["code"],
                maximum_ingestion_run_id,
                maximum_ingestion_run_id,
                previous["code"],
            ),
        ).fetchall()
        total_matches = sum(int(row["matches"]) for row in rows)
        if not total_matches:
            return {}, None
        previous_league_average = (
            sum(float(row["goals_for"]) for row in rows) / total_matches
        )
        regression = self.config.carry_forward_regression_matches
        by_name = {
            str(row["name"]): (
                (
                    float(row["goals_for"])
                    + regression * previous_league_average
                )
                / (int(row["matches"]) + regression),
                (
                    float(row["goals_against"])
                    + regression * previous_league_average
                )
                / (int(row["matches"]) + regression),
            )
            for row in rows
            if int(row["matches"]) > 0
        }
        current = self.database.connection.execute(
            """
            SELECT teams.id AS team_id, teams.name AS name
            FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        ).fetchall()
        promoted_attack = (
            previous_league_average * self.config.promoted_team_attack_multiplier
        )
        promoted_defence = (
            previous_league_average
            * self.config.promoted_team_defence_multiplier
        )
        return (
            {
                str(row["team_id"]): by_name.get(
                    str(row["name"]),
                    (promoted_attack, promoted_defence),
                )
                for row in current
            },
            previous_league_average,
        )

    def fixture_lambdas(
        self,
        strengths: dict[str, dict[str, float]],
        *,
        team_id: str,
        opponent_id: str,
        is_home: bool,
    ) -> tuple[float, float, float]:
        """Expected goals for and against in one fixture.

        The single place a team rating becomes a goal expectation. Every
        consumer goes through it — the player projection, the squad simulator
        and the historical evaluation — so a model cannot be scored under
        venue constants the live forecast does not use. Returns the scoring
        factor, the team's expected goals and the opponent's.
        """

        team = strengths[team_id]
        opponent = strengths[opponent_id]
        league_average = float(team["league_average_goals"])
        attack_venue = (
            self.config.home_attack_multiplier
            if is_home
            else self.config.away_attack_multiplier
        )
        defence_venue = (
            self.config.away_attack_multiplier
            if is_home
            else self.config.home_attack_multiplier
        )
        scoring_factor = (
            float(team["attack"]) * float(opponent["defence"]) * attack_venue
        )
        return (
            scoring_factor,
            league_average * scoring_factor,
            league_average
            * float(opponent["attack"])
            * float(team["defence"])
            * defence_venue,
        )

    def fixture_expected_goals(
        self,
        *,
        season_code: str,
        gameweek_number: int,
        team_overrides: tuple[TeamStrengthOverride, ...] = (),
        as_of: datetime | None = None,
        maximum_ingestion_run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every fixture in a Gameweek, with the goals this model expects.

        The public entry point the historical evaluation uses, so what is
        scored is what a projection at that origin would actually have used.
        """

        strengths = self._team_strengths(
            season_code,
            gameweek_number,
            team_overrides,
            as_of=as_of,
            maximum_ingestion_run_id=maximum_ingestion_run_id,
        )
        rows = self.database.connection.execute(
            """
            SELECT fixtures.id AS fixture_id,
                   fixtures.home_team_id, fixtures.away_team_id,
                   fixtures.home_score, fixtures.away_score
            FROM fixtures
            JOIN seasons ON seasons.id = fixtures.season_id
            JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
            WHERE seasons.code = ? AND gameweeks.number = ?
            ORDER BY fixtures.id
            """,
            (season_code, gameweek_number),
        ).fetchall()
        fixtures = []
        for row in rows:
            home = str(row["home_team_id"])
            away = str(row["away_team_id"])
            if home not in strengths or away not in strengths:
                continue
            _, home_lambda, away_lambda = self.fixture_lambdas(
                strengths, team_id=home, opponent_id=away, is_home=True
            )
            fixtures.append(
                {
                    "fixture_id": int(row["fixture_id"]),
                    "gameweek_number": gameweek_number,
                    "home_team_id": home,
                    "away_team_id": away,
                    "home_expected_goals": home_lambda,
                    "away_expected_goals": away_lambda,
                    "home_score": row["home_score"],
                    "away_score": row["away_score"],
                }
            )
        return fixtures

    def _opponent_adjusted_team_strengths(
        self,
        season_code: str,
        start_gameweek: int,
        overrides: tuple[TeamStrengthOverride, ...],
        *,
        as_of: datetime | None,
        maximum_ingestion_run_id: int | None,
    ) -> dict[str, dict[str, float]]:
        """Adapt the consolidated estimator to the projection strength dict.

        The estimator is the single source of team strength for this
        configuration: preseason prior, current-season updating and contextual
        adjustment all happen inside it, so the two rival implementations below
        are bypassed entirely rather than blended with.

        `TeamStrengthOverride` is preserved and still wins outright. An
        override is an operator asserting a value; a `ContextualAdjustment` is
        a bounded, dated, explained nudge to a derived one. Both are recorded.
        """

        state = estimate_team_strength(
            self.database,
            season_code=season_code,
            gameweek_number=start_gameweek,
            settings=self.team_strength_settings,
            adjustments=self.team_strength_adjustments,
            as_of=as_of,
            maximum_ingestion_run_id=maximum_ingestion_run_id,
        )
        self._last_team_strength_state = state
        result: dict[str, dict[str, float]] = {
            team_id: {
                # Already clamped inside the estimator, against the same bounds
                # this config declares. Clamping again here would be harmless
                # but would hide a divergence rather than surface one.
                "attack": team.attack,
                "defence": team.defence,
                "matches": team.matches_observed,
                "league_average_goals": state.league_average_goals,
                "assist_per_goal": state.assist_per_goal,
                "source_is_expected_goals": (
                    1.0 if team.evidence_source == "expected_goals" else 0.0
                ),
                "uncertainty": team.uncertainty,
                "prior_weight": team.prior_weight,
                "current_weight": team.current_weight,
                "schedule_strength": team.schedule_strength,
                "is_promoted": 1.0 if team.is_promoted else 0.0,
                "adjustments_applied": float(len(team.adjustments)),
            }
            for team_id, team in state.teams.items()
        }
        by_source = {
            team.source_team_id: team_id for team_id, team in state.teams.items()
        }
        for override in overrides:
            team_id = by_source.get(override.source_team_id)
            if team_id is None:
                raise ValueError(f"Unknown team override {override.source_team_id!r}")
            result[team_id]["attack"] = override.attack_multiplier
            result[team_id]["defence"] = override.defence_susceptibility
            result[team_id]["overridden"] = 1.0
        return result

    def _expected_goal_team_strengths(
        self,
        season_code: str,
        start_gameweek: int,
        overrides: tuple[TeamStrengthOverride, ...],
    ) -> dict[str, dict[str, float]]:
        """Opponent-adjusted, decayed team xG strengths using prior fixtures only."""

        rows = self.database.connection.execute(
            """
            WITH fixture_events AS (
                SELECT fixtures.id AS fixture_id,
                       gameweeks.number AS gameweek_number,
                       fixtures.home_team_id,
                       fixtures.away_team_id,
                       fixtures.home_score,
                       fixtures.away_score,
                       SUM(
                           CASE WHEN player_seasons.team_id = fixtures.home_team_id
                                THEN COALESCE(stats.expected_goals, stats.goals)
                                ELSE 0 END
                       ) AS home_xg,
                       SUM(
                           CASE WHEN player_seasons.team_id = fixtures.away_team_id
                                THEN COALESCE(stats.expected_goals, stats.goals)
                                ELSE 0 END
                       ) AS away_xg,
                       SUM(
                           CASE WHEN player_seasons.team_id = fixtures.home_team_id
                                THEN COALESCE(stats.expected_assists, stats.assists)
                                ELSE 0 END
                       ) AS home_xa,
                       SUM(
                           CASE WHEN player_seasons.team_id = fixtures.away_team_id
                                THEN COALESCE(stats.expected_assists, stats.assists)
                                ELSE 0 END
                       ) AS away_xa
                FROM fixtures
                JOIN seasons ON seasons.id = fixtures.season_id
                JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                LEFT JOIN player_fixture_stats stats
                  ON stats.fixture_id = fixtures.id
                LEFT JOIN player_seasons
                  ON player_seasons.id = stats.player_season_id
                WHERE seasons.code = ?
                  AND gameweeks.number < ?
                  AND fixtures.finished = 1
                GROUP BY fixtures.id
            ),
            team_events AS (
                SELECT gameweek_number, home_team_id AS team_id,
                       home_xg AS xg_for, away_xg AS xg_against,
                       home_xa AS xa_for
                FROM fixture_events
                UNION ALL
                SELECT gameweek_number, away_team_id,
                       away_xg, home_xg, away_xa
                FROM fixture_events
            )
            SELECT teams.id AS team_id, teams.source_team_id,
                   team_events.gameweek_number,
                   team_events.xg_for, team_events.xg_against,
                   team_events.xa_for
            FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            LEFT JOIN team_events ON team_events.team_id = teams.id
            WHERE seasons.code = ?
            ORDER BY teams.id, team_events.gameweek_number
            """,
            (season_code, start_gameweek, season_code),
        ).fetchall()
        team_rows: dict[str, list[Any]] = {}
        by_source: dict[str, str] = {}
        for row in rows:
            team_id = str(row["team_id"])
            team_rows.setdefault(team_id, [])
            by_source[str(row["source_team_id"])] = team_id
            if row["gameweek_number"] is not None:
                team_rows[team_id].append(row)

        weighted_xg = 0.0
        weighted_matches = 0.0
        weighted_xa = 0.0
        for values in team_rows.values():
            for row in values:
                weight = _decay_weight(
                    start_gameweek - int(row["gameweek_number"]),
                    self.config.team_form_half_life_gameweeks,
                )
                weighted_xg += float(row["xg_for"]) * weight
                weighted_xa += float(row["xa_for"]) * weight
                weighted_matches += weight
        league_average = weighted_xg / weighted_matches if weighted_matches else 1.4
        if league_average <= 0:
            # Some imported seasons store expected_goals as 0.0 rather than
            # NULL, so the COALESCE fallback to actual goals never fires and
            # every prior fixture reads as goalless. Without this the next
            # division raises. Same fallback as the no-fixtures case above;
            # this changes no output that previously computed.
            league_average = 1.4
        assist_per_goal = (
            (
                weighted_xa
                + self.config.team_prior_matches
                * league_average
                * self.config.team_assist_per_goal_prior
            )
            / (weighted_xg + self.config.team_prior_matches * league_average)
            if league_average > 0
            else self.config.team_assist_per_goal_prior
        )
        assist_per_goal = _clamp(assist_per_goal, 0.0, 1.0)
        result: dict[str, dict[str, float]] = {}
        for team_id, values in team_rows.items():
            matches = 0.0
            xg_for = 0.0
            xg_against = 0.0
            for row in values:
                weight = _decay_weight(
                    start_gameweek - int(row["gameweek_number"]),
                    self.config.team_form_half_life_gameweeks,
                )
                matches += weight
                xg_for += float(row["xg_for"]) * weight
                xg_against += float(row["xg_against"]) * weight
            prior = self.config.team_prior_matches
            attack_rate = (xg_for + prior * league_average) / (matches + prior)
            defence_rate = (xg_against + prior * league_average) / (matches + prior)
            result[team_id] = {
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
                "matches": matches,
                "league_average_goals": league_average,
                "assist_per_goal": assist_per_goal,
                "source_is_expected_goals": 1.0,
            }
        for override in overrides:
            team_id = by_source.get(override.source_team_id)
            if team_id is None:
                raise ValueError(f"Unknown team override {override.source_team_id!r}")
            result[team_id]["attack"] = override.attack_multiplier
            result[team_id]["defence"] = override.defence_susceptibility
            result[team_id]["overridden"] = 1.0
        return result

    def _prepare_team_event_shares(
        self,
        players: list[dict[str, Any]],
    ) -> None:
        """Create coherent player shares without reapplying team strength.

        A share is a player's slice of their own club's attacking output, so
        the club's quality is applied exactly once — to the team total — and
        never again to the player. This is the fix for the double count: the
        rate path multiplies a per-90 rate that was itself earned at that club
        by that club's strength a second time.

        Each player's weight is their expected goal (or assist) involvement in
        one fixture: a shrunk per-90 rate scaled by expected minutes. Four
        things feed the rate:

        * historical expected goals and assists, falling back to actual events
          where the expected fields are absent;
        * recent evidence, weighted by `scoring_recent_evidence_weight`, so a
          player who has just taken over a role moves faster than their season
          total suggests;
        * a position prior, shrinking small samples toward what a typical
          player in that position does;
        * the cold-start price factor on the prior term only, so a player with
          no minutes at this club is not assumed to be a reserve, and the
          adjustment fades automatically as real minutes accumulate.

        Shares are normalised to sum to one within a club, so the players'
        goal expectations reconcile exactly to the team's. What that does not
        model: own goals, and the fact that not every goal is assisted — the
        latter is handled separately by the league assist-per-goal ratio, and
        the former is left in the per-player deduction where it already lives.
        No penalty or set-piece role data is available in this schema, so
        share is inferred from output alone; a reviewed role override remains
        the only way to assert one.
        """

        raw: dict[str, list[tuple[dict[str, Any], float, float]]] = {}
        prior_minutes = self.config.player_rate_prior_minutes
        recent_extra = self.config.scoring_recent_evidence_weight - 1.0
        for player in players:
            position = Position(player["position"])
            sample_minutes = float(player["minutes"]) + recent_extra * float(
                player["recent_minutes"]
            )
            price_factor = float(player.get("_cold_start_price_factor", 1.0))
            goal_rate = (
                (
                    float(player["expected_goals"])
                    + recent_extra * float(player["recent_expected_goals"])
                )
                * 90.0
                + POSITION_PRIORS[position]["goals"] * price_factor * prior_minutes
            ) / (sample_minutes + prior_minutes)
            assist_rate = (
                (
                    float(player["expected_assists"])
                    + recent_extra * float(player["recent_expected_assists"])
                )
                * 90.0
                + POSITION_PRIORS[position]["assists"] * price_factor * prior_minutes
            ) / (sample_minutes + prior_minutes)
            minute_factor = float(player["_expected_minutes_per_fixture"]) / 90.0
            raw.setdefault(str(player["team_id"]), []).append(
                (
                    player,
                    max(0.0, goal_rate * minute_factor),
                    max(0.0, assist_rate * minute_factor),
                )
            )
        for team_players in raw.values():
            total_goals = sum(value[1] for value in team_players)
            total_assists = sum(value[2] for value in team_players)
            for player, goal_weight, assist_weight in team_players:
                player["_goal_share"] = goal_weight / total_goals if total_goals > 0 else 0.0
                player["_assist_share"] = (
                    assist_weight / total_assists if total_assists > 0 else 0.0
                )

    def _prepare_cold_start_priors(
        self,
        players: list[dict[str, Any]],
    ) -> None:
        """Scale each player's attacking prior by their price within position.

        The reference is the position's median price in the current pool, so
        the factor is one for a typical player and the adjustment is relative
        rather than absolute. Positions are kept separate because a £5.5m
        defender and a £5.5m forward carry very different expectations.
        """

        for player in players:
            player["_cold_start_price_factor"] = 1.0
        if self.config.cold_start_prior != "position_price":
            return
        by_position: dict[str, list[int]] = {}
        for player in players:
            by_position.setdefault(str(player["position"]), []).append(
                int(player["price_tenths"])
            )
        medians = {
            position: sorted(prices)[len(prices) // 2]
            for position, prices in by_position.items()
        }
        for player in players:
            reference = medians[str(player["position"])]
            if reference <= 0:
                continue
            player["_cold_start_price_factor"] = _clamp(
                (int(player["price_tenths"]) / reference)
                ** self.config.cold_start_price_elasticity,
                self.config.cold_start_minimum_factor,
                self.config.cold_start_maximum_factor,
            )

    def _prepare_minutes(
        self,
        players: list[dict[str, Any]],
        *,
        season_code: str,
        start_gameweek: int,
        use_availability: bool,
    ) -> None:
        """Estimate appearance and conditional minutes before team reconciliation."""

        learned_predictions = None
        if self.config.minutes_model == "learned_hurdle":
            from .playing_time import predict_live_hurdles

            learned_predictions = predict_live_hurdles(
                self.database,
                str(self.config.playing_time_artifact),
                season_code=season_code,
                start_gameweek=start_gameweek,
                players=players,
            )
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
            if learned_predictions is not None:
                (
                    appearance_probability,
                    start_probability,
                    sixty_probability,
                    conditional_minutes,
                ) = learned_predictions[int(player["player_season_id"])]
                appearance_probability = _clamp(
                    appearance_probability * availability,
                    0.0,
                    1.0,
                )
                sixty_probability = min(
                    appearance_probability,
                    sixty_probability * availability,
                )
                sixty_given_appearance = (
                    sixty_probability / appearance_probability
                    if appearance_probability > 0
                    else 0.0
                )
                expected_minutes = (
                    appearance_probability * conditional_minutes
                )
                player["_start_probability"] = min(
                    appearance_probability,
                    start_probability * availability,
                )
            elif self.config.minutes_model == "legacy":
                prior_matches = self.config.minutes_prior_matches
                expected_minutes = (
                    float(player["minutes"]) + POSITION_PRIORS[position]["minutes"] * prior_matches
                ) / (int(player["matches"]) + prior_matches)
                expected_minutes = _clamp(expected_minutes * availability, 0.0, 90.0)
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
                    MINUTES_PRIORS[position]["sixty_probability_given_appearance"],
                    0.0,
                    1.0,
                )
            else:
                extra_recent_weight = self.config.recent_evidence_weight - 1.0
                weighted_matches = float(player["matches"]) + extra_recent_weight * float(
                    player["recent_matches"]
                )
                weighted_appearances = float(player["appearances"]) + extra_recent_weight * float(
                    player["recent_appearances"]
                )
                weighted_sixty = float(player["sixty_appearances"]) + extra_recent_weight * float(
                    player["recent_sixty_appearances"]
                )
                weighted_minutes = float(player["minutes"]) + extra_recent_weight * float(
                    player["recent_minutes"]
                )
                appearance_probability = (
                    weighted_appearances
                    + self.config.appearance_prior_matches
                    * self.config.appearance_prior_probability
                ) / (weighted_matches + self.config.appearance_prior_matches)
                appearance_probability = _clamp(appearance_probability * availability, 0.0, 1.0)
                conditional_prior = self.config.conditional_minutes_prior_appearances
                conditional_minutes = (
                    weighted_minutes
                    + conditional_prior * MINUTES_PRIORS[position]["conditional_minutes"]
                ) / (weighted_appearances + conditional_prior)
                conditional_minutes = _clamp(conditional_minutes, 1.0, 90.0)
                sixty_given_appearance = (
                    weighted_sixty
                    + conditional_prior
                    * MINUTES_PRIORS[position]["sixty_probability_given_appearance"]
                ) / (weighted_appearances + conditional_prior)
                sixty_given_appearance = _clamp(sixty_given_appearance, 0.0, 1.0)
                expected_minutes = appearance_probability * conditional_minutes

            player["_availability"] = availability
            player["_conditional_minutes"] = conditional_minutes
            player["_appearance_probability"] = appearance_probability
            player["_sixty_given_appearance"] = sixty_given_appearance
            player["_sixty_probability"] = appearance_probability * sixty_given_appearance
            player["_expected_minutes_per_fixture"] = expected_minutes

        if self.config.minutes_model == "legacy" or not self.config.enforce_team_minutes:
            return

        players_by_team: dict[str, list[dict[str, Any]]] = {}
        for player in players:
            players_by_team.setdefault(str(player["team_id"]), []).append(player)
        for team_players in players_by_team.values():
            allocation_groups = (
                (
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
                )
                if self.config.minutes_allocation == "position_aware"
                else ((iter(team_players), self.config.team_minutes_per_fixture),)
            )
            reconciled: list[tuple[dict[str, Any], float]] = []
            for group, target in allocation_groups:
                group_players = list(group)
                allocations = _allocate_capped_minutes(
                    [float(player["_expected_minutes_per_fixture"]) for player in group_players],
                    target=target,
                    cap=90.0,
                )
                reconciled.extend(zip(group_players, allocations, strict=True))
            for player, expected_minutes in reconciled:
                conditional_minutes = float(player["_conditional_minutes"])
                appearance_probability = _clamp(
                    expected_minutes / max(conditional_minutes, 1.0),
                    0.0,
                    1.0,
                )
                player["_expected_minutes_per_fixture"] = expected_minutes
                player["_appearance_probability"] = appearance_probability
                player["_sixty_probability"] = min(
                    appearance_probability,
                    appearance_probability * float(player["_sixty_given_appearance"]),
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
        recent_scoring_extra = self.config.scoring_recent_evidence_weight - 1.0
        scoring_sample_minutes = sample_minutes + recent_scoring_extra * float(
            player["recent_minutes"]
        )
        rate_source_names = (
            {
                "goals": "expected_goals",
                "assists": "expected_assists",
            }
            if (self.config.scoring_event_source == "expected_with_actual_fallback")
            else {}
        )
        # A player with no Premier League history falls back entirely to the
        # position prior, so a record signing and a reserve start identical.
        # Price is the market's own estimate of attacking role and is the only
        # such estimate available before a ball is kicked. It scales the prior
        # term only, so it fades automatically as real minutes accumulate.
        price_factor = float(player.get("_cold_start_price_factor", 1.0))
        rates = {
            name: (
                (
                    float(player[rate_source_names.get(name, name)])
                    + recent_scoring_extra
                    * float(player[f"recent_{rate_source_names.get(name, name)}"])
                )
                * 90.0
                + (
                    DEFENSIVE_CONTRIBUTION_COUNT_PRIORS[position]
                    if (
                        name == "defensive_contributions"
                        and self.config.defensive_contribution_model == "threshold_poisson"
                    )
                    else prior[name]
                    * (price_factor if name in COLD_START_SCALED_RATES else 1.0)
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
                if player["team_id"] in (fixture["home_team_id"], fixture["away_team_id"])
            ]
            fixture_count = len(player_fixtures)
            override = overrides.get((player["source_player_id"], gameweek))
            expected_minutes = minutes_per_fixture * fixture_count
            if override is not None:
                expected_minutes = _clamp(override.expected_minutes, 0.0, 90.0 * fixture_count)
            per_fixture_minutes = 0.0 if fixture_count == 0 else expected_minutes / fixture_count
            fixture_appearance_probability = appearance_probability
            fixture_sixty_probability = sixty_probability
            if override is not None and self.config.minutes_model != "legacy":
                fixture_appearance_probability = _clamp(
                    per_fixture_minutes / max(conditional_minutes, 1.0),
                    0.0,
                    1.0,
                )
                fixture_sixty_probability = min(
                    fixture_appearance_probability,
                    fixture_appearance_probability * float(player["_sixty_given_appearance"]),
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
            latent = {
                "team_expected_goals": 0.0,
                "opponent_expected_goals": 0.0,
                "goal_share": float(player.get("_goal_share", 0.0)),
                "assist_share": float(player.get("_assist_share", 0.0)),
            }
            fixture_notes = []
            for fixture in player_fixtures:
                is_home = player["team_id"] == fixture["home_team_id"]
                opponent_id = str(fixture["away_team_id"] if is_home else fixture["home_team_id"])
                team_strength = strengths[str(player["team_id"])]
                scoring_factor, team_lambda, opponent_lambda = self.fixture_lambdas(
                    strengths,
                    team_id=str(player["team_id"]),
                    opponent_id=opponent_id,
                    is_home=is_home,
                )
                latent["team_expected_goals"] += team_lambda
                latent["opponent_expected_goals"] += opponent_lambda
                minute_factor = per_fixture_minutes / 90.0
                if self.config.minutes_model == "legacy":
                    sixty_factor = _clamp(per_fixture_minutes / 60.0, 0.0, 1.0)
                    components["appearance"] += min(per_fixture_minutes / 30.0, 2.0)
                else:
                    sixty_factor = fixture_sixty_probability
                    components["appearance"] += (
                        (fixture_appearance_probability - fixture_sixty_probability)
                        * self.rules.scoring.appearance_under_60
                        + fixture_sixty_probability * self.rules.scoring.appearance_60_or_more
                    )
                if self.config.scoring_event_source == "team_share_expected":
                    components["goal"] += (
                        team_lambda
                        * float(player["_goal_share"])
                        * self.rules.scoring.goals[position.value]
                    )
                    components["assist"] += (
                        team_lambda
                        * team_strength["assist_per_goal"]
                        * float(player["_assist_share"])
                        * self.rules.scoring.assists
                    )
                else:
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
                    rates["saves"] * minute_factor / self.rules.scoring.saves_per_point
                )
                if self.config.defensive_contribution_model == "threshold_poisson":
                    contribution_threshold = self.rules.scoring.defensive_contribution_thresholds[
                        position.value
                    ]
                    if contribution_threshold is not None:
                        contribution_lambda = (
                            rates["defensive_contributions"] * conditional_minutes / 90.0
                        )
                        components["defensive"] += (
                            fixture_appearance_probability
                            * _poisson_at_least(
                                contribution_lambda,
                                contribution_threshold,
                            )
                            * self.rules.scoring.defensive_contribution_points
                        )
                elif self.config.defensive_contribution_model == "empirical_2025_minutes_band":
                    contribution_threshold = self.rules.scoring.defensive_contribution_thresholds[
                        position.value
                    ]
                    if contribution_threshold is not None:
                        under_sixty_rate, sixty_rate = DEFENSIVE_CONTRIBUTION_HIT_RATES_2025[
                            position
                        ]
                        components["defensive"] += (
                            max(
                                0.0,
                                fixture_appearance_probability - fixture_sixty_probability,
                            )
                            * under_sixty_rate
                            + fixture_sixty_probability * sixty_rate
                        ) * (self.rules.scoring.defensive_contribution_points)
                else:
                    components["defensive"] += rates["defensive_contributions"] * minute_factor
                if self.config.include_penalty_events:
                    components["save"] += (
                        rates["penalties_saved"] * minute_factor * self.rules.scoring.penalty_save
                    )
                    components["deduction"] += (
                        rates["penalties_missed"] * minute_factor * self.rules.scoring.penalty_miss
                    )
                components["bonus"] += rates["bonus"] * minute_factor
                components["deduction"] -= (
                    rates["yellow_cards"] * minute_factor
                    + 3.0 * rates["red_cards"] * minute_factor
                    + 2.0 * rates["own_goals"] * minute_factor
                )
                if position in (Position.GK, Position.DEF):
                    if self.config.minutes_model != "legacy":
                        conceded_lambda = opponent_lambda * conditional_minutes / 90.0
                        expected_conceded_pairs = (
                            fixture_appearance_probability
                            * _poisson_expected_complete_pairs(conceded_lambda)
                        )
                    else:
                        expected_conceded_pairs = _poisson_expected_complete_pairs(
                            opponent_lambda * minute_factor
                        )
                    components["deduction"] += (
                        expected_conceded_pairs
                        * self.rules.scoring.goals_conceded_per_two[position.value]
                    )
                fixture_notes.append(
                    f"{'home' if is_home else 'away'} fixture factor {scoring_factor:.2f}"
                )
                if self.config.scoring_event_source == "team_share_expected":
                    fixture_notes.append(
                        f"team xG {team_lambda:.2f}, goal share {float(player['_goal_share']):.3f}"
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
                        1.0 - (1.0 - fixture_appearance_probability) ** fixture_count,
                        6,
                    ),
                    sixty_probability=round(
                        1.0 - (1.0 - fixture_sixty_probability) ** fixture_count,
                        6,
                    ),
                    appearance_points=round(components["appearance"], 3),
                    goal_points=round(components["goal"], 3),
                    assist_points=round(components["assist"], 3),
                    clean_sheet_points=round(components["clean"], 3),
                    save_points=round(components["save"], 3),
                    defensive_contribution_points=round(components["defensive"], 3),
                    bonus_points=round(components["bonus"], 3),
                    deduction_points=round(components["deduction"], 3),
                    expected_points=round(expected_points, 3),
                    uncertainty=round(uncertainty, 3),
                    assumptions=assumptions,
                    override_rationale=None if override is None else override.rationale,
                    latent_expectations={name: round(value, 8) for name, value in latent.items()},
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
            raise ValueError("Projection source ingestion run is missing or incomplete")
        assumptions = {
            "position_priors": POSITION_PRIORS,
            "team_strengths": strengths,
            "model_config": self.config.__dict__,
        }
        if self._last_team_strength_state is not None:
            # Every contextual adjustment travels with the run that used it,
            # so no adjustment can be applied without leaving a record.
            assumptions["team_strength_state"] = (
                self._last_team_strength_state.as_dict()
            )
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
    return sorted(totals.values(), key=lambda row: row["expected_points"], reverse=True)


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
        capped = {index for index in active if max(raw_values[index], 1e-9) * scale >= cap}
        if not capped:
            for index in active:
                allocations[index] = max(raw_values[index], 1e-9) * scale
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


def _decay_weight(age_gameweeks: int, half_life_gameweeks: float) -> float:
    return math.exp(-math.log(2.0) * max(0, age_gameweeks - 1) / half_life_gameweeks)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
