"""The candidate changes two things; the evaluation has to separate them."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.allocation_variants import (
    ALLOCATION_VARIANTS,
    VARIANT_CONTRASTS,
    evaluate_allocation_variants,
)
from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def test_the_four_variants_cross_both_factors() -> None:
    """A two-by-two design, or the contrasts do not isolate anything."""

    cells = {
        (spec["team_strength"], spec["allocation"])
        for spec in ALLOCATION_VARIANTS.values()
    }
    assert cells == {
        ("existing", "player_rate"),
        ("opponent_adjusted", "player_rate"),
        ("existing", "team_share"),
        ("opponent_adjusted", "team_share"),
    }
    # Each contrast must move exactly one factor, except the one that is
    # explicitly labelled as not attributable to either.
    for treatment, control, isolates in VARIANT_CONTRASTS:
        first = ALLOCATION_VARIANTS[treatment]
        second = ALLOCATION_VARIANTS[control]
        changed = sum(
            first[factor] != second[factor]
            for factor in ("team_strength", "allocation")
        )
        if "Not attributable" in isolates:
            assert changed == 2
        else:
            assert changed == 1, f"{treatment} vs {control} moves {changed} factors"


def test_the_double_counting_variant_is_declared_unsound() -> None:
    """B must be measured and must not be mistaken for a candidate."""

    unsound = [
        name for name, spec in ALLOCATION_VARIANTS.items() if not spec["sound"]
    ]
    assert unsound == ["B_opponent_adjusted_rate_allocation"]
    spec = ALLOCATION_VARIANTS[unsound[0]]
    assert spec["team_strength"] == "opponent_adjusted"
    assert spec["allocation"] == "player_rate"
    assert "double-count" in spec["note"]
    # It is still a runnable configuration, so the claim rests on a number.
    assert spec["config"].team_strength_model == "opponent_adjusted"
    assert spec["config"].scoring_event_source == "actual"


def test_every_variant_is_scored_on_the_same_measures(tmp_path) -> None:
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        report = evaluate_allocation_variants(
            database,
            RULES,
            season_code="2026-27",
            origin_gameweek_start=1,
            origin_gameweek_end=3,
            horizon_gameweeks=1,
            # The MILP replay dominates runtime and is exercised elsewhere.
            include_transfer_regret=False,
        )
    finally:
        database.__exit__(None, None, None)

    assert set(report["variants"]) == set(ALLOCATION_VARIANTS)
    run_ids = {
        entry["backtest_run_id"] for entry in report["variants"].values()
    }
    # Four distinct runs over identical origins, so the comparison is paired.
    assert len(run_ids) == 4
    for name, entry in report["variants"].items():
        assert entry["player_points"]["observations"] > 0
        assert entry["player_points"]["rmse"] >= 0
        assert entry["top_player_calibration"], name
        assert entry["legal_squad_regret"]["origins"] > 0
        assert entry["owned_captain_regret"]["mean"] >= 0

    assert len(report["contrasts"]) == len(VARIANT_CONTRASTS)
    for contrast in report["contrasts"]:
        assert contrast["differences"]
        assert "player_points_rmse" in contrast["differences"]
    assert any(
        "structurally unsound" in line for line in report["limitations"]
    )


def test_the_isolating_contrast_holds_allocation_fixed() -> None:
    """D against C is the contrast that answers the actual question."""

    treatment, control, isolates = VARIANT_CONTRASTS[0]
    assert treatment == "D_opponent_adjusted_share_allocation"
    assert control == "C_existing_strength_share_allocation"
    assert (
        ALLOCATION_VARIANTS[treatment]["allocation"]
        == ALLOCATION_VARIANTS[control]["allocation"]
        == "team_share"
    )
    assert (
        ALLOCATION_VARIANTS[treatment]["team_strength"]
        != ALLOCATION_VARIANTS[control]["team_strength"]
    )
    assert "marginal contribution of opponent adjustment" in isolates


def test_an_unknown_variant_name_is_not_silently_dropped(tmp_path) -> None:
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    try:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        with pytest.raises(KeyError):
            evaluate_allocation_variants(
                database,
                RULES,
                season_code="2026-27",
                origin_gameweek_start=1,
                origin_gameweek_end=2,
                variants={"broken": {"team_strength": "existing"}},
                include_transfer_regret=False,
            )
    finally:
        database.__exit__(None, None, None)
