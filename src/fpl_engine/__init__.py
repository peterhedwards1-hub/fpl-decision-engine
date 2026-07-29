"""Core package for the FPL Decision Engine."""

from .config import SeasonRules, load_season_rules
from .domain import Chip, Player, PlayerGameweekStats, Position, Squad
from .manager import (
    ManagerSnapshot,
    ManagerSquadEntry,
    ManagerStateError,
    ManagerStateRepository,
    StoredManagerSnapshot,
)
from .rules import (
    ResolvedLineup,
    TeamScore,
    ValidationError,
    allocate_bonus_points,
    calculate_player_points,
    calculate_selling_price,
    calculate_team_score,
    calculate_transfer_cost,
    next_free_transfer_count,
    resolve_automatic_substitutions,
    validate_chip_use,
    validate_squad,
)

__all__ = [
    "Chip",
    "ManagerSnapshot",
    "ManagerSquadEntry",
    "ManagerStateError",
    "ManagerStateRepository",
    "Player",
    "PlayerGameweekStats",
    "Position",
    "ResolvedLineup",
    "SeasonRules",
    "Squad",
    "StoredManagerSnapshot",
    "TeamScore",
    "ValidationError",
    "allocate_bonus_points",
    "calculate_player_points",
    "calculate_selling_price",
    "calculate_team_score",
    "calculate_transfer_cost",
    "load_season_rules",
    "next_free_transfer_count",
    "resolve_automatic_substitutions",
    "validate_chip_use",
    "validate_squad",
]
