import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_engine.config import load_season_rules
from fpl_engine.domain import Position
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    MODEL_VERSION,
    PRESEASON_V5_MODEL_CONFIG,
)
from fpl_engine.promotion import (
    DecisionGateEvidence,
    PromotionGatePolicy,
    build_decision_gate_evidence,
    declared_challenger_config,
    evaluate_forward_candidate,
    load_forward_candidate,
    register_forward_candidate,
    run_forward_candidate_pair,
)
from fpl_engine.prospective import build_prospective_capture_status


def test_forward_candidate_registration_is_hashed_and_immutable(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            """
            INSERT INTO seasons (code, name)
            VALUES ('2026-27', '2026/27')
            """
        )
        database.connection.commit()

        registration = register_forward_candidate(
            database,
            candidate_key="team-share-v1",
            season_code="2026-27",
            model_version="team-share-v1",
            model_config={"scoring_event_source": "team_share_expected"},
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        assert len(registration["model_config_sha256"]) == 64
        stored = database.connection.execute(
            """
            SELECT model_config_json, status
            FROM model_candidate_registrations
            """
        ).fetchone()
        assert json.loads(stored["model_config_json"]) == {
            "scoring_event_source": "team_share_expected"
        }
        assert stored["status"] == "declared"
        with pytest.raises(
            sqlite3.IntegrityError,
            match="declarations are immutable",
        ):
            database.connection.execute(
                """
                UPDATE model_candidate_registrations
                SET model_version = 'changed'
                """
            )


def test_historical_candidate_registration_is_rejected(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            "INSERT INTO seasons (code, name) VALUES ('2025-26', '2025/26')"
        )
        database.connection.commit()

        with pytest.raises(ValueError, match="forward seasons"):
            register_forward_candidate(
                database,
                candidate_key="leaking",
                season_code="2025-26",
                model_version="bad",
                model_config={},
            )


def test_prospective_status_requires_each_registered_candidate_run(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.execute(
            """
            INSERT INTO seasons (id, code, name)
            VALUES (1, '2026-27', '2026/27')
            """
        )
        database.connection.execute(
            """
            INSERT INTO ingestion_runs (
                id, source_name, retrieved_at, status
            ) VALUES (
                1, 'test', '2026-07-30T00:00:00+00:00', 'completed'
            )
            """
        )
        database.connection.execute(
            """
            INSERT INTO gameweeks (
                season_id, number, deadline_time, provenance_run_id
            ) VALUES (
                1, 1, '2026-08-21T17:30:00+00:00', 1
            )
            """
        )
        database.connection.commit()
        register_forward_candidate(
            database,
            candidate_key="candidate",
            season_code="2026-27",
            model_version="candidate-v1",
            model_config={"value": 1},
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        report = build_prospective_capture_status(
            database,
            "2026-27",
            as_of=datetime(2026, 8, 22, tzinfo=UTC),
        )

    assert "candidate_projection:candidate" in (
        report["gameweeks"][0]["missing_required"]
    )
    assert (
        report["gameweeks"][0]["candidate_projections"]["candidate"][
            "valid_pre_deadline_runs"
        ]
        == 0
    )


def test_matched_forward_rows_can_qualify_declared_candidate(
    tmp_path,
) -> None:
    policy = PromotionGatePolicy(
        minimum_samples=4,
        minimum_position_samples=1,
        bootstrap_samples=100,
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.executescript(
            """
            INSERT INTO ingestion_runs (
                id, source_name, retrieved_at, status
            ) VALUES (
                1, 'test', '2026-07-30T00:00:00+00:00', 'completed'
            );
            INSERT INTO seasons (id, code, name)
            VALUES (1, '2026-27', '2026/27');
            INSERT INTO teams (
                id, season_id, identifier_namespace, source_team_id,
                name, short_name, provenance_run_id
            ) VALUES
                (1, 1, 'official-fpl', '1', 'One', 'ONE', 1),
                (2, 1, 'official-fpl', '2', 'Two', 'TWO', 1);
            INSERT INTO gameweeks (
                id, season_id, number, deadline_time, provenance_run_id
            ) VALUES (
                1, 1, 1, '2026-08-21T17:30:00+00:00', 1
            );
            """
        )
        for index, position in enumerate(
            ("GK", "DEF", "MID", "FWD"),
            start=1,
        ):
            database.connection.execute(
                """
                INSERT INTO players (id, web_name, provenance_run_id)
                VALUES (?, ?, 1)
                """,
                (index, position),
            )
            database.connection.execute(
                """
                INSERT INTO player_seasons (
                    id, season_id, player_id, identifier_namespace,
                    source_player_id, team_id, position, provenance_run_id
                ) VALUES (?, 1, ?, 'official-fpl', ?, 1, ?, 1)
                """,
                (index, index, str(index), position),
            )
        run_values = (
            (
                1,
                "incumbent",
                '{"value":0}',
            ),
            (
                2,
                "candidate-v1",
                '{"value":1}',
            ),
        )
        for run_id, version, config in run_values:
            database.connection.execute(
                """
                INSERT INTO projection_backtest_runs (
                    id, season_id, model_version, created_at,
                    origin_gameweek_start, origin_gameweek_end,
                    horizon_gameweeks, evidence_policy,
                    model_config_json, limitations_json, status,
                    prediction_count
                ) VALUES (
                    ?, 1, ?, '2026-08-22T00:00:00+00:00',
                    1, 1, 1, 'pre_deadline_only',
                    ?, '[]', 'completed', 4
                )
                """,
                (run_id, version, config),
            )
            for player_id in range(1, 5):
                database.connection.execute(
                    """
                    INSERT INTO projection_backtest_predictions (
                        backtest_run_id, origin_gameweek,
                        target_gameweek, horizon_step,
                        player_season_id, fixture_count,
                        expected_minutes, appearance_probability,
                        sixty_probability, actual_minutes,
                        expected_points, actual_points, uncertainty
                    ) VALUES (
                        ?, 1, 1, 1, ?, 1, 75, 0.9, 0.8,
                        90, 2, 2, 1
                    )
                    """,
                    (run_id, player_id),
                )
        database.connection.commit()
        register_forward_candidate(
            database,
            candidate_key="candidate",
            season_code="2026-27",
            model_version="candidate-v1",
            model_config={"value": 1},
            gate_policy=policy,
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        report = evaluate_forward_candidate(
            database,
            candidate_key="candidate",
            incumbent_run_ids=(1,),
            challenger_run_ids=(2,),
            decision_evidence=DecisionGateEvidence(
                legal_squad_regret_change=0,
                owned_captain_regret_change=0,
                transfer_regret_change=0,
                source_report="test",
            ),
        )

        assert report["passed"] is True
        assert report["status"] == "qualified"
        assert database.connection.execute(
            """
            SELECT status FROM model_candidate_registrations
            WHERE candidate_key = 'candidate'
            """
        ).fetchone()[0] == "qualified"


def test_probability_gate_fails_cleanly_without_single_fixture_rows(
    tmp_path,
) -> None:
    policy = PromotionGatePolicy(
        minimum_samples=4,
        minimum_position_samples=1,
        bootstrap_samples=100,
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.connection.executescript(
            """
            INSERT INTO ingestion_runs (
                id, source_name, retrieved_at, status
            ) VALUES (
                1, 'test', '2026-07-30T00:00:00+00:00', 'completed'
            );
            INSERT INTO seasons (id, code, name)
            VALUES (1, '2026-27', '2026/27');
            INSERT INTO teams (
                id, season_id, identifier_namespace, source_team_id,
                name, short_name, provenance_run_id
            ) VALUES
                (1, 1, 'official-fpl', '1', 'One', 'ONE', 1),
                (2, 1, 'official-fpl', '2', 'Two', 'TWO', 1);
            INSERT INTO gameweeks (
                id, season_id, number, deadline_time, provenance_run_id
            ) VALUES (
                1, 1, 1, '2026-08-21T17:30:00+00:00', 1
            );
            """
        )
        for index, position in enumerate(("GK", "DEF", "MID", "FWD"), start=1):
            database.connection.execute(
                """
                INSERT INTO players (id, web_name, provenance_run_id)
                VALUES (?, ?, 1)
                """,
                (index, position),
            )
            database.connection.execute(
                """
                INSERT INTO player_seasons (
                    id, season_id, player_id, identifier_namespace,
                    source_player_id, team_id, position, provenance_run_id
                ) VALUES (?, 1, ?, 'official-fpl', ?, 1, ?, 1)
                """,
                (index, index, str(index), position),
            )
        for run_id, version, config in (
            (1, "incumbent", '{"value":0}'),
            (2, "candidate-v1", '{"value":1}'),
        ):
            database.connection.execute(
                """
                INSERT INTO projection_backtest_runs (
                    id, season_id, model_version, created_at,
                    origin_gameweek_start, origin_gameweek_end,
                    horizon_gameweeks, evidence_policy,
                    model_config_json, limitations_json, status,
                    prediction_count
                ) VALUES (
                    ?, 1, ?, '2026-08-22T00:00:00+00:00',
                    1, 1, 1, 'pre_deadline_only',
                    ?, '[]', 'completed', 4
                )
                """,
                (run_id, version, config),
            )
            for player_id in range(1, 5):
                database.connection.execute(
                    """
                    INSERT INTO projection_backtest_predictions (
                        backtest_run_id, origin_gameweek,
                        target_gameweek, horizon_step,
                        player_season_id, fixture_count,
                        expected_minutes, appearance_probability,
                        sixty_probability, actual_minutes,
                        expected_points, actual_points, uncertainty
                    ) VALUES (
                        ?, 1, 1, 1, ?, 2, 75, 0.9, 0.8,
                        90, 2, 2, 1
                    )
                    """,
                    (run_id, player_id),
                )
        database.connection.commit()
        register_forward_candidate(
            database,
            candidate_key="candidate",
            season_code="2026-27",
            model_version="candidate-v1",
            model_config={"value": 1},
            gate_policy=policy,
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

        report = evaluate_forward_candidate(
            database,
            candidate_key="candidate",
            incumbent_run_ids=(1,),
            challenger_run_ids=(2,),
            decision_evidence=DecisionGateEvidence(
                legal_squad_regret_change=0,
                owned_captain_regret_change=0,
                transfer_regret_change=0,
                source_report="test",
            ),
        )

    assert report["passed"] is False
    assert report["gates"]["probability_evidence"] is False
    assert report["probability"]["samples"] == 0


FORWARD_RULES = load_season_rules(Path("config/seasons/2026-27.json"))
FORWARD_SOURCE = IngestionSource(
    name="forward-test",
    retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    identifier_namespace="official-fpl",
)
SQUAD_SHAPE = (
    ("GK", Position.GK, 3),
    ("DEF", Position.DEF, 6),
    ("MID", Position.MID, 6),
    ("FWD", Position.FWD, 5),
)


def _forward_bundle() -> HistoricalBundle:
    """A 2026/27 season large enough to select a legal £100m squad each origin."""

    teams = tuple(
        TeamRecord(str(index), f"Club {index}", f"C{index}")
        for index in range(1, 9)
    )
    players = []
    player_seasons = []
    identifier = 0
    for label, position, count in SQUAD_SHAPE:
        for slot in range(count):
            identifier += 1
            source_id = str(1000 + identifier)
            players.append(
                PlayerRecord(
                    source_id,
                    f"{label}{slot}",
                    f"{label}{slot}",
                    f"{label} {slot}",
                    official_fpl_code=str(9000 + identifier),
                )
            )
            player_seasons.append(
                PlayerSeasonRecord(
                    source_id,
                    str((identifier % 8) + 1),
                    position,
                    45,
                    45,
                )
            )
    gameweeks = tuple(
        GameweekRecord(number, f"2026-08-{7 + number * 7:02d}T17:30:00Z", True)
        for number in (1, 2, 3)
    )
    # Four fixtures a Gameweek so every club plays exactly once; a club with two
    # fixtures would make projected fixture_count outrun the recorded outcomes.
    pairings = ((1, 2), (3, 4), (5, 6), (7, 8))
    fixtures = []
    fixture_by_team = {}
    for number in (1, 2, 3):
        for index, (first, second) in enumerate(pairings):
            home, away = (first, second) if number % 2 else (second, first)
            fixture_id = f"{700 + number * 10 + index}"
            fixtures.append(
                FixtureRecord(
                    fixture_id,
                    str(home),
                    str(away),
                    number,
                    finished=True,
                    home_score=2,
                    away_score=1,
                )
            )
            fixture_by_team[(number, home)] = fixture_id
            fixture_by_team[(number, away)] = fixture_id
    fixtures = tuple(fixtures)
    fixture_stats = []
    snapshots = []
    for index, season in enumerate(player_seasons):
        team = int(season.source_team_id)
        for number in (1, 2, 3):
            fixture_stats.append(
                PlayerFixtureStatsRecord(
                    season.source_player_id,
                    fixture_by_team[(number, team)],
                    minutes=90,
                    starts=True,
                    goals=(index + number) % 3,
                    assists=(index + number) % 2,
                    total_points=2 + (index + number) % 7,
                )
            )
            snapshots.append(
                PlayerGameweekSnapshotRecord(
                    season.source_player_id,
                    number,
                    45,
                    # Captured a day before the deadline: the promotion gate
                    # only scores pre-deadline evidence.
                    datetime(2026, 8, 6 + number * 7, 12, tzinfo=UTC),
                    source_team_id=season.source_team_id,
                    status="a",
                    observation_kind="live_pre_deadline",
                    timing_quality="exact",
                    source_observation_key=(
                        f"forward-{season.source_player_id}-{number}"
                    ),
                )
            )
    return HistoricalBundle(
        season=SeasonRecord("2026-27", "2026/27"),
        teams=teams,
        players=tuple(players),
        player_seasons=tuple(player_seasons),
        gameweeks=gameweeks,
        fixtures=fixtures,
        fixture_stats=tuple(fixture_stats),
        gameweek_snapshots=tuple(snapshots),
    )


def _register_preseason_candidate(database) -> None:
    register_forward_candidate(
        database,
        candidate_key="preseason-priors-v1",
        season_code="2026-27",
        model_version="preseason-priors-v1",
        model_config=asdict(PRESEASON_V5_MODEL_CONFIG),
        registered_at=datetime(2026, 7, 30, tzinfo=UTC),
    )


def test_candidate_run_pair_satisfies_every_gate_scope_check(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        _register_preseason_candidate(database)

        pair = run_forward_candidate_pair(
            database,
            FORWARD_RULES,
            candidate_key="preseason-priors-v1",
            incumbent_config=CORRECTED_V4_MODEL_CONFIG,
            origin_gameweek_start=1,
            origin_gameweek_end=3,
            horizon_gameweeks=1,
        )

        assert pair.evidence_policy == "pre_deadline_only"
        assert pair.incumbent_run_id != pair.challenger_run_id
        assert pair.challenger_model_version == "preseason-priors-v1"
        assert pair.incumbent_model_version == MODEL_VERSION

        rows = {
            int(row["id"]): dict(row)
            for row in database.connection.execute(
                """
                SELECT id, model_version, evidence_policy, model_config_json,
                       origin_gameweek_start, origin_gameweek_end,
                       horizon_gameweeks, season_id, status
                FROM projection_backtest_runs
                """
            )
        }
        incumbent = rows[pair.incumbent_run_id]
        challenger = rows[pair.challenger_run_id]

        # Every equality the promotion gate asserts before it scores a row.
        declaration = load_forward_candidate(database, "preseason-priors-v1")
        assert json.loads(challenger["model_config_json"]) == declaration["model_config"]
        assert challenger["model_version"] == declaration["model_version"]
        assert challenger["evidence_policy"] == "pre_deadline_only"
        for field in (
            "season_id",
            "origin_gameweek_start",
            "origin_gameweek_end",
            "horizon_gameweeks",
        ):
            assert incumbent[field] == challenger[field]
        assert incumbent["model_config_json"] != challenger["model_config_json"]


def test_candidate_run_pair_rejects_a_control_equal_to_the_candidate(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        _register_preseason_candidate(database)

        with pytest.raises(ValueError, match="would measure nothing"):
            run_forward_candidate_pair(
                database,
                FORWARD_RULES,
                candidate_key="preseason-priors-v1",
                incumbent_config=PRESEASON_V5_MODEL_CONFIG,
                origin_gameweek_start=1,
                origin_gameweek_end=3,
            )


def test_candidate_run_pair_rejects_a_shared_model_version(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        _register_preseason_candidate(database)

        with pytest.raises(ValueError, match="different model versions"):
            run_forward_candidate_pair(
                database,
                FORWARD_RULES,
                candidate_key="preseason-priors-v1",
                incumbent_config=CORRECTED_V4_MODEL_CONFIG,
                incumbent_model_version="preseason-priors-v1",
                origin_gameweek_start=1,
                origin_gameweek_end=3,
            )


def test_candidate_run_pair_rejects_mismatched_season_rules(tmp_path) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        _register_preseason_candidate(database)

        with pytest.raises(ValueError, match="does not match candidate season"):
            run_forward_candidate_pair(
                database,
                load_season_rules(Path("config/seasons/2025-26.json")),
                candidate_key="preseason-priors-v1",
                incumbent_config=CORRECTED_V4_MODEL_CONFIG,
                origin_gameweek_start=1,
                origin_gameweek_end=3,
            )


def test_declared_configuration_drift_fails_before_a_run_is_spent() -> None:
    unknown_field = {**asdict(PRESEASON_V5_MODEL_CONFIG), "invented_option": 1}
    with pytest.raises(ValueError, match="does not match ProjectionModelConfig"):
        declared_challenger_config(
            {"candidate_key": "drifted", "model_config": unknown_field}
        )

    missing_field = asdict(PRESEASON_V5_MODEL_CONFIG)
    del missing_field["cold_start_prior"]
    with pytest.raises(ValueError, match="no longer round-trips"):
        declared_challenger_config(
            {"candidate_key": "drifted", "model_config": missing_field}
        )


def test_decision_evidence_measures_legal_squad_regret_from_the_pair(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        _register_preseason_candidate(database)
        pair = run_forward_candidate_pair(
            database,
            FORWARD_RULES,
            candidate_key="preseason-priors-v1",
            incumbent_config=CORRECTED_V4_MODEL_CONFIG,
            origin_gameweek_start=1,
            origin_gameweek_end=3,
            horizon_gameweeks=1,
        )

        evidence = build_decision_gate_evidence(
            database,
            FORWARD_RULES,
            incumbent_run_id=pair.incumbent_run_id,
            challenger_run_id=pair.challenger_run_id,
        )

        # All three gates are derived from the pair; none is supplied.
        assert isinstance(evidence.legal_squad_regret_change, float)
        assert isinstance(evidence.owned_captain_regret_change, float)
        assert isinstance(evidence.transfer_regret_change, float)
        assert "incumbent_run=" in evidence.source_report
        assert "origins=3" in evidence.source_report
        assert "captain_decisions=3" in evidence.source_report
        assert "transfer_decisions=2" in evidence.source_report


def test_decision_evidence_refuses_runs_covering_different_origins(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        _register_preseason_candidate(database)
        pair = run_forward_candidate_pair(
            database,
            FORWARD_RULES,
            candidate_key="preseason-priors-v1",
            incumbent_config=CORRECTED_V4_MODEL_CONFIG,
            origin_gameweek_start=1,
            origin_gameweek_end=3,
            horizon_gameweeks=1,
        )
        database.connection.execute(
            """
            DELETE FROM projection_backtest_predictions
            WHERE backtest_run_id = ? AND origin_gameweek = 3
            """,
            (pair.challenger_run_id,),
        )
        database.connection.commit()

        with pytest.raises(ValueError, match="identical origins"):
            build_decision_gate_evidence(
                database,
                FORWARD_RULES,
                incumbent_run_id=pair.incumbent_run_id,
                challenger_run_id=pair.challenger_run_id,
            )


def test_declared_control_file_is_the_incumbent_minus_stage_3a() -> None:
    candidate = json.loads(
        Path("config/model_candidates/preseason-priors-v1.json").read_text()
    )
    control = json.loads(
        Path("config/model_candidates/preseason-priors-v1-incumbent.json").read_text()
    )

    assert control == asdict(CORRECTED_V4_MODEL_CONFIG)
    assert candidate == asdict(PRESEASON_V5_MODEL_CONFIG)
    divergent = {key for key in candidate if candidate[key] != control[key]}
    assert divergent == {"team_strength_carry_forward", "cold_start_prior"}
