"""Typed records accepted by the historical database layer."""

from dataclasses import dataclass
from datetime import date, datetime

from fpl_engine.domain import Position


@dataclass(frozen=True)
class IngestionSource:
    name: str
    retrieved_at: datetime
    url: str | None = None
    content_sha256: str | None = None
    identifier_namespace: str = "official-fpl"
    source_revision: str | None = None
    adapter_version: str | None = None


@dataclass(frozen=True)
class SeasonRecord:
    code: str
    name: str
    starts_on: str | None = None
    ends_on: str | None = None


@dataclass(frozen=True)
class TeamRecord:
    source_team_id: str
    name: str
    short_name: str


@dataclass(frozen=True)
class PlayerRecord:
    source_player_id: str
    web_name: str
    first_name: str = ""
    second_name: str = ""
    date_of_birth: str | None = None
    official_fpl_code: str | None = None
    opta_code: str | None = None


@dataclass(frozen=True)
class PlayerSeasonRecord:
    source_player_id: str
    source_team_id: str
    position: Position
    start_price_tenths: int | None = None
    end_price_tenths: int | None = None
    identifier_namespace: str | None = None


@dataclass(frozen=True)
class GameweekRecord:
    number: int
    deadline_time: str | None = None
    is_finished: bool = False


@dataclass(frozen=True)
class FixtureRecord:
    source_fixture_id: str
    home_team_source_id: str
    away_team_source_id: str
    gameweek_number: int | None = None
    kickoff_time: str | None = None
    home_score: int | None = None
    away_score: int | None = None
    finished: bool = False


@dataclass(frozen=True)
class PlayerFixtureStatsRecord:
    source_player_id: str
    source_fixture_id: str
    minutes: int = 0
    starts: bool = False
    goals: int = 0
    assists: int = 0
    clean_sheet: bool = False
    goals_conceded: int = 0
    own_goals: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    saves: int = 0
    bonus: int = 0
    bps: int = 0
    defensive_contributions: int = 0
    expected_goals: float | None = None
    expected_assists: float | None = None
    expected_goal_involvements: float | None = None
    expected_goals_conceded: float | None = None
    total_points: int = 0


@dataclass(frozen=True)
class PlayerGameweekSnapshotRecord:
    source_player_id: str
    gameweek_number: int
    price_tenths: int
    captured_at: datetime | None
    selected_by_percent: float | None = None
    transfers_in: int | None = None
    transfers_out: int | None = None
    status: str | None = None
    chance_of_playing_next_round: int | None = None
    news: str | None = None
    source_team_id: str | None = None
    selected_count: int | None = None
    observation_kind: str = "live_pre_deadline"
    timing_quality: str = "exact"
    source_observation_key: str | None = None
    observed_on: date | None = None

    def validate_timing(self) -> None:
        if self.timing_quality == "exact":
            if self.captured_at is None or self.captured_at.tzinfo is None:
                raise ValueError(
                    "exact observation timing requires a timezone-aware captured_at"
                )
            if self.observed_on is not None:
                raise ValueError("exact observation timing cannot include observed_on")
        elif self.timing_quality == "date_only":
            if self.captured_at is not None:
                raise ValueError("date_only observation timing cannot include captured_at")
            if self.observed_on is None:
                raise ValueError("date_only observation timing requires observed_on")
        elif self.timing_quality == "unknown":
            if self.captured_at is not None or self.observed_on is not None:
                raise ValueError("unknown observation timing cannot include a date or timestamp")
        else:
            raise ValueError(f"invalid observation timing quality {self.timing_quality!r}")


@dataclass(frozen=True)
class HistoricalBundle:
    season: SeasonRecord
    teams: tuple[TeamRecord, ...] = ()
    players: tuple[PlayerRecord, ...] = ()
    player_seasons: tuple[PlayerSeasonRecord, ...] = ()
    gameweeks: tuple[GameweekRecord, ...] = ()
    fixtures: tuple[FixtureRecord, ...] = ()
    fixture_stats: tuple[PlayerFixtureStatsRecord, ...] = ()
    gameweek_snapshots: tuple[PlayerGameweekSnapshotRecord, ...] = ()
