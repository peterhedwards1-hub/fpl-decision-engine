"""One coherent, auditable team-strength estimator.

The production path rates a club by its raw goals for and against, shrunk
toward the league average. That has three structural problems. Before a ball is
kicked every club is identical unless carry-forward is switched on. Nothing
adjusts for who the goals were scored against, so a team that has played three
promoted sides looks elite. And the separate expected-goal path inside
`team_share_expected` re-implements a decayed rating with neither opponent
adjustment nor a preseason prior, so the two disagree.

This module replaces all of that with a single estimator producing, for every
club at a forecast origin:

* attack and defence multipliers relative to the league average;
* expected goals for and against, split home and away;
* the preseason prior and the current-season estimate that produced them;
* the weight given to each;
* uncertainty;
* squad continuity;
* every contextual adjustment applied, with its rationale.

The rating itself is a multiplicative Poisson model in the Dixon-Coles family,
solved by a few fixed-point sweeps rather than a fitted optimiser. Expected
goals in a fixture are

    lambda_home = league_average * attack_home * defence_away * home_factor
    lambda_away = league_average * attack_away * defence_home * away_factor

so a club's attack is identified only relative to the defences it actually
faced. Scoring twice against a strong defence therefore moves the rating more
than scoring twice against a weak one, which is the whole point.

Every constant is declared and interpretable. There is no fitted parameter
search here: the project's own evidence budget cannot identify one, and a clear
approximate model is worth more than an opaque unidentifiable one.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .history.database import HistoricalDatabase

#: Fixed-point sweeps used to solve the attack/defence ratings. The system is
#: small and well behaved; five sweeps move the ratings by less than a
#: thousandth of a goal on a full season.
RATING_SWEEPS = 5

#: Bounds on any single contextual adjustment. A reviewed note may move a club
#: meaningfully but must never be able to invent a different team.
MINIMUM_ADJUSTMENT = 0.70
MAXIMUM_ADJUSTMENT = 1.40


@dataclass(frozen=True)
class ContextualAdjustment:
    """A reviewed, effective-dated change match data cannot know about.

    Deliberately inert until someone enters it: there is no scraper and no
    automatic news interpreter behind this. It exists so that reviewed
    information has a bounded, auditable path into the model instead of being
    applied silently or not at all.
    """

    source_team_id: str
    category: str
    attack_multiplier: float = 1.0
    defence_multiplier: float = 1.0
    effective_from_gameweek: int = 1
    effective_to_gameweek: int | None = None
    rationale: str = ""
    source: str = ""
    confidence: str = "medium"

    def __post_init__(self) -> None:
        if not self.category.strip():
            raise ValueError("A contextual adjustment needs a category")
        if not self.rationale.strip():
            raise ValueError(
                f"Adjustment {self.category!r} needs a rationale; an "
                "unexplained adjustment cannot be reviewed"
            )
        for name, value in (
            ("attack", self.attack_multiplier),
            ("defence", self.defence_multiplier),
        ):
            if not MINIMUM_ADJUSTMENT <= value <= MAXIMUM_ADJUSTMENT:
                raise ValueError(
                    f"{name} adjustment {value} is outside the permitted "
                    f"range [{MINIMUM_ADJUSTMENT}, {MAXIMUM_ADJUSTMENT}]"
                )
        if self.confidence not in {"low", "medium", "high"}:
            raise ValueError("Confidence must be 'low', 'medium' or 'high'")
        if self.effective_to_gameweek is not None and (
            self.effective_to_gameweek < self.effective_from_gameweek
        ):
            raise ValueError("An adjustment cannot expire before it begins")

    def applies_at(self, gameweek_number: int) -> bool:
        if gameweek_number < self.effective_from_gameweek:
            return False
        return (
            self.effective_to_gameweek is None
            or gameweek_number <= self.effective_to_gameweek
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SquadContinuity:
    """How much of last season's team is still here.

    Used only to decide how much to trust the previous-season prior. It is
    explicitly *not* used to infer that a club got better or worse: transfer
    fees and reputations are not evidence of quality, and nothing here reads
    them. A heavily rebuilt squad simply gets a prior pulled harder toward the
    league mean and a wider uncertainty band.
    """

    source_team_id: str
    retained_minutes_share: float
    retained_expected_goals_share: float
    retained_expected_assists_share: float
    retained_defensive_minutes_share: float
    unmatched_share: float
    retained_players: int
    departed_players: int

    @property
    def continuity_score(self) -> float:
        """One number in [0, 1] summarising how intact the squad is.

        Minutes carry the most weight because they are the most reliably
        recorded; attacking output matters for the attack prior; defensive
        minutes matter for the defence prior.
        """

        return _clamp(
            0.40 * self.retained_minutes_share
            + 0.25 * self.retained_expected_goals_share
            + 0.15 * self.retained_expected_assists_share
            + 0.20 * self.retained_defensive_minutes_share,
            0.0,
            1.0,
        )

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "continuity_score": round(self.continuity_score, 4)}


@dataclass(frozen=True)
class TeamStrength:
    """A club's rating at one forecast origin, with its whole derivation."""

    team_id: str
    source_team_id: str
    name: str
    attack: float
    defence: float
    expected_goals_for_home: float
    expected_goals_for_away: float
    expected_goals_against_home: float
    expected_goals_against_away: float
    league_average_goals: float
    prior_attack: float
    prior_defence: float
    current_attack: float | None
    current_defence: float | None
    prior_weight: float
    current_weight: float
    uncertainty: float
    matches_observed: float
    schedule_strength: float
    is_promoted: bool
    evidence_source: str
    squad_continuity: SquadContinuity | None = None
    adjustments: tuple[ContextualAdjustment, ...] = ()
    rationale: tuple[str, ...] = ()

    @property
    def prior_share(self) -> float:
        total = self.prior_weight + self.current_weight
        return 0.0 if total <= 0 else self.prior_weight / total

    def as_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "source_team_id": self.source_team_id,
            "name": self.name,
            "attack": round(self.attack, 6),
            "defence": round(self.defence, 6),
            "expected_goals_for_home": round(self.expected_goals_for_home, 4),
            "expected_goals_for_away": round(self.expected_goals_for_away, 4),
            "expected_goals_against_home": round(
                self.expected_goals_against_home, 4
            ),
            "expected_goals_against_away": round(
                self.expected_goals_against_away, 4
            ),
            "league_average_goals": round(self.league_average_goals, 4),
            "prior_attack": round(self.prior_attack, 6),
            "prior_defence": round(self.prior_defence, 6),
            "current_attack": (
                None if self.current_attack is None else round(self.current_attack, 6)
            ),
            "current_defence": (
                None
                if self.current_defence is None
                else round(self.current_defence, 6)
            ),
            "prior_weight": round(self.prior_weight, 4),
            "current_weight": round(self.current_weight, 4),
            "prior_share": round(self.prior_share, 4),
            "uncertainty": round(self.uncertainty, 4),
            "matches_observed": round(self.matches_observed, 3),
            "schedule_strength": round(self.schedule_strength, 4),
            "is_promoted": self.is_promoted,
            "evidence_source": self.evidence_source,
            "squad_continuity": (
                None
                if self.squad_continuity is None
                else self.squad_continuity.as_dict()
            ),
            "adjustments": [entry.as_dict() for entry in self.adjustments],
            "rationale": list(self.rationale),
        }


