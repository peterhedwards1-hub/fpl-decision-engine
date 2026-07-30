"""Joint Monte Carlo squad simulation with shared fixture outcomes."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import TYPE_CHECKING

from .config import SeasonRules
from .domain import Chip, Player, PlayerGameweekStats, Squad
from .history.database import HistoricalDatabase
from .rules import (
    allocate_bonus_points,
    calculate_player_points,
    calculate_team_score,
)

if TYPE_CHECKING:
    from .projections import ProjectionResult


@dataclass(frozen=True)
class FixtureSimulationInput:
    fixture_id: str
    home_team_id: int
    away_team_id: int
    home_expected_goals: float
    away_expected_goals: float
    assist_probability_per_goal: float = 0.72


@dataclass(frozen=True)
class PlayerSimulationInput:
    player: Player
    appearance_probability: float
    sixty_probability_given_appearance: float
    conditional_minutes: float
    goal_share: float
    assist_share: float
    saves_per_90: float = 0.0
    defensive_contributions_per_90: float = 0.0
    defensive_dispersion: float = 2.0
    yellow_card_probability: float = 0.0
    red_card_probability: float = 0.0
    own_goal_probability: float = 0.0
    penalty_save_probability: float = 0.0
    penalty_miss_probability: float = 0.0


@dataclass(frozen=True)
class SquadSimulationInput:
    name: str
    squad: Squad
    active_chip: Chip | None = None


@dataclass(frozen=True)
class DistributionSummary:
    samples: int
    mean: float
    standard_deviation: float
    p05: float
    p25: float
    median: float
    p75: float
    p95: float
    probability_below_40: float
    probability_at_least_60: float


@dataclass(frozen=True)
class SquadSimulationResult:
    seed: int
    iterations: int
    distributions: dict[str, DistributionSummary]
    pairwise_win_probabilities: dict[str, float]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class DistributionForecastOutcome:
    samples: tuple[int, ...]
    actual_points: int


@dataclass(frozen=True)
class SimulationCalibrationReport:
    forecasts: int
    mean_crps: float
    coverage_50: float
    coverage_80: float
    coverage_95: float
    threshold_40_brier: float
    threshold_60_brier: float
    pit_bins: tuple[int, ...]


def simulation_inputs_from_projection(
    database: HistoricalDatabase,
    projection_result: ProjectionResult,
    *,
    season_code: str,
    gameweek_number: int,
    rules: SeasonRules,
) -> tuple[
    tuple[FixtureSimulationInput, ...],
    tuple[PlayerSimulationInput, ...],
]:
    """Translate a coherent single-fixture projection into simulator inputs."""

    from .projections import DEFENSIVE_CONTRIBUTION_COUNT_PRIORS

    rows = database.connection.execute(
        """
        SELECT fixtures.source_fixture_id,
               fixtures.home_team_id, fixtures.away_team_id
        FROM fixtures
        JOIN seasons ON seasons.id = fixtures.season_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        WHERE seasons.code = ? AND gameweeks.number = ?
        ORDER BY fixtures.id
        """,
        (season_code, gameweek_number),
    ).fetchall()
    strengths = projection_result.team_strengths
    config = projection_result.model_config
    fixtures = []
    for row in rows:
        home_id = str(row["home_team_id"])
        away_id = str(row["away_team_id"])
        home = strengths[home_id]
        away = strengths[away_id]
        league_average = float(home["league_average_goals"])
        fixtures.append(
            FixtureSimulationInput(
                fixture_id=str(row["source_fixture_id"]),
                home_team_id=int(home_id),
                away_team_id=int(away_id),
                home_expected_goals=(
                    league_average
                    * home["attack"]
                    * away["defence"]
                    * config.home_attack_multiplier
                ),
                away_expected_goals=(
                    league_average
                    * away["attack"]
                    * home["defence"]
                    * config.away_attack_multiplier
                ),
                assist_probability_per_goal=float(
                    home.get(
                        "assist_per_goal",
                        config.team_assist_per_goal_prior,
                    )
                ),
            )
        )
    metadata = database.connection.execute(
        """
        WITH latest AS (
            SELECT observations.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY observations.player_season_id
                       ORDER BY observations.id DESC
                   ) AS observation_rank
            FROM player_gameweek_observations observations
            JOIN player_seasons
              ON player_seasons.id = observations.player_season_id
            JOIN seasons ON seasons.id = player_seasons.season_id
            WHERE seasons.code = ?
        )
        SELECT player_seasons.source_player_id, player_seasons.team_id,
               player_seasons.position, players.web_name,
               COALESCE(latest.price_tenths,
                        player_seasons.start_price_tenths, 50)
                   AS price_tenths
        FROM player_seasons
        JOIN seasons ON seasons.id = player_seasons.season_id
        JOIN players ON players.id = player_seasons.player_id
        LEFT JOIN latest
          ON latest.player_season_id = player_seasons.id
         AND latest.observation_rank = 1
        WHERE seasons.code = ?
        """,
        (season_code, season_code),
    ).fetchall()
    by_source = {str(row["source_player_id"]): row for row in metadata}
    player_inputs = []
    for projection in projection_result.projections:
        if projection.gameweek_number != gameweek_number:
            continue
        if projection.fixture_count != 1:
            raise ValueError(
                "Projection-to-simulation translation currently requires "
                "one fixture per player"
            )
        row = by_source[projection.source_player_id]
        appearance = projection.appearance_probability
        conditional_minutes = (
            projection.expected_minutes / appearance
            if appearance > 0
            else 72.0
        )
        minute_factor = projection.expected_minutes / 90.0
        latent = projection.latent_expectations or {}
        player_inputs.append(
            PlayerSimulationInput(
                player=Player(
                    player_id=int(projection.source_player_id),
                    name=str(row["web_name"]),
                    team_id=int(row["team_id"]),
                    position=projection.position,
                    price_tenths=int(row["price_tenths"]),
                ),
                appearance_probability=appearance,
                sixty_probability_given_appearance=(
                    projection.sixty_probability / appearance
                    if appearance > 0
                    else 0.0
                ),
                conditional_minutes=min(
                    90.0, max(1.0, conditional_minutes)
                ),
                goal_share=float(latent.get("goal_share", 0.0)),
                assist_share=float(latent.get("assist_share", 0.0)),
                saves_per_90=(
                    projection.save_points
                    * rules.scoring.saves_per_point
                    / minute_factor
                    if minute_factor > 0
                    else 0.0
                ),
                defensive_contributions_per_90=(
                    DEFENSIVE_CONTRIBUTION_COUNT_PRIORS[
                        projection.position
                    ]
                ),
            )
        )
    return tuple(fixtures), tuple(player_inputs)


def evaluate_simulation_calibration(
    outcomes: tuple[DistributionForecastOutcome, ...],
) -> SimulationCalibrationReport:
    """Score empirical forecast distributions with proper/coverage metrics."""

    if not outcomes:
        raise ValueError("At least one forecast outcome is required")
    if any(len(outcome.samples) < 100 for outcome in outcomes):
        raise ValueError("Every empirical distribution needs at least 100 samples")
    crps = []
    coverages = {0.50: 0, 0.80: 0, 0.95: 0}
    brier_40 = []
    brier_60 = []
    pit_bins = [0] * 10
    for outcome in outcomes:
        ordered = sorted(outcome.samples)
        crps.append(_empirical_crps(ordered, outcome.actual_points))
        for interval in coverages:
            tail = (1.0 - interval) / 2.0
            lower = _quantile(ordered, tail)
            upper = _quantile(ordered, 1.0 - tail)
            coverages[interval] += int(
                lower <= outcome.actual_points <= upper
            )
        probability_40 = (
            sum(value >= 40 for value in ordered) / len(ordered)
        )
        probability_60 = (
            sum(value >= 60 for value in ordered) / len(ordered)
        )
        brier_40.append(
            (probability_40 - int(outcome.actual_points >= 40)) ** 2
        )
        brier_60.append(
            (probability_60 - int(outcome.actual_points >= 60)) ** 2
        )
        less = sum(value < outcome.actual_points for value in ordered)
        equal = sum(value == outcome.actual_points for value in ordered)
        pit = (less + 0.5 * equal) / len(ordered)
        pit_bins[min(9, int(pit * 10))] += 1
    count = len(outcomes)
    return SimulationCalibrationReport(
        forecasts=count,
        mean_crps=round(fmean(crps), 6),
        coverage_50=round(coverages[0.50] / count, 6),
        coverage_80=round(coverages[0.80] / count, 6),
        coverage_95=round(coverages[0.95] / count, 6),
        threshold_40_brier=round(fmean(brier_40), 6),
        threshold_60_brier=round(fmean(brier_60), 6),
        pit_bins=tuple(pit_bins),
    )


def simulate_squads(
    fixtures: tuple[FixtureSimulationInput, ...],
    players: tuple[PlayerSimulationInput, ...],
    squads: tuple[SquadSimulationInput, ...],
    *,
    rules: SeasonRules,
    iterations: int = 10_000,
    seed: int = 20260730,
) -> SquadSimulationResult:
    """Simulate complete Gameweek outcomes and score legal squads exactly."""

    if iterations < 100:
        raise ValueError("At least 100 simulation iterations are required")
    if not fixtures:
        raise ValueError("At least one fixture is required")
    if not squads:
        raise ValueError("At least one squad is required")
    if len({fixture.fixture_id for fixture in fixtures}) != len(fixtures):
        raise ValueError("Fixture IDs must be unique")
    if len({entry.player.player_id for entry in players}) != len(players):
        raise ValueError("Player IDs must be unique")
    _validate_inputs(fixtures, players, squads)

    rng = random.Random(seed)
    by_team: dict[int, tuple[PlayerSimulationInput, ...]] = {}
    for team_id in {
        team for fixture in fixtures for team in (fixture.home_team_id, fixture.away_team_id)
    }:
        by_team[team_id] = tuple(entry for entry in players if entry.player.team_id == team_id)
    scores = {squad.name: [] for squad in squads}
    for _ in range(iterations):
        points_by_player = {entry.player.player_id: 0 for entry in players}
        minutes_by_player = {entry.player.player_id: 0 for entry in players}
        for fixture in fixtures:
            home_goals = _poisson(rng, fixture.home_expected_goals)
            away_goals = _poisson(rng, fixture.away_expected_goals)
            fixture_points, fixture_minutes = _simulate_fixture(
                fixture,
                by_team[fixture.home_team_id],
                by_team[fixture.away_team_id],
                home_goals,
                away_goals,
                rules,
                rng,
            )
            for player_id, value in fixture_points.items():
                points_by_player[player_id] += value
            for player_id, value in fixture_minutes.items():
                minutes_by_player[player_id] += value
        for squad in squads:
            score = calculate_team_score(
                squad.squad,
                points_by_player,
                minutes_by_player,
                rules,
                active_chip=squad.active_chip,
            )
            scores[squad.name].append(score.total_points)

    distributions = {name: _distribution(values) for name, values in scores.items()}
    pairwise: dict[str, float] = {}
    for left_index, left in enumerate(squads):
        for right in squads[left_index + 1 :]:
            left_scores = scores[left.name]
            right_scores = scores[right.name]
            wins = sum(
                1.0 if a > b else 0.5 if a == b else 0.0
                for a, b in zip(left_scores, right_scores, strict=True)
            )
            pairwise[f"{left.name}>{right.name}"] = round(wins / iterations, 6)
            pairwise[f"{right.name}>{left.name}"] = round(1.0 - wins / iterations, 6)
    return SquadSimulationResult(
        seed=seed,
        iterations=iterations,
        distributions=distributions,
        pairwise_win_probabilities=pairwise,
        assumptions=(
            "Scorelines are shared Poisson fixture outcomes.",
            "Scorers and assisters are allocated only among simulated participants.",
            "Clean sheets and goals conceded are shared team outcomes.",
            "Defensive contributions use a Gamma-Poisson mixture when dispersion exceeds zero.",
            "Autosubs, captain fallback and scoring chips use the configured season rules.",
        ),
    )


def _simulate_fixture(
    fixture: FixtureSimulationInput,
    home_players: tuple[PlayerSimulationInput, ...],
    away_players: tuple[PlayerSimulationInput, ...],
    home_goals: int,
    away_goals: int,
    rules: SeasonRules,
    rng: random.Random,
) -> tuple[dict[int, int], dict[int, int]]:
    entries = (*home_players, *away_players)
    minutes = {entry.player.player_id: _simulate_minutes(entry, rng) for entry in entries}
    stats = {
        entry.player.player_id: PlayerGameweekStats(minutes=minutes[entry.player.player_id])
        for entry in entries
    }
    for team_players, goals in (
        (home_players, home_goals),
        (away_players, away_goals),
    ):
        participants = tuple(entry for entry in team_players if minutes[entry.player.player_id] > 0)
        for _ in range(goals):
            scorer = _weighted_choice(
                participants,
                [entry.goal_share for entry in participants],
                rng,
            )
            if scorer is not None:
                player_id = scorer.player.player_id
                stats[player_id] = _replace_stat(stats[player_id], goals=stats[player_id].goals + 1)
            if rng.random() >= fixture.assist_probability_per_goal:
                continue
            assist_candidates = tuple(
                entry
                for entry in participants
                if scorer is None or entry.player.player_id != scorer.player.player_id
            )
            assister = _weighted_choice(
                assist_candidates,
                [entry.assist_share for entry in assist_candidates],
                rng,
            )
            if assister is not None:
                player_id = assister.player.player_id
                stats[player_id] = _replace_stat(
                    stats[player_id], assists=stats[player_id].assists + 1
                )

    for team_players, conceded in (
        (home_players, away_goals),
        (away_players, home_goals),
    ):
        for entry in team_players:
            player_id = entry.player.player_id
            played = minutes[player_id] > 0
            if not played:
                continue
            contribution_rate = entry.defensive_contributions_per_90 * minutes[player_id] / 90.0
            contributions = _gamma_poisson(
                rng,
                contribution_rate,
                entry.defensive_dispersion,
            )
            stats[player_id] = _replace_stat(
                stats[player_id],
                clean_sheet=conceded == 0,
                goals_conceded=conceded,
                saves=_poisson(
                    rng,
                    entry.saves_per_90 * minutes[player_id] / 90.0,
                ),
                defensive_contributions=contributions,
                yellow_cards=int(rng.random() < entry.yellow_card_probability),
                red_cards=int(rng.random() < entry.red_card_probability),
                own_goals=int(rng.random() < entry.own_goal_probability),
                penalties_saved=int(rng.random() < entry.penalty_save_probability),
                penalties_missed=int(rng.random() < entry.penalty_miss_probability),
            )

    bps = {}
    for entry in entries:
        player_id = entry.player.player_id
        player_stats = stats[player_id]
        if player_stats.minutes <= 0:
            continue
        bps[player_id] = (
            player_stats.goals * 24
            + player_stats.assists * 12
            + int(player_stats.clean_sheet) * 6
            + player_stats.saves
            + rng.randrange(0, 8)
        )
    bonus = allocate_bonus_points(bps)
    points = {}
    for entry in entries:
        player_id = entry.player.player_id
        player_stats = _replace_stat(
            stats[player_id],
            bonus=bonus.get(player_id, 0),
        )
        points[player_id] = calculate_player_points(entry.player, player_stats, rules)
    return points, minutes


def _replace_stat(
    stats: PlayerGameweekStats,
    **changes: int | bool,
) -> PlayerGameweekStats:
    values = {name: getattr(stats, name) for name in PlayerGameweekStats.__dataclass_fields__}
    values.update(changes)
    return PlayerGameweekStats(**values)


def _simulate_minutes(
    entry: PlayerSimulationInput,
    rng: random.Random,
) -> int:
    if rng.random() >= entry.appearance_probability:
        return 0
    if rng.random() < entry.sixty_probability_given_appearance:
        centre = max(60.0, entry.conditional_minutes)
        return min(90, max(60, round(rng.triangular(60, 90, centre))))
    centre = min(59.0, entry.conditional_minutes)
    return min(59, max(1, round(rng.triangular(1, 59, centre))))


def _weighted_choice(
    entries: tuple[PlayerSimulationInput, ...],
    weights: list[float],
    rng: random.Random,
) -> PlayerSimulationInput | None:
    if not entries:
        return None
    total = sum(max(0.0, weight) for weight in weights)
    if total <= 0:
        return entries[rng.randrange(len(entries))]
    threshold = rng.random() * total
    running = 0.0
    for entry, weight in zip(entries, weights, strict=True):
        running += max(0.0, weight)
        if running >= threshold:
            return entry
    return entries[-1]


def _poisson(rng: random.Random, rate: float) -> int:
    if rate <= 0:
        return 0
    if rate > 30:
        return max(0, round(rng.gauss(rate, math.sqrt(rate))))
    limit = math.exp(-rate)
    product = 1.0
    count = 0
    while product > limit:
        product *= rng.random()
        count += 1
    return count - 1


def _gamma_poisson(
    rng: random.Random,
    mean: float,
    dispersion: float,
) -> int:
    if mean <= 0:
        return 0
    if dispersion <= 0:
        return _poisson(rng, mean)
    latent_rate = rng.gammavariate(dispersion, mean / dispersion)
    return _poisson(rng, latent_rate)


def _distribution(values: list[int]) -> DistributionSummary:
    ordered = sorted(values)
    return DistributionSummary(
        samples=len(values),
        mean=round(fmean(values), 4),
        standard_deviation=round(pstdev(values), 4),
        p05=float(_quantile(ordered, 0.05)),
        p25=float(_quantile(ordered, 0.25)),
        median=float(_quantile(ordered, 0.5)),
        p75=float(_quantile(ordered, 0.75)),
        p95=float(_quantile(ordered, 0.95)),
        probability_below_40=round(sum(value < 40 for value in values) / len(values), 6),
        probability_at_least_60=round(sum(value >= 60 for value in values) / len(values), 6),
    )


def _quantile(ordered: list[int], probability: float) -> float:
    index = probability * (len(ordered) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    fraction = index - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _empirical_crps(ordered: list[int], actual: int) -> float:
    count = len(ordered)
    absolute_error = sum(
        abs(value - actual) for value in ordered
    ) / count
    half_pairwise_difference = sum(
        (2 * index - count + 1) * value
        for index, value in enumerate(ordered)
    ) / (count**2)
    return absolute_error - half_pairwise_difference


def _validate_inputs(
    fixtures: tuple[FixtureSimulationInput, ...],
    players: tuple[PlayerSimulationInput, ...],
    squads: tuple[SquadSimulationInput, ...],
) -> None:
    fixture_teams = {
        team for fixture in fixtures for team in (fixture.home_team_id, fixture.away_team_id)
    }
    player_ids = {entry.player.player_id for entry in players}
    for fixture in fixtures:
        if (
            fixture.home_team_id == fixture.away_team_id
            or fixture.home_expected_goals < 0
            or fixture.away_expected_goals < 0
        ):
            raise ValueError("Fixtures require distinct teams and non-negative rates")
        if not 0 <= fixture.assist_probability_per_goal <= 1:
            raise ValueError("Assist probability must be between zero and one")
    for entry in players:
        if entry.player.team_id not in fixture_teams:
            raise ValueError("Every simulated player must belong to a fixture team")
        probabilities = (
            entry.appearance_probability,
            entry.sixty_probability_given_appearance,
            entry.yellow_card_probability,
            entry.red_card_probability,
            entry.own_goal_probability,
            entry.penalty_save_probability,
            entry.penalty_miss_probability,
        )
        if any(not 0 <= value <= 1 for value in probabilities):
            raise ValueError("Player probabilities must be between zero and one")
        if not 1 <= entry.conditional_minutes <= 90:
            raise ValueError("Conditional minutes must be within 1-90")
        if (
            min(
                entry.goal_share,
                entry.assist_share,
                entry.saves_per_90,
                entry.defensive_contributions_per_90,
                entry.defensive_dispersion,
            )
            < 0
        ):
            raise ValueError("Player rates, shares and dispersion cannot be negative")
    for squad in squads:
        if not {player.player_id for player in squad.squad.players} <= player_ids:
            raise ValueError("Squad players must be present in simulation inputs")
