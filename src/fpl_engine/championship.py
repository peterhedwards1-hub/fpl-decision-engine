"""Previous-division goal evidence for newly promoted clubs.

The preseason carry-forward model gives a promoted club the same declared
prior as every other promoted club: 0.85 of league-average attack and 1.20 of
league-average goals conceded. That is a stated assumption, and it says a club
that walked the Championship scoring ninety goals and a club that scraped
through the play-offs are the same team.

This module supplies the only evidence available before a ball is kicked that
could tell them apart: what they actually did in the division they came from.
For each promoted club,

    attack_relative  = club Championship goals for per match
                       / Championship average goals for per team per match
    defence_relative = club Championship goals against per match
                       / Championship average goals against per team per match

and the declared prior is varied around its average by a declared weight:

    promoted_attack  = 0.85 x [1 + w x (attack_relative  - 1)]
    promoted_defence = 1.20 x [1 + w x (defence_relative - 1)]

At ``w = 0`` this is exactly the incumbent fixed prior, which is what makes the
comparison a single-field change.

One correction is applied to that formula, and it matters. The mean of
``attack_relative`` is 1 across the *division*, not across the three clubs that
came up: promoted clubs are by definition among the division's best, and their
relatives average well above 1. Applied literally, the formula would lift the
promoted cohort's mean attack multiplier from the declared 0.85 to about 0.97 —
that is not varying around the baseline, it is quietly replacing it with a
claim that promoted clubs are nearly league average, which no evidence here
supports. So the adjustment factors are normalised across the promoted cohort:

    factor_i     = 1 + w x (relative_i - 1)
    multiplier_i = base x factor_i / mean_j(factor_j)

The cohort mean is then exactly the declared baseline at every weight, and the
only thing the weight changes is the spread inside the cohort. Declared bounds
are applied after normalisation and can move the realised mean slightly; the
realised mean is reported rather than assumed.

Two bounds are declared, and they are asymmetric on purpose. A promoted club's
attack multiplier is capped at the league average and its defensive
vulnerability floored at the league average, so Championship evidence can say
which promoted club is likelier to cope — it can never say a promoted club is
better than an average established one. Championship goals are not Premier
League goals, and nothing here is entitled to claim they are.

The data are a small documented file, ``data/reference/championship-seasons.json``,
carrying per-club regular-season goals with source, revision and a SHA-256 of
the retrieved bytes. Play-off matches are excluded: a play-off run is three to
five knockout matches and would distort a rate computed over a 46-match season.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .history.database import HistoricalDatabase

DEFAULT_CHAMPIONSHIP_DATA_PATH = Path("data/reference/championship-seasons.json")
SUPPORTED_FORMAT = "fpl-decision-engine/championship-season-goals/v1"

#: The declared average promoted prior. Unchanged from the incumbent, and the
#: value the differentiated prior varies around.
BASE_PROMOTED_ATTACK = 0.85
BASE_PROMOTED_DEFENCE = 1.20

#: Declared bounds. Championship evidence may separate promoted clubs from one
#: another; it may not rate one above an average established club.
MINIMUM_PROMOTED_ATTACK = 0.60
MAXIMUM_PROMOTED_ATTACK = 1.00
MINIMUM_PROMOTED_DEFENCE = 1.00
MAXIMUM_PROMOTED_DEFENCE = 1.50

#: The only weights tested. Not a search space.
TESTED_PROMOTED_WEIGHTS: tuple[float, ...] = (0.25, 0.50, 0.75)

PROMOTED_PRIOR_MODES = ("fixed", "championship_relative")


class ChampionshipDataError(ValueError):
    """Raised when the Championship reference file violates its contract."""


@dataclass(frozen=True)
class ChampionshipTeamSeason:
    name: str
    matches: int
    goals_for: int
    goals_against: int

    @property
    def goals_for_per_match(self) -> float:
        return self.goals_for / self.matches

    @property
    def goals_against_per_match(self) -> float:
        return self.goals_against / self.matches


@dataclass(frozen=True)
class ChampionshipSeason:
    season_code: str
    competition: str
    stage: str
    matches: int
    teams: tuple[ChampionshipTeamSeason, ...]
    source_url: str
    source_sha256: str

    @property
    def team_matches(self) -> int:
        return sum(team.matches for team in self.teams)

    @property
    def average_goals_for_per_team_match(self) -> float:
        return sum(team.goals_for for team in self.teams) / self.team_matches

    @property
    def average_goals_against_per_team_match(self) -> float:
        return sum(team.goals_against for team in self.teams) / self.team_matches


@dataclass(frozen=True)
class ChampionshipDocument:
    format: str
    source: dict[str, Any]
    aliases: dict[str, tuple[str, ...]]
    seasons: tuple[ChampionshipSeason, ...]

    def season(self, season_code: str) -> ChampionshipSeason | None:
        for entry in self.seasons:
            if entry.season_code == season_code:
                return entry
        return None


def load_championship_document(
    path: str | Path = DEFAULT_CHAMPIONSHIP_DATA_PATH,
) -> ChampionshipDocument:
    """Read and validate the reference file."""

    location = Path(path)
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ChampionshipDataError(
            f"Championship reference file not found: {location}"
        ) from error
    except json.JSONDecodeError as error:
        raise ChampionshipDataError(f"{location} is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ChampionshipDataError(f"{location} must contain a JSON object")
    if raw.get("format") != SUPPORTED_FORMAT:
        raise ChampionshipDataError(
            f"{location} declares format {raw.get('format')!r}; "
            f"{SUPPORTED_FORMAT!r} is required"
        )
    source = raw.get("source")
    if not isinstance(source, dict) or not source.get("name") or not source.get("url"):
        raise ChampionshipDataError(
            f"{location} must record a source name and url for provenance"
        )
    aliases: dict[str, tuple[str, ...]] = {}
    for championship_name, fpl_names in (raw.get("team_name_aliases") or {}).items():
        values = (fpl_names,) if isinstance(fpl_names, str) else tuple(fpl_names)
        aliases[str(championship_name)] = tuple(str(value) for value in values)
    seasons: list[ChampionshipSeason] = []
    for entry in raw.get("seasons") or ():
        teams = tuple(
            ChampionshipTeamSeason(
                name=str(team["name"]),
                matches=int(team["matches"]),
                goals_for=int(team["goals_for"]),
                goals_against=int(team["goals_against"]),
            )
            for team in entry["teams"]
        )
        if not teams:
            raise ChampionshipDataError(
                f"{location}: season {entry.get('season_code')} has no clubs"
            )
        if any(team.matches <= 0 for team in teams):
            raise ChampionshipDataError(
                f"{location}: season {entry.get('season_code')} has a club with "
                "no matches"
            )
        season = ChampionshipSeason(
            season_code=str(entry["season_code"]),
            competition=str(entry.get("competition", "english-championship")),
            stage=str(entry.get("stage", "regular")),
            matches=int(entry["matches"]),
            teams=teams,
            source_url=str(entry["source_url"]),
            source_sha256=str(entry["source_sha256"]),
        )
        scored = sum(team.goals_for for team in season.teams)
        conceded = sum(team.goals_against for team in season.teams)
        if scored != conceded:
            raise ChampionshipDataError(
                f"{location}: season {season.season_code} records {scored} goals "
                f"scored and {conceded} conceded; every goal must appear on both "
                "sides of the ledger"
            )
        seasons.append(season)
    if not seasons:
        raise ChampionshipDataError(f"{location} contains no seasons")
    return ChampionshipDocument(
        format=str(raw["format"]),
        source=source,
        aliases=aliases,
        seasons=tuple(seasons),
    )


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def import_championship_document(
    database: HistoricalDatabase,
    document: ChampionshipDocument,
    *,
    imported_at: datetime | None = None,
) -> dict[str, Any]:
    """Load the reference file into the database, idempotently.

    Re-importing the same file replaces each season's rows rather than
    duplicating them, so a corrected file can be applied without rebuilding
    the database.
    """

    moment = (imported_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    connection = database.connection
    imported: list[str] = []
    with connection:
        for season in document.seasons:
            connection.execute(
                "DELETE FROM championship_seasons WHERE season_code = ?",
                (season.season_code,),
            )
            cursor = connection.execute(
                """
                INSERT INTO championship_seasons (
                    season_code, competition, stage, matches, source_name,
                    source_url, source_revision, source_sha256, retrieved_at,
                    imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    season.season_code,
                    season.competition,
                    season.stage,
                    season.matches,
                    str(document.source.get("name")),
                    season.source_url,
                    document.source.get("revision"),
                    season.source_sha256,
                    str(document.source.get("retrieved_at") or moment),
                    moment,
                ),
            )
            season_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO championship_team_seasons (
                    championship_season_id, name, matches, goals_for, goals_against
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        season_id,
                        team.name,
                        team.matches,
                        team.goals_for,
                        team.goals_against,
                    )
                    for team in season.teams
                ],
            )
            imported.append(season.season_code)
        connection.execute("DELETE FROM championship_team_aliases")
        connection.executemany(
            "INSERT INTO championship_team_aliases (championship_name, fpl_name) "
            "VALUES (?, ?)",
            [
                (championship_name, fpl_name)
                for championship_name, fpl_names in document.aliases.items()
                for fpl_name in fpl_names
            ],
        )
    return {
        "seasons": imported,
        "teams": sum(len(season.teams) for season in document.seasons),
        "aliases": sum(len(values) for values in document.aliases.values()),
        "source": document.source,
    }


