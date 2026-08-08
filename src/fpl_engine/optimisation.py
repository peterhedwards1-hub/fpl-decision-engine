"""Exact discrete optimisation for FPL selection decisions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, permutations, product
from typing import Any

from .config import SeasonRules
from .domain import Chip, Player, Position, Squad
from .rules import validate_squad

DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE = 0.6

#: Wall-clock cap, in seconds, on a single *generation* solve inside
#: :func:`enumerate_squad_ids`. Generation only proposes candidate squads; every
#: candidate is exact-rescored before it is ranked, so a solve that stops early
#: with a feasible-but-unproven incumbent can only change *which* legal squad is
#: proposed, never how one is scored or ordered. Without a cap, the linear
#: objective's massive degeneracy — every completion of one weekly XI ties
#: exactly — lets CBC spend arbitrarily long proving optimality over an
#: interchangeable tie set, which has cost multi-hour and hung runs. ``None``
#: restores uncapped, proven-optimal generation. The exact-scoring solves in
#: :func:`optimise_full_squad` and the starting-XI selection are never capped.
#:
#: 30 seconds is far more than a healthy generation solve needs — real ones
#: return in a few seconds — so the cap only bites on the degenerate proofs it
#: is meant to bound, where CBC has already found the incumbent it will return.
DEFAULT_GENERATION_TIME_LIMIT_SECONDS: float | None = 30.0


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
    """A selected squad with solver and exact appearance-state valuations.

    `lineup_expected_points` is the linear legal-XI-plus-captain quantity CBC
    optimises. `horizon_expected_points` then revalues the selected squad over
    every projected Gameweek with its own bench order, legal autosubs and
    captain fallback. `decision_value` adds any explicitly supplied residual
    player values once, beyond that horizon.

    `gameweek_expected_points` is the **exact current-Gameweek value**:
    starters, autosub activation and captain/vice fallback integrated over
    joint independent appearance outcomes.
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
    lineup_expected_points: float = 0.0
    horizon_expected_bench_contribution: float = 0.0
    terminal_value: float = 0.0
    decision_value: float = 0.0
    #: The two goalkeepers owned, the exact horizon value of the pair under
    #: its best weekly orientation, and that orientation Gameweek by Gameweek.
    goalkeeper_pair: tuple[str, ...] = ()
    goalkeeper_pair_value: float = 0.0
    goalkeeper_orientations: tuple[GoalkeeperOrientation, ...] = ()
    #: The linear objective CBC actually proved, kept so a report can say
    #: whether exact rescoring reorders the solver's own ranking.
    solver_objective: float = 0.0


@dataclass(frozen=True)
class OpeningSquadRecommendation:
    primary: FullSquadResult
    alternatives: tuple[FullSquadResult, ...]
    objective: str
    assumptions: tuple[str, ...]
    transfer_triggers: tuple[str, ...]


def mean_appearance(player: CandidatePlayer) -> float:
    """Mean availability over the candidate's supplied decision horizon."""

    if not player.gameweek_values:
        return player.appearance_probability
    return sum(
        value.appearance_probability for value in player.gameweek_values
    ) / len(player.gameweek_values)


def appearance_qualified_candidates(
    candidates: tuple[CandidatePlayer, ...],
    *,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
) -> tuple[CandidatePlayer, ...]:
    """Apply the historically evaluated no-dead-fodder opening guardrail."""

    if not 0.0 <= minimum_mean_appearance <= 1.0:
        raise ValueError("Minimum mean appearance must be between zero and one")
    return tuple(
        player
        for player in candidates
        if mean_appearance(player) >= minimum_mean_appearance
    )


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


@dataclass(frozen=True)
class GoalkeeperOrientation:
    """Which of a goalkeeper pair starts one Gameweek, and what the pair is worth.

    Goalkeepers cannot be valued one at a time. Exactly one of the two plays,
    and the reserve is not a reserve in the usual sense: when the nominated
    starter records zero minutes the substitute goalkeeper replaces them
    automatically, because no legal formation exists without a goalkeeper. The
    quantity a manager actually owns is therefore

        value_if_A_starts = A unconditional xP
                          + P(A records zero minutes) x B unconditional xP

    and the mirror for B. A goalkeeper with the lower standalone expectation
    can be the correct nomination when the partner behind them is strong and
    their own appearance is doubtful, because the pair collects from both.

    Appearance states are assumed independent, exactly as everywhere else in
    this module. That assumption is visible here: a manager's two goalkeepers
    are ordinarily at different clubs, but if they were at the same club, or
    were a first choice and their own understudy, the independence assumption
    would overstate the protection.
    """

    gameweek_number: int
    starter_id: str
    substitute_id: str
    value: float
    starter_standalone: float
    substitute_standalone: float
    alternative_value: float

    @property
    def uplift(self) -> float:
        """How much the pairing adds over the nominated starter alone."""

        return self.value - self.starter_standalone

    @property
    def prefers_lower_standalone(self) -> bool:
        """Whether protection outweighed a lower standalone expectation."""

        return self.starter_standalone < self.substitute_standalone


