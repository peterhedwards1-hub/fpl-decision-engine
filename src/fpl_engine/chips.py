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
) -> ChipRecommendation:
    if future_opportunity_cost < 0:
        raise ValueError("Future chip opportunity cost cannot be negative")
    errors = validate_chip_use(
        chip,
        gameweek_number,
        rules,
        already_used_in_half=(
            frozenset({chip})
            if any(
                (previous <= rules.chips.first_set_expiry_gameweek)
                == (gameweek_number <= rules.chips.first_set_expiry_gameweek)
                for previous in previous_chip_gameweeks
            )
            else frozenset()
        ),
        previous_gameweek_chip=(
            chip
            if gameweek_number - 1 in previous_chip_gameweeks
            else None
        ),
        last_used_gameweek=(
            max(previous_chip_gameweeks)
            if previous_chip_gameweeks
            else None
        ),
    )
    if errors:
        raise ValueError("; ".join(error.message for error in errors))
    eligible = candidates
    if (
        current_player_ids is not None
        and chip in {Chip.TRIPLE_CAPTAIN, Chip.BENCH_BOOST}
    ):
        eligible = tuple(
            player
            for player in candidates
            if player.source_player_id in current_player_ids
        )
    if current_player_ids is None:
        raise ValueError(
            "Comparable chip recommendations require the current squad"
        )
    current = tuple(
        player
        for player in candidates
        if player.source_player_id in current_player_ids
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
    squad = optimise_full_squad(
        weekly_candidates,
        budget_tenths=budget_tenths,
        rules=rules,
    )
    if chip == Chip.BENCH_BOOST:
        all_fifteen = sum(
            _gameweek_points(player) for player in baseline.players
        ) + baseline.expected_captain_contribution
        value = (
            all_fifteen
            - baseline.gameweek_expected_points
            - future_opportunity_cost
        )
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


def _gameweek_points(player: CandidatePlayer) -> float:
    return (
        player.expected_points
        if player.gameweek_expected_points is None
        else player.gameweek_expected_points
    )