def championship_coverage(database: HistoricalDatabase) -> dict[str, Any]:
    """What the database actually holds, for the report's coverage section."""

    rows = database.connection.execute(
        """
        SELECT s.season_code, s.matches, s.source_name, s.source_url,
               s.source_revision, s.source_sha256, s.retrieved_at,
               COUNT(t.name) AS clubs,
               SUM(t.matches) AS team_matches,
               SUM(t.goals_for) AS goals
        FROM championship_seasons s
        LEFT JOIN championship_team_seasons t
               ON t.championship_season_id = s.id
        GROUP BY s.id
        ORDER BY s.season_code
        """
    ).fetchall()
    return {
        "seasons": [
            {
                "season_code": str(row["season_code"]),
                "matches": int(row["matches"]),
                "clubs": int(row["clubs"] or 0),
                "team_matches": int(row["team_matches"] or 0),
                "average_goals_per_team_match": (
                    round(float(row["goals"]) / float(row["team_matches"]), 4)
                    if row["team_matches"]
                    else None
                ),
                "source_name": str(row["source_name"]),
                "source_url": str(row["source_url"]),
                "source_revision": row["source_revision"],
                "source_sha256": str(row["source_sha256"]),
                "retrieved_at": str(row["retrieved_at"]),
            }
            for row in rows
        ],
    }


