"""Which chips remain, and which are legal to play this Gameweek.

Chip availability is state, not a flag. A season grants two sets, each chip
once per set, at most one chip active per Gameweek, with per-chip blackout
Gameweeks and cooldowns. A replay that does not carry that state either plays
chips it does not have or refuses ones it does.

This module tracks the plays and defers every legality question to
`validate_chip_use`, so the rules live in one place and stay configurable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import SeasonRules
from .domain import Chip
from .rules import validate_chip_use

#: Chips whose whole effect is on one Gameweek's score, given a fixed squad.
#: Wildcard and Free Hit instead change which squad exists, so their value
#: depends on future state and opportunity cost and they are not replayable
#: by the same local argument.
SCORING_CHIPS = (Chip.BENCH_BOOST, Chip.TRIPLE_CAPTAIN)


@dataclass(frozen=True)
class ChipPlay:
    chip: str
    gameweek_number: int

    def as_dict(self) -> dict[str, Any]:
        return {"chip": self.chip, "gameweek_number": self.gameweek_number}


@dataclass(frozen=True)
class ChipLedger:
    """The chips a manager has spent, and when."""

    plays: tuple[ChipPlay, ...] = ()

    @property
    def played_gameweeks(self) -> tuple[int, ...]:
        return tuple(play.gameweek_number for play in self.plays)

    def chip_for(self, gameweek_number: int) -> Chip | None:
        for play in self.plays:
            if play.gameweek_number == gameweek_number:
                return Chip(play.chip)
        return None

    def _half(self, gameweek_number: int, rules: SeasonRules) -> bool:
        return gameweek_number <= rules.chips.first_set_expiry_gameweek

    def used_in_half(
        self,
        gameweek_number: int,
        rules: SeasonRules,
    ) -> frozenset[Chip]:
        """Chips already spent in the same set as this Gameweek."""

        target = self._half(gameweek_number, rules)
        return frozenset(
            Chip(play.chip)
            for play in self.plays
            if self._half(play.gameweek_number, rules) == target
        )

    def errors_for(
        self,
        chip: Chip,
        gameweek_number: int,
        rules: SeasonRules,
    ) -> tuple[Any, ...]:
        """Every reason this chip cannot be played in this Gameweek."""

        if self.chip_for(gameweek_number) is not None:
            return (
                _Error(
                    "chip_already_active",
                    f"Gameweek {gameweek_number} already has a chip active",
                ),
            )
        earlier = [
            play.gameweek_number
            for play in self.plays
            if play.chip == chip.value and play.gameweek_number < gameweek_number
        ]
        return validate_chip_use(
            chip,
            gameweek_number,
            rules,
            already_used_in_half=self.used_in_half(gameweek_number, rules),
            previous_gameweek_chip=(
                chip if (gameweek_number - 1) in earlier else None
            ),
            last_used_gameweek=max(earlier) if earlier else None,
        )

    def available(
        self,
        gameweek_number: int,
        rules: SeasonRules,
        *,
        chips: tuple[Chip, ...] = SCORING_CHIPS,
    ) -> tuple[Chip, ...]:
        """Chips legally playable in this Gameweek, in the order supplied."""

        return tuple(
            chip
            for chip in chips
            if not self.errors_for(chip, gameweek_number, rules)
        )

    def after_playing(
        self,
        chip: Chip,
        gameweek_number: int,
        rules: SeasonRules,
    ) -> ChipLedger:
        errors = self.errors_for(chip, gameweek_number, rules)
        if errors:
            raise ValueError(
                f"Cannot play {chip.value} in Gameweek {gameweek_number}: "
                + "; ".join(error.message for error in errors)
            )
        return ChipLedger(
            plays=(*self.plays, ChipPlay(chip.value, gameweek_number))
        )

    def as_dict(self) -> dict[str, Any]:
        return {"plays": [play.as_dict() for play in self.plays]}


@dataclass(frozen=True)
class _Error:
    code: str
    message: str


@dataclass(frozen=True)
class ScoringChipPolicy:
    """When to play a scoring chip, declared rather than learned.

    Both thresholds are in expected points and default high enough that the
    chip is never played, so a replay stays chip-free unless a policy is
    deliberately declared. A weakly validated chip adviser left switched on
    would obscure whether the underlying projections improved.
    """

    bench_boost_threshold: float = float("inf")
    triple_captain_threshold: float = float("inf")

    def threshold(self, chip: Chip) -> float:
        if chip == Chip.BENCH_BOOST:
            return self.bench_boost_threshold
        if chip == Chip.TRIPLE_CAPTAIN:
            return self.triple_captain_threshold
        raise ValueError(f"{chip.value} is not a scoring chip")

    @property
    def plays_anything(self) -> bool:
        return any(
            self.threshold(chip) != float("inf") for chip in SCORING_CHIPS
        )

    def choose(
        self,
        forecast_gains: dict[Chip, float],
        available: tuple[Chip, ...],
    ) -> Chip | None:
        """Pick the legal chip with the largest gain that clears its threshold."""

        eligible = [
            (forecast_gains[chip], chip.value, chip)
            for chip in available
            if chip in forecast_gains
            and forecast_gains[chip] >= self.threshold(chip)
        ]
        if not eligible:
            return None
        return max(eligible)[2]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bench_boost_threshold": self.bench_boost_threshold,
            "triple_captain_threshold": self.triple_captain_threshold,
        }
