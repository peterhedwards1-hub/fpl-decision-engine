from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.readiness import build_preseason_readiness_report

RULES = load_season_rules(Path("config/seasons/2026-27.json"))


def test_readiness_report_fails_closed_without_a_production_projection(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (code, name) VALUES ('2026-27', '2026/27')"
        )

        report = build_preseason_readiness_report(
            database,
            RULES,
            season_code="2026-27",
        )

    assert not report["ready_for_provisional_selection"]
    assert not report["ready_to_submit"]
    assert report["production_projection"] is None
    assert report["recommendation"] is None
    assert "qualified incumbent projection" in report["blockers"][0]


def test_readiness_report_validates_appearance_policy(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        with pytest.raises(ValueError, match="between zero and one"):
            build_preseason_readiness_report(
                database,
                RULES,
                season_code="2026-27",
                minimum_mean_appearance=1.1,
            )
