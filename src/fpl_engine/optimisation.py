"""Exact discrete optimisation for FPL selection decisions."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .config import SeasonRules
from .domain import Player, Position, Squad
from .rules import resolve_automatic_substitutions, validate_squad


class OptimisationError(RuntimeError):
    """Raised when no proven optimal solution can be produced."""


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
    # Expected points dominate by six orders of magnitude. The smaller terms
    # make equally projected solutions prefer lower cost, then stable ID order.
    problem += pulp.lpSum(
        selected[player.source_player_id]
        * (
            round(player.expected_points, 6) * 1_000_000
            - player.price_tenths * 0.001
            - index * 0.000001
        )
        for index, player in enumerate(ordered)
    )
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

    status_code = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[status_code]
    if status != "Optimal":
        raise OptimisationError(
            f"Starting-XI optimisation did not prove an optimum: {status}"
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
    starter_vars = {
        player.source_player_id: pulp.LpVariable(
            f"starter_{index}", cat=pulp.LpBinary
        )
        for index, player in enumerate(ordered)
    }
    captain_vars = {
        player.source_player_id: pulp.LpVariable(
            f"captain_{index}", cat=pulp.LpBinary
        )
        for index, player in enumerate(ordered)
    }
    vice_vars = {
        player.source_player_id: pulp.LpVariable(
            f"vice_{index}", cat=pulp.LpBinary
        )
        for index, player in enumerate(ordered)
    }
    problem += pulp.lpSum(
        squad_vars[player.source_player_id]
        * _robust_horizon_value(player)
        * 0.15
        + starter_vars[player.source_player_id]
        * _gameweek_points(player)
        * 0.85
        + captain_vars[player.source_player_id] * _gameweek_points(player)
        + vice_vars[player.source_player_id] * _gameweek_points(player) * 0.05
        for player in ordered
    )
    problem += (
        pulp.lpSum(squad_vars.values()) == rules.squad.squad_size,
        "squad_size",
    )
    problem += (
        pulp.lpSum(starter_vars.values()) == rules.squad.starting_size,
        "starting_size",
    )
    problem += (
        pulp.lpSum(
            squad_vars[player.source_player_id] * player.price_tenths
            for player in ordered
        )
        <= budget_tenths,
        "budget",
    )
    problem += (pulp.lpSum(captain_vars.values()) == 1, "one_captain")
    problem += (pulp.lpSum(vice_vars.values()) == 1, "one_vice")
    for player in ordered:
        player_id = player.source_player_id
        problem += (
            starter_vars[player_id] <= squad_vars[player_id],
            f"starter_in_squad_{player_id}",
        )
        problem += (
            captain_vars[player_id] <= starter_vars[player_id],
            f"captain_starts_{player_id}",
        )
        problem += (
            vice_vars[player_id] <= starter_vars[player_id],
            f"vice_starts_{player_id}",
        )
        problem += (
            captain_vars[player_id] + vice_vars[player_id] <= 1,
            f"roles_distinct_{player_id}",
        )
    for position in Position:
        squad_position_total = pulp.lpSum(
            squad_vars[player.source_player_id]
            for player in ordered
            if player.position == position
        )
        starter_position_total = pulp.lpSum(
            starter_vars[player.source_player_id]
            for player in ordered
            if player.position == position
        )
        problem += (
            squad_position_total == rules.squad.position_counts[position.value],
            f"squad_{position.value}",
        )
        problem += (
            starter_position_total >= rules.squad.formation_min[position.value],
            f"starter_{position.value}_minimum",
        )
        problem += (
            starter_position_total <= rules.squad.formation_max[position.value],
            f"starter_{position.value}_maximum",
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
    starter_ids = frozenset(
        player.source_player_id
        for player in selected
        if (starter_vars[player.source_player_id].value() or 0) > 0.5
    )
    captain_id = next(
        player.source_player_id
        for player in selected
        if (captain_vars[player.source_player_id].value() or 0) > 0.5
    )
    vice_id = next(
        player.source_player_id
        for player in selected
        if (vice_vars[player.source_player_id].value() or 0) > 0.5
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
    errors = validate_squad(squad, rules)
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
            sum(player.expected_points for player in selected), 3
        ),
        gameweek_expected_points=round(expected, 3),
        expected_bench_contribution=round(bench_contribution, 3),
        expected_captain_contribution=round(captain_contribution, 3),
        solver_status=status,
        proof=(
            "CBC returned Optimal for the full-squad surrogate objective; "
            "the reported weekly value then enumerates all 2^15 player "
            "appearance outcomes with legal autosubs."
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
            "Eight-Gameweek projected value with uncertainty penalty, "
            "residual value, Gameweek 1 lineup, captaincy and bench resilience"
        ),
        assumptions=(
            "Later Gameweeks inherit the projection model's increasing uncertainty",
            "Uncertainty is penalised at 0.20 points per unit",
            "Residual value contributes 0.10 points per unit",
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
            -_gameweek_points(player) * player.appearance_probability,
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
    numeric_ids = {
        player.source_player_id: index
        for index, player in enumerate(selected, start=1)
    }
    source_ids = {numeric_id: source_id for source_id, numeric_id in numeric_ids.items()}
    domain_squad = _domain_squad(
        selected, starter_ids, bench, captain_id, vice_id
    )
    player_ids = sorted(by_id)
    expected_base = 0.0
    expected_captain = 0.0
    without_bench = sum(
        _gameweek_points(by_id[player_id]) for player_id in starter_ids
    )
    for outcomes in product((False, True), repeat=len(player_ids)):
        appeared = dict(zip(player_ids, outcomes, strict=True))
        probability = 1.0
        for player_id, did_appear in appeared.items():
            appearance_probability = by_id[player_id].appearance_probability
            probability *= (
                appearance_probability
                if did_appear
                else 1.0 - appearance_probability
            )
        if probability == 0:
            continue
        minutes = {
            numeric_ids[player.source_player_id]: (
                90 if appeared[player.source_player_id] else 0
            )
            for player in selected
        }
        resolved = resolve_automatic_substitutions(
            domain_squad, minutes, rules
        )
        base_score = sum(
            _conditional_points(by_id[source_ids[player_id]])
            for player_id in resolved.scoring_player_ids
        )
        expected_base += probability * base_score
        effective_captain = None
        captain_numeric = numeric_ids[captain_id]
        vice_numeric = numeric_ids[vice_id]
        if (
            captain_numeric in resolved.scoring_player_ids
            and minutes[captain_numeric]
        ):
            effective_captain = captain_id
        elif (
            vice_numeric in resolved.scoring_player_ids
            and minutes[vice_numeric]
        ):
            effective_captain = vice_id
        if effective_captain is not None:
            expected_captain += probability * _conditional_points(
                by_id[effective_captain]
            )
    return (
        expected_base + expected_captain,
        max(0.0, expected_base - without_bench),
        expected_captain,
    )


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


def _robust_horizon_value(player: CandidatePlayer) -> float:
    return (
        player.expected_points
        - 0.20 * player.uncertainty
        + 0.10 * player.residual_value
    )


def _conditional_points(player: CandidatePlayer) -> float:
    if player.appearance_probability <= 0:
        return 0.0
    return _gameweek_points(player) / player.appearance_probability
