"""Compare the squads two projection configurations actually pick.

A configuration change is only interesting if it changes a decision. This
builds the opening squad each configuration would select, values each squad
under *both* sets of beliefs, and reports which players survive plausible
perturbations of the parameters that changed.

Cross-valuation matters because two configurations produce expected points on
their own scales. "Configuration B scores 4 points more than A" is meaningless
across scales; "B thinks its own squad beats A's by 4 points, and A thinks the
reverse by 1" is not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .config import SeasonRules
from .domain import Position
from .evaluation import player_metadata_as_of
from .history.database import HistoricalDatabase
from .optimisation import (
    CandidatePlayer,
    FullSquadResult,
    GameweekPlayerValue,
    optimise_full_squad,
)
from .projections import ProjectionModelConfig, RatesProjectionModel


@dataclass(frozen=True)
class SquadPlayer:
    source_player_id: str
    web_name: str
    team_short_name: str
    position: str
    price_tenths: int
    role: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OpeningSquad:
    label: str
    season_code: str
    gameweek: int
    horizon_gameweeks: int
    players: tuple[SquadPlayer, ...]
    captain_id: str
    vice_captain_id: str
    total_cost_tenths: int
    horizon_expected_points: float

    @property
    def player_ids(self) -> frozenset[str]:
        return frozenset(player.source_player_id for player in self.players)

    @property
    def starting_ids(self) -> frozenset[str]:
        return frozenset(
            player.source_player_id
            for player in self.players
            if player.role == "starter"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "season_code": self.season_code,
            "gameweek": self.gameweek,
            "horizon_gameweeks": self.horizon_gameweeks,
            "players": [player.as_dict() for player in self.players],
            "captain_id": self.captain_id,
            "vice_captain_id": self.vice_captain_id,
            "total_cost_tenths": self.total_cost_tenths,
            "horizon_expected_points": self.horizon_expected_points,
        }


def build_opening_squad(
    database: HistoricalDatabase,
    rules: SeasonRules,
    config: ProjectionModelConfig,
    *,
    label: str,
    season_code: str,
    gameweek: int = 1,
    horizon_gameweeks: int = 8,
    generated_at: datetime | None = None,
) -> tuple[OpeningSquad, tuple[CandidatePlayer, ...]]:
    """Project with one configuration and select its optimal £100m squad."""

    candidates = opening_candidates(
        database,
        rules,
        config,
        season_code=season_code,
        gameweek=gameweek,
        horizon_gameweeks=horizon_gameweeks,
        generated_at=generated_at,
    )
    result = optimise_full_squad(
        candidates,
        budget_tenths=rules.squad.budget_tenths,
        rules=rules,
    )
    return (
        _summarise(result, label, season_code, gameweek, horizon_gameweeks),
        candidates,
    )


def opening_candidates(
    database: HistoricalDatabase,
    rules: SeasonRules,
    config: ProjectionModelConfig,
    *,
    season_code: str,
    gameweek: int,
    horizon_gameweeks: int,
    generated_at: datetime | None = None,
    overrides: tuple[Any, ...] = (),
    team_overrides: tuple[Any, ...] = (),
    model_version: str = "squad-comparison",
) -> tuple[CandidatePlayer, ...]:
    """Project in memory and attach the prices the optimiser needs.

    ``team_overrides`` replaces a named club's attack and defence multipliers
    outright, which is what a concentration test needs: the perturbation has to
    travel through expected goals into every consumer — the club's own
    attackers, their opponents' clean sheets, bonus and defensive contribution
    — rather than being applied to a finished points total.
    """

    season = database.connection.execute(
        "SELECT id FROM seasons WHERE code = ?", (season_code,)
    ).fetchone()
    if season is None:
        raise ValueError(f"Season {season_code!r} is unavailable")
    result = RatesProjectionModel(
        database,
        rules,
        config=config,
        model_version=model_version,
    ).project(
        season_code=season_code,
        start_gameweek=gameweek,
        horizon_gameweeks=horizon_gameweeks,
        overrides=overrides,
        team_overrides=team_overrides,
        generated_at=generated_at or datetime.now(UTC),
        persist=False,
    )
    metadata = player_metadata_as_of(database, int(season["id"]), gameweek, None)
    by_source_id = {
        str(row["source_player_id"]): row for row in metadata.values()
    }
    grouped: dict[str, list[Any]] = {}
    for projection in result.projections:
        grouped.setdefault(projection.source_player_id, []).append(projection)
    candidates = []
    for source_player_id, rows in grouped.items():
        player = by_source_id.get(source_player_id)
        if player is None or player["price_tenths"] is None:
            continue
        ordered = sorted(rows, key=lambda row: row.gameweek_number)
        values = tuple(
            GameweekPlayerValue(
                gameweek_number=row.gameweek_number,
                expected_points=row.expected_points,
                appearance_probability=row.appearance_probability,
                sixty_probability=row.sixty_probability,
            )
            for row in ordered
        )
        candidates.append(
            CandidatePlayer(
                source_player_id=source_player_id,
                web_name=str(player["web_name"]),
                team_id=str(player["team_id"]),
                team_short_name=str(player["team_short_name"]),
                position=Position(str(player["position"])),
                price_tenths=int(player["price_tenths"]),
                expected_points=sum(value.expected_points for value in values),
                gameweek_expected_points=values[0].expected_points,
                appearance_probability=values[0].appearance_probability,
                gameweek_values=values,
            )
        )
    if not candidates:
        raise ValueError(
            f"No priced candidates exist for {season_code} GW{gameweek}"
        )
    return tuple(sorted(candidates, key=lambda player: player.source_player_id))


def value_squad_under(
    squad_ids: frozenset[str],
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
) -> float:
    """Score a fixed fifteen using another configuration's beliefs.

    The squad is held fixed and only the weekly lineups are re-optimised, so
    the number answers "what would this configuration expect from that squad".
    """

    held = tuple(
        player for player in candidates if player.source_player_id in squad_ids
    )
    if len(held) != len(squad_ids):
        missing = sorted(squad_ids - {p.source_player_id for p in held})
        raise ValueError(
            "Cannot value a squad whose players are absent from the other "
            f"configuration's candidates: {missing}"
        )
    result = optimise_full_squad(
        held,
        budget_tenths=sum(player.price_tenths for player in held),
        rules=rules,
    )
    return result.horizon_expected_points


def compare_opening_squads(
    database: HistoricalDatabase,
    rules: SeasonRules,
    configs: dict[str, ProjectionModelConfig],
    *,
    season_code: str,
    gameweek: int = 1,
    horizon_gameweeks: int = 8,
    sensitivity_parameters: tuple[str, ...] = (),
    sensitivity_fraction: float = 0.25,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build each configuration's squad, cross-value them, and test stability."""

    if len(configs) != 2:
        raise ValueError("Exactly two configurations are compared")
    if not 0 < sensitivity_fraction < 1:
        raise ValueError("Sensitivity fraction must be within (0, 1)")
    labels = tuple(configs)
    squads = {}
    candidate_sets = {}
    for label in labels:
        squads[label], candidate_sets[label] = build_opening_squad(
            database,
            rules,
            configs[label],
            label=label,
            season_code=season_code,
            gameweek=gameweek,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated_at,
        )

    first, second = labels
    shared = squads[first].player_ids & squads[second].player_ids
    # Each configuration values both squads on its own scale, because the two
    # scales are not comparable to one another.
    cross = {}
    for holder in labels:
        for owner in labels:
            cross[f"{owner}_squad_under_{holder}"] = round(
                value_squad_under(
                    squads[owner].player_ids,
                    candidate_sets[holder],
                    rules,
                ),
                3,
            )
    disagreement = {
        holder: round(
            cross[f"{holder}_squad_under_{holder}"]
            - cross[
                f"{next(other for other in labels if other != holder)}"
                f"_squad_under_{holder}"
            ],
            3,
        )
        for holder in labels
    }

    sensitivity = _sensitivity(
        database,
        rules,
        configs[second],
        parameters=sensitivity_parameters,
        fraction=sensitivity_fraction,
        season_code=season_code,
        gameweek=gameweek,
        horizon_gameweeks=horizon_gameweeks,
        generated_at=generated_at,
        base_ids=squads[second].player_ids,
    )
    return {
        "season_code": season_code,
        "gameweek": gameweek,
        "horizon_gameweeks": horizon_gameweeks,
        "squads": {label: squads[label].as_dict() for label in labels},
        "shared_player_count": len(shared),
        "only_in_" + first: sorted(squads[first].player_ids - shared),
        "only_in_" + second: sorted(squads[second].player_ids - shared),
        "cross_valuation": cross,
        "self_minus_other": disagreement,
        "sensitivity": sensitivity,
        "limitations": [
            "Expected points are on each configuration's own scale, so only "
            "the self-minus-other differences may be read as a gap.",
            "The comparison is a selection difference, not evidence that "
            "either configuration forecasts better.",
            "Prices are those recorded at the compared Gameweek.",
        ],
    }


