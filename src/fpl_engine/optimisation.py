"""Exact discrete optimisation for FPL selection decisions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, permutations, product

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
class GameweekLineupPlan:
    """One Gameweek's lineup decision.

    The bench belongs here, not only on the squad: the optimiser may start a
    different XI each Gameweek, so a squad-level bench order is only correct
    for the Gameweek it was computed against.
    """

    gameweek_number: int
    starting_player_ids: frozenset[str]
    captain_id: str
    vice_captain_id: str
    bench_player_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FullSquadResult:
    """A selected squad and the two distinct values it is scored on.

    `horizon_expected_points` is the solver's **primary objective**: legal-XI
    plus captain points summed over every projected Gameweek. It is the
    quantity CBC proves optimal, and it is the only figure that may be
    compared across squads to rank them.

    `gameweek_expected_points` is the **exact current-Gameweek value**:
    starters, autosub activation and captain/vice fallback integrated over
    joint appearance outcomes. It covers one Gameweek only, so it is larger
    than the primary objective's per-Gameweek share and must never be read as
    the objective. It is maximised lexicographically *within* the set of
    solutions that hold `horizon_expected_points` at its optimum.
    """

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
    gameweek_plans: tuple[GameweekLineupPlan, ...]
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
    excluded_starting_xis: tuple[frozenset[str], ...] = (),
) -> FullSquadResult:
    """Select a legal squad and weekly lineup, then value autosubs exactly.

    The legal-XI-plus-captain objective is massively degenerate: bench
    composition, bench order and the vice-captain are all absent from it, so
    many squads tie at the proven optimum and CBC returns an arbitrary one.
    Selection therefore proceeds lexicographically. Stage one proves the
    primary optimum. Stage two pins it and maximises current-Gameweek bench
    quality, which is a linear surrogate for autosub value. Stage three pins
    that too and breaks any remaining tie deterministically. Bench order and
    the captain/vice pair are then chosen by exact enumeration, which needs no
    surrogate because the state space is small.
    """

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
    # The primary objective is expected FPL points from a legal XI plus its
    # captain in every projected Gameweek. Uncertainty, bank and terminal
    # heuristics are reported separately rather than silently priced.
    # Vice-captain variables are deliberately absent: the fallback term is a
    # product of two binaries, and it is resolved exactly after the solve by
    # _optimal_captaincy rather than linearised here.
    primary_objective = pulp.lpSum(
        (
            starter_vars[(gameweek, player.source_player_id)]
            + captain_vars[(gameweek, player.source_player_id)]
        )
        * round(values[(gameweek, player.source_player_id)].expected_points, 6)
        for gameweek in gameweeks
        for player in ordered
    )
    problem += primary_objective
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
    current_gameweek = gameweeks[0]
    for index, excluded in enumerate(excluded_squads):
        known_ids = excluded & set(squad_vars)
        if known_ids:
            problem += (
                pulp.lpSum(squad_vars[player_id] for player_id in known_ids)
                <= rules.squad.squad_size - 1,
                f"exclude_squad_{index}",
            )
    for index, excluded in enumerate(excluded_starting_xis):
        known_ids = excluded & set(squad_vars)
        if known_ids:
            problem += (
                pulp.lpSum(
                    starter_vars[(current_gameweek, player_id)]
                    for player_id in known_ids
                )
                <= rules.squad.starting_size - 1,
                f"exclude_starting_xi_{index}",
            )

    solver = pulp.PULP_CBC_CMD(msg=False)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            f"Full-squad optimisation did not prove an optimum: {status}"
        )
    primary_optimum = float(pulp.value(primary_objective))
    problem += (
        primary_objective >= primary_optimum - 1e-6,
        "primary_optimum",
    )

    # Bench players contribute nothing to the primary objective, so without a
    # secondary stage the solver is free to complete the squad with the
    # cheapest legal fillers. Their projected points are a linear surrogate
    # for the autosub value that cannot be written as a linear expression.
    bench_objective = pulp.lpSum(
        (
            squad_vars[player.source_player_id]
            - starter_vars[(current_gameweek, player.source_player_id)]
        )
        * round(
            values[(current_gameweek, player.source_player_id)].expected_points,
            6,
        )
        for player in ordered
    )
    problem.setObjective(bench_objective)
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            f"Full-squad bench tie-break did not prove an optimum: {status}"
        )
    bench_optimum = float(pulp.value(bench_objective))
    problem += (bench_objective >= bench_optimum - 1e-6, "bench_optimum")

    # Any solutions still tied are equivalent on both objectives. Choose one
    # by candidate order so repeated runs on identical inputs agree.
    problem.sense = pulp.LpMinimize
    problem.setObjective(
        pulp.lpSum(
            (
                squad_vars[player.source_player_id]
                + starter_vars[(current_gameweek, player.source_player_id)]
            )
            * index
            for index, player in enumerate(ordered)
        )
    )
    status_code = problem.solve(solver)
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            "Full-squad deterministic tie-break did not prove an optimum: "
            f"{status}"
        )
    selected = tuple(
        player
        for player in ordered
        if (squad_vars[player.source_player_id].value() or 0) > 0.5
    )
    plans = []
    for gameweek in gameweeks:
        gameweek_starters = tuple(
            player
            for player in selected
            if (
                starter_vars[(gameweek, player.source_player_id)].value() or 0
            )
            > 0.5
        )
        # The solve fixes who starts. Captaincy is then resolved exactly: the
        # vice-captain never entered the objective, so the solver's choice
        # carries no information at all.
        gameweek_values = {
            player.source_player_id: _value_for_gameweek(player, gameweek)
            for player in gameweek_starters
        }
        captain_id, vice_id = _optimal_captaincy(
            gameweek_starters,
            points={
                player_id: value.expected_points
                for player_id, value in gameweek_values.items()
            },
            appearance={
                player_id: value.appearance_probability
                for player_id, value in gameweek_values.items()
            },
        )
        gameweek_starter_ids = frozenset(
            player.source_player_id for player in gameweek_starters
        )
        # Each Gameweek gets its own legal bench and exact order. Reusing the
        # opening Gameweek's bench would leave a rotated-in starter listed as a
        # substitute and the newly benched player missing altogether, which is
        # not a squad the autosub rules can be applied to.
        gameweek_points, gameweek_appearance = _gameweek_value_maps(
            selected,
            gameweek,
        )
        gameweek_bench, _, _, _ = _best_bench_order(
            selected,
            gameweek_starter_ids,
            captain_id,
            vice_id,
            rules,
            points=gameweek_points,
            appearance=gameweek_appearance,
        )
        plans.append(
            GameweekLineupPlan(
                gameweek_number=gameweek,
                starting_player_ids=gameweek_starter_ids,
                captain_id=captain_id,
                vice_captain_id=vice_id,
                bench_player_ids=gameweek_bench,
            )
        )
    starter_ids = plans[0].starting_player_ids
    current_starters = tuple(
        player
        for player in selected
        if player.source_player_id in starter_ids
    )
    # The reported weekly value uses each player's headline current-Gameweek
    # fields, so captaincy for this Gameweek is resolved against those same
    # fields rather than the per-Gameweek projection rows.
    captain_id, vice_id = _optimal_captaincy(
        current_starters,
        points={
            player.source_player_id: _gameweek_points(player)
            for player in current_starters
        },
        appearance={
            player.source_player_id: player.appearance_probability
            for player in current_starters
        },
    )
    headline_points = {
        player.source_player_id: _gameweek_points(player)
        for player in selected
    }
    headline_appearance = {
        player.source_player_id: player.appearance_probability
        for player in selected
    }
    bench, expected, bench_contribution, captain_contribution = (
        _best_bench_order(
            selected,
            starter_ids,
            captain_id,
            vice_id,
            rules,
            points=headline_points,
            appearance=headline_appearance,
        )
    )
    plans[0] = GameweekLineupPlan(
        gameweek_number=plans[0].gameweek_number,
        starting_player_ids=starter_ids,
        captain_id=captain_id,
        vice_captain_id=vice_id,
        bench_player_ids=bench,
    )
    gameweek_plans = tuple(plans)
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
                sum(
                    values[(plan.gameweek_number, player_id)].expected_points
                    for player_id in plan.starting_player_ids
                )
                + values[
                    (plan.gameweek_number, plan.captain_id)
                ].expected_points
                for plan in gameweek_plans
            ),
            3,
        ),
        gameweek_expected_points=round(expected, 3),
        expected_bench_contribution=round(bench_contribution, 3),
        expected_captain_contribution=round(captain_contribution, 3),
        gameweek_plans=gameweek_plans,
        solver_status=status,
        proof=(
            "CBC returned Optimal for the projected multi-Gameweek legal-XI "
            "and captain objective, then again with that objective pinned "
            "while current-Gameweek bench quality was maximised, and a third "
            "time for a deterministic tie-break; "
            "bench order and the captain/vice pair were then chosen by exact "
            "enumeration, so "
            "the reported weekly value exactly integrates independent player "
            "appearance outcomes with legal autosubs and captain fallback. "
            "Bench quality is a linear surrogate for autosub value, so the "
            "squad is optimal for the primary objective and best-in-class "
            "rather than proven optimal for the weekly value."
        ),
    )


def optimise_opening_squads(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    alternative_count: int = 2,
) -> OpeningSquadRecommendation:
    """Return a robust opening squad and genuinely distinct alternatives.

    Excluding only the exact fifteen produced alternatives that reused the
    same XI and swapped a bench filler, which is not a decision the manager
    can act on. Each alternative must now differ in its starting XI as well.
    """

    if alternative_count < 0:
        raise ValueError("Alternative count cannot be negative")
    results: list[FullSquadResult] = []
    excluded_squads: list[frozenset[str]] = []
    excluded_starting_xis: list[frozenset[str]] = []
    for _ in range(alternative_count + 1):
        result = optimise_full_squad(
            candidates,
            budget_tenths=budget_tenths,
            rules=rules,
            excluded_squads=tuple(excluded_squads),
            excluded_starting_xis=tuple(excluded_starting_xis),
        )
        results.append(result)
        excluded_squads.append(
            frozenset(player.source_player_id for player in result.players)
        )
        excluded_starting_xis.append(result.starting_player_ids)
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


def _optimal_captaincy(
    starters: tuple[CandidatePlayer, ...],
    *,
    points: dict[str, float],
    appearance: dict[str, float],
) -> tuple[str, str]:
    """Choose the captain and vice-captain jointly.

    The armband is worth `captain + P(captain absent) x vice`, but only the
    first term reaches the solver's objective, so the vice-captain has to be
    resolved here. Captaincy stays restricted to starters at the maximum
    projected points: the primary objective already counts captain points, so
    demoting a top scorer would lower a value that has been proven optimal.
    Within that restriction the fallback term is free, and a captain with a
    lower appearance probability can be preferred because it puts more weight
    on the vice-captain.
    """

    if len(starters) < 2:
        raise OptimisationError(
            "Captaincy requires at least two starters"
        )
    best_points = max(points[player.source_player_id] for player in starters)
    eligible_captains = tuple(
        player
        for player in starters
        if points[player.source_player_id] >= best_points - 1e-9
    )
    best_key: tuple[float, str, str] | None = None
    for captain in eligible_captains:
        captain_id = captain.source_player_id
        for vice in starters:
            vice_id = vice.source_player_id
            if vice_id == captain_id:
                continue
            value = points[captain_id] + (
                (1.0 - appearance[captain_id]) * points[vice_id]
            )
            # Negated so the smallest key is the highest value, leaving the
            # identifiers to break ties in a stable direction.
            key = (-round(value, 9), captain_id, vice_id)
            if best_key is None or key < best_key:
                best_key = key
    if best_key is None:
        raise OptimisationError("No legal captain and vice-captain pair exists")
    return best_key[1], best_key[2]


def _best_bench_order(
    selected: tuple[CandidatePlayer, ...],
    starter_ids: frozenset[str],
    captain_id: str,
    vice_id: str,
    rules: SeasonRules,
    *,
    points: dict[str, float],
    appearance: dict[str, float],
) -> tuple[tuple[str, ...], float, float, float]:
    """Order the bench to maximise exact autosub value.

    Ordering by projected points is not optimal, because a substitute only
    scores when a starter is absent *and* the resulting formation stays legal.
    A cheap high-scoring forward behind two defenders can be skipped in most
    states. Three outfield substitutes give six orderings, so the exact value
    is enumerable and no heuristic is needed.
    """

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
        key=lambda player: player.source_player_id,
    )
    best_key: tuple[float, tuple[str, ...]] | None = None
    best: tuple[tuple[str, ...], float, float, float] | None = None
    for ordering in permutations(outfield):
        bench = (
            goalkeeper.source_player_id,
            *(player.source_player_id for player in ordering),
        )
        expected, bench_contribution, captain_contribution = (
            _expected_weekly_score(
                selected,
                starter_ids,
                bench,
                captain_id,
                vice_id,
                rules,
                points=points,
                appearance=appearance,
            )
        )
        # Negated so the smallest key is the highest value, leaving the bench
        # order itself to break ties in a stable direction.
        key = (-round(expected, 9), bench)
        if best_key is None or key < best_key:
            best_key = key
            best = (bench, expected, bench_contribution, captain_contribution)
    if best is None:
        raise OptimisationError("The squad has no legal bench ordering")
    return best


def _expected_weekly_score(
    selected: tuple[CandidatePlayer, ...],
    starter_ids: frozenset[str],
    bench: tuple[str, ...],
    captain_id: str,
    vice_id: str,
    rules: SeasonRules,
    *,
    points: dict[str, float],
    appearance: dict[str, float],
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
    without_bench = sum(points[player_id] for player_id in starter_ids)
    starting_goalkeeper = next(
        player
        for player in selected
        if player.source_player_id in starter_ids
        and player.position == Position.GK
    )
    bench_contribution = (
        1.0 - appearance[starting_goalkeeper.source_player_id]
    ) * points[bench[0]]
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
            appearance_probability = appearance[player.source_player_id]
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
            _conditional_points(
                bench_outfield[index].source_player_id,
                points,
                appearance,
            )
            for index in used_bench_indexes
        )
    expected_captain = points[captain_id] + (
        (1.0 - appearance[captain_id]) * points[vice_id]
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
    return _cached_used_outfield_bench_indexes(
        starter_positions,
        bench_positions,
        outcomes,
        tuple(
            (
                position.value,
                rules.squad.formation_min[position.value],
                rules.squad.formation_max[position.value],
            )
            for position in (Position.DEF, Position.MID, Position.FWD)
        ),
    )


# Choosing a bench order re-values every joint appearance state once per
# ordering. The legal substitution set depends only on positions and
# outcomes, so it is shared across orderings and across squads.
@lru_cache(maxsize=100_000)
def _cached_used_outfield_bench_indexes(
    starter_positions: tuple[Position, ...],
    bench_positions: tuple[Position, ...],
    outcomes: tuple[bool, ...],
    formation_bounds: tuple[tuple[str, int, int], ...],
) -> tuple[int, ...]:
    minimums = {name: minimum for name, minimum, _ in formation_bounds}
    maximums = {name: maximum for name, _, maximum in formation_bounds}
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
                    not minimums[position.value]
                    <= counts[position]
                    <= maximums[position.value]
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


def _gameweek_value_maps(
    selected: tuple[CandidatePlayer, ...],
    gameweek_number: int,
) -> tuple[dict[str, float], dict[str, float]]:
    values = {
        player.source_player_id: _value_for_gameweek(player, gameweek_number)
        for player in selected
    }
    return (
        {
            player_id: value.expected_points
            for player_id, value in values.items()
        },
        {
            player_id: value.appearance_probability
            for player_id, value in values.items()
        },
    )


def _conditional_points(
    player_id: str,
    points: dict[str, float],
    appearance: dict[str, float],
) -> float:
    if appearance[player_id] <= 0:
        return 0.0
    return points[player_id] / appearance[player_id]
