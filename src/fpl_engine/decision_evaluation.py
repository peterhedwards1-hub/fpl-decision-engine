"""Continuous transfer replay with hits, free transfers and exact autosubs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import SeasonRules
from .domain import Player, Squad
from .optimisation import CandidatePlayer, FullSquadResult
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


@dataclass(frozen=True)
class TransferContinuityReport:
    weeks: tuple[TransferReplayResult, ...]
    total_net_points: int
    total_same_state_hindsight_points: int
    total_regret: int
    total_hits: int
    final_free_transfers: int
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
) -> TransferContinuityReport:
    """Replay one persistent model-owned squad over consecutive Gameweeks."""

    if not weeks:
        raise ValueError("At least one replay week is required")
    if tuple(sorted(week.gameweek_number for week in weeks)) != tuple(
        week.gameweek_number for week in weeks
    ):
        raise ValueError("Replay weeks must be chronological")
    if len({week.gameweek_number for week in weeks}) != len(weeks):
        raise ValueError("Replay Gameweeks must be unique")
    current = initial_squad
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
        recommendation = recommend_transfers(
            week.forecast_candidates,
            current,
            rules=rules,
            max_transfers=max_transfers_per_week,
        )
        route = recommendation.primary
        gross, autosubs, captain = _score_result(
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
        hindsight_gross, _, _ = _score_result(
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
            )
        )
        selected_ids = frozenset(
            player.source_player_id for player in route.resulting_squad.players
        )
        current = CurrentSquad(
            player_ids=selected_ids,
            selling_prices_tenths={
                player_id: forecast_by_id[player_id].price_tenths for player_id in selected_ids
            },
            bank_tenths=route.bank_tenths,
            free_transfers=route.next_free_transfers,
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
        limitations=(
            "The squad, bank and free-transfer state persist between Gameweeks.",
            "Realised scoring applies the forecast lineup, bench order, exact "
            "autosubs and captain fallback.",
            "The comparator is a same-state one-Gameweek hindsight action, "
            "not a globally clairvoyant season policy.",
            "Selling prices use the supplied candidate price at each replay "
            "origin; purchase-price profit history is not reconstructed.",
        ),
    )


def _score_result(
    result: FullSquadResult,
    outcomes: dict[str, RealisedPlayerOutcome],
    rules: SeasonRules,
    gameweek: int,
) -> tuple[int, int, str | None]:
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
    squad = Squad(
        players=players,
        starting_player_ids=frozenset(id_lookup[value] for value in starting),
        bench_player_ids=tuple(id_lookup[value] for value in result.bench_player_ids),
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
    return (
        score.total_points,
        len(score.substitutions),
        (None if score.effective_captain_id is None else inverse[score.effective_captain_id]),
    )


def _numeric_team_id(
    team_id: str,
    players: tuple[CandidatePlayer, ...],
) -> int:
    teams = sorted({player.team_id for player in players})
    return teams.index(team_id) + 1
