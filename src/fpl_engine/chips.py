"""Chip-specific optimisation using the shared rules and squad solver."""

from __future__ import annotations

from dataclasses import dataclass

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
) -> ChipTimingRecommendation:
    """Value a chip at every supplied horizon step instead of manual cost."""

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
    options = []
    for gameweek in gameweeks:
        later_values = [
            gross[later].expected_incremental_points for later in gameweeks if later > gameweek
        ]
        opportunity_cost = max(later_values, default=0.0)
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
    set_expiry = rules.chips.first_set_expiry_gameweek if first_half else 38
    reaches_expiry = max(gameweeks) >= set_expiry
    return ChipTimingRecommendation(
        chip=chip,
        recommended_gameweek=best_gameweek,
        options=tuple(options),
        recommendation=gross[best_gameweek],
        explanation=(
            f"{chip.value} is strongest in GW{best_gameweek}; every option "
            "is compared with the best still-available later opportunity."
            + (
                " The projection reaches this chip set's expiry."
                if reaches_expiry
                else f" This is provisional because the projection stops before GW{set_expiry}."
            )
        ),
        horizon_reaches_set_expiry=reaches_expiry,
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
