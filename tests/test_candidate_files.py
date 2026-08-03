"""Every shipped candidate file must survive the promotion gate.

`declared_challenger_declaration` rebuilds a declaration and refuses it unless
it round-trips exactly. A candidate file missing a field that
`ProjectionModelConfig` has since gained still *loads* — the dataclass fills the
gap from its default — so nothing fails until a forward run is spent and the
gate rejects it. That is the worst possible moment to find out.

This has already happened twice: adding `team_strength_model` orphaned six
files, and a partial fix caught three of them. A test over the shipped files is
the only thing that makes the next field addition self-announcing.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from fpl_engine.declaration import ModelDeclaration
from fpl_engine.projections import ProjectionModelConfig
from fpl_engine.promotion import declared_challenger_declaration

CANDIDATE_DIRECTORY = Path("config/model_candidates")


def _projection_candidates() -> list[Path]:
    paths = []
    for path in sorted(CANDIDATE_DIRECTORY.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Gate-policy files live here too and are not model configurations.
        if "player_rate_prior_minutes" in payload.get("model_config", payload):
            paths.append(path)
    return paths


def test_the_candidate_directory_is_not_empty() -> None:
    assert _projection_candidates()


@pytest.mark.parametrize(
    "path", _projection_candidates(), ids=lambda path: path.stem
)
def test_every_candidate_survives_the_promotion_gate(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))

    # The exact call the forward-candidate runner makes. It raises rather than
    # silently filling defaults, which is the whole point.
    declaration = declared_challenger_declaration(
        {"candidate_key": path.stem, "model_config": payload}
    )

    assert declaration.as_dict() == payload


@pytest.mark.parametrize(
    "path", _projection_candidates(), ids=lambda path: path.stem
)
def test_every_candidate_declares_every_configuration_field(path: Path) -> None:
    """A missing field is a silent default, not a declaration."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    config = payload.get("model_config", payload)
    missing = sorted({field.name for field in fields(ProjectionModelConfig)} - set(config))

    assert not missing, (
        f"{path.name} does not declare {missing}. It would still run, taking "
        "the code default silently — which is exactly what a preregistered "
        "declaration must not do."
    )


@pytest.mark.parametrize(
    "path", _projection_candidates(), ids=lambda path: path.stem
)
def test_every_candidate_hashes_stably(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    declaration = ModelDeclaration.from_dict(payload)

    assert declaration.digest() == ModelDeclaration.from_dict(payload).digest()
    assert len(declaration.digest()) == 64
