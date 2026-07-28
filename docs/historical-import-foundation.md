# Historical import foundation and hardening

Milestone 2.1 prepares the SQLite store for several seasons of FPL data. It does not
download or import the Vaastav dataset; the next milestone will provide that adapter.

## Identity model

`players` is the internally stable identity for a footballer. It contains descriptive
metadata, but names are never used to merge records. Optional stable identifiers live in
`player_identifiers`, currently supporting `official_fpl_code` and `opta_code` with a
unique `(identifier_type, identifier_value)` constraint.

`player_seasons` stores the season-specific FPL element ID in `source_player_id`, together
with its `identifier_namespace`, position, baseline team and season prices. Without a
stable identifier, the same element ID in another season creates a new player identity.
With a matching FPL or Opta code, the season membership links to the existing player
identity. Missing stable identifiers are valid.

## Namespaces and provenance

The identifier namespace answers “what system issued this ID?”; for the current official
records it is `official-fpl`. The ingestion run’s `source_name` answers “where did this
copy of the data come from?”, such as `official-fpl-api` or a future historical dataset
adapter. A run also records its URL, retrieval time, checksum, optional source revision
or commit SHA, and optional adapter version.

Teams, fixtures and player-seasons are unique by season, identifier namespace and
upstream ID. Refreshing an official ID through a different delivery source is allowed and
updates provenance. A contradictory team identity or fixture home/away pairing is
rejected rather than silently overwritten.

## Gameweek observations

`player_gameweek_observations` replaces the version-2 one-row snapshot constraint. An
observation contains price, optional selected count, optional selected percentage, current
team, availability/news fields, an `observation_kind`, timing quality, optional exact
`observed_at`, and ingestion provenance.

Supported kinds are `live_pre_deadline`, `post_gameweek`, and
`historical_reconstruction`. Timing quality is `exact`, `date_only`, or `unknown`.
Historical data must leave `observed_at` null when an exact timestamp is unavailable.
Date-only observations use the nullable `observed_on` ISO date. SQLite and the typed record
layer enforce the three valid combinations, and schema v4 adds this invariant.

`source_observation_key` is the observation idempotency key, not the content checksum.
The live collector derives it from delivery source, season, Gameweek, UTC capture time and
the archived content checksum. Reprocessing the same archive is idempotent; a later
capture of identical bytes is a new observation. Historical adapters may provide their own
stable deterministic key. The checksum remains provenance/content identity and does not
collapse separate captures.

Latest queries use explicit timing precedence: exact timestamps, then known dates, then
unknown timing, followed by ingestion retrieval time and row ID. Callers can request
`latest_available`, `latest_pre_deadline`, or `latest_post_gameweek`; reports use the same
selection rules so post-Gameweek observations do not contaminate a pre-deadline view.

The legacy `player_gameweek_snapshots` name remains as a read-only compatibility view.
The season-level team remains the initial/baseline affiliation. A transfer is stored on
the timestamped observation and does not rewrite earlier affiliation.

## Historical ownership

`selected_count` and `selected_by_percent` are independent nullable fields. Historical
sources that provide selected-manager counts do not require a manager total and do not
fabricate a percentage. The live API continues to populate the percentage field.

## Migration

The application supports schema version 4 and migrates version 2 in place through the
version-3 identity/observation model. The v2-to-v3 migration
rebuilds the affected SQLite tables transactionally with foreign-key enforcement disabled
only for the table swap, preserves existing row IDs and copies all domain/provenance
records. Existing snapshot rows become `live_pre_deadline` observations with their old
captured timestamp and a `legacy-v2-{id}` idempotency key. The v3-to-v4 migration adds
`observed_on`, converts any legacy date-only representation to a date, checks foreign keys
before committing, fails clearly on errors, is repeatable, and rejects newer schema
versions. No domain rows are recreated by ordinary initialisation.

When a migrated season-specific record has no stable identifier and a later delivery
supplies an official FPL or Opta identifier, reconciliation is explicit and transactional.
The stable-ID player row survives; `player_seasons` rows move to it, so fixture statistics
and observations retain their foreign-key identities. Contradictory stable identifiers or
duplicate season-specific keys abort the ingestion. Names alone never reconcile players.

## Generated operational data

SQLite databases, raw API archives, reports, journal files and WAL files under `data/` are
operational outputs and are ignored by Git. They can still be created by the collector and
downloaded as workflow artifacts. Small, intentional fixtures under `tests/fixtures/`
remain trackable.

## Next adapter interface

The next milestone can expose a source-specific command such as:

```text
fpl-history import-vaastav \
  --database data/fpl.sqlite3 \
  --source-ref <commit-sha> \
  --seasons 2022-23 2023-24 2024-25 2025-26
```

That adapter should emit the normalised typed records, set `identifier_namespace=official-fpl`,
record the dataset commit in `source_revision`, and use `historical_reconstruction`
observations where a precise capture time is unavailable. It should validate every
fixture-stat and observation reference against `player_seasons.csv`, and use
`selected_count` without deriving `selected_by_percent` unless manager totals are known.

Deliberate limitations for the next milestone are the absence of the Vaastav downloader,
automated cross-source fuzzy identity matching, and any private manager data. Temporary
official FPL player codes from live payloads are intentionally ignored until a permanent
code is available; an Opta code remains usable when present.
