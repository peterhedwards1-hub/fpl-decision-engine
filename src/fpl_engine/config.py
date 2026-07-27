from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SquadRules:
    budget_tenths: int
    squad_size: int
    max_players_per_team: int
    position_counts: dict[str, int]
    starting_size: int
    formation_min: dict[str, int]
    formation_max: dict[str, int]


@dataclass(frozen=True)
class ScoringRules:
    appearance_under_60: int
    appearance_60_or_more: int
    goals: dict[str, int]
    assists: int
    clean_sheets: dict[str, int]
    goals_conceded_threshold_minutes: int
    goals_conceded_per_two: dict[str, int]
    saves_per_point: int
    penalty_save: int
    penalty_miss: int
    yellow_card: int
    red_card: int
    own_goal: int
    bonus_max: int
    defensive_contribution_thresholds: dict[str, int]
    defensive_contribution_points: int


@dataclass(frozen=True)
class TransferRules:
    initial_free_transfers: int
    maximum_free_transfers: int
    transfer_hit_cost: int


@dataclass(frozen=True)
class SeasonRules:
    season: str
    currency: str
    squad: SquadRules
    scoring: ScoringRules
    transfers: TransferRules
    chip_sets: int
    first_chip_set_expiry_gameweek: int | None


def _required(data: dict[str, Any], key: str) -> Any:
    try:
        return data[key]
    except KeyError as exc:
        raise ValueError(f"Missing required season rule: {key}") from exc


def load_season_rules(path: str | Path) -> SeasonRules:
    """Load immutable, versioned rules from a JSON file."""
    file_path = Path(path)
    with file_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    squad_raw = _required(raw, "squad")
    scoring_raw = _required(raw, "scoring")
    transfer_raw = _required(raw, "transfers")

    rules = SeasonRules(
        season=str(_required(raw, "season")),
        currency=str(raw.get("currency", "GBP")),
        squad=SquadRules(
            budget_tenths=int(_required(squad_raw, "budget_tenths")),
            squad_size=int(_required(squad_raw, "squad_size")),
            max_players_per_team=int(_required(squad_raw, "max_players_per_team")),
            position_counts={k: int(v) for k, v in _required(squad_raw, "position_counts").items()},
            starting_size=int(_required(squad_raw, "starting_size")),
            formation_min={k: int(v) for k, v in _required(squad_raw, "formation_min").items()},
            formation_max={k: int(v) for k, v in _required(squad_raw, "formation_max").items()},
        ),
        scoring=ScoringRules(
            appearance_under_60=int(_required(scoring_raw, "appearance_under_60")),
            appearance_60_or_more=int(_required(scoring_raw, "appearance_60_or_more")),
            goals={k: int(v) for k, v in _required(scoring_raw, "goals").items()},
            assists=int(_required(scoring_raw, "assists")),
            clean_sheets={k: int(v) for k, v in _required(scoring_raw, "clean_sheets").items()},
            goals_conceded_threshold_minutes=int(
                _required(scoring_raw, "goals_conceded_threshold_minutes")
            ),
            goals_conceded_per_two={
                k: int(v) for k, v in _required(scoring_raw, "goals_conceded_per_two").items()
            },
            saves_per_point=int(_required(scoring_raw, "saves_per_point")),
            penalty_save=int(_required(scoring_raw, "penalty_save")),
            penalty_miss=int(_required(scoring_raw, "penalty_miss")),
            yellow_card=int(_required(scoring_raw, "yellow_card")),
            red_card=int(_required(scoring_raw, "red_card")),
            own_goal=int(_required(scoring_raw, "own_goal")),
            bonus_max=int(_required(scoring_raw, "bonus_max")),
            defensive_contribution_thresholds={
                k: int(v)
                for k, v in _required(scoring_raw, "defensive_contribution_thresholds").items()
            },
            defensive_contribution_points=int(
                _required(scoring_raw, "defensive_contribution_points")
            ),
        ),
        transfers=TransferRules(
            initial_free_transfers=int(_required(transfer_raw, "initial_free_transfers")),
            maximum_free_transfers=int(_required(transfer_raw, "maximum_free_transfers")),
            transfer_hit_cost=int(_required(transfer_raw, "transfer_hit_cost")),
        ),
        chip_sets=int(_required(raw, "chip_sets")),
        first_chip_set_expiry_gameweek=(
            int(raw["first_chip_set_expiry_gameweek"])
            if raw.get("first_chip_set_expiry_gameweek") is not None
            else None
        ),
    )

    _validate_rules(rules)
    return rules


def _validate_rules(rules: SeasonRules) -> None:
    if sum(rules.squad.position_counts.values()) != rules.squad.squad_size:
        raise ValueError("Configured position counts do not equal squad size")
    if rules.squad.starting_size >= rules.squad.squad_size:
        raise ValueError("Starting size must be smaller than squad size")
    if rules.squad.budget_tenths <= 0:
        raise ValueError("Budget must be positive")
    if rules.transfers.maximum_free_transfers < rules.transfers.initial_free_transfers:
        raise ValueError("Maximum free transfers cannot be below the initial allocation")
