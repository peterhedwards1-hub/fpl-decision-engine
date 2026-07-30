"""Exact discrete optimisation for FPL selection decisions."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product

from .config import SeasonRules
from .domain import Player, Position, Squad
from .rules import validate_squad


class OptimisationError(RuntimeError):
    """Raised when no proven optimal solution can be produced."""


@dataclass(frozen=True)
class GameweekPlayerValue:
    gameweek_number: int
    expected_points: float
    appearance_probability: float = 1.0
    sixty_probability: float = 1.0


@dataclass(frozen=True)
class CandidatePlayer:
    source_player_id: str
    web_name: str
    team_id: str
    team_short_name: str
    position: Position
    price_tenths: int
    expected_points: float
    gameweek_expected_points: float | None = None
    appearance_probability: float = 1.0
    uncertainty: float = 0.0
    residual_value: float = 0.0
    gameweek_values: tuple[GameweekPlayerValue, ...] = ()


@dataclass(frozen=True)
class StartingXIResult:
    players: tuple[CandidatePlayer, ...]
    total_cost_tenths: int
    expected_points: float
    solver_status: str
    proof: str
    near_selected: tuple[CandidatePlayer, ...]


@dataclass(frozen=True)
class FullSquadResult:
    players: tuple[CandidatePlayer, ...]
    starting_player_ids: frozenset[str]
    bench_player_ids: tuple[str, ...]
    captain_id: str
    vice_captain_id: str
    total_cost_tenths: int
    horizon_expected_points: float
    gameweek_expected_points: float
    expected_bench_contribution: float
    expected_captain_contribution: float
    solver_status: str
    proof: str


@dataclass(frozen=True)
class OpeningSquadRecommendation:
    primary: FullSquadResult
    alternatives: tuple[FullSquadResult, ...]
    objective: str
    assumptions: tuple[str, ...]
    transfer_triggers: tuple[str, ...]


def optimise_starting_xi(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
) -> StartingXIResult:
    """Return a solver-proven optimal legal XI for the supplied objective."""

    try:
        import pulp
    except ImportError as error:
        raise OptimisationError(
            "Exact optimisation requires the 'optimize' project dependency"
        ) from error

    if budget_tenths <= 0:
        raise ValueError("Budget must be positive")
    if len({candidate.source_player_id for candidate in candidates}) != len(
        candidates
    ):
        raise ValueError("Candidate player IDs must be unique")

    ordered = tuple(sorted(candidates, key=lambda player: player.source_player_id))
    problem = pulp.LpProblem("fpl_starting_xi", pulp.LpMaximize)
    selected = {
        player.source_player_id: pulp.LpVariable(
            f"select_{index}", cat=pulp.LpBinary
        )
        for index, player in enumerate(ordered)
    }
    primary_objective = pulp.lpSum(
        selected[player.source_player_id]
        * round(player.expected_points, 6)
        for player in ordered
    )
    problem += primary_objective
    problem += (
        pulp.lpSum(selected.values()) == rules.squad.starting_size,
        "starting_size",
    )
    problem += (
        pulp.lpSum(
            selected[player.source_player_id] * player.price_tenths
            for player in ordered
        )
        <= budget_tenths,
        "budget",
    )
    for position in Position:
        position_total = pulp.lpSum(
            selected[player.source_player_id]
            for player in ordered
            if player.position == position
        )
        problem += (
            position_total >= rules.squad.formation_min[position.value],
            f"{position.value}_minimum",
        )
        problem += (
            position_total <= rules.squad.formation_max[position.value],
            f"{position.value}_maximum",
        )
    for team_id in sorted({player.team_id for player in ordered}):
        problem += (
            pulp.lpSum(
                selected[player.source_player_id]
                for player in ordered
                if player.team_id == team_id
            )
            <= rules.squad.max_players_per_team,
            f"club_{team_id}_limit",
        )

    solver = pulp.PULP_CBC_CMD(msg=False)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            f"Starting-XI optimisation did not prove an optimum: {status}"
        )
    primary_optimum = float(pulp.value(primary_objective))
    problem += primary_objective >= primary_optimum - 1e-7
    cost_objective = pulp.lpSum(
        selected[player.source_player_id] * player.price_tenths
        for player in ordered
    )
    problem.sense = pulp.LpMinimize
    problem.setObjective(cost_objective)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            "Starting-XI cost tie-break did not prove an optimum: "
            f"{status}"
        )
    minimum_cost = round(float(pulp.value(cost_objective)))
    problem += cost_objective == minimum_cost
    stable_order_objective = pulp.lpSum(
        selected[player.source_player_id] * index
        for index, player in enumerate(ordered)
    )
    problem.setObjective(stable_order_objective)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            "Starting-XI deterministic tie-break did not prove an optimum: "
            f"{status}"
        )
    chosen = tuple(
        player
        for player in ordered
        if selected[player.source_player_id].value() is not None
        and selected[player.source_player_id].value() > 0.5
    )
    chosen_ids = {player.source_player_id for player in chosen}
    near_selected = tuple(
        sorted(
            (
                player
                for player in ordered
                if player.source_player_id not in chosen_ids
            ),
            key=lambda player: (-player.expected_points, player.price_tenths),
        )[:5]
    )
    total_cost = sum(player.price_tenths for player in chosen)
    expected_points = sum(player.expected_points for player in chosen)
    formation = {
        position.value: sum(player.position == position for player in chosen)
        for position in Position
    }
    return StartingXIResult(
        players=chosen,
        total_cost_tenths=total_cost,
        expected_points=round(expected_points, 3),
        solver_status=status,
        proof=(
            "CBC returned Optimal for all supplied candidates under budget, "
            f"club and formation constraints; formation {formation}."
        ),
        near_selected=near_selected,
    )


def optimise_full_squad(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    excluded_squads: tuple[frozenset[str], ...] = (),
) -> FullSquadResult:
    """Select a legal squad and weekly lineup, then value autosubs exactly."""

    try:
        import pulp
    except ImportError as error:
        raise OptimisationError(
            "Exact optimisation requires the 'optimize' project dependency"
        ) from error
    if any(
        not 0.0 <= player.appearance_probability <= 1.0
        for player in candidates
    ):
        raise ValueError("Appearance probabilities must be between zero and one")

    ordered = tuple(sorted(candidates, key=lambda player: player.source_player_id))
    problem = pulp.LpProblem("fpl_full_squad", pulp.LpMaximize)
    squad_vars = {
        player.source_player_id: pulp.LpVariable(
            f"squad_{index}", cat=pulp.LpBinary
        )
        for index, player in enumerate(ordered)
    }
    gameweeks = _optimisation_gameweeks(ordered)
    values = {
        (gameweek, player.source_player_id): _value_for_gameweek(
            player, gameweek
        )
        for gameweek in gameweeks
        for player in ordered
    }
    starter_vars = {
        (gameweek, player.source_player_id): pulp.LpVariable(
            f"starter_{gameweek}_{index}", cat=pulp.LpBinary
        )
        for gameweek in gameweeks
        for index, player in enumerate(ordered)
    }
    captain_vars = {
        (gameweek, player.source_player_id): pulp.LpVariable(
            f"captain_{gameweek}_{index}", cat=pulp.LpBinary
        )
        for gameweek in gameweeks
        for index, player in enumerate(ordered)
    }
    vice_vars = {
        (gameweek, player.source_player_id): pulp.LpVariable(
            f"vice_{gameweek}_{index}", cat=pulp.LpBinary
        )
        for gameweek in gameweeks
        for index, player in enumerate(ordered)
    }
    # The primary objective is expected FPL points from a legal XI plus its
    # captain in every projected Gameweek. Uncertainty, bank and terminal
    # heuristics are reported separately rather than silently priced.
    problem += pulp.lpSum(
        starter_vars[(gameweek, player.source_player_id)]
        * values[(gameweek, player.source_player_id)].expected_points
        + captain_vars[(gameweek, player.source_player_id)]
        * values[(gameweek, player.source_player_id)].expected_points
        for gameweek in gameweeks
        for player in ordered
    )
    problem += (
        pulp.lpSum(squad_vars.values()) == rules.squad.squad_size,
        "squad_size",
    )
    problem += (
        pulp.lpSum(
            squad_vars[player.source_player_id] * player.price_tenths
            for player in ordered
        )
        <= budget_tenths,
        "budget",
    )
    for gameweek in gameweeks:
        problem += (
            pulp.lpSum(
                starter_vars[(gameweek, player.source_player_id)]
                for player in ordered
            )
            == rules.squad.starting_size,
            f"starting_size_{gameweek}",
        )
        problem += (
            pulp.lpSum(
                captain_vars[(gameweek, player.source_player_id)]
                for player in ordered
            )
            == 1,
            f"one_captain_{gameweek}",
        )
        problem += (
            pulp.lpSum(
                vice_vars[(gameweek, player.source_player_id)]
                for player in ordered
            )
            == 1,
            f"one_vice_{gameweek}",
        )
        for player in ordered:
            player_id = player.source_player_id
            problem += (
                starter_vars[(gameweek, player_id)]
                <= squad_vars[player_id],
                f"starter_in_squad_{gameweek}_{player_id}",
            )
            problem += (
                captain_vars[(gameweek, player_id)]
                <= starter_vars[(gameweek, player_id)],
                f"captain_starts_{gameweek}_{player_id}",
            )
            problem += (
                vice_vars[(gameweek, player_id)]
                <= starter_vars[(gameweek, player_id)],
                f"vice_starts_{gameweek}_{player_id}",
            )
            problem += (
                captain_vars[(gameweek, player_id)]
                + vice_vars[(gameweek, player_id)]
                <= 1,
                f"roles_distinct_{gameweek}_{player_id}",
            )
    for position in Position:
        squad_position_total = pulp.lpSum(
            squad_vars[player.source_player_id]
            for player in ordered
            if player.position == position
        )
        problem += (
            squad_position_total == rules.squad.position_counts[position.value],
            f"squad_{position.value}",
        )
        for gameweek in gameweeks:
            starter_position_total = pulp.lpSum(
                starter_vars[(gameweek, player.source_player_id)]
                for player in ordered
                if player.position == position
            )
            problem += (
                starter_position_total
                >= rules.squad.formation_min[position.value],
                f"starter_{gameweek}_{position.value}_minimum",
            )
            problem += (
                starter_position_total
                <= rules.squad.formation_max[position.value],
                f"starter_{gameweek}_{position.value}_maximum",
            )
    for team_id in sorted({player.team_id for player in ordered}):
        problem += (
            pulp.lpSum(
                squad_vars[player.source_player_id]
                for player in ordered
                if player.team_id == team_id
            )
            <= rules.squad.max_players_per_team,
            f"club_{team_id}_limit",
        )
    for index, excluded in enumerate(excluded_squads):
        known_ids = excluded & set(squad_vars)
        if known_ids:
            problem += (
                pulp.lpSum(squad_vars[player_id] for player_id in known_ids)
                <= rules.squad.squad_size - 1,
                f"exclude_squad_{index}",
            )

    status_code = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            f"Full-squad optimisation did not prove an optimum: {status}"
        )
    selected = tuple(
        player
        for player in ordered
        if (squad_vars[player.source_player_id].value() or 0) > 0.5
    )
    current_gameweek = gameweeks[0]
    starter_ids = frozenset(
        player.source_player_id
        for player in selected
        if (
            starter_vars[
                (current_gameweek, player.source_player_id)
            ].value()
            or 0
        )
        > 0.5
    )
    captain_id = next(
        player.source_player_id
        for player in selected
        if (
            captain_vars[
                (current_gameweek, player.source_player_id)
            ].value()
            or 0
        )
        > 0.5
    )
    vice_id = next(
        player.source_player_id
        for player in selected
        if (
            vice_vars[
                (current_gameweek, player.source_player_id)
            ].value()
            or 0
        )
        > 0.5
    )
    bench = _ordered_bench(selected, starter_ids)
    expected, bench_contribution, captain_contribution = _expected_weekly_score(
        selected,
        starter_ids,
        bench,
        captain_id,
        vice_id,
        rules,
    )
    squad = _domain_squad(
        selected, starter_ids, bench, captain_id, vice_id
    )
    errors = validate_squad(squad, rules, check_budget=False)
    if errors:
        raise OptimisationError(
            "Solver returned an invalid squad: "
            + "; ".join(error.message for error in errors)
        )
    return FullSquadResult(
        players=selected,
        starting_player_ids=starter_ids,
        bench_player_ids=bench,
        captain_id=captain_id,
        vice_captain_id=vice_id,
        total_cost_tenths=sum(player.price_tenths for player in selected),
        horizon_expected_points=round(
            sum(
                values[(gameweek, player.source_player_id)].expected_points
                * (
                    (starter_vars[(gameweek, player.source_player_id)].value() or 0)
                    + (captain_vars[(gameweek, player.source_player_id)].value() or 0)
                )
                for gameweek in gameweeks
                for player in selected
            ),
            3,
        ),
        gameweek_expected_points=round(expected, 3),
        expected_bench_contribution=round(bench_contribution, 3),
        expected_captain_contribution=round(captain_contribution, 3),
        solver_status=status,
        proof=(
            "CBC returned Optimal for the projected multi-Gameweek legal-XI "
            "and captain objective; "
            "the reported weekly value exactly integrates independent player "
            "appearance outcomes with legal autosubs and captain fallback."
        ),
    )


def optimise_opening_squads(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    alternative_count: int = 2,
) -> OpeningSquadRecommendation:
    """Return a robust opening squad and distinct near-optimal alternatives."""

    if alternative_count < 0:
        raise ValueError("Alternative count cannot be negative")
    results: list[FullSquadResult] = []
    exclusions: list[frozenset[str]] = []
    for _ in range(alternative_count + 1):
        result = optimise_full_squad(
            candidates,
            budget_tenths=budget_tenths,
            rules=rules,
            excluded_squads=tuple(exclusions),
        )
        results.append(result)
        exclusions.append(
            frozenset(player.source_player_id for player in result.players)
        )
    primary = results[0]
    uncertain = sorted(
        (
            player
            for player in primary.players
            if player.appearance_probability < 0.85 or player.uncertainty > 5.0
        ),
        key=lambda player: (
            player.appearance_probability,
            -player.uncertainty,
        ),
    )
    triggers = tuple(
        f"Re-run if {player.web_name}'s expected role or availability changes"
        for player in uncertain[:5]
    ) or (
        "Re-run after material injury, role, transfer or fixture news",
        "Re-run if price changes make the squad unaffordable",
    )
    return OpeningSquadRecommendation(
        primary=primary,
        alternatives=tuple(results[1:]),
        objective=(
            "Projected legal-XI and captain points across every Gameweek in "
            "the active horizon, with exact current-week autosub valuation"
        ),
        assumptions=(
            "Uncertainty is disclosed but does not receive an arbitrary points penalty",
            "No terminal value is added until it can be measured beyond the horizon",
            "Alternative squads must differ by at least one player",
        ),
        transfer_triggers=triggers,
    )


def _ordered_bench(
    selected: tuple[CandidatePlayer, ...], starter_ids: frozenset[str]
) -> tuple[str, ...]:
    substitutes = [
        player
        for player in selected
        if player.source_player_id not in starter_ids
    ]
    goalkeeper = next(
        player for player in substitutes if player.position == Position.GK
    )
    outfield = sorted(
        (player for player in substitutes if player.position != Position.GK),
        key=lambda player: (
            -_gameweek_points(player),
            player.source_player_id,
        ),
    )
    return (
        goalkeeper.source_player_id,
        *(player.source_player_id for player in outfield),
    )


def _expected_weekly_score(
    selected: tuple[CandidatePlayer, ...],
    starter_ids: frozenset[str],
    bench: tuple[str, ...],
    captain_id: str,
    vice_id: str,
    rules: SeasonRules,
) -> tuple[float, float, float]:
    by_id = {player.source_player_id: player for player in selected}
    domain_squad = _domain_squad(
        selected, starter_ids, bench, captain_id, vice_id
    )
    errors = validate_squad(domain_squad, rules, check_budget=False)
    if errors:
        raise OptimisationError(
            "Cannot value an invalid solver squad: "
            + "; ".join(error.message for error in errors)
        )
    # A starter's projected points already include their non-appearance
    # probability. Only bench activation depends on the joint appearance
    # state, so enumerate the 10 outfield starters and three outfield
    # substitutes rather than repeatedly invoking the full rule engine for
    # all 2^15 states.
    without_bench = sum(
        _gameweek_points(by_id[player_id]) for player_id in starter_ids
    )
    starting_goalkeeper = next(
        player
        for player in selected
        if player.source_player_id in starter_ids
        and player.position == Position.GK
    )
    bench_goalkeeper = by_id[bench[0]]
    bench_contribution = (
        (1.0 - starting_goalkeeper.appearance_probability)
        * _gameweek_points(bench_goalkeeper)
    )
    starting_outfield = tuple(
        player
        for player in selected
        if player.source_player_id in starter_ids
        and player.position != Position.GK
    )
    bench_outfield = tuple(by_id[player_id] for player_id in bench[1:])
    state_players = (*starting_outfield, *bench_outfield)
    for outcomes in product((False, True), repeat=len(state_players)):
        probability = 1.0
        for player, did_appear in zip(state_players, outcomes, strict=True):
            appearance_probability = player.appearance_probability
            probability *= (
                appearance_probability
                if did_appear
                else 1.0 - appearance_probability
            )
        if probability == 0:
            continue
        used_bench_indexes = _used_outfield_bench_indexes(
            tuple(player.position for player in starting_outfield),
            tuple(player.position for player in bench_outfield),
            outcomes,
            rules,
        )
        bench_contribution += probability * sum(
            _conditional_points(bench_outfield[index])
            for index in used_bench_indexes
        )
    captain = by_id[captain_id]
    vice = by_id[vice_id]
    expected_captain = _gameweek_points(captain) + (
        (1.0 - captain.appearance_probability) * _gameweek_points(vice)
    )
    expected_base = without_bench + bench_contribution
    return (
        expected_base + expected_captain,
        max(0.0, bench_contribution),
        expected_captain,
    )


def _used_outfield_bench_indexes(
    starter_positions: tuple[Position, ...],
    bench_positions: tuple[Position, ...],
    outcomes: tuple[bool, ...],
    rules: SeasonRules,
) -> tuple[int, ...]:
    starter_count = len(starter_positions)
    absent_starters = tuple(
        index
        for index, appeared in enumerate(outcomes[:starter_count])
        if not appeared
    )
    played_bench = tuple(
        index
        for index, appeared in enumerate(outcomes[starter_count:])
        if appeared
    )
    maximum = min(len(absent_starters), len(played_bench))
    starting_counts = {
        position: sum(
            starter_position == position
            for starter_position in starter_positions
        )
        for position in Position
    }
    best_key: tuple[int, tuple[int, ...]] = (-1, ())
    best_bench: tuple[int, ...] = ()
    for substitution_count in range(maximum + 1):
        for bench_indexes in combinations(played_bench, substitution_count):
            priority = tuple(
                int(index in bench_indexes)
                for index in range(len(bench_positions))
            )
            for replaced_indexes in combinations(
                absent_starters, substitution_count
            ):
                counts = dict(starting_counts)
                for index in replaced_indexes:
                    counts[starter_positions[index]] -= 1
                for index in bench_indexes:
                    counts[bench_positions[index]] += 1
                if any(
                    not rules.squad.formation_min[position.value]
                    <= counts[position]
                    <= rules.squad.formation_max[position.value]
                    for position in (Position.DEF, Position.MID, Position.FWD)
                ):
                    continue
                key = (substitution_count, priority)
                if key > best_key:
                    best_key = key
                    best_bench = bench_indexes
                break
    return best_bench


def _domain_squad(
    selected: tuple[CandidatePlayer, ...],
    starter_ids: frozenset[str],
    bench: tuple[str, ...],
    captain_id: str,
    vice_id: str,
) -> Squad:
    numeric_ids = {
        player.source_player_id: index
        for index, player in enumerate(selected, start=1)
    }
    numeric_teams = {
        team_id: index
        for index, team_id in enumerate(
            sorted({player.team_id for player in selected}), start=1
        )
    }
    return Squad(
        players=tuple(
            Player(
                player_id=numeric_ids[player.source_player_id],
                name=player.web_name,
                team_id=numeric_teams[player.team_id],
                position=player.position,
                price_tenths=player.price_tenths,
            )
            for player in selected
        ),
        starting_player_ids=frozenset(
            numeric_ids[player_id] for player_id in starter_ids
        ),
        bench_player_ids=tuple(numeric_ids[player_id] for player_id in bench),
        captain_id=numeric_ids[captain_id],
        vice_captain_id=numeric_ids[vice_id],
    )


def _gameweek_points(player: CandidatePlayer) -> float:
    return (
        player.expected_points
        if player.gameweek_expected_points is None
        else player.gameweek_expected_points
    )


def _optimisation_gameweeks(
    players: tuple[CandidatePlayer, ...],
) -> tuple[int, ...]:
    configured = {
        value.gameweek_number
        for player in players
        for value in player.gameweek_values
    }
    if not configured:
        return (0,)
    for player in players:
        player_gameweeks = {
            value.gameweek_number for value in player.gameweek_values
        }
        if player_gameweeks != configured:
            raise ValueError(
                "Every candidate must cover the same projection Gameweeks"
            )
    return tuple(sorted(configured))


def _value_for_gameweek(
    player: CandidatePlayer, gameweek_number: int
) -> GameweekPlayerValue:
    if not player.gameweek_values:
        return GameweekPlayerValue(
            gameweek_number=gameweek_number,
            expected_points=_gameweek_points(player),
            appearance_probability=player.appearance_probability,
        )
    try:
        return next(
            value
            for value in player.gameweek_values
            if value.gameweek_number == gameweek_number
        )
    except StopIteration as error:
        raise ValueError(
            f"Player {player.source_player_id} has no value for "
            f"Gameweek {gameweek_number}"
        ) from error


def _conditional_points(player: CandidatePlayer) -> float:
    if player.appearance_probability <= 0:
        return 0.0
    return _gameweek_points(player) / player.appearance_probability
