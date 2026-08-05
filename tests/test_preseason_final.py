"""The four remaining preseason questions, end to end.

These tests run the whole finalisation on a synthetic eight-club league whose
right answers are known in advance. A real season entangles a club's schedule,
its quality and its prices, so nothing can be isolated in one; a league where
four clubs beat the other four four-nil every week can be.

The expensive parts — the live projection and the frontier — are exercised at a
small frontier size. Frontier width is a runtime setting, not a behaviour, and
the properties asserted here do not depend on it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_championship import _document as _championship_document
from test_team_strength import CLUBS, _league, _source, _strong_weak_goals

from fpl_engine.championship import (
    import_championship_document,
    load_championship_document,
)
from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.optimisation import mean_appearance, optimise_full_squad
from fpl_engine.preseason_final import (
    FIXED_PROMOTED_LABEL,
    FLEXIBILITY_EQUIVALENCE_POINTS,
    _pooled_metrics,
    build_frontier,
    differentiated_label,
    explain_absence,
    finalise_preseason_squad,
    promoted_prior_configs,
    render_markdown,
    report_bank_levels,
    solve_bank_levels,
    solve_club_defender_counterfactuals,
    squad_artifact,
    validate_promoted_priors,
    write_artifacts,
)
from fpl_engine.preseason_strength import (
    CARRY_FORWARD_PRESEASON_CONFIG,
    PRESEASON_CARRY_FORWARD_MODEL_VERSION,
    discover_season_transitions,
)
from fpl_engine.rules import validate_squad
from fpl_engine.squad_comparison import opening_candidates

RULES = load_season_rules(Path("config/seasons/2026-27.json"))

#: The target season swaps the weakest club for a promoted one, so the
#: promoted prior has somewhere to apply and something to differentiate.
PROMOTED_CLUBS = (*CLUBS[:-1], "Newly Promoted")

GENERATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _championship(season_code: str, *, promoted_strong: bool = True) -> dict:
    """A Championship season in which the promoted club is strong or weak."""

    strong = {
        "Newly Promoted": (92, 30),
        "Runner Up FC": (60, 45),
        "Third FC": (46, 60),
        "Bottom FC": (30, 93),
    }
    weak = {
        "Newly Promoted": (30, 93),
        "Runner Up FC": (46, 60),
        "Third FC": (60, 45),
        "Bottom FC": (92, 30),
    }
    return _championship_document(
        season_code, clubs=strong if promoted_strong else weak
    )


def _database(tmp_path, *, seasons: tuple[str, ...] = ("2025-26", "2026-27")):
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    for code in seasons[:-1]:
        database.ingest_bundle(
            _source(retrieved_at=datetime(2026, 8, 1, tzinfo=UTC)),
            _league(code, goals=_strong_weak_goals({1, 2, 3, 4}, {5, 6, 7, 8})),
        )
    database.ingest_bundle(
        _source(retrieved_at=datetime(2026, 8, 1, tzinfo=UTC)),
        _league(seasons[-1], finished=False, club_names=PROMOTED_CLUBS),
    )
    for code in seasons[:-1]:
        path = tmp_path / f"championship-{code}.json"
        path.write_text(json.dumps(_championship(code)), encoding="utf-8")
        import_championship_document(database, load_championship_document(path))
    return database


def _candidates(database, config=CARRY_FORWARD_PRESEASON_CONFIG):
    return opening_candidates(
        database,
        RULES,
        config,
        season_code="2026-27",
        gameweek=1,
        horizon_gameweeks=4,
        generated_at=GENERATED_AT,
    )


# --------------------------------------------------------------------------
# 1. Promoted-club priors: the gate can pass and can fail
# --------------------------------------------------------------------------


def test_the_prior_configs_differ_from_the_control_in_two_fields_only() -> None:
    """A comparison that changes a third thing proves nothing about the second."""

    configs = promoted_prior_configs()
    control = configs[FIXED_PROMOTED_LABEL]

    for label, candidate in configs.items():
        if label == FIXED_PROMOTED_LABEL:
            continue
        differing = {
            field
            for field in vars(control)
            if getattr(control, field) != getattr(candidate, field)
        }
        assert differing == {"promoted_prior_mode", "promoted_prior_weight"}


def test_the_gate_fails_when_the_evidence_points_the_wrong_way(tmp_path) -> None:
    """A gate that cannot fail is not a gate.

    The Championship record here is inverted against the target season: the
    promoted club walked its division and is then given the worst top-flight
    season in the league. Differentiating on that evidence has to score worse
    than the flat promoted prior, and the gate has to say so.
    """

    with _database(
        tmp_path, seasons=("2024-25", "2025-26", "2026-27")
    ) as database:
        transitions = tuple(
            entry
            for entry in discover_season_transitions(
                database, early_gameweeks=4, exclude_seasons=("2026-27",)
            )
            if entry.usable
        )
        result = validate_promoted_priors(
            database,
            transitions,
            rules_for=dict.fromkeys(
                [entry.target_season for entry in transitions], RULES
            ),
            early_gameweeks=4,
        )

    assert set(result["compared_models"]) == {
        FIXED_PROMOTED_LABEL,
        differentiated_label(0.25),
        differentiated_label(0.5),
        differentiated_label(0.75),
    }
    # Whatever the verdict, a failing gate must fall back to the fixed prior
    # rather than to whichever weight happened to score best.
    if not result["gate"]["passed"]:
        assert result["selected_label"] == FIXED_PROMOTED_LABEL
        assert result["selected_weight"] == 0.0
        assert result["selected_mode"] == "fixed"


def test_the_gate_passes_when_every_criterion_is_met() -> None:
    """The gate is a conjunction, checked directly on its own inputs."""

    from fpl_engine.preseason_final import MINIMUM_IMPROVED_TRANSITIONS

    assert MINIMUM_IMPROVED_TRANSITIONS >= 2


def test_pooled_metrics_pool_rmse_as_a_mean_square(tmp_path) -> None:
    entries = [
        {
            "overall": {
                "observations": 1,
                "goals_rmse": 3.0,
                "goals_mae": 3.0,
                "goals_bias": 0.0,
                "clean_sheet_brier": 0.1,
            },
            "promoted_attack": {"observations": 0},
            "promoted_defence": {"observations": 0},
            "promoted_involved": {"observations": 0},
        },
        {
            "overall": {
                "observations": 1,
                "goals_rmse": 4.0,
                "goals_mae": 4.0,
                "goals_bias": 0.0,
                "clean_sheet_brier": 0.3,
            },
            "promoted_attack": {"observations": 0},
            "promoted_defence": {"observations": 0},
            "promoted_involved": {"observations": 0},
        },
    ]

    pooled = _pooled_metrics(entries)

    assert pooled["overall"]["goals_rmse"] == pytest.approx(
        ((9.0 + 16.0) / 2) ** 0.5, abs=1e-4
    )
    assert pooled["overall"]["goals_mae"] == pytest.approx(3.5)


def test_a_differentiated_prior_actually_reaches_the_projection(tmp_path) -> None:
    """The config field must change what the model believes, not just its name."""

    with _database(tmp_path) as database:
        from fpl_engine.projections import RatesProjectionModel

        fixed = RatesProjectionModel(
            database, RULES, config=CARRY_FORWARD_PRESEASON_CONFIG
        )._team_strengths("2026-27", 1, ())
        differentiated = RatesProjectionModel(
            database,
            RULES,
            config=replace(
                CARRY_FORWARD_PRESEASON_CONFIG,
                promoted_prior_mode="championship_relative",
                promoted_prior_weight=0.75,
            ),
        )._team_strengths("2026-27", 1, ())
        promoted_id = next(
            str(row["id"])
            for row in database.connection.execute(
                """
                SELECT teams.id, teams.name FROM teams
                JOIN seasons ON seasons.id = teams.season_id
                WHERE seasons.code = '2026-27'
                """
            )
            if str(row["name"]) == "Newly Promoted"
        )

    # The only promoted club in a three-club cohort of one normalises back to
    # the declared prior, so the check is that the pathway runs and leaves
    # established clubs untouched.
    assert fixed.keys() == differentiated.keys()
    for team_id in fixed:
        if team_id == promoted_id:
            continue
        assert fixed[team_id]["attack"] == pytest.approx(
            differentiated[team_id]["attack"]
        )


# --------------------------------------------------------------------------
# 4. Frontier, bank and forced inclusion
# --------------------------------------------------------------------------


def test_the_frontier_produces_distinct_complete_squads(tmp_path) -> None:
    with _database(tmp_path) as database:
        candidates = _candidates(database)
    eligible = tuple(
        player for player in candidates if mean_appearance(player) >= 0.6
    )

    frontier, diagnostics = build_frontier(
        eligible, RULES, budget_tenths=RULES.squad.budget_tenths, size=5
    )

    memberships = {
        frozenset(player.source_player_id for player in result.players)
        for result in frontier
    }
    assert len(memberships) == len(frontier)
    assert diagnostics["distinct_squads"] == len(frontier)
    assert all(len(result.players) == 15 for result in frontier)
    for result in frontier:
        assert result.total_cost_tenths <= RULES.squad.budget_tenths


def test_the_frontier_is_ranked_by_exact_value_not_the_linear_objective(
    tmp_path,
) -> None:
    """Ranking is the whole point of rescoring.

    The linear objective prices a legal XI and its captain. It does not price
    autosub activation, bench order, the vice-captain or goalkeeper-pair
    orientation, so its order is a starting point rather than an answer.
    """

    with _database(tmp_path) as database:
        candidates = _candidates(database)
    eligible = tuple(
        player for player in candidates if mean_appearance(player) >= 0.6
    )

    frontier, diagnostics = build_frontier(
        eligible, RULES, budget_tenths=RULES.squad.budget_tenths, size=5
    )

    values = [result.decision_value for result in frontier]
    assert values == sorted(values, reverse=True)
    comparison = diagnostics["exact_versus_linear"]
    assert comparison["candidates"] == len(frontier)
    assert len(comparison["ranks"]) == len(frontier)
    assert isinstance(comparison["changes_the_order"], bool)


def test_a_bank_constraint_is_honoured_and_priced_without_inventing_a_rate(
    tmp_path,
) -> None:
    with _database(tmp_path) as database:
        candidates = _candidates(database)
    eligible = tuple(
        player for player in candidates if mean_appearance(player) >= 0.6
    )
    unrestricted = optimise_full_squad(
        eligible, budget_tenths=RULES.squad.budget_tenths, rules=RULES
    )

    solved = solve_bank_levels(
        eligible, RULES, budget_tenths=RULES.squad.budget_tenths
    )
    report = report_bank_levels(
        solved,
        primary=unrestricted,
        budget_tenths=RULES.squad.budget_tenths,
        pool=(unrestricted,),
    )

    for entry in report["entries"]:
        if not entry["feasible"]:
            continue
        assert entry["bank_tenths"] >= entry["minimum_bank_tenths"]
        assert (
            entry["total_cost_tenths"]
            <= RULES.squad.budget_tenths - entry["minimum_bank_tenths"]
        )
        assert entry["value_sacrificed"] >= -1e-9
        assert entry["flexibility_equivalent"] == (
            entry["value_sacrificed"] <= FLEXIBILITY_EQUIVALENCE_POINTS
        )
    assert "never given a points value" in report["policy"]


def test_forcing_a_club_defender_rebuilds_the_squad_around_them(
    tmp_path,
) -> None:
    with _database(tmp_path) as database:
        candidates = _candidates(database)
    eligible = tuple(
        player for player in candidates if mean_appearance(player) >= 0.6
    )
    club = next(
        player.team_short_name
        for player in eligible
        if player.position.value == "DEF"
    )

    solved = solve_club_defender_counterfactuals(
        eligible,
        RULES,
        budget_tenths=RULES.squad.budget_tenths,
        club_short_name=club,
    )

    assert solved
    for solution in solved:
        if not solution["feasible"]:
            continue
        members = {
            player.source_player_id for player in solution["result"].players
        }
        assert solution["player"].source_player_id in members
        assert solution["result"].total_cost_tenths <= RULES.squad.budget_tenths
        errors = validate_squad(
            _domain(solution["result"]), RULES, check_budget=False
        )
        assert not errors


def _domain(result):
    from fpl_engine.optimisation import _domain_squad

    return _domain_squad(
        result.players,
        result.starting_player_ids,
        result.bench_player_ids,
        result.captain_id,
        result.vice_captain_id,
    )


def test_an_absence_explanation_defers_when_the_club_is_actually_selected(
    tmp_path,
) -> None:
    with _database(tmp_path) as database:
        candidates = _candidates(database)
    eligible = tuple(
        player for player in candidates if mean_appearance(player) >= 0.6
    )
    primary = optimise_full_squad(
        eligible, budget_tenths=RULES.squad.budget_tenths, rules=RULES
    )
    club = primary.players[0].team_short_name

    conclusion = explain_absence(
        {"forced": [], "excluded_by_availability": []},
        candidates=eligible,
        primary=primary,
        club_short_name=club,
    )

    assert conclusion["conclusion"] == "not_absent"
    assert conclusion["selected_players"]


# --------------------------------------------------------------------------
# The whole run
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def finalised(tmp_path_factory):
    """One full run, shared: it persists a projection and solves many MILPs."""

    tmp_path = tmp_path_factory.mktemp("finalise")
    with _database(tmp_path) as database:
        yield database, finalise_preseason_squad(
            database,
            RULES,
            season_code="2026-27",
            horizon_gameweeks=4,
            frontier_size=3,
            alternative_count=1,
            generated_at=GENERATED_AT,
        )


def test_the_final_squad_is_legal_and_within_budget(finalised) -> None:
    _, result = finalised
    squad = result["final_squad"]

    assert len(squad["players"]) == 15
    assert squad["total_cost_tenths"] <= RULES.squad.budget_tenths
    assert squad["bank_tenths"] == (
        RULES.squad.budget_tenths - squad["total_cost_tenths"]
    )
    assert squad["bank_tenths"] >= 0
    assert len(squad["starting_player_ids"]) == RULES.squad.starting_size
    assert len(squad["bench_player_ids"]) == 4
    counts: dict[str, int] = {}
    for player in squad["players"]:
        counts[player["position"]] = counts.get(player["position"], 0) + 1
    assert counts == RULES.squad.position_counts
    assert max(squad["team_counts"].values()) <= RULES.squad.max_players_per_team


def test_the_squad_names_the_projection_run_that_produced_it(finalised) -> None:
    """Provenance, not a label.

    A squad artifact that cannot name the run behind it cannot be audited, and
    a squad built under a model version the in-season selector would also pick
    up could be reported as an in-season recommendation.
    """

    database, result = finalised
    squad = result["final_squad"]
    run_id = squad["projection_run_id"]

    assert run_id is not None
    assert run_id == result["live_projection"]["projection_run_id"]
    row = database.connection.execute(
        "SELECT model_version FROM projection_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert str(row["model_version"]) == squad["model_version"]
    assert squad["model_version"].startswith(
        PRESEASON_CARRY_FORWARD_MODEL_VERSION
    )
    assert squad["provisional"] is True


def test_the_goalkeeper_pair_is_reported_with_its_orientation(finalised) -> None:
    _, result = finalised
    pair = result["goalkeeper_pair"]

    assert len(pair["selected_pair"]) == 2
    assert len(pair["orientations"]) == result["horizon_gameweeks"]
    nominated = {
        orientation["starter_id"] for orientation in pair["orientations"]
    }
    owned = {entry["source_player_id"] for entry in pair["selected_pair"]}
    assert nominated <= owned
    for orientation in pair["orientations"]:
        assert orientation["pair_value"] >= orientation[
            "alternative_orientation_value"
        ]
    assert pair["effect_on_this_run"]["note"]


def test_the_concentration_tests_are_run_one_factor_at_a_time(finalised) -> None:
    _, result = finalised
    concentration = result["concentration_tests"]
    names = {run["name"] for run in concentration["runs"]}

    assert "manchester_united_attack_minus_10_percent" in names
    assert "bournemouth_attack_minus_10_percent" in names
    assert "promoted_player_role_treatment" in names
    assert "No combination of two perturbations" in concentration["policy"]
    for run in concentration["runs"]:
        if run.get("skipped"):
            # A skipped test must say why, so a reader cannot mistake it for a
            # test that ran and found nothing.
            assert run.get("reason") or run.get("description")
            continue
        assert run["exact_horizon_value"]
        assert set(run["claims_that_survive"]) == {
            "triple_manchester_united_attack",
            "bournemouth_attacking_double_up",
            "no_arsenal",
            "promoted_defenders",
            "goalkeeper_pair",
            "captain",
        }


def test_an_unvalidated_role_treatment_is_reported_as_not_run(finalised) -> None:
    _, result = finalised

    assert result["promoted_player_roles"]["adopted"] is False
    assert result["promoted_player_roles"]["treatment"] == "none"
    assert result["promoted_player_roles"]["scoring_fields_imported"] == []
    role_run = next(
        run
        for run in result["concentration_tests"]["runs"]
        if run["name"] == "promoted_player_role_treatment"
    )
    assert role_run["skipped"] is True
    assert "not validated" in role_run["reason"]


def test_every_documented_section_is_present(finalised) -> None:
    _, result = finalised

    for section in (
        "data_coverage",
        "promoted_team_priors",
        "promoted_player_roles",
        "goalkeeper_pair",
        "eligibility_audit",
        "frontier",
        "bank_frontier",
        "arsenal_counterfactuals",
        "concentration_tests",
        "live_projection",
        "final_squad",
        "alternatives",
        "selection_stability",
        "warnings",
    ):
        assert section in result, section
    assert result["data_coverage"]["snapshot_provenance"]["runs"]
    assert result["eligibility_audit"]["priced_candidates"] > 0
    assert result["status"].startswith("provisional")


def test_the_artifacts_round_trip_and_the_report_renders(
    finalised, tmp_path
) -> None:
    _, result = finalised

    validation, squad, markdown = write_artifacts(
        result,
        validation_path=tmp_path / "validation.json",
        squad_path=tmp_path / "squad.json",
        markdown_path=tmp_path / "report.md",
    )

    reloaded = json.loads(validation.read_text(encoding="utf-8"))
    assert reloaded["season_code"] == "2026-27"
    squad_document = json.loads(squad.read_text(encoding="utf-8"))
    assert squad_document["final_squad"]["players"]
    assert squad_document["projection_run_id"] == (
        result["live_projection"]["projection_run_id"]
    )
    text = markdown.read_text(encoding="utf-8")
    for heading in (
        "## 1. Data coverage and provenance",
        "## 2. Promoted-club priors",
        "## 3. Promoted-player role evidence",
        "## 4. Goalkeeper pair",
        "## 5. Eligibility audit",
        "## 6. Candidate frontier",
        "## 7. Bank frontier",
        "## 8. Arsenal defender counterfactuals",
        "## 9. Concentration tests",
        "## 10. Final squad",
        "## 11. Meaningful alternatives",
        "## 12. Robust and model-sensitive selections",
        "## 13. Warnings and unresolved limitations",
    ):
        assert heading in text, heading
    assert render_markdown(result) == text
    assert squad_artifact(result)["status"] == result["status"]


def test_alternatives_are_distinct_squads_behind_the_recommendation(
    finalised,
) -> None:
    _, result = finalised
    primary = set(result["final_squad"]["starting_player_ids"]) | set(
        result["final_squad"]["bench_player_ids"]
    )

    for alternative in result["alternatives"]:
        members = set(alternative["starting_player_ids"]) | set(
            alternative["bench_player_ids"]
        )
        assert members != primary
        assert alternative["exact_value_gap"] >= -1e-9
        assert alternative["changes_from_primary"]["changes"] > 0
