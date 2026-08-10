"""The candidate search has to reach squads the linear objective cannot see.

The failure this replaces was not a bug in any one function. Every part behaved
as written; the search space was simply the wrong shape. So these tests are
about the shape: a synthetic league is built where the linear optimum and the
exact optimum are *known in advance* to be different squads, and the tests
assert that exclusion alone never finds the exact one and that the declared
families do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_engine.candidate_search import (
    LINEAR_SLACK_BANDS,
    GenerationRequest,
    convergence_report,
    declared_requests,
    generate_pool,
    pool_report,
    rank_pool,
    rescore_pool,
)
from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.optimisation import (
    CandidatePlayer,
    GameweekPlayerValue,
    OptimisationError,
    SquadGroupConstraint,
    SquadSpendConstraint,
    enumerate_squad_ids,
    optimise_full_squad,
)

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
GAMEWEEKS = 2


def _player(
    identifier: str,
    position: Position,
    price_tenths: int,
    points: float,
    appearance: float,
    *,
    team: str | None = None,
) -> CandidatePlayer:
    values = tuple(
        GameweekPlayerValue(gameweek, points, appearance, appearance)
        for gameweek in range(1, GAMEWEEKS + 1)
    )
    return CandidatePlayer(
        source_player_id=identifier,
        web_name=identifier,
        team_id=team or identifier,
        team_short_name=(team or identifier)[:3].upper(),
        position=position,
        price_tenths=price_tenths,
        expected_points=points * GAMEWEEKS,
        gameweek_expected_points=points,
        appearance_probability=appearance,
        gameweek_values=values,
    )


#: Tight enough that a premium defender and a real bench cannot both be
#: afforded. That is the whole point of the fixture: the trade has to be
#: genuinely exclusive or the linear and exact optima coincide and the test
#: proves nothing.
BUDGET = 865


def _league() -> tuple[CandidatePlayer, ...]:
    """A league whose linear and exact optima are deliberately different squads.

    Core midfielders and forwards outscore every defender, so the eleven is a
    fixed 1-4-3-3 and the only lineup question is who fills the fourth
    defensive slot: a premium defender worth three and a half points a week
    more than the fourth-best ordinary one, at forty times the price.

    The linear objective prices the eleven and its captain and nothing else, so
    it always buys the premium defender. At this budget, doing so leaves only
    junk for the bench. The starters play three weeks in four, so a real bench
    is worth a great deal in autosub value — and none of that is visible to the
    objective the solver optimises.

        premium + junk bench = 865   (the linear optimum, exactly the budget)
        four ordinary + good bench = 855   (the exact optimum)
        premium + good bench = 895   (unaffordable)
        premium + four ordinary = 875   (unaffordable, so the swing is a choice)

    The four ordinary defenders are separated by a tenth of a point so that
    which three of them start is never ambiguous. Without that the eleven would
    vary for a reason that has nothing to do with the effect under test.
    """

    players: list[CandidatePlayer] = []
    players.append(_player("gk_first", Position.GK, 45, 5.0, 0.9, team="gk1"))
    players.append(_player("gk_reserve", Position.GK, 40, 1.0, 0.9, team="gk2"))
    for index, points in enumerate((6.3, 6.2, 6.1, 6.0)):
        players.append(
            _player(f"def_core{index}", Position.DEF, 50, points, 0.75, team=f"dc{index}")
        )
    for index in range(3):
        players.append(
            _player(f"mid_core{index}", Position.MID, 70, 8.0, 0.75, team=f"mc{index}")
        )
    for index in range(3):
        players.append(
            _player(f"fwd_core{index}", Position.FWD, 70, 8.0, 0.75, team=f"fc{index}")
        )
    players.append(_player("def_premium", Position.DEF, 90, 9.5, 0.75, team="dp"))
    # Reserves worth owning, and reserves that are not. Several of each at
    # identical price and points, so exclusion has interchangeable completions
    # to walk — which is exactly the pathology under test.
    for index in range(3):
        players.append(
            _player(f"bench_good_def{index}", Position.DEF, 50, 5.5, 0.9, team=f"bgd{index}")
        )
        players.append(
            _player(f"bench_good_mid{index}", Position.MID, 50, 5.5, 0.9, team=f"bgm{index}")
        )
        players.append(
            _player(f"bench_junk_def{index}", Position.DEF, 40, 0.2, 0.9, team=f"bjd{index}")
        )
        players.append(
            _player(f"bench_junk_mid{index}", Position.MID, 40, 0.2, 0.9, team=f"bjm{index}")
        )
    return tuple(sorted(players, key=lambda player: player.source_player_id))


def _reserve_value(result) -> float:
    """Horizon points sitting on the bench — the term the objective omits."""

    return sum(
        player.expected_points
        for player in result.players
        if player.source_player_id not in result.starting_player_ids
    )


def _rescore(squad_ids, candidates):
    by_id = {player.source_player_id: player for player in candidates}
    held = tuple(by_id[player_id] for player_id in sorted(squad_ids))
    return optimise_full_squad(
        held,
        budget_tenths=sum(player.price_tenths for player in held),
        rules=RULES,
    )


# --------------------------------------------------------------------------
# The failure, reproduced
# --------------------------------------------------------------------------


def test_exclusion_alone_walks_one_starting_eleven() -> None:
    """The diagnosis, as a test.

    Bench players appear nowhere in the linear objective, so every completion
    of one eleven ties exactly. Excluding complete fifteens therefore enumerates
    interchangeable reserves and never changes the lineup.
    """

    league = _league()

    produced = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=6, objective="primary"
    )

    assert len(produced) == 6
    assert len(set(produced)) == 6, "squads must at least be distinct"
    results = [_rescore(ids, league) for ids in produced]
    assert len({result.starting_player_ids for result in results}) == 1
    # And they are all tied on the objective the solver actually proved.
    assert len({round(result.solver_objective, 6) for result in results}) == 1
    # The premium defender is in every one of them.
    assert all(
        "def_premium" in {player.source_player_id for player in result.players}
        for result in results
    )


def test_a_slack_band_reaches_the_squad_exclusion_cannot() -> None:
    """The fix, as a test.

    The exact optimum sits below the linear optimum, so no amount of excluding
    complete squads reaches it. Pinning the objective inside a band and
    maximising reserve quality does, in one solve.
    """

    league = _league()

    excluded = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=8, objective="primary"
    )
    banded = enumerate_squad_ids(
        league,
        budget_tenths=BUDGET,
        rules=RULES,
        count=2,
        objective="reserve",
        linear_slack=8.0,
    )

    best_excluded = max(
        (_rescore(ids, league) for ids in excluded),
        key=lambda result: result.decision_value,
    )
    best_banded = max(
        (_rescore(ids, league) for ids in banded),
        key=lambda result: result.decision_value,
    )

    assert best_banded.decision_value > best_excluded.decision_value
    # It wins on exact value while losing on the linear objective, which is the
    # entire point.
    assert best_banded.solver_objective < best_excluded.solver_objective
    # And it gets there the way the diagnosis says it must: by buying reserve
    # value the linear objective cannot see. Asserting *which* player is
    # dropped would test the fixture's arithmetic; asserting that the bench is
    # stronger tests the mechanism.
    assert _reserve_value(best_banded) > _reserve_value(best_excluded)


def test_the_uplift_is_what_separates_them() -> None:
    """Exact-minus-linear is not a constant, which is why ranking on linear fails."""

    league = _league()
    excluded = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=4, objective="primary"
    )
    banded = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=2,
        objective="reserve", linear_slack=8.0,
    )
    uplifts = [
        _rescore(ids, league).decision_value
        - _rescore(ids, league).lineup_expected_points
        for ids in (*excluded, *banded)
    ]

    assert max(uplifts) - min(uplifts) > 1.0


# --------------------------------------------------------------------------
# Generation must never touch valuation
# --------------------------------------------------------------------------


def test_a_perturbation_never_reaches_a_reported_number() -> None:
    """The perturbation shakes out ties and is then gone.

    A squad found under a perturbed objective must score exactly what it scores
    without one, or the perturbation would be deciding rankings.
    """

    league = _league()
    perturbation = {
        player.source_player_id: 0.002 * ((index % 5) - 2)
        for index, player in enumerate(league)
    }

    perturbed = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=3,
        objective="primary", perturbation=perturbation,
    )

    for squad_ids in perturbed:
        first = _rescore(squad_ids, league)
        second = _rescore(squad_ids, league)
        assert first.decision_value == second.decision_value
        assert first.solver_objective == second.solver_objective


def test_a_structural_constraint_does_not_change_a_squads_exact_value() -> None:
    league = _league()
    constraint = SquadGroupConstraint(
        name="no_premium_defender",
        player_ids=frozenset({"def_premium"}),
        maximum=0,
    )

    produced = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=1,
        group_constraints=(constraint,),
    )

    assert produced
    squad_ids = produced[0]
    assert "def_premium" not in squad_ids
    # Rescoring is unaware of the constraint that generated the squad.
    assert _rescore(squad_ids, league).decision_value == pytest.approx(
        _rescore(squad_ids, league).decision_value
    )


def test_a_spend_constraint_is_honoured() -> None:
    league = _league()
    defensive = frozenset(
        player.source_player_id
        for player in league
        if player.position in (Position.GK, Position.DEF)
    )
    constraint = SquadSpendConstraint(
        name="defence", player_ids=defensive, maximum_tenths=380
    )

    produced = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=1,
        spend_constraints=(constraint,),
    )

    assert produced
    by_id = {player.source_player_id: player for player in league}
    spent = sum(
        by_id[player_id].price_tenths
        for player_id in produced[0]
        if player_id in defensive
    )
    assert spent <= 380


def test_forced_in_and_forced_out_are_both_honoured() -> None:
    league = _league()

    produced = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=1,
        required_player_ids=frozenset({"bench_good_def0"}),
        forbidden_player_ids=frozenset({"def_premium"}),
    )

    assert produced
    assert "bench_good_def0" in produced[0]
    assert "def_premium" not in produced[0]


def test_requiring_and_forbidding_the_same_player_is_refused() -> None:
    league = _league()

    with pytest.raises(OptimisationError, match="both require and forbid"):
        enumerate_squad_ids(
            league, budget_tenths=BUDGET, rules=RULES, count=1,
            required_player_ids=frozenset({"def_premium"}),
            forbidden_player_ids=frozenset({"def_premium"}),
        )


def test_every_squad_stays_within_budget() -> None:
    league = _league()

    produced = enumerate_squad_ids(
        league, budget_tenths=BUDGET - 20, rules=RULES, count=3
    )

    for squad_ids in produced:
        assert _rescore(squad_ids, league).total_cost_tenths <= BUDGET - 20


# --------------------------------------------------------------------------
# Pool assembly
# --------------------------------------------------------------------------


def _small_pool(league):
    requests = [
        GenerationRequest("A_complete_squads", "exclusion", 4),
        GenerationRequest("B_distinct_xis", "distinct_xi", 3, exclude_starting_xis=True),
        GenerationRequest(
            "C_slack_bands", "slack_8_reserve", 3, objective="reserve", linear_slack=8.0
        ),
        GenerationRequest(
            "D_forced", "force_good_bench", 1,
            required_player_ids=frozenset({"bench_good_def0"}),
        ),
    ]
    pool, diagnostics = generate_pool(
        league, RULES, budget_tenths=BUDGET, requests=requests
    )
    return pool, diagnostics, rescore_pool(pool, league, RULES)


def test_the_pool_deduplicates_and_records_every_source() -> None:
    league = _league()
    pool, diagnostics, scored = _small_pool(league)

    memberships = [entry.squad_ids for entry in pool]
    assert len(set(memberships)) == len(memberships)
    assert diagnostics["unique_squads"] == len(pool)
    assert diagnostics["raw_candidates"] >= diagnostics["unique_squads"]
    # A squad two families both reached carries both.
    assert any(len(entry.families) >= 1 for entry in scored)
    assert all(entry.sources for entry in scored)


def test_the_pool_is_ranked_only_by_exact_value() -> None:
    league = _league()
    _, diagnostics, scored = _small_pool(league)

    ranked = rank_pool(scored)
    values = [entry.exact_value for entry in ranked]
    assert values == sorted(values, reverse=True)

    report = pool_report(scored, diagnostics)
    assert report["candidates"][0]["exact_rank"] == 1
    assert report["candidates"][0]["exact_decision_value"] == max(values)
    assert report["distinct_starting_xis"] >= 1
    assert report["uplift_spread"] is not None
    assert set(report["candidates_first_found_by_family"]) <= {
        "A_complete_squads",
        "B_distinct_xis",
        "C_slack_bands",
        "D_forced",
    }


def test_the_report_names_the_generation_source_of_every_candidate() -> None:
    league = _league()
    _, diagnostics, scored = _small_pool(league)

    report = pool_report(scored, diagnostics)

    for entry in report["candidates"]:
        assert entry["generation_source"]
        assert entry["generation_family"]
        assert entry["all_sources"]
        assert len(entry["player_ids"]) == RULES.squad.squad_size


def test_the_mixed_pool_contains_what_plain_exclusion_would_have_found() -> None:
    """The new search must not be able to lose to the old one.

    Family A *is* the old behaviour, so anything the incumbent frontier could
    produce is in the pool by construction. This asserts it rather than
    assuming it.
    """

    league = _league()
    old = enumerate_squad_ids(
        league, budget_tenths=BUDGET, rules=RULES, count=4, objective="primary"
    )
    _, _, scored = _small_pool(league)
    pool_ids = {entry.squad_ids for entry in scored}

    assert set(old) <= pool_ids
    best_old = max(
        _rescore(ids, league).decision_value for ids in old
    )
    best_new = max(entry.exact_value for entry in scored)
    assert best_new >= best_old


# --------------------------------------------------------------------------
# Convergence
# --------------------------------------------------------------------------


def test_convergence_expands_every_family_and_ends_on_the_whole_pool() -> None:
    league = _league()
    _, _, scored = _small_pool(league)

    report = convergence_report(scored, stages=(0.25, 0.5, 0.75, 1.0))

    assert report["stages"]
    sizes = [row["actual_pool_size"] for row in report["stages"]]
    # Nested fractional stages can only grow the pool.
    assert sizes == sorted(sizes)
    best = [row["best_exact_value"] for row in report["stages"]]
    # A superset of candidates cannot have a worse best.
    assert best == sorted(best)
    # The final stage is the whole pool, so its best is the pool's best and the
    # winning squad is inside the last stage rather than beyond it.
    assert report["stages"][-1]["best_exact_value"] == max(
        entry.exact_value for entry in scored
    )
    assert report["stages"][-1]["actual_pool_size"] == len(scored)
    # Every stage expands all families, so the family count is carried through.
    distinct_families = len({entry.first_family for entry in scored})
    assert all(
        row["families_expanded"] == distinct_families for row in report["stages"]
    )
    assert "global nonlinear optimality" in report["note"]


def test_a_search_that_keeps_improving_is_reported_as_not_converged() -> None:
    """A convergence criterion that cannot fail would be decoration."""

    league = _league()
    _, _, scored = _small_pool(league)

    strict = convergence_report(
        scored, stages=(0.5, 1.0), tolerance=0.0, stages_required=5
    )

    assert strict["converged"] is False
    assert "NOT converged" in strict["verdict"]


def test_declared_requests_cover_every_declared_family() -> None:
    league = _league()

    requests = declared_requests(league, budget_tenths=BUDGET, scale=0.1)

    families = {request.family for request in requests}
    assert families == {
        "A_complete_squads",
        "B_distinct_xis",
        "C_slack_bands",
        "D_forced",
        "E_structural",
        "F_perturbations",
    }
    # At full scale every declared band is generated. At reduced scale only
    # the widest survive, because a narrow band can only reach squads a wider
    # one also reaches.
    full = declared_requests(league, budget_tenths=BUDGET, scale=1.0)
    assert {
        request.linear_slack
        for request in full
        if request.family == "C_slack_bands"
    } == set(LINEAR_SLACK_BANDS)
    reduced = {
        request.linear_slack
        for request in requests
        if request.family == "C_slack_bands"
    }
    assert reduced
    assert reduced <= set(LINEAR_SLACK_BANDS)
    assert max(reduced) == max(LINEAR_SLACK_BANDS)
    # Forced-out runs exist for the incumbent squad's own players.
    incumbent = frozenset({"def_premium"})
    with_incumbent = declared_requests(
        league, budget_tenths=BUDGET, incumbent_winner=incumbent, scale=0.1
    )
    assert any(
        request.forbidden_player_ids == incumbent
        for request in with_incumbent
    )