def goalkeeper_pair_orientation(
    first_id: str,
    second_id: str,
    *,
    gameweek_number: int,
    points: dict[str, float],
    appearance: dict[str, float],
) -> GoalkeeperOrientation:
    """The better of the two orientations for one goalkeeper pair, exactly."""

    first_value = points[first_id] + (1.0 - appearance[first_id]) * points[second_id]
    second_value = points[second_id] + (1.0 - appearance[second_id]) * points[first_id]
    # Ties resolve on identifier so a rerun on identical inputs agrees.
    if (second_value, second_id) > (first_value, first_id):
        starter, substitute = second_id, first_id
        value, alternative = second_value, first_value
    else:
        starter, substitute = first_id, second_id
        value, alternative = first_value, second_value
    return GoalkeeperOrientation(
        gameweek_number=gameweek_number,
        starter_id=starter,
        substitute_id=substitute,
        value=value,
        starter_standalone=points[starter],
        substitute_standalone=points[substitute],
        alternative_value=alternative,
    )


def goalkeeper_pair_horizon_value(
    first: CandidatePlayer,
    second: CandidatePlayer,
    gameweeks: tuple[int, ...],
) -> tuple[float, tuple[GoalkeeperOrientation, ...]]:
    """A pair's total value over the horizon, orientation chosen each Gameweek.

    The orientation is a weekly decision, not a squad-level one: a fixture
    swing or a doubt can flip which goalkeeper should be nominated without
    changing which two are owned.
    """

    orientations = []
    total = 0.0
    for gameweek in gameweeks:
        first_value = _value_for_gameweek(first, gameweek)
        second_value = _value_for_gameweek(second, gameweek)
        orientation = goalkeeper_pair_orientation(
            first.source_player_id,
            second.source_player_id,
            gameweek_number=gameweek,
            points={
                first.source_player_id: first_value.expected_points,
                second.source_player_id: second_value.expected_points,
            },
            appearance={
                first.source_player_id: first_value.appearance_probability,
                second.source_player_id: second_value.appearance_probability,
            },
        )
        orientations.append(orientation)
        total += orientation.value
    return total, tuple(orientations)


@dataclass
class SquadModel:
    """The mixed-integer model every squad search shares.

    Built once and handed to whichever search wants it. Two searches used to
    build their own copies of these constraints and would have drifted apart:
    a squad proven legal by one and scored by the other has to be legal under
    the same rules, or the comparison means nothing.
    """

    problem: Any
    ordered: tuple[CandidatePlayer, ...]
    gameweeks: tuple[int, ...]
    values: dict[tuple[int, str], GameweekPlayerValue]
    squad_vars: dict[str, Any]
    starter_vars: dict[tuple[int, str], Any]
    captain_vars: dict[tuple[int, str], Any]
    pair_vars: dict[tuple[str, str], Any]
    pair_values: dict[tuple[str, str], float]
    pair_reserve_values: dict[tuple[str, str], float]
    pair_orientations: dict[tuple[str, str], tuple[GoalkeeperOrientation, ...]]
    scored_starters: tuple[CandidatePlayer, ...]
    primary_objective: Any
    lineup_objective: Any
    pair_objective: Any
    terminal_objective: Any
    bench_objective: Any

    @property
    def current_gameweek(self) -> int:
        return self.gameweeks[0]

    def selected_ids(self) -> frozenset[str]:
        return frozenset(
            player.source_player_id
            for player in self.ordered
            if (self.squad_vars[player.source_player_id].value() or 0) > 0.5
        )

    def starting_ids(self, gameweek: int) -> frozenset[str]:
        return frozenset(
            player.source_player_id
            for player in self.ordered
            if (self.starter_vars[(gameweek, player.source_player_id)].value() or 0)
            > 0.5
        )


