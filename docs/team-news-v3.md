# Team-news v3 operator guide

Team-news v3 is a manual, reviewable weekly workflow. It focuses research on
the selected squad and near-optimal alternatives while retaining a bounded
league-wide sweep. Selected players receive special attention because their
status can change the actual XI, bench autosubs, captaincy or the value of a
transfer; alternatives matter because news can move the decision even when no
current player is injured.

## Weekly flow

1. Collect and freeze the official FPL snapshot.
2. Generate a pre-news projection.
3. Save the manager squad and generate the provisional squad or transfer recommendation.
4. Export the package:

   fpl-history --database data/fpl.sqlite3 export-team-news-research-package 2026-27 --gameweek 1 --projection-run 123 --research-mode provisional --research-window-start 2026-08-14T18:00:00+00:00 --alternatives 15 --output data/team-news-gw1.json

5. Paste the package into a dedicated ChatGPT research chat with
   prompts/team-news-v3.md. No API integration is used.
6. Import the JSON result:

   fpl-history --database data/fpl.sqlite3 import-team-news-research-result --season-code 2026-27 --gameweek 1 --input data/team-news-gw1-result.json

7. Review every material item, incomplete critical coverage, conflicts and expiring evidence in the browser.
8. Generate the post-news projection from the same underlying FPL snapshot.
9. Freeze the final decision. No ChatGPT suggestion changes a forecast without an explicit human review and rationale.

The exporter accepts a projection run, optional recommendation identifier or
JSON context, research mode, explicit window start, alternative bound and
output path. Preseason does not require a previous Gameweek deadline; its
window start must still be supplied. Provisional focuses on carried issues and
early updates. Final focuses on late press conferences, training, lineups,
registrations and unresolved critical players.

## Reading the result

Coverage answers “was this player checked?” Evidence answers “what material
fact was found?”. checked_no_material_evidence is a positive completed check;
not_checked is not. An empty evidence list is valid only with complete
coverage. Coverage is required for every selected player and supplied
alternative.

The importer records source tier, publication time, expiry, priority, selected
status, decision question, adjustment basis and review state. Official sources
and manager statements outrank direct reporting, predicted lineups and rumours.
Conflicting evidence is stored as supporting and conflicting source groups with
unresolved uncertainty and a likely resolution event. It is not flattened into
one certain conclusion.

V3 supports expected-minutes deltas plus structured areas such as appearance,
starting and 60-minute probabilities, availability, return dates, penalties,
corners, free kicks, tactical role, attacking position, team attack/defence and
fixture status. Only directly supported adjustment types that the production
model can apply are eligible for automatic projection use. Other numerical
suggestions, role/set-piece flags and informational findings remain visible for
human review and are never silently translated into minutes.

## Stale evidence and evaluation

Each package and result is linked by package ID and deterministic hash, target
Gameweek, target deadline, research window, prompt version and source ingestion
run. Unknown packages, hash mismatches, wrong Gameweeks and post-deadline
results are rejected. Sources before the window are marked as outside-window
or background context. Expired accepted evidence is excluded from current
adjustments. Earlier runs are historical navigation only.

Pre-news and post-news projections must use the same snapshot and are linked to
the package, research run, evidence and ingestion run. Their difference is
reported as news uplift only when the underlying snapshot matches; it is not a
model promotion signal. V1 and v2 records remain readable and retain their
original schema and prompt labels. V1/v2 records do not acquire v3 coverage by
reinterpretation.

## Research-to-decision loop

The complete decision loop is:

```text
baseline projection -> baseline decision -> ChatGPT research
    -> human review -> reviewed modifiers -> revised projection
    -> same optimiser -> before/after decision comparison
```

The research result is advisory. Importing it never changes a projection. For
each material evidence item choose **Reject**, **Accept as informational**, or
**Accept with model modifier**. The first two choices do not create a model
input. A modifier is created only after the reviewer supplies its supported
type, operation, value, Gameweek range, rationale and evidence link. Unsupported
role suggestions remain visible as reviewed evidence and are labelled
“Reviewed but not model-supported”.

Supported production modifiers are the expected-minutes, probability and
availability fields listed in the v3 schema. Values apply only in their
explicit Gameweek range and are excluded after expiry. Accepted modifiers are
immutable; corrections use a superseding record.

Use **Apply research and rerun decision** in Weekly Cycle after all current
evidence has been reviewed. The revised run uses the same source-ingestion run,
model configuration, start Gameweek and horizon as the baseline. The
comparison reports the baseline recommendation under baseline beliefs, that
same recommendation revalued under revised beliefs, and the revised
recommendation under revised beliefs. Decision improvement is the third minus
the second; projection impact is the second minus the first. Changed actions
include accepted modifier IDs and are classified as `robust`, `moderate` or
`near_tie`.

CLI equivalents are:

```text
fpl-history --database data/fpl.sqlite3 review-team-news-modifier ...
fpl-history --database data/fpl.sqlite3 apply-team-news-research --baseline-projection-run 123 --decision-type opening_squad
fpl-history --database data/fpl.sqlite3 compare-research-decision --baseline-projection-run 123 --revised-projection-run 140
```

Transfer comparisons additionally accept `--current-squad-json` containing
`player_ids`, `selling_prices_tenths`, `bank_tenths`, `free_transfers` and
optional `available_chips`. Transfer and weekly-XI paths reuse the exact
manager state and optimiser rules; ChatGPT never selects players or performs
FPL account actions.