def _sensitivity(
    database: HistoricalDatabase,
    rules: SeasonRules,
    config: ProjectionModelConfig,
    *,
    parameters: tuple[str, ...],
    fraction: float,
    season_code: str,
    gameweek: int,
    horizon_gameweeks: int,
    generated_at: datetime | None,
    base_ids: frozenset[str],
) -> dict[str, Any]:
    """Perturb each parameter up and down, and see who keeps their place."""

    if not parameters:
        return {
            "parameters": [],
            "variants": [],
            "core_player_ids": sorted(base_ids),
            "swing_player_ids": [],
            "note": "No sensitivity parameters were requested.",
        }
    variants = []
    memberships = [base_ids]
    for name in parameters:
        base_value = float(getattr(config, name))
        for direction in (-1, 1):
            value = base_value * (1 + direction * fraction)
            perturbed = replace(config, **{name: value})
            if (
                perturbed.cold_start_maximum_factor
                < perturbed.cold_start_minimum_factor
            ):
                continue
            squad, _ = build_opening_squad(
                database,
                rules,
                perturbed,
                label=f"{name}{'+' if direction > 0 else '-'}",
                season_code=season_code,
                gameweek=gameweek,
                horizon_gameweeks=horizon_gameweeks,
                generated_at=generated_at,
            )
            memberships.append(squad.player_ids)
            variants.append(
                {
                    "parameter": name,
                    "value": round(value, 6),
                    "changed_from_base": sorted(
                        base_ids ^ squad.player_ids
                    ),
                }
            )
    core = frozenset.intersection(*memberships) if memberships else frozenset()
    swing = frozenset.union(*memberships) - core
    return {
        "parameters": list(parameters),
        "fraction": fraction,
        "variants": variants,
        "core_player_ids": sorted(core),
        "swing_player_ids": sorted(swing),
        "note": (
            "Core players hold their place under every perturbation; swing "
            "players are selections the parameter values are carrying."
        ),
    }


def _summarise(
    result: FullSquadResult,
    label: str,
    season_code: str,
    gameweek: int,
    horizon_gameweeks: int,
) -> OpeningSquad:
    bench_order = {
        player_id: index
        for index, player_id in enumerate(result.bench_player_ids)
    }
    players = tuple(
        SquadPlayer(
            source_player_id=player.source_player_id,
            web_name=player.web_name,
            team_short_name=player.team_short_name,
            position=player.position.value,
            price_tenths=player.price_tenths,
            role=(
                "starter"
                if player.source_player_id in result.starting_player_ids
                else f"bench{bench_order[player.source_player_id]}"
            ),
        )
        for player in sorted(
            result.players,
            key=lambda player: (
                player.source_player_id not in result.starting_player_ids,
                bench_order.get(player.source_player_id, -1),
                player.position.value,
                player.web_name,
            ),
        )
    )
    return OpeningSquad(
        label=label,
        season_code=season_code,
        gameweek=gameweek,
        horizon_gameweeks=horizon_gameweeks,
        players=players,
        captain_id=result.captain_id,
        vice_captain_id=result.vice_captain_id,
        total_cost_tenths=result.total_cost_tenths,
        horizon_expected_points=result.horizon_expected_points,
    )
