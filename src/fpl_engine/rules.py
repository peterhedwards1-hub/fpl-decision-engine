from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from itertools import combinations

from .config import SeasonRules
from .domain import Chip, Player, PlayerGameweekStats, Position, Squad


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str


@dataclass(frozen=True)
class ResolvedLineup:
    scoring_player_ids: frozenset[int]
    substitutions: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class TeamScore:
    total_points: int
    scoring_player_ids: frozenset[int]
    effective_captain_id: int | None
    substitutions: tuple[tuple[int, int], ...]


def validate_squad(
    squad: Squad,
    rules: SeasonRules,
    *,
    check_budget: bool = True,
) -> tuple[ValidationError, ...]:
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
    if check_budget and cost > rules.squad.budget_tenths:
        errors.append(
            ValidationError(
                "budget",
                f"Squad costs {cost / 10:.1f}; budget is {rules.squad.budget_tenths / 10:.1f}",
            )
        )

    if squad.starting_player_ids:
        errors.extend(_validate_lineup(squad, rules))
        errors.extend(_validate_bench(squad, rules))

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


def _validate_bench(squad: Squad, rules: SeasonRules) -> list[ValidationError]:
    if not squad.bench_player_ids:
        return []

    errors: list[ValidationError] = []
    expected_size = rules.squad.squad_size - rules.squad.starting_size
    if len(squad.bench_player_ids) != expected_size:
        errors.append(
            ValidationError(
                "bench_size",
                f"Bench must contain {expected_size} players",
            )
        )
    if len(set(squad.bench_player_ids)) != len(squad.bench_player_ids):
        errors.append(ValidationError("duplicate_bench", "Bench contains duplicate IDs"))

    squad_ids = {player.player_id for player in squad.players}
    expected_bench = squad_ids - squad.starting_player_ids
    if set(squad.bench_player_ids) != expected_bench:
        errors.append(
            ValidationError(
                "invalid_bench",
                "Bench must contain every non-starting squad player exactly once",
            )
        )
        return errors

    players_by_id = {player.player_id: player for player in squad.players}
    if players_by_id[squad.bench_player_ids[0]].position != Position.GK:
        errors.append(
            ValidationError(
                "bench_goalkeeper",
                "The substitute goalkeeper must be first in bench order",
            )
        )
    if any(
        players_by_id[player_id].position == Position.GK
        for player_id in squad.bench_player_ids[1:]
    ):
        errors.append(
            ValidationError(
                "bench_outfield",
                "Outfield bench-priority places cannot contain a goalkeeper",
            )
        )
    return errors


def calculate_selling_price(
    purchase_price_tenths: int,
    current_price_tenths: int,
    rules: SeasonRules,
) -> int:
    """Return the FPL selling price using the configured profit-sharing rule."""

    if purchase_price_tenths < 0 or current_price_tenths < 0:
        raise ValueError("Prices cannot be negative")
    if current_price_tenths <= purchase_price_tenths:
        return current_price_tenths

    profit = current_price_tenths - purchase_price_tenths
    steps = profit // rules.selling_prices.profit_step_tenths
    return (
        purchase_price_tenths
        + steps * rules.selling_prices.profit_return_tenths
    )


def calculate_transfer_cost(
    transfers_made: int,
    available_free_transfers: int,
    rules: SeasonRules,
    *,
    active_chip: Chip | None = None,
) -> int:
    """Return the points deducted for confirmed transfers in a Gameweek."""

    if transfers_made < 0 or available_free_transfers < 0:
        raise ValueError("Transfer counts cannot be negative")
    if active_chip in {Chip.WILDCARD, Chip.FREE_HIT}:
        return 0
    paid_transfers = max(0, transfers_made - available_free_transfers)
    return paid_transfers * rules.transfers.transfer_hit_cost


def next_free_transfer_count(
    available_free_transfers: int,
    transfers_made: int,
    rules: SeasonRules,
    *,
    active_chip: Chip | None = None,
) -> int:
    """Advance the free-transfer bank to the following Gameweek."""

    if available_free_transfers < 0 or transfers_made < 0:
        raise ValueError("Transfer counts cannot be negative")
    if available_free_transfers > rules.transfers.maximum_free_transfers:
        raise ValueError("Available free transfers exceed the configured cap")
    if active_chip in {Chip.WILDCARD, Chip.FREE_HIT}:
        if not rules.chips.banked_transfers_preserved:
            return rules.transfers.initial_free_transfers
        return available_free_transfers

    remaining = max(0, available_free_transfers - transfers_made)
    return min(
        remaining + rules.transfers.free_transfers_per_gameweek,
        rules.transfers.maximum_free_transfers,
    )


