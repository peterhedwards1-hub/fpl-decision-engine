import json
from datetime import timedelta

import pytest
from test_manager_state import CAPTURED_AT, _bundle

from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import IngestionSource
from fpl_engine.news import (
    ingest_structured_news,
    parse_structured_news,
    structured_news_schema,
)
from fpl_engine.workflow import WeeklyWorkflowRepository


def test_structured_news_contract_is_strict_and_timezone_aware() -> None:
    payload = structured_news_schema()
    payload["evidence"][0]["source_player_id"] = "101"
    evidence = parse_structured_news(json.dumps(payload))

    assert len(evidence) == 1
    assert evidence[0].evidence_type == "injury"
    assert evidence[0].source_player_id == "101"
    assert evidence[0].evidence_at.utcoffset() is not None
    assert evidence[0].schema_version == 2
    assert evidence[0].source_tier == "official"
    assert evidence[0].suggested_adjustment is not None
    assert evidence[0].suggested_adjustment.value == -30

    payload["evidence"][0]["invented_recommendation"] = "buy"
    with pytest.raises(ValueError, match="exactly"):
        parse_structured_news(json.dumps(payload))


def test_legacy_news_contract_remains_readable() -> None:
    evidence = parse_structured_news(
        json.dumps(
            {
                "schema_version": 1,
                "evidence": [
                    {
                        "evidence_type": "training",
                        "summary": "Returned to training",
                        "confidence": "medium",
                        "evidence_at": "2026-08-13T12:00:00+00:00",
                        "source_player_id": "101",
                        "source_url": None,
                    }
                ],
            }
        )
    )

    assert evidence[0].schema_version == 1
    assert evidence[0].summary == "Returned to training"


def test_v2_news_is_persisted_and_its_proposal_can_be_reviewed(
    tmp_path,
) -> None:
    with HistoricalDatabase(tmp_path / "news.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(
            IngestionSource(
                name="official-fpl-api",
                retrieved_at=CAPTURED_AT,
                identifier_namespace="official-fpl",
            ),
            _bundle(),
        )
        payload = structured_news_schema()
        payload["generated_at"] = CAPTURED_AT.isoformat()
        payload["evidence"][0]["published_at"] = CAPTURED_AT.isoformat()
        payload["evidence"][0]["expiry"] = (
            CAPTURED_AT + timedelta(days=1)
        ).isoformat()
        payload["evidence"][0]["source_player_id"] = "13"
        repository = WeeklyWorkflowRepository(database)

        evidence_id = ingest_structured_news(
            repository,
            season_code="2026-27",
            gameweek_number=1,
            payload=json.dumps(payload),
        )[0]
        repository.review_evidence(
            evidence_id,
            status="accepted",
            rationale="Official and current",
            decision_maker="test-user",
            reviewed_at=CAPTURED_AT,
        )

        row = database.connection.execute(
            """
            SELECT schema_version, source_tier, prompt_version,
                   research_run_id, expected_minutes_adjustment,
                   decision_maker
            FROM news_evidence WHERE id = ?
            """,
            (evidence_id,),
        ).fetchone()
        assert dict(row) == {
            "schema_version": 2,
            "source_tier": "official",
            "prompt_version": "fpl-team-news-v2",
            "research_run_id": "unique-run-id",
            "expected_minutes_adjustment": -30,
            "decision_maker": "test-user",
        }