@dataclass(frozen=True)
class SquadGroupConstraint:
    """How many of a named set of players the squad must or may contain.

    Purely a *generation* device. It shapes which squads are produced, never
    how one is valued, and every squad it produces is scored by exactly the
    same exact objective as every other candidate.
    """

    name: str
    player_ids: frozenset[str]
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class SquadSpendConstraint:
    """How much of the budget may go to a named set of players."""

    name: str
    player_ids: frozenset[str]
    minimum_tenths: int | None = None
    maximum_tenths: int | None = None


def build_squad_model(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    excluded_squads: tuple[frozenset[str], ...] = (),
    excluded_starting_xis: tuple[frozenset[str], ...] = (),
    required_player_ids: frozenset[str] = frozenset(),
    forbidden_player_ids: frozenset[str] = frozenset(),
    group_constraints: tuple[SquadGroupConstraint, ...] = (),
    spend_constraints: tuple[SquadSpendConstraint, ...] = (),
    goalkeeper_pair_valuation: bool = True,
) -> SquadModel:
    """Assemble the legal-squad model, its objectives and its side constraints."""

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
    goalkeepers = tuple(
        player for player in ordered if player.position == Position.GK
    )
    if goalkeeper_pair_valuation and len(goalkeepers) < 2:
        raise OptimisationError(
            "Goalkeeper-pair valuation needs at least two eligible goalkeepers"
        )
    # A goalkeeper is never a sensible captain under this scoring model, and
    # allowing one would double-count a goalkeeper already valued in the pair
    # term. The post-solve captaincy enumeration still sees every starter, so
    # the exact value reported is unaffected either way.
    captainable = (
        tuple(player for player in ordered if player.position != Position.GK)
        if goalkeeper_pair_valuation
        else ordered
    )
    captain_vars = {
        (gameweek, player.source_player_id): pulp.LpVariable(
            f"captain_{gameweek}_{index}", cat=pulp.LpBinary
        )
        for gameweek in gameweeks
        for index, player in enumerate(captainable)
    }
    # Every legal ownable goalkeeper pair, with its exact horizon value under
    # the better weekly orientation. Both members must be in the squad for the
    # pair to be selected, and exactly one pair is selected, so the squad's
    # two goalkeepers and the pair are the same choice.
    pair_values: dict[tuple[str, str], float] = {}
    pair_reserve_values: dict[tuple[str, str], float] = {}
    pair_orientations: dict[tuple[str, str], tuple[GoalkeeperOrientation, ...]] = {}
    pair_vars: dict[tuple[str, str], object] = {}
    if goalkeeper_pair_valuation:
        for index, (first, second) in enumerate(combinations(goalkeepers, 2)):
            key = (first.source_player_id, second.source_player_id)
            total, orientations = goalkeeper_pair_horizon_value(
                first, second, gameweeks
            )
            pair_values[key] = total
            pair_orientations[key] = orientations
            # Reserve quality, for the tie-break stage only. When a nominated
            # goalkeeper's appearance probability saturates at one the pair
            # value is identical for every partner, and without this the
            # reserve would be settled by the deterministic index tie-break
            # rather than by who is actually the better substitute.
            pair_reserve_values[key] = sum(
                orientation.substitute_standalone for orientation in orientations
            )
            pair_vars[key] = pulp.LpVariable(
                f"gk_pair_{index}", cat=pulp.LpBinary
            )
    # The primary objective is expected FPL points from a legal XI plus its
    # captain in every projected Gameweek, with an explicitly supplied
    # residual player value added once beyond the horizon. A zero residual is
    # neutral; callers must not smuggle an undeclared heuristic into it.
    # Vice-captain variables are deliberately absent: the fallback term is a
    # product of two binaries, and it is resolved exactly after the solve by
    # _optimal_captaincy rather than linearised here.
    scored_starters = (
        tuple(player for player in ordered if player.position != Position.GK)
        if goalkeeper_pair_valuation
        else ordered
    )
    lineup_objective = pulp.lpSum(
        starter_vars[(gameweek, player.source_player_id)]
        * round(values[(gameweek, player.source_player_id)].expected_points, 6)
        for gameweek in gameweeks
        for player in scored_starters
    ) + pulp.lpSum(
        captain_vars[(gameweek, player.source_player_id)]
        * round(values[(gameweek, player.source_player_id)].expected_points, 6)
        for gameweek in gameweeks
        for player in captainable
    )
    # The goalkeepers' only appearance in the objective.
    pair_objective = pulp.lpSum(
        variable * round(pair_values[key], 6)
        for key, variable in pair_vars.items()
    )
    terminal_objective = pulp.lpSum(
        squad_vars[player.source_player_id]
        * round(player.residual_value, 6)
        for player in ordered
    )
    primary_objective = lineup_objective + pair_objective + terminal_objective
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
                for player in captainable
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
            if (gameweek, player_id) in captain_vars:
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
    if pair_vars:
        problem += (
            pulp.lpSum(pair_vars.values()) == 1,
            "one_goalkeeper_pair",
        )
        for (first_id, second_id), variable in pair_vars.items():
            problem += (
                variable <= squad_vars[first_id],
                f"pair_first_{first_id}_{second_id}",
            )
            problem += (
                variable <= squad_vars[second_id],
                f"pair_second_{first_id}_{second_id}",
            )
            # Nominate the goalkeeper the pair was priced under, inside the
            # solve. Exactly one goalkeeper starts each Gameweek, so this
            # pins the starter variable to the orientation rather than
            # leaving it free and correcting it afterwards — which would
            # make every constraint written against the starting XI, such as
            # frontier exclusions, read a lineup nobody selected.
            for orientation in pair_orientations[(first_id, second_id)]:
                problem += (
                    starter_vars[
                        (orientation.gameweek_number, orientation.starter_id)
                    ]
                    >= variable,
                    f"pair_nominates_{first_id}_{second_id}_"
                    f"{orientation.gameweek_number}",
                )
    missing = sorted(required_player_ids - set(squad_vars))
    if missing:
        raise OptimisationError(
            "Cannot force players who are not eligible candidates: "
            + ", ".join(missing)
        )
    overlap = sorted(required_player_ids & forbidden_player_ids)
    if overlap:
        raise OptimisationError(
            "Cannot both require and forbid: " + ", ".join(overlap)
        )
    for player_id in sorted(required_player_ids):
        problem += (squad_vars[player_id] == 1, f"required_{player_id}")
    for player_id in sorted(forbidden_player_ids & set(squad_vars)):
        problem += (squad_vars[player_id] == 0, f"forbidden_{player_id}")

    # Structural generation constraints. These shape which squads are
    # produced and never how one is valued: a squad born under a "no defender
    # above 5.5m" constraint is scored by exactly the same exact objective as
    # every other candidate, and competes on that number alone.
    for index, group in enumerate(group_constraints):
        known = sorted(group.player_ids & set(squad_vars))
        total = pulp.lpSum(squad_vars[player_id] for player_id in known)
        if group.minimum is not None:
            if group.minimum > len(known):
                raise OptimisationError(
                    f"Group {group.name!r} needs {group.minimum} players but "
                    f"only {len(known)} are eligible"
                )
            problem += (total >= group.minimum, f"group_min_{index}")
        if group.maximum is not None:
            problem += (total <= group.maximum, f"group_max_{index}")
    for index, spend in enumerate(spend_constraints):
        known = sorted(spend.player_ids & set(squad_vars))
        by_id = {player.source_player_id: player for player in ordered}
        total = pulp.lpSum(
            squad_vars[player_id] * by_id[player_id].price_tenths
            for player_id in known
        )
        if spend.minimum_tenths is not None:
            problem += (
                total >= spend.minimum_tenths, f"spend_min_{index}"
            )
        if spend.maximum_tenths is not None:
            problem += (
                total <= spend.maximum_tenths, f"spend_max_{index}"
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

    bench_objective = pulp.lpSum(
        (
            squad_vars[player.source_player_id]
            - starter_vars[(gameweek, player.source_player_id)]
        )
        * round(values[(gameweek, player.source_player_id)].expected_points, 6)
        for gameweek in gameweeks
        for player in scored_starters
    ) + pulp.lpSum(
        variable * round(pair_reserve_values[key], 6)
        for key, variable in pair_vars.items()
    )
    return SquadModel(
        problem=problem,
        ordered=ordered,
        gameweeks=gameweeks,
        values=values,
        squad_vars=squad_vars,
        starter_vars=starter_vars,
        captain_vars=captain_vars,
        pair_vars=pair_vars,
        pair_values=pair_values,
        pair_reserve_values=pair_reserve_values,
        pair_orientations=pair_orientations,
        scored_starters=scored_starters,
        primary_objective=primary_objective,
        lineup_objective=lineup_objective,
        pair_objective=pair_objective,
        terminal_objective=terminal_objective,
        bench_objective=bench_objective,
    )


def enumerate_squad_ids(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    count: int,
    objective: str = "primary",
    linear_slack: float | None = None,
    exclude_starting_xis: bool = False,
    seed_excluded_squads: tuple[frozenset[str], ...] = (),
    perturbation: dict[str, float] | None = None,
    required_player_ids: frozenset[str] = frozenset(),
    forbidden_player_ids: frozenset[str] = frozenset(),
    group_constraints: tuple[SquadGroupConstraint, ...] = (),
    spend_constraints: tuple[SquadSpendConstraint, ...] = (),
    time_limit_seconds: float | None = DEFAULT_GENERATION_TIME_LIMIT_SECONDS,
) -> tuple[frozenset[str], ...]:
    """Produce many distinct legal squads from one model, cheaply.

    ``optimise_full_squad`` runs three solves and an exact valuation for every
    squad it returns, because it is answering "what is the best squad and what
    is it worth". A candidate *search* wants neither: it wants breadth, and the
    exact valuation happens once per unique squad afterwards. So this builds
    the model once, solves it repeatedly with accumulating exclusions, and
    returns bare squad memberships. One solve per candidate rather than three.

    Only memberships. Every number attached to a candidate — its linear
    objective as much as its exact value — comes from rescoring the fifteen
    afterwards, so a squad reached under a slack band, a forced inclusion or a
    perturbation carries numbers computed exactly the way an unconstrained
    squad's are. A generator that reported its own objective would report the
    band edge it was pinned against rather than the squad's own value.

    Two knobs make the search find things exclusion alone cannot.

    ``linear_slack`` pins the primary objective at ``optimum - slack`` instead
    of at the optimum. This matters more than anything else here. Bench players
    appear nowhere in the primary objective, so every squad sharing a weekly XI
    is *exactly* tied on it, and excluding complete squads walks that tie set —
    thousands of interchangeable cheap reserves — without ever reaching a squad
    that trades a little XI quality for a much better bench. Only a slack band
    can cross that gap.

    ``objective="reserve"`` then maximises reserve quality inside the band,
    which is the linear surrogate for the autosub value the primary objective
    omits. Together they aim the search directly at the structures the
    incumbent frontier was blind to.

    ``perturbation`` adds tiny declared per-player coefficients to the
    generation objective to shake out otherwise-identical tied structures. It
    is removed before anything is scored: the returned linear objective and
    every exact value are computed without it, so it can never decide a
    ranking.

    ``time_limit_seconds`` caps each individual generation solve. When CBC hits
    the cap with a feasible incumbent, that incumbent is accepted as a proposed
    candidate rather than discarded — safe precisely because generation never
    scores anything: the fifteen it returns is exact-rescored afterwards like
    every other. The cap exists because the linear objective's degeneracy can
    otherwise send CBC into an arbitrarily long optimality proof over a tie set
    of interchangeable reserves. ``None`` disables it and restores uncapped,
    proven-optimal generation.
    """

    try:
        import pulp
    except ImportError as error:
        raise OptimisationError(
            "Exact optimisation requires the 'optimize' project dependency"
        ) from error
    if count < 1:
        raise ValueError("Candidate count must be at least one")
    if objective not in {"primary", "reserve"}:
        raise ValueError("Objective must be 'primary' or 'reserve'")
    if linear_slack is not None and linear_slack < 0:
        raise ValueError("Linear slack cannot be negative")

    model = build_squad_model(
        candidates,
        budget_tenths=budget_tenths,
        rules=rules,
        excluded_squads=seed_excluded_squads,
        required_player_ids=required_player_ids,
        forbidden_player_ids=forbidden_player_ids,
        group_constraints=group_constraints,
        spend_constraints=spend_constraints,
    )
    problem = model.problem
    solver = (
        pulp.PULP_CBC_CMD(msg=False)
        if time_limit_seconds is None
        else pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_seconds)
    )

    def solve(name: str) -> bool:
        problem.solve(solver)
        # ``sol_status`` describes the solution regardless of why the solver
        # stopped: ``Optimal`` is a proven optimum, ``IntegerFeasible`` is a
        # valid legal squad found before the time cap expired. Both are usable
        # candidates here — generation only proposes, and every proposal is
        # exact-rescored before it is ranked. ``NoSolutionFound`` covers both a
        # genuinely infeasible model and a cap that expired before any incumbent
        # was reached; either way there is nothing to propose, so stop.
        if problem.sol_status in {
            pulp.LpSolutionOptimal,
            pulp.LpSolutionIntegerFeasible,
        }:
            return True
        if problem.sol_status in {
            pulp.LpSolutionNoSolutionFound,
            pulp.LpSolutionInfeasible,
        }:
            return False
        raise OptimisationError(
            f"{name} did not resolve: {pulp.LpStatus[problem.status]}"
        )

    if linear_slack is not None:
        problem.setObjective(model.primary_objective)
        if not solve("Slack-band reference solve"):
            return ()
        optimum = float(pulp.value(model.primary_objective))
        problem += (
            model.primary_objective >= optimum - linear_slack - 1e-6,
            "linear_slack_band",
        )

    search_objective = (
        model.bench_objective if objective == "reserve" else model.primary_objective
    )
    if perturbation:
        search_objective = search_objective + pulp.lpSum(
            model.squad_vars[player_id] * round(weight, 9)
            for player_id, weight in sorted(perturbation.items())
            if player_id in model.squad_vars
        )
    problem.setObjective(search_objective)

    found: list[frozenset[str]] = []
    for index in range(count):
        if not solve(f"Candidate enumeration {index}"):
            break
        selected = model.selected_ids()
        found.append(selected)
        problem += (
            pulp.lpSum(model.squad_vars[player_id] for player_id in sorted(selected))
            <= rules.squad.squad_size - 1,
            f"enumerated_squad_{index}",
        )
        if exclude_starting_xis:
            starting = model.starting_ids(model.current_gameweek)
            problem += (
                pulp.lpSum(
                    model.starter_vars[(model.current_gameweek, player_id)]
                    for player_id in sorted(starting)
                )
                <= rules.squad.starting_size - 1,
                f"enumerated_xi_{index}",
            )
    return tuple(found)


