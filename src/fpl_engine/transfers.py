"""Exact transfer-route comparison from a current legal squad."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from .config import SeasonRules
from .domain import Position
from .history.database import HistoricalDatabase
from .optimisation import (
    CandidatePlayer,
    FullSquadResult,
    OptimisationError,
    _optimisation_gameweeks,
    _value_for_gameweek,
    optimise_full_squad,
)
from .rules import calculate_transfer_cost, next_free_transfer_count


@dataclass(frozen=True)
class CurrentSquad:
    player_ids: frozenset[str]
    selling_prices_tenths: dict[str, int]
    bank_tenths: int
    free_transfers: int
    #: Chips not yet spent. Carried so a decision's state is complete and both
    #: branches of a regret comparison are equally constrained; no route in
    #: this module plays one.
    available_chips: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransferRoute:
    transfers_out: tuple[CandidatePlayer, ...]
    transfers_in: tuple[CandidatePlayer, ...]
    resulting_squad: FullSquadResult
    transfer_count: int
    points_hit: int
    horizon_points_gain: float
    bank_tenths: int
    next_free_transfers: int
    flexibility_value: float
    route_score: float
    explanation: str


@dataclass(frozen=True)
class TransferRecommendation:
    primary: TransferRoute
    routes: tuple[TransferRoute, ...]
    baseline_horizon_points: float
    search_scope: str


def recommend_transfers(
    candidates: tuple[CandidatePlayer, ...],
    current: CurrentSquad,
    *,
    rules: SeasonRules,
    max_transfers: int | None = None,
    future_transfer_needs: Mapping[int, float] | None = None,
    candidate_pool_size: int = 1,
) -> TransferRecommendation:
    """Compare rolling and exact-rescored routes for each transfer count."""

    try:
        import pulp
    except ImportError as error:
        raise OptimisationError(
            "Exact optimisation requires the 'optimize' project dependency"
        ) from error
    by_id = {player.source_player_id: player for player in candidates}
    missing = current.player_ids - set(by_id)
    if missing:
        raise ValueError(f"Current squad players are missing: {sorted(missing)}")
    if set(current.selling_prices_tenths) != current.player_ids:
        raise ValueError("Selling prices must be supplied for every current player")
    search_limit = (
        rules.transfers.maximum_free_transfers if max_transfers is None else max_transfers
    )
    if not 0 <= search_limit <= rules.squad.squad_size:
        raise ValueError("Maximum transfers is outside the legal squad range")
    if candidate_pool_size < 1:
        raise ValueError("Candidate pool size must be positive")
    max_transfers = search_limit
    current_candidates = tuple(by_id[player_id] for player_id in current.player_ids)
    baseline = optimise_full_squad(
        current_candidates,
        budget_tenths=sum(player.price_tenths for player in current_candidates),
        rules=rules,
    )
    routes = [
        _route(
            baseline.players,
            baseline,
            current,
            rules,
            transfer_count=0,
            future_transfer_needs=future_transfer_needs,
        )
    ]
    for transfer_count in range(1, search_limit + 1):
        excluded_squads: list[frozenset[str]] = []
        for _ in range(candidate_pool_size):
            selected_ids = _best_transfer_squad(
                candidates,
                current,
                transfer_count,
                rules,
                pulp,
                excluded_squads=tuple(excluded_squads),
            )
            if selected_ids is None:
                break
            excluded_squads.append(selected_ids)
            selected = tuple(by_id[player_id] for player_id in selected_ids)
            incoming = selected_ids - current.player_ids
            outgoing = current.player_ids - selected_ids
            money_available = current.bank_tenths + sum(
                current.selling_prices_tenths[player_id]
                for player_id in outgoing
            )
            resulting = optimise_full_squad(
                selected,
                budget_tenths=sum(player.price_tenths for player in selected),
                rules=rules,
            )
            routes.append(
                _route(
                    selected,
                    resulting,
                    current,
                    rules,
                    transfer_count=transfer_count,
                    transfers_out=tuple(
                        by_id[player_id] for player_id in outgoing
                    ),
                    transfers_in=tuple(
                        by_id[player_id] for player_id in incoming
                    ),
                    bank_tenths=money_available
                    - sum(
                        by_id[player_id].price_tenths
                        for player_id in incoming
                    ),
                    baseline=baseline,
                    future_transfer_needs=future_transfer_needs,
                )
            )
    ordered = tuple(
        sorted(
            routes,
            key=lambda route: (
                -route.route_score,
                route.transfer_count,
                tuple(player.source_player_id for player in route.transfers_in),
            ),
        )
    )
    return TransferRecommendation(
        primary=ordered[0],
        routes=ordered,
        baseline_horizon_points=baseline.decision_value,
        search_scope=(
            f"Roll and up to {candidate_pool_size} distinct solver-optimal legal "
            f"routes for each of 1–{max_transfers} transfers, all exactly rescored "
            "for autosubs and captain fallback; "
            + (
                "free-transfer option value uses the supplied empirical future-need distribution"
                if future_transfer_needs is not None
                else "free-transfer option value is omitted because no "
                "empirical future-need distribution was supplied"
            )
        ),
    )


def _best_transfer_squad(
    candidates: tuple[CandidatePlayer, ...],
    current: CurrentSquad,
    transfer_count: int,
    rules: SeasonRules,
    pulp: object,
    *,
    excluded_squads: tuple[frozenset[str], ...] = (),
) -> frozenset[str] | None:
    ordered = tuple(sorted(candidates, key=lambda player: player.source_player_id))
    problem = pulp.LpProblem(f"transfer_route_{transfer_count}", pulp.LpMaximize)
    selected = {
        player.source_player_id: pulp.LpVariable(f"selected_{index}", cat=pulp.LpBinary)
        for index, player in enumerate(ordered)
    }
    gameweeks = _optimisation_gameweeks(ordered)
    starters = {
        (gameweek, player.source_player_id): pulp.LpVariable(
            f"starter_{gameweek}_{index}", cat=pulp.LpBinary
        )
        for gameweek in gameweeks
        for index, player in enumerate(ordered)
    }
    captains = {
        (gameweek, player.source_player_id): pulp.LpVariable(
            f"captain_{gameweek}_{index}", cat=pulp.LpBinary
        )
        for gameweek in gameweeks
        for index, player in enumerate(ordered)
    }
    problem += pulp.lpSum(
        (
            starters[(gameweek, player.source_player_id)]
            + captains[(gameweek, player.source_player_id)]
        )
        * _value_for_gameweek(player, gameweek).expected_points
        for gameweek in gameweeks
        for player in ordered
    ) + pulp.lpSum(
        selected[player.source_player_id] * player.residual_value
        for player in ordered
    )
    problem += pulp.lpSum(selected.values()) == rules.squad.squad_size
    problem += (
        pulp.lpSum(selected[player_id] for player_id in current.player_ids)
        == rules.squad.squad_size - transfer_count
    )
    incoming_cost = pulp.lpSum(
        selected[player.source_player_id] * player.price_tenths
        for player in ordered
        if player.source_player_id not in current.player_ids
    )
    outgoing_value = pulp.lpSum(
        (1 - selected[player_id]) * current.selling_prices_tenths[player_id]
        for player_id in current.player_ids
    )
    problem += incoming_cost <= current.bank_tenths + outgoing_value
    for position in Position:
        problem += (
            pulp.lpSum(
                selected[player.source_player_id]
                for player in ordered
                if player.position == position
            )
            == rules.squad.position_counts[position.value]
        )
        for gameweek in gameweeks:
            position_starters = pulp.lpSum(
                starters[(gameweek, player.source_player_id)]
                for player in ordered
                if player.position == position
            )
            problem += position_starters >= rules.squad.formation_min[position.value]
            problem += position_starters <= rules.squad.formation_max[position.value]
    for gameweek in gameweeks:
        problem += (
            pulp.lpSum(starters[(gameweek, player.source_player_id)] for player in ordered)
            == rules.squad.starting_size
        )
        problem += (
            pulp.lpSum(captains[(gameweek, player.source_player_id)] for player in ordered) == 1
        )
        for player in ordered:
            player_id = player.source_player_id
            problem += starters[(gameweek, player_id)] <= selected[player_id]
            problem += captains[(gameweek, player_id)] <= starters[(gameweek, player_id)]
    for team_id in sorted({player.team_id for player in ordered}):
        problem += (
            pulp.lpSum(
                selected[player.source_player_id] for player in ordered if player.team_id == team_id
            )
            <= rules.squad.max_players_per_team
        )
    candidate_ids = set(selected)
    for index, excluded in enumerate(excluded_squads):
        excluded_here = excluded & candidate_ids
        if len(excluded_here) == rules.squad.squad_size:
            problem += (
                pulp.lpSum(selected[player_id] for player_id in excluded_here)
                <= rules.squad.squad_size - 1,
                f"exclude_squad_{index}",
            )
    status_code = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status_code] == "Infeasible":
        return None
    if pulp.LpStatus[status_code] != "Optimal":
        raise OptimisationError(
            f"Transfer optimisation did not prove an optimum: {pulp.LpStatus[status_code]}"
        )
    return frozenset(
        player.source_player_id
        for player in ordered
        if (selected[player.source_player_id].value() or 0) > 0.5
    )


def _route(
    selected: tuple[CandidatePlayer, ...],
    resulting: FullSquadResult,
    current: CurrentSquad,
    rules: SeasonRules,
    *,
    transfer_count: int,
    transfers_out: tuple[CandidatePlayer, ...] = (),
    transfers_in: tuple[CandidatePlayer, ...] = (),
    bank_tenths: int | None = None,
    baseline: FullSquadResult | None = None,
    future_transfer_needs: Mapping[int, float] | None = None,
) -> TransferRoute:
    baseline_result = resulting if baseline is None else baseline
    hit = calculate_transfer_cost(transfer_count, current.free_transfers, rules)
    next_free = next_free_transfer_count(current.free_transfers, transfer_count, rules)
    final_bank = current.bank_tenths if bank_tenths is None else bank_tenths
    flexibility = (
        0.0
        if future_transfer_needs is None
        else free_transfer_option_value(
            next_free,
            future_transfer_needs,
            rules,
        )
    )
    gain = resulting.decision_value - baseline_result.decision_value
    score = gain - hit + flexibility
    action = (
        "Roll the transfer"
        if transfer_count == 0
        else (
            f"Sell {', '.join(player.web_name for player in transfers_out)}; "
            f"buy {', '.join(player.web_name for player in transfers_in)}"
        )
    )
    return TransferRoute(
        transfers_out=tuple(sorted(transfers_out, key=lambda player: player.web_name)),
        transfers_in=tuple(sorted(transfers_in, key=lambda player: player.web_name)),
        resulting_squad=resulting,
        transfer_count=transfer_count,
        points_hit=hit,
        horizon_points_gain=round(gain, 3),
        bank_tenths=final_bank,
        next_free_transfers=next_free,
        flexibility_value=round(flexibility, 3),
        route_score=round(score, 3),
        explanation=(
            f"{action}. Horizon gain {gain:.2f}, hit {hit}, "
            f"next-GW free transfers {next_free}, option value "
            f"{flexibility:.2f}; bank £{final_bank / 10:.1f}m."
        ),
    )


def free_transfer_option_value(
    available_free_transfers: int,
    future_transfer_needs: Mapping[int, float],
    rules: SeasonRules,
) -> float:
    """Expected hit cost avoided versus entering next week with one FT."""

    if not 0 <= available_free_transfers <= rules.transfers.maximum_free_transfers:
        raise ValueError("Available free transfers are outside the configured range")
    if not future_transfer_needs:
        raise ValueError("A future transfer-need distribution is required")
    if any(count < 0 or probability < 0 for count, probability in future_transfer_needs.items()):
        raise ValueError("Transfer needs and probabilities cannot be negative")
    total_probability = sum(future_transfer_needs.values())
    if abs(total_probability - 1.0) > 1e-6:
        raise ValueError("Future transfer-need probabilities must sum to one")
    reference = rules.transfers.initial_free_transfers
    avoided = sum(
        probability
        * (
            calculate_transfer_cost(count, reference, rules)
            - calculate_transfer_cost(
                count,
                available_free_transfers,
                rules,
            )
        )
        for count, probability in future_transfer_needs.items()
    )
    return round(avoided, 6)


def empirical_transfer_need_distribution(
    database: HistoricalDatabase,
    season_codes: tuple[str, ...],
    *,
    minimum_samples: int = 8,
) -> dict[int, float]:
    """Estimate next-week transfer counts from recorded prospective actions."""

    if not season_codes:
        raise ValueError("At least one season is required")
    placeholders = ",".join("?" for _ in season_codes)
    rows = database.connection.execute(
        f"""
        SELECT actions.action_json
        FROM actual_actions actions
        JOIN weekly_decision_runs decisions
          ON decisions.id = actions.weekly_decision_run_id
        JOIN seasons ON seasons.id = decisions.season_id
        WHERE seasons.code IN ({placeholders})
        ORDER BY actions.recorded_at
        """,
        season_codes,
    ).fetchall()
    counts: list[int] = []
    for row in rows:
        action = json.loads(row["action_json"])
        transfers = action.get("transfers")
        if isinstance(transfers, list):
            counts.append(len(transfers))
        elif isinstance(action.get("transfer_count"), int):
            counts.append(int(action["transfer_count"]))
    if len(counts) < minimum_samples:
        raise ValueError(
            "Insufficient recorded actual actions to estimate transfer option "
            f"value: {len(counts)} < {minimum_samples}"
        )
    frequencies = {count: counts.count(count) / len(counts) for count in sorted(set(counts))}
    return frequencies
