"""A pinned public mirror of the official FPL snapshot, for when the API is unreachable.

The production capture path is :class:`fpl_engine.live.client.FplApiClient`
talking to ``fantasy.premierleague.com``. Some execution environments cannot
reach that host at all — an egress policy refuses the connection before any
request is made — and in that situation the honest options are to stop, or to
read the same collections from a public mirror and label them as a mirror.

This module is the second option, and it is deliberately noisy about being one:

- the ingestion source is named ``vaastav-fpl-mirror``, never
  ``official-fpl-api``, so no downstream reader can mistake a mirrored capture
  for a direct one;
- the mirror revision is an immutable commit SHA, not a moving branch;
- every field the mirror cannot supply is either derived by a stated rule or
  left absent, and the derivations are listed on :class:`MirrorNotes`.

Two derivations exist, and both are visible in the returned notes.

**Gameweek deadlines.** The mirror publishes ``fixtures.csv`` and
``players_raw.csv`` but no ``events`` collection, so deadline times are
reconstructed from the fixture list using the published FPL rule: a Gameweek's
deadline is 90 minutes before its first kick-off. That is a rule, not a
measurement, and a deadline reconstructed this way must not be used to argue a
capture was taken before a real deadline.

**Preseason season-to-date statistics.** The mirrored ``players_raw.csv`` for a
season with no finished fixture still carries the *previous* season's totals
for continuing players — Arsenal players show a full season of minutes while
newly promoted clubs show none. Recording those as observations of the new
season would be a false statement about a season that has not started, so when
no fixture in the payload is finished every season-to-date counter is zeroed
and ``preseason_statistics_zeroed`` is reported. Nothing in the projection path
reads those counters; the season rates it does read come from the previous
season's fixture-level records, which are imported separately and untouched.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .client import ApiPayload, FplApiError

#: The public mirror. Read-only, and pinned by commit SHA at every call site.
DEFAULT_MIRROR_BASE_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League"
)

#: The ingestion provenance a mirrored capture is recorded under. Deliberately
#: not the official name.
MIRROR_SOURCE_NAME = "vaastav-fpl-mirror"
MIRROR_ADAPTER_VERSION = "vaastav-fpl-mirror-v1"

#: Published FPL rule: the deadline is 90 minutes before the first kick-off of
#: the Gameweek.
DEADLINE_MINUTES_BEFORE_FIRST_KICKOFF = 90

#: Season-to-date counters zeroed for a season with no finished fixture.
PRESEASON_ZEROED_FIELDS: tuple[str, ...] = (
    "minutes",
    "starts",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "own_goals",
    "penalties_saved",
    "penalties_missed",
    "yellow_cards",
    "red_cards",
    "saves",
    "bonus",
    "bps",
    "defensive_contribution",
    "defensive_contributions",
    "expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "expected_goals_conceded",
    "total_points",
    "event_points",
)


class MirrorSnapshotError(RuntimeError):
    """Raised when the mirror cannot supply a usable snapshot."""


@dataclass
class MirrorNotes:
    """Everything about a mirrored capture that a direct capture would not need."""

    base_url: str = DEFAULT_MIRROR_BASE_URL
    source_ref: str = ""
    season_code: str = ""
    files: tuple[str, ...] = ()
    derived_deadlines: bool = False
    deadline_rule: str = (
        "Gameweek deadline reconstructed as "
        f"{DEADLINE_MINUTES_BEFORE_FIRST_KICKOFF} minutes before the "
        "Gameweek's first kick-off; the mirror publishes no events collection."
    )
    preseason_statistics_zeroed: bool = False
    zeroed_fields: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": MIRROR_SOURCE_NAME,
            "adapter_version": MIRROR_ADAPTER_VERSION,
            "base_url": self.base_url,
            "source_ref": self.source_ref,
            "season_code": self.season_code,
            "files": list(self.files),
            "derived_deadlines": self.derived_deadlines,
            "deadline_rule": self.deadline_rule,
            "preseason_statistics_zeroed": self.preseason_statistics_zeroed,
            "zeroed_fields": list(self.zeroed_fields),
            "warnings": list(self.warnings),
        }


class MirrorSnapshotClient:
    """Reconstruct the two payloads the collector needs from mirrored CSV files.

    Satisfies the collector's ``SnapshotClient`` protocol, so the mirrored
    capture goes through exactly the same validation, archival, ingestion and
    verification-report path as a direct one.
    """

    def __init__(
        self,
        *,
        season_code: str,
        source_ref: str,
        base_url: str = DEFAULT_MIRROR_BASE_URL,
        timeout_seconds: float = 30.0,
        attempts: int = 4,
    ) -> None:
        if not source_ref or source_ref in {"main", "master", "HEAD"}:
            raise MirrorSnapshotError(
                "Mirror source_ref must be an immutable commit SHA, not a "
                "moving branch"
            )
        if attempts < 1:
            raise ValueError("Attempts must be at least one")
        self.season_code = season_code
        self.source_ref = source_ref
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.notes = MirrorNotes(
            base_url=self.base_url,
            source_ref=source_ref,
            season_code=season_code,
        )
        self._cache: dict[str, list[dict[str, str]]] = {}

    # -- fetching ---------------------------------------------------------

    def _url(self, filename: str) -> str:
        return (
            f"{self.base_url}/{self.source_ref}/data/{self.season_code}/{filename}"
        )

    def _fetch_csv(self, filename: str) -> list[dict[str, str]]:
        if filename in self._cache:
            return self._cache[filename]
        url = self._url(filename)
        request = Request(
            url,
            headers={
                "Accept": "text/csv",
                "User-Agent": "fpl-decision-engine/0.3",
            },
        )
        last: Exception | None = None
        for _ in range(self.attempts):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                break
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                last = error
        else:  # pragma: no cover - exhausted retries
            raise MirrorSnapshotError(f"Could not fetch {url}: {last}")
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        if not rows:
            raise MirrorSnapshotError(f"{url} contained no rows")
        self._cache[filename] = rows
        if filename not in self.notes.files:
            self.notes.files = (*self.notes.files, filename)
        return rows

    # -- payloads ---------------------------------------------------------

    def bootstrap_static(self) -> ApiPayload:
        teams = [_json_scalars(row) for row in self._fetch_csv("teams.csv")]
        elements = [_json_scalars(row) for row in self._fetch_csv("players_raw.csv")]
        fixtures = [_json_scalars(row) for row in self._fetch_csv("fixtures.csv")]
        events = self._events(fixtures)
        if not any(bool(fixture.get("finished")) for fixture in fixtures):
            elements = [_zero_season_counters(element) for element in elements]
            self.notes.preseason_statistics_zeroed = True
            self.notes.zeroed_fields = PRESEASON_ZEROED_FIELDS
        payload = {"events": events, "teams": teams, "elements": elements}
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        return ApiPayload(url=self._url("players_raw.csv"), body=body, data=payload)

    def fixtures(self) -> ApiPayload:
        payload = [_json_scalars(row) for row in self._fetch_csv("fixtures.csv")]
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        return ApiPayload(url=self._url("fixtures.csv"), body=body, data=payload)

    def _events(self, fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
        first_kickoff: dict[int, datetime] = {}
        finished: dict[int, bool] = {}
        for fixture in fixtures:
            event = fixture.get("event")
            kickoff = fixture.get("kickoff_time")
            if event is None or not kickoff:
                continue
            number = int(event)
            moment = _parse_time(str(kickoff))
            if moment is None:
                continue
            if number not in first_kickoff or moment < first_kickoff[number]:
                first_kickoff[number] = moment
            finished[number] = finished.get(number, True) and bool(
                fixture.get("finished")
            )
        if not first_kickoff:
            raise MirrorSnapshotError(
                "The mirrored fixture list has no Gameweek kick-off times, so "
                "no deadline can be reconstructed"
            )
        self.notes.derived_deadlines = True
        return [
            {
                "id": number,
                "deadline_time": (
                    first_kickoff[number]
                    - timedelta(minutes=DEADLINE_MINUTES_BEFORE_FIRST_KICKOFF)
                )
                .astimezone(UTC)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "finished": finished.get(number, False),
                "deadline_time_source": "derived_from_first_kickoff",
            }
            for number in sorted(first_kickoff)
        ]


def _parse_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _zero_season_counters(element: dict[str, Any]) -> dict[str, Any]:
    zeroed = dict(element)
    for name in PRESEASON_ZEROED_FIELDS:
        if name in zeroed:
            zeroed[name] = 0
    return zeroed


def _json_scalars(row: dict[str, str]) -> dict[str, Any]:
    """Turn CSV text back into the JSON scalar types the API would have sent."""

    converted: dict[str, Any] = {}
    for key, raw in row.items():
        if key is None:
            continue
        text = (raw or "").strip()
        if text == "" or text == "None":
            converted[key] = None
            continue
        if text == "True":
            converted[key] = True
            continue
        if text == "False":
            converted[key] = False
            continue
        try:
            converted[key] = int(text)
            continue
        except ValueError:
            pass
        try:
            converted[key] = float(text)
            continue
        except ValueError:
            pass
        converted[key] = text
    return converted


@dataclass(frozen=True)
class SnapshotProvenance:
    """How an ingested snapshot should describe where it came from."""

    source_name: str
    adapter_version: str
    identifier_namespace: str = "official-fpl"


OFFICIAL_PROVENANCE = SnapshotProvenance(
    source_name="official-fpl-api",
    adapter_version="official-fpl-api-v1",
)

MIRROR_PROVENANCE = SnapshotProvenance(
    source_name=MIRROR_SOURCE_NAME,
    adapter_version=MIRROR_ADAPTER_VERSION,
)


def official_api_reachable(*, timeout_seconds: float = 15.0) -> tuple[bool, str]:
    """Whether a direct official capture is possible right now, and why not.

    Called before falling back, so an artifact can state that the mirror was
    used because the official host was unreachable rather than because nobody
    tried.
    """

    from .client import FplApiClient

    try:
        FplApiClient(timeout_seconds=timeout_seconds).bootstrap_static()
    except FplApiError as error:
        return False, str(error)
    except Exception as error:  # pragma: no cover - defensive
        return False, f"{type(error).__name__}: {error}"
    return True, "The official FPL API responded."
