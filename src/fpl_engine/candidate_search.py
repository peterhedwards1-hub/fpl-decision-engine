"""Why the opening-squad frontier was blind, and what replaces it.

The previous frontier generated forty distinct complete squads and every one of
them fielded the same eleven in all eight Gameweeks. That is not bad luck. It
follows directly from the objective CBC is given.

The linear objective is, per Gameweek, the expected points of a legal eleven
plus its captain. **Bench players appear in it nowhere.** So every squad that
shares a weekly XI and completes itself with any affordable legal set of four
reserves has *exactly* the same objective value — in the 2026/27 run, all forty
scored 429.962 to the last decimal. The frontier excluded complete fifteens and
therefore walked that tie set, swapping £4.0m defenders for other £4.0m
defenders, none of whom ever started. Reaching a genuinely different structure
would have meant exhausting thousands of interchangeable completions first.

Meanwhile the exact value the squads are finally ranked by *does* price the
bench: autosub activation, bench order, the vice-captain fallback and
goalkeeper-pair orientation are all in it and none are in the linear objective.
The gap between the two is large and, crucially, it *varies* between squads —
20.9 points for one, 25.7 for another, 31.1 for a third. A squad that gives up
five linear points of XI quality to buy a much stronger bench wins on exact
value and is unreachable by exclusion, because it is not in the tie set at all.

That is the whole failure, and it explains every symptom: one XI across forty
candidates, a linear ranking that was pure tie-break noise, and an exact winner
that only turned up because a forced-inclusion diagnostic happened to solve a
differently constrained problem.

The replacement generates candidates from several declared families rather than
one. The family that matters most is the slack band: pin the linear objective
at ``optimum - delta`` and then maximise reserve quality inside the band, which
aims the search at precisely the value the linear objective cannot see. The
other families — distinct starting elevens, forced inclusions and exclusions,
structural constraints, tiny perturbations — cover structures a single
objective would not reach whatever its slack.

Every family only *generates*. Nothing here touches the exact decision value,
and every candidate, however it was produced, is rescored by the same function
and ranked on that number alone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from math import ceil
from typing import Any

from .config import SeasonRules
from .domain import Position
from .optimisation import (
    CandidatePlayer,
    FullSquadResult,
    OptimisationError,
    SquadGroupConstraint,
    SquadSpendConstraint,
    enumerate_squad_ids,
    optimise_full_squad,
    squad_ranking_key,
)

#: The declared slack bands, in linear points below the proven optimum.
#:
#: The first five are the declared set. The last two are there for a stated
#: reason rather than to be generous. Exact-minus-linear uplift is not a
#: constant: across the 2026/27 pool it ranges over about thirteen points. A
#: squad that far below the linear optimum can therefore still win on exact
#: value, so a band set whose widest member is narrower than the measured
#: uplift spread cannot reach the exact optimum however many candidates it
#: draws — it is looking in the wrong place by construction. The realised
#: spread and whether the widest band still dominates it are both reported, so
#: this stays a checked claim rather than a fixed assumption.
LINEAR_SLACK_BANDS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

#: Declared deterministic perturbation vectors, in linear points per player.
#: Small enough that they cannot reorder genuinely different structures,
#: large enough to shake apart exact ties. Removed before anything is scored.
PERTURBATION_SCALE = 0.002
PERTURBATION_COUNT = 6

#: Practical convergence: this many successive expansions with no new winner
#: and no more than this much exact improvement.
CONVERGENCE_STAGES_REQUIRED = 2
CONVERGENCE_IMPROVEMENT_TOLERANCE = 0.05

#: The declared convergence stages, as fractions of *every* candidate family.
#:
#: Stage ``f`` contains the first ``ceil(f * n)`` candidates of each family in
#: that family's own generation order, so the stages are genuinely nested and
#: each one expands every family. The final stage is the whole pool. This
#: replaces absolute prefix sizes taken in global generation order: because the
#: families are generated one after another and the first families reproduce the
#: incumbent frontier, an absolute prefix small enough to test would be filled
#: entirely by those families while the slack-band family that actually finds
#: the exact winner sat beyond the prefix — so convergence could be declared on
#: a pool that did not contain the winning squad.
DEFAULT_CONVERGENCE_STAGES: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)


@dataclass(frozen=True)
class GenerationRequest:
    """One declared way of producing candidates."""

    family: str
    name: str
    count: int
    objective: str = "primary"
    linear_slack: float | None = None
    exclude_starting_xis: bool = False
    required_player_ids: frozenset[str] = frozenset()
    forbidden_player_ids: frozenset[str] = frozenset()
    group_constraints: tuple[SquadGroupConstraint, ...] = ()
    spend_constraints: tuple[SquadSpendConstraint, ...] = ()
    budget_tenths: int | None = None
    perturbation: dict[str, float] | None = None


@dataclass
class RawCandidate:
    """A squad membership and every declared route that produced it."""

    squad_ids: frozenset[str]
    first_source: str
    first_family: str
    order: int
    sources: list[str] = field(default_factory=list)
    families: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate with the numbers it is actually judged on."""

    squad_ids: frozenset[str]
    result: FullSquadResult
    first_source: str
    first_family: str
    sources: tuple[str, ...]
    families: tuple[str, ...]
    order: int

    @property
    def exact_value(self) -> float:
        return self.result.decision_value

    @property
    def linear_objective(self) -> float:
        return self.result.solver_objective

    @property
    def uplift(self) -> float:
        """Exact decision value above the solver's own linear objective.

        The slack bands are declared in the solver objective's own units — the
        solver maximises ``solver_objective`` and the exact value exceeds it by
        this much — so this is the quantity that says whether the band set is
        wide enough to reach the exact optimum. Comparing the exact value to
        the lineup-expected-points instead (see :attr:`exact_minus_lineup`)
        answers a different, narrower question.
        """
        return round(
            self.result.decision_value - self.result.solver_objective, 3
        )

    @property
    def exact_minus_lineup(self) -> float:
        """Exact decision value above the legal-XI-plus-captain points.

        Retained as a separate diagnostic from :attr:`uplift`: it isolates the
        autosub, bench-order and captain-fallback value beyond the eleven that
        actually started, whereas ``uplift`` measures the gap to the whole
        linear objective the solver optimised (which also carries the
        goalkeeper-pair and terminal terms).
        """
        return round(
            self.result.decision_value - self.result.lineup_expected_points, 3
        )