@dataclass(frozen=True)
class TeamStrengthSettings:
    """Declared, interpretable constants. None of these are fitted."""

    #: Pseudo-matches of previous-season evidence a fully intact squad carries.
    #: Roughly a third of a season: enough to dominate the opening Gameweeks,
    #: gone by the midpoint.
    prior_matches_intact_squad: float = 12.0
    #: Pseudo-matches a completely rebuilt squad carries. Not zero, because
    #: even a rebuilt club keeps its stadium, coaching and division.
    prior_matches_rebuilt_squad: float = 4.0
    #: Previous-season estimates are regressed this far toward the league mean
    #: before being used, in pseudo-matches.
    prior_regression_matches: float = 8.0
    #: Half-life, in Gameweeks, of current-season evidence.
    current_half_life_gameweeks: float = 8.0
    #: Half-life, in Gameweeks, of the prior's own weight. This is not
    #: optional bookkeeping. Current-season weight is decayed, so it saturates
    #: at roughly `current_half_life / ln 2` matches however long the season
    #: runs; against a prior of fixed weight it would never win, and last
    #: season would still be shaping the rating in May. Decaying the prior on
    #: the same clock makes the constant mean what it says: the prior is worth
    #: this many matches at the opener and halves every half-life thereafter.
    prior_half_life_gameweeks: float = 8.0
    #: How far a completely rebuilt squad's prior is dragged toward the league
    #: mean, as a share. Continuity changes confidence in the prior; this is
    #: the part that changes the prior itself. A rebuilt club is not assumed
    #: better or worse, only less like last season's club.
    rebuild_prior_regression: float = 0.50
    #: Promoted clubs have no top-flight evidence at all. These are declared
    #: priors relative to the league average, not measurements.
    promoted_attack: float = 0.85
    promoted_defence: float = 1.18
    #: Promoted clubs also carry far less prior weight, because the declared
    #: values above are guesses rather than observations.
    promoted_prior_matches: float = 5.0
    #: Venue split. Applied to the league average, not to a club's rating, so
    #: it cannot be confused with team quality.
    home_factor: float = 1.08
    away_factor: float = 0.92
    #: Ratings are clamped to keep a small sample from producing absurdities.
    minimum_multiplier: float = 0.60
    maximum_multiplier: float = 1.55
    #: Uncertainty floor and the sample size at which it halves.
    base_uncertainty: float = 0.30
    uncertainty_half_life_matches: float = 10.0
    #: Extra uncertainty a fully rebuilt squad carries.
    rebuild_uncertainty: float = 0.15
    #: League-wide share of goals that carry an assist. Used only to split a
    #: team's goal expectation into goal and assist events; it is a league
    #: constant shrunk toward this prior, never a per-club rating.
    assist_per_goal_prior: float = 0.72
    assist_prior_matches: float = 20.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeamStrengthState:
    """Every club's rating at one origin, plus what produced it."""

    season_code: str
    gameweek_number: int
    league_average_goals: float
    teams: dict[str, TeamStrength]
    settings: TeamStrengthSettings
    assist_per_goal: float = 0.72
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def by_source_id(self) -> dict[str, TeamStrength]:
        return {team.source_team_id: team for team in self.teams.values()}

    def as_dict(self) -> dict[str, Any]:
        ranked_attack = sorted(
            self.teams.values(), key=lambda team: (-team.attack, team.name)
        )
        ranked_defence = sorted(
            self.teams.values(), key=lambda team: (team.defence, team.name)
        )
        attack_rank = {
            team.team_id: index for index, team in enumerate(ranked_attack, start=1)
        }
        defence_rank = {
            team.team_id: index for index, team in enumerate(ranked_defence, start=1)
        }
        return {
            "season_code": self.season_code,
            "gameweek_number": self.gameweek_number,
            "league_average_goals": round(self.league_average_goals, 4),
            "assist_per_goal": round(self.assist_per_goal, 4),
            "settings": self.settings.as_dict(),
            "teams": [
                {
                    **team.as_dict(),
                    "attack_rank": attack_rank[team.team_id],
                    "defence_rank": defence_rank[team.team_id],
                }
                for team in ranked_attack
            ],
            "limitations": list(self.limitations),
        }


