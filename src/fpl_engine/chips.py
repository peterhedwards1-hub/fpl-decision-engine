"""Chip-specific optimisation using the shared rules and squad solver."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from statistics import median

from .config import SeasonRules
from .domain import Chip
from .optimisation import (
    CandidatePlayer,
    FullSquadResult,
    optimise_full_squad,
    optimise_opening_squads,
)
from .rules import validate_chip_use


@dataclass(frozen=True)
class ChipRecommendation:
    chip: Chip
    gameweek_number: int
    expected_incremental_points: float
    explanation: str
    squad: FullSquadResult | None = None
    captain_id: str | None = None


@dataclass(frozen=True)
class ChipTimingOption:
    gameweek_number: int
    gross_incremental_points: float
    future_opportunity_cost: float
    net_value_versus_best_later: float


@dataclass(frozen=True)
class ChipTimingRecommendation:
    chip: Chip
    recommended_gameweek: int
    options: tuple[ChipTimingOption, ...]
    recommendation: ChipRecommendation
    explanation: str
    horizon_reaches_set_expiry: bool = False
    expected_double_reserve: float = 0.0
    hold_for_expected_double: bool = False


def recommend_chip_timing(
    chip: Chip,
    candidates: tuple[CandidatePlayer, ...],
    *,
    candidate_gameweeks: tuple[int, ...],
    previous_chip_gameweeks: tuple[int, ...],
    budget_tenths: int,
    rules: SeasonRules,
    current_player_ids: frozenset[str],
    candidate_pool_size: int = 4,
    expected_double_reserve: float = 0.0,
    reserve_until_gameweek: int | None = None,
) -> ChipTimingRecommendation:
    """Value a chip at every supplied horizon step instead of manual cost.

    ``expected_double_reserve`` is the empirically expected gain of the best
    future double Gameweek in this set — a value the projection cannot see,
    because a second-half double is created by an in-season reschedule and does
    not exist in the fixtures until it is announced. When the supplied
    Gameweeks do not reach the set's expiry, and a double could still occur
    before ``reserve_until_gameweek``, this reserve is folded into every option's
    opportunity cost, so a merely-decent week does not spend the chip that an
    expected double would use far better. Once the horizon reaches expiry — any
    real double is then already visible in ``gross`` — or the reserve window has
    passed, the reserve drops out and the last legal week is play-or-lose.
    """

    gameweeks = tuple(sorted(set(candidate_gameweeks)))
    if not gameweeks:
        raise ValueError("At least one candidate Gameweek is required")
    first_half = gameweeks[0] <= rules.chips.first_set_expiry_gameweek
    gameweeks = tuple(
        gameweek
        for gameweek in gameweeks
        if (gameweek <= rules.chips.first_set_expiry_gameweek) == first_half
        and not _chip_use_errors(
            chip,
            gameweek,
            previous_chip_gameweeks,
            rules,
        )
    )
    if not gameweeks:
        raise ValueError("The chip is not legal in any projected Gameweek in this set")
    gross: dict[int, ChipRecommendation] = {}
    for gameweek in gameweeks:
        eligible = _candidates_from_gameweek(candidates, gameweek)
        gross[gameweek] = recommend_chip(
            chip,
            eligible,
            gameweek_number=gameweek,
            previous_chip_gameweeks=previous_chip_gameweeks,
            budget_tenths=budget_tenths,
            rules=rules,
            current_player_ids=current_player_ids,
            candidate_pool_size=candidate_pool_size,
        )
    best_gameweek = max(
        gameweeks,
        key=lambda gameweek: (
            gross[gameweek].expected_incremental_points,
            -gameweek,
        ),
    )
    set_expiry = rules.chips.first_set_expiry_gameweek if first_half else 38
    reaches_expiry = max(gameweeks) >= set_expiry
    # The reserve only bites while a real double is still hidden (the horizon
    # has not reached expiry) and one could still plausibly occur.
    reserve_active = expected_double_reserve > 0.0 and not reaches_expiry
    reserve_cutoff = reserve_until_gameweek if reserve_until_gameweek is not None else set_expiry

    options = []
    for gameweek in gameweeks:
        later_values = [
            gross[later].expected_incremental_points for later in gameweeks if later > gameweek
        ]
        # A double expected after this week is an opportunity cost of playing now,
        # exactly like a visible better week, but the fixtures cannot show it yet.
        reserve_here = (
            expected_double_reserve
            if reserve_active and gameweek < reserve_cutoff
            else 0.0
        )
        opportunity_cost = max([*later_values, reserve_here], default=0.0)
        options.append(
            ChipTimingOption(
                gameweek_number=gameweek,
                gross_incremental_points=(gross[gameweek].expected_incremental_points),
                future_opportunity_cost=round(opportunity_cost, 3),
                net_value_versus_best_later=round(
                    gross[gameweek].expected_incremental_points - opportunity_cost,
                    3,
                ),
            )
        )
    best_option = max(options, key=lambda option: option.net_value_versus_best_later)
    # Hold when even the strongest visible week loses to the expected double:
    # no week beats the reserve, so spending now would waste the chip.
    hold_for_expected_double = (
        reserve_active
        and best_gameweek < reserve_cutoff
        and best_option.net_value_versus_best_later < 0.0
    )
    if hold_for_expected_double:
        explanation = (
            f"Hold {chip.value}: the strongest visible week (GW{best_gameweek}, "
            f"{gross[best_gameweek].expected_incremental_points:.1f} pts) does not beat the "
            f"expected best future double Gameweek ({expected_double_reserve:.1f} pts). "
            "A second-half double is not in the fixtures yet, so this stays provisional "
            "until one is scheduled."
        )
    else:
        explanation = (
            f"{chip.value} is strongest in GW{best_gameweek}; every option is compared "
            "with the best still-available later opportunity"
            + (
                ", including the expected future double Gameweek."
                if reserve_active
                else "."
            )
            + (
                " The projection reaches this chip set's expiry."
                if reaches_expiry
                else f" This is provisional because the projection stops before GW{set_expiry}."
            )
        )
    return ChipTimingRecommendation(
        chip=chip,
        recommended_gameweek=best_gameweek,
        options=tuple(options),
        recommendation=gross[best_gameweek],
        explanation=explanation,
        horizon_reaches_set_expiry=reaches_expiry,
        expected_double_reserve=round(expected_double_reserve, 3),
        hold_for_expected_double=hold_for_expected_double,
    )


def recommend_chip(
    chip: Chip,
    candidates: tuple[CandidatePlayer, ...],
    *,
    gameweek_number: int,
    previous_chip_gameweeks: tuple[int, ...],
    budget_tenths: int,
    rules: SeasonRules,
    current_player_ids: frozenset[str] | None = None,
    future_opportunity_cost: float = 0.0,
    candidate_pool_size: int = 1,
) -> ChipRecommendation:
    if future_opportunity_cost < 0:
        raise ValueError("Future chip opportunity cost cannot be negative")
    if candidate_pool_size < 1:
        raise ValueError("Candidate pool size must be positive")
    errors = _chip_use_errors(
        chip,
        gameweek_number,
        previous_chip_gameweeks,
        rules,
    )
    if errors:
        raise ValueError("; ".join(error.message for error in errors))
    eligible = candidates
    if current_player_ids is not None and chip in {Chip.TRIPLE_CAPTAIN, Chip.BENCH_BOOST}:
        eligible = tuple(
            player for player in candidates if player.source_player_id in current_player_ids
        )
    if current_player_ids is None:
        raise ValueError("Comparable chip recommendations require the current squad")
    current = tuple(
        player for player in candidates if player.source_player_id in current_player_ids
    )
    if len(current) != rules.squad.squad_size:
        raise ValueError("Current squad must contain every configured squad player")
    current_budget = sum(player.price_tenths for player in current)
    baseline = optimise_full_squad(
        current,
        budget_tenths=current_budget,
        rules=rules,
    )
    if chip == Chip.TRIPLE_CAPTAIN:
        value = baseline.expected_captain_contribution - future_opportunity_cost
        return ChipRecommendation(
            chip=chip,
            gameweek_number=gameweek_number,
            expected_incremental_points=round(value, 3),
            captain_id=baseline.captain_id,
            explanation=(
                "Triple Captain adds one expected effective captain score, "
                "including vice-captain fallback, less the supplied future "
                f"opportunity cost: {value:.2f} points."
            ),
        )
    if chip == Chip.WILDCARD:
        squad = optimise_opening_squads(
            candidates,
            budget_tenths=budget_tenths,
            rules=rules,
            alternative_count=0,
            candidate_pool_size=candidate_pool_size,
        ).primary
        value = (
            squad.horizon_expected_points
            - baseline.horizon_expected_points
            - future_opportunity_cost
        )
        return ChipRecommendation(
            chip=chip,
            gameweek_number=gameweek_number,
            expected_incremental_points=round(value, 3),
            squad=squad,
            explanation=(
                "Wildcard value is the persistent rebuilt squad minus the "
                "current-squad no-chip horizon and the supplied future "
                "opportunity cost."
            ),
        )
    weekly_candidates = tuple(
        CandidatePlayer(
            source_player_id=player.source_player_id,
            web_name=player.web_name,
            team_id=player.team_id,
            team_short_name=player.team_short_name,
            position=player.position,
            price_tenths=player.price_tenths,
            expected_points=_gameweek_points(player),
            gameweek_expected_points=_gameweek_points(player),
            appearance_probability=player.appearance_probability,
            uncertainty=player.uncertainty,
        )
        for player in eligible
    )
    squad = (
        optimise_full_squad(
            weekly_candidates,
            budget_tenths=budget_tenths,
            rules=rules,
        )
        if chip == Chip.BENCH_BOOST
        else optimise_opening_squads(
            weekly_candidates,
            budget_tenths=budget_tenths,
            rules=rules,
            alternative_count=0,
            candidate_pool_size=candidate_pool_size,
        ).primary
    )
    if chip == Chip.BENCH_BOOST:
        all_fifteen = (
            sum(_gameweek_points(player) for player in baseline.players)
            + baseline.expected_captain_contribution
        )
        value = all_fifteen - baseline.gameweek_expected_points - future_opportunity_cost
        squad = baseline
        explanation = (
            "Bench Boost value is all 15 expected scores minus the normal XI "
            "with automatic substitutions, less the supplied future "
            "opportunity cost."
        )
    else:
        value = (
            squad.gameweek_expected_points
            - baseline.gameweek_expected_points
            - future_opportunity_cost
        )
        explanation = (
            "Free Hit value is the optimised one-Gameweek squad minus the "
            "current no-chip squad, less the supplied future opportunity cost."
        )
    return ChipRecommendation(
        chip=chip,
        gameweek_number=gameweek_number,
        expected_incremental_points=round(value, 3),
        squad=squad,
        explanation=explanation,
    )


def _chip_use_errors(
    chip: Chip,
    gameweek_number: int,
    previous_chip_gameweeks: tuple[int, ...],
    rules: SeasonRules,
):
    same_half_uses = tuple(
        previous
        for previous in previous_chip_gameweeks
        if (previous <= rules.chips.first_set_expiry_gameweek)
        == (gameweek_number <= rules.chips.first_set_expiry_gameweek)
    )
    return validate_chip_use(
        chip,
        gameweek_number,
        rules,
        already_used_in_half=(
            frozenset({chip}) if same_half_uses else frozenset()
        ),
        previous_gameweek_chip=(
            chip if gameweek_number - 1 in same_half_uses else None
        ),
        last_used_gameweek=max(same_half_uses) if same_half_uses else None,
    )


def _gameweek_points(player: CandidatePlayer) -> float:
    return (
        player.expected_points
        if player.gameweek_expected_points is None
        else player.gameweek_expected_points
    )


def _candidates_from_gameweek(
    candidates: tuple[CandidatePlayer, ...],
    gameweek: int,
) -> tuple[CandidatePlayer, ...]:
    result = []
    for player in candidates:
        values = tuple(
            value for value in player.gameweek_values if value.gameweek_number >= gameweek
        )
        target = next(
            (value for value in values if value.gameweek_number == gameweek),
            None,
        )
        if player.gameweek_values and target is None:
            raise ValueError(f"Player {player.source_player_id} has no GW{gameweek} value")
        target_points = _gameweek_points(player) if target is None else target.expected_points
        result.append(
            CandidatePlayer(
                source_player_id=player.source_player_id,
                web_name=player.web_name,
                team_id=player.team_id,
                team_short_name=player.team_short_name,
                position=player.position,
                price_tenths=player.price_tenths,
                expected_points=(
                    sum(value.expected_points for value in values) if values else target_points
                ),
                gameweek_expected_points=target_points,
                appearance_probability=(
                    player.appearance_probability
                    if target is None
                    else target.appearance_probability
                ),
                uncertainty=player.uncertainty,
                residual_value=player.residual_value,
                gameweek_values=values,
            )
        )
    return tuple(result)


def estimate_double_gameweek_reserve(
    connection: sqlite3.Connection,
    *,
    seasons: tuple[str, ...] | None = None,
    minimum_gameweek: int = 20,
    maximum_per_club: int = 3,
    squad_size: int = 15,
    bench_size: int = 4,
) -> dict[Chip, float]:
    """Expected gain of the best future double Gameweek, per scoring chip.

    A second-half double Gameweek is created by an in-season reschedule and does
    not exist in the fixtures until it is announced, so a projection cannot see
    it and a chip policy needs a *prior* for what one is worth. This estimates
    that prior from realised history rather than guessing it. For each season's
    single biggest second-half double — the week a manager would target — it
    reads which players actually played twice and their realised two-fixture
    points, builds a plausible all-doubling squad (best realised scorers,
    capped at ``maximum_per_club`` per club, as a real squad must be), and
    reports what each scoring chip would have banked:

    - **Triple Captain**: one extra copy of the top pick's realised two-fixture
      score — the marginal multiple Triple Captain adds.
    - **Bench Boost**: the realised points of the four weakest squad members
      (the bench a Bench Boost activates).

    The per-season values are reduced to their median, robust to a single freak
    week and to seasons with only a small double. This is a realised-outcome
    estimate, deliberately independent of the projection model; a
    projection-and-optimiser valuation of each historical double would refine it
    but is not required for the reserve to be sound. It ignores squad position
    quotas, so it is a mild over-estimate — which is the safe direction for a
    reserve, since erring high holds the chip rather than wasting it.
    """

    where = ""
    params: list[object] = [minimum_gameweek]
    if seasons is not None:
        where = f" AND se.code IN ({','.join('?' for _ in seasons)})"
        params.extend(seasons)
    rows = connection.execute(
        f"""
        SELECT se.code AS season, g.number AS gw, teams.short_name AS club,
               COUNT(*) AS fixtures, SUM(s.total_points) AS points
        FROM player_fixture_stats s
        JOIN fixtures f ON f.id = s.fixture_id
        JOIN gameweeks g ON g.id = f.gameweek_id
        JOIN seasons se ON se.id = g.season_id
        JOIN player_seasons ps ON ps.id = s.player_season_id
        JOIN teams ON teams.id = ps.team_id
        WHERE g.number >= ?{where}
        GROUP BY se.code, g.number, ps.id
        HAVING COUNT(*) >= 2
        """,
        params,
    ).fetchall()

    # Doubling players (club, realised two-fixture points) keyed by the double.
    doubles: dict[tuple[str, int], list[tuple[str, float]]] = {}
    for row in rows:
        doubles.setdefault((row["season"], row["gw"]), []).append(
            (row["club"], float(row["points"] or 0.0))
        )
    return _reserve_from_doubles(
        doubles,
        maximum_per_club=maximum_per_club,
        squad_size=squad_size,
        bench_size=bench_size,
    )


def _reserve_from_doubles(
    doubles: dict[tuple[str, int], list[tuple[str, float]]],
    *,
    maximum_per_club: int,
    squad_size: int,
    bench_size: int,
) -> dict[Chip, float]:
    """Reduce realised doubling-player scores to a per-chip reserve.

    ``doubles`` maps each (season, gameweek) double to its doubling players as
    (club, realised two-fixture points). The biggest double per season is
    chosen, a best-scorers squad is built under the club cap, and the medians of
    the top pick (Triple Captain) and the four weakest (Bench Boost) are taken.
    """

    best_per_season: dict[str, list[tuple[str, float]]] = {}
    for (season, _gw), players in doubles.items():
        if season not in best_per_season or len(players) > len(best_per_season[season]):
            best_per_season[season] = players

    triple_captain: list[float] = []
    bench_boost: list[float] = []
    for players in best_per_season.values():
        squad: list[float] = []
        per_club: dict[str, int] = {}
        for club, points in sorted(players, key=lambda item: item[1], reverse=True):
            if per_club.get(club, 0) >= maximum_per_club:
                continue
            per_club[club] = per_club.get(club, 0) + 1
            squad.append(points)
            if len(squad) == squad_size:
                break
        if not squad:
            continue
        triple_captain.append(squad[0])
        bench_boost.append(sum(squad[-bench_size:]) if len(squad) >= bench_size else sum(squad))

    return {
        Chip.TRIPLE_CAPTAIN: round(median(triple_captain), 3) if triple_captain else 0.0,
        Chip.BENCH_BOOST: round(median(bench_boost), 3) if bench_boost else 0.0,
    }
