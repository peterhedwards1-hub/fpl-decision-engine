from datetime import UTC, datetime

from fpl_engine.domain import Position
from fpl_engine.history.database import HistoricalDatabase
from fpl_engine.history.records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerFixtureStatsRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    SeasonRecord,
    TeamRecord,
)
from fpl_engine.playing_time import (
    predict_live_hurdles,
    train_and_evaluate_hurdle_model,
)


def _season_bundle(season: str) -> HistoricalBundle:
    players = tuple(
        PlayerRecord(str(player), f"Player {player}")
        for player in range(1, 5)
    )
    gameweeks = tuple(
        GameweekRecord(gameweek, None, True)
        for gameweek in range(1, 7)
    )
    fixtures = tuple(
        FixtureRecord(
            str(gameweek),
            "1",
            "2",
            gameweek,
            finished=True,
        )
        for gameweek in range(1, 7)
    )
    minute_pattern = (0, 30, 60, 90)
    stats = tuple(
        PlayerFixtureStatsRecord(
            str(player),
            str(gameweek),
            minutes=minute_pattern[(player + gameweek) % 4],
            starts=minute_pattern[(player + gameweek) % 4] >= 60,
            total_points=0,
        )
        for gameweek in range(1, 7)
        for player in range(1, 5)
    )
    return HistoricalBundle(
        season=SeasonRecord(season, season.replace("-", "/")),
        teams=(
            TeamRecord("1", "One", "ONE"),
            TeamRecord("2", "Two", "TWO"),
        ),
        players=players,
        player_seasons=tuple(
            PlayerSeasonRecord(
                str(player),
                "1" if player <= 2 else "2",
                (
                    Position.DEF
                    if player <= 2
                    else Position.MID
                ),
                50,
                50,
            )
            for player in range(1, 5)
        ),
        gameweeks=gameweeks,
        fixtures=fixtures,
        fixture_stats=stats,
    )


def test_playing_time_hurdle_trains_all_four_parts(tmp_path) -> None:
    source = IngestionSource(
        name="test",
        retrieved_at=datetime(2026, 7, 30, tzinfo=UTC),
        identifier_namespace="official-fpl",
    )
    with HistoricalDatabase(tmp_path / "fpl.sqlite3") as database:
        database.initialise()
        database.ingest_bundle(source, _season_bundle("2023-24"))
        database.ingest_bundle(source, _season_bundle("2024-25"))

        report = train_and_evaluate_hurdle_model(
            database,
            training_seasons=("2023-24",),
            validation_season="2024-25",
            artifact_path=tmp_path / "hurdle.joblib",
            family="logistic",
            seed=3,
        )
        current_players = [
            dict(row)
            for row in database.connection.execute(
                """
                SELECT player_seasons.id AS player_season_id,
                       player_seasons.team_id, position,
                       COALESCE(start_price_tenths, 50) AS price_tenths
                FROM player_seasons
                JOIN seasons ON seasons.id = player_seasons.season_id
                WHERE seasons.code = '2024-25'
                ORDER BY player_seasons.id
                """
            )
        ]
        live = predict_live_hurdles(
            database,
            tmp_path / "hurdle.joblib",
            season_code="2024-25",
            start_gameweek=6,
            players=current_players,
        )

    assert report.challenger.samples == 24
    assert report.chronological_oof_samples > 0
    assert report.downstream_points is None
    assert (tmp_path / "hurdle.joblib").exists()
    assert (tmp_path / "hurdle.json").exists()
    assert len(live) == 4
    assert all(0 < values[0] < 1 for values in live.values())
