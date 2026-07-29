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
) -> ChipRecommendation:
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
    if chip == Chip.TRIPLE_CAPTAIN:
        captain = max(
            eligible,
            key=lambda player: (
                _gameweek_points(player) * player.appearance_probability,
                -player.uncertainty,
            ),
        )
        value = _gameweek_points(captain)
        return ChipRecommendation(
            chip=chip,
            gameweek_number=gameweek_number,
            expected_incremental_points=round(value, 3),
            captain_id=captain.source_player_id,
            explanation=(
                f"Triple-captain {captain.web_name}; the chip adds one extra "
                f"captain score worth {value:.2f} expected points."
            ),
        )
    if chip == Chip.WILDCARD:
        squad = optimise_opening_squads(
            candidates,
            budget_tenths=budget_tenths,
            rules=rules,
            alternative_count=0,
        ).primary
        return ChipRecommendation(
            chip=chip,
            gameweek_number=gameweek_number,
            expected_incremental_points=squad.horizon_expected_points,
            squad=squad,
            explanation=(
                "Wildcard rebuilds the persistent squad for the configured "
                "projection horizon; transfer hits are not charged."
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
        value = sum(
            _gameweek_points(player)
            for player in squad.players
            if player.source_player_id in squad.bench_player_ids
        )
        explanation = (
            "Bench Boost adds the four ordered substitutes' expected points; "
            "the squad is optimised for this single Gameweek."
        )
    else:
        value = squad.gameweek_expected_points
        explanation = (
            "Free Hit selects a one-Gameweek squad; the stored manager squad "
            "remains unchanged for the following Gameweek."
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