def estimate_team_strength(
    database: HistoricalDatabase,
    *,
    season_code: str,
    gameweek_number: int,
    settings: TeamStrengthSettings | None = None,
    adjustments: tuple[ContextualAdjustment, ...] = (),
    as_of: datetime | None = None,
    maximum_ingestion_run_id: int | None = None,
) -> TeamStrengthState:
    """Rate every club using only evidence available before this Gameweek.

    Reads fixtures with `gameweeks.number < gameweek_number` and, for the
    preseason prior, only a season that finished before this one began. No
    later evidence is consulted, so the same origin always produces the same
    ratings regardless of what happened afterwards.
    """

    if gameweek_number < 1:
        raise ValueError("Gameweek numbers start at 1")
    options = settings or TeamStrengthSettings()
    teams = _load_teams(database, season_code)
    if not teams:
        raise ValueError(f"Season {season_code!r} has no registered teams")

    previous_season = _previous_season(database, season_code)
    continuity = (
        _squad_continuity(database, season_code, previous_season, teams)
        if previous_season
        else {}
    )
    (
        prior_ratings,
        prior_league_average,
        prior_source,
        previous_results,
    ) = _preseason_prior(
        database,
        season_code=season_code,
        previous_season=previous_season,
        teams=teams,
        options=options,
    )
    (
        observed,
        current_league_average,
        current_source,
        observed_results,
    ) = _current_season_ratings(
        database,
        season_code=season_code,
        gameweek_number=gameweek_number,
        teams=teams,
        options=options,
        as_of=as_of,
        maximum_ingestion_run_id=maximum_ingestion_run_id,
    )
    league_average = (
        current_league_average
        if current_league_average is not None
        else prior_league_average
        if prior_league_average is not None
        else 1.40
    )
    schedule = _schedule_strength(observed_results, prior_ratings)
    assist_per_goal = _assist_per_goal(
        observed_results or previous_results, options
    )

    result: dict[str, TeamStrength] = {}
    for team_id, team in teams.items():
        squad = continuity.get(team_id)
        prior = prior_ratings.get(
            team_id,
            {"attack": 1.0, "defence": 1.0, "promoted": True},
        )
        is_promoted = bool(prior["promoted"])
        prior_weight = _prior_weight(prior, squad, options, gameweek_number)
        # A promoted club's prior is already a declared guess about a squad
        # nobody has seen in this division, so continuity has nothing to add.
        prior_attack = (
            float(prior["attack"])
            if is_promoted
            else _continuity_adjusted_prior(
                float(prior["attack"]), squad, options
            )
        )
        prior_defence = (
            float(prior["defence"])
            if is_promoted
            else _continuity_adjusted_prior(
                float(prior["defence"]), squad, options
            )
        )
        sample = observed.get(team_id)
        current_weight = 0.0 if sample is None else float(sample["weight"])
        current_attack = None if sample is None else float(sample["attack"])
        current_defence = None if sample is None else float(sample["defence"])
        rationale: list[str] = []

        attack = _blend(prior_attack, current_attack, prior_weight, current_weight)
        defence = _blend(
            prior_defence, current_defence, prior_weight, current_weight
        )
        if squad is not None and squad.continuity_score < 0.5:
            rationale.append(
                f"Squad continuity {squad.continuity_score:.2f}: the "
                "previous-season prior is pulled harder toward the league mean "
                "and uncertainty is widened."
            )
        if is_promoted:
            rationale.append(
                "Promoted club: attack and defence use declared priors, not "
                "measurements, and carry reduced weight and wider uncertainty."
            )

        applied = tuple(
            entry
            for entry in adjustments
            if entry.source_team_id == team.source_team_id
            and entry.applies_at(gameweek_number)
        )
        for entry in applied:
            attack *= entry.attack_multiplier
            defence *= entry.defence_multiplier
            rationale.append(
                f"Contextual adjustment ({entry.category}, "
                f"confidence {entry.confidence}): attack "
                f"x{entry.attack_multiplier:.3f}, defence "
                f"x{entry.defence_multiplier:.3f}. {entry.rationale}"
            )
        attack = _clamp(attack, options.minimum_multiplier, options.maximum_multiplier)
        defence = _clamp(
            defence, options.minimum_multiplier, options.maximum_multiplier
        )
        matches = 0.0 if sample is None else float(sample["matches"])
        result[team_id] = TeamStrength(
            team_id=team_id,
            source_team_id=team.source_team_id,
            name=team.name,
            attack=attack,
            defence=defence,
            expected_goals_for_home=league_average * attack * options.home_factor,
            expected_goals_for_away=league_average * attack * options.away_factor,
            expected_goals_against_home=(
                league_average * defence * options.away_factor
            ),
            expected_goals_against_away=(
                league_average * defence * options.home_factor
            ),
            league_average_goals=league_average,
            prior_attack=prior_attack,
            prior_defence=prior_defence,
            current_attack=current_attack,
            current_defence=current_defence,
            prior_weight=prior_weight,
            current_weight=current_weight,
            uncertainty=_uncertainty(matches, squad, is_promoted, options),
            matches_observed=matches,
            schedule_strength=schedule.get(team_id, 1.0),
            is_promoted=is_promoted,
            evidence_source=(
                current_source if current_weight > 0 else prior_source
            ),
            squad_continuity=squad,
            adjustments=applied,
            rationale=tuple(rationale),
        )
    return TeamStrengthState(
        season_code=season_code,
        gameweek_number=gameweek_number,
        league_average_goals=league_average,
        teams=result,
        settings=options,
        assist_per_goal=assist_per_goal,
        limitations=_limitations(prior_source, current_source, bool(continuity)),
    )


