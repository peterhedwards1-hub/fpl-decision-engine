"""The complete, hashable specification of a forecasting model.

A candidate declaration has to pin down everything that decides the forecast,
before the outcome. `ProjectionModelConfig` alone no longer does that: the
opponent-adjusted team-strength model reads a second object of declared
constants, and it can read a list of reviewed contextual adjustments. Both were
previously supplied at runtime from code defaults, which meant a candidate's
hash could stay identical while a later edit to a default changed every
forecast it produced.

Persisting the resulting state afterwards records what happened. It does not
make the declaration immutable. Only hashing the inputs does that.

The legacy shape — a bare `ProjectionModelConfig` dictionary — is still
accepted and still hashes to the same digest it always did, so the three
candidates already registered against 2026/27 keep their identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .projections import DEFAULT_MODEL_CONFIG, ProjectionModelConfig
from .team_strength import ContextualAdjustment, TeamStrengthSettings

#: Bumped only when the serialised shape changes in a way that would alter an
#: existing candidate's digest.
DECLARATION_VERSION = 2

# Bare projection declarations predate ``team_strength_model``.  This is an
# explicit historical schema, rather than ``current fields minus one``, so a
# later field addition cannot silently become an unpinned default in an old
# registration.  It preserves the identity and semantics of declarations
# stored before the component selector was introduced.
_BARE_PROJECTION_CONFIG_V1_FIELDS = frozenset(
    {
        "player_rate_prior_minutes",
        "minutes_prior_matches",
        "team_prior_matches",
        "home_attack_multiplier",
        "away_attack_multiplier",
        "minimum_team_multiplier",
        "maximum_team_multiplier",
        "minutes_model",
        "playing_time_artifact",
        "recent_gameweeks",
        "recent_evidence_weight",
        "appearance_prior_matches",
        "appearance_prior_probability",
        "conditional_minutes_prior_appearances",
        "team_minutes_per_fixture",
        "enforce_team_minutes",
        "minutes_allocation",
        "scoring_recent_evidence_weight",
        "scoring_event_source",
        "coherent_assist_unassisted_goal_fraction",
        "coherent_penalty_goal_fraction",
        "coherent_role_shrinkage_minutes",
        "coherent_transfer_shrinkage",
        "team_form_half_life_gameweeks",
        "team_assist_per_goal_prior",
        "defensive_contribution_model",
        "include_penalty_events",
        "team_strength_carry_forward",
        "carry_forward_regression_matches",
        "promoted_team_attack_multiplier",
        "promoted_team_defence_multiplier",
        "cold_start_prior",
        "cold_start_price_elasticity",
        "cold_start_minimum_factor",
        "cold_start_maximum_factor",
        "minutes_reconciliation_mode",
        "minutes_reconciliation_max_relative_adjustment",
        "minutes_reconciliation_max_absolute_adjustment",
        "minutes_reconciliation_warning_deficit",
        "participation_start_prior_probability",
        "participation_start_prior_matches",
        "participation_substitute_prior_probability",
        "participation_substitute_prior_matches",
        "participation_start_minutes_prior",
        "participation_substitute_minutes_prior",
        "participation_role_decay_per_gameweek",
    }
)


@dataclass(frozen=True)
class ModelDeclaration:
    """Everything a forecast depends on, in one hashable object."""

    model_config: ProjectionModelConfig = DEFAULT_MODEL_CONFIG
    #: Only meaningful when `model_config.team_strength_model` is
    #: "opponent_adjusted". Left as None for the incumbent so its declaration
    #: keeps the legacy shape and the legacy digest.
    team_strength_settings: TeamStrengthSettings | None = None
    #: The reviewed contextual-adjustment manifest, frozen with the rest.
    contextual_adjustments: tuple[ContextualAdjustment, ...] = field(
        default_factory=tuple
    )
    #: Serialisation-only compatibility for the bare schema above.  It is not
    #: part of a model's mathematical identity and is never used for new
    #: declarations.
    _bare_config_schema: str | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if (
            self.team_strength_settings is not None
            and self.model_config.team_strength_model != "opponent_adjusted"
        ):
            raise ValueError(
                "Team-strength settings only apply to the "
                "'opponent_adjusted' team strength model"
            )
        if (
            self.contextual_adjustments
            and self.model_config.team_strength_model != "opponent_adjusted"
        ):
            raise ValueError(
                "Contextual adjustments are only read by the "
                "'opponent_adjusted' team strength model, so declaring them "
                "against another model would have no effect"
            )
        if self.team_strength_settings is not None:
            # These constants are consumed by both layers.  The projection
            # configuration is the canonical source and TeamStrengthSettings
            # is adapted from it at runtime.  Allowing conflicting declared
            # copies made the declaration needlessly misleading: changing the
            # inactive value changed the hash but not the forecast.
            for setting_name, config_name in (
                self.team_strength_settings.SHARED_WITH_PROJECTION_CONFIG
            ):
                setting_value = getattr(self.team_strength_settings, setting_name)
                config_value = getattr(self.model_config, config_name)
                if not math.isclose(
                    setting_value, config_value, rel_tol=0.0, abs_tol=1e-12
                ):
                    raise ValueError(
                        "Conflicting declared team-strength setting "
                        f"{setting_name}={setting_value!r}; the canonical "
                        f"projection setting {config_name} is {config_value!r}"
                    )
        seen: set[tuple[str, str, int]] = set()
        for entry in self.contextual_adjustments:
            key = (
                entry.source_team_id,
                entry.category,
                entry.effective_from_gameweek,
            )
            if key in seen:
                raise ValueError(
                    f"Duplicate adjustment for team {entry.source_team_id!r}, "
                    f"category {entry.category!r}, from Gameweek "
                    f"{entry.effective_from_gameweek}"
                )
            seen.add(key)

    @property
    def resolved_settings(self) -> TeamStrengthSettings:
        """The settings as the engine will actually apply them.

        The projection config owns every constant the two share, so what is
        declared and what runs are the same object.
        """

        return (
            self.team_strength_settings or TeamStrengthSettings()
        ).for_projection_config(self.model_config)

    def as_dict(self) -> dict[str, Any]:
        """The canonical serialisation, and the thing that gets hashed.

        A declaration carrying nothing beyond the projection config serialises
        to the bare config dictionary — the legacy shape — so candidates
        registered before this existed keep their digests.
        """

        config = asdict(self.model_config)
        if self.team_strength_settings is None and not self.contextual_adjustments:
            if self._bare_config_schema == "projection-config-v1":
                return {
                    key: config[key]
                    for key in sorted(_BARE_PROJECTION_CONFIG_V1_FIELDS)
                }
            return config
        return {
            "declaration_version": DECLARATION_VERSION,
            "model_config": config,
            "team_strength_settings": (
                None
                if self.team_strength_settings is None
                else asdict(self.team_strength_settings)
            ),
            "contextual_adjustments": [
                asdict(entry)
                for entry in sorted(
                    self.contextual_adjustments,
                    key=lambda entry: (
                        entry.source_team_id,
                        entry.category,
                        entry.effective_from_gameweek,
                    ),
                )
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ModelDeclaration:
        """Rebuild a declaration from either shape."""

        if "declaration_version" not in payload:
            keys = set(payload)
            if keys == _BARE_PROJECTION_CONFIG_V1_FIELDS:
                return cls(
                    model_config=ProjectionModelConfig(
                        **{**payload, "team_strength_model": "raw_goals"}
                    ),
                    _bare_config_schema="projection-config-v1",
                )
            return cls(model_config=ProjectionModelConfig(**payload))
        version = int(payload["declaration_version"])
        if version > DECLARATION_VERSION:
            raise ValueError(
                f"Declaration version {version} is newer than this code "
                f"understands ({DECLARATION_VERSION})"
            )
        settings = payload.get("team_strength_settings")
        return cls(
            model_config=ProjectionModelConfig(**payload["model_config"]),
            team_strength_settings=(
                None if settings is None else TeamStrengthSettings(**settings)
            ),
            contextual_adjustments=tuple(
                ContextualAdjustment(**entry)
                for entry in payload.get("contextual_adjustments", ())
            ),
        )

    def digest(self) -> str:
        return declaration_digest(self.as_dict())

    def adjustments_before(self, cutoff: datetime) -> tuple[ContextualAdjustment, ...]:
        """The adjustments reviewed strictly before a moment.

        A declaration is only preregistration if the judgements inside it
        predate the thing they are scored against. This makes that checkable
        rather than assumed.
        """

        return tuple(
            entry
            for entry in self.contextual_adjustments
            if datetime.fromisoformat(entry.reviewed_at).astimezone(UTC)
            < cutoff.astimezone(UTC)
        )


def declaration_digest(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON form, matching the registration path."""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
