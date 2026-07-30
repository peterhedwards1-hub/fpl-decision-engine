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
    free_transfers_per_gameweek: int
    maximum_free_transfers: int
    transfer_hit_cost: int


@dataclass(frozen=True)
class SellingPriceRules:
    profit_step_tenths: int
    profit_return_tenths: int


@dataclass(frozen=True)
class ChipRules:
    names: tuple[str, ...]
    sets_per_season: int
    first_set_expiry_gameweek: int
    second_set_start_gameweek: int
    maximum_active_per_gameweek: int
    unavailable_gameweeks: dict[str, tuple[int, ...]]
    minimum_gap_gameweeks: dict[str, int]
    banked_transfers_preserved: bool


@dataclass(frozen=True)
class SeasonRules:
    season: str
    currency: str
    squad: SquadRules
    scoring: ScoringRules
    transfers: TransferRules
    selling_prices: SellingPriceRules
    chips: ChipRules
    validated_on: str
    source_urls: tuple[str, ...]

    @property
    def chip_sets(self) -> int:
        """Compatibility alias for callers written before the chip contract."""

        return self.chips.sets_per_season

    @property
    def first_chip_set_expiry_gameweek(self) -> int:
        """Compatibility alias for callers written before the chip contract."""

        return self.chips.first_set_expiry_gameweek


def _required(data: dict[str, Any], key: str) -> Any:
    try:
        return data[key]
    except KeyError as exc:
        raise ValueError(f"Missing required season rule: {key}") from exc


def load_season_rules(path: str | Path) -> SeasonRules:
    """Load immutable, versioned rules from a JSON file."""
    file_path = Path(path)
    raw = _load_rule_document(file_path, seen=set())

    squad_raw = _required(raw, "squad")
    scoring_raw = _required(raw, "scoring")
    transfer_raw = _required(raw, "transfers")
    selling_price_raw = _required(raw, "selling_prices")
    chips_raw = _required(raw, "chips")

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
            free_transfers_per_gameweek=int(
                _required(transfer_raw, "free_transfers_per_gameweek")
            ),
            maximum_free_transfers=int(_required(transfer_raw, "maximum_free_transfers")),
            transfer_hit_cost=int(_required(transfer_raw, "transfer_hit_cost")),
        ),
        selling_prices=SellingPriceRules(
            profit_step_tenths=int(
                _required(selling_price_raw, "profit_step_tenths")
            ),
            profit_return_tenths=int(
                _required(selling_price_raw, "profit_return_tenths")
            ),
        ),
        chips=ChipRules(
            names=tuple(str(value) for value in _required(chips_raw, "names")),
            sets_per_season=int(_required(chips_raw, "sets_per_season")),
            first_set_expiry_gameweek=int(
                _required(chips_raw, "first_set_expiry_gameweek")
            ),
            second_set_start_gameweek=int(
                _required(chips_raw, "second_set_start_gameweek")
            ),
            maximum_active_per_gameweek=int(
                _required(chips_raw, "maximum_active_per_gameweek")
            ),
            unavailable_gameweeks={
                str(name): tuple(int(value) for value in gameweeks)
                for name, gameweeks in _required(
                    chips_raw, "unavailable_gameweeks"
                ).items()
            },
            minimum_gap_gameweeks={
                str(name): int(value)
                for name, value in _required(
                    chips_raw, "minimum_gap_gameweeks"
                ).items()
            },
            banked_transfers_preserved=bool(
                _required(chips_raw, "banked_transfers_preserved")
            ),
        ),
        validated_on=str(_required(raw, "validated_on")),
        source_urls=tuple(str(value) for value in _required(raw, "source_urls")),
    )

    _validate_rules(rules)
    return rules


def _load_rule_document(
    path: Path,
    *,
    seen: set[Path],
) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved in seen:
        raise ValueError(f"Circular season-rule inheritance at {path}")
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("Season rules must be a JSON object")
    parent_name = raw.pop("extends", None)
    if parent_name is None:
        return raw
    if not isinstance(parent_name, str) or not parent_name.strip():
        raise ValueError("Season-rule extends must name a JSON file")
    parent = _load_rule_document(
        path.parent / parent_name,
        seen={*seen, resolved},
    )
    return _deep_merge(parent, raw)


def _deep_merge(
    base: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_rules(rules: SeasonRules) -> None:
    if sum(rules.squad.position_counts.values()) != rules.squad.squad_size:
        raise ValueError("Configured position counts do not equal squad size")
    if rules.squad.starting_size >= rules.squad.squad_size:
        raise ValueError("Starting size must be smaller than squad size")
    if rules.squad.budget_tenths <= 0:
        raise ValueError("Budget must be positive")
    if rules.transfers.maximum_free_transfers < rules.transfers.initial_free_transfers:
        raise ValueError("Maximum free transfers cannot be below the initial allocation")
    if rules.transfers.free_transfers_per_gameweek <= 0:
        raise ValueError("Free transfers per Gameweek must be positive")
    if rules.transfers.transfer_hit_cost <= 0:
        raise ValueError("Transfer hit cost must be positive")
    if rules.selling_prices.profit_step_tenths <= 0:
        raise ValueError("Selling-price profit step must be positive")
    if not 0 < rules.selling_prices.profit_return_tenths <= (
        rules.selling_prices.profit_step_tenths
    ):
        raise ValueError("Selling-price return must be within the configured profit step")
    if rules.chips.sets_per_season <= 0:
        raise ValueError("Chip sets per season must be positive")
    if rules.chips.maximum_active_per_gameweek != 1:
        raise ValueError("FPL permits exactly one active chip per Gameweek")
    if rules.chips.second_set_start_gameweek != (
        rules.chips.first_set_expiry_gameweek + 1
    ):
        raise ValueError("The second chip set must start after the first set expires")
    if len(set(rules.chips.names)) != len(rules.chips.names):
        raise ValueError("Chip names must be unique")
    unknown_availability = set(rules.chips.unavailable_gameweeks) - set(
        rules.chips.names
    )
    unknown_gaps = set(rules.chips.minimum_gap_gameweeks) - set(rules.chips.names)
    if unknown_availability or unknown_gaps:
        raise ValueError("Chip availability configuration contains an unknown chip")
    if not rules.source_urls:
        raise ValueError("At least one official rules source URL is required")