@dataclass(frozen=True)
class _Team:
    team_id: str
    source_team_id: str
    name: str


def _load_teams(
    database: HistoricalDatabase, season_code: str
) -> dict[str, _Team]:
    return {
        str(row["id"]): _Team(
            team_id=str(row["id"]),
            source_team_id=str(row["source_team_id"]),
            name=str(row["name"]),
        )
        for row in database.connection.execute(
            """
            SELECT teams.id, teams.source_team_id, teams.name
            FROM teams
            JOIN seasons ON seasons.id = teams.season_id
            WHERE seasons.code = ?
            ORDER BY teams.id
            """,
            (season_code,),
        )
    }


def _previous_season(
    database: HistoricalDatabase, season_code: str
) -> str | None:
    row = database.connection.execute(
        "SELECT code FROM seasons WHERE code < ? ORDER BY code DESC LIMIT 1",
        (season_code,),
    ).fetchone()
    return None if row is None else str(row["code"])


def _fixture_results(
    database: HistoricalDatabase,
    *,
    season_code: str,
    maximum_gameweek: int | None,
    as_of: datetime | None,
    maximum_ingestion_run_id: int | None,
) -> list[dict[str, Any]]:
    """Completed fixtures with goals and expected goals for both sides.

    Expected goals are summed from player fixture stats, falling back to actual
    goals per player where the expected field is missing. When `as_of` is
    supplied, only fixture observations recorded by then are read, which is
    what keeps a backtest honest.
    """

    clause = "gameweeks.number < ?" if maximum_gameweek is not None else "1 = 1"
    parameters: list[Any] = [season_code]
    if maximum_gameweek is not None:
        parameters.append(maximum_gameweek)
    rows = database.connection.execute(
        f"""
        SELECT fixtures.id AS fixture_id,
               gameweeks.number AS gameweek_number,
               fixtures.home_team_id, fixtures.away_team_id,
               fixtures.home_score, fixtures.away_score,
               SUM(
                   CASE WHEN player_seasons.team_id = fixtures.home_team_id
                        THEN COALESCE(stats.expected_goals, stats.goals)
                        ELSE 0 END
               ) AS home_xg,
               SUM(
                   CASE WHEN player_seasons.team_id = fixtures.away_team_id
                        THEN COALESCE(stats.expected_goals, stats.goals)
                        ELSE 0 END
               ) AS away_xg,
               SUM(
                   CASE WHEN player_seasons.team_id = fixtures.home_team_id
                        THEN COALESCE(stats.expected_assists, stats.assists)
                        ELSE 0 END
               ) AS home_xa,
               SUM(
                   CASE WHEN player_seasons.team_id = fixtures.away_team_id
                        THEN COALESCE(stats.expected_assists, stats.assists)
                        ELSE 0 END
               ) AS away_xa,
               SUM(
                   CASE WHEN stats.expected_goals IS NOT NULL THEN 1 ELSE 0 END
               ) AS expected_rows
        FROM fixtures
        JOIN seasons ON seasons.id = fixtures.season_id
        JOIN gameweeks ON gameweeks.id = fixtures.gameweek_id
        LEFT JOIN player_fixture_stats stats ON stats.fixture_id = fixtures.id
        LEFT JOIN player_seasons ON player_seasons.id = stats.player_season_id
        WHERE seasons.code = ?
          AND fixtures.finished = 1
          AND fixtures.home_score IS NOT NULL
          AND fixtures.away_score IS NOT NULL
          AND {clause}
        GROUP BY fixtures.id
        ORDER BY gameweeks.number, fixtures.id
        """,
        parameters,
    ).fetchall()
    if as_of is not None:
        allowed = _observed_fixture_ids(
            database,
            season_code=season_code,
            as_of=as_of,
            maximum_ingestion_run_id=maximum_ingestion_run_id,
        )
        rows = [row for row in rows if int(row["fixture_id"]) in allowed]
    return [dict(row) for row in rows]


