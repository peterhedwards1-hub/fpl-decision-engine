"""Strict contract for structured news evidence produced by an LLM review."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .workflow import WeeklyWorkflowRepository

EVIDENCE_TYPES = {
    "injury",
    "suspension",
    "training",
    "manager_quote",
    "predicted_lineup",
    "tactical_role",
    "transfer",
    "other",
}


@dataclass(frozen=True)
class StructuredNewsEvidence:
    evidence_type: str
    summary: str
    confidence: str
    evidence_at: datetime
    source_player_id: str | None = None
    source_url: str | None = None


def parse_structured_news(payload: str) -> tuple[StructuredNewsEvidence, ...]:
    """Parse the versioned JSON contract without accepting invented fields."""

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"News evidence is not valid JSON: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "evidence"}:
        raise ValueError(
            "News payload must contain only schema_version and evidence"
        )
    if raw["schema_version"] != 1:
        raise ValueError("Unsupported news evidence schema version")
    if not isinstance(raw["evidence"], list):
        raise ValueError("News evidence must be a list")
    expected_fields = {
        "evidence_type",
        "summary",
        "confidence",
        "evidence_at",
        "source_player_id",
        "source_url",
    }
    result = []
    for index, item in enumerate(raw["evidence"]):
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ValueError(
                f"Evidence item {index} must contain exactly "
                f"{sorted(expected_fields)}"
            )
        if item["evidence_type"] not in EVIDENCE_TYPES:
            raise ValueError(
                f"Evidence item {index} has unknown evidence_type"
            )
        if item["confidence"] not in {"low", "medium", "high"}:
            raise ValueError(f"Evidence item {index} has invalid confidence")
        if not isinstance(item["summary"], str) or not item["summary"].strip():
            raise ValueError(f"Evidence item {index} needs a summary")
        try:
            evidence_at = datetime.fromisoformat(
                str(item["evidence_at"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(
                f"Evidence item {index} has an invalid evidence_at"
            ) from error
        if evidence_at.tzinfo is None:
            raise ValueError(
                f"Evidence item {index} evidence_at must include a timezone"
            )
        for optional in ("source_player_id", "source_url"):
            if item[optional] is not None and not isinstance(item[optional], str):
                raise ValueError(
                    f"Evidence item {index} {optional} must be text or null"
                )
        result.append(
            StructuredNewsEvidence(
                evidence_type=item["evidence_type"],
                summary=item["summary"].strip(),
                confidence=item["confidence"],
                evidence_at=evidence_at,
                source_player_id=item["source_player_id"],
                source_url=item["source_url"],
            )
        )
    return tuple(result)


def ingest_structured_news(
    repository: WeeklyWorkflowRepository,
    *,
    season_code: str,
    gameweek_number: int,
    payload: str,
) -> tuple[int, ...]:
    """Validate the whole batch, then append each item to human review."""

    evidence = parse_structured_news(payload)
    return tuple(
        repository.add_news_evidence(
            season_code=season_code,
            gameweek_number=gameweek_number,
            evidence_type=item.evidence_type,
            summary=item.summary,
            confidence=item.confidence,  # type: ignore[arg-type]
            evidence_at=item.evidence_at,
            source_player_id=item.source_player_id,
            source_url=item.source_url,
        )
        for item in evidence
    )


def structured_news_schema() -> dict[str, Any]:
    """Return the JSON shape to request from an LLM or other evidence agent."""

    return {
        "schema_version": 1,
        "evidence": [
            {
                "evidence_type": "injury",
                "summary": "Concise factual evidence, not a recommendation",
                "confidence": "medium",
                "evidence_at": "2026-08-13T12:00:00+00:00",
                "source_player_id": "official FPL element ID or null",
                "source_url": "direct source URL or null",
            }
        ],
    }
