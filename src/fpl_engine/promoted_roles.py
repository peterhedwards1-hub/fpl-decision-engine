"""Previous-division playing time for players who came up with their club.

A player promoted with their club has no Premier League record, so the
projection gives them the cold-start positional prior: the model knows their
position and their price and nothing else. It cannot tell a Championship
captain who started forty-six matches from a squad player who came off the
bench eleven times. Both are simply "a promoted club's midfielder".

Previous-division playing time is the one piece of evidence that can separate
them *without* claiming anything about how they will score. Minutes are
minutes. A player who started every week in the Championship is more likely to
start in the Premier League than a fringe player at the same club, and that is
a statement about role, not about quality.

So this module reads exactly five things — appearances, starts, substitute
appearances, minutes and share of team minutes — and is allowed to move exactly
four:

- appearance probability;
- start probability;
- expected minutes;
- 60-minute probability.

It has no access to goals, assists, clean sheets or fantasy points, by
construction rather than by discipline: the storage table has no column for
them and :func:`load_role_document` refuses a file that carries one. A
Championship goal rate is not a Premier League goal rate and nothing here is
entitled to pretend otherwise.

Every estimate is shrunk toward the positional prior the model already uses, at
a declared prior weight in team-matches. A player with a full season of
Championship starts moves a long way; a player with six appearances barely
moves at all. At infinite shrinkage this module is a no-op, which is what makes
"with" and "without" a single-parameter comparison.

Identity matching is deliberately strict. An official FPL player code is used
when the source supplies one. Otherwise a name must match exactly, after
normalisation, **within the same club** — never across clubs, and never
fuzzily. A name that matches two players is treated as matching neither. The
cost of a missed match is that a player keeps the existing prior; the cost of a
wrong match is a fabricated role for a real footballer, and those are not
comparable risks.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import Position
from .history.database import HistoricalDatabase
from .optimisation import CandidatePlayer
from .projections import MINUTES_PRIORS

DEFAULT_ROLE_DATA_PATH = Path("data/reference/championship-player-roles.json")
SUPPORTED_FORMAT = "fpl-decision-engine/championship-player-roles/v1"

#: Columns that would carry scoring information. Their presence is a contract
#: violation, not a field to ignore: a file that carries them was built for a
#: different purpose and should not be loaded by this module at all.
FORBIDDEN_SCORING_FIELDS = frozenset(
    {
        "goals",
        "goals_scored",
        "assists",
        "clean_sheets",
        "goals_conceded",
        "saves",
        "bonus",
        "bps",
        "points",
        "total_points",
        "fantasy_points",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "yellow_cards",
        "red_cards",
    }
)

#: Shrinkage strength, in team-matches. A player needs roughly this many
#: matches of Championship evidence before it outweighs the positional prior.
#: Declared, not fitted.
DEFAULT_ROLE_PRIOR_MATCHES = 15.0

#: The share of a promoted club's eligible players that must carry usable role
#: evidence before the treatment may be adopted at all. Below this, applying it
#: would move some players and leave their direct competitors on the prior,
#: which distorts a squad decision more than leaving everyone on the prior.
MINIMUM_ROLE_COVERAGE = 0.60


class RoleDataError(ValueError):
    """Raised when the role reference file violates its contract."""


@dataclass(frozen=True)
class ChampionshipPlayerRole:
    club_name: str
    player_name: str
    official_fpl_code: str | None
    appearances: int
    starts: int
    substitute_appearances: int
    minutes: int
    team_matches: int

    @property
    def start_rate(self) -> float:
        return self.starts / self.team_matches

    @property
    def appearance_rate(self) -> float:
        return self.appearances / self.team_matches

    @property
    def minutes_share(self) -> float:
        """Share of the club's available minutes, ignoring stoppage time."""

        return self.minutes / (self.team_matches * 90.0)


@dataclass(frozen=True)
class RoleDocument:
    format: str
    season_code: str
    source: dict[str, Any]
    roles: tuple[ChampionshipPlayerRole, ...]