def validate_chip_use(
    active_chip: Chip | None,
    gameweek_number: int,
    rules: SeasonRules,
    *,
    already_used_in_half: frozenset[Chip] = frozenset(),
    previous_gameweek_chip: Chip | None = None,
    last_used_gameweek: int | None = None,
) -> tuple[ValidationError, ...]:
    """Validate availability constraints for a proposed chip activation."""

    if active_chip is None:
        return ()
    errors: list[ValidationError] = []
    if active_chip.value not in rules.chips.names:
        errors.append(
            ValidationError("unknown_chip", f"Chip {active_chip.value!r} is not configured")
        )
    if gameweek_number in rules.chips.unavailable_gameweeks.get(
        active_chip.value, ()
    ):
        errors.append(
            ValidationError(
                "chip_unavailable",
                f"{active_chip.value} is unavailable in Gameweek {gameweek_number}",
            )
        )
    if active_chip in already_used_in_half:
        errors.append(
            ValidationError(
                "chip_already_used",
                f"{active_chip.value} has already been used in this half",
            )
        )
    minimum_gap = rules.chips.minimum_gap_gameweeks.get(active_chip.value, 0)
    effective_last_use = (
        gameweek_number - 1
        if previous_gameweek_chip == active_chip
        else last_used_gameweek
    )
    if (
        minimum_gap > 0
        and effective_last_use is not None
        and gameweek_number - effective_last_use <= minimum_gap
    ):
        errors.append(
            ValidationError(
                "chip_cooldown",
                f"{active_chip.value} requires a gap of {minimum_gap} "
                "Gameweek(s)",
            )
        )
    return tuple(errors)


def allocate_bonus_points(bps_by_player_id: Mapping[int, int]) -> dict[int, int]:
    """Allocate fixture bonus points from official BPS totals, including ties."""

    ordered_scores = sorted(set(bps_by_player_id.values()), reverse=True)
    result = {player_id: 0 for player_id in bps_by_player_id}
    players_above = 0
    for score in ordered_scores:
        player_ids = [
            player_id
            for player_id, player_score in bps_by_player_id.items()
            if player_score == score
        ]
        rank = players_above + 1
        bonus = {1: 3, 2: 2, 3: 1}.get(rank, 0)
        for player_id in player_ids:
            result[player_id] = bonus
        players_above += len(player_ids)
        if rank > 3:
            break
    return result


