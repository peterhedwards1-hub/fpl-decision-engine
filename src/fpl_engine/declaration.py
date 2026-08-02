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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .projections import DEFAULT_MODEL_CONFIG, ProjectionModelConfig
from .team_strength import ContextualAdjustment, TeamStrengthSettings

#: Bumped only when the serialised shape changes in a way that would alter an
#: existing candidate's digest.
DECLARATION_VERSION = 2


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