def load_role_document(
    path: str | Path = DEFAULT_ROLE_DATA_PATH,
) -> RoleDocument:
    """Read and validate the role reference file.

    Raises rather than returning an empty document when the file is missing,
    so a caller has to decide explicitly what an absent file means.
    """

    location = Path(path)
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RoleDataError(
            f"Championship role reference file not found: {location}"
        ) from error
    except json.JSONDecodeError as error:
        raise RoleDataError(f"{location} is not valid JSON: {error}") from error
    if raw.get("format") != SUPPORTED_FORMAT:
        raise RoleDataError(
            f"{location} declares format {raw.get('format')!r}; "
            f"{SUPPORTED_FORMAT!r} is required"
        )
    source = raw.get("source")
    if not isinstance(source, dict) or not source.get("name") or not source.get("url"):
        raise RoleDataError(
            f"{location} must record a source name and url for provenance"
        )
    roles: list[ChampionshipPlayerRole] = []
    for entry in raw.get("players") or ():
        offending = FORBIDDEN_SCORING_FIELDS & set(entry)
        if offending:
            raise RoleDataError(
                f"{location}: role evidence may not carry scoring fields "
                f"({', '.join(sorted(offending))}). Championship scoring is "
                "not convertible to Premier League scoring and this module "
                "must not be able to import it."
            )
        starts = int(entry["starts"])
        substitutes = int(entry["substitute_appearances"])
        appearances = int(entry["appearances"])
        if starts + substitutes != appearances:
            raise RoleDataError(
                f"{location}: {entry.get('player_name')} records {starts} "
                f"starts and {substitutes} substitute appearances but "
                f"{appearances} appearances"
            )
        team_matches = int(entry["team_matches"])
        if team_matches <= 0:
            raise RoleDataError(
                f"{location}: {entry.get('player_name')} has no team matches"
            )
        if appearances > team_matches:
            raise RoleDataError(
                f"{location}: {entry.get('player_name')} appeared in "
                f"{appearances} of {team_matches} matches"
            )
        if int(entry["minutes"]) > team_matches * 120:
            raise RoleDataError(
                f"{location}: {entry.get('player_name')} records more minutes "
                "than the club could have played"
            )
        roles.append(
            ChampionshipPlayerRole(
                club_name=str(entry["club_name"]),
                player_name=str(entry["player_name"]),
                official_fpl_code=(
                    None
                    if entry.get("official_fpl_code") in (None, "")
                    else str(entry["official_fpl_code"])
                ),
                appearances=appearances,
                starts=starts,
                substitute_appearances=substitutes,
                minutes=int(entry["minutes"]),
                team_matches=team_matches,
            )
        )
    return RoleDocument(
        format=str(raw["format"]),
        season_code=str(raw["season_code"]),
        source=source,
        roles=tuple(roles),
    )