def _observed_fixture_ids(
    database: HistoricalDatabase,
    *,
    season_code: str,
    as_of: datetime,
    maximum_ingestion_run_id: int | None,
) -> set[int]:
    cutoff = as_of.astimezone(UTC).isoformat()
    return {
        int(row["fixture_id"])
        for row in database.connection.execute(
            """
            SELECT DISTINCT observations.fixture_id
            FROM fixture_observations observations
            JOIN ingestion_runs
              ON ingestion_runs.id = observations.provenance_run_id
            JOIN fixtures ON fixtures.id = observations.fixture_id
            JOIN seasons ON seasons.id = fixtures.season_id
            WHERE seasons.code = ?
              AND ingestion_runs.status = 'completed'
              AND datetime(ingestion_runs.retrieved_at) <= datetime(?)
              AND (? IS NULL OR ingestion_runs.id <= ?)
              AND observations.finished = 1
            """,
            (
                season_code,
                cutoff,
                maximum_ingestion_run_id,
                maximum_ingestion_run_id,
            ),
        )
    }


def _solve_ratings(
    results: list[dict[str, Any]],
    team_ids: list[str],
    *,
    options: TeamStrengthSettings,
    use_expected_goals: bool,
    half_life: float | None,
    reference_gameweek: int | None,
) -> tuple[dict[str, dict[str, float]], float]:
    """Fixed-point solve of multiplicative attack and defence ratings.

    A club's attack is divided by the defences it actually faced, so identical
    raw output against stronger opponents produces a higher rating. Venue is
    handled by the league-average factor, never by the club rating.

    Returns the ratings and the league average *of actual goals*, not of the
    series the ratings were fitted to. The two come apart when expected-goal
    coverage is partial: a provider missing a third of its rows produces a
    perfectly usable ranking and a badly deflated level. Ratings are
    renormalised to mean 1.0, so only the level is at risk, and the level is
    something the scoreline can be trusted to give.
    """

    attack = dict.fromkeys(team_ids, 1.0)
    defence = dict.fromkeys(team_ids, 1.0)
    entries = []
    total_goals = 0.0
    total_weight = 0.0
    total_actual_goals = 0.0
    for result in results:
        weight = 1.0
        if half_life is not None and reference_gameweek is not None:
            weight = _decay(
                reference_gameweek - int(result["gameweek_number"]), half_life
            )
        if weight <= 0:
            continue
        home = str(result["home_team_id"])
        away = str(result["away_team_id"])
        if home not in attack or away not in attack:
            continue
        if use_expected_goals:
            home_goals = float(result["home_xg"] or 0.0)
            away_goals = float(result["away_xg"] or 0.0)
        else:
            home_goals = float(result["home_score"])
            away_goals = float(result["away_score"])
        entries.append((home, away, home_goals, away_goals, weight))
        total_goals += (home_goals + away_goals) * weight
        total_actual_goals += (
            float(result["home_score"]) + float(result["away_score"])
        ) * weight
        total_weight += 2.0 * weight
    if not entries or total_weight <= 0:
        return {}, 0.0
    # The units the fixed point is solved in: whatever series is being rated.
    league_average = total_goals / total_weight
    if league_average <= 0:
        return {}, 0.0
    # The units the projection consumes: goals actually scored.
    goal_league_average = total_actual_goals / total_weight

    for _ in range(RATING_SWEEPS):
        for_numerator: dict[str, float] = dict.fromkeys(team_ids, 0.0)
        for_denominator: dict[str, float] = dict.fromkeys(team_ids, 0.0)
        against_numerator: dict[str, float] = dict.fromkeys(team_ids, 0.0)
        against_denominator: dict[str, float] = dict.fromkeys(team_ids, 0.0)
        for home, away, home_goals, away_goals, weight in entries:
            for_numerator[home] += home_goals * weight
            for_denominator[home] += (
                league_average * defence[away] * options.home_factor * weight
            )
            for_numerator[away] += away_goals * weight
            for_denominator[away] += (
                league_average * defence[home] * options.away_factor * weight
            )
            against_numerator[home] += away_goals * weight
            against_denominator[home] += (
                league_average * attack[away] * options.away_factor * weight
            )
            against_numerator[away] += home_goals * weight
            against_denominator[away] += (
                league_average * attack[home] * options.home_factor * weight
            )
        for team_id in team_ids:
            if for_denominator[team_id] > 0:
                attack[team_id] = for_numerator[team_id] / for_denominator[team_id]
            if against_denominator[team_id] > 0:
                defence[team_id] = (
                    against_numerator[team_id] / against_denominator[team_id]
                )
        # Renormalise so the average club sits at 1.0 and the ratings stay
        # interpretable as multipliers of the league average.
        attack = _normalise(attack)
        defence = _normalise(defence)

    # `weight` is decayed evidence, used for blending against the prior.
    # `matches` is a plain count, used for uncertainty and for regression, so a
    # club that played ten matches long ago is not reported as having played
    # two and a half.
    weights: dict[str, float] = dict.fromkeys(team_ids, 0.0)
    matches: dict[str, float] = dict.fromkeys(team_ids, 0.0)
    for home, away, _, _, weight in entries:
        weights[home] += weight
        weights[away] += weight
        matches[home] += 1.0
        matches[away] += 1.0
    return (
        {
            team_id: {
                "attack": attack[team_id],
                "defence": defence[team_id],
                "weight": weights[team_id],
                "matches": matches[team_id],
            }
            for team_id in team_ids
            if weights[team_id] > 0
        },
        goal_league_average,
    )


