"""Transparent rates-based player projections."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
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
OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_VERSION = "opponent-adjusted-team-strength-v1"
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
COLD_START_SCALED_RATES: frozenset[str] = frozenset({"goals", "assists", "bonus"})

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
    # Explicit challenger paths. ``actual`` remains the incumbent; the
    # coherent path allocates the fixture-level team expectation across
    # players instead of multiplying independent player rates by team form.
    coherent_assist_unassisted_goal_fraction: float = 0.10
    coherent_penalty_goal_fraction: float = 0.08
    coherent_role_shrinkage_minutes: float = 900.0
    coherent_transfer_shrinkage: float = 0.50
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
    # How a promoted club's declared prior is set. "fixed" gives every
    # promoted club the two multipliers above, which is the incumbent.
    # "championship_relative" keeps those as the cohort average and varies
    # each club around it by its previous-division goals for and against,
    # at ``promoted_prior_weight``. Weight zero reproduces "fixed" exactly,
    # which is what makes the two a single-field comparison.
    promoted_prior_mode: str = "fixed"
    promoted_prior_weight: float = 0.0
    # Declared and asymmetric: Championship evidence may separate promoted
    # clubs from one another, never rate one above an average established club.
    promoted_prior_minimum_attack: float = 0.60
    promoted_prior_maximum_attack: float = 1.00
    promoted_prior_minimum_defence: float = 1.00
    promoted_prior_maximum_defence: float = 1.50
    cold_start_prior: str = "position"
    cold_start_price_elasticity: float = 1.5
    cold_start_minimum_factor: float = 0.35
    cold_start_maximum_factor: float = 3.0
    # ``participation_v1`` is an explicit minutes challenger.  The incumbent
    # reconciliation remains the default and is deliberately unchanged.
    minutes_reconciliation_mode: str = "legacy_capped"
    # The team-minutes budget is a statement about *minutes*, not about who is
    # available. Left off, reconciliation back-derives appearance probability
    # from a player's allocated share, which makes availability a function of
    # how much recorded history his club-mates happen to have. Switched on, the
    # estimated appearance probability survives and the correction is absorbed
    # into minutes conditional on an appearance instead. Default is off so the
    # incumbent is unchanged.
    minutes_reconciliation_preserves_appearance: bool = False
    # Path to an isotonic appearance-reliability map fitted on historical
    # origins of this same configuration. A map fitted for one minutes
    # configuration does not transfer to another, so the artifact records
    # which one it belongs to. ``None`` leaves probabilities uncalibrated.
    appearance_calibration_artifact: str | None = None
    minutes_reconciliation_max_relative_adjustment: float = 0.25
    minutes_reconciliation_max_absolute_adjustment: float = 12.0
    minutes_reconciliation_warning_deficit: float = 90.0
    participation_start_prior_probability: float = 0.45
    participation_start_prior_matches: float = 6.0
    participation_substitute_prior_probability: float = 0.18
    participation_substitute_prior_matches: float = 4.0
    participation_start_minutes_prior: float = 78.0
    participation_substitute_minutes_prior: float = 22.0
    participation_role_decay_per_gameweek: float = 0.03
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
            "participation_v1",
        }:
            raise ValueError("Minutes model must be 'legacy', 'two_stage' or 'learned_hurdle'")
        if self.minutes_model == "learned_hurdle" and not self.playing_time_artifact:
            raise ValueError("Learned hurdle minutes require an artifact path")
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
            "coherent_team_allocation",
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
        if self.promoted_prior_mode not in {"fixed", "championship_relative"}:
            raise ValueError(
                "Promoted prior mode must be 'fixed' or 'championship_relative'"
            )
        if self.promoted_prior_weight < 0:
            raise ValueError("Promoted prior weight cannot be negative")
        if self.promoted_prior_minimum_attack <= 0:
            raise ValueError("Minimum promoted attack multiplier must be positive")
        if self.promoted_prior_maximum_attack < self.promoted_prior_minimum_attack:
            raise ValueError(
                "Maximum promoted attack multiplier cannot be below the minimum"
            )
        if self.promoted_prior_minimum_defence <= 0:
            raise ValueError("Minimum promoted defence multiplier must be positive")
        if self.promoted_prior_maximum_defence < self.promoted_prior_minimum_defence:
            raise ValueError(
                "Maximum promoted defence multiplier cannot be below the minimum"
            )
        if self.cold_start_prior not in {"position", "position_price"}:
            raise ValueError("Cold-start prior must be 'position' or 'position_price'")
        if self.cold_start_price_elasticity < 0:
            raise ValueError("Cold-start price elasticity cannot be negative")
        if not 0 < self.cold_start_minimum_factor <= 1:
            raise ValueError("Cold-start minimum factor must be within (0, 1]")
        if self.cold_start_maximum_factor < self.cold_start_minimum_factor:
            raise ValueError("Cold-start maximum factor cannot be below the minimum")
        if not 0 <= self.coherent_assist_unassisted_goal_fraction <= 1:
            raise ValueError("Coherent unassisted-goal fraction must be between zero and one")
        if self.coherent_penalty_goal_fraction < 0 or self.coherent_penalty_goal_fraction > 1:
            raise ValueError("Coherent penalty-goal fraction must be between zero and one")
        if self.coherent_role_shrinkage_minutes <= 0:
            raise ValueError("Coherent role shrinkage minutes must be positive")
        if not 0 <= self.coherent_transfer_shrinkage <= 1:
            raise ValueError("Coherent transfer shrinkage must be between zero and one")
        if self.minutes_reconciliation_mode not in {"legacy_capped", "bounded_role_preserving"}:
            raise ValueError("Unknown minutes reconciliation mode")
        if self.minutes_reconciliation_max_relative_adjustment < 0:
            raise ValueError("Maximum relative minutes adjustment cannot be negative")
        if self.minutes_reconciliation_max_absolute_adjustment < 0:
            raise ValueError("Maximum absolute minutes adjustment cannot be negative")
        if self.minutes_reconciliation_warning_deficit < 0:
            raise ValueError("Minutes warning deficit cannot be negative")
        if not 0 < self.participation_start_prior_probability < 1:
            raise ValueError("Participation start prior must be between zero and one")
        if (
            self.participation_start_prior_matches <= 0
            or self.participation_substitute_prior_matches <= 0
        ):
            raise ValueError("Participation prior matches must be positive")
        if not 0 <= self.participation_substitute_prior_probability < 1:
            raise ValueError("Participation substitute prior must be between zero and one")
        if not 0 < self.participation_start_minutes_prior <= 90:
            raise ValueError("Participation starting minutes prior must be within a match")
        if not 0 < self.participation_substitute_minutes_prior <= 90:
            raise ValueError("Participation substitute minutes prior must be within a match")
        if not 0 <= self.participation_role_decay_per_gameweek <= 1:
            raise ValueError("Participation role decay must be between zero and one")
        if self.team_strength_model not in {"raw_goals", "opponent_adjusted"}:
            raise ValueError("Team strength model must be 'raw_goals' or 'opponent_adjusted'")


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


@lru_cache(maxsize=8)
def _appearance_calibration_knots(path: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Load and validate an isotonic appearance-reliability map."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("kind") != "appearance-isotonic-v1":
        raise ValueError(
            f"{path!r} is not an appearance-isotonic-v1 calibration artifact"
        )
    xs = tuple(float(value) for value in payload["knots"]["x"])
    ys = tuple(float(value) for value in payload["knots"]["y"])
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError(f"{path!r} needs at least two matching calibration knots")
    if any(b < a for a, b in zip(xs, xs[1:], strict=False)):
        raise ValueError(f"{path!r} calibration inputs must be non-decreasing")
    if any(b < a for a, b in zip(ys, ys[1:], strict=False)):
        raise ValueError(f"{path!r} calibration outputs must be non-decreasing")
    if not all(0.0 <= value <= 1.0 for value in (*xs, *ys)):
        raise ValueError(f"{path!r} calibration knots must be probabilities")
    return xs, ys


def _interpolate(
    knots: tuple[tuple[float, ...], tuple[float, ...]], value: float
) -> float:
    """Piecewise-linear lookup, clamped to the fitted range at both ends."""

    xs, ys = knots
    if value <= xs[0]:
        return ys[0]
    if value >= xs[-1]:
        return ys[-1]
    index = bisect_right(xs, value)
    left, right = xs[index - 1], xs[index]
    if right == left:
        return ys[index]
    weight = (value - left) / (right - left)
    return ys[index - 1] + weight * (ys[index] - ys[index - 1])


def _config_hash(config: ProjectionModelConfig) -> str:
    """Canonical hash used in persisted projection assumptions."""
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectionOverride:
    source_player_id: str
    gameweek_number: int
    expected_minutes: float | None
    rationale: str
    start_probability: float | None = None
    substitute_appearance_probability: float | None = None
    conditional_start_minutes: float | None = None
    conditional_substitute_minutes: float | None = None
    availability: float | None = None
    penalty_role_probability: float | None = None
    set_piece_role_probability: float | None = None
    effective_from: str | None = None
    expires_at: str | None = None
    source: str | None = None
    confidence: str | None = None
    expected_minutes_delta: float = 0.0
    expected_minutes_multiplier: float = 1.0
    appearance_probability: float | None = None
    appearance_probability_delta: float = 0.0
    appearance_probability_multiplier: float = 1.0
    start_probability_delta: float = 0.0
    start_probability_multiplier: float = 1.0
    sixty_probability: float | None = None
    sixty_probability_delta: float = 0.0
    sixty_probability_multiplier: float = 1.0
    availability_delta: float = 0.0
    availability_multiplier: float = 1.0
    modifier_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.set_piece_role_probability is not None:
            raise ValueError(
                "set_piece_role_probability is stored-only and cannot change projections"
            )
        if self.confidence is not None and self.confidence not in {"low", "medium", "high"}:
            raise ValueError("Override confidence must be low, medium or high")
        if any(
            value is not None
            for value in (
                self.start_probability,
                self.substitute_appearance_probability,
                self.conditional_start_minutes,
                self.conditional_substitute_minutes,
                self.availability,
                self.penalty_role_probability,
                self.appearance_probability,
                self.sixty_probability,
            )
        ) and (not self.source or not self.confidence):
            raise ValueError("Active component overrides require source and confidence")
        for name, value in (
            ("expected_minutes_delta", self.expected_minutes_delta),
            ("appearance_probability_delta", self.appearance_probability_delta),
            ("start_probability_delta", self.start_probability_delta),
            ("sixty_probability_delta", self.sixty_probability_delta),
            ("availability_delta", self.availability_delta),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        for name, value in (
            ("expected_minutes_multiplier", self.expected_minutes_multiplier),
            ("appearance_probability_multiplier", self.appearance_probability_multiplier),
            ("start_probability_multiplier", self.start_probability_multiplier),
            ("sixty_probability_multiplier", self.sixty_probability_multiplier),
            ("availability_multiplier", self.availability_multiplier),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


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
    modifier_ids: tuple[int, ...] = ()
    latent_expectations: dict[str, float] | None = None
    start_probability: float = 0.0
    substitute_appearance_probability: float = 0.0
    no_appearance_probability: float = 1.0
    expected_minutes_if_start: float = 0.0
    expected_minutes_if_substitute: float = 0.0
    sixty_minute_probability: float = 0.0
    role_unknown: bool = False
    role_evidence_source: str = "historical appearance/minutes evidence"
    reconciliation_adjustment: float = 0.0
    unresolved_minutes: float = 0.0
    reconciliation_warning: bool = False
    goal_share: float = 0.0
    assist_share: float = 0.0
    penalty_share: float = 0.0
    team_expected_goals: float = 0.0
    expected_assisted_goals: float = 0.0
    expected_goals: float = 0.0
    expected_assists: float = 0.0


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
        self._prepare_gameweek_participation(
            players,
            start_gameweek=start_gameweek,
            horizon_gameweeks=horizon_gameweeks,
            overrides=override_lookup,
            generated_at=generated,
        )
        if self.config.scoring_event_source == "coherent_team_allocation":
            self._prepare_coherent_event_allocations(
                players,
                fixtures,
                strengths,
                start_gameweek=start_gameweek,
                horizon_gameweeks=horizon_gameweeks,
            )
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
                       CASE WHEN COUNT(DISTINCT history_ps.team_id) > 1
                            THEN 1 ELSE 0 END AS club_changed,
                       COALESCE(SUM(stats.starts), 0) AS starts,
                       COALESCE(SUM(stats.minutes = 0), 0) AS zero_minute_records,
                       COALESCE(SUM(stats.minutes > 0), 0) AS appearances,
                       COALESCE(SUM(stats.minutes > 0 AND stats.starts = 0), 0)
                           AS substitute_appearances,
                       COALESCE(SUM(CASE WHEN stats.starts = 1 THEN stats.minutes ELSE 0 END), 0)
                           AS starting_minutes,
                       COALESCE(SUM(CASE WHEN stats.minutes > 0
                                         AND stats.starts = 0
                                         THEN stats.minutes ELSE 0 END), 0)
                           AS substitute_minutes,
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
                       COALESCE(SUM(stats.starts), 0) AS recent_starts,
                       COALESCE(SUM(stats.minutes = 0), 0) AS recent_zero_minute_records,
                       COALESCE(SUM(stats.minutes > 0), 0)
                           AS recent_appearances,
                       COALESCE(SUM(stats.minutes > 0 AND stats.starts = 0), 0)
                           AS recent_substitute_appearances,
                       COALESCE(SUM(CASE WHEN stats.starts = 1 THEN stats.minutes ELSE 0 END), 0)
                           AS recent_starting_minutes,
                       COALESCE(SUM(CASE WHEN stats.minutes > 0
                                         AND stats.starts = 0
                                         THEN stats.minutes ELSE 0 END), 0)
                           AS recent_substitute_minutes,
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
                   career.club_changed,
                   career.starts, career.zero_minute_records,
                   career.substitute_appearances, career.starting_minutes,
                   career.substitute_minutes,
                   career.sixty_appearances, career.minutes,
                   recent.recent_matches, recent.recent_appearances,
                   recent.recent_starts, recent.recent_zero_minute_records,
                   recent.recent_substitute_appearances,
                   recent.recent_starting_minutes,
                   recent.recent_substitute_minutes,
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
        previous_rows = self.database.connection.execute(
            """
            WITH ranked_previous AS (
                SELECT current_ps.id AS current_player_season_id,
                       history_ps.team_id AS previous_team_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY current_ps.id
                           ORDER BY COALESCE(
                               history_seasons.ends_on, history_seasons.starts_on
                           ) DESC,
                                    history_ps.id DESC
                       ) AS rank
                FROM player_seasons current_ps
                JOIN seasons current_seasons ON current_seasons.id = current_ps.season_id
                JOIN player_seasons history_ps
                  ON history_ps.player_id = current_ps.player_id
                 AND history_ps.identifier_namespace = current_ps.identifier_namespace
                 AND history_ps.season_id <> current_ps.season_id
                JOIN seasons history_seasons ON history_seasons.id = history_ps.season_id
                WHERE current_seasons.code = ?
                  AND (
                      history_seasons.ends_on < current_seasons.starts_on
                      OR (
                          history_seasons.ends_on IS NULL
                          AND history_seasons.starts_on < current_seasons.starts_on
                      )
                  )
            )
            SELECT current_player_season_id, previous_team_id
            FROM ranked_previous WHERE rank = 1
            """,
            (season_code,),
        ).fetchall()
        previous_by_player_season = {
            int(row["current_player_season_id"]): int(row["previous_team_id"])
            for row in previous_rows
        }
        cross_season_recent_rows = self.database.connection.execute(
            """
            WITH ranked AS (
                SELECT current_ps.id AS current_player_season_id,
                       stats.minutes, stats.starts,
                       ROW_NUMBER() OVER (
                           PARTITION BY current_ps.id
                           ORDER BY COALESCE(fixtures.kickoff_time, gameweeks.deadline_time) DESC,
                                    stats.id DESC
                       ) AS rank
                FROM player_seasons current_ps
                JOIN seasons current_seasons ON current_seasons.id = current_ps.season_id
                JOIN player_seasons history_ps
                  ON history_ps.player_id = current_ps.player_id
                 AND history_ps.identifier_namespace = current_ps.identifier_namespace
                JOIN player_fixture_stats stats ON stats.player_season_id = history_ps.id
                JOIN fixtures ON fixtures.id = stats.fixture_id
                JOIN seasons history_seasons ON history_seasons.id = fixtures.season_id
                LEFT JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
                WHERE current_seasons.code = ?
                  AND (
                      history_seasons.starts_on < current_seasons.starts_on
                      OR (
                          history_seasons.id = current_ps.season_id
                          AND gameweeks.number < ?
                      )
                  )
            )
            SELECT current_player_season_id,
                   COUNT(*) AS matches,
                   SUM(starts) AS starts,
                   SUM(minutes > 0) AS appearances,
                   SUM(minutes > 0 AND starts = 0) AS substitute_appearances,
                   SUM(minutes = 0) AS zero_minute_records,
                   SUM(CASE WHEN starts = 1 THEN minutes ELSE 0 END) AS starting_minutes,
                   SUM(CASE WHEN minutes > 0 AND starts = 0
                            THEN minutes ELSE 0 END) AS substitute_minutes,
                   SUM(minutes >= 60) AS sixty_appearances,
                   SUM(minutes) AS minutes
            FROM ranked WHERE rank <= ?
            GROUP BY current_player_season_id
            """,
            (season_code, start_gameweek, self.config.recent_gameweeks),
        ).fetchall()
        cross_recent_by_player_season = {
            int(row["current_player_season_id"]): dict(row) for row in cross_season_recent_rows
        }
        result = [dict(row) for row in rows]
        for player in result:
            previous_team = previous_by_player_season.get(int(player["player_season_id"]))
            player["previous_team_id"] = previous_team
            player["current_transfer"] = previous_team is not None and previous_team != int(
                player["team_id"]
            )
            if int(player.get("recent_matches", 0)) == 0:
                recent = cross_recent_by_player_season.get(int(player["player_season_id"]))
                if recent is not None:
                    for name in (
                        "matches",
                        "starts",
                        "appearances",
                        "substitute_appearances",
                        "zero_minute_records",
                        "starting_minutes",
                        "substitute_minutes",
                        "sixty_appearances",
                        "minutes",
                    ):
                        player[f"recent_{name}"] = int(recent.get(name) or 0)
                    player["recent_cross_season_matches"] = int(recent["matches"] or 0)
                else:
                    player["recent_cross_season_matches"] = 0
            else:
                player["recent_cross_season_matches"] = 0
        return result

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
        previous_league_average = sum(float(row["goals_for"]) for row in rows) / total_matches
        regression = self.config.carry_forward_regression_matches
        by_name = {
            str(row["name"]): (
                (float(row["goals_for"]) + regression * previous_league_average)
                / (int(row["matches"]) + regression),
                (float(row["goals_against"]) + regression * previous_league_average)
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
        promoted_attack = previous_league_average * self.config.promoted_team_attack_multiplier
        promoted_defence = previous_league_average * self.config.promoted_team_defence_multiplier
        # A club with no same-named entry in the previous top-flight season is
        # promoted and has nothing to carry forward. Under the incumbent every
        # such club takes the same declared prior; under the differentiated
        # mode each takes its own, derived from the division it came from.
        promoted_names = tuple(
            str(row["name"]) for row in current if str(row["name"]) not in by_name
        )
        promoted_rates = {
            name: (promoted_attack, promoted_defence) for name in promoted_names
        }
        if (
            self.config.promoted_prior_mode == "championship_relative"
            and promoted_names
        ):
            for name, prior in self._promoted_club_priors(
                previous_season_code=str(previous["code"]),
                promoted_names=promoted_names,
            ).items():
                promoted_rates[name] = (
                    previous_league_average * prior.attack_multiplier,
                    previous_league_average * prior.defence_multiplier,
                )
        return (
            {
                str(row["team_id"]): by_name.get(
                    str(row["name"]),
                    promoted_rates.get(
                        str(row["name"]), (promoted_attack, promoted_defence)
                    ),
                )
                for row in current
            },
            previous_league_average,
        )

    def _promoted_club_priors(
        self,
        *,
        previous_season_code: str,
        promoted_names: tuple[str, ...],
    ) -> dict[str, Any]:
        """Differentiated promoted priors from the previous Championship season.

        The Championship season read is the one that ran alongside the previous
        Premier League season, and it finished before the target season began,
        so it cannot leak into any origin inside the target season.
        """

        from .championship import promoted_club_priors

        return promoted_club_priors(
            self.database,
            championship_season_code=previous_season_code,
            promoted_fpl_names=promoted_names,
            weight=self.config.promoted_prior_weight,
            base_attack=self.config.promoted_team_attack_multiplier,
            base_defence=self.config.promoted_team_defence_multiplier,
            minimum_attack=self.config.promoted_prior_minimum_attack,
            maximum_attack=self.config.promoted_prior_maximum_attack,
            minimum_defence=self.config.promoted_prior_minimum_defence,
            maximum_defence=self.config.promoted_prior_maximum_defence,
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
            self.config.home_attack_multiplier if is_home else self.config.away_attack_multiplier
        )
        defence_venue = (
            self.config.away_attack_multiplier if is_home else self.config.home_attack_multiplier
        )
        scoring_factor = float(team["attack"]) * float(opponent["defence"]) * attack_venue
        return (
            scoring_factor,
            league_average * scoring_factor,
            league_average * float(opponent["attack"]) * float(team["defence"]) * defence_venue,
        )

    def fixture_expected_goals(
        self,
        *,
        season_code: str,
        gameweek_number: int,
        target_gameweek: int | None = None,
        team_overrides: tuple[TeamStrengthOverride, ...] = (),
        as_of: datetime | None = None,
        maximum_ingestion_run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every fixture in a Gameweek, with the goals this model expects.

        The public entry point the historical evaluation uses, so what is
        scored is what a projection at that origin would actually have used.

        `gameweek_number` is the *origin*: team strength reads only evidence
        from before it. `target_gameweek` defaults to the origin, which is the
        one-step case the in-season evaluation scores. Supplying a later target
        holds the origin's beliefs fixed and asks what they predicted for a
        Gameweek further out — which is the only honest way to score a
        preseason forecast, because a preseason forecast is not re-estimated
        each week.
        """

        target = gameweek_number if target_gameweek is None else target_gameweek
        if target < gameweek_number:
            raise ValueError(
                "A forecast target cannot precede the origin it was made at"
            )
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
            (season_code, target),
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
                    "origin_gameweek": gameweek_number,
                    "gameweek_number": target,
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
        by_source = {team.source_team_id: team_id for team_id, team in state.teams.items()}
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

    def _prepare_gameweek_participation(
        self,
        players: list[dict[str, Any]],
        *,
        start_gameweek: int,
        horizon_gameweeks: int,
        overrides: dict[tuple[str, int], ProjectionOverride],
        generated_at: datetime,
    ) -> None:
        """Resolve each Gameweek's participation before event allocation.

        Overrides are immutable inputs to a run. Their effective/expiry dates
        are evaluated against the forecast origin; the explicit Gameweek key
        prevents an override leaking into another Gameweek.
        """
        for player in players:
            by_gameweek: dict[int, dict[str, float]] = {}
            for offset, gameweek in enumerate(
                range(start_gameweek, start_gameweek + horizon_gameweeks)
            ):
                decay = (
                    (1.0 - self.config.participation_role_decay_per_gameweek) ** offset
                    if self.config.minutes_model == "participation_v1"
                    else 1.0
                )
                start = float(player["_start_probability"])
                sub = float(player["_substitute_probability"])
                if self.config.minutes_model == "participation_v1":
                    start = (
                        self.config.participation_start_prior_probability
                        + (start - self.config.participation_start_prior_probability) * decay
                    )
                    sub = (
                        self.config.participation_substitute_prior_probability
                        + (sub - self.config.participation_substitute_prior_probability) * decay
                    )
                state = {
                    "_availability": float(player["_availability"]),
                    "_start_probability": start,
                    "_substitute_probability": sub,
                    "_conditional_start_minutes": float(player["_conditional_start_minutes"]),
                    "_conditional_substitute_minutes": float(
                        player["_conditional_substitute_minutes"]
                    ),
                    "_penalty_role_probability": None,
                    "_sixty_probability_override": None,
                }
                override = overrides.get((str(player["source_player_id"]), gameweek))
                if self.config.minutes_model != "participation_v1" and override is None:
                    by_gameweek[gameweek] = {
                        "start_probability": state["_start_probability"],
                        "substitute_probability": state["_substitute_probability"],
                        "appearance_probability": float(player["_appearance_probability"]),
                        "no_appearance_probability": 1.0 - float(player["_appearance_probability"]),
                        "sixty_probability": float(player["_sixty_probability"]),
                        "expected_minutes": float(player["_expected_minutes_per_fixture"]),
                        "conditional_start_minutes": state["_conditional_start_minutes"],
                        "conditional_substitute_minutes": state["_conditional_substitute_minutes"],
                        "override_active": 0.0,
                        "penalty_role_probability": 0.0,
                    }
                    continue
                if override is not None and _override_active(override, generated_at):
                    if override.availability is not None:
                        state["_availability"] = _clamp(override.availability, 0.0, 1.0)
                    state["_availability"] = _clamp(
                        state["_availability"] * override.availability_multiplier
                        + override.availability_delta,
                        0.0,
                        1.0,
                    )
                    if override.start_probability is not None:
                        state["_start_probability"] = override.start_probability
                    state["_start_probability"] = _clamp(
                        state["_start_probability"] * override.start_probability_multiplier
                        + override.start_probability_delta,
                        0.0,
                        state["_availability"],
                    )
                    if override.substitute_appearance_probability is not None:
                        state["_substitute_probability"] = _clamp(
                            override.substitute_appearance_probability * state["_availability"],
                            0.0,
                            1.0,
                        )
                    if override.appearance_probability is not None:
                        _set_participation_appearance(
                            state,
                            _clamp(override.appearance_probability, 0.0, 1.0),
                        )
                    elif (
                        override.appearance_probability_delta != 0.0
                        or override.appearance_probability_multiplier != 1.0
                    ):
                        current_appearance = _participation_appearance(state)
                        _set_participation_appearance(
                            state,
                            _clamp(
                                current_appearance * override.appearance_probability_multiplier
                                + override.appearance_probability_delta,
                                0.0,
                                1.0,
                            ),
                        )
                    if override.conditional_start_minutes is not None:
                        state["_conditional_start_minutes"] = _clamp(
                            override.conditional_start_minutes, 0.0, 90.0
                        )
                    if override.conditional_substitute_minutes is not None:
                        state["_conditional_substitute_minutes"] = _clamp(
                            override.conditional_substitute_minutes, 0.0, 90.0
                        )
                    if override.penalty_role_probability is not None:
                        state["_penalty_role_probability"] = _clamp(
                            override.penalty_role_probability, 0.0, 1.0
                        )
                    if override.expected_minutes is not None:
                        _reconcile_participation_to_minutes(
                            state,
                            _clamp(
                                override.expected_minutes * override.expected_minutes_multiplier
                                + override.expected_minutes_delta,
                                0.0,
                                90.0,
                            ),
                        )
                    elif (
                        override.expected_minutes_delta != 0.0
                        or override.expected_minutes_multiplier != 1.0
                    ):
                        _reconcile_participation_to_minutes(
                            state,
                            _clamp(
                                _participation_minutes(state)
                                * override.expected_minutes_multiplier
                                + override.expected_minutes_delta,
                                0.0,
                                90.0,
                            ),
                        )
                _reconcile_participation_to_minutes(
                    state,
                    _participation_minutes(state),
                )
                if override is not None and _override_active(override, generated_at):
                    if override.sixty_probability is not None:
                        state["_sixty_probability_override"] = _clamp(
                            override.sixty_probability, 0.0, state["_appearance_probability"]
                        )
                    elif (
                        override.sixty_probability_delta != 0.0
                        or override.sixty_probability_multiplier != 1.0
                    ):
                        state["_sixty_probability_override"] = _clamp(
                            state["_sixty_probability"]
                            * override.sixty_probability_multiplier
                            + override.sixty_probability_delta,
                            0.0,
                            state["_appearance_probability"],
                        )
                    if state["_sixty_probability_override"] is not None:
                        state["_sixty_probability"] = state["_sixty_probability_override"]
                by_gameweek[gameweek] = {
                    "start_probability": state["_start_probability"],
                    "substitute_probability": state["_substitute_probability"],
                    "appearance_probability": state["_appearance_probability"],
                    "no_appearance_probability": state["_no_appearance_probability"],
                    "sixty_probability": state["_sixty_probability"],
                    "expected_minutes": state["_expected_minutes_per_fixture"],
                    "conditional_start_minutes": state["_conditional_start_minutes"],
                    "conditional_substitute_minutes": state["_conditional_substitute_minutes"],
                    "override_active": float(
                        override is not None and _override_active(override, generated_at)
                    ),
                    "penalty_role_probability": (
                        0.0
                        if state["_penalty_role_probability"] is None
                        else float(state["_penalty_role_probability"])
                    ),
                }
            player["_participation_by_gameweek"] = by_gameweek

    def _prepare_coherent_event_allocations(
        self,
        players: list[dict[str, Any]],
        fixtures: dict[int, list[dict[str, Any]]],
        strengths: dict[str, dict[str, float]],
        *,
        start_gameweek: int,
        horizon_gameweeks: int,
    ) -> None:
        """Allocate the fixture team expectation once, then share it.

        The incumbent path estimates player events independently.  This
        challenger deliberately reverses that order: a fixture has one team
        goal expectation and player shares are conditional on participation.
        The resulting allocation is stored on each prepared player and later
        consumed without another team-strength multiplier.
        """
        by_team: dict[str, list[dict[str, Any]]] = {}
        for player in players:
            by_team.setdefault(str(player["team_id"]), []).append(player)
            player["_coherent_by_gameweek"] = {}
        prior_minutes = self.config.coherent_role_shrinkage_minutes
        for gameweek in range(start_gameweek, start_gameweek + horizon_gameweeks):
            for fixture in fixtures.get(gameweek, ()):
                home_id = str(fixture["home_team_id"])
                away_id = str(fixture["away_team_id"])
                for team_id, opponent_id, is_home in (
                    (home_id, away_id, True),
                    (away_id, home_id, False),
                ):
                    _, team_lambda, _ = self.fixture_lambdas(
                        strengths,
                        team_id=team_id,
                        opponent_id=opponent_id,
                        is_home=is_home,
                    )
                    team_players = by_team.get(team_id, ())
                    goal_weights: list[tuple[dict[str, Any], float]] = []
                    assist_weights: list[tuple[dict[str, Any], float]] = []
                    for player in team_players:
                        position = Position(player["position"])
                        sample_minutes = float(player["minutes"])
                        expected_xg = max(0.0, float(player["expected_goals"]))
                        expected_xa = max(0.0, float(player["expected_assists"]))
                        # Historical role rates are priors, not totals.  The
                        # minutes shrinkage prevents tiny samples from taking
                        # an extreme share; price only enters the prior path.
                        goal_rate = (
                            expected_xg * 90.0 + POSITION_PRIORS[position]["goals"] * prior_minutes
                        ) / (sample_minutes + prior_minutes)
                        assist_rate = (
                            expected_xa * 90.0
                            + POSITION_PRIORS[position]["assists"] * prior_minutes
                        ) / (sample_minutes + prior_minutes)
                        if bool(player.get("current_transfer", False)):
                            transfer_shrinkage = self.config.coherent_transfer_shrinkage
                            goal_rate = (
                                1.0 - transfer_shrinkage
                            ) * goal_rate + transfer_shrinkage * POSITION_PRIORS[position]["goals"]
                            assist_rate = (
                                1.0 - transfer_shrinkage
                            ) * assist_rate + transfer_shrinkage * POSITION_PRIORS[position][
                                "assists"
                            ]
                        gameweek_participation = player.get("_participation_by_gameweek", {}).get(
                            gameweek, {}
                        )
                        participation = _clamp(
                            float(
                                gameweek_participation.get(
                                    "expected_minutes",
                                    player.get("_expected_minutes_per_fixture", 0.0),
                                )
                            )
                            / 90.0,
                            0.0,
                            1.0,
                        )
                        if bool(player.get("_role_unknown", False)):
                            participation *= 0.80
                        goal_weights.append((player, max(0.0, goal_rate * participation)))
                        assist_weights.append((player, max(0.0, assist_rate * participation)))
                    total_goal_weight = sum(weight for _, weight in goal_weights)
                    total_assist_weight = sum(weight for _, weight in assist_weights)
                    goal_shares = {
                        id(player): weight / total_goal_weight if total_goal_weight else 0.0
                        for player, weight in goal_weights
                    }
                    assist_shares = {
                        id(player): weight / total_assist_weight if total_assist_weight else 0.0
                        for player, weight in assist_weights
                    }
                    explicit_penalty_weights = {
                        id(player): float(
                            player.get("_participation_by_gameweek", {})
                            .get(gameweek, {})
                            .get("penalty_role_probability", 0.0)
                        )
                        for player in team_players
                    }
                    eligible_penalty_players = [
                        player
                        for player in team_players
                        if float(
                            player.get("_participation_by_gameweek", {})
                            .get(gameweek, {})
                            .get(
                                "appearance_probability",
                                player.get("_appearance_probability", 0.0),
                            )
                        )
                        > 0
                    ]
                    total_explicit_penalty = sum(explicit_penalty_weights.values())
                    penalty_shares = (
                        {
                            id(player): explicit_penalty_weights[id(player)]
                            / total_explicit_penalty
                            for player in team_players
                        }
                        if total_explicit_penalty > 0
                        else {
                            id(player): 1.0 / len(eligible_penalty_players)
                            if player in eligible_penalty_players
                            else 0.0
                            for player in team_players
                        }
                        if eligible_penalty_players
                        else {}
                    )
                    penalty_total = team_lambda * self.config.coherent_penalty_goal_fraction
                    open_play_total = team_lambda - penalty_total
                    assisted_total = team_lambda * (
                        1.0 - self.config.coherent_assist_unassisted_goal_fraction
                    )
                    for player in team_players:
                        goal_share = goal_shares.get(id(player), 0.0)
                        assist_share = assist_shares.get(id(player), 0.0)
                        row = player["_coherent_by_gameweek"].setdefault(
                            gameweek,
                            {
                                "goals": 0.0,
                                "assists": 0.0,
                                "team_expected_goals": 0.0,
                                "expected_assisted_goals": 0.0,
                                "goal_share": 0.0,
                                "assist_share": 0.0,
                                "penalty_share": 0.0,
                            },
                        )
                        penalty_share = penalty_shares.get(id(player), 0.0)
                        row["goals"] += open_play_total * goal_share + penalty_total * penalty_share
                        row["open_play_goals"] = (
                            row.get("open_play_goals", 0.0) + open_play_total * goal_share
                        )
                        row["penalty_goals"] = (
                            row.get("penalty_goals", 0.0) + penalty_total * penalty_share
                        )
                        row["assists"] += assisted_total * assist_share
                        row["team_expected_goals"] += team_lambda
                        row["expected_assisted_goals"] += assisted_total
                        row["goal_share"] += goal_share
                        row["assist_share"] += assist_share
                        row["penalty_share"] += (
                            penalty_total * penalty_share / team_lambda if team_lambda > 0 else 0.0
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
            by_position.setdefault(str(player["position"]), []).append(int(player["price_tenths"]))
        medians = {
            position: sorted(prices)[len(prices) // 2] for position, prices in by_position.items()
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
                expected_minutes = appearance_probability * conditional_minutes
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
            elif self.config.minutes_model == "participation_v1":
                extra_recent_weight = self.config.recent_evidence_weight - 1.0
                weighted_matches = float(player["matches"]) + extra_recent_weight * float(
                    player["recent_matches"]
                )
                weighted_starts = float(player.get("starts", 0.0)) + extra_recent_weight * float(
                    player.get("recent_starts", 0.0)
                )
                weighted_substitute_appearances = float(
                    player.get("substitute_appearances", 0.0)
                ) + extra_recent_weight * float(player.get("recent_substitute_appearances", 0.0))
                weighted_unused_records = float(
                    player.get("zero_minute_records", 0.0)
                ) + extra_recent_weight * float(player.get("recent_zero_minute_records", 0.0))
                weighted_starting_minutes = float(
                    player.get("starting_minutes", 0.0)
                ) + extra_recent_weight * float(player.get("recent_starting_minutes", 0.0))
                weighted_substitute_minutes = float(
                    player.get("substitute_minutes", 0.0)
                ) + extra_recent_weight * float(player.get("recent_substitute_minutes", 0.0))
                role_unknown = weighted_matches < 3 or (
                    float(player.get("recent_matches", 0.0)) == 0
                    and weighted_starts / max(weighted_matches, 1.0) < 0.35
                )
                start_probability = (
                    weighted_starts
                    + self.config.participation_start_prior_matches
                    * self.config.participation_start_prior_probability
                ) / (weighted_matches + self.config.participation_start_prior_matches)
                start_probability = _clamp(start_probability * availability, 0.0, 1.0)
                non_start_matches = max(
                    weighted_substitute_appearances + weighted_unused_records,
                    weighted_matches - weighted_starts,
                    0.0,
                )
                substitute_probability = (
                    weighted_substitute_appearances
                    + self.config.participation_substitute_prior_matches
                    * self.config.participation_substitute_prior_probability
                ) / (non_start_matches + self.config.participation_substitute_prior_matches)
                substitute_probability = _clamp(substitute_probability * availability, 0.0, 1.0)
                if role_unknown:
                    start_probability = (
                        _clamp(
                            0.60 * start_probability
                            + 0.40 * self.config.participation_start_prior_probability,
                            0.05,
                            0.80,
                        )
                        * availability
                    )
                    substitute_probability = (
                        _clamp(
                            0.60 * substitute_probability
                            + 0.40 * self.config.participation_substitute_prior_probability,
                            0.02,
                            0.55,
                        )
                        * availability
                    )
                conditional_prior = self.config.conditional_minutes_prior_appearances
                start_minutes = _clamp(
                    (
                        weighted_starting_minutes
                        + conditional_prior * self.config.participation_start_minutes_prior
                    )
                    / (weighted_starts + conditional_prior),
                    1.0,
                    90.0,
                )
                substitute_minutes = _clamp(
                    (
                        weighted_substitute_minutes
                        + conditional_prior * self.config.participation_substitute_minutes_prior
                    )
                    / (weighted_substitute_appearances + conditional_prior),
                    1.0,
                    90.0,
                )
                conditional_minutes = (
                    start_probability * start_minutes
                    + (1.0 - start_probability) * substitute_probability * substitute_minutes
                ) / max(
                    start_probability + (1.0 - start_probability) * substitute_probability,
                    1e-9,
                )
                appearance_probability = (
                    start_probability + (1.0 - start_probability) * substitute_probability
                )
                start_sixty = _clamp((start_minutes - 45.0) / 30.0, 0.0, 1.0)
                sub_sixty = 0.0
                sixty_probability = (
                    start_probability * start_sixty
                    + (1.0 - start_probability) * substitute_probability * sub_sixty
                )
                sixty_given_appearance = (
                    sixty_probability / appearance_probability
                    if appearance_probability > 0
                    else 0.0
                )
                expected_minutes = (
                    start_probability * start_minutes
                    + (1.0 - start_probability) * substitute_probability * substitute_minutes
                )
                player["_start_probability"] = start_probability
                player["_substitute_probability"] = substitute_probability
                player["_conditional_start_minutes"] = start_minutes
                player["_conditional_substitute_minutes"] = substitute_minutes
                player["_no_appearance_probability"] = 1.0 - appearance_probability
                player["_role_unknown"] = role_unknown
                player["_role_evidence"] = (
                    "unknown_role: insufficient starts/minutes evidence"
                    if role_unknown
                    else "historical starts and minutes"
                )
                player["_participation_evidence"] = {
                    "starts": weighted_starts,
                    "substitute_appearances": weighted_substitute_appearances,
                    "unused_substitute_records": weighted_unused_records,
                    "starting_minutes": weighted_starting_minutes,
                    "substitute_minutes": weighted_substitute_minutes,
                    "conditional_minutes_prior_weight": conditional_prior,
                }
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
            player.setdefault("_start_probability", appearance_probability)
            player.setdefault("_substitute_probability", 0.0)
            player.setdefault("_conditional_start_minutes", conditional_minutes)
            player.setdefault("_conditional_substitute_minutes", 0.0)
            player.setdefault("_no_appearance_probability", 1.0 - appearance_probability)
            player.setdefault("_role_unknown", False)
            player.setdefault("_role_evidence", "historical appearance/minutes evidence")
            player.setdefault("_reconciliation_adjustment", 0.0)
            player.setdefault("_unresolved_minutes", 0.0)

        if self.config.minutes_model == "legacy" or not self.config.enforce_team_minutes:
            self._calibrate_appearance(players)
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
                if self.config.minutes_reconciliation_mode == "bounded_role_preserving":
                    allocations = _bounded_minutes_reconciliation(
                        group_players,
                        target=target,
                        max_relative=self.config.minutes_reconciliation_max_relative_adjustment,
                        max_absolute=self.config.minutes_reconciliation_max_absolute_adjustment,
                    )
                else:
                    allocations = _allocate_capped_minutes(
                        [
                            float(player["_expected_minutes_per_fixture"])
                            for player in group_players
                        ],
                        target=target,
                        cap=90.0,
                    )
                reconciled.extend(zip(group_players, allocations, strict=True))
            for player, expected_minutes in reconciled:
                prior_expected = float(player["_expected_minutes_per_fixture"])
                if (
                    self.config.minutes_reconciliation_mode == "bounded_role_preserving"
                    and self.config.minutes_model == "participation_v1"
                ):
                    player["_unresolved_minutes"] = 0.0
                    expected_minutes = _reconcile_participation_to_minutes(
                        player,
                        expected_minutes,
                    )
                    player["_reconciliation_adjustment"] = expected_minutes - prior_expected
                    conditional_minutes = float(player["_conditional_minutes"])
                    appearance_probability = float(player["_appearance_probability"])
                elif self.config.minutes_reconciliation_preserves_appearance:
                    # Keep the estimated availability and move the team-budget
                    # correction into conditional minutes. Whatever the
                    # 90-minute ceiling cannot absorb is reported as unresolved
                    # rather than fabricated into a higher appearance
                    # probability.
                    allocated = expected_minutes
                    appearance_probability = float(player["_appearance_probability"])
                    if appearance_probability <= 0.0:
                        conditional_minutes = float(player["_conditional_minutes"])
                        expected_minutes = 0.0
                    else:
                        conditional_minutes = _clamp(
                            allocated / appearance_probability, 1.0, 90.0
                        )
                        expected_minutes = appearance_probability * conditional_minutes
                    player["_conditional_minutes"] = conditional_minutes
                    player["_unresolved_minutes"] = max(0.0, allocated - expected_minutes)
                    player["_reconciliation_adjustment"] = expected_minutes - prior_expected
                else:
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
            if self.config.minutes_reconciliation_mode == "bounded_role_preserving":
                residual = max(
                    0.0,
                    target
                    - sum(float(item["_expected_minutes_per_fixture"]) for item in group_players),
                )
                for index, player in enumerate(group_players):
                    player["_unresolved_minutes"] = residual if index == 0 else 0.0
                    player["_reconciliation_warning"] = (
                        residual > self.config.minutes_reconciliation_warning_deficit
                    )

        self._calibrate_appearance(players)

    def _calibrate_appearance(self, players: list[dict[str, Any]]) -> None:
        """Map raw appearance probabilities through a fitted reliability curve.

        The estimator is systematically overconfident about apparent nailed
        starters at a preseason origin — the band it calls certain appears
        about four times in five. An isotonic map fitted on historical
        origins of the *same* configuration corrects that without disturbing
        the ordering, because isotonic regression is monotone.

        The map is applied after reconciliation, so a calibrated run no longer
        forces each club to exactly the fixture minutes budget. That is
        deliberate: the budget is the weaker claim, and silently restoring it
        would reintroduce the distortion the calibration is correcting.
        """

        artifact = self.config.appearance_calibration_artifact
        if not artifact:
            return
        knots = _appearance_calibration_knots(str(artifact))
        for player in players:
            raw = float(player["_appearance_probability"])
            calibrated = _interpolate(knots, raw)
            player["_appearance_probability"] = calibrated
            player["_no_appearance_probability"] = 1.0 - calibrated
            player["_sixty_probability"] = min(
                calibrated,
                calibrated * float(player["_sixty_given_appearance"]),
            )
            player["_expected_minutes_per_fixture"] = calibrated * float(
                player["_conditional_minutes"]
            )
            player["_start_probability"] = min(
                calibrated, float(player.get("_start_probability", calibrated))
            )
            player["_appearance_calibration_shift"] = calibrated - raw

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
                    else prior[name] * (price_factor if name in COLD_START_SCALED_RATES else 1.0)
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
            state = player.get("_participation_by_gameweek", {}).get(gameweek, {})
            start_probability = float(
                state.get(
                    "start_probability", player.get("_start_probability", appearance_probability)
                )
            )
            substitute_probability = float(
                state.get("substitute_probability", player.get("_substitute_probability", 0.0))
            )
            conditional_start_minutes = float(
                state.get(
                    "conditional_start_minutes",
                    player.get("_conditional_start_minutes", conditional_minutes),
                )
            )
            conditional_substitute_minutes = float(
                state.get(
                    "conditional_substitute_minutes",
                    player.get("_conditional_substitute_minutes", 0.0),
                )
            )
            fixture_appearance_probability = float(
                state.get("appearance_probability", appearance_probability)
            )
            fixture_sixty_probability = float(state.get("sixty_probability", sixty_probability))
            per_fixture_minutes = float(state.get("expected_minutes", minutes_per_fixture))
            expected_minutes = per_fixture_minutes * fixture_count
            per_fixture_minutes = 0.0 if fixture_count == 0 else expected_minutes / fixture_count
            conditional_minutes = (
                per_fixture_minutes / fixture_appearance_probability
                if fixture_appearance_probability > 0
                else 0.0
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
            expected_goals_events = 0.0
            expected_assists_events = 0.0
            latent = {
                "team_expected_goals": 0.0,
                "opponent_expected_goals": 0.0,
                "goal_share": float(player.get("_goal_share", 0.0)),
                "assist_share": float(player.get("_assist_share", 0.0)),
            }
            coherent_events = player.get("_coherent_by_gameweek", {}).get(gameweek, {})
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
                    expected_goals_events += team_lambda * float(player["_goal_share"])
                    expected_assists_events += (
                        team_lambda
                        * team_strength["assist_per_goal"]
                        * float(player["_assist_share"])
                    )
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
                elif self.config.scoring_event_source == "coherent_team_allocation":
                    # Added once below from the fixture-level allocation.  A
                    # DGW has multiple fixtures, so adding this inside the
                    # loop would double count the already aggregated share.
                    pass
                else:
                    expected_goals_events += rates["goals"] * minute_factor * scoring_factor
                    expected_assists_events += rates["assists"] * minute_factor * scoring_factor
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
                elif self.config.scoring_event_source == "coherent_team_allocation":
                    fixture_notes.append(
                        f"coherent team xG {team_lambda:.2f}, allocated share "
                        f"{float(coherent_events.get('goal_share', 0.0)):.3f}"
                    )
            if self.config.scoring_event_source == "coherent_team_allocation":
                expected_goals_events = float(coherent_events.get("goals", 0.0))
                expected_assists_events = float(coherent_events.get("assists", 0.0))
                components["goal"] += (
                    float(coherent_events.get("goals", 0.0))
                    * self.rules.scoring.goals[position.value]
                )
                components["assist"] += (
                    float(coherent_events.get("assists", 0.0)) * self.rules.scoring.assists
                )
                if coherent_events.get("team_expected_goals", 0.0):
                    latent["goal_share"] = float(coherent_events.get("goals", 0.0)) / float(
                        coherent_events["team_expected_goals"]
                    )
                    latent["assist_share"] = float(coherent_events.get("assists", 0.0)) / max(
                        float(coherent_events.get("expected_assisted_goals", 0.0)),
                        1e-9,
                    )
                latent["expected_assisted_goals"] = float(
                    coherent_events.get("expected_assisted_goals", 0.0)
                )
                latent["penalty_share"] = float(coherent_events.get("penalty_share", 0.0))
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
                    modifier_ids=() if override is None else override.modifier_ids,
                    latent_expectations={name: round(value, 8) for name, value in latent.items()},
                    start_probability=round(start_probability, 6),
                    substitute_appearance_probability=round(
                        (1.0 - start_probability) * substitute_probability,
                        6,
                    ),
                    no_appearance_probability=round(
                        max(
                            0.0,
                            1.0
                            - start_probability
                            - (1.0 - start_probability) * substitute_probability,
                        ),
                        6,
                    ),
                    expected_minutes_if_start=round(conditional_start_minutes, 3),
                    expected_minutes_if_substitute=round(conditional_substitute_minutes, 3),
                    sixty_minute_probability=round(fixture_sixty_probability, 6),
                    role_unknown=bool(player.get("_role_unknown", False)),
                    role_evidence_source=str(
                        player.get("_role_evidence", "historical appearance/minutes evidence")
                    ),
                    reconciliation_adjustment=round(
                        float(player.get("_reconciliation_adjustment", 0.0)),
                        3,
                    ),
                    unresolved_minutes=round(
                        float(player.get("_unresolved_minutes", 0.0)),
                        3,
                    ),
                    reconciliation_warning=bool(player.get("_reconciliation_warning", False)),
                    goal_share=round(float(latent.get("goal_share", 0.0)), 8),
                    assist_share=round(float(latent.get("assist_share", 0.0)), 8),
                    penalty_share=round(float(latent.get("penalty_share", 0.0)), 8),
                    team_expected_goals=round(float(latent.get("team_expected_goals", 0.0)), 8),
                    expected_assisted_goals=round(
                        float(latent.get("expected_assisted_goals", 0.0)),
                        8,
                    ),
                    expected_goals=round(expected_goals_events, 8),
                    expected_assists=round(expected_assists_events, 8),
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
            assumptions["team_strength_state"] = self._last_team_strength_state.as_dict()
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
                        json.dumps(
                            _persisted_projection_assumptions(
                                projection,
                                rules=self.rules,
                                config_hash=_config_hash(self.config),
                            ),
                            sort_keys=True,
                        ),
                        projection.override_rationale,
                    )
                    for projection in projections
                ),
            )
        return run_id


def _persisted_projection_assumptions(
    projection: PlayerGameweekProjection,
    *,
    rules: SeasonRules,
    config_hash: str,
) -> dict[str, Any]:
    return {
        "notes": projection.assumptions,
        "modifier_ids": list(projection.modifier_ids),
        "participation": {
            "start_probability": projection.start_probability,
            "substitute_appearance_probability": projection.substitute_appearance_probability,
            "no_appearance_probability": projection.no_appearance_probability,
            "expected_minutes_if_start": projection.expected_minutes_if_start,
            "expected_minutes_if_substitute": projection.expected_minutes_if_substitute,
            "sixty_minute_probability": projection.sixty_minute_probability,
            "role_unknown": projection.role_unknown,
            "role_evidence_source": projection.role_evidence_source,
            "reconciliation_adjustment": projection.reconciliation_adjustment,
            "unresolved_minutes": projection.unresolved_minutes,
            "reconciliation_warning": projection.reconciliation_warning,
        },
        "allocation": {
            "team_expected_goals": projection.team_expected_goals,
            "expected_assisted_goals": projection.expected_assisted_goals,
            "goal_share": projection.goal_share,
            "assist_share": projection.assist_share,
            "penalty_share": projection.penalty_share,
            "expected_goals": projection.expected_goals,
            "expected_assists": projection.expected_assists,
        },
        "model_config_hash": config_hash,
    }


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


def coherence_diagnostics(
    projections: tuple[PlayerGameweekProjection, ...],
    rules: SeasonRules,
) -> list[dict[str, Any]]:
    """Return team/Gameweek reconciliation diagnostics for reports/tests."""
    grouped: dict[tuple[str, int], dict[str, float]] = {}
    for projection in projections:
        key = (projection.team_short_name, projection.gameweek_number)
        row = grouped.setdefault(
            key,
            {
                "team_expected_goals": 0.0,
                "player_expected_goals": 0.0,
                "expected_assisted_goals": 0.0,
                "player_expected_assists": 0.0,
                "max_role_unknown": 0.0,
            },
        )
        row["team_expected_goals"] = max(row["team_expected_goals"], projection.team_expected_goals)
        row["expected_assisted_goals"] = max(
            row["expected_assisted_goals"], projection.expected_assisted_goals
        )
        row["player_expected_goals"] += projection.expected_goals
        row["player_expected_assists"] += projection.expected_assists
        row["max_role_unknown"] = max(row["max_role_unknown"], float(projection.role_unknown))
    output = []
    for (team, gameweek), row in sorted(grouped.items()):
        row = dict(row)
        row.update(
            {
                "team_short_name": team,
                "gameweek_number": gameweek,
                "goal_reconciliation_error": row["player_expected_goals"]
                - row["team_expected_goals"],
                "assist_reconciliation_error": row["player_expected_assists"]
                - row["expected_assisted_goals"],
            }
        )
        output.append(row)
    return output


def minutes_diagnostics(
    projections: tuple[PlayerGameweekProjection, ...],
) -> list[dict[str, Any]]:
    """Summarize raw/reconciled minutes and unresolved deficits by team/GW."""
    grouped: dict[tuple[str, int], dict[str, float]] = {}
    for projection in projections:
        key = (projection.team_short_name, projection.gameweek_number)
        row = grouped.setdefault(
            key,
            {
                "raw_expected_minutes": 0.0,
                "reconciled_expected_minutes": 0.0,
                "unresolved_minutes": 0.0,
                "reconciliation_adjustment": 0.0,
                "warnings": 0.0,
            },
        )
        row["reconciled_expected_minutes"] += projection.expected_minutes
        row["reconciliation_adjustment"] += projection.reconciliation_adjustment
        row["raw_expected_minutes"] += (
            projection.expected_minutes - projection.reconciliation_adjustment
        )
        row["unresolved_minutes"] += projection.unresolved_minutes
        row["warnings"] += float(projection.reconciliation_warning)
    return [
        {
            "team_short_name": team,
            "gameweek_number": gameweek,
            **{name: round(value, 6) for name, value in values.items()},
        }
        for (team, gameweek), values in sorted(grouped.items())
    ]


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


def _bounded_minutes_reconciliation(
    players: list[dict[str, Any]],
    *,
    target: float,
    max_relative: float,
    max_absolute: float,
) -> list[float]:
    """Apply a bounded, role-preserving adjustment to player minutes.

    This is intentionally a soft consistency correction.  If the target is
    unreachable within the configured bounds, the residual remains visible
    to diagnostics instead of being redistributed into fringe players.
    """
    raw = [
        _clamp(float(player.get("_expected_minutes_per_fixture", 0.0)), 0.0, 90.0)
        for player in players
    ]
    if not raw:
        return []
    current = sum(raw)
    delta = target - current
    if abs(delta) < 1e-9:
        return raw
    total_weight = sum(raw) or float(len(raw))
    result = []
    for player, value in zip(players, raw, strict=True):
        weight = value / total_weight if total_weight else 1.0 / len(raw)
        limit = min(max_absolute, max_relative * max(value, 1.0))
        if float(player.get("_availability", 1.0)) <= 0.0:
            limit = 0.0
        adjusted = value + delta * weight
        adjusted = _clamp(adjusted, value - limit, value + limit)
        result.append(_clamp(adjusted, 0.0, 90.0))
    return result


def _reconcile_participation_to_minutes(
    player: dict[str, Any],
    target_minutes: float,
) -> float:
    """Change participation routes in a defined order and return valid minutes.

    Start probability is adjusted first, then substitute probability only if a
    reachable target remains. Derived appearance, no-appearance, 60-minute
    probability and unconditional minutes are recomputed together.
    """
    if float(player.get("_availability", 1.0)) <= 0.0:
        player["_start_probability"] = 0.0
        player["_substitute_probability"] = 0.0
        player["_appearance_probability"] = 0.0
        player["_no_appearance_probability"] = 1.0
        player["_sixty_probability"] = 0.0
        player["_expected_minutes_per_fixture"] = 0.0
        return 0.0
    start_minutes = float(player["_conditional_start_minutes"])
    substitute_minutes = float(player["_conditional_substitute_minutes"])
    substitute_probability = float(player["_substitute_probability"])
    denominator = start_minutes - substitute_probability * substitute_minutes
    start_probability = (
        (target_minutes - substitute_probability * substitute_minutes) / denominator
        if abs(denominator) > 1e-9
        else float(player["_start_probability"])
    )
    start_probability = _clamp(start_probability, 0.0, 1.0)
    remaining = target_minutes - start_probability * start_minutes
    substitute_probability = (
        remaining / ((1.0 - start_probability) * substitute_minutes)
        if (1.0 - start_probability) * substitute_minutes > 1e-9
        else 0.0
    )
    substitute_probability = _clamp(substitute_probability, 0.0, 1.0)
    appearance_probability = start_probability + (1.0 - start_probability) * substitute_probability
    expected_minutes = (
        start_probability * start_minutes
        + (1.0 - start_probability) * substitute_probability * substitute_minutes
    )
    sixty_probability = start_probability * _clamp(
        (start_minutes - 45.0) / 30.0,
        0.0,
        1.0,
    )
    player["_start_probability"] = start_probability
    player["_substitute_probability"] = substitute_probability
    player["_appearance_probability"] = appearance_probability
    player["_no_appearance_probability"] = 1.0 - appearance_probability
    player["_sixty_probability"] = min(appearance_probability, sixty_probability)
    player["_sixty_given_appearance"] = (
        player["_sixty_probability"] / appearance_probability if appearance_probability > 0 else 0.0
    )
    player["_expected_minutes_per_fixture"] = expected_minutes
    return expected_minutes


def _participation_minutes(state: dict[str, Any]) -> float:
    start = float(state["_start_probability"])
    substitute = float(state["_substitute_probability"])
    return start * float(state["_conditional_start_minutes"]) + (1.0 - start) * substitute * float(
        state["_conditional_substitute_minutes"]
    )


def _participation_appearance(state: dict[str, Any]) -> float:
    return _clamp(
        float(state["_start_probability"])
        + (1.0 - float(state["_start_probability"]))
        * float(state["_substitute_probability"]),
        0.0,
        1.0,
    )


def _set_participation_appearance(state: dict[str, Any], target: float) -> None:
    """Set appearance while preserving the existing start/sub route split."""

    target = _clamp(target, 0.0, float(state.get("_availability", 1.0)))
    current = _participation_appearance(state)
    if current <= 1e-9:
        state["_start_probability"] = target
        state["_substitute_probability"] = 0.0
        return
    start_share = float(state["_start_probability"]) / current
    start = min(target, target * start_share)
    substitute = (
        (target - start) / (1.0 - start)
        if 1.0 - start > 1e-9
        else 0.0
    )
    state["_start_probability"] = _clamp(start, 0.0, 1.0)
    state["_substitute_probability"] = _clamp(substitute, 0.0, 1.0)


def _override_active(override: ProjectionOverride, generated_at: datetime) -> bool:
    """Enforce optional ISO effective/expiry bounds without guessing dates."""
    for value, is_expiry in (
        (override.effective_from, False),
        (override.expires_at, True),
    ):
        if value is None:
            continue
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Projection override dates must be timezone-aware")
        if not is_expiry and generated_at < parsed:
            return False
        if is_expiry and generated_at >= parsed:
            return False
    return True


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
