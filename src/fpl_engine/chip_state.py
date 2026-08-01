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


def set_expiry_gameweek(gameweek_number: int, rules: SeasonRules) -> int:
    """The last Gameweek of the chip set this Gameweek belongs to.

    Chips do not carry across the halves, so look-ahead must stop at the
    boundary; waiting for a better week beyond it is waiting for a week the
    chip cannot be played in.
    """

    if gameweek_number <= rules.chips.first_set_expiry_gameweek:
        return rules.chips.first_set_expiry_gameweek
    return 38


@dataclass(frozen=True)
class ChipDecisionContext:
    """Everything a chip policy may consider at one Gameweek.

    `values_by_gameweek` holds the forecast gain of each chip in this Gameweek
    and in every later Gameweek the projection covers, so a policy can decline
    now because a double Gameweek is coming. It reaches only as far as the
    projection horizon: `reaches_expiry` says whether that was far enough to
    see the whole set.
    """

    gameweek_number: int
    values_by_gameweek: dict[int, dict[Chip, float]]
    legal_by_gameweek: dict[int, tuple[Chip, ...]]
    expiry_gameweek: int

    @property
    def legal_now(self) -> tuple[Chip, ...]:
        return self.legal_by_gameweek.get(self.gameweek_number, ())

    @property
    def lookahead_gameweeks(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                gameweek
                for gameweek in self.values_by_gameweek
                if self.gameweek_number < gameweek <= self.expiry_gameweek
            )
        )

    @property
    def reaches_expiry(self) -> bool:
        """Whether the look-ahead saw every Gameweek the chip could be used in."""

        return max(self.values_by_gameweek, default=self.gameweek_number) >= (
            self.expiry_gameweek
        )

    def value(self, chip: Chip, gameweek_number: int) -> float:
        return self.values_by_gameweek.get(gameweek_number, {}).get(chip, 0.0)

    def best_later_value(self, chip: Chip) -> float:
        """The best forecast gain still available to this chip after now."""

        return max(
            (
                self.value(chip, gameweek)
                for gameweek in self.lookahead_gameweeks
                if chip in self.legal_by_gameweek.get(gameweek, ())
            ),
            default=0.0,
        )


@dataclass(frozen=True)
class ScoringChipPolicy:
    """Play a scoring chip as soon as its gain clears a threshold.

    Myopic by construction: it never asks whether a better Gameweek is coming.
    Both thresholds default high enough that nothing is played, so a replay
    stays chip-free unless a policy is deliberately declared — a weakly
    validated chip adviser left switched on would obscure whether the
    underlying projections improved.
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

    def choose(self, context: ChipDecisionContext) -> Chip | None:
        eligible = [
            (context.value(chip, context.gameweek_number), chip.value, chip)
            for chip in context.legal_now
            if context.value(chip, context.gameweek_number)
            >= self.threshold(chip)
        ]
        if not eligible:
            return None
        return max(eligible)[2]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "threshold",
            "bench_boost_threshold": self.bench_boost_threshold,
            "triple_captain_threshold": self.triple_captain_threshold,
        }


@dataclass(frozen=True)
class LookaheadChipPolicy:
    """Play a chip only when this Gameweek beats every later one still available.

    This is what makes a double Gameweek pull the chip toward it: a modest
    week is declined while a bigger week remains reachable before the set
    expires, and the chip is played once nothing better is left.

    `minimum_gain` refuses trivial plays outright. `margin` requires this week
    to beat the best later week by a stated amount, which biases toward
    waiting when the two are close and the later forecast is less certain.
    """

    minimum_gain: float = 0.0
    margin: float = 0.0
    enabled: bool = False

    @property
    def plays_anything(self) -> bool:
        return self.enabled

    def choose(self, context: ChipDecisionContext) -> Chip | None:
        if not self.enabled:
            return None
        eligible = []
        for chip in context.legal_now:
            now = context.value(chip, context.gameweek_number)
            if now < self.minimum_gain:
                continue
            # An empty look-ahead means nothing is left before expiry, so the
            # comparison becomes "play it or lose it".
            if now < context.best_later_value(chip) + self.margin:
                continue
            eligible.append((now, chip.value, chip))
        if not eligible:
            return None
        return max(eligible)[2]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "lookahead",
            "enabled": self.enabled,
            "minimum_gain": self.minimum_gain,
            "margin": self.margin,
        }