def _preseason_prior(
    database: HistoricalDatabase,
    *,
    season_code: str,
    previous_season: str | None,
    teams: dict[str, _Team],
    options: TeamStrengthSettings,
) -> tuple[dict[str, dict[str, Any]], float | None, str, list[dict[str, Any]]]:
    """Rate last season opponent-adjusted, then carry it across by club name."""

    if previous_season is None:
        return (
            {
                team_id: {"attack": 1.0, "defence": 1.0, "promoted": True}
                for team_id in teams
            },
            None,
            "none",
            [],
        )
    previous_teams = _load_teams(database, previous_season)
    results = _fixture_results(
        database,
        season_code=previous_season,
        maximum_gameweek=None,
        as_of=None,
        maximum_ingestion_run_id=None,
    )
    if not results:
        return (
            {
                team_id: {"attack": 1.0, "defence": 1.0, "promoted": True}
                for team_id in teams
            },
            None,
            "none",
            [],
        )
    use_expected = _expected_goals_usable(results)
    ratings, league_average = _solve_ratings(
        results,
        list(previous_teams),
        options=options,
        use_expected_goals=use_expected,
        half_life=None,
        reference_gameweek=None,
    )
    # Regress toward the league mean before carrying anything forward: a
    # 38-match sample still contains a lot of noise at club level.
    regressed = {
        team_id: {
            "attack": _regress(
                value["attack"], value["matches"], options.prior_regression_matches
            ),
            "defence": _regress(
                value["defence"], value["matches"], options.prior_regression_matches
            ),
        }
        for team_id, value in ratings.items()
    }
    by_name = {
        _normalise_name(previous_teams[team_id].name): value
        for team_id, value in regressed.items()
        if team_id in previous_teams
    }
    prior: dict[str, dict[str, Any]] = {}
    for team_id, team in teams.items():
        matched = by_name.get(_normalise_name(team.name))
        if matched is None:
            prior[team_id] = {
                "attack": options.promoted_attack,
                "defence": options.promoted_defence,
                "promoted": True,
            }
        else:
            prior[team_id] = {
                "attack": matched["attack"],
                "defence": matched["defence"],
                "promoted": False,
            }
    return (
        prior,
        league_average,
        "expected_goals" if use_expected else "goals",
        results,
    )


#: Below this share of goals, the expected-goal feed is treated as too patchy
#: to rate on. Expected goals normally land within a few per cent of goals
#: over a season; a fifth missing means rows are absent, not that the league
#: underperformed, and the clubs whose rows are missing would be rated as if
#: they had created nothing.
MINIMUM_EXPECTED_GOAL_COVERAGE = 0.80

#: Below this league-wide retained-minutes share, squad continuity is treated
#: as unmeasurable rather than as a division-wide rebuild.
MINIMUM_LEAGUE_RETENTION = 0.10


def _expected_goals_usable(results: list[dict[str, Any]]) -> bool:
    """Whether the expected-goal rows are complete enough to rate on."""

    expected_rows = sum(int(row["expected_rows"] or 0) for row in results)
    if expected_rows <= 0:
        return False
    expected = sum(
        float(row["home_xg"] or 0.0) + float(row["away_xg"] or 0.0)
        for row in results
    )
    goals = sum(
        float(row["home_score"]) + float(row["away_score"]) for row in results
    )
    if goals <= 0:
        return False
    return expected / goals >= MINIMUM_EXPECTED_GOAL_COVERAGE


def _assist_per_goal(
    results: list[dict[str, Any]],
    options: TeamStrengthSettings,
) -> float:
    """League-wide assists per goal, shrunk toward the declared prior.

    Deliberately one number for the whole league. Club-level assist rates are
    noisy and the difference between clubs is small next to the difference
    between players, which the player share already captures.
    """

    goals = 0.0
    assists = 0.0
    for result in results:
        goals += float(result["home_xg"] or 0.0) + float(result["away_xg"] or 0.0)
        assists += float(result["home_xa"] or 0.0) + float(result["away_xa"] or 0.0)
    prior = options.assist_prior_matches
    if goals + prior <= 0:
        return options.assist_per_goal_prior
    return _clamp(
        (assists + prior * options.assist_per_goal_prior) / (goals + prior),
        0.0,
        1.0,
    )


def _current_season_ratings(
    database: HistoricalDatabase,
    *,
    season_code: str,
    gameweek_number: int,
    teams: dict[str, _Team],
    options: TeamStrengthSettings,
    as_of: datetime | None,
    maximum_ingestion_run_id: int | None,
) -> tuple[dict[str, dict[str, float]], float | None, str, list[dict[str, Any]]]:
    results = _fixture_results(
        database,
        season_code=season_code,
        maximum_gameweek=gameweek_number,
        as_of=as_of,
        maximum_ingestion_run_id=maximum_ingestion_run_id,
    )
    if not results:
        return {}, None, "none", []
    use_expected = _expected_goals_usable(results)
    ratings, league_average = _solve_ratings(
        results,
        list(teams),
        options=options,
        use_expected_goals=use_expected,
        half_life=options.current_half_life_gameweeks,
        reference_gameweek=gameweek_number,
    )
    return (
        ratings,
        league_average,
        "expected_goals" if use_expected else "goals",
        results,
    )


