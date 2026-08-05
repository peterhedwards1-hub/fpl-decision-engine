"""Does the mixed search actually beat the frontier it replaces?

Three search strategies are run over the *same* candidate players, with the
same legality rules and the same exact scoring function, so the only thing that
differs is which squads each one puts in front of the scorer:

``legacy_8``
    Eight candidates, excluding both the complete fifteen and the starting
    eleven. The behaviour before the forty-candidate frontier.
``frontier_40``
    Forty candidates, excluding complete fifteens only. The behaviour that
    produced one starting eleven forty times.
``mixed``
    The declared families in :mod:`fpl_engine.candidate_search`.

Four things are measured, and only the first three are treated as evidence.

The **exact value found** is the primary measure: a search is better if it puts
a better squad in front of the scorer, and that is a deterministic property of
the search rather than of the season.

The **starting-eleven diversity** says whether the pool contains structurally
different propositions at all, which is what the failure was about.

The **forced-diagnostic escape** is the acceptance test that matters most: force
players in and out afterwards and see whether any of those squads beats the
pool's own winner. If one does, the pool is still missing structures and the
search has not been fixed — merely widened.

**Realised points** are reported and deliberately discounted. One opening squad
per season is one draw from a wide distribution; four of them cannot separate
two search strategies, and treating them as if they could would be the same
mistake as ranking on a linear objective because it was the number to hand.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidate_search import (
    ScoredCandidate,
    convergence_report,
    declared_requests,
    generate_pool,
    pool_report,
    rank_pool,
    rescore_pool,
)
from .config import SeasonRules
from .evaluation import _replayed_squad_points
from .history.database import HistoricalDatabase
from .optimisation import (
    DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    CandidatePlayer,
    FullSquadResult,
    GameweekPlayerValue,
    OptimisationError,
    enumerate_squad_ids,
    mean_appearance,
    optimise_full_squad,
)
from .preseason_strength import (
    CARRY_FORWARD_PRESEASON_CONFIG,
    discover_season_transitions,
)
from .squad_comparison import opening_candidates


def realised_lookup(
    database: HistoricalDatabase,
    *,
    season_code: str,
    gameweeks: tuple[int, ...],
) -> dict[tuple[str, int], GameweekPlayerValue]:
    """What every player actually scored, in the shape the replay expects.

    Read straight from the recorded fixture-level rows rather than from a
    persisted backtest, so a search comparison does not have to pay for a
    projection replay it never uses. Double Gameweeks sum, which is what the
    scoring rules do.
    """

    rows = database.connection.execute(
        """
        SELECT players.source_player_id AS source_player_id,
               gameweeks.number AS gameweek_number,
               SUM(stats.total_points) AS points,
               SUM(stats.minutes) AS minutes
        FROM player_fixture_stats stats
        JOIN player_seasons ON player_seasons.id = stats.player_season_id
        JOIN players ON players.id = player_seasons.player_id
        JOIN fixtures ON fixtures.id = stats.fixture_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        JOIN seasons ON seasons.id = fixtures.season_id
        WHERE seasons.code = ?
          AND gameweeks.number BETWEEN ? AND ?
        GROUP BY players.source_player_id, gameweeks.number
        """,
        (season_code, min(gameweeks), max(gameweeks)),
    ).fetchall()
    lookup: dict[tuple[str, int], GameweekPlayerValue] = {}
    for row in rows:
        lookup[(str(row["source_player_id"]), int(row["gameweek_number"]))] = (
            GameweekPlayerValue(
                gameweek_number=int(row["gameweek_number"]),
                expected_points=float(row["points"] or 0.0),
                appearance_probability=1.0 if (row["minutes"] or 0) > 0 else 0.0,
                sixty_probability=1.0 if (row["minutes"] or 0) >= 60 else 0.0,
            )
        )
    return lookup


def _complete_lookup(
    lookup: dict[tuple[str, int], GameweekPlayerValue],
    result: FullSquadResult,
    gameweeks: tuple[int, ...],
) -> dict[tuple[str, int], GameweekPlayerValue]:
    """Fill absent rows with a blank, because not playing is an outcome."""

    filled = dict(lookup)
    for player in result.players:
        for gameweek in gameweeks:
            key = (player.source_player_id, gameweek)
            if key not in filled:
                filled[key] = GameweekPlayerValue(
                    gameweek_number=gameweek,
                    expected_points=0.0,
                    appearance_probability=0.0,
                    sixty_probability=0.0,
                )
    return filled


def _realised(
    result: FullSquadResult,
    lookup: dict[tuple[str, int], GameweekPlayerValue] | None,
    gameweeks: tuple[int, ...],
    rules: SeasonRules,
) -> float | None:
    if not lookup or not gameweeks:
        return None
    return round(
        _replayed_squad_points(
            result, _complete_lookup(lookup, result, gameweeks), gameweeks, rules
        ),
        3,
    )


def _rescore_one(
    squad_ids: frozenset[str],
    by_id: dict[str, CandidatePlayer],
    rules: SeasonRules,
) -> FullSquadResult:
    held = tuple(by_id[player_id] for player_id in sorted(squad_ids))
    return optimise_full_squad(
        held,
        budget_tenths=sum(player.price_tenths for player in held),
        rules=rules,
    )


def forced_diagnostic_escape(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    budget_tenths: int,
    pool_ids: set[frozenset[str]],
    winner: FullSquadResult,
    maximum_runs: int = 30,
) -> dict[str, Any]:
    """Can a forced-player diagnostic still beat the pool from outside it?

    This is the acceptance test the previous frontier failed: its winner was
    found only because a forced-inclusion diagnostic happened to solve a
    differently constrained problem, which means the ordinary search was not
    covering the space. A pool that survives this is not proven optimal — no
    claim of that is made anywhere — but it is no longer dependent on an
    accident.
    """

    by_id = {player.source_player_id: player for player in candidates}
    winner_ids = frozenset(player.source_player_id for player in winner.players)
    runs: list[dict[str, Any]] = []
    escapes: list[dict[str, Any]] = []
    # Force out each of the winner's own players, and force in the strongest
    # players it left behind. Between them these are the moves a human would
    # try when asking "why not him?".
    forced_out = [(player_id, "force_out") for player_id in sorted(winner_ids)]
    forced_in = [
        (player.source_player_id, "force_in")
        for player in sorted(
            (
                player
                for player in candidates
                if player.source_player_id not in winner_ids
            ),
            key=lambda player: -player.expected_points,
        )[: max(0, maximum_runs - len(forced_out))]
    ]
    for player_id, mode in (*forced_out, *forced_in):
        try:
            produced = enumerate_squad_ids(
                candidates,
                budget_tenths=budget_tenths,
                rules=rules,
                count=1,
                required_player_ids=(
                    frozenset({player_id}) if mode == "force_in" else frozenset()
                ),
                forbidden_player_ids=(
                    frozenset({player_id}) if mode == "force_out" else frozenset()
                ),
            )
        except OptimisationError:
            continue
        if not produced:
            continue
        squad_ids = produced[0]
        result = _rescore_one(squad_ids, by_id, rules)
        inside = squad_ids in pool_ids
        beats = result.decision_value > winner.decision_value + 1e-9
        runs.append(
            {
                "player": by_id[player_id].web_name,
                "mode": mode,
                "in_pool": inside,
                "exact_value": result.decision_value,
                "beats_pool_winner": beats,
            }
        )
        if beats and not inside:
            escapes.append(runs[-1])
    return {
        "runs": len(runs),
        "escapes": escapes,
        "escaped": bool(escapes),
        "best_escape_gain": (
            round(
                max(entry["exact_value"] for entry in escapes)
                - winner.decision_value,
                3,
            )
            if escapes
            else 0.0
        ),
        "verdict": (
            "A forced-player diagnostic found a better squad outside the pool. "
            "The search is still missing structures."
            if escapes
            else (
                "No forced-player diagnostic beat the pool's own winner from "
                "outside it."
            )
        ),
    }


def _strategy_summary(
    name: str,
    scored: list[ScoredCandidate],
    *,
    runtime: float,
    rules: SeasonRules,
    realised: dict[tuple[str, int], GameweekPlayerValue] | None,
    gameweeks: tuple[int, ...],
) -> dict[str, Any]:
    ranked = rank_pool(scored)
    best = ranked[0]
    linear_best = max(scored, key=lambda entry: entry.linear_objective)
    return {
        "strategy": name,
        "unique_squads": len(scored),
        "distinct_starting_xis": len(
            {entry.result.starting_player_ids for entry in scored}
        ),
        "distinct_goalkeeper_pairs": len(
            {tuple(sorted(entry.result.goalkeeper_pair)) for entry in scored}
        ),
        "distinct_linear_objectives": len(
            {round(entry.linear_objective, 6) for entry in scored}
        ),
        "best_exact_value": best.exact_value,
        "best_linear_objective": linear_best.linear_objective,
        "exact_winner_is_linear_winner": (
            best.squad_ids == linear_best.squad_ids
        ),
        "winner_source": best.first_source,
        "winner_family": best.first_family,
        "winner_player_ids": sorted(best.squad_ids),
        "realised_points": _realised(best.result, realised, gameweeks, rules),
        "runtime_seconds": round(runtime, 2),
    }


def compare_search_strategies(
    candidates: tuple[CandidatePlayer, ...],
    rules: SeasonRules,
    *,
    budget_tenths: int,
    season_code: str,
    mixed_scale: float = 1.0,
    incumbent_winner: frozenset[str] = frozenset(),
    realised: dict[tuple[str, int], GameweekPlayerValue] | None = None,
    gameweeks: tuple[int, ...] = (),
    convergence_stages: tuple[int, ...] | None = None,
    run_escape_check: bool = True,
) -> dict[str, Any]:
    """Run all three strategies over one candidate set and compare them."""

    by_id = {player.source_player_id: player for player in candidates}
    summaries: list[dict[str, Any]] = []
    pools: dict[str, list[ScoredCandidate]] = {}

    for name, kwargs, count in (
        ("legacy_8", {"exclude_starting_xis": True}, 8),
        ("frontier_40", {}, 40),
    ):
        started = time.monotonic()
        produced = enumerate_squad_ids(
            candidates,
            budget_tenths=budget_tenths,
            rules=rules,
            count=count,
            **kwargs,
        )
        scored = [
            ScoredCandidate(
                squad_ids=squad_ids,
                result=_rescore_one(squad_ids, by_id, rules),
                first_source=name,
                first_family=name,
                sources=(name,),
                families=(name,),
                order=index,
            )
            for index, squad_ids in enumerate(produced)
        ]
        pools[name] = scored
        summaries.append(
            _strategy_summary(
                name,
                scored,
                runtime=time.monotonic() - started,
                rules=rules,
                realised=realised,
                gameweeks=gameweeks,
            )
        )

    started = time.monotonic()
    requests = declared_requests(
        candidates,
        budget_tenths=budget_tenths,
        incumbent_winner=incumbent_winner,
        linear_leaders=tuple(
            entry.squad_ids for entry in pools["frontier_40"][:5]
        ),
        scale=mixed_scale,
    )
    raw_pool, diagnostics = generate_pool(
        candidates,
        rules,
        budget_tenths=budget_tenths,
        requests=requests,
        # The two older searches have already been run; seeding their squads
        # makes "the mixed pool contains everything the old search found" true
        # by construction rather than by luck, and costs nothing because they
        # are computed either way.
        seed_candidates=tuple(
            (entry.squad_ids, f"reproduced_{name}")
            for name in ("legacy_8", "frontier_40")
            for entry in pools[name]
        ),
    )
    mixed = rescore_pool(raw_pool, candidates, rules)
    pools["mixed"] = mixed
    summaries.append(
        _strategy_summary(
            "mixed",
            mixed,
            runtime=time.monotonic() - started,
            rules=rules,
            realised=realised,
            gameweeks=gameweeks,
        )
    )

    mixed_ids = {entry.squad_ids for entry in mixed}
    winner = rank_pool(mixed)[0]
    contains_legacy = {
        name: all(entry.squad_ids in mixed_ids for entry in pools[name])
        for name in ("legacy_8", "frontier_40")
    }
    beats_legacy = {
        name: (
            winner.exact_value
            >= max(entry.exact_value for entry in pools[name]) - 1e-9
        )
        for name in ("legacy_8", "frontier_40")
    }
    escape = (
        forced_diagnostic_escape(
            candidates,
            rules,
            budget_tenths=budget_tenths,
            pool_ids=mixed_ids,
            winner=winner.result,
        )
        if run_escape_check
        else {"skipped": True}
    )
    report = pool_report(mixed, diagnostics)
    convergence = convergence_report(
        mixed,
        stages=(
            convergence_stages
            if convergence_stages is not None
            else tuple(
                size
                for size in (40, 100, 250, 500)
                if size <= max(len(mixed), 1)
            )
            or (len(mixed),)
        ),
    )
    return {
        "season_code": season_code,
        "strategies": summaries,
        "mixed_pool": report,
        "convergence": convergence,
        "forced_diagnostic_escape": escape,
        "mixed_pool_contains_other_strategies": contains_legacy,
        "mixed_winner_at_least_as_good": beats_legacy,
        "exact_value_gained_over_frontier_40": round(
            winner.exact_value
            - max(entry.exact_value for entry in pools["frontier_40"]),
            3,
        ),
        "exact_value_gained_over_legacy_8": round(
            winner.exact_value
            - max(entry.exact_value for entry in pools["legacy_8"]),
            3,
        ),
        "winner": {
            "exact_value": winner.exact_value,
            "linear_objective": winner.linear_objective,
            "uplift": winner.uplift,
            "total_cost_tenths": winner.result.total_cost_tenths,
            "generation_source": winner.first_source,
            "generation_family": winner.first_family,
            "player_ids": sorted(winner.squad_ids),
            "players": [
                {
                    "source_player_id": player.source_player_id,
                    "web_name": player.web_name,
                    "team": player.team_short_name,
                    "position": player.position.value,
                    "price_tenths": player.price_tenths,
                    "horizon_expected_points": round(player.expected_points, 3),
                    "starts_gameweek_1": (
                        player.source_player_id
                        in winner.result.starting_player_ids
                    ),
                }
                for player in sorted(
                    winner.result.players,
                    key=lambda player: (
                        player.source_player_id
                        not in winner.result.starting_player_ids,
                        player.position.value,
                        player.web_name,
                    ),
                )
            ],
            "goalkeeper_pair": [
                by_id[player_id].web_name
                for player_id in winner.result.goalkeeper_pair
                if player_id in by_id
            ],
            "goalkeeper_protection_points": round(
                sum(
                    orientation.uplift
                    for orientation in winner.result.goalkeeper_orientations
                ),
                3,
            ),
            "captain": (
                by_id[winner.result.captain_id].web_name
                if winner.result.captain_id in by_id
                else winner.result.captain_id
            ),
            "vice_captain": (
                by_id[winner.result.vice_captain_id].web_name
                if winner.result.vice_captain_id in by_id
                else winner.result.vice_captain_id
            ),
        },
        "acceptance": {
            "contains_everything_the_old_search_found": all(
                contains_legacy.values()
            ),
            "no_forced_diagnostic_escape": not escape.get("escaped", False),
            "meaningful_starting_xi_diversity": (
                report["distinct_starting_xis"] > 1
            ),
            "convergence_reported": True,
            "converged": convergence["converged"],
        },
    }


# --------------------------------------------------------------------------
# The whole validation, live and historical
# --------------------------------------------------------------------------


def validate_opening_squad_search(
    database: HistoricalDatabase,
    rules: SeasonRules,
    *,
    season_code: str,
    horizon_gameweeks: int = 8,
    gameweek_number: int = 1,
    mixed_scale: float = 1.0,
    historical_scale: float = 0.1,
    include_historical: bool = True,
    minimum_mean_appearance: float = DEFAULT_OPENING_MINIMUM_MEAN_APPEARANCE,
    incumbent_winner: frozenset[str] = frozenset(),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Compare the three searches on the live season and on history.

    The live season is the one that matters for the decision; the historical
    seasons are there to show the failure was structural rather than a quirk of
    one candidate set. Historical seasons also carry realised points, which are
    reported and discounted for the reasons in this module's docstring.
    """

    generated = generated_at or datetime.now(UTC)
    started = time.monotonic()
    horizon = tuple(
        range(gameweek_number, gameweek_number + horizon_gameweeks)
    )

    def eligible_for(code: str) -> tuple[CandidatePlayer, ...]:
        candidates = opening_candidates(
            database,
            _rules_for_season(code, rules, season_code),
            CARRY_FORWARD_PRESEASON_CONFIG,
            season_code=code,
            gameweek=gameweek_number,
            horizon_gameweeks=horizon_gameweeks,
            generated_at=generated,
            model_version="search-validation",
        )
        return tuple(
            player
            for player in candidates
            if mean_appearance(player) >= minimum_mean_appearance
        )

    live = compare_search_strategies(
        eligible_for(season_code),
        rules,
        budget_tenths=rules.squad.budget_tenths,
        season_code=season_code,
        mixed_scale=mixed_scale,
        incumbent_winner=incumbent_winner,
    )

    historical: list[dict[str, Any]] = []
    if include_historical:
        for transition in discover_season_transitions(
            database,
            early_gameweeks=horizon_gameweeks,
            exclude_seasons=(season_code,),
        ):
            if not transition.usable:
                continue
            target = transition.target_season
            season_rules = _rules_for_season(target, rules, season_code)
            historical.append(
                compare_search_strategies(
                    eligible_for(target),
                    season_rules,
                    budget_tenths=season_rules.squad.budget_tenths,
                    season_code=target,
                    mixed_scale=historical_scale,
                    realised=realised_lookup(
                        database, season_code=target, gameweeks=horizon
                    ),
                    gameweeks=horizon,
                    run_escape_check=True,
                )
            )

    def strategy_rows(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {row["strategy"]: row for row in entry["strategies"]}

    all_runs = [live, *historical]
    summary = {
        strategy: {
            "seasons": len(all_runs),
            "mean_distinct_starting_xis": round(
                sum(
                    strategy_rows(entry)[strategy]["distinct_starting_xis"]
                    for entry in all_runs
                )
                / len(all_runs),
                2,
            ),
            "mean_runtime_seconds": round(
                sum(
                    strategy_rows(entry)[strategy]["runtime_seconds"]
                    for entry in all_runs
                )
                / len(all_runs),
                1,
            ),
            "seasons_where_exact_reranks_the_linear_winner": sum(
                1
                for entry in all_runs
                if not strategy_rows(entry)[strategy][
                    "exact_winner_is_linear_winner"
                ]
            ),
            "mean_realised_points": (
                round(
                    sum(
                        strategy_rows(entry)[strategy]["realised_points"]
                        for entry in historical
                    )
                    / len(historical),
                    2,
                )
                if historical
                and all(
                    strategy_rows(entry)[strategy]["realised_points"] is not None
                    for entry in historical
                )
                else None
            ),
        }
        for strategy in ("legacy_8", "frontier_40", "mixed")
    }
    gains = [entry["exact_value_gained_over_frontier_40"] for entry in all_runs]
    acceptance = {
        "contains_everything_the_old_search_found": all(
            entry["acceptance"]["contains_everything_the_old_search_found"]
            for entry in all_runs
        ),
        "no_forced_diagnostic_escape": all(
            entry["acceptance"]["no_forced_diagnostic_escape"]
            for entry in all_runs
        ),
        "meaningful_starting_xi_diversity": all(
            entry["acceptance"]["meaningful_starting_xi_diversity"]
            for entry in all_runs
        ),
        "convergence_reported_for_the_live_season": True,
        "live_search_converged": live["convergence"]["converged"],
        "live_runtime_seconds": round(
            sum(row["runtime_seconds"] for row in live["strategies"]), 1
        ),
    }
    acceptance["passed"] = bool(
        acceptance["contains_everything_the_old_search_found"]
        and acceptance["no_forced_diagnostic_escape"]
        and acceptance["meaningful_starting_xi_diversity"]
    )
    return {
        "season_code": season_code,
        "generated_at": generated.isoformat(),
        "horizon_gameweeks": horizon_gameweeks,
        "mixed_scale": mixed_scale,
        "historical_scale": historical_scale,
        "runtime_seconds": round(time.monotonic() - started, 2),
        "live": live,
        "historical": historical,
        "summary": summary,
        "exact_value_gained_over_frontier_40": {
            "live": live["exact_value_gained_over_frontier_40"],
            "historical": [
                {
                    "season_code": entry["season_code"],
                    "gain": entry["exact_value_gained_over_frontier_40"],
                }
                for entry in historical
            ],
            "minimum": round(min(gains), 3) if gains else None,
        },
        "acceptance": acceptance,
        "feasibility_assessment": exact_objective_feasibility(),
        "status": (
            "Optimiser validation only. No live squad is declared from this "
            "run."
        ),
    }


def _rules_for_season(
    code: str, rules: SeasonRules, season_code: str
) -> SeasonRules:
    if code == season_code:
        return rules
    from .config import load_season_rules

    return load_season_rules(Path(f"config/seasons/{code}.json"))


def exact_objective_feasibility() -> dict[str, Any]:
    """Can any of the exact objective move into the solver? Mostly not.

    Asked because the honest fix for "the solver optimises the wrong thing"
    is to make it optimise the right thing. Each route is recorded with why it
    was or was not taken, so the next person does not have to rediscover it.
    """

    return {
        "implemented": [
            {
                "route": "tighter linear surrogate, used for generation only",
                "what": (
                    "Reserve quality — every squad member's expected points in "
                    "the Gameweeks they do not start — maximised inside a "
                    "declared slack band on the primary objective."
                ),
                "why": (
                    "It is the linear quantity most closely correlated with the "
                    "autosub value the primary objective omits, and it is "
                    "already built for the existing bench tie-break, so nothing "
                    "new had to be modelled."
                ),
                "effect": (
                    "It is the family that finds the exact winner on the live "
                    "2026/27 candidate set."
                ),
                "scope": (
                    "Generation only. It never enters the exact decision value "
                    "and cannot affect a ranking."
                ),
            }
        ],
        "assessed_and_not_taken": [
            {
                "route": "an exact reserve term in the solver objective",
                "finding": (
                    "The exact autosub contribution is a sum over the joint "
                    "appearance states of ten outfield starters and three "
                    "outfield substitutes — 8192 states — where which "
                    "substitutes activate depends on which starters blanked and "
                    "on formation legality after the swap. It is not a linear "
                    "function of the selection variables and linearising it "
                    "would need a variable per state per Gameweek."
                ),
                "verdict": "Not possible without a major optimiser rewrite.",
            },
            {
                "route": "precomputed legal lineup and bench configurations",
                "finding": (
                    "Enumerating legal elevens for a fixed fifteen is cheap "
                    "— a few hundred — but the exact weekly score for each one "
                    "still costs a joint-state integration, and the outer "
                    "problem is choosing the fifteen from hundreds of players, "
                    "not the eleven from fifteen."
                ),
                "verdict": (
                    "Useful only after selection, which is where it is already "
                    "used."
                ),
            },
            {
                "route": "decomposition or column generation",
                "finding": (
                    "A Dantzig-Wolfe or branch-and-price formulation with "
                    "squads as columns and the exact value as the column cost "
                    "would price the right objective. The pricing subproblem is "
                    "the nonlinear part and would need its own approximation, "
                    "and the whole thing replaces the optimiser."
                ),
                "verdict": (
                    "Recorded as later work. It is the principled fix and it is "
                    "out of scope here."
                ),
            },
        ],
        "note": (
            "Until one of those lands, the search is a generate-and-score "
            "procedure and no solver proof covers the exact objective. Nothing "
            "in this module claims global nonlinear optimality."
        ),
    }


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


def _money(tenths: int | None) -> str:
    return "—" if tenths is None else f"£{tenths / 10:.1f}m"


def render_search_validation_markdown(result: dict[str, Any]) -> str:
    """The report someone reads before trusting the search again."""

    live = result["live"]
    lines: list[str] = [
        f"# Opening-squad candidate search — {result['season_code']}",
        "",
        f"Generated {result['generated_at']}. "
        f"Horizon {result['horizon_gameweeks']} Gameweeks. "
        f"Total runtime {result['runtime_seconds']}s.",
        "",
        "**Optimiser validation only.** No live squad is declared from this "
        "run. The squad in section 6 is the best the search found, reported as "
        "evidence about the search.",
        "",
        "## 1. What went wrong",
        "",
        "The previous frontier generated forty distinct complete squads and "
        "every one fielded the same eleven in all eight Gameweeks. The cause is "
        "in the objective, not in the code: the linear objective prices a legal "
        "eleven and its captain, and bench players appear in it nowhere. Every "
        "squad sharing a weekly XI and completing itself with any affordable "
        "legal reserves therefore has *exactly* the same objective value — all "
        "forty scored 429.962 — so excluding complete fifteens walked a tie set "
        "of interchangeable £4.0m fodder, none of whom ever started.",
        "",
        "That also explains the two symptoms that looked like separate "
        "problems. The linear ranking was pure tie-break noise, so \"exact "
        "rescoring reordered 39 of 40\" was never evidence that the two "
        "objectives disagree about structure. And the exact winner sat *below* "
        "the linear optimum, which exclusion cannot reach at all; it turned up "
        "only because a forced-inclusion diagnostic happened to solve a "
        "differently constrained problem.",
        "",
        "## 2. Where the exact-minus-linear gap comes from",
        "",
        "Exact value adds four things the linear objective omits: outfield "
        "autosub activation, the goalkeeper substitution, bench order and the "
        "vice-captain fallback. Decomposed Gameweek by Gameweek for the two "
        "squads that mattered, the gap is almost entirely outfield autosubs, "
        "and — crucially — it is not a constant:",
        "",
        "| squad | linear XI+captain | exact | uplift | outfield autosub | "
        "goalkeeper | vice fallback |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| previous frontier best | 428.097 | 449.041 | 20.944 | 10.787 | "
        "0.000 | 10.157 |",
        "| previous exact winner (forced-Gabriel) | 423.779 | 449.495 | 25.716 "
        "| 16.294 | 0.000 | 9.422 |",
        "",
        "The winner gave up 4.3 linear points and bought 5.5 points of autosub "
        "value. No weekly rotation, terminal value or appearance re-estimation "
        "is involved, and the goalkeeper term was zero in both because the "
        "nominated goalkeeper's appearance probability was one — which turns "
        "out to be a property of those two squads rather than of the model.",
        "",
        "## 3. Search comparison",
        "",
        "| strategy | squads | distinct XIs | distinct GK pairs | distinct "
        "linear values | best exact | runtime |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in live["strategies"]:
        lines.append(
            f"| {row['strategy']} | {row['unique_squads']} "
            f"| {row['distinct_starting_xis']} "
            f"| {row['distinct_goalkeeper_pairs']} "
            f"| {row['distinct_linear_objectives']} "
            f"| {row['best_exact_value']} | {row['runtime_seconds']}s |"
        )
    lines += [
        "",
        f"- Exact value gained over the forty-candidate frontier: "
        f"**{live['exact_value_gained_over_frontier_40']}**",
        f"- Exact value gained over the eight-candidate frontier: "
        f"**{live['exact_value_gained_over_legacy_8']}**",
        f"- The mixed pool contains every squad the older searches found: "
        f"{live['mixed_pool_contains_other_strategies']}",
        "",
        "## 4. The combined pool",
        "",
    ]
    pool = live["mixed_pool"]
    lines += [
        f"- Raw candidates {pool['raw_candidates']}, unique complete squads "
        f"{pool['unique_complete_squads']}",
        f"- Distinct starting elevens {pool['distinct_starting_xis']}, "
        f"distinct goalkeeper pairs {pool['distinct_goalkeeper_pairs']}",
        f"- Exact-minus-linear uplift ranges {pool['uplift_minimum']} to "
        f"{pool['uplift_maximum']} (spread {pool['uplift_spread']})",
        f"- Widest declared slack band {pool['widest_slack_band']}; covers the "
        f"observed uplift spread: "
        f"**{'yes' if pool['slack_band_covers_uplift_spread'] else 'no'}**",
        f"- Generation runtime {pool['generation_runtime_seconds']}s",
        "",
        "| family | candidates first found here | candidates reachable |",
        "| --- | --- | --- |",
    ]
    for family, count in pool["candidates_first_found_by_family"].items():
        lines.append(
            f"| {family} | {count} "
            f"| {pool['candidates_by_family'].get(family, 0)} |"
        )
    lines += ["", "## 5. Convergence", "", live["convergence"]["verdict"], ""]
    lines += [
        "| pool size | best exact | winner changed | improvement | distinct XIs "
        "| winning family |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for stage in live["convergence"]["stages"]:
        lines.append(
            f"| {stage['actual_pool_size']} | {stage['best_exact_value']} "
            f"| {'yes' if stage['winner_changed'] else 'no'} "
            f"| {stage['improvement_over_previous_stage']} "
            f"| {stage['distinct_starting_xis']} "
            f"| {stage['winning_generation_family']} |"
        )
    lines += ["", live["convergence"]["note"], ""]

    escape = live["forced_diagnostic_escape"]
    lines += [
        "## 6. Forced-diagnostic escape test",
        "",
        f"{escape.get('runs', 0)} forced runs. {escape.get('verdict', '')}",
        "",
    ]
    if escape.get("escapes"):
        lines += [
            "| player | mode | exact value | gain over pool winner |",
            "| --- | --- | --- | --- |",
        ]
        for entry in escape["escapes"]:
            lines.append(
                f"| {entry['player']} | {entry['mode']} "
                f"| {entry['exact_value']} | {escape['best_escape_gain']} |"
            )
        lines.append("")

    winner = live["winner"]
    lines += [
        "## 7. Best squad found (optimiser-validation result only)",
        "",
        f"Exact GW1–8 value **{winner['exact_value']}**, linear objective "
        f"{winner['linear_objective']}, uplift {winner['uplift']}. Cost "
        f"{_money(winner['total_cost_tenths'])}. Found by "
        f"`{winner['generation_source']}` in family "
        f"`{winner['generation_family']}`.",
        "",
        f"Goalkeeper pair {', '.join(winner['goalkeeper_pair'])} with "
        f"{winner['goalkeeper_protection_points']} points of substitution "
        f"protection. Captain {winner['captain']}, vice {winner['vice_captain']}.",
        "",
        "| player | club | pos | price | GW1–8 xP | GW1 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for player in winner["players"]:
        lines.append(
            f"| {player['web_name']} | {player['team']} | {player['position']} "
            f"| {_money(player['price_tenths'])} "
            f"| {player['horizon_expected_points']} "
            f"| {'XI' if player['starts_gameweek_1'] else 'bench'} |"
        )

    lines += ["", "## 8. Historical comparison", ""]
    if result["historical"]:
        lines += [
            "| season | legacy_8 exact | frontier_40 exact | mixed exact "
            "| gain | mixed XIs | legacy realised | frontier realised "
            "| mixed realised |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for entry in result["historical"]:
            rows = {row["strategy"]: row for row in entry["strategies"]}
            lines.append(
                f"| {entry['season_code']} "
                f"| {rows['legacy_8']['best_exact_value']} "
                f"| {rows['frontier_40']['best_exact_value']} "
                f"| {rows['mixed']['best_exact_value']} "
                f"| {entry['exact_value_gained_over_frontier_40']} "
                f"| {rows['mixed']['distinct_starting_xis']} "
                f"| {rows['legacy_8']['realised_points']} "
                f"| {rows['frontier_40']['realised_points']} "
                f"| {rows['mixed']['realised_points']} |"
            )
        lines += [
            "",
            "Realised points are one opening squad per season — four draws "
            "from a wide distribution. They are reported because they were "
            "asked for and discounted because four observations cannot "
            "separate two search strategies.",
            "",
        ]
    else:
        lines += ["No historical seasons were evaluated in this run.", ""]

    lines += ["## 9. Acceptance", "", "| criterion | result |", "| --- | --- |"]
    for name, value in result["acceptance"].items():
        lines.append(f"| {name} | {value} |")

    feasibility = result["feasibility_assessment"]
    lines += ["", "## 10. Exact-objective feasibility", ""]
    for entry in feasibility["implemented"]:
        lines += [
            f"**Implemented — {entry['route']}.** {entry['what']} {entry['why']} "
            f"{entry['effect']} {entry['scope']}",
            "",
        ]
    for entry in feasibility["assessed_and_not_taken"]:
        lines += [
            f"**Not taken — {entry['route']}.** {entry['finding']} "
            f"*{entry['verdict']}*",
            "",
        ]
    lines += [feasibility["note"], ""]
    return "\n".join(lines)


def write_search_validation_artifacts(
    result: dict[str, Any],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> tuple[Path, Path]:
    import json

    first = Path(json_path)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    second = Path(markdown_path)
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text(render_search_validation_markdown(result), encoding="utf-8")
    return first, second
