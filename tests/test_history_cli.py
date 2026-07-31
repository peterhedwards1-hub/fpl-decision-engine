from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from test_promotion import (
    FORWARD_SOURCE,
    _forward_bundle,
    _register_preseason_candidate,
)

from fpl_engine.history.cli import main
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    MODEL_VERSION,
    PRESEASON_V5_MODEL_CONFIG,
)

CANDIDATE_CONFIG = "config/model_candidates/preseason-priors-v1.json"
CONTROL_CONFIG = "config/model_candidates/preseason-priors-v1-incumbent.json"


def _seeded_database(tmp_path, *, register: bool = False):
    path = tmp_path / "fpl.sqlite3"
    with HistoricalDatabase(path) as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        if register:
            _register_preseason_candidate(database)
    return path


def _run(monkeypatch, capsys, database_path, *argv: str) -> dict:
    monkeypatch.setattr(
        "sys.argv",
        ["fpl-history", "--database", str(database_path), *argv],
    )
    main()
    return json.loads(capsys.readouterr().out)


def _stored_config(database_path, run_id: int) -> dict:
    with HistoricalDatabase(database_path) as database:
        row = database.connection.execute(
            "SELECT model_config_json FROM projection_backtest_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return json.loads(row["model_config_json"])


def test_backtest_defaults_to_the_incumbent_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = _seeded_database(tmp_path)

    report = _run(
        monkeypatch,
        capsys,
        database_path,
        "backtest-projections",
        "2026-27",
        "--origin-start",
        "2",
        "--origin-end",
        "3",
    )

    assert _stored_config(database_path, report["backtest_run_id"]) == asdict(
        CORRECTED_V4_MODEL_CONFIG
    )


def test_backtest_runs_a_declared_configuration_file(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = _seeded_database(tmp_path)

    report = _run(
        monkeypatch,
        capsys,
        database_path,
        "backtest-projections",
        "2026-27",
        "--origin-start",
        "2",
        "--origin-end",
        "3",
        "--model-config",
        CANDIDATE_CONFIG,
    )

    # Without this flag the Stage 3a fields were unreachable from the CLI, so no
    # run could ever match the preseason-priors-v1 declaration.
    stored = _stored_config(database_path, report["backtest_run_id"])
    assert stored == asdict(PRESEASON_V5_MODEL_CONFIG)
    assert stored["team_strength_carry_forward"] is True
    assert stored["cold_start_prior"] == "position_price"


def test_backtest_flags_override_single_fields_of_a_declared_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = _seeded_database(tmp_path)

    report = _run(
        monkeypatch,
        capsys,
        database_path,
        "backtest-projections",
        "2026-27",
        "--origin-start",
        "2",
        "--origin-end",
        "3",
        "--model-config",
        CANDIDATE_CONFIG,
        "--recent-gameweeks",
        "6",
    )

    stored = _stored_config(database_path, report["backtest_run_id"])
    assert stored["recent_gameweeks"] == 6
    assert stored["team_strength_carry_forward"] is True
    unchanged = {
        key: value
        for key, value in asdict(PRESEASON_V5_MODEL_CONFIG).items()
        if key != "recent_gameweeks"
    }
    assert {key: stored[key] for key in unchanged} == unchanged


def test_candidate_backtest_and_decision_evidence_feed_the_gate(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = _seeded_database(tmp_path, register=True)

    pair = _run(
        monkeypatch,
        capsys,
        database_path,
        "backtest-forward-candidate",
        "preseason-priors-v1",
        "--incumbent-config",
        CONTROL_CONFIG,
        "--origin-start",
        "1",
        "--origin-end",
        "3",
    )

    assert pair["evidence_policy"] == "pre_deadline_only"
    assert pair["incumbent_model_version"] == MODEL_VERSION
    assert _stored_config(database_path, pair["challenger_run_id"]) == asdict(
        PRESEASON_V5_MODEL_CONFIG
    )
    assert _stored_config(database_path, pair["incumbent_run_id"]) == asdict(
        CORRECTED_V4_MODEL_CONFIG
    )

    evidence_path = tmp_path / "decision-evidence.json"
    evidence = _run(
        monkeypatch,
        capsys,
        database_path,
        "build-decision-evidence",
        "--incumbent-run",
        str(pair["incumbent_run_id"]),
        "--challenger-run",
        str(pair["challenger_run_id"]),
        "--owned-captain-regret-change",
        "0.0",
        "--transfer-regret-change",
        "0.0",
        "--output",
        str(evidence_path),
    )

    assert set(evidence) == {
        "legal_squad_regret_change",
        "owned_captain_regret_change",
        "transfer_regret_change",
        "source_report",
    }
    assert json.loads(evidence_path.read_text()) == evidence


def test_candidate_backtest_refuses_an_unregistered_candidate(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = _seeded_database(tmp_path)

    with pytest.raises(ValueError, match="not registered"):
        _run(
            monkeypatch,
            capsys,
            database_path,
            "backtest-forward-candidate",
            "preseason-priors-v1",
            "--incumbent-config",
            CONTROL_CONFIG,
        )
