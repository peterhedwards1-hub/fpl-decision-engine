"""A candidate declaration has to pin down everything that decides a forecast.

Recording the derivation afterwards says what happened. It does not make a
preregistration immutable — only hashing the inputs does that. These tests
exist because the projection config alone stopped being the whole model the
moment team strength grew its own constants and an adjustment manifest.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_engine.declaration import (
    DECLARATION_VERSION,
    ModelDeclaration,
    declaration_digest,
)
from fpl_engine.projections import (
    CORRECTED_V4_MODEL_CONFIG,
    OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG,
    PRESEASON_V5_MODEL_CONFIG,
    ProjectionModelConfig,
    RatesProjectionModel,
)
from fpl_engine.team_strength import ContextualAdjustment, TeamStrengthSettings

CHALLENGER = OPPONENT_ADJUSTED_TEAM_STRENGTH_V1_MODEL_CONFIG


def _settings(**overrides: float) -> TeamStrengthSettings:
    """Settings with every shared value derived from the model config."""

    return replace(
        TeamStrengthSettings().for_projection_config(CHALLENGER), **overrides
    )


def _adjustment(**kwargs) -> ContextualAdjustment:
    defaults = {
        "source_team_id": "1",
        "category": "long_term_injury",
        "attack_multiplier": 0.88,
        "rationale": "First-choice striker out until December.",
        "source": "club statement",
        "confidence": "high",
        "reviewed_at": "2026-08-01T09:00:00+00:00",
        "reviewed_by": "p.edwards",
    }
    return ContextualAdjustment(**{**defaults, **kwargs})


def test_a_settings_change_moves_the_digest(tmp_path) -> None:
    """The defect this exists to close.

    Previously the settings came from code defaults at runtime, so editing one
    changed every forecast the candidate produced while its hash stayed put.
    """

    base = ModelDeclaration(
        model_config=CHALLENGER, team_strength_settings=_settings()
    )
    for field, value in (
        ("prior_matches_intact_squad", 20.0),
        ("promoted_attack", 0.70),
        ("solver_prior_matches", 1.0),
    ):
        drifted = replace(
            base,
            team_strength_settings=replace(
                base.team_strength_settings, **{field: value}
            ),
        )
        assert drifted.digest() != base.digest(), f"{field} left the digest alone"


def test_an_adjustment_manifest_moves_the_digest() -> None:
    base = ModelDeclaration(
        model_config=CHALLENGER, team_strength_settings=_settings()
    )
    with_one = replace(base, contextual_adjustments=(_adjustment(),))

    assert with_one.digest() != base.digest()
    # Every audit field is part of the identity, including who reviewed it.
    for field, value in (
        ("attack_multiplier", 0.90),
        ("effective_from_gameweek", 6),
        ("effective_to_gameweek", 20),
        ("rationale", "A different reason entirely."),
        ("source", "press conference"),
        ("confidence", "low"),
        ("reviewed_at", "2026-08-02T09:00:00+00:00"),
        ("reviewed_by", "someone.else"),
    ):
        altered = replace(
            with_one, contextual_adjustments=(_adjustment(**{field: value}),)
        )
        assert altered.digest() != with_one.digest(), f"{field} is not hashed"


def test_the_manifest_order_does_not_change_the_digest() -> None:
    first = _adjustment(source_team_id="1")
    second = _adjustment(source_team_id="2", category="manager_change")

    forwards = ModelDeclaration(
        model_config=CHALLENGER, contextual_adjustments=(first, second)
    )
    backwards = ModelDeclaration(
        model_config=CHALLENGER, contextual_adjustments=(second, first)
    )

    assert forwards.digest() == backwards.digest()


def test_a_declaration_round_trips() -> None:
    original = ModelDeclaration(
        model_config=CHALLENGER,
        team_strength_settings=_settings(prior_matches_intact_squad=9.0),
        contextual_adjustments=(_adjustment(),),
    )

    rebuilt = ModelDeclaration.from_dict(original.as_dict())

    assert rebuilt == original
    assert rebuilt.digest() == original.digest()
    assert original.as_dict()["declaration_version"] == DECLARATION_VERSION


def test_the_legacy_shape_keeps_its_identity() -> None:
    """Candidates registered before this existed must not be re-identified."""

    for config in (CORRECTED_V4_MODEL_CONFIG, PRESEASON_V5_MODEL_CONFIG):
        declaration = ModelDeclaration(model_config=config)
        # A declaration carrying nothing extra serialises to the bare config...
        assert declaration.as_dict() == asdict(config)
        # ...and therefore hashes to exactly what it hashed to before.
        assert declaration.digest() == declaration_digest(asdict(config))
        assert ModelDeclaration.from_dict(asdict(config)) == declaration


def test_the_previous_bare_schema_rebuilds_without_changing_its_hash() -> None:
    """A stored pre-component registration must remain reproducible."""

    historic = asdict(PRESEASON_V5_MODEL_CONFIG)
    historic.pop("team_strength_model")

    declaration = ModelDeclaration.from_dict(historic)

    assert declaration.model_config.team_strength_model == "raw_goals"
    assert declaration.as_dict() == historic
    assert declaration.digest() == declaration_digest(historic)


def test_a_declaration_refuses_settings_the_model_would_ignore() -> None:
    with pytest.raises(ValueError, match="only apply to the 'opponent_adjusted'"):
        ModelDeclaration(
            model_config=CORRECTED_V4_MODEL_CONFIG,
            team_strength_settings=_settings(),
        )
    with pytest.raises(ValueError, match="would have no effect"):
        ModelDeclaration(
            model_config=CORRECTED_V4_MODEL_CONFIG,
            contextual_adjustments=(_adjustment(),),
        )
    with pytest.raises(ValueError, match="Duplicate adjustment"):
        ModelDeclaration(
            model_config=CHALLENGER,
            contextual_adjustments=(_adjustment(), _adjustment()),
        )


def test_a_newer_declaration_version_is_refused() -> None:
    payload = ModelDeclaration(
        model_config=CHALLENGER, team_strength_settings=_settings()
    ).as_dict()
    payload["declaration_version"] = DECLARATION_VERSION + 1

    with pytest.raises(ValueError, match="newer than this code understands"):
        ModelDeclaration.from_dict(payload)


def test_the_resolved_settings_are_the_ones_the_engine_applies() -> None:
    declaration = ModelDeclaration(
        model_config=CHALLENGER,
        team_strength_settings=_settings(prior_matches_intact_squad=9.0),
    )

    from fpl_engine.config import load_season_rules

    rules = load_season_rules(Path("config/seasons/2026-27.json"))
    model = RatesProjectionModel(
        None,
        rules,
        config=declaration.model_config,
        team_strength_settings=declaration.team_strength_settings,
    )

    assert model.team_strength_settings == declaration.resolved_settings
    # The projection config still owns the shared constants.
    assert declaration.resolved_settings.away_factor == (
        CHALLENGER.away_attack_multiplier
    )
    assert declaration.resolved_settings.prior_matches_intact_squad == 9.0


def test_a_declaration_refuses_conflicting_shared_team_strength_values() -> None:
    with pytest.raises(ValueError, match="Conflicting declared team-strength"):
        ModelDeclaration(
            model_config=CHALLENGER,
            team_strength_settings=_settings(away_factor=0.92),
        )


def test_adjustments_can_be_proven_to_predate_a_deadline() -> None:
    declaration = ModelDeclaration(
        model_config=CHALLENGER,
        contextual_adjustments=(
            _adjustment(reviewed_at="2026-08-01T09:00:00+00:00"),
            _adjustment(
                source_team_id="2",
                category="manager_change",
                reviewed_at="2026-08-14T09:00:00+00:00",
            ),
        ),
    )

    before = declaration.adjustments_before(datetime(2026, 8, 10, tzinfo=UTC))

    assert len(before) == 1
    assert before[0].source_team_id == "1"
    assert len(declaration.adjustments_before(datetime(2026, 9, 1, tzinfo=UTC))) == 2


def test_a_capture_runs_the_declared_settings_not_the_code_defaults(
    tmp_path,
) -> None:
    """The end-to-end form of the defect.

    Registering a candidate whose declaration pins an unusual team-strength
    setting, then capturing a forecast, must produce the forecast that setting
    implies — not the one the code default implies.
    """

    from test_promotion import FORWARD_SOURCE, _forward_bundle

    from fpl_engine.capture import capture_gameweek_forecasts
    from fpl_engine.config import load_season_rules
    from fpl_engine.history.database import HistoricalDatabase
    from fpl_engine.promotion import register_forward_candidate

    rules = load_season_rules(Path("config/seasons/2026-27.json"))
    declared = ModelDeclaration(
        model_config=CHALLENGER,
        # Nothing like the default of 12.0, so the difference is unmissable.
        team_strength_settings=_settings(
            prior_matches_intact_squad=1.0,
            prior_matches_rebuilt_squad=1.0,
            promoted_prior_matches=1.0,
        ),
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        register_forward_candidate(
            database,
            candidate_key="pinned",
            season_code="2026-27",
            model_version="pinned-v1",
            model_config=declared.as_dict(),
            registered_at=datetime(2026, 7, 30, tzinfo=UTC),
        )
        capture = capture_gameweek_forecasts(
            database,
            rules,
            season_code="2026-27",
            gameweek=2,
            horizon_gameweeks=1,
            generated_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        )
        run_id = next(
            entry["projection_run_id"]
            for entry in capture["forecasts"]
            if entry["key"] == "pinned"
        )
        assumptions = json.loads(
            database.connection.execute(
                "SELECT assumptions_json FROM projection_runs WHERE id = ?",
                (run_id,),
            ).fetchone()["assumptions_json"]
        )

    settings = assumptions["team_strength_state"]["settings"]
    assert settings["prior_matches_intact_squad"] == 1.0
    assert settings["prior_matches_rebuilt_squad"] == 1.0
    assert settings["promoted_prior_matches"] == 1.0
    # And the shared constants still follow the projection config.
    assert settings["away_factor"] == CHALLENGER.away_attack_multiplier


def test_the_shipped_candidate_declares_its_whole_model() -> None:
    payload = json.loads(
        Path(
            "config/model_candidates/opponent-adjusted-team-strength-v1.json"
        ).read_text(encoding="utf-8")
    )
    declaration = ModelDeclaration.from_dict(payload)

    # Not the legacy shape: the settings are pinned, so a later edit to a code
    # default cannot change what this candidate forecasts.
    assert payload["declaration_version"] == DECLARATION_VERSION
    assert declaration.team_strength_settings is not None
    assert declaration.model_config == CHALLENGER
    assert declaration.model_config.team_strength_model == "opponent_adjusted"
    # Shipped with no adjustments; the mechanism is declared, not exercised.
    assert declaration.contextual_adjustments == ()
    assert declaration.as_dict() == payload


def test_every_shipped_forecast_candidate_round_trips_through_the_gate() -> None:
    """A partial JSON file silently imports code defaults and cannot qualify."""

    expected_fields = set(ProjectionModelConfig.__dataclass_fields__)
    candidates = sorted(Path("config/model_candidates").glob("*.json"))
    assert candidates
    for path in candidates:
        if path.name == "forward-promotion-policy-v1.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        config_payload = payload.get("model_config", payload)
        assert set(config_payload) == expected_fields, path.name
        declaration = ModelDeclaration.from_dict(payload)
        assert declaration.as_dict() == payload, path.name


def test_component_challengers_change_only_their_declared_component() -> None:
    """The challenger matrix is an ablation, not a collection of defaults."""

    root = Path("config/model_candidates")
    control = ModelDeclaration.from_dict(
        json.loads((root / "preseason-priors-v1.json").read_text())
    ).model_config
    cases = {
        "uncertain-minutes-v1.json": {
            "minutes_model",
            "minutes_allocation",
            "minutes_reconciliation_mode",
        },
        "coherent-player-allocation-v1.json": {"scoring_event_source"},
        "coherent-points-minutes-v1.json": {
            "minutes_model",
            "minutes_allocation",
            "minutes_reconciliation_mode",
            "scoring_event_source",
        },
        "coherent-allocation-participation-v1.json": {
            "minutes_model",
            "minutes_allocation",
            "minutes_reconciliation_mode",
            "scoring_event_source",
        },
        "opponent-adjusted-team-strength-only-v1.json": {
            "team_strength_model"
        },
        "opponent-adjusted-coherent-allocation-v1.json": {
            "team_strength_model",
            "scoring_event_source",
        },
        "opponent-adjusted-coherent-participation-v1.json": {
            "team_strength_model",
            "minutes_model",
            "minutes_allocation",
            "minutes_reconciliation_mode",
            "scoring_event_source",
        },
    }
    control_values = asdict(control)
    for filename, expected_changes in cases.items():
        candidate = ModelDeclaration.from_dict(
            json.loads((root / filename).read_text(encoding="utf-8"))
        ).model_config
        changed = {
            field
            for field, value in asdict(candidate).items()
            if value != control_values[field]
        }
        assert changed == expected_changes, filename


def test_component_matrix_can_be_registered_and_captured_from_its_files(tmp_path) -> None:
    """Exercise the same declaration path before a real capture slot is used."""

    from test_promotion import FORWARD_SOURCE, _forward_bundle

    from fpl_engine.capture import capture_gameweek_forecasts
    from fpl_engine.config import load_season_rules
    from fpl_engine.history.database import HistoricalDatabase
    from fpl_engine.promotion import register_forward_candidate

    root = Path("config/model_candidates")
    candidates = (
        "coherent-player-allocation-v1.json",
        "uncertain-minutes-v1.json",
        "coherent-points-minutes-v1.json",
        "opponent-adjusted-team-strength-only-v1.json",
        "opponent-adjusted-coherent-allocation-v1.json",
        "opponent-adjusted-coherent-participation-v1.json",
    )
    rules = load_season_rules(Path("config/seasons/2026-27.json"))
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(FORWARD_SOURCE, _forward_bundle())
        for number, filename in enumerate(candidates):
            register_forward_candidate(
                database,
                candidate_key=f"dry-run-{number}",
                season_code="2026-27",
                model_version=f"dry-run-{number}-v1",
                model_config=json.loads((root / filename).read_text()),
                registered_at=datetime(2026, 7, 30, tzinfo=UTC),
            )
        result = capture_gameweek_forecasts(
            database,
            rules,
            season_code="2026-27",
            gameweek=2,
            horizon_gameweeks=1,
            generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        )

    captured = {entry["key"] for entry in result["forecasts"]}
    assert {f"dry-run-{number}" for number in range(len(candidates))} <= captured