def optimise_full_squad(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    excluded_squads: tuple[frozenset[str], ...] = (),
    excluded_starting_xis: tuple[frozenset[str], ...] = (),
    required_player_ids: frozenset[str] = frozenset(),
    forbidden_player_ids: frozenset[str] = frozenset(),
    group_constraints: tuple[SquadGroupConstraint, ...] = (),
    spend_constraints: tuple[SquadSpendConstraint, ...] = (),
    goalkeeper_pair_valuation: bool = True,
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

    Goalkeepers are handled as a pair rather than as two independent players.
    A single starter variable per Gameweek would value the nominated
    goalkeeper alone and leave the substitute contributing nothing to
    selection, even though the substitute automatically replaces the starter
    whenever the starter records no minutes. Instead every eligible pair is
    enumerated, each pair's best weekly orientation and exact value computed,
    and that value carried into the same objective the outfield players are
    selected under. Goalkeepers therefore appear in the objective **once**, in
    the pair term, and are excluded from the ordinary starter, captain and
    bench-quality terms so no goalkeeper's points can be counted twice. Which
    two goalkeepers are owned, and which one starts each Gameweek, both come
    out of the solve; nothing is swapped afterwards.

    ``required_player_ids`` forces named players into the squad, which is what
    a counterfactual ("what if this club's defender had to be owned?") needs:
    the optimiser then rebuilds the rest of the squad around them rather than
    substituting them into a squad chosen without them.
    """

    try:
        import pulp
    except ImportError as error:
        raise OptimisationError(
            "Exact optimisation requires the 'optimize' project dependency"
        ) from error
    model = build_squad_model(
        candidates,
        budget_tenths=budget_tenths,
        rules=rules,
        excluded_squads=excluded_squads,
        excluded_starting_xis=excluded_starting_xis,
        required_player_ids=required_player_ids,
        forbidden_player_ids=forbidden_player_ids,
        group_constraints=group_constraints,
        spend_constraints=spend_constraints,
        goalkeeper_pair_valuation=goalkeeper_pair_valuation,
    )
    problem = model.problem
    ordered = model.ordered
    gameweeks = model.gameweeks
    values = model.values
    squad_vars = model.squad_vars
    starter_vars = model.starter_vars
    pair_vars = model.pair_vars
    pair_values = model.pair_values
    pair_orientations = model.pair_orientations
    primary_objective = model.primary_objective
    bench_objective = model.bench_objective
    current_gameweek = model.current_gameweek
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

    # Bench players contribute when rotated into a later XI, but a player who
    # remains benched throughout the horizon is absent from the primary
    # objective. Maximise reserve quality over *every* Gameweek while holding
    # the XI/captain/terminal optimum fixed. Exact autosub value is calculated
    # after selection because its joint appearance states are nonlinear.
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
    selected_pair: tuple[str, str] | None = None
    orientation_by_gameweek: dict[int, GoalkeeperOrientation] = {}
    if pair_vars:
        selected_ids = {player.source_player_id for player in selected}
        selected_pair = next(
            key for key in pair_vars if selected_ids.issuperset(key)
        )
        orientation_by_gameweek = {
            orientation.gameweek_number: orientation
            for orientation in pair_orientations[selected_pair]
        }
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
        if orientation_by_gameweek:
            # The solve valued the pair, not a nominated goalkeeper, so the
            # solver's goalkeeper starter variable carries no information.
            # Nominate the orientation the pair value was computed under, so
            # the reported lineup is the one that was actually priced.
            nominated = orientation_by_gameweek[gameweek].starter_id
            gameweek_starters = tuple(
                sorted(
                    (
                        player
                        for player in gameweek_starters
                        if player.position != Position.GK
                    ),
                    key=lambda player: player.source_player_id,
                )
            ) + tuple(
                player
                for player in selected
                if player.source_player_id == nominated
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
    lineup_expected = sum(
        sum(
            values[(plan.gameweek_number, player_id)].expected_points
            for player_id in plan.starting_player_ids
        )
        + values[(plan.gameweek_number, plan.captain_id)].expected_points
        for plan in gameweek_plans
    )
    horizon_expected = 0.0
    horizon_bench_contribution = 0.0
    for plan in gameweek_plans:
        gameweek_points, gameweek_appearance = _gameweek_value_maps(
            selected,
            plan.gameweek_number,
        )
        exact, exact_bench, _ = _expected_weekly_score(
            selected,
            plan.starting_player_ids,
            plan.bench_player_ids,
            plan.captain_id,
            plan.vice_captain_id,
            rules,
            points=gameweek_points,
            appearance=gameweek_appearance,
        )
        horizon_expected += exact
        horizon_bench_contribution += exact_bench
    terminal_value = sum(player.residual_value for player in selected)
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
        horizon_expected_points=round(horizon_expected, 3),
        gameweek_expected_points=round(expected, 3),
        expected_bench_contribution=round(bench_contribution, 3),
        expected_captain_contribution=round(captain_contribution, 3),
        gameweek_plans=gameweek_plans,
        solver_status=status,
        lineup_expected_points=round(lineup_expected, 3),
        horizon_expected_bench_contribution=round(
            horizon_bench_contribution,
            3,
        ),
        terminal_value=round(terminal_value, 3),
        decision_value=round(horizon_expected + terminal_value, 3),
        goalkeeper_pair=tuple(selected_pair or ()),
        goalkeeper_pair_value=round(
            pair_values.get(selected_pair, 0.0) if selected_pair else 0.0, 3
        ),
        goalkeeper_orientations=(
            pair_orientations[selected_pair] if selected_pair else ()
        ),
        solver_objective=round(primary_optimum, 3),
        proof=(
            "CBC returned Optimal for the projected multi-Gameweek legal-XI "
            "and captain objective plus any declared terminal values, then "
            "again with that objective pinned while reserve quality across "
            "the full horizon was maximised, and a third "
            "time for a deterministic tie-break; "
            "each Gameweek's bench order and captain/vice pair were then "
            "chosen by exact enumeration. The reported horizon value exactly "
            "integrates independent player appearance outcomes with legal "
            "autosubs and captain fallback for the selected squad. The squad "
            "is proven optimal for the linear objective; exact autosub "
            "optimality is established only among the opening-squad candidate "
            "frontier that is explicitly compared. Goalkeepers entered that "
            "objective once, as an exactly valued pair with its best weekly "
            "orientation, and are absent from the starter, captain and "
            "bench-quality terms."
        ),
    )


def squad_ranking_key(
    result: FullSquadResult,
) -> tuple[float, float, float, int, tuple[str, ...]]:
    """Rank squads by exact decision value, cheapest first among true ties.

    Money left in the bank is worth something a projection cannot price — a
    later transfer, a price rise absorbed — so it is never converted into
    points here. It only breaks ties: two squads the model cannot separate on
    expected points are separated by cost, and only then by identifier so a
    rerun agrees with itself.
    """

    return (
        -result.decision_value,
        -result.horizon_expected_points,
        -result.lineup_expected_points,
        result.total_cost_tenths,
        tuple(sorted(player.source_player_id for player in result.players)),
    )


def optimise_opening_squads(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    rules: SeasonRules,
    alternative_count: int = 2,
    candidate_pool_size: int | None = None,
    require_distinct_starting_xi: bool = True,
    required_player_ids: frozenset[str] = frozenset(),
    goalkeeper_pair_valuation: bool = True,
) -> OpeningSquadRecommendation:
    """Return a robust opening squad and genuinely distinct alternatives.

    Excluding only the exact fifteen produced alternatives that reused the
    same XI and swapped a bench filler, which is not a decision the manager
    can act on, so by default each alternative must differ in its starting XI
    as well.

    A broad frontier wants the opposite: forty *complete squads* that are each
    distinct as squads, whether or not two of them happen to field the same
    opening eleven. Two squads with the same XI and different benches make
    genuinely different autosub and rotation propositions, and excluding one
    of them shrinks the frontier for no reason. Pass
    ``require_distinct_starting_xi=False`` for that.
    """

    if alternative_count < 0:
        raise ValueError("Alternative count cannot be negative")
    pool_size = (
        alternative_count + 1
        if candidate_pool_size is None
        else candidate_pool_size
    )
    if pool_size < alternative_count + 1:
        raise ValueError(
            "Candidate pool must contain the primary squad and every requested alternative"
        )
    results: list[FullSquadResult] = []
    excluded_squads: list[frozenset[str]] = []
    excluded_starting_xis: list[frozenset[str]] = []
    for _ in range(pool_size):
        try:
            result = optimise_full_squad(
                candidates,
                budget_tenths=budget_tenths,
                rules=rules,
                excluded_squads=tuple(excluded_squads),
                excluded_starting_xis=tuple(excluded_starting_xis),
                required_player_ids=required_player_ids,
                goalkeeper_pair_valuation=goalkeeper_pair_valuation,
            )
        except OptimisationError:
            # The frontier is exhausted: every remaining legal squad has
            # already been produced. Fewer candidates than asked for is a
            # fact about the candidate set, not a failure.
            if not results:
                raise
            break
        results.append(result)
        excluded_squads.append(
            frozenset(player.source_player_id for player in result.players)
        )
        if require_distinct_starting_xi:
            excluded_starting_xis.append(result.starting_player_ids)
    ranked = sorted(results, key=squad_ranking_key)
    primary = ranked[0]
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
        alternatives=tuple(ranked[1 : alternative_count + 1]),
        objective=(
            "Expected points from each Gameweek's legal-XI, captain fallback "
            "and autosubs across the active horizon, plus declared terminal "
            f"value; best of {len(results)} distinct solver-proven candidates"
        ),
        assumptions=(
            "Appearance states are independent until the joint simulator qualifies",
            "Terminal value is zero unless explicitly supplied on candidates",
            "Candidate-frontier comparison is exact; a global nonlinear autosub optimum "
            "is not claimed",
            "Goalkeepers are valued as a pair: the substitute's automatic "
            "replacement of a starter who records no minutes is priced into "
            "selection, under the same independent-appearance assumption",
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

    The armband is worth `captain + P(captain absent) x vice`. Evaluate every
    ordered starter pair: a slightly lower raw-points captain can be superior
    when their absence transfers the armband to a strong vice-captain often
    enough. The MILP captain variable is only a linear selection guide; this
    exact independent-appearance value drives the reported lineup plan.
    """

    if len(starters) < 2:
        raise OptimisationError(
            "Captaincy requires at least two starters"
        )
    best_key: tuple[float, str, str] | None = None
    for captain in starters:
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


def chip_values_for_gameweek(
    result: FullSquadResult,
    gameweek_number: int,
    rules: SeasonRules,
) -> dict[Chip, float]:
    """What each scoring chip would add in one Gameweek of a selected squad.

    Evaluated from that Gameweek's own projected values, so a double Gameweek
    — where a player carries two fixtures and roughly twice the expected
    points — raises the chip's value exactly as it should.

    Bench Boost is worth the bench's points *less* the autosub value the squad
    would have collected anyway, otherwise a bench that was going to come on
    would be counted twice. Triple Captain is worth one further copy of the
    effective captain, including vice-captain fallback.
    """

    plan = next(
        (
            value
            for value in result.gameweek_plans
            if value.gameweek_number == gameweek_number
        ),
        None,
    )
    if plan is None:
        raise ValueError(
            f"The squad has no lineup plan for Gameweek {gameweek_number}"
        )
    points, appearance = _gameweek_value_maps(result.players, gameweek_number)
    bench = plan.bench_player_ids or result.bench_player_ids
    _, bench_contribution, captain_contribution = _expected_weekly_score(
        result.players,
        plan.starting_player_ids,
        bench,
        plan.captain_id,
        plan.vice_captain_id,
        rules,
        points=points,
        appearance=appearance,
    )
    bench_total = sum(points[player_id] for player_id in bench)
    return {
        Chip.BENCH_BOOST: max(0.0, bench_total - bench_contribution),
        Chip.TRIPLE_CAPTAIN: max(0.0, captain_contribution),
    }


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