def resolve_automatic_substitutions(
    squad: Squad,
    minutes_by_player_id: Mapping[int, int],
    rules: SeasonRules,
    *,
    _skip_validation: bool = False,
) -> ResolvedLineup:
    """Resolve the final scoring lineup while respecting bench priority and formation."""

    # Substitution legality is independent of the season's initial £100m
    # budget. Existing squads, Wildcards and Free Hits use their caller's
    # current selling-value budget and can legitimately cost more.
    if not _skip_validation:
        errors = validate_squad(squad, rules, check_budget=False)
        if errors:
            raise ValueError(
                "Cannot resolve substitutions for an invalid squad: "
                + "; ".join(error.message for error in errors)
            )
    if not squad.bench_player_ids:
        raise ValueError("Bench order is required to resolve automatic substitutions")

    players_by_id = {player.player_id: player for player in squad.players}
    scoring_ids = {
        player_id
        for player_id in squad.starting_player_ids
        if minutes_by_player_id.get(player_id, 0) > 0
    }
    substitutions: list[tuple[int, int]] = []

    starting_goalkeeper_id = next(
        player_id
        for player_id in squad.starting_player_ids
        if players_by_id[player_id].position == Position.GK
    )
    bench_goalkeeper_id = squad.bench_player_ids[0]
    if (
        minutes_by_player_id.get(starting_goalkeeper_id, 0) == 0
        and minutes_by_player_id.get(bench_goalkeeper_id, 0) > 0
    ):
        scoring_ids.add(bench_goalkeeper_id)
        substitutions.append((starting_goalkeeper_id, bench_goalkeeper_id))

    absent_outfield = tuple(
        sorted(
            player_id
            for player_id in squad.starting_player_ids
            if players_by_id[player_id].position != Position.GK
            and minutes_by_player_id.get(player_id, 0) == 0
        )
    )
    played_bench = tuple(
        player_id
        for player_id in squad.bench_player_ids[1:]
        if minutes_by_player_id.get(player_id, 0) > 0
    )

    best_key: tuple[int, tuple[int, ...]] = (-1, ())
    best_bench: tuple[int, ...] = ()
    best_replaced: tuple[int, ...] = ()
    maximum_substitutions = min(len(absent_outfield), len(played_bench))
    for substitution_count in range(maximum_substitutions + 1):
        for bench_indexes in combinations(range(len(played_bench)), substitution_count):
            bench_ids = tuple(played_bench[index] for index in bench_indexes)
            inclusion_priority = tuple(
                int(index in bench_indexes) for index in range(len(played_bench))
            )
            for replaced_ids in combinations(absent_outfield, substitution_count):
                nominal_lineup = (
                    squad.starting_player_ids - frozenset(replaced_ids)
                ) | frozenset(bench_ids)
                if not _is_legal_formation(nominal_lineup, players_by_id, rules):
                    continue
                key = (substitution_count, inclusion_priority)
                if key > best_key:
                    best_key = key
                    best_bench = bench_ids
                    best_replaced = replaced_ids

    scoring_ids.update(best_bench)
    substitutions.extend(zip(best_replaced, best_bench, strict=True))
    return ResolvedLineup(frozenset(scoring_ids), tuple(substitutions))


def calculate_team_score(
    squad: Squad,
    points_by_player_id: Mapping[int, int],
    minutes_by_player_id: Mapping[int, int],
    rules: SeasonRules,
    *,
    active_chip: Chip | None = None,
) -> TeamScore:
    """Calculate final squad points after autosubs, captaincy and scoring chips."""

    lineup = resolve_automatic_substitutions(squad, minutes_by_player_id, rules)
    if active_chip == Chip.BENCH_BOOST:
        scoring_player_ids = frozenset(
            player.player_id
            for player in squad.players
            if minutes_by_player_id.get(player.player_id, 0) > 0
        )
    else:
        scoring_player_ids = lineup.scoring_player_ids

    total = sum(points_by_player_id.get(player_id, 0) for player_id in scoring_player_ids)
    effective_captain_id = None
    for player_id in (squad.captain_id, squad.vice_captain_id):
        if (
            player_id is not None
            and minutes_by_player_id.get(player_id, 0) > 0
        ):
            effective_captain_id = player_id
            break
    if effective_captain_id is not None:
        extra_multiplier = 2 if active_chip == Chip.TRIPLE_CAPTAIN else 1
        total += (
            points_by_player_id.get(effective_captain_id, 0)
            * extra_multiplier
        )

    return TeamScore(
        total_points=total,
        scoring_player_ids=scoring_player_ids,
        effective_captain_id=effective_captain_id,
        substitutions=lineup.substitutions,
    )


def _is_legal_formation(
    player_ids: frozenset[int] | set[int],
    players_by_id: Mapping[int, Player],
    rules: SeasonRules,
) -> bool:
    positions = tuple(Position)
    counts = Counter(
        players_by_id[player_id].position for player_id in player_ids
    )
    return _formation_signature_is_legal(
        tuple(counts[position] for position in positions),
        tuple(
            rules.squad.formation_min[position.value]
            for position in positions
        ),
        tuple(
            rules.squad.formation_max[position.value]
            for position in positions
        ),
    )


@cache
def _formation_signature_is_legal(
    counts: tuple[int, ...],
    minimums: tuple[int, ...],
    maximums: tuple[int, ...],
) -> bool:
    return all(
        minimum <= count <= maximum
        for count, minimum, maximum in zip(
            counts, minimums, maximums, strict=True
        )
    )


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

    # The 60-minute threshold applies to clean sheets, not to goals-conceded
    # deductions. Official fixture statistics already count only goals
    # conceded while the player was on the pitch.
    if stats.goals_conceded > 0:
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
