"""Continuous transfer replay with hits, free transfers and exact autosubs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .chip_state import (
    SCORING_CHIPS,
    ChipDecisionContext,
    ChipLedger,
    ScoringChipPolicy,
    set_expiry_gameweek,
)
from .config import SeasonRules
from .domain import Chip, Player, Squad
from .optimisation import (
    CandidatePlayer,
    FullSquadResult,
    chip_values_for_gameweek,
)
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
    active_chip: str | None = None
    chip_forecast_gain: float | None = None
    chip_realised_gain: int | None = None
    chip_best_later_forecast: dict[str, float] | None = None
    chip_lookahead_gameweeks: tuple[int, ...] = ()
    chip_lookahead_reaches_expiry: bool = False


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
    chip_plays: tuple[dict[str, Any], ...]
    chip_counterfactual: dict[str, tuple[dict[str, Any], ...]]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["weeks"] = [asdict(week) for week in self.weeks]
        result["chip_plays"] = [dict(play) for play in self.chip_plays]
        result["chip_counterfactual"] = {
            chip: [dict(entry) for entry in entries]
            for chip, entries in self.chip_counterfactual.items()
        }
        result["limitations"] = list(self.limitations)
        return result


def replay_transfer_continuity(
    weeks: tuple[TransferReplayWeek, ...],
    initial_squad: CurrentSquad,
    *,
    rules: SeasonRules,
    max_transfers_per_week: int = 2,
    initial_ledger: PurchaseLedger | None = None,
    chip_policy: Any | None = None,
    initial_chip_ledger: ChipLedger | None = None,
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
    policy = chip_policy or ScoringChipPolicy()
    chips = initial_chip_ledger or ChipLedger()
    counterfactual: dict[str, list[dict[str, Any]]] = {
        chip.value: [] for chip in SCORING_CHIPS
    }
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
        # The chip is chosen from the forecast, like every other decision, and
        # only then scored against outcomes. Every projected Gameweek is
        # valued, not only this one, so a later double Gameweek can hold the
        # chip back.
        plan_numbers = [
            plan.gameweek_number
            for plan in route.resulting_squad.gameweek_plans
        ]
        if plan_numbers == [0]:
            # Candidates carrying no per-Gameweek values leave a single
            # unnumbered plan, so only this Gameweek can be valued and there is
            # nothing to look ahead to.
            projected = {week.gameweek_number: 0}
        else:
            projected = {
                number: number
                for number in plan_numbers
                if number >= week.gameweek_number
            }
        context = ChipDecisionContext(
            gameweek_number=week.gameweek_number,
            values_by_gameweek={
                gameweek: chip_values_for_gameweek(
                    route.resulting_squad, source, rules
                )
                for gameweek, source in projected.items()
            },
            legal_by_gameweek={
                gameweek: chips.available(gameweek, rules)
                for gameweek in projected
            },
            expiry_gameweek=set_expiry_gameweek(week.gameweek_number, rules),
        )
        chip_gains = context.values_by_gameweek[week.gameweek_number]
        chosen_chip = policy.choose(context)
        gross, autosubs, captain = score_squad_gameweek(
            route.resulting_squad,
            realised,
            rules,
            week.gameweek_number,
            active_chip=chosen_chip,
        )
        # What each scoring chip would have been worth on the squad actually
        # owned this Gameweek. Recorded for every week so chip timing can be
        # scored against the alternatives, not only the week chosen.
        without_chip_points, _, _ = score_squad_gameweek(
            route.resulting_squad,
            realised,
            rules,
            week.gameweek_number,
        )
        for chip in SCORING_CHIPS:
            with_chip, _, _ = score_squad_gameweek(
                route.resulting_squad,
                realised,
                rules,
                week.gameweek_number,
                active_chip=chip,
            )
            counterfactual[chip.value].append(
                {
                    "gameweek_number": week.gameweek_number,
                    "chip": chip.value,
                    "legal": not chips.errors_for(
                        chip, week.gameweek_number, rules
                    ),
                    "realised_gain": with_chip - without_chip_points,
                }
            )
        chip_realised_gain = None
        if chosen_chip is not None:
            chip_realised_gain = gross - without_chip_points
            chips = chips.after_playing(chosen_chip, week.gameweek_number, rules)

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
            # The same chip on both sides, so the difference stays a transfer
            # difference rather than a chip-timing one.
            active_chip=chosen_chip,
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
                active_chip=(
                    None if chosen_chip is None else chosen_chip.value
                ),
                chip_forecast_gain=(
                    None
                    if chosen_chip is None
                    else round(chip_gains[chosen_chip], 3)
                ),
                chip_realised_gain=chip_realised_gain,
                # Recorded so a decision to wait is auditable, and so a short
                # horizon is visible rather than silently assumed sufficient.
                chip_best_later_forecast={
                    chip.value: round(context.best_later_value(chip), 3)
                    for chip in SCORING_CHIPS
                },
                chip_lookahead_gameweeks=context.lookahead_gameweeks,
                chip_lookahead_reaches_expiry=context.reaches_expiry,
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
            # Only chips actually spent are removed. The ledger replays two of
            # them; it must not silently drop the ones it cannot value.
            available_chips=tuple(
                chip
                for chip in current.available_chips
                if chip not in {play.chip for play in chips.plays}
            ),
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
        chip_plays=tuple(play.as_dict() for play in chips.plays),
        chip_counterfactual={
            chip: tuple(entries) for chip, entries in counterfactual.items()
        },
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
            "Chips follow the declared policy, which plays nothing by default; "
            "both branches are scored under the same chip, so the comparison "
            "stays a transfer comparison rather than a chip-timing one.",
            "Only Bench Boost and Triple Captain are replayable. Wildcard and "
            "Free Hit change which squad exists, so their value depends on "
            "future state and opportunity cost.",
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
    active_chip: str | None = None


def score_squad_gameweek(
    result: FullSquadResult,
    outcomes: dict[str, RealisedPlayerOutcome],
    rules: SeasonRules,
    gameweek: int,
    *,
    active_chip: Chip | None = None,
) -> tuple[int, int, str | None]:
    """Points, substitution count and the captain who actually counted."""

    resolved = resolve_squad_gameweek(
        result, outcomes, rules, gameweek, active_chip=active_chip
    )
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
    *,
    active_chip: Chip | None = None,
) -> ResolvedGameweek:
    """Apply the Gameweek's lineup plan, bench-order autosubs and captain fallback.

    Returns the scoring lineup as well as the score, because the set of players
    who actually counted is what a captaincy decision could have chosen from.
    An active chip changes what counts: Bench Boost scores every player who
    appeared, Triple Captain multiplies the effective captain once more.
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
    score = calculate_team_score(
        squad, points, minutes, rules, active_chip=active_chip
    )
    inverse = {value: key for key, value in id_lookup.items()}
    return ResolvedGameweek(
        active_chip=None if active_chip is None else active_chip.value,
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
