"""What previous-division goals are allowed to say about a promoted club.

The differentiated prior is a small formula with two properties that carry the
whole argument: it must leave the promoted cohort's average where the declared
prior put it, and it must not be able to read the season it is forecasting.
Both are asserted here on data whose right answer is arithmetic rather than
football.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from fpl_engine.championship import (
    BASE_PROMOTED_ATTACK,
    BASE_PROMOTED_DEFENCE,
    MAXIMUM_PROMOTED_ATTACK,
    MINIMUM_PROMOTED_DEFENCE,
    ChampionshipDataError,
    championship_coverage,
    cohort_mean_multipliers,
    import_championship_document,
    load_championship_document,
    promoted_club_priors,
)
from fpl_engine.history.database import HistoricalDatabase


def _document(
    season_code: str = "2025-26",
    *,
    clubs: dict[str, tuple[int, int]] | None = None,
    aliases: dict[str, str] | None = None,
) -> dict:
    """A synthetic division whose totals balance by construction."""

    clubs = clubs or {
        "Runaway FC": (92, 30),
        "Solid FC": (60, 45),
        "Scraped Through FC": (46, 60),
        "Also Ran FC": (30, 93),
    }
    scored = sum(goals for goals, _ in clubs.values())
    conceded = sum(against for _, against in clubs.values())
    assert scored == conceded, "test data must balance"
    return {
        "format": "fpl-decision-engine/championship-season-goals/v1",
        "source": {
            "name": "synthetic",
            "url": "https://example.invalid/synthetic",
            "revision": "test",
            "retrieved_at": "2026-06-01T00:00:00+00:00",
        },
        "team_name_aliases": aliases or {},
        "seasons": [
            {
                "season_code": season_code,
                "competition": "english-championship",
                "stage": "regular",
                "matches": 92,
                "teams": [
                    {
                        "name": name,
                        "matches": 46,
                        "goals_for": goals,
                        "goals_against": against,
                    }
                    for name, (goals, against) in clubs.items()
                ],
                "source_url": "https://example.invalid/synthetic/2025-26",
                "source_sha256": "0" * 64,
            }
        ],
    }


def _write(tmp_path, payload: dict):
    path = tmp_path / "championship.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _database(tmp_path, payload: dict) -> HistoricalDatabase:
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    import_championship_document(
        database,
        load_championship_document(_write(tmp_path, payload)),
        imported_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    return database


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


def test_a_file_whose_goals_do_not_balance_is_rejected(tmp_path) -> None:
    payload = _document()
    payload["seasons"][0]["teams"][0]["goals_for"] += 5

    with pytest.raises(ChampionshipDataError, match="both sides of the ledger"):
        load_championship_document(_write(tmp_path, payload))


def test_a_file_without_provenance_is_rejected(tmp_path) -> None:
    payload = _document()
    payload["source"] = {"name": "synthetic"}

    with pytest.raises(ChampionshipDataError, match="source name and url"):
        load_championship_document(_write(tmp_path, payload))


def test_reimporting_the_same_season_replaces_rather_than_duplicates(
    tmp_path,
) -> None:
    with _database(tmp_path, _document()) as database:
        import_championship_document(
            database, load_championship_document(_write(tmp_path, _document()))
        )
        coverage = championship_coverage(database)

    assert len(coverage["seasons"]) == 1
    assert coverage["seasons"][0]["clubs"] == 4
    assert coverage["seasons"][0]["source_sha256"] == "0" * 64


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("weight", [0.0, 0.25, 0.5, 0.75])
def test_the_promoted_cohort_mean_stays_on_the_declared_prior(
    tmp_path, weight
) -> None:
    """The whole point of normalising.

    Promoted clubs are the top of the division they left, so their goal rates
    average well above the divisional mean. Applied literally the formula would
    lift the cohort toward league average and quietly replace the declared
    prior with a claim nothing here supports.
    """

    # A cohort whose spread is wide enough to differentiate and narrow enough
    # that no declared bound binds, so the invariant is tested rather than the
    # clamp.
    payload = _document(
        clubs={
            "Runaway FC": (60, 40),
            "Solid FC": (55, 45),
            "Scraped Through FC": (50, 52),
            "Also Ran FC": (40, 68),
        }
    )
    with _database(tmp_path, payload) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway FC", "Solid FC", "Scraped Through FC"),
            weight=weight,
        )

    assert not any(
        prior.attack_bound_applied or prior.defence_bound_applied
        for prior in priors.values()
    )
    mean_attack, mean_defence = cohort_mean_multipliers(priors)
    assert mean_attack == pytest.approx(BASE_PROMOTED_ATTACK, abs=1e-9)
    assert mean_defence == pytest.approx(BASE_PROMOTED_DEFENCE, abs=1e-9)


def test_zero_weight_reproduces_the_fixed_prior_exactly(tmp_path) -> None:
    with _database(tmp_path, _document()) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway FC", "Solid FC", "Scraped Through FC"),
            weight=0.0,
        )

    assert {prior.attack_multiplier for prior in priors.values()} == {
        BASE_PROMOTED_ATTACK
    }
    assert {prior.defence_multiplier for prior in priors.values()} == {
        BASE_PROMOTED_DEFENCE
    }


def test_a_stronger_championship_club_gets_the_stronger_prior(tmp_path) -> None:
    with _database(tmp_path, _document()) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway FC", "Solid FC", "Scraped Through FC"),
            weight=0.5,
        )

    assert (
        priors["Runaway FC"].attack_multiplier
        > priors["Solid FC"].attack_multiplier
        > priors["Scraped Through FC"].attack_multiplier
    )
    # The club that conceded most is rated most vulnerable.
    assert (
        priors["Scraped Through FC"].defence_multiplier
        > priors["Solid FC"].defence_multiplier
        > priors["Runaway FC"].defence_multiplier
    )


def test_the_declared_bounds_never_rate_a_promoted_club_above_average(
    tmp_path,
) -> None:
    """A club can be the best in the Championship and still not be league average."""

    payload = _document(
        clubs={
            "Colossus FC": (150, 10),
            "Nearly FC": (60, 50),
            "Barely FC": (40, 90),
            "Sacrificial FC": (10, 110),
        }
    )
    with _database(tmp_path, payload) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Colossus FC", "Nearly FC", "Barely FC"),
            weight=0.75,
        )

    assert priors["Colossus FC"].attack_multiplier <= MAXIMUM_PROMOTED_ATTACK
    assert priors["Colossus FC"].attack_bound_applied
    assert all(
        prior.defence_multiplier >= MINIMUM_PROMOTED_DEFENCE
        for prior in priors.values()
    )


def test_an_unmatched_club_keeps_the_fixed_prior_and_says_so(tmp_path) -> None:
    with _database(tmp_path, _document()) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway FC", "Nobody FC"),
            weight=0.5,
        )

    assert priors["Nobody FC"].matched is False
    assert priors["Nobody FC"].attack_multiplier == BASE_PROMOTED_ATTACK
    assert "could not be matched" in priors["Nobody FC"].reason


def test_aliases_link_a_premier_league_name_to_its_championship_name(
    tmp_path,
) -> None:
    payload = _document(aliases={"Runaway FC": "Runaway"})
    with _database(tmp_path, payload) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway",),
            weight=0.5,
        )

    assert priors["Runaway"].matched
    assert priors["Runaway"].championship_name == "Runaway FC"


# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------


def test_a_prior_reads_only_the_season_it_was_asked_for(tmp_path) -> None:
    """A later Championship season cannot reach an earlier prior.

    The seasons are deliberately opposites: a club that walked one division
    was bottom of the other. If the wrong season were read the multipliers
    would invert, and the assertion below would fail loudly rather than drift.
    """

    earlier = _document("2024-25")
    later = _document(
        "2025-26",
        clubs={
            "Runaway FC": (30, 93),
            "Solid FC": (46, 60),
            "Scraped Through FC": (60, 45),
            "Also Ran FC": (92, 30),
        },
    )
    with _database(tmp_path, earlier) as database:
        import_championship_document(
            database, load_championship_document(_write(tmp_path, later))
        )
        from_earlier = promoted_club_priors(
            database,
            championship_season_code="2024-25",
            promoted_fpl_names=("Runaway FC", "Solid FC", "Scraped Through FC"),
            weight=0.5,
        )
        from_later = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway FC", "Solid FC", "Scraped Through FC"),
            weight=0.5,
        )

    assert (
        from_earlier["Runaway FC"].attack_multiplier
        > from_earlier["Scraped Through FC"].attack_multiplier
    )
    assert (
        from_later["Runaway FC"].attack_multiplier
        < from_later["Scraped Through FC"].attack_multiplier
    )


def test_a_missing_championship_season_falls_back_rather_than_guessing(
    tmp_path,
) -> None:
    with _database(tmp_path, _document()) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2019-20",
            promoted_fpl_names=("Runaway FC",),
            weight=0.75,
        )

    assert priors["Runaway FC"].matched is False
    assert priors["Runaway FC"].attack_multiplier == BASE_PROMOTED_ATTACK
    assert "No Championship record" in priors["Runaway FC"].reason


def test_the_shipped_reference_file_loads_and_balances() -> None:
    """The file that actually feeds the live decision, checked as data."""

    document = load_championship_document()

    assert document.source["name"]
    assert document.source["url"]
    assert len(document.seasons) >= 4
    for season in document.seasons:
        assert season.source_sha256
        assert len(season.teams) == 24
        assert season.team_matches == sum(team.matches for team in season.teams)
        assert sum(team.goals_for for team in season.teams) == sum(
            team.goals_against for team in season.teams
        )


def test_a_declared_bound_moves_the_realised_cohort_mean_and_is_flagged(
    tmp_path,
) -> None:
    """The invariant holds before the bounds, and the bounds are visible.

    A cohort containing a club that outscored its division by sixty per cent
    hits the attack cap, which pulls the realised mean below the declared
    prior. That is the bound doing its job, and the artifact has to be able to
    say so rather than quietly reporting a mean that is not 0.85.
    """

    with _database(tmp_path, _document()) as database:
        priors = promoted_club_priors(
            database,
            championship_season_code="2025-26",
            promoted_fpl_names=("Runaway FC", "Solid FC", "Scraped Through FC"),
            weight=0.75,
        )

    assert priors["Runaway FC"].attack_bound_applied
    mean_attack, _ = cohort_mean_multipliers(priors)
    assert mean_attack < BASE_PROMOTED_ATTACK
