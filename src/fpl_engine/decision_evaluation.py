"""Continuous transfer replay with hits, free transfers and exact autosubs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import SeasonRules
from .domain import Player, Squad
from .optimisation import CandidatePlayer, FullSquadResult
from .pricing import PurchaseLedger
from .rules import calculate_team_score
from .transfers import CurrentSquad, recommend_transfers


@dataclass(frozen=True)
class RealisedPlayerOutcome:
    source_player_id: str
    points: int
    minutes: int


@dataclass(frozen=True)
class TransferReplayWeek:
    gameweek_number: int
    forecast_candidates: tuple[CandidatePlayer, ...]
    realised_outcomes: tuple[RealisedPlayerOutcome, ...]


@dataclass(frozen=True)
class TransferDecisionState:
    """The state a transfer decision was actually taken from.

    Both the model and the hindsight branch see exactly this, so the only
    difference between them is that hindsight knows the Gameweek's outcomes.
    """

    gameweek_number: int
    player_ids: tuple[str, ...]
    purchase_prices_tenths: dict[str, int]
    selling_prices_tenths: dict[str, int]
    bank_tenths: int
    free_transfers: int
    available_chips: tuple[str, ...]
    max_transfers: int


@dataclass(frozen=True)
class TransferReplayResult:
    gameweek_number: int
    transfers_made: int
    points_hit: int
    free_transfers_after: int
    gross_points: int
    net_points: int
    same_state_hindsight_net_points: int
    regret: int
    transfers_out: tuple[str, ...]
    transfers_in: tuple[str, ...]
    autosub_count: int
    effective_captain_id: str | None
    bank_tenths: int
    squad_selling_value_tenths: int
    state: TransferDecisionState


@dataclass(frozen=True)
class TransferContinuityReport:
    weeks: tuple[TransferReplayResult, ...]
    total_net_points: int
    total_same_state_hindsight_points: int
    total_regret: int
    total_hits: int
    final_free_transfers: int
    final_bank_tenths: int
    final_purchase_prices_tenths: dict[str, int]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["weeks"] = [asdict(week) for week in self.weeks]
        result["limitations"] = list(self.limitations)
        return result


def replay_transfer_continuity(
    weeks: tuple[TransferReplayWeek, ...],
    initial_squad: CurrentSquad,
    *,
    rules: SeasonRules,
    max_transfers_per_week: int = 2,
    initial_ledger: PurchaseLedger | None = None,
) -> TransferContinuityReport:
    """Replay one persistent model-owned squad over consecutive Gameweeks.

    Sale values follow the FPL half-profit rule from a carried purchase-price
    ledger. Supply `initial_ledger` when the opening squad's purchase prices
    differ from its selling prices; without it the opening selling prices are
    taken as the purchase prices, which is correct for a squad bought at the
    first replayed Gameweek.
    """

    if not weeks:
        raise ValueError("At least one replay week is required")
    if tuple(sorted(week.gameweek_number for week in weeks)) != tuple(
        week.gameweek_number for week in weeks
    ):
        raise ValueError("Replay weeks must be chronological")
    if len({week.gameweek_number for week in weeks}) != len(weeks):
        raise ValueError("Replay Gameweeks must be unique")
    current = initial_squad
    ledger = initial_ledger or PurchaseLedger.from_entry_prices(
        initial_squad.selling_prices_tenths
    )
    if ledger.player_ids != current.player_ids:
        raise ValueError(
            "The purchase ledger must cover exactly the opening squad"
        )
    results = []
    for week in weeks:
        forecast_by_id = {player.source_player_id: player for player in week.forecast_candidates}
        missing = current.player_ids - set(forecast_by_id)
        if missing:
            raise ValueError(
                f"GW{week.gameweek_number} is missing current players: {sorted(missing)}"
            )
        realised = {outcome.source_player_id: outcome for outcome in week.realised_outcomes}
        if set(forecast_by_id) - set(realised):
            raise ValueError(f"GW{week.gameweek_number} needs outcomes for every candidate")
        # Sale value is re-derived from this Gameweek's market prices against
        # the prices the squad was bought at, so a rise between Gameweeks adds
        # only half its value to the money available now.
        current = replace(
            current,
            selling_prices_tenths=ledger.selling_prices(
                {
                    player_id: forecast_by_id[player_id].price_tenths
                    for player_id in current.player_ids
                },
                rules,
            ),
        )
        state = TransferDecisionState(
            gameweek_number=week.gameweek_number,
            player_ids=tuple(sorted(current.player_ids)),
            purchase_prices_tenths=dict(
                sorted(ledger.purchase_prices_tenths.items())
            ),
            selling_prices_tenths=dict(
                sorted(current.selling_prices_tenths.items())
            ),
            bank_tenths=current.bank_tenths,
            free_transfers=current.free_transfers,
            available_chips=current.available_chips,
            max_transfers=max_transfers_per_week,
        )
        recommendation = recommend_transfers(
            week.forecast_candidates,
            current,
            rules=rules,
            max_transfers=max_transfers_per_week,
        )
        route = recommendation.primary
        gross, autosubs, captain = score_squad_gameweek(
            route.resulting_squad,
            realised,
            rules,
            week.gameweek_number,
        )

        actual_candidates = tuple(
            replace(
                player,
                expected_points=float(realised[player.source_player_id].points),
                gameweek_expected_points=float(realised[player.source_player_id].points),
                appearance_probability=float(realised[player.source_player_id].minutes > 0),
                gameweek_values=(),
                uncertainty=0.0,
            )
            for player in week.forecast_candidates
        )
        hindsight = recommend_transfers(
            actual_candidates,
            current,
            rules=rules,
            max_transfers=max_transfers_per_week,
        ).primary
        hindsight_gross, _, _ = score_squad_gameweek(
            hindsight.resulting_squad,
            realised,
            rules,
            week.gameweek_number,
        )
        net = gross - route.points_hit
        hindsight_net = hindsight_gross - hindsight.points_hit
        results.append(
            TransferReplayResult(
                gameweek_number=week.gameweek_number,
                transfers_made=route.transfer_count,
                points_hit=route.points_hit,
                free_transfers_after=route.next_free_transfers,
                gross_points=gross,
                net_points=net,
                same_state_hindsight_net_points=hindsight_net,
                regret=max(0, hindsight_net - net),
                transfers_out=tuple(player.source_player_id for player in route.transfers_out),
                transfers_in=tuple(player.source_player_id for player in route.transfers_in),
                autosub_count=autosubs,
                effective_captain_id=captain,
                bank_tenths=route.bank_tenths,
                squad_selling_value_tenths=sum(
                    current.selling_prices_tenths.values()
                ),
                state=state,
            )
        )
        selected_ids = frozenset(
            player.source_player_id for player in route.resulting_squad.players
        )
        # Arrivals are bought at this Gameweek's price; everyone kept holds the
        # price they were originally bought at, so a later sale returns only
        # half of any profit.
        ledger = ledger.after_transfers(
            sold=current.player_ids - selected_ids,
            bought={
                player_id: forecast_by_id[player_id].price_tenths
                for player_id in selected_ids - current.player_ids
            },
        )
        current = CurrentSquad(
            player_ids=selected_ids,
            # Provisional: the next iteration re-derives these against that
            # Gameweek's prices before any decision is taken.
            selling_prices_tenths=ledger.selling_prices(
                {
                    player_id: forecast_by_id[player_id].price_tenths
                    for player_id in selected_ids
                },
                rules,
            ),
            bank_tenths=route.bank_tenths,
            free_transfers=route.next_free_transfers,
            # No route plays a chip, so availability is carried unchanged and
            # both branches remain equally constrained.
            available_chips=current.available_chips,
        )
    return TransferContinuityReport(
        weeks=tuple(results),
        total_net_points=sum(result.net_points for result in results),
        total_same_state_hindsight_points=sum(
            result.same_state_hindsight_net_points for result in results
        ),
        total_regret=sum(result.regret for result in results),
        total_hits=sum(result.points_hit for result in results),
        final_free_transfers=current.free_transfers,
        final_bank_tenths=current.bank_tenths,
        final_purchase_prices_tenths=dict(
            sorted(ledger.purchase_prices_tenths.items())
        ),
        limitations=(
            "The squad, bank and free-transfer state persist between Gameweeks.",
            "Realised scoring applies the forecast lineup, bench order, exact "
            "autosubs and captain fallback.",
            "The comparator is a same-state one-Gameweek hindsight action, "
            "not a globally clairvoyant season policy.",
            "The model chooses over its full forecast horizon while hindsight "
            "optimises the scored Gameweek alone, so a horizon-aware model "
            "shows positive regret by construction; only the change between "
            "matched runs at the same horizon is comparable.",
            "No chip is played by either branch, so chip availability is "
            "carried but never spent.",
            "Selling prices apply the FPL half-profit rule to a carried "
            "purchase-price ledger, so a price rise adds only half its value "
            "to spending power.",
        ),
    )


@dataclass(frozen=True)
class ResolvedGameweek:
    """One Gameweek of a selected squad, resolved exactly as FPL would score it."""

    total_points: int
    scoring_player_ids: frozenset[str]
    effective_captain_id: str | None
    substitution_count: int


def score_squad_gameweek(
    result: FullSquadResult,
    outcomes: dict[str, RealisedPlayerOutcome],
    rules: SeasonRules,
    gameweek: int,
) -> tuple[int, int, str | None]:
    """Points, substitution count and the captain who actually counted."""

    resolved = resolve_squad_gameweek(result, outcomes, rules, gameweek)
    return (
        resolved.total_points,
        resolved.substitution_count,
        resolved.effective_captain_id,
    )


def resolve_squad_gameweek(
    result: FullSquadResult,
    outcomes: dict[str, RealisedPlayerOutcome],
    rules: SeasonRules,
    gameweek: int,
) -> ResolvedGameweek:
    """Apply the Gameweek's lineup plan, bench-order autosubs and captain fallback.

    Returns the scoring lineup as well as the score, because the set of players
    who actually counted is what a captaincy decision could have chosen from.
    """

    id_lookup = {
        player.source_player_id: index
        for index, player in enumerate(
            sorted(result.players, key=lambda value: value.source_player_id),
            start=1,
        )
    }
    players = tuple(
        Player(
            player_id=id_lookup[player.source_player_id],
            name=player.web_name,
            team_id=_numeric_team_id(player.team_id, result.players),
            position=player.position,
            price_tenths=player.price_tenths,
        )
        for player in result.players
    )
    plan = next(
        (value for value in result.gameweek_plans if value.gameweek_number == gameweek),
        None,
    )
    starting = result.starting_player_ids if plan is None else plan.starting_player_ids
    captain = result.captain_id if plan is None else plan.captain_id
    vice = result.vice_captain_id if plan is None else plan.vice_captain_id
    # The plan's own bench, never the squad-level one: a rotated XI leaves the
    # opening Gameweek's bench listing a player who is now starting and
    # omitting the one who is not.
    bench = (
        result.bench_player_ids
        if plan is None or not plan.bench_player_ids
        else plan.bench_player_ids
    )
    if plan is not None and plan.bench_player_ids and set(bench) & set(starting):
        raise ValueError(
            f"GW{gameweek} plan lists a starter on its bench; the lineup is not "
            "a legal squad"
        )
    squad = Squad(
        players=players,
        starting_player_ids=frozenset(id_lookup[value] for value in starting),
        bench_player_ids=tuple(id_lookup[value] for value in bench),
        captain_id=id_lookup[captain],
        vice_captain_id=id_lookup[vice],
    )
    points = {
        id_lookup[player_id]: outcome.points
        for player_id, outcome in outcomes.items()
        if player_id in id_lookup
    }
    minutes = {
        id_lookup[player_id]: outcome.minutes
        for player_id, outcome in outcomes.items()
        if player_id in id_lookup
    }
    score = calculate_team_score(squad, points, minutes, rules)
    inverse = {value: key for key, value in id_lookup.items()}
    return ResolvedGameweek(
        total_points=score.total_points,
        scoring_player_ids=frozenset(
            inverse[player_id] for player_id in score.scoring_player_ids
        ),
        effective_captain_id=(
            None
            if score.effective_captain_id is None
            else inverse[score.effective_captain_id]
        ),
        substitution_count=len(score.substitutions),
    )


def _numeric_team_id(
    team_id: str,
    players: tuple[CandidatePlayer, ...],
) -> int:
    teams = sorted({player.team_id for player in players})
    return teams.index(team_id) + 1