# --------------------------------------------------------------------------
# Priors
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotedClubPrior:
    """One promoted club's differentiated prior, with everything behind it."""

    fpl_name: str
    championship_name: str | None
    matched: bool
    attack_relative: float | None
    defence_relative: float | None
    attack_multiplier: float
    defence_multiplier: float
    attack_bound_applied: bool = False
    defence_bound_applied: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fpl_name": self.fpl_name,
            "championship_name": self.championship_name,
            "matched": self.matched,
            "attack_relative": (
                None if self.attack_relative is None else round(self.attack_relative, 4)
            ),
            "defence_relative": (
                None
                if self.defence_relative is None
                else round(self.defence_relative, 4)
            ),
            "attack_multiplier": round(self.attack_multiplier, 4),
            "defence_multiplier": round(self.defence_multiplier, 4),
            "attack_bound_applied": self.attack_bound_applied,
            "defence_bound_applied": self.defence_bound_applied,
            "reason": self.reason,
        }


def _normalise(name: str) -> str:
    cleaned = "".join(
        character
        for character in name.lower().replace("'", "").replace(".", "")
        if character.isalnum() or character.isspace()
    )
    tokens = [token for token in cleaned.split() if token not in {"fc", "afc"}]
    return " ".join(tokens)


def promoted_club_priors(
    database: HistoricalDatabase,
    *,
    championship_season_code: str,
    promoted_fpl_names: tuple[str, ...],
    weight: float,
    base_attack: float = BASE_PROMOTED_ATTACK,
    base_defence: float = BASE_PROMOTED_DEFENCE,
    minimum_attack: float = MINIMUM_PROMOTED_ATTACK,
    maximum_attack: float = MAXIMUM_PROMOTED_ATTACK,
    minimum_defence: float = MINIMUM_PROMOTED_DEFENCE,
    maximum_defence: float = MAXIMUM_PROMOTED_DEFENCE,
) -> dict[str, PromotedClubPrior]:
    """One prior per promoted club, keyed by the club's Premier League name.

    A club that cannot be matched to a Championship record keeps the declared
    fixed prior and says so, rather than silently receiving a neutral
    adjustment that would be indistinguishable from a measurement.
    """

    if weight < 0:
        raise ValueError("Promoted prior weight cannot be negative")
    rows = database.connection.execute(
        """
        SELECT t.name, t.matches, t.goals_for, t.goals_against
        FROM championship_team_seasons t
        JOIN championship_seasons s ON s.id = t.championship_season_id
        WHERE s.season_code = ?
        """,
        (championship_season_code,),
    ).fetchall()
    aliases: dict[str, str] = {}
    for row in database.connection.execute(
        "SELECT championship_name, fpl_name FROM championship_team_aliases"
    ):
        aliases[_normalise(str(row["fpl_name"]))] = _normalise(
            str(row["championship_name"])
        )

    priors: dict[str, PromotedClubPrior] = {}
    if not rows:
        for name in promoted_fpl_names:
            priors[name] = PromotedClubPrior(
                fpl_name=name,
                championship_name=None,
                matched=False,
                attack_relative=None,
                defence_relative=None,
                attack_multiplier=base_attack,
                defence_multiplier=base_defence,
                reason=(
                    f"No Championship record for {championship_season_code}; "
                    "the declared fixed promoted prior is used."
                ),
            )
        return priors

    total_matches = sum(int(row["matches"]) for row in rows)
    average_for = sum(float(row["goals_for"]) for row in rows) / total_matches
    average_against = sum(float(row["goals_against"]) for row in rows) / total_matches
    by_key = {_normalise(str(row["name"])): row for row in rows}

    matched_rows: dict[str, Any] = {}
    for name in promoted_fpl_names:
        key = _normalise(name)
        row = by_key.get(key) or by_key.get(aliases.get(key, ""))
        if row is not None:
            matched_rows[name] = row

    # Normalise the adjustment factors across the promoted cohort so the
    # cohort mean stays on the declared baseline. Without this the formula
    # would raise every promoted club, because promoted clubs are the top of
    # the division they left and their relatives average well above one.
    attack_factors = {
        name: 1.0
        + weight
        * (
            (float(row["goals_for"]) / int(row["matches"])) / average_for - 1.0
        )
        for name, row in matched_rows.items()
    }
    defence_factors = {
        name: 1.0
        + weight
        * (
            (float(row["goals_against"]) / int(row["matches"])) / average_against
            - 1.0
        )
        for name, row in matched_rows.items()
    }
    attack_normaliser = (
        sum(attack_factors.values()) / len(attack_factors) if attack_factors else 1.0
    )
    defence_normaliser = (
        sum(defence_factors.values()) / len(defence_factors)
        if defence_factors
        else 1.0
    )
    if attack_normaliser <= 0 or defence_normaliser <= 0:
        raise ChampionshipDataError(
            "Promoted-cohort normalisation collapsed; the weight is too large "
            "for this cohort's spread"
        )

    for name in promoted_fpl_names:
        row = matched_rows.get(name)
        if row is None:
            priors[name] = PromotedClubPrior(
                fpl_name=name,
                championship_name=None,
                matched=False,
                attack_relative=None,
                defence_relative=None,
                attack_multiplier=base_attack,
                defence_multiplier=base_defence,
                reason=(
                    f"{name} could not be matched to a "
                    f"{championship_season_code} Championship club; the "
                    "declared fixed promoted prior is used."
                ),
            )
            continue
        matches = int(row["matches"])
        attack_relative = (float(row["goals_for"]) / matches) / average_for
        defence_relative = (float(row["goals_against"]) / matches) / average_against
        raw_attack = base_attack * attack_factors[name] / attack_normaliser
        raw_defence = base_defence * defence_factors[name] / defence_normaliser
        attack = min(max(raw_attack, minimum_attack), maximum_attack)
        defence = min(max(raw_defence, minimum_defence), maximum_defence)
        priors[name] = PromotedClubPrior(
            fpl_name=name,
            championship_name=str(row["name"]),
            matched=True,
            attack_relative=attack_relative,
            defence_relative=defence_relative,
            attack_multiplier=attack,
            defence_multiplier=defence,
            attack_bound_applied=abs(attack - raw_attack) > 1e-12,
            defence_bound_applied=abs(defence - raw_defence) > 1e-12,
            reason=(
                f"{row['name']} scored {row['goals_for']} and conceded "
                f"{row['goals_against']} in {matches} Championship matches "
                f"({championship_season_code})."
            ),
        )
    return priors


def cohort_mean_multipliers(
    priors: dict[str, PromotedClubPrior],
) -> tuple[float | None, float | None]:
    """The promoted cohort's mean attack and defence multipliers.

    Used by the normalisation test: at any weight the cohort mean must stay
    close to the declared average prior, because differentiation is meant to
    redistribute belief inside the cohort rather than move the cohort.
    """

    if not priors:
        return None, None
    attacks = [prior.attack_multiplier for prior in priors.values()]
    defences = [prior.defence_multiplier for prior in priors.values()]
    return sum(attacks) / len(attacks), sum(defences) / len(defences)
