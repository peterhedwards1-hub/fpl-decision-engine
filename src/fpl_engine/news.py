"""Strict contracts for structured, reviewable team-news evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
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
SOURCE_TIERS = {
    "official",
    "strong_reporting",
    "predicted_lineup",
    "rumour",
}
MODEL_AREAS = {
    "minutes",
    "role",
    "availability",
    "set_pieces",
    "fixture",
    "none",
}


@dataclass(frozen=True)
class SuggestedAdjustment:
    kind: str
    value: float


@dataclass(frozen=True)
class StructuredNewsEvidence:
    schema_version: int
    evidence_type: str
    summary: str
    confidence: str
    evidence_at: datetime
    source_player_id: str | None = None
    source_url: str | None = None
    source_name: str | None = None
    source_tier: str | None = None
    model_area: str | None = None
    suggested_adjustment: SuggestedAdjustment | None = None
    adjustment_basis: str | None = None
    requires_decision: bool = True
    decision_question: str | None = None
    expiry: datetime | None = None
    prompt_version: str | None = None
    research_run_id: str | None = None


def parse_structured_news(payload: str) -> tuple[StructuredNewsEvidence, ...]:
    """Parse a complete batch without accepting unknown or invented fields."""

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"News evidence is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("News payload must be an object")
    schema_version = raw.get("schema_version")
    if schema_version == 1:
        return _parse_v1(raw)
    if schema_version == 2:
        return _parse_v2(raw)
    raise ValueError("Unsupported news evidence schema version")


def _parse_v1(raw: dict[str, Any]) -> tuple[StructuredNewsEvidence, ...]:
    if set(raw) != {"schema_version", "evidence"}:
        raise ValueError(
            "Version 1 news payload must contain only schema_version and evidence"
        )
    items = _evidence_list(raw)
    expected_fields = {
        "evidence_type",
        "summary",
        "confidence",
        "evidence_at",
        "source_player_id",
        "source_url",
    }
    result = []
    for index, item in enumerate(items):
        _exact_fields(item, expected_fields, index)
        result.append(
            StructuredNewsEvidence(
                schema_version=1,
                evidence_type=_evidence_type(item, index),
                summary=_required_text(item["summary"], "summary", index),
                confidence=_confidence(item, index),
                evidence_at=_timestamp(
                    item["evidence_at"], "evidence_at", index
                ),
                source_player_id=_optional_text(
                    item["source_player_id"], "source_player_id", index
                ),
                source_url=_optional_text(
                    item["source_url"], "source_url", index
                ),
            )
        )
    return tuple(result)


def _parse_v2(raw: dict[str, Any]) -> tuple[StructuredNewsEvidence, ...]:
    expected_root = {
        "schema_version",
        "prompt_version",
        "research_run_id",
        "generated_at",
        "evidence",
    }
    if set(raw) != expected_root:
        raise ValueError(
            f"Version 2 news payload must contain exactly {sorted(expected_root)}"
        )
    prompt_version = _required_root_text(raw["prompt_version"], "prompt_version")
    research_run_id = _required_root_text(
        raw["research_run_id"], "research_run_id"
    )
    _timestamp(raw["generated_at"], "generated_at", -1)
    expected_fields = {
        "evidence_type",
        "fact_summary",
        "source_name",
        "source_url",
        "published_at",
        "source_tier",
        "confidence",
        "source_player_id",
        "model_area",
        "suggested_adjustment",
        "adjustment_basis",
        "requires_decision",
        "decision_question",
        "expiry",
    }
    result = []
    for index, item in enumerate(_evidence_list(raw)):
        _exact_fields(item, expected_fields, index)
        source_tier = item["source_tier"]
        if source_tier not in SOURCE_TIERS:
            raise ValueError(f"Evidence item {index} has invalid source_tier")
        model_area = item["model_area"]
        if model_area not in MODEL_AREAS:
            raise ValueError(f"Evidence item {index} has invalid model_area")
        requires_decision = item["requires_decision"]
        if not isinstance(requires_decision, bool):
            raise ValueError(
                f"Evidence item {index} requires_decision must be boolean"
            )
        adjustment = _adjustment(item["suggested_adjustment"], index)
        adjustment_basis = _optional_text(
            item["adjustment_basis"], "adjustment_basis", index
        )
        if adjustment is not None and adjustment_basis is None:
            raise ValueError(
                f"Evidence item {index} adjustment requires an adjustment_basis"
            )
        decision_question = _optional_text(
            item["decision_question"], "decision_question", index
        )
        if requires_decision and decision_question is None:
            raise ValueError(
                f"Evidence item {index} requiring a decision needs a question"
            )
        result.append(
            StructuredNewsEvidence(
                schema_version=2,
                evidence_type=_evidence_type(item, index),
                summary=_required_text(
                    item["fact_summary"], "fact_summary", index
                ),
                confidence=_confidence(item, index),
                evidence_at=_timestamp(
                    item["published_at"], "published_at", index
                ),
                source_player_id=_optional_text(
                    item["source_player_id"], "source_player_id", index
                ),
                source_url=_optional_text(
                    item["source_url"], "source_url", index
                ),
                source_name=_required_text(
                    item["source_name"], "source_name", index
                ),
                source_tier=source_tier,
                model_area=model_area,
                suggested_adjustment=adjustment,
                adjustment_basis=adjustment_basis,
                requires_decision=requires_decision,
                decision_question=decision_question,
                expiry=_optional_timestamp(item["expiry"], "expiry", index),
                prompt_version=prompt_version,
                research_run_id=research_run_id,
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
            schema_version=item.schema_version,
            source_name=item.source_name,
            source_tier=item.source_tier,
            model_area=item.model_area,
            suggested_adjustment=(
                None
                if item.suggested_adjustment is None
                else {
                    "kind": item.suggested_adjustment.kind,
                    "value": item.suggested_adjustment.value,
                }
            ),
            adjustment_basis=item.adjustment_basis,
            requires_decision=item.requires_decision,
            decision_question=item.decision_question,
            expires_at=item.expiry,
            prompt_version=item.prompt_version,
            research_run_id=item.research_run_id,
        )
        for item in evidence
    )


def structured_news_schema() -> dict[str, Any]:
    """Return the current JSON shape to request from a news research agent."""

    return {
        "schema_version": 2,
        "prompt_version": "fpl-team-news-v2",
        "research_run_id": "unique-run-id",
        "generated_at": "2026-08-13T12:00:00+00:00",
        "evidence": [
            {
                "evidence_type": "injury",
                "fact_summary": "Concise factual evidence, not a recommendation",
                "source_name": "Club press conference",
                "source_url": "https://example.invalid/direct-source",
                "published_at": "2026-08-13T11:30:00+00:00",
                "source_tier": "official",
                "confidence": "high",
                "source_player_id": "official FPL element ID or null",
                "model_area": "minutes",
                "suggested_adjustment": {
                    "kind": "expected_minutes_delta",
                    "value": -30.0,
                },
                "adjustment_basis": "Expected to miss part of the match",
                "requires_decision": True,
                "decision_question": "Accept the proposed minutes adjustment?",
                "expiry": "2026-08-15T10:00:00+00:00",
            }
        ],
    }


def _evidence_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw["evidence"]
    if not isinstance(items, list):
        raise ValueError("News evidence must be a list")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("Every news evidence item must be an object")
    return items


def _exact_fields(
    item: dict[str, Any], expected: set[str], index: int
) -> None:
    if set(item) != expected:
        raise ValueError(
            f"Evidence item {index} must contain exactly {sorted(expected)}"
        )


def _evidence_type(item: dict[str, Any], index: int) -> str:
    value = item["evidence_type"]
    if value not in EVIDENCE_TYPES:
        raise ValueError(f"Evidence item {index} has unknown evidence_type")
    return value


def _confidence(item: dict[str, Any], index: int) -> str:
    value = item["confidence"]
    if value not in {"low", "medium", "high"}:
        raise ValueError(f"Evidence item {index} has invalid confidence")
    return value


def _required_text(value: Any, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evidence item {index} needs {field}")
    return value.strip()


def _required_root_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"News payload needs {field}")
    return value.strip()


def _optional_text(value: Any, field: str, index: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evidence item {index} {field} must be text or null")
    return value.strip()


def _timestamp(value: Any, field: str, index: int) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Evidence item {index} has an invalid {field}"
        ) from error
    if result.tzinfo is None:
        raise ValueError(
            f"Evidence item {index} {field} must include a timezone"
        )
    return result.astimezone(UTC)


def _optional_timestamp(
    value: Any, field: str, index: int
) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, field, index)


def _adjustment(value: Any, index: int) -> SuggestedAdjustment | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        raise ValueError(
            f"Evidence item {index} suggested_adjustment must contain "
            "only kind and value"
        )
    if value["kind"] != "expected_minutes_delta":
        raise ValueError(
            f"Evidence item {index} has unsupported adjustment kind"
        )
    adjustment = value["value"]
    if isinstance(adjustment, bool) or not isinstance(adjustment, (int, float)):
        raise ValueError(
            f"Evidence item {index} adjustment value must be numeric"
        )
    if not -90 <= float(adjustment) <= 90:
        raise ValueError(
            f"Evidence item {index} minutes adjustment must be between -90 and 90"
        )
    return SuggestedAdjustment(value=float(adjustment), kind=value["kind"])
