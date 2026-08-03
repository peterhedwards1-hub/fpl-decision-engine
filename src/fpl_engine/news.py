"""Strict contracts for structured, reviewable team-news evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .team_news_v3 import (
    ADJUSTMENT_KINDS,
    ADJUSTMENT_SUPPORT,
    COVERAGE_STATUSES,
    PRIORITIES,
    RESEARCH_MODES,
    validate_package_against_database,
)
from .team_news_v3 import (
    EVIDENCE_TYPES as V3_EVIDENCE_TYPES,
)
from .team_news_v3 import (
    MODEL_AREAS as V3_MODEL_AREAS,
)
from .team_news_v3 import (
    SOURCE_TIERS as V3_SOURCE_TIERS,
)
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
    priority: str | None = None
    selected_player_status: str | None = None
    adjustment_support: str | None = None
    input_package_id: str | None = None
    input_package_hash: str | None = None
    research_window_start: datetime | None = None
    target_deadline: datetime | None = None
    research_mode: str | None = None
    temporal_status: str | None = None
    conflict_group_id: str | None = None
    supporting_evidence: tuple[dict[str, Any], ...] = ()
    conflicting_evidence: tuple[dict[str, Any], ...] = ()
    unresolved_uncertainty: str | None = None
    resolution_event: str | None = None
    confidence_after_conflict: str | None = None


@dataclass(frozen=True)
class StructuredNewsV3Result:
    research_run_id: str
    input_package_id: str
    input_package_hash: str
    season_code: str
    gameweek: int
    research_mode: str
    research_window_start: datetime
    generated_at: datetime
    target_deadline: datetime | None
    coverage: tuple[dict[str, Any], ...]
    evidence: tuple[StructuredNewsEvidence, ...]
    discoveries: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]


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
    if schema_version == 3:
        return parse_structured_news_v3(payload).evidence
    raise ValueError("Unsupported news evidence schema version")


def parse_structured_news_v3(payload: str) -> StructuredNewsV3Result:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"News evidence is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("News payload must be an object")
    expected_root = {
        "schema_version",
        "prompt_version",
        "research_run_id",
        "input_package_id",
        "input_package_hash",
        "season_code",
        "gameweek",
        "target_deadline",
        "research_mode",
        "research_window_start",
        "generated_at",
        "coverage",
        "evidence",
        "discoveries",
        "limitations",
    }
    if set(raw) != expected_root:
        raise ValueError(f"Version 3 news payload must contain exactly {sorted(expected_root)}")
    if raw["schema_version"] != 3 or raw["prompt_version"] != "fpl-team-news-v3":
        raise ValueError("Version 3 payload has an invalid schema or prompt version")
    research_run_id = _required_root_text(raw["research_run_id"], "research_run_id")
    package_id = _required_root_text(raw["input_package_id"], "input_package_id")
    package_hash = _required_root_text(raw["input_package_hash"], "input_package_hash")
    season_code = _required_root_text(raw["season_code"], "season_code")
    if (
        isinstance(raw["gameweek"], bool)
        or not isinstance(raw["gameweek"], int)
        or raw["gameweek"] < 1
    ):
        raise ValueError("Version 3 gameweek must be a positive integer")
    research_mode = raw["research_mode"]
    if research_mode not in RESEARCH_MODES:
        raise ValueError("Version 3 research_mode is invalid")
    research_window_start = _timestamp(raw["research_window_start"], "research_window_start", -1)
    generated_at = _timestamp(raw["generated_at"], "generated_at", -1)
    target_deadline = _optional_timestamp(raw["target_deadline"], "target_deadline", -1)
    coverage = _parse_v3_coverage(raw["coverage"])
    evidence = tuple(
        _parse_v3_evidence(item, index, raw)
        for index, item in enumerate(_list_of_dicts(raw["evidence"], "evidence"))
    )
    discoveries = tuple(
        _parse_v3_discovery(item, index)
        for index, item in enumerate(_list_of_dicts(raw["discoveries"], "discoveries"))
    )
    limitations = _string_list(raw["limitations"], "limitations")
    if len({item["source_player_id"] for item in coverage}) != len(coverage):
        raise ValueError("Version 3 coverage contains duplicate source_player_id values")
    return StructuredNewsV3Result(
        research_run_id=research_run_id,
        input_package_id=package_id,
        input_package_hash=package_hash,
        season_code=season_code,
        gameweek=raw["gameweek"],
        research_mode=research_mode,
        research_window_start=research_window_start,
        generated_at=generated_at,
        target_deadline=target_deadline,
        coverage=coverage,
        evidence=evidence,
        discoveries=discoveries,
        limitations=limitations,
    )


def _list_of_dicts(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Version 3 {field} must be a list of objects")
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"Version 3 {field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _parse_v3_coverage(value: Any) -> tuple[dict[str, Any], ...]:
    expected = {
        "source_player_id",
        "priority",
        "status",
        "areas_checked",
        "latest_source_checked_at",
        "notes",
    }
    result = []
    for index, item in enumerate(_list_of_dicts(value, "coverage")):
        _exact_fields(item, expected, index)
        player_id = _required_text(item["source_player_id"], "source_player_id", index)
        if item["priority"] not in PRIORITIES:
            raise ValueError(f"Coverage item {index} has invalid priority")
        if item["status"] not in COVERAGE_STATUSES:
            raise ValueError(f"Coverage item {index} has invalid status")
        areas = _string_list(item["areas_checked"], "areas_checked")
        checked_at = _optional_timestamp(
            item["latest_source_checked_at"], "latest_source_checked_at", index
        )
        notes = _optional_text(item["notes"], "notes", index)
        result.append(
            {
                "source_player_id": player_id,
                "priority": item["priority"],
                "status": item["status"],
                "areas_checked": list(areas),
                "latest_source_checked_at": checked_at,
                "notes": notes,
            }
        )
    return tuple(result)


def _parse_v3_sources(value: Any, field: str, index: int) -> tuple[dict[str, Any], ...]:
    expected = {"source_name", "source_url", "published_at", "source_tier", "fact_summary"}
    result = []
    for source in _list_of_dicts(value, field):
        _exact_fields(source, expected, index)
        source_name = _required_text(source["source_name"], "source_name", index)
        source_url = _required_text(source["source_url"], "source_url", index)
        published_at = _timestamp(source["published_at"], "published_at", index)
        if source["source_tier"] not in V3_SOURCE_TIERS:
            raise ValueError(
                f"Evidence item {index} has invalid source_tier; "
                f"use one of {sorted(V3_SOURCE_TIERS)}"
            )
        result.append(
            {
                "source_name": source_name,
                "source_url": source_url,
                "published_at": published_at,
                "source_tier": source["source_tier"],
                "fact_summary": _required_text(source["fact_summary"], "fact_summary", index),
            }
        )
    return tuple(result)


def _parse_v3_evidence(
    item: dict[str, Any], index: int, root: dict[str, Any]
) -> StructuredNewsEvidence:
    expected = {
        "evidence_id",
        "issue_id",
        "source_player_id",
        "priority",
        "selected_player_status",
        "evidence_type",
        "fact_summary",
        "source_name",
        "source_url",
        "published_at",
        "source_tier",
        "confidence",
        "model_area",
        "suggested_adjustment",
        "adjustment_basis",
        "adjustment_support",
        "requires_decision",
        "decision_question",
        "expiry",
        "context_scope",
        "supporting_evidence",
        "conflicting_evidence",
        "unresolved_uncertainty",
        "resolution_event",
        "confidence_after_conflict",
    }
    _exact_fields(item, expected, index)
    if item["priority"] not in PRIORITIES:
        raise ValueError(f"Evidence item {index} has invalid priority")
    if item["selected_player_status"] not in {"selected", "alternative", "discovery", "general"}:
        raise ValueError(f"Evidence item {index} has invalid selected_player_status")
    if item["evidence_type"] not in V3_EVIDENCE_TYPES:
        raise ValueError(f"Evidence item {index} has invalid evidence_type")
    if item["source_tier"] not in V3_SOURCE_TIERS:
        raise ValueError(
            f"Evidence item {index} has invalid source_tier; "
            f"use one of {sorted(V3_SOURCE_TIERS)}"
        )
    if item["model_area"] not in V3_MODEL_AREAS:
        raise ValueError(f"Evidence item {index} has invalid model_area")
    if item["adjustment_support"] not in ADJUSTMENT_SUPPORT:
        raise ValueError(f"Evidence item {index} has invalid adjustment_support")
    source_player_id = _optional_text(item["source_player_id"], "source_player_id", index)
    source_name = _required_text(item["source_name"], "source_name", index)
    source_url = _required_text(item["source_url"], "source_url", index)
    published_at = _timestamp(item["published_at"], "published_at", index)
    expiry = _optional_timestamp(item["expiry"], "expiry", index)
    if item["context_scope"] not in {"current_window", "background_context"}:
        raise ValueError(f"Evidence item {index} has invalid context_scope")
    if not isinstance(item["requires_decision"], bool):
        raise ValueError(f"Evidence item {index} requires_decision must be boolean")
    decision_question = _optional_text(item["decision_question"], "decision_question", index)
    if item["requires_decision"] and decision_question is None:
        raise ValueError(f"Evidence item {index} requiring a decision needs a question")
    adjustment = _adjustment_v3(item["suggested_adjustment"], index)
    basis = _optional_text(item["adjustment_basis"], "adjustment_basis", index)
    if adjustment is not None and basis is None:
        raise ValueError(f"Evidence item {index} adjustment requires an adjustment_basis")
    if (
        adjustment is not None
        and item["adjustment_support"] == "supported_numeric"
        and adjustment.kind != "expected_minutes_delta"
    ):
        raise ValueError(
            f"Evidence item {index} marks a production-unsupported numeric adjustment as supported"
        )
    supporting = _parse_v3_sources(item["supporting_evidence"], "supporting_evidence", index)
    conflicting = _parse_v3_sources(item["conflicting_evidence"], "conflicting_evidence", index)
    uncertainty = _optional_text(item["unresolved_uncertainty"], "unresolved_uncertainty", index)
    resolution = _optional_text(item["resolution_event"], "resolution_event", index)
    after_conflict = _optional_text(
        item["confidence_after_conflict"], "confidence_after_conflict", index
    )
    if conflicting and (uncertainty is None or after_conflict is None):
        raise ValueError(
            f"Evidence item {index} with conflicting evidence must preserve "
            "uncertainty and confidence"
        )
    return StructuredNewsEvidence(
        schema_version=3,
        evidence_type=item["evidence_type"],
        summary=_required_text(item["fact_summary"], "fact_summary", index),
        confidence=_confidence(item, index),
        evidence_at=published_at,
        source_player_id=source_player_id,
        source_url=source_url,
        source_name=source_name,
        source_tier=item["source_tier"],
        model_area=item["model_area"],
        suggested_adjustment=adjustment,
        adjustment_basis=basis,
        requires_decision=item["requires_decision"],
        decision_question=decision_question,
        expiry=expiry,
        prompt_version=root["prompt_version"],
        research_run_id=root["research_run_id"],
        priority=item["priority"],
        selected_player_status=item["selected_player_status"],
        adjustment_support=item["adjustment_support"],
        input_package_id=root["input_package_id"],
        input_package_hash=root["input_package_hash"],
        research_window_start=_timestamp(
            root["research_window_start"], "research_window_start", -1
        ),
        target_deadline=_optional_timestamp(root["target_deadline"], "target_deadline", -1),
        research_mode=root["research_mode"],
        conflict_group_id=_optional_text(item["issue_id"], "issue_id", index),
        supporting_evidence=supporting,
        conflicting_evidence=conflicting,
        unresolved_uncertainty=uncertainty,
        resolution_event=resolution,
        confidence_after_conflict=after_conflict,
    )


def _parse_v3_discovery(item: dict[str, Any], index: int) -> dict[str, Any]:
    expected = {
        "discovery_id",
        "source_player_id",
        "identity_status",
        "discovery_type",
        "fact_summary",
        "source_name",
        "source_url",
        "published_at",
        "source_tier",
        "decision_relevance",
    }
    _exact_fields(item, expected, index)
    if item["identity_status"] not in {"resolved", "unresolved"}:
        raise ValueError(f"Discovery {index} has invalid identity_status")
    if item["identity_status"] == "unresolved" and item["source_player_id"] is not None:
        raise ValueError(f"Discovery {index} unresolved identity must use null source_player_id")
    if item["identity_status"] == "resolved":
        _required_text(item["source_player_id"], "source_player_id", index)
    if item["source_tier"] not in V3_SOURCE_TIERS:
        raise ValueError(
            f"Discovery {index} has invalid source_tier; "
            f"use one of {sorted(V3_SOURCE_TIERS)}"
        )
    _timestamp(item["published_at"], "published_at", index)
    for field in (
        "discovery_id",
        "discovery_type",
        "fact_summary",
        "source_name",
        "source_url",
        "decision_relevance",
    ):
        _required_text(item[field], field, index)
    return item


def _adjustment_v3(value: Any, index: int) -> SuggestedAdjustment | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        raise ValueError(
            f"Evidence item {index} suggested_adjustment must contain only kind and value"
        )
    if value["kind"] not in ADJUSTMENT_KINDS:
        raise ValueError(f"Evidence item {index} has unsupported adjustment kind")
    raw = value["value"]
    if value["kind"] in {
        "expected_minutes_delta",
        "appearance_probability_delta",
        "starting_probability_delta",
        "sixty_probability_delta",
    }:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"Evidence item {index} numeric adjustment value must be numeric")
        limit = 90 if value["kind"] == "expected_minutes_delta" else 1
        if not -limit <= float(raw) <= limit:
            raise ValueError(
                f"Evidence item {index} numeric adjustment is outside its allowed range"
            )
        raw = float(raw)
    elif not isinstance(raw, (str, int, float, bool)) and raw is not None:
        raise ValueError(f"Evidence item {index} adjustment value must be scalar")
    return SuggestedAdjustment(kind=value["kind"], value=raw)  # type: ignore[arg-type]


def _parse_v1(raw: dict[str, Any]) -> tuple[StructuredNewsEvidence, ...]:
    if set(raw) != {"schema_version", "evidence"}:
        raise ValueError("Version 1 news payload must contain only schema_version and evidence")
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
                evidence_at=_timestamp(item["evidence_at"], "evidence_at", index),
                source_player_id=_optional_text(
                    item["source_player_id"], "source_player_id", index
                ),
                source_url=_optional_text(item["source_url"], "source_url", index),
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
        raise ValueError(f"Version 2 news payload must contain exactly {sorted(expected_root)}")
    prompt_version = _required_root_text(raw["prompt_version"], "prompt_version")
    research_run_id = _required_root_text(raw["research_run_id"], "research_run_id")
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
            raise ValueError(f"Evidence item {index} requires_decision must be boolean")
        adjustment = _adjustment(item["suggested_adjustment"], index)
        adjustment_basis = _optional_text(item["adjustment_basis"], "adjustment_basis", index)
        if adjustment is not None and adjustment_basis is None:
            raise ValueError(f"Evidence item {index} adjustment requires an adjustment_basis")
        decision_question = _optional_text(item["decision_question"], "decision_question", index)
        if requires_decision and decision_question is None:
            raise ValueError(f"Evidence item {index} requiring a decision needs a question")
        result.append(
            StructuredNewsEvidence(
                schema_version=2,
                evidence_type=_evidence_type(item, index),
                summary=_required_text(item["fact_summary"], "fact_summary", index),
                confidence=_confidence(item, index),
                evidence_at=_timestamp(item["published_at"], "published_at", index),
                source_player_id=_optional_text(
                    item["source_player_id"], "source_player_id", index
                ),
                source_url=_optional_text(item["source_url"], "source_url", index),
                source_name=_required_text(item["source_name"], "source_name", index),
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
    raw = json.loads(payload)
    if raw.get("schema_version") == 3:
        return _ingest_v3_news(
            repository,
            season_code=season_code,
            gameweek_number=gameweek_number,
            payload=payload,
        )
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


def _ingest_v3_news(
    repository: WeeklyWorkflowRepository,
    *,
    season_code: str,
    gameweek_number: int,
    payload: str,
) -> tuple[int, ...]:
    result = parse_structured_news_v3(payload)
    if result.season_code != season_code or result.gameweek != gameweek_number:
        raise ValueError("v3 result season or Gameweek does not match the import target")
    package = validate_package_against_database(repository.database, json.loads(payload))
    if result.research_mode != package["research_mode"]:
        raise ValueError("v3 research mode does not match the input package")
    if any(item.evidence_at > result.generated_at for item in result.evidence):
        raise ValueError("v3 evidence cannot be published after the research timestamp")
    package_json = json.loads(package["package_json"])
    directory_ids = {str(item["source_player_id"]) for item in package_json["player_directory"]}
    for discovery in result.discoveries:
        if (
            discovery["identity_status"] == "resolved"
            and str(discovery["source_player_id"]) not in directory_ids
        ):
            raise ValueError(
                f"Discovery {discovery['discovery_id']!r} uses an ID outside the "
                "supplied official player directory"
            )
    required_ids = {item["source_player_id"] for item in package_json["selected_squad"]} | {
        item["source_player_id"] for item in package_json["alternatives"]
    }
    supplied_ids = {item["source_player_id"] for item in result.coverage}
    missing = required_ids - supplied_ids
    if missing:
        raise ValueError(f"v3 coverage is missing supplied players: {sorted(missing)}")
    if any(
        item["priority"] == "broad_scan" and item["source_player_id"] in required_ids
        for item in result.coverage
    ):
        raise ValueError("Supplied squad and alternatives cannot be marked broad_scan")
    existing_run = repository.database.connection.execute(
        "SELECT id FROM team_news_research_runs WHERE research_run_id = ?",
        (result.research_run_id,),
    ).fetchone()
    if existing_run is not None:
        raise ValueError(f"Research run {result.research_run_id!r} has already been imported")
    season_row = repository.database.connection.execute(
        """
        SELECT seasons.id AS season_id, gameweeks.id AS gameweek_id
        FROM seasons JOIN gameweeks ON gameweeks.season_id = seasons.id
        WHERE seasons.code = ? AND gameweeks.number = ?
        """,
        (season_code, gameweek_number),
    ).fetchone()
    timestamp = result.generated_at.astimezone(UTC).isoformat()
    with repository.database.transaction():
        cursor = repository.database.connection.execute(
            """
            INSERT INTO team_news_research_runs (
                research_run_id, input_package_id, input_package_hash, season_id,
                gameweek_id, research_mode, research_window_start, generated_at,
                target_deadline, prompt_version, schema_version, raw_result_json,
                import_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, ?, 'imported', ?)
            RETURNING id
            """,
            (
                result.research_run_id,
                result.input_package_id,
                result.input_package_hash,
                season_row["season_id"],
                season_row["gameweek_id"],
                result.research_mode,
                result.research_window_start.astimezone(UTC).isoformat(),
                timestamp,
                None
                if result.target_deadline is None
                else result.target_deadline.astimezone(UTC).isoformat(),
                "fpl-team-news-v3",
                payload,
                timestamp,
            ),
        )
        research_result_id = int(cursor.fetchone()[0])
        for item in result.coverage:
            repository.database.connection.execute(
                """
                INSERT INTO team_news_coverage (
                    research_result_id, source_player_id, priority, status,
                    areas_checked_json, latest_source_checked_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    research_result_id,
                    item["source_player_id"],
                    item["priority"],
                    item["status"],
                    _json(item["areas_checked"]),
                    item["latest_source_checked_at"],
                    item["notes"],
                ),
            )
        for discovery in result.discoveries:
            repository.database.connection.execute(
                """
                INSERT INTO team_news_discoveries (
                    research_result_id, discovery_id, source_player_id,
                    identity_status, discovery_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    research_result_id,
                    discovery["discovery_id"],
                    discovery["source_player_id"],
                    discovery["identity_status"],
                    _json(discovery),
                ),
            )
    ids: list[int] = []
    for item in result.evidence:
        duplicate = repository.database.connection.execute(
            """
            SELECT id FROM news_evidence
            WHERE season_id = ? AND gameweek_id = ? AND source_url = ?
              AND evidence_at = ? AND COALESCE(player_season_id, 0) = COALESCE(
                  (SELECT id FROM player_seasons WHERE season_id = ? AND source_player_id = ?), 0)
            """,
            (
                season_row["season_id"],
                season_row["gameweek_id"],
                item.source_url,
                item.evidence_at.astimezone(UTC).isoformat(),
                season_row["season_id"],
                item.source_player_id,
            ),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(f"Duplicate evidence detected for source {item.source_url!r}")
        temporal_status = None
        if item.evidence_at < result.research_window_start:
            temporal_status = (
                "outside_research_window"
                if item.selected_player_status != "general"
                else "background_context"
            )
        ids.append(
            repository.add_news_evidence(
                season_code=season_code,
                gameweek_number=gameweek_number,
                evidence_type=item.evidence_type,
                summary=item.summary,
                confidence=item.confidence,
                evidence_at=item.evidence_at,
                source_player_id=item.source_player_id,
                source_url=item.source_url,
                schema_version=3,
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
                input_package_id=result.input_package_id,
                input_package_hash=result.input_package_hash,
                research_window_start=result.research_window_start,
                target_deadline=result.target_deadline,
                research_mode=result.research_mode,
                priority=item.priority,
                selected_player_status=item.selected_player_status,
                adjustment_support=item.adjustment_support,
                temporal_status=temporal_status,
                conflict_group_id=item.conflict_group_id,
                supporting_evidence=item.supporting_evidence,
                conflicting_evidence=item.conflicting_evidence,
                unresolved_uncertainty=item.unresolved_uncertainty,
                resolution_event=item.resolution_event,
                confidence_after_conflict=item.confidence_after_conflict,
                research_result_id=research_result_id,
            )
        )
    return tuple(ids)


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


def structured_news_v3_schema() -> dict[str, Any]:
    """Return a complete strict v3 example for the manual research chat."""

    return {
        "schema_version": 3,
        "prompt_version": "fpl-team-news-v3",
        "research_run_id": "research-run-id",
        "input_package_id": "tnp-package-id",
        "input_package_hash": "sha256-package-hash",
        "season_code": "2026-27",
        "gameweek": 1,
        "target_deadline": "2026-08-21T17:30:00+00:00",
        "research_mode": "preseason",
        "research_window_start": "2026-07-01T00:00:00+00:00",
        "generated_at": "2026-08-20T18:00:00+00:00",
        "coverage": [
            {
                "source_player_id": "123",
                "priority": "starting_xi",
                "status": "checked_no_material_evidence",
                "areas_checked": [
                    "injury",
                    "training",
                    "predicted_lineup",
                    "tactical_role",
                    "set_pieces",
                ],
                "latest_source_checked_at": "2026-08-20T18:00:00+00:00",
                "notes": None,
            }
        ],
        "evidence": [
            {
                "evidence_id": "evidence-1",
                "issue_id": None,
                "source_player_id": "123",
                "priority": "starting_xi",
                "selected_player_status": "selected",
                "evidence_type": "manager_quote",
                "fact_summary": "Manager said the player trained normally.",
                "source_name": "Official club press conference",
                "source_url": "https://example.invalid/direct-source",
                "published_at": "2026-08-20T16:00:00+00:00",
                "source_tier": "official",
                "confidence": "high",
                "model_area": "availability",
                "suggested_adjustment": None,
                "adjustment_basis": None,
                "adjustment_support": "informational",
                "requires_decision": True,
                "decision_question": "Does this resolve the availability concern?",
                "expiry": "2026-08-21T17:30:00+00:00",
                "context_scope": "current_window",
                "supporting_evidence": [],
                "conflicting_evidence": [],
                "unresolved_uncertainty": None,
                "resolution_event": None,
                "confidence_after_conflict": None,
            }
        ],
        "discoveries": [],
        "limitations": [],
    }


def _evidence_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw["evidence"]
    if not isinstance(items, list):
        raise ValueError("News evidence must be a list")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("Every news evidence item must be an object")
    return items


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _exact_fields(item: dict[str, Any], expected: set[str], index: int) -> None:
    if set(item) != expected:
        raise ValueError(f"Evidence item {index} must contain exactly {sorted(expected)}")


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
        raise ValueError(f"Evidence item {index} has an invalid {field}") from error
    if result.tzinfo is None:
        raise ValueError(f"Evidence item {index} {field} must include a timezone")
    return result.astimezone(UTC)


def _optional_timestamp(value: Any, field: str, index: int) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, field, index)


def _adjustment(value: Any, index: int) -> SuggestedAdjustment | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        raise ValueError(
            f"Evidence item {index} suggested_adjustment must contain only kind and value"
        )
    if value["kind"] != "expected_minutes_delta":
        raise ValueError(f"Evidence item {index} has unsupported adjustment kind")
    adjustment = value["value"]
    if isinstance(adjustment, bool) or not isinstance(adjustment, (int, float)):
        raise ValueError(f"Evidence item {index} adjustment value must be numeric")
    if not -90 <= float(adjustment) <= 90:
        raise ValueError(f"Evidence item {index} minutes adjustment must be between -90 and 90")
    return SuggestedAdjustment(value=float(adjustment), kind=value["kind"])
