from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .config import SeasonRules
from .domain import Player, PlayerGameweekStats, Position, Squad


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str


def validate_squad(squad: Squad, rules: SeasonRules) -> tuple[ValidationError, ...]:
    """Return all deterministic squad and lineup rule violations."""
    errors: list[ValidationError] = []
    players = squad.players

    if len(players) != rules.squad.squad_size:
        errors.append(
            ValidationError(
                "squad_size",
                f"Squad must contain {rules.squad.squad_size} players, found {len(players)}",
            )
        )

    player_ids = [player.player_id for player in players]
    if len(set(player_ids)) != len(player_ids):
        errors.append(ValidationError("duplicate_player", "Squad contains duplicate player IDs"))

    position_counts = Counter(player.position.value for player in players)
    for position, required in rules.squad.position_counts.items():
        actual = position_counts[position]
        if actual != required:
            errors.append(
                ValidationError(
                    "position_count",
                    f"Squad requires {required} {position}, found {actual}",
                )
            )

    team_counts = Counter(player.team_id for player in players)
    for team_id, count in team_counts.items():
        if count > rules.squad.max_players_per_team:
            errors.append(
                ValidationError(
                    "club_limit",
                    f"Team {team_id} has {count} players; maximum is "
                    f"{rules.squad.max_players_per_team}",
                )
            )

    cost = sum(player.price_tenths for player in players)
    if cost > rules.squad.budget_tenths:
        errors.append(
            ValidationError(
                "budget",
                f"Squad costs {cost / 10:.1f}; budget is {rules.squad.budget_tenths / 10:.1f}",
            )
        )

    if squad.starting_player_ids:
        errors.extend(_validate_lineup(squad, rules))

    squad_ids = set(player_ids)
    for role, player_id in (
        ("captain", squad.captain_id),
        ("vice-captain", squad.vice_captain_id),
    ):
        if player_id is not None and player_id not in squad_ids:
            errors.append(ValidationError(f"invalid_{role}", f"{role.title()} is not in the squad"))
        elif player_id is not None and squad.starting_player_ids and player_id not in squad.starting_player_ids:
            errors.append(
                ValidationError(f"invalid_{role}", f"{role.title()} must be in the starting XI")
            )

    if (
        squad.captain_id is not None
        and squad.vice_captain_id is not None
        and squad.captain_id == squad.vice_captain_id
    ):
        errors.append(
            ValidationError("captain_conflict", "Captain and vice-captain must be different players")
        )

    return tuple(errors)


def _validate_lineup(squad: Squad, rules: SeasonRules) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if len(squad.starting_player_ids) != rules.squad.starting_size:
        errors.append(
            ValidationError(
                "starting_size",
                f"Starting lineup must contain {rules.squad.starting_size} players",
            )
        )

    players_by_id = {player.player_id: player for player in squad.players}
    unknown = squad.starting_player_ids - players_by_id.keys()
    if unknown:
        errors.append(
            ValidationError("unknown_starter", f"Starting lineup contains unknown IDs: {sorted(unknown)}")
        )
        return errors

    starters = [players_by_id[player_id] for player_id in squad.starting_player_ids]
    counts = Counter(player.position.value for player in starters)
    for position, minimum in rules.squad.formation_min.items():
        maximum = rules.squad.formation_max[position]
        actual = counts[position]
        if not minimum <= actual <= maximum:
            errors.append(
                ValidationError(
                    "formation",
                    f"Starting {position} count must be between {minimum} and {maximum}, found {actual}",
                )
            )
    return errors


def calculate_player_points(
    player: Player,
    stats: PlayerGameweekStats,
    rules: SeasonRules,
) -> int:
    """Calculate deterministic FPL points from a player's recorded Gameweek statistics."""
    scoring = rules.scoring
    position = player.position.value
    points = 0

    if stats.minutes > 0:
        points += (
            scoring.appearance_60_or_more
            if stats.minutes >= 60
            else scoring.appearance_under_60
        )

    points += stats.goals * scoring.goals[position]
    points += stats.assists * scoring.assists

    if stats.clean_sheet and stats.minutes >= scoring.goals_conceded_threshold_minutes:
        points += scoring.clean_sheets[position]

    if stats.minutes >= scoring.goals_conceded_threshold_minutes and stats.goals_conceded > 0:
        points += (stats.goals_conceded // 2) * scoring.goals_conceded_per_two[position]

    if player.position == Position.GK:
        points += (stats.saves // scoring.saves_per_point)
        points += stats.penalties_saved * scoring.penalty_save

    points += stats.penalties_missed * scoring.penalty_miss
    points += stats.yellow_cards * scoring.yellow_card
    points += stats.red_cards * scoring.red_card
    points += stats.own_goals * scoring.own_goal
    points += min(max(stats.bonus, 0), scoring.bonus_max)

    threshold = scoring.defensive_contribution_thresholds[position]
    if stats.defensive_contributions >= threshold:
        points += scoring.defensive_contribution_points

    return points
