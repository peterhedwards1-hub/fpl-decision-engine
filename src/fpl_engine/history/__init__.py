"""Historical data storage and ingestion."""

from .csv_bundle import CsvBundleError, load_csv_bundle
from .database import HistoricalDatabase
from .records import (
    FixtureRecord,
    GameweekRecord,
    HistoricalBundle,
    IngestionSource,
    PlayerFixtureStatsRecord,
    PlayerGameweekSnapshotRecord,
    PlayerRecord,
    PlayerSeasonRecord,
    PlayerSeasonStatsObservationRecord,
    SeasonRecord,
    TeamRecord,
)
from .vaastav import (
    VaastavAdapter,
    VaastavClient,
    VaastavImportError,
    VaastavLoadResult,
    VaastavQualityReport,
)

__all__ = [
    "FixtureRecord",
    "CsvBundleError",
    "GameweekRecord",
    "HistoricalBundle",
    "HistoricalDatabase",
    "IngestionSource",
    "PlayerFixtureStatsRecord",
    "PlayerGameweekSnapshotRecord",
    "PlayerRecord",
    "PlayerSeasonStatsObservationRecord",
    "PlayerSeasonRecord",
    "SeasonRecord",
    "TeamRecord",
    "VaastavAdapter",
    "VaastavClient",
    "VaastavImportError",
    "VaastavLoadResult",
    "VaastavQualityReport",
    "load_csv_bundle",
]