def _squad_continuity(
    database: HistoricalDatabase,
    season_code: str,
    previous_season: str,
    teams: dict[str, _Team],
) -> dict[str, SquadContinuity]:
    """Share of last season's output still registered to the same club.

    Retained means registered at the *same* club this season, not merely still
    in the division. Matching on the player alone would count a transfer within
    the league as continuity for the club that lost them, which is exactly
    backwards.

    Players are matched on the database's own player identity, not on the
    source's player id. The source reassigns those between seasons, so keying
    on them reports a settled squad as a total rebuild — and since continuity
    controls how much the prior is trusted, that quietly throws the prior away
    for every club. The identity link is the same one the projection uses to
    find a player's career history.

    Anyone who still cannot be matched counts as departed. That is the
    conservative direction: it lowers confidence in the prior, never raises it.
    """

    previous = database.connection.execute(
        """
        SELECT teams.name AS team_name,
               player_seasons.player_id,
               COALESCE(SUM(stats.minutes), 0) AS minutes,
               COALESCE(SUM(COALESCE(stats.expected_goals, stats.goals)), 0)
                   AS expected_goals,
               COALESCE(SUM(COALESCE(stats.expected_assists, stats.assists)), 0)
                   AS expected_assists,
               player_seasons.position
        FROM player_seasons
        JOIN seasons ON seasons.id = player_seasons.season_id
        JOIN teams ON teams.id = player_seasons.team_id
        LEFT JOIN player_fixture_stats stats
          ON stats.player_season_id = player_seasons.id
        WHERE seasons.code = ?
        GROUP BY player_seasons.id
        """,
        (previous_season,),
    ).fetchall()
    current = {
        (_normalise_name(str(row["team_name"])), int(row["player_id"]))
        for row in database.connection.execute(
            """
            SELECT teams.name AS team_name, player_seasons.player_id
            FROM player_seasons
            JOIN seasons ON seasons.id = player_seasons.season_id
            JOIN teams ON teams.id = player_seasons.team_id
            WHERE seasons.code = ?
            """,
            (season_code,),
        )
    }
    grouped: dict[str, list[Any]] = {}
    for row in previous:
        grouped.setdefault(_normalise_name(str(row["team_name"])), []).append(row)

    result: dict[str, SquadContinuity] = {}
    for team_id, team in teams.items():
        club = _normalise_name(team.name)
        rows = grouped.get(club)
        if not rows:
            continue
        totals = {"minutes": 0.0, "xg": 0.0, "xa": 0.0, "defensive": 0.0}
        retained = {"minutes": 0.0, "xg": 0.0, "xa": 0.0, "defensive": 0.0}
        retained_players = 0
        departed_players = 0
        for row in rows:
            minutes = float(row["minutes"] or 0.0)
            expected_goals = float(row["expected_goals"] or 0.0)
            expected_assists = float(row["expected_assists"] or 0.0)
            defensive = minutes if str(row["position"]) in {"GK", "DEF"} else 0.0
            totals["minutes"] += minutes
            totals["xg"] += expected_goals
            totals["xa"] += expected_assists
            totals["defensive"] += defensive
            still_here = (club, int(row["player_id"])) in current
            if still_here:
                retained_players += 1
                retained["minutes"] += minutes
                retained["xg"] += expected_goals
                retained["xa"] += expected_assists
                retained["defensive"] += defensive
            else:
                departed_players += 1
        result[team_id] = SquadContinuity(
            source_team_id=team.source_team_id,
            retained_minutes_share=_share(retained["minutes"], totals["minutes"]),
            retained_expected_goals_share=_share(retained["xg"], totals["xg"]),
            retained_expected_assists_share=_share(retained["xa"], totals["xa"]),
            retained_defensive_minutes_share=_share(
                retained["defensive"], totals["defensive"]
            ),
            unmatched_share=1.0 - _share(retained["minutes"], totals["minutes"]),
            retained_players=retained_players,
            departed_players=departed_players,
        )
    if not result:
        return result
    # Cross-season identity depends on a stable player code being present. If
    # it is not, every player looks new and the whole division reads as a
    # simultaneous total rebuild. Real leagues retain most of their minutes, so
    # a league-wide retention this low is a broken link, not twenty rebuilds.
    # Reporting nothing is the honest answer; reporting zero would strip the
    # preseason prior from every club without saying so.
    league_retention = sum(
        entry.retained_minutes_share for entry in result.values()
    ) / len(result)
    if league_retention < MINIMUM_LEAGUE_RETENTION:
        return {}
    return result