# --------------------------------------------------------------------------
# The declared families
# --------------------------------------------------------------------------


def _price_group(
    candidates: tuple[CandidatePlayer, ...],
    *,
    name: str,
    position: Position | None = None,
    minimum_price: int | None = None,
    maximum_price: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> SquadGroupConstraint:
    ids = frozenset(
        player.source_player_id
        for player in candidates
        if (position is None or player.position == position)
        and (minimum_price is None or player.price_tenths >= minimum_price)
        and (maximum_price is None or player.price_tenths <= maximum_price)
    )
    return SquadGroupConstraint(
        name=name, player_ids=ids, minimum=minimum, maximum=maximum
    )


def declared_requests(
    candidates: tuple[CandidatePlayer, ...],
    *,
    budget_tenths: int,
    incumbent_winner: frozenset[str] = frozenset(),
    linear_leaders: tuple[frozenset[str], ...] = (),
    scale: float = 1.0,
    minimum_exclusion_candidates: int = 8,
) -> list[GenerationRequest]:
    """Every declared generation route, in the order the search runs them.

    The order is itself declared, and it is what makes the convergence stages
    meaningful: family A alone reproduces the incumbent frontier, so the first
    stage is a like-for-like reproduction of the old behaviour and each later
    stage adds a family. A reader can see exactly which family bought which
    improvement.
    """

    def sized(value: int) -> int:
        return max(1, int(round(value * scale)))

    by_id = {player.source_player_id: player for player in candidates}
    requests: list[GenerationRequest] = []

    # A. Distinct complete squads. The incumbent behaviour, kept so the new
    # search provably contains everything the old one could find.
    # ``minimum_exclusion_candidates`` is the backwards-compatibility floor.
    # Families A and B *are* the old searches, and they are also the cheapest
    # to run, so they are never scaled below the width the caller asked for.
    # Without that floor a reduced-scale pool can fail to contain a squad the
    # incumbent frontier would have found, which is the one thing the new
    # search must not be able to do.
    exclusion_count = max(sized(50), minimum_exclusion_candidates)
    requests.append(
        GenerationRequest(
            family="A_complete_squads",
            name="complete_squad_exclusion",
            count=exclusion_count,
        )
    )

    # B. Distinct starting elevens. Excluding the XI as well as the fifteen is
    # what stops the search walking bench permutations of one lineup.
    requests.append(
        GenerationRequest(
            family="B_distinct_xis",
            name="distinct_starting_xi",
            count=exclusion_count,
            exclude_starting_xis=True,
        )
    )

    # C. Linear slack bands with a reserve objective. The family that reaches
    # the structures the linear objective is blind to.
    for band in LINEAR_SLACK_BANDS:
        requests.append(
            GenerationRequest(
                family="C_slack_bands",
                name=f"slack_{band:g}_reserve",
                count=sized(8),
                objective="reserve",
                linear_slack=band,
            )
        )
        requests.append(
            GenerationRequest(
                family="C_slack_bands",
                name=f"slack_{band:g}_reserve_distinct_xi",
                count=sized(4),
                objective="reserve",
                linear_slack=band,
                exclude_starting_xis=True,
            )
        )

    # D. Forced in and forced out. Systematic, so that a diagnostic can no
    # longer find something the ordinary pool missed — the diagnostics are the
    # pool.
    arsenal_defenders = sorted(
        player.source_player_id
        for player in candidates
        if player.team_short_name == "ARS" and player.position == Position.DEF
    )
    for player_id in arsenal_defenders:
        requests.append(
            GenerationRequest(
                family="D_forced",
                name=f"force_in_arsenal_{by_id[player_id].web_name}",
                count=sized(2),
                required_player_ids=frozenset({player_id}),
            )
        )
    for position in (Position.GK, Position.DEF, Position.MID, Position.FWD):
        ranked = sorted(
            (
                player
                for player in candidates
                if player.position == position
                and player.source_player_id not in incumbent_winner
            ),
            key=lambda player: -player.expected_points,
        )[: sized(10)]
        for player in ranked:
            requests.append(
                GenerationRequest(
                    family="D_forced",
                    name=f"force_in_{position.value}_{player.web_name}",
                    count=1,
                    required_player_ids=frozenset({player.source_player_id}),
                )
            )
    leaders = frozenset().union(*linear_leaders) if linear_leaders else frozenset()
    for player_id in sorted(leaders - incumbent_winner):
        if player_id not in by_id:
            continue
        requests.append(
            GenerationRequest(
                family="D_forced",
                name=f"force_in_linear_leader_{by_id[player_id].web_name}",
                count=1,
                required_player_ids=frozenset({player_id}),
            )
        )
    # Forced-out runs. At full scale every player in the incumbent squad gets
    # one, which is what makes "why not him?" answerable for all fifteen. At
    # reduced scale the sample is taken deterministically by identifier so a
    # rerun asks the same questions.
    forced_out = sorted(player_id for player_id in incumbent_winner if player_id in by_id)
    for player_id in forced_out[: sized(len(forced_out))] if forced_out else ():
        requests.append(
            GenerationRequest(
                family="D_forced",
                name=f"force_out_{by_id[player_id].web_name}",
                count=1,
                forbidden_player_ids=frozenset({player_id}),
            )
        )

    # E. Structural families. Each is a shape a manager might want for reasons
    # a projection cannot see; none of them alters how a squad is scored.
    structural: list[tuple[str, dict[str, Any]]] = [
        ("bank_at_least_0.5m", {"budget_tenths": budget_tenths - 5}),
        ("bank_at_least_1.0m", {"budget_tenths": budget_tenths - 10}),
        (
            "at_least_one_defender_6.0m_or_above",
            {
                "group_constraints": (
                    _price_group(
                        candidates,
                        name="premium_defender",
                        position=Position.DEF,
                        minimum_price=60,
                        minimum=1,
                    ),
                )
            },
        ),
        (
            "no_defender_above_5.5m",
            {
                "group_constraints": (
                    _price_group(
                        candidates,
                        name="expensive_defender",
                        position=Position.DEF,
                        minimum_price=60,
                        maximum=0,
                    ),
                )
            },
        ),
        (
            "defensive_spend_at_most_22.5m",
            {
                "spend_constraints": (
                    SquadSpendConstraint(
                        name="defence",
                        player_ids=frozenset(
                            player.source_player_id
                            for player in candidates
                            if player.position in (Position.GK, Position.DEF)
                        ),
                        maximum_tenths=225,
                    ),
                )
            },
        ),
        (
            "defensive_spend_at_least_30.0m",
            {
                "spend_constraints": (
                    SquadSpendConstraint(
                        name="defence",
                        player_ids=frozenset(
                            player.source_player_id
                            for player in candidates
                            if player.position in (Position.GK, Position.DEF)
                        ),
                        minimum_tenths=300,
                    ),
                )
            },
        ),
        (
            "exactly_one_premium_midfielder",
            {
                "group_constraints": (
                    _price_group(
                        candidates,
                        name="premium_midfielder",
                        position=Position.MID,
                        minimum_price=100,
                        minimum=1,
                        maximum=1,
                    ),
                )
            },
        ),
        (
            "two_premium_midfielders",
            {
                "group_constraints": (
                    _price_group(
                        candidates,
                        name="premium_midfielder",
                        position=Position.MID,
                        minimum_price=100,
                        minimum=2,
                    ),
                )
            },
        ),
        (
            "no_triple_up_from_any_club",
            {
                "group_constraints": tuple(
                    SquadGroupConstraint(
                        name=f"club_{club}",
                        player_ids=frozenset(
                            player.source_player_id
                            for player in candidates
                            if player.team_short_name == club
                        ),
                        maximum=2,
                    )
                    for club in sorted(
                        {player.team_short_name for player in candidates}
                    )
                )
            },
        ),
    ]
    for name, options in structural:
        requests.append(
            GenerationRequest(
                family="E_structural",
                name=name,
                count=sized(4),
                **options,
            )
        )
    # Slack bands are the family that matters most, so at reduced scale the
    # widest bands are kept in preference to the narrowest: a narrow band can
    # only reach squads a wider one also reaches.
    if scale < 1.0:
        kept = max(2, sized(len(LINEAR_SLACK_BANDS)))
        widest = set(sorted(LINEAR_SLACK_BANDS)[-kept:])
        requests = [
            request
            for request in requests
            if request.family != "C_slack_bands"
            or request.linear_slack in widest
        ]

    # F. Tiny deterministic perturbations, to shake apart exact ties. Declared
    # by index rather than drawn, so a rerun reproduces them exactly.
    ordered_ids = sorted(by_id)
    for index in range(sized(PERTURBATION_COUNT)):
        perturbation = {
            player_id: PERTURBATION_SCALE
            * (((position * (index + 1)) % 7) - 3)
            / 3.0
            for position, player_id in enumerate(ordered_ids)
        }
        requests.append(
            GenerationRequest(
                family="F_perturbations",
                name=f"perturbation_{index}",
                count=sized(4),
                perturbation=perturbation,
            )
        )
    return requests


# --------------------------------------------------------------------------
# Generation, deduplication and scoring
# --------------------------------------------------------------------------


def generate_pool(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    budget_tenths: int,
    requests: list[GenerationRequest],
    target_unique: int | None = None,
    seed_candidates: tuple[tuple[frozenset[str], str], ...] = (),
) -> tuple[list[RawCandidate], dict[str, Any]]:
    """Run every declared request, deduplicating by full fifteen as it goes.

    Deduplication happens before any exact rescoring, because rescoring is the
    expensive half and two families very often produce the same squad. Which
    families produced it is kept, because the overlap is the interesting part:
    a squad only one family can reach is a squad the old search could not.
    """

    seen: dict[frozenset[str], RawCandidate] = {}
    order: list[RawCandidate] = []
    # Squads already produced elsewhere — by a comparison run's reproduction of
    # an older search, say — enter the pool first, so containment of the old
    # behaviour is a fact about the pool rather than a hope about the families.
    for squad_ids, source in seed_candidates:
        if squad_ids in seen:
            continue
        candidate = RawCandidate(
            squad_ids=squad_ids,
            first_source=source,
            first_family="A0_seeded",
            order=len(order),
            sources=[source],
            families={"A0_seeded"},
        )
        seen[squad_ids] = candidate
        order.append(candidate)
    per_request: list[dict[str, Any]] = []
    raw_total = 0
    started = time.monotonic()
    for request in requests:
        request_started = time.monotonic()
        try:
            produced = enumerate_squad_ids(
                candidates,
                budget_tenths=(
                    budget_tenths
                    if request.budget_tenths is None
                    else request.budget_tenths
                ),
                rules=rules,
                count=request.count,
                objective=request.objective,
                linear_slack=request.linear_slack,
                exclude_starting_xis=request.exclude_starting_xis,
                perturbation=request.perturbation,
                required_player_ids=request.required_player_ids,
                forbidden_player_ids=request.forbidden_player_ids,
                group_constraints=request.group_constraints,
                spend_constraints=request.spend_constraints,
            )
        except OptimisationError as error:
            per_request.append(
                {
                    "family": request.family,
                    "name": request.name,
                    "requested": request.count,
                    "produced": 0,
                    "new": 0,
                    "infeasible": True,
                    "reason": str(error),
                    "runtime_seconds": round(time.monotonic() - request_started, 2),
                }
            )
            continue
        new = 0
        for squad_ids in produced:
            raw_total += 1
            existing = seen.get(squad_ids)
            if existing is None:
                candidate = RawCandidate(
                    squad_ids=squad_ids,
                    first_source=request.name,
                    first_family=request.family,
                    order=len(order),
                    sources=[request.name],
                    families={request.family},
                )
                seen[squad_ids] = candidate
                order.append(candidate)
                new += 1
            else:
                existing.sources.append(request.name)
                existing.families.add(request.family)
        per_request.append(
            {
                "family": request.family,
                "name": request.name,
                "requested": request.count,
                "produced": len(produced),
                "new": new,
                "infeasible": False,
                "runtime_seconds": round(time.monotonic() - request_started, 2),
            }
        )
        if target_unique is not None and len(order) >= target_unique:
            break
    diagnostics = {
        "raw_candidates": raw_total,
        "unique_squads": len(order),
        "requests": per_request,
        "generation_runtime_seconds": round(time.monotonic() - started, 2),
    }
    return order, diagnostics


def rescore_pool(
    pool: list[RawCandidate],
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
) -> list[ScoredCandidate]:
    """Score every unique squad with the one exact objective, unchanged.

    Each squad is re-solved over its own fifteen players at its own cost, which
    is the same computation the full optimiser performs, so a candidate's exact
    value does not depend on how it was generated.
    """

    by_id = {player.source_player_id: player for player in candidates}
    scored: list[ScoredCandidate] = []
    for entry in pool:
        held = tuple(by_id[player_id] for player_id in sorted(entry.squad_ids))
        result = optimise_full_squad(
            held,
            budget_tenths=sum(player.price_tenths for player in held),
            rules=rules,
        )
        scored.append(
            ScoredCandidate(
                squad_ids=entry.squad_ids,
                result=result,
                first_source=entry.first_source,
                first_family=entry.first_family,
                sources=tuple(sorted(set(entry.sources))),
                families=tuple(sorted(entry.families)),
                order=entry.order,
            )
        )
    return scored


def rank_pool(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Rank on the existing exact decision value and nothing else."""

    return sorted(scored, key=lambda entry: squad_ranking_key(entry.result))


def pool_report(scored: list[ScoredCandidate], diagnostics: dict[str, Any]) -> dict[str, Any]:
    """Everything asked of the combined pool, in one document."""

    ranked = rank_pool(scored)
    linear_order = sorted(
        scored,
        key=lambda entry: (
            -entry.linear_objective,
            tuple(sorted(entry.squad_ids)),
        ),
    )
    linear_rank = {
        id(entry): index + 1 for index, entry in enumerate(linear_order)
    }
    by_family: dict[str, int] = {}
    first_family: dict[str, int] = {}
    for entry in scored:
        first_family[entry.first_family] = first_family.get(entry.first_family, 0) + 1
        for family in entry.families:
            by_family[family] = by_family.get(family, 0) + 1
    overlap: dict[str, int] = {}
    for entry in scored:
        if len(entry.families) > 1:
            key = "+".join(entry.families)
            overlap[key] = overlap.get(key, 0) + 1
    uplifts = [entry.uplift for entry in scored]
    return {
        **diagnostics,
        "unique_complete_squads": len(scored),
        "distinct_starting_xis": len(
            {entry.result.starting_player_ids for entry in scored}
        ),
        "distinct_goalkeeper_pairs": len(
            {tuple(sorted(entry.result.goalkeeper_pair)) for entry in scored}
        ),
        "candidates_by_family": dict(sorted(by_family.items())),
        "candidates_first_found_by_family": dict(sorted(first_family.items())),
        "family_overlap": dict(sorted(overlap.items())),
        "uplift_minimum": round(min(uplifts), 3) if uplifts else None,
        "uplift_maximum": round(max(uplifts), 3) if uplifts else None,
        "uplift_spread": (
            round(max(uplifts) - min(uplifts), 3) if uplifts else None
        ),
        "widest_slack_band": max(LINEAR_SLACK_BANDS),
        "slack_band_covers_uplift_spread": (
            bool(uplifts and max(LINEAR_SLACK_BANDS) >= max(uplifts) - min(uplifts))
        ),
        "candidates": [
            {
                "exact_rank": index + 1,
                "linear_rank": linear_rank[id(entry)],
                "exact_decision_value": entry.exact_value,
                "linear_objective": entry.linear_objective,
                "lineup_expected_points": entry.result.lineup_expected_points,
                "exact_minus_linear_uplift": entry.uplift,
                "exact_minus_lineup": entry.exact_minus_lineup,
                "total_cost_tenths": entry.result.total_cost_tenths,
                "generation_source": entry.first_source,
                "generation_family": entry.first_family,
                "all_sources": list(entry.sources),
                "all_families": list(entry.families),
                "generation_order": entry.order,
                "player_ids": sorted(entry.squad_ids),
            }
            for index, entry in enumerate(ranked)
        ],
    }


# --------------------------------------------------------------------------
# Convergence
# --------------------------------------------------------------------------


def convergence_report(
    scored: list[ScoredCandidate],
    *,
    stages: tuple[float, ...] = DEFAULT_CONVERGENCE_STAGES,
    tolerance: float = CONVERGENCE_IMPROVEMENT_TOLERANCE,
    stages_required: int = CONVERGENCE_STAGES_REQUIRED,
) -> dict[str, Any]:
    """Best exact value against a *balanced* expansion of every family.

    ``stages`` are fractions in ``(0, 1]``. Stage ``f`` contains the first
    ``ceil(f * n)`` candidates of each family, taken in that family's own
    generation order, so the stages are genuinely nested and each one expands
    *every* family rather than adding whole families one at a time. The final
    stage is the entire pool.

    This is a deliberate replacement for prefixes taken in global generation
    order. The families run in a fixed order and the first ones reproduce the
    incumbent frontier, so a prefix small enough to test would be filled by
    those families while the slack-band family that finds the exact winner sat
    beyond it — convergence could then be declared on a pool that never
    contained the winning squad. Because the last stage here is the whole pool,
    the winning squad is always inside it, and "no new winner across the final
    expansions" means what it says.
    """

    by_family: dict[str, list[ScoredCandidate]] = {}
    for entry in scored:
        by_family.setdefault(entry.first_family, []).append(entry)
    for entries in by_family.values():
        entries.sort(key=lambda entry: entry.order)

    rows: list[dict[str, Any]] = []
    previous_best: float | None = None
    previous_winner: frozenset[str] | None = None
    quiet_stages = 0
    for fraction in stages:
        prefix: list[ScoredCandidate] = []
        for entries in by_family.values():
            take = ceil(fraction * len(entries)) if entries else 0
            prefix.extend(entries[:take])
        if not prefix:
            continue
        best = min(prefix, key=lambda entry: squad_ranking_key(entry.result))
        improvement = (
            None if previous_best is None else round(best.exact_value - previous_best, 3)
        )
        changed = previous_winner is not None and best.squad_ids != previous_winner
        if previous_best is not None:
            if not changed and (improvement or 0.0) <= tolerance:
                quiet_stages += 1
            else:
                quiet_stages = 0
        rows.append(
            {
                "stage_fraction": fraction,
                "families_expanded": len(by_family),
                "actual_pool_size": len(prefix),
                "best_exact_value": best.exact_value,
                "winner_changed": changed,
                "improvement_over_previous_stage": improvement,
                "distinct_starting_xis": len(
                    {entry.result.starting_player_ids for entry in prefix}
                ),
                "winning_generation_source": best.first_source,
                "winning_generation_family": best.first_family,
                "winner_player_ids": sorted(best.squad_ids),
            }
        )
        previous_best = best.exact_value
        previous_winner = best.squad_ids
    converged = quiet_stages >= stages_required
    return {
        "stages": rows,
        "stage_fractions": list(stages),
        "tolerance": tolerance,
        "stages_required": stages_required,
        "quiet_stages": quiet_stages,
        "converged": converged,
        "verdict": (
            "Practical convergence reached: the final expansions of every "
            "family produced no new winning squad and no material improvement."
            if converged
            else (
                "The search has NOT converged by the declared criterion. The "
                "best squad below is the best found, not the best that exists."
            )
        ),
        "note": (
            "Balanced staged convergence: each stage expands every candidate "
            "family and the final stage is the whole pool, so the winning "
            "squad is always inside the last stage. Practical convergence "
            "only — no claim of global nonlinear optimality is made or "
            "implied: the exact objective is not the one the solver optimises, "
            "so no solver proof covers it."
        ),
    }
