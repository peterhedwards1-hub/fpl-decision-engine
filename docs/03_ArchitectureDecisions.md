# FPL Decision Engine — Architecture Decisions

**Status:** Accepted  
**Date:** 2026-07-29

This document records decisions that apply across the canonical Milestones 0–10 roadmap.
They remain in force until replaced by a dated decision with an explicit migration plan.

## ADR-001 — Canonical implementation roadmap

`02_ImplementationRoadmap.md` is the canonical delivery roadmap. Its Milestones 0–10,
acceptance criteria, dependencies and definition of done govern implementation status.

The broader phases in `00_ProjectSpecification.md` describe product scope. Where their
numbering differs, the implementation roadmap controls sequencing and milestone names.

## ADR-002 — SQLite is the operational database

SQLite supersedes the roadmap's original DuckDB proposal for the operational store.

Reasons:

- the application is a single-user decision-support system;
- the existing SQLite schema, migrations and transactional ingestion are tested;
- SQLite supports the required integrity constraints and point-in-time records;
- it keeps collection and first use dependency-free;
- replacing the store now would add migration risk without improving a current milestone.

DuckDB may later be introduced as a read-only analytical layer for large historical replay
or model-development queries. It must not become a second source of truth.

## ADR-003 — Durable public-source snapshots

Official public FPL responses and manifests will be retained as immutable, compressed files
on a private append-only `snapshot-data` branch. The operational SQLite database will be
rebuildable from those archives and will not be committed as a changing binary.

The collection workflow must:

1. preserve the exact decompressed provider bytes and checksum;
2. never rewrite an existing capture;
3. restore prior captures before producing the current database and report;
4. fail on a conflicting capture path or checksum;
5. keep private manager state and credentials out of the snapshot branch.

GitHub Actions artifacts remain convenient report downloads, but they are not durable
storage and must not be the only copy of a capture.

## ADR-004 — Initial manager-state contract

A manager-state snapshot applies to one season, Gameweek and deadline. It contains:

- 15 official FPL player identifiers;
- purchase and current selling price for every player;
- money in the bank;
- available free transfers;
- remaining and used chip state;
- starting XI;
- ordered substitutes;
- captain and vice-captain;
- capture timestamp, source and schema version.

Manager state must be validated against the applicable season rules, saved without direct
database editing and preserved when a later correction is recorded. The first implementation
uses manual browser entry; authenticated collection is a later optional extension.