def _schedule_strength(
    results: list[dict[str, Any]],
    prior: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """How hard the opponents faced so far have been, relative to average.

    A club's quality is summarised as its attack multiplier divided by its
    defence multiplier — high attack and low goals conceded both raise it — and
    a club's schedule is the mean quality of the opponents it actually played,
    expressed against the league mean. Above 1.0 means a harder schedule than
    average.

    Prior ratings are used deliberately rather than current ones: judging the
    schedule by ratings that the schedule itself produced would be circular.
    This is reported, never applied; the opponent adjustment inside the rating
    already does the work.
    """

    if not results or not prior:
        return {}
    quality: dict[str, float] = {}
    for team_id, values in prior.items():
        defence = float(values["defence"])
        quality[team_id] = float(values["attack"]) / defence if defence > 0 else 1.0
    league_mean = sum(quality.values()) / len(quality)
    if league_mean <= 0:
        return {}

    faced: dict[str, list[float]] = {}
    for result in results:
        home = str(result["home_team_id"])
        away = str(result["away_team_id"])
        if home in quality and away in quality:
            faced.setdefault(home, []).append(quality[away])
            faced.setdefault(away, []).append(quality[home])
    return {
        team_id: round((sum(opponents) / len(opponents)) / league_mean, 6)
        for team_id, opponents in faced.items()
        if opponents
    }


def _prior_weight(
    prior: dict[str, Any],
    squad: SquadContinuity | None,
    options: TeamStrengthSettings,
    gameweek_number: int,
) -> float:
    """Pseudo-matches of prior evidence at this origin.

    Scaled by how intact the squad is, then decayed by how far into the season
    the origin sits, so last season stops mattering at the same rate that an
    early fixture does.
    """

    if prior.get("promoted", False):
        opening = options.promoted_prior_matches
    elif squad is None:
        opening = options.prior_matches_rebuilt_squad
    else:
        span = (
            options.prior_matches_intact_squad
            - options.prior_matches_rebuilt_squad
        )
        opening = (
            options.prior_matches_rebuilt_squad + span * squad.continuity_score
        )
    return opening * _decay(
        gameweek_number - 1, options.prior_half_life_gameweeks
    )


def _continuity_adjusted_prior(
    value: float,
    squad: SquadContinuity | None,
    options: TeamStrengthSettings,
) -> float:
    """Pull a rebuilt club's prior toward the league mean.

    Only the distance from average changes. Nothing here decides which way a
    rebuild went, because nothing in the data can: this reads retained minutes
    and output, never a fee or a reputation.
    """

    if squad is None:
        return value
    pull = options.rebuild_prior_regression * (1.0 - squad.continuity_score)
    return value * (1.0 - pull) + 1.0 * pull


def _uncertainty(
    matches: float,
    squad: SquadContinuity | None,
    is_promoted: bool,
    options: TeamStrengthSettings,
) -> float:
    uncertainty = options.base_uncertainty * _decay(
        matches, options.uncertainty_half_life_matches
    )
    if squad is not None:
        uncertainty += options.rebuild_uncertainty * (1.0 - squad.continuity_score)
    if is_promoted:
        uncertainty += options.rebuild_uncertainty
    return round(uncertainty, 6)


def _blend(
    prior_value: float,
    current_value: float | None,
    prior_weight: float,
    current_weight: float,
) -> float:
    if current_value is None or current_weight <= 0:
        return prior_value
    total = prior_weight + current_weight
    if total <= 0:
        return prior_value
    return (prior_value * prior_weight + current_value * current_weight) / total


def _regress(value: float, matches: float, regression_matches: float) -> float:
    if matches <= 0:
        return 1.0
    return (value * matches + 1.0 * regression_matches) / (
        matches + regression_matches
    )


def _normalise(values: dict[str, float]) -> dict[str, float]:
    positive = [value for value in values.values() if value > 0]
    average = sum(positive) / len(positive) if positive else 1.0
    if average <= 0:
        return values
    return {key: value / average for key, value in values.items()}


def _normalise_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _share(part: float, whole: float) -> float:
    return 0.0 if whole <= 0 else _clamp(part / whole, 0.0, 1.0)


def _decay(steps: float, half_life: float) -> float:
    if half_life <= 0:
        return 1.0
    return math.pow(0.5, max(0.0, steps) / half_life)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _limitations(
    prior_source: str,
    current_source: str,
    has_continuity: bool,
) -> tuple[str, ...]:
    limitations = [
        "Ratings are relative multipliers of the league average, not absolute "
        "goal expectations; venue is applied to the league average.",
        "Opponent adjustment is a multiplicative Poisson fixed point, not a "
        "fitted model. Its constants are declared, not estimated.",
    ]
    if prior_source == "goals":
        limitations.append(
            "The preseason prior fell back to actual goals because the "
            "previous season's expected-goal rows cover less than "
            f"{MINIMUM_EXPECTED_GOAL_COVERAGE:.0%} of the goals scored. "
            "Goals are a noisier measure of how a club played, so the prior "
            "is weaker than it would be with complete coverage."
        )
    elif prior_source == "none":
        limitations.append(
            "No previous season is available, so every club starts at the "
            "league average with promoted-club uncertainty."
        )
    if current_source == "goals":
        limitations.append(
            "Current-season evidence fell back to actual goals because the "
            "expected-goal rows are absent or too incomplete to rate on."
        )
    if not has_continuity:
        limitations.append(
            "Squad continuity could not be measured — there is no previous "
            "season, or no stable player code links the two — so prior "
            "confidence uses the rebuilt-squad default for every club. That "
            "is a statement of ignorance, not a finding that squads turned "
            "over."
        )
    else:
        limitations.append(
            "Squad continuity measures retained share of last season's "
            "minutes and output. It cannot tell whether a signing is an "
            "improvement, and no transfer fee or reputation is read."
        )
    return tuple(limitations)
