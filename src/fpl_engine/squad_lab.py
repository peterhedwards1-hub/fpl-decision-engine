"""Constrained squad search you can drive by hand.

The exact decision value is not the objective the solver optimises, so the
best squad is found by search rather than proof: enumerate a neighbourhood,
exact-value every member, move to the best improvement, repeat, then kick out
of the basin and climb again.

Two things make it usable interactively rather than as an overnight job. It is
bounded by a wall-clock budget and returns the best squad found so far when
that runs out, and every constraint the caller sets is honoured by the
neighbourhood itself, so a search that must keep a player never wastes a solve
on a squad without them.

The objective may be a single projection, or the *worst case* across several.
Two minutes configurations that score alike on historical accuracy can still
disagree by double digits about which squad to pick, so a squad chosen under
one of them is partly fitted to its quirks. Scoring on the lower of several
asks for a squad that is good under all of them.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import SeasonRules
from .optimisation import (
    CandidatePlayer,
    OptimisationError,
    SquadGroupConstraint,
    optimise_full_squad,
)

#: Worker-process state. Populated once per process by ``_initialise`` so the
#: candidate pools are pickled per worker rather than per task.
_WORKER: dict[str, Any] = {}


@dataclass(frozen=True)
class ClubLimit:
    """A tighter cap than the rules require, for one club."""

    team_short_name: str
    maximum: int


@dataclass(frozen=True)
class SquadLabRequest:
    """Everything a hand-driven search needs, and nothing about how it runs."""

    budget_tenths: int
    required_player_ids: frozenset[str] = frozenset()
    forbidden_player_ids: frozenset[str] = frozenset()
    club_limits: tuple[ClubLimit, ...] = ()
    time_budget_seconds: float | None = 300.0
    kicks: int = 3
    kick_size: int = 3
    seed: int = 20260812

    def __post_init__(self) -> None:
        if self.budget_tenths <= 0:
            raise ValueError("Budget must be positive")
        if self.time_budget_seconds is not None and self.time_budget_seconds <= 0:
            raise ValueError("Time budget must be positive")
        if self.kicks < 0:
            raise ValueError("Kick count cannot be negative")
        if self.kick_size < 1:
            raise ValueError("Kick size must be at least one")
        overlap = self.required_player_ids & self.forbidden_player_ids
        if overlap:
            raise ValueError(
                "These players are both required and excluded: "
                + ", ".join(sorted(overlap))
            )
        for limit in self.club_limits:
            if limit.maximum < 0:
                raise ValueError(
                    f"Club limit for {limit.team_short_name} cannot be negative"
                )


@dataclass
class SquadLabResult:
    """The best squad found, and enough context to judge whether to trust it."""

    player_ids: tuple[str, ...]
    objective: float
    per_model: dict[str, float]
    climbs: list[dict[str, Any]] = field(default_factory=list)
    evaluations: int = 0
    seconds: float = 0.0
    exhausted_budget: bool = False
    note: str = ""


def _initialise(
    pools: Mapping[str, tuple[CandidatePlayer, ...]], rules: SeasonRules
) -> None:
    _WORKER["pools"] = {
        label: {player.source_player_id: player for player in players}
        for label, players in pools.items()
    }
    _WORKER["rules"] = rules


def _score(
    player_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], float, dict[str, float]]:
    """Exact decision value in every pool; the objective is the worst of them."""

    values: dict[str, float] = {}
    for label, pool in _WORKER["pools"].items():
        held = tuple(pool[player_id] for player_id in player_ids)
        try:
            result = optimise_full_squad(
                held,
                budget_tenths=sum(player.price_tenths for player in held),
                rules=_WORKER["rules"],
            )
        except (OptimisationError, ValueError):
            return player_ids, float("-inf"), {}
        values[label] = result.decision_value
    return player_ids, min(values.values()), values


def _club_counts(players: Sequence[CandidatePlayer]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for player in players:
        counts[player.team_short_name] = counts.get(player.team_short_name, 0) + 1
    return counts


def _admissible(
    players: Sequence[CandidatePlayer],
    request: SquadLabRequest,
    rules: SeasonRules,
    caps: Mapping[str, int],
) -> bool:
    if sum(player.price_tenths for player in players) > request.budget_tenths:
        return False
    held = {player.source_player_id for player in players}
    if not request.required_player_ids <= held:
        return False
    if held & request.forbidden_player_ids:
        return False
    for club, count in _club_counts(players).items():
        if count > caps.get(club, rules.squad.max_players_per_team):
            return False
    shape: dict[str, int] = {}
    for player in players:
        shape[player.position.value] = shape.get(player.position.value, 0) + 1
    return shape == dict(rules.squad.position_counts)


def _neighbours(
    player_ids: tuple[str, ...],
    pool: Mapping[str, CandidatePlayer],
    request: SquadLabRequest,
    rules: SeasonRules,
    caps: Mapping[str, int],
) -> list[tuple[str, ...]]:
    """Every legal one-for-one replacement that still satisfies the request."""

    squad = [pool[player_id] for player_id in player_ids]
    held = set(player_ids)
    moves: list[tuple[str, ...]] = []
    for index, outgoing in enumerate(squad):
        # A required player is never a candidate to leave, which is both
        # correct and the cheapest possible way to enforce it.
        if outgoing.source_player_id in request.required_player_ids:
            continue
        rest = [player for position, player in enumerate(squad) if position != index]
        rest_cost = sum(player.price_tenths for player in rest)
        rest_clubs = _club_counts(rest)
        for incoming in pool.values():
            if (
                incoming.source_player_id in held
                or incoming.source_player_id in request.forbidden_player_ids
                or incoming.position is not outgoing.position
                or rest_cost + incoming.price_tenths > request.budget_tenths
            ):
                continue
            cap = caps.get(incoming.team_short_name, rules.squad.max_players_per_team)
            if rest_clubs.get(incoming.team_short_name, 0) + 1 > cap:
                continue
            moves.append(
                tuple(
                    sorted(
                        [player.source_player_id for player in rest]
                        + [incoming.source_player_id]
                    )
                )
            )
    return moves


def seed_squad(
    candidates: tuple[CandidatePlayer, ...],
    request: SquadLabRequest,
    rules: SeasonRules,
) -> tuple[str, ...]:
    """A legal starting squad from the solver, honouring every constraint."""

    group_constraints = tuple(
        SquadGroupConstraint(
            name=f"club-{limit.team_short_name}",
            player_ids=frozenset(
                player.source_player_id
                for player in candidates
                if player.team_short_name == limit.team_short_name
            ),
            maximum=limit.maximum,
        )
        for limit in request.club_limits
    )
    # Enumerating every goalkeeper pair costs about eighty seconds on a full
    # pool and buys nothing here: this is only a legal place to start, and the
    # climb re-values every squad with pairs priced properly. Skipping it takes
    # the seed to roughly four seconds, which is what lets the caller's time
    # budget govern the search rather than be spent before it begins.
    result = optimise_full_squad(
        candidates,
        budget_tenths=request.budget_tenths,
        rules=rules,
        required_player_ids=request.required_player_ids,
        forbidden_player_ids=request.forbidden_player_ids,
        group_constraints=group_constraints,
        goalkeeper_pair_valuation=False,
    )
    return tuple(sorted(player.source_player_id for player in result.players))


def _kick(
    player_ids: tuple[str, ...],
    pool: Mapping[str, CandidatePlayer],
    request: SquadLabRequest,
    rules: SeasonRules,
    caps: Mapping[str, int],
    rng: random.Random,
) -> tuple[str, ...] | None:
    """Random multi-swap out of the current basin, still legal."""

    movable = [
        player_id
        for player_id in player_ids
        if player_id not in request.required_player_ids
    ]
    if not movable:
        return None
    for _ in range(400):
        current = list(player_ids)
        for _ in range(request.kick_size):
            leaving = rng.choice(movable)
            if leaving not in current:
                continue
            outgoing = pool[leaving]
            options = [
                player
                for player in pool.values()
                if player.position is outgoing.position
                and player.source_player_id not in current
                and player.source_player_id not in request.forbidden_player_ids
            ]
            if not options:
                continue
            current[current.index(leaving)] = rng.choice(options).source_player_id
        candidate = tuple(sorted(current))
        if _admissible([pool[i] for i in candidate], request, rules, caps):
            return candidate
    return None


def _executor(
    pools: Mapping[str, tuple[CandidatePlayer, ...]],
    rules: SeasonRules,
    workers: int,
) -> tuple[Executor, bool]:
    """A process pool where one can be started, threads where it cannot.

    Most of the cost is Python-level enumeration rather than the solver
    subprocess, so processes are markedly faster and are always tried first.
    But Streamlit runs the app as a main script, and a spawned child re-imports
    it; where that is refused, a thread pool still produces the right answer.
    """

    try:
        pool = ProcessPoolExecutor(
            max_workers=max(1, workers),
            initializer=_initialise,
            initargs=(dict(pools), rules),
        )
        # Force a worker to start now, so an unusable pool fails here rather
        # than part-way through a long search.
        list(pool.map(_ping, [0]))
        return pool, True
    except (RuntimeError, OSError, BrokenProcessPool, ImportError):
        _initialise(pools, rules)
        return ThreadPoolExecutor(max_workers=max(1, workers)), False


def _ping(value: int) -> int:
    return value


def run_squad_lab_search(
    pools: Mapping[str, tuple[CandidatePlayer, ...]],
    rules: SeasonRules,
    request: SquadLabRequest,
    *,
    workers: int = 8,
    progress: Callable[[str, float], None] | None = None,
) -> SquadLabResult:
    """Climb, kick, and climb again until the time budget runs out.

    Returns the best squad found whether or not the budget was exhausted, so
    a short search yields a coarse answer rather than no answer.
    """

    if not pools:
        raise ValueError("At least one projection pool is required")
    labels = sorted(pools)
    reference = pools[labels[0]]
    reference_ids = {player.source_player_id for player in reference}
    for label in labels[1:]:
        if {player.source_player_id for player in pools[label]} != reference_ids:
            raise ValueError(
                "Every pool must cover the same players, or the worst case "
                "compares different squads"
            )
    pool = {player.source_player_id: player for player in reference}
    missing = (request.required_player_ids | request.forbidden_player_ids) - set(pool)
    if missing:
        raise ValueError(f"Unknown players: {', '.join(sorted(missing))}")

    caps = {limit.team_short_name: limit.maximum for limit in request.club_limits}
    rng = random.Random(request.seed)
    started = time.monotonic()

    def announce(message: str) -> None:
        if progress is not None:
            elapsed = time.monotonic() - started
            fraction = (
                0.0
                if request.time_budget_seconds is None
                else min(1.0, elapsed / request.time_budget_seconds)
            )
            progress(message, fraction)

    def remaining() -> float:
        if request.time_budget_seconds is None:
            return float("inf")
        return request.time_budget_seconds - (time.monotonic() - started)

    announce("Building a legal starting squad")
    start = seed_squad(reference, request, rules)

    climbs: list[dict[str, Any]] = []
    evaluations = 0
    best_ids = start
    best_value = float("-inf")
    best_per_model: dict[str, float] = {}
    exhausted = False

    executor, parallel = _executor(pools, rules, workers)
    if not parallel:
        announce("Process pool unavailable; running with threads")
    with executor:
        try:
            _, best_value, best_per_model = list(executor.map(_score, [start]))[0]
            evaluations += 1

            for attempt in range(request.kicks + 1):
                if remaining() <= 0:
                    exhausted = True
                    break
                if attempt == 0:
                    current, current_value = best_ids, best_value
                else:
                    kicked = _kick(best_ids, pool, request, rules, caps, rng)
                    if kicked is None:
                        break
                    _, current_value, _ = list(executor.map(_score, [kicked]))[0]
                    current = kicked
                    evaluations += 1
                rounds = 0
                while True:
                    if remaining() <= 0:
                        exhausted = True
                        break
                    moves = _neighbours(current, pool, request, rules, caps)
                    if not moves:
                        break
                    # A full sweep of a 570-player pool is thousands of exact
                    # valuations and can run for many minutes, so the budget is
                    # checked batch by batch rather than only between rounds.
                    # Stopping part-way is still a sound move: the best
                    # improvement found so far is a real improvement.
                    ids, value, per_model = current, current_value, {}
                    batch = max(8, len(moves) // 20)
                    for offset in range(0, len(moves), batch):
                        if remaining() <= 0:
                            exhausted = True
                            break
                        announce(
                            f"Climb {attempt + 1}, round {rounds + 1}: "
                            f"{min(offset + batch, len(moves))} of {len(moves)} "
                            "neighbours"
                        )
                        chunk = moves[offset : offset + batch]
                        scored = list(executor.map(_score, chunk, chunksize=4))
                        evaluations += len(scored)
                        best_in_chunk = max(scored, key=lambda row: row[1])
                        if best_in_chunk[1] > value:
                            ids, value, per_model = best_in_chunk
                    if value <= current_value + 1e-9:
                        break
                    current, current_value = ids, value
                    rounds += 1
                    if value > best_value and per_model:
                        best_ids, best_value, best_per_model = ids, value, per_model
                    if exhausted:
                        break
                climbs.append(
                    {
                        "climb": attempt + 1,
                        "kind": "seed" if attempt == 0 else "kick",
                        "value": round(current_value, 3),
                        "rounds": rounds,
                    }
                )
                if exhausted:
                    break
        except BrokenProcessPool:
            return SquadLabResult(
                player_ids=best_ids,
                objective=best_value,
                per_model=best_per_model,
                climbs=climbs,
                evaluations=evaluations,
                seconds=time.monotonic() - started,
                note=(
                    "A worker process died, so the search stopped early. The "
                    "squad below is the best found before that happened."
                ),
            )

    note = ""
    if exhausted:
        note = (
            "The time budget ran out mid-search. This is the best squad found "
            "so far, not a converged answer - raise the budget to keep going."
        )
    elif climbs and all(entry["rounds"] == 0 for entry in climbs):
        note = (
            "No single swap improved any starting squad, so this is a local "
            "optimum under these constraints."
        )
    return SquadLabResult(
        player_ids=best_ids,
        objective=best_value,
        per_model=best_per_model,
        climbs=climbs,
        evaluations=evaluations,
        seconds=time.monotonic() - started,
        exhausted_budget=exhausted,
        note=note,
    )


# --------------------------------------------------------------------------
# Shortlist
# --------------------------------------------------------------------------
#: Squads worth keeping while experimenting. Written to disk so a shortlist
#: survives an app restart, which is the whole point of recording one.
SHORTLIST_FILENAME = "squad-lab-shortlist-{season_code}.json"


def shortlist_path(season_code: str, directory: str | Path = "data/models") -> Path:
    return Path(directory) / SHORTLIST_FILENAME.format(season_code=season_code)


def load_shortlist(
    season_code: str, directory: str | Path = "data/models"
) -> list[dict[str, Any]]:
    """Every saved squad, oldest first. A missing or unreadable file is empty."""

    path = shortlist_path(season_code, directory)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return entries if isinstance(entries, list) else []


def save_to_shortlist(
    season_code: str,
    name: str,
    result: SquadLabResult,
    *,
    constraints: dict[str, Any],
    projection_run_id: int | None = None,
    directory: str | Path = "data/models",
) -> list[dict[str, Any]]:
    """Append one squad, replacing any earlier entry with the same name.

    The constraints are stored alongside the squad because a value is not
    comparable without them: a squad forced to include a player is answering a
    different question from an unconstrained one.
    """

    label = name.strip()
    if not label:
        raise ValueError("A shortlist entry needs a name")
    entries = [
        entry for entry in load_shortlist(season_code, directory)
        if entry.get("name") != label
    ]
    entries.append(
        {
            "name": label,
            "saved_at": datetime.now(UTC).isoformat(),
            "objective": round(result.objective, 3),
            "per_model": {k: round(v, 3) for k, v in result.per_model.items()},
            "player_ids": list(result.player_ids),
            "constraints": constraints,
            "projection_run_id": projection_run_id,
            "evaluations": result.evaluations,
            "seconds": round(result.seconds, 1),
            "converged": not result.exhausted_budget,
        }
    )
    path = shortlist_path(season_code, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"season_code": season_code, "entries": entries}, indent=1) + "\n",
        encoding="utf-8",
    )
    return entries


def remove_from_shortlist(
    season_code: str, name: str, directory: str | Path = "data/models"
) -> list[dict[str, Any]]:
    entries = [
        entry for entry in load_shortlist(season_code, directory)
        if entry.get("name") != name
    ]
    path = shortlist_path(season_code, directory)
    path.write_text(
        json.dumps({"season_code": season_code, "entries": entries}, indent=1) + "\n",
        encoding="utf-8",
    )
    return entries


def compare_to(
    entry_ids: Sequence[str], baseline_ids: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Which players a squad drops from, and adds to, a baseline."""

    entry, baseline = set(entry_ids), set(baseline_ids)
    return sorted(baseline - entry), sorted(entry - baseline)
