from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Position(StrEnum):
    GK = "GK"
    DEF = "DEF"
    MID = "MID"
    FWD = "FWD"


class Chip(StrEnum):
    WILDCARD = "wildcard"
    FREE_HIT = "free_hit"
    BENCH_BOOST = "bench_boost"
    TRIPLE_CAPTAIN = "triple_captain"


@dataclass(frozen=True)
class Player:
    player_id: int
    name: str
    team_id: int
    position: Position
    price_tenths: int


@dataclass(frozen=True)
class Squad:
    players: tuple[Player, ...]
    starting_player_ids: frozenset[int] = frozenset()
    bench_player_ids: tuple[int, ...] = ()
    captain_id: int | None = None
    vice_captain_id: int | None = None


@dataclass(frozen=True)
class PlayerGameweekStats:
    minutes: int = 0
    goals: int = 0
    assists: int = 0
    clean_sheet: bool = False
    goals_conceded: int = 0
    saves: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    own_goals: int = 0
    bonus: int = 0
    defensive_contributions: int = 0