def import_role_document(
    database: HistoricalDatabase,
    document: RoleDocument,
    *,
    source_url: str = "",
    source_sha256: str = "",
    imported_at: datetime | None = None,
) -> dict[str, Any]:
    """Store role evidence against an already-imported Championship season."""

    row = database.connection.execute(
        "SELECT id FROM championship_seasons WHERE season_code = ?",
        (document.season_code,),
    ).fetchone()
    if row is None:
        raise RoleDataError(
            f"Championship season {document.season_code} must be imported "
            "before role evidence can be attached to it"
        )
    season_id = int(row["id"])
    moment = (imported_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    with database.connection:
        database.connection.execute(
            "DELETE FROM championship_player_roles WHERE championship_season_id = ?",
            (season_id,),
        )
        database.connection.executemany(
            """
            INSERT INTO championship_player_roles (
                championship_season_id, club_name, player_name,
                official_fpl_code, appearances, starts,
                substitute_appearances, minutes, team_matches,
                source_url, source_sha256, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    season_id,
                    role.club_name,
                    role.player_name,
                    role.official_fpl_code,
                    role.appearances,
                    role.starts,
                    role.substitute_appearances,
                    role.minutes,
                    role.team_matches,
                    source_url or str(document.source.get("url", "")),
                    source_sha256 or str(document.source.get("sha256", "")),
                    moment,
                )
                for role in document.roles
            ],
        )
    return {"season_code": document.season_code, "players": len(document.roles)}


def stored_roles(
    database: HistoricalDatabase, *, championship_season_code: str
) -> tuple[ChampionshipPlayerRole, ...]:
    rows = database.connection.execute(
        """
        SELECT r.* FROM championship_player_roles r
        JOIN championship_seasons s ON s.id = r.championship_season_id
        WHERE s.season_code = ?
        """,
        (championship_season_code,),
    ).fetchall()
    return tuple(
        ChampionshipPlayerRole(
            club_name=str(row["club_name"]),
            player_name=str(row["player_name"]),
            official_fpl_code=row["official_fpl_code"],
            appearances=int(row["appearances"]),
            starts=int(row["starts"]),
            substitute_appearances=int(row["substitute_appearances"]),
            minutes=int(row["minutes"]),
            team_matches=int(row["team_matches"]),
        )
        for row in rows
    )


# --------------------------------------------------------------------------
# Identity matching
# --------------------------------------------------------------------------


def _normalise_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    cleaned = "".join(
        character.lower()
        for character in stripped
        if character.isalnum() or character.isspace()
    )
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class RoleMatch:
    source_player_id: str
    role: ChampionshipPlayerRole
    method: str


def match_roles(
    roles: tuple[ChampionshipPlayerRole, ...],
    players: tuple[dict[str, Any], ...],
) -> tuple[dict[str, RoleMatch], list[dict[str, Any]]]:
    """Link role rows to Premier League players, conservatively.

    ``players`` carries ``source_player_id``, ``club_name``, ``name`` and an
    optional ``official_fpl_code``. Returns the matches and every unresolved
    row with the reason it stayed unresolved, because an unexplained gap in
    coverage and a deliberate refusal to guess look identical in a count.
    """

    by_code: dict[str, list[dict[str, Any]]] = {}
    by_club_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for player in players:
        code = player.get("official_fpl_code")
        if code:
            by_code.setdefault(str(code), []).append(player)
        key = (_normalise_name(str(player["club_name"])), _normalise_name(str(player["name"])))
        by_club_name.setdefault(key, []).append(player)

    matches: dict[str, RoleMatch] = {}
    unresolved: list[dict[str, Any]] = []
    for role in roles:
        candidates: list[dict[str, Any]] = []
        method = ""
        if role.official_fpl_code:
            candidates = by_code.get(role.official_fpl_code, [])
            method = "official_fpl_code"
        if not candidates:
            key = (
                _normalise_name(role.club_name),
                _normalise_name(role.player_name),
            )
            candidates = by_club_name.get(key, [])
            method = "club_and_exact_name"
        if not candidates:
            unresolved.append(
                {
                    "club_name": role.club_name,
                    "player_name": role.player_name,
                    "reason": "no Premier League player matched",
                }
            )
            continue
        if len(candidates) > 1:
            # Two footballers, one name. Guessing would attach a real
            # player's season to the wrong person.
            unresolved.append(
                {
                    "club_name": role.club_name,
                    "player_name": role.player_name,
                    "reason": (
                        f"{len(candidates)} Premier League players matched; "
                        "an ambiguous identity is treated as no match"
                    ),
                }
            )
            continue
        player_id = str(candidates[0]["source_player_id"])
        if player_id in matches:
            unresolved.append(
                {
                    "club_name": role.club_name,
                    "player_name": role.player_name,
                    "reason": "a different role row already matched this player",
                }
            )
            continue
        matches[player_id] = RoleMatch(
            source_player_id=player_id, role=role, method=method
        )
    return matches, unresolved


# --------------------------------------------------------------------------
# Shrinkage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleEstimate:
    """One player's shrunk role, and the prior it was shrunk toward."""

    source_player_id: str
    position: Position
    prior_appearance_probability: float
    prior_sixty_probability: float
    prior_expected_minutes: float
    appearance_probability: float
    start_probability: float
    expected_minutes: float
    sixty_probability: float
    evidence_matches: int
    shrinkage_weight: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_player_id": self.source_player_id,
            "position": self.position.value,
            "prior_appearance_probability": round(
                self.prior_appearance_probability, 4
            ),
            "appearance_probability": round(self.appearance_probability, 4),
            "start_probability": round(self.start_probability, 4),
            "prior_expected_minutes": round(self.prior_expected_minutes, 2),
            "expected_minutes": round(self.expected_minutes, 2),
            "prior_sixty_probability": round(self.prior_sixty_probability, 4),
            "sixty_probability": round(self.sixty_probability, 4),
            "evidence_matches": self.evidence_matches,
            "shrinkage_weight": round(self.shrinkage_weight, 4),
        }


def shrink_role_evidence(
    role: ChampionshipPlayerRole,
    *,
    source_player_id: str,
    position: Position,
    prior_appearance_probability: float,
    prior_sixty_probability: float,
    prior_expected_minutes: float,
    prior_matches: float = DEFAULT_ROLE_PRIOR_MATCHES,
) -> RoleEstimate:
    """Blend one player's Championship role with the positional prior.

    The blend is a straight beta-style shrinkage in team-matches: the prior
    counts for ``prior_matches`` matches and the evidence for as many matches
    as the club actually played. Nothing is fitted; the weight is declared.
    """

    if prior_matches <= 0:
        raise ValueError("Role prior matches must be positive")
    played = float(role.team_matches)
    weight = played / (played + prior_matches)
    appearance = (
        weight * role.appearance_rate + (1.0 - weight) * prior_appearance_probability
    )
    prior_start = prior_appearance_probability * prior_sixty_probability
    start = weight * role.start_rate + (1.0 - weight) * prior_start
    minutes = weight * (role.minutes_share * 90.0) + (1.0 - weight) * (
        prior_expected_minutes
    )
    conditional = MINUTES_PRIORS[position]["sixty_probability_given_appearance"]
    # A start is the route to sixty minutes; a substitute appearance almost
    # never is. Splitting the two is the whole point of reading starts
    # separately from appearances.
    substitute_rate = max(0.0, appearance - start)
    sixty = min(
        appearance,
        start * conditional + substitute_rate * 0.05,
    )
    return RoleEstimate(
        source_player_id=source_player_id,
        position=position,
        prior_appearance_probability=prior_appearance_probability,
        prior_sixty_probability=prior_sixty_probability,
        prior_expected_minutes=prior_expected_minutes,
        appearance_probability=min(1.0, max(0.0, appearance)),
        start_probability=min(1.0, max(0.0, start)),
        expected_minutes=max(0.0, minutes),
        sixty_probability=min(1.0, max(0.0, sixty)),
        evidence_matches=role.team_matches,
        shrinkage_weight=weight,
    )


def apply_role_estimates(
    candidates: tuple[CandidatePlayer, ...],
    estimates: dict[str, RoleEstimate],
) -> tuple[CandidatePlayer, ...]:
    """Rewrite availability on the candidates the evidence covers.

    Expected points are rescaled by the change in appearance probability, and
    by nothing else. That is the honest reach of minutes evidence: a player
    expected to play twice as often is worth roughly twice as much, but
    Championship minutes say nothing about their per-minute scoring rate and
    this must not pretend otherwise. A rescale is an approximation to
    reprojecting, and it is used only because the alternative — re-running the
    projection with a role overlay — would let previous-division evidence
    reach the scoring model, which is out of scope.
    """

    adjusted: list[CandidatePlayer] = []
    for player in candidates:
        estimate = estimates.get(player.source_player_id)
        if estimate is None:
            adjusted.append(player)
            continue
        values = []
        for value in player.gameweek_values:
            scale = (
                estimate.appearance_probability / value.appearance_probability
                if value.appearance_probability > 0
                else 1.0
            )
            values.append(
                replace(
                    value,
                    expected_points=value.expected_points * scale,
                    appearance_probability=estimate.appearance_probability,
                    sixty_probability=estimate.sixty_probability,
                )
            )
        scale = (
            estimate.appearance_probability / player.appearance_probability
            if player.appearance_probability > 0
            else 1.0
        )
        adjusted.append(
            replace(
                player,
                appearance_probability=estimate.appearance_probability,
                expected_points=player.expected_points * scale,
                gameweek_expected_points=(
                    None
                    if player.gameweek_expected_points is None
                    else player.gameweek_expected_points * scale
                ),
                gameweek_values=tuple(values),
            )
        )
    return tuple(adjusted)


# --------------------------------------------------------------------------
# Coverage and the adoption gate
# --------------------------------------------------------------------------


def role_coverage(
    *,
    eligible_promoted_players: int,
    matched_players: int,
    unresolved: list[dict[str, Any]],
    minimum_coverage: float = MINIMUM_ROLE_COVERAGE,
) -> dict[str, Any]:
    """Whether the evidence covers enough of the cohort to be usable at all."""

    share = (
        matched_players / eligible_promoted_players
        if eligible_promoted_players
        else 0.0
    )
    sufficient = (
        eligible_promoted_players > 0 and share >= minimum_coverage
    )
    return {
        "eligible_promoted_players": eligible_promoted_players,
        "matched_players": matched_players,
        "coverage": round(share, 4),
        "minimum_coverage": minimum_coverage,
        "sufficient": sufficient,
        "unresolved": unresolved,
        "verdict": (
            "Coverage is sufficient to evaluate the role treatment."
            if sufficient
            else (
                "Coverage is insufficient. The existing player model is kept "
                "and Championship role evidence is reported as an audit field "
                "only, because applying it to some players and not their "
                "direct competitors would distort a squad decision more than "
                "leaving the whole cohort on the positional prior."
            )
        ),
    }
