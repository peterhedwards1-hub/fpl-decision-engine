from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_promotion import FORWARD_SOURCE, _forward_bundle

from fpl_engine.capture import capture_gameweek_forecasts
from fpl_engine.config import load_season_rules
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    MODEL_VERSION,
    PRESEASON_V5_MODEL_CONFIG,
)
from fpl_engine.promotion import register_forward_candidate
from fpl_engine.prospective import build_prospective_capture_status

RULES = load_season_rules(Path("config/seasons/2026-27.json"))
# The forward bundle's GW1 deadline is 2026-08-14T17:30Z, with snapshots
# captured the day before.
BEFORE_DEADLINE = datetime(2026, 8, 14, 12, tzinfo=UTC)
AFTER_DEADLINE = datetime(2026, 8, 15, 12, tzinfo=UTC)


def _database(tmp_path, *, register: bool = False):
    database = HistoricalDatabase(tmp_path / "fpl.sqlite3")
    database.__enter__()
    database.initialise()
    database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
    if register:
        register_forward_candidate(
            database,
            candidate_key="preseason-priors-v1",
            season_code="2026-27",
            model_version="preseason-priors-v1",
            model_config=asdict(PRESEASON_V5_MODEL_CONFIG),
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )
    return database


def test_capture_persists_the_incumbent_and_every_declared_candidate(
    tmp_path,
) -> None:
    database = _database(tmp_path, register=True)
    try:
        capture = capture_gameweek_forecasts(
            database,
            RULES,
            season_code="2026-27",
            gameweek=1,
            horizon_gameweeks=2,
            generated_at=BEFORE_DEADLINE,
        )
    finally:
        database.__exit__(None, None, None)

    roles = [forecast["role"] for forecast in capture["forecasts"]]
    # A challenger run with nothing to compare against is not evidence.
    assert roles == ["incumbent", "challenger"]
    incumbent, challenger = capture["forecasts"]
    assert incumbent["model_version"] == MODEL_VERSION
    assert challenger["key"] == "preseason-priors-v1"
    assert incumbent["projection_run_id"] != challenger["projection_run_id"]
    assert incumbent["projection_rows"] > 0
    assert challenger["projection_rows"] > 0
    # The ingestion run that produced the snapshot is recorded, so the forecast
    # can be tied back to the data it was generated from.
    assert capture["source_ingestion_run_id"] is not None
    assert capture["captured_at"] < capture["deadline_time"]


def test_capture_fails_closed_once_the_deadline_has_passed(tmp_path) -> None:
    database = _database(tmp_path)
    try:
        with pytest.raises(ValueError, match="deadline has passed"):
            capture_gameweek_forecasts(
                database,
                RULES,
                season_code="2026-27",
                gameweek=1,
                generated_at=AFTER_DEADLINE,
            )
        # Nothing may be written after the fact.
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM projection_runs"
            ).fetchone()[0]
            == 0
        )
    finally:
        database.__exit__(None, None, None)


def test_capture_refuses_a_candidate_sharing_the_incumbent_version(
    tmp_path,
) -> None:
    database = _database(tmp_path)
    try:
        register_forward_candidate(
            database,
            candidate_key="clashing",
            season_code="2026-27",
            model_version=MODEL_VERSION,
            model_config=asdict(CORRECTED_V4_MODEL_CONFIG),
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="distinct model versions"):
            capture_gameweek_forecasts(
                database,
                RULES,
                season_code="2026-27",
                gameweek=1,
                generated_at=BEFORE_DEADLINE,
            )
    finally:
        database.__exit__(None, None, None)


def test_capture_requires_a_recorded_deadline(tmp_path) -> None:
    database = _database(tmp_path)
    try:
        database.connection.execute(
            "UPDATE gameweeks SET deadline_time = NULL WHERE number = 1"
        )
        database.connection.commit()
        with pytest.raises(ValueError, match="no recorded deadline"):
            capture_gameweek_forecasts(
                database,
                RULES,
                season_code="2026-27",
                gameweek=1,
                generated_at=BEFORE_DEADLINE,
            )
    finally:
        database.__exit__(None, None, None)


def _status(database, as_of: datetime):
    return build_prospective_capture_status(database, "2026-27", as_of=as_of)


def test_a_passed_deadline_requires_an_incumbent_projection(tmp_path) -> None:
    database = _database(tmp_path, register=True)
    try:
        before = _status(database, AFTER_DEADLINE)["gameweeks"][0]
        assert "incumbent_projection" in before["missing_required"]
        assert before["incumbent_projection"]["valid_pre_deadline_runs"] == 0

        capture_gameweek_forecasts(
            database,
            RULES,
            season_code="2026-27",
            gameweek=1,
            horizon_gameweeks=2,
            generated_at=BEFORE_DEADLINE,
        )
        after = _status(database, AFTER_DEADLINE)["gameweeks"][0]
    finally:
        database.__exit__(None, None, None)

    assert after["incumbent_projection"]["valid_pre_deadline_runs"] >= 1
    assert "incumbent_projection" not in after["missing_required"]
    # The candidate is satisfied by the same capture.
    assert "candidate_projection:preseason-priors-v1" not in (
        after["missing_required"]
    )


def test_a_decision_record_without_its_choices_is_incomplete(tmp_path) -> None:
    database = _database(tmp_path)
    try:
        status = _status(database, AFTER_DEADLINE)["gameweeks"][0]
        record = status["decision_record"]
        assert record["final_runs"] == 0
        assert record["complete"] is False
        # Existence is not enough: the week must record what was chosen.
        assert set(record["missing_fields"]) == {
            "squad",
            "starting_xi",
            "captain",
            "vice_captain",
            "bench_order",
        }
        assert "complete_decision_record" in status["missing_required"]
    finally:
        database.__exit__(None, None, None)


def test_the_capture_policy_states_what_it_now_requires(tmp_path) -> None:
    database = _database(tmp_path)
    try:
        policy = _status(database, AFTER_DEADLINE)["policy"]
    finally:
        database.__exit__(None, None, None)

    joined = " ".join(policy)
    assert "incumbent requires the same" in joined
    assert "run of nulls is not evidence" in joined
    assert "bench order" in joined


def test_capture_output_is_a_complete_record_of_what_was_frozen(
    tmp_path,
) -> None:
    database = _database(tmp_path, register=True)
    try:
        capture = capture_gameweek_forecasts(
            database,
            RULES,
            season_code="2026-27",
            gameweek=1,
            horizon_gameweeks=2,
            generated_at=BEFORE_DEADLINE,
        )
    finally:
        database.__exit__(None, None, None)

    # Everything needed to reconstruct which model, from which data, at what
    # time — the part that cannot be recovered after the deadline.
    assert set(capture) >= {
        "season_code",
        "gameweek",
        "horizon_gameweeks",
        "captured_at",
        "deadline_time",
        "source_ingestion_run_id",
        "snapshot_observed_at",
        "forecasts",
        "declared_candidates",
    }
    for forecast in capture["forecasts"]:
        assert len(forecast["model_config_sha256"]) == 64
    assert json.loads(json.dumps(capture)) == capture
