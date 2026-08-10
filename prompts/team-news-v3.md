# FPL team-news research prompt v3

You are completing a structured, decision-focused team-news pass for the exact
input package supplied below. The package is authoritative for player identity,
the selected squad, planned XI, bench order, captain, vice-captain, projection
assumptions, alternatives and research window. Do not infer identities from
names. Use only official FPL IDs in the package or directory; use null for an
unresolved discovery. Return JSON only: no Markdown, explanations or code
fences. Echo input_package_id and input_package_hash exactly.

## Priority and coverage

Every selected squad member and every supplied alternative needs exactly one
coverage record, even when no material evidence is found. Coverage is not
evidence. Use checked_no_material_evidence when relevant checks were made and
found nothing material; use not_checked when they were not made. Use
partially_checked, source_unavailable or identity_unresolved when appropriate.
Never use an empty evidence list as proof of coverage.

Priorities:

- critical: captain, vice-captain, doubtful high-value starter or
  decision-changing uncertainty;
- starting_xi: other planned starters;
- bench_cover: first bench and players likely to be needed by autosubs;
- squad: remaining selected players;
- alternative: supplied realistic near-optimal replacements;
- broad_scan: players discovered in the wider league sweep.

Derive research questions from supplied data. Examples include: Is the player
expected to start? Is the player fully fit and training normally? Has the
tactical position changed? Is the player first-choice for penalties, corners or
free kicks? Has the manager indicated rotation? Is there a reliable return date?
Has a transfer changed the role? Do not invent a question without a reason in
the package.

## Research scope

Check the selected squad exhaustively, then check all supplied alternatives.
Also perform a bounded league-wide sweep for major injuries or suspensions,
significant transfers, manager changes, newly established starters, unexpected
predicted line-ups, penalty-taker or set-piece changes, tactical-role changes,
important team disruption, and outside-shortlist players whose new information
could make them decision-relevant. Keep discoveries separate from exhaustive
coverage. Resolve discoveries against the directory when possible and otherwise
use a null ID with identity_status unresolved; never guess.

Use this source hierarchy: official club/FPL/league statements and manager
press conferences first; reliable direct reporting next; established
predicted-lineup sources after that; rumours last and rarely as a numerical
basis. Every evidence item and discovery needs a direct URL and publication
time. A source before the research window is background context only unless
explicitly marked as such.

For `source_tier`, use exactly one of these four lowercase enum values. Do not
invent alternatives such as `reliable_direct_reporting`, `secondary`, `direct`,
or `reliable`:

- `official`: an official club, FPL, league or manager source;
- `strong_reporting`: reliable named journalism or direct reporting;
- `predicted_lineup`: an established predicted-line-up source;
- `rumour`: unverified, speculative, fan-site or otherwise weak reporting.

Classify the source itself, not the certainty of the claim. Use the `confidence`
field for claim certainty. Before returning, check every evidence source,
supporting source, conflicting source and discovery uses one of those four exact
values.

Modes:

- preseason: friendlies are experimental, travel squads are incomplete,
  training absences may be unexplained, registrations/transfers may still move,
  and roles are less certain. The supplied window start is mandatory;
- provisional: focus on carried injuries, suspensions, congestion, likely
  transfers, early manager comments and known return timelines;
- final: focus on fresh manager comments, latest training, predicted line-ups,
  late injuries, registrations and unresolved critical players.

## Evidence, conflicts and adjustments

Keep facts separate from suggestions. Preserve supporting and conflicting
sources instead of collapsing them into false certainty. If sources conflict,
record unresolved uncertainty, the event likely to resolve it, and confidence
after considering the conflict.

No numerical adjustment is preferable to an invented one. Rumours should rarely
support one. A predicted lineup normally changes confidence or prompts review;
it does not automatically set minutes. Interpret manager quotes narrowly:
available is not starting; back in training is not ready for 90; absence from
one training image is not proof of injury.

Use adjustment_support supported_numeric only for a directly supported
expected_minutes_delta currently understood by this application. Use
structured_flag for role, set-piece or availability flags stored for review but
not directly applied. Use informational when no model change is proposed and
unsupported when a suggestion cannot currently be applied. State
adjustment_basis for every suggestion. Never translate a role, injury or
set-piece finding into guessed minutes. Every accepted model-affecting change
still requires explicit human review.

Allowed `evidence_type` values are exactly: injury, suspension, training,
manager_quote, predicted_lineup, tactical_role, transfer, other. There is **no**
`set_piece` or `team_disruption` type — the importer rejects them. For a
set-piece change use tactical_role or other; for a coaching change or midfield
reshuffle use tactical_role when it bears on one player's role, transfer for an
actual signing or sale, and other otherwise. Unknown values are forbidden.

Set `model_area` to exactly one of: expected_minutes, appearance_probability,
starting_probability, sixty_probability, availability, return_date, penalties,
corners, direct_free_kicks, tactical_role, attacking_position, team_attack,
team_defence, fixture_status, informational. Do not invent areas such as
`club_assignment` or `minutes`; for a transfer or club-label change that only
needs a data refresh, use `informational`.

`suggested_adjustment` is **either null or an object with exactly the two keys
`kind` and `value`** — never a sentence, and never any other keys. Provide a
non-null object only with adjustment_support `supported_numeric`, where `kind`
is one of expected_minutes_delta, appearance_probability_delta,
starting_probability_delta or sixty_probability_delta and `value` is the numeric
delta (within ±90 for minutes, ±1 for probabilities). For adjustment_support
`structured_flag`, `informational` or `unsupported`, `suggested_adjustment`
**must be null** and the reasoning goes in `adjustment_basis`.

## Required JSON

Use the generated package for all root metadata and coverage IDs. This is a
complete root shape; include every required selected/alternative coverage row.

```json
{
  "schema_version": 3,
  "prompt_version": "fpl-team-news-v3",
  "research_run_id": "research-run-id",
  "input_package_id": "tnp-package-id",
  "input_package_hash": "sha256-package-hash",
  "season_code": "2026-27",
  "gameweek": 1,
  "target_deadline": "2026-08-21T17:30:00+00:00",
  "research_mode": "preseason",
  "research_window_start": "2026-07-01T00:00:00+00:00",
  "generated_at": "2026-08-20T18:00:00+00:00",
  "coverage": [
    {
      "source_player_id": "123",
      "priority": "starting_xi",
      "status": "checked_no_material_evidence",
      "areas_checked": ["injury", "training", "predicted_lineup", "tactical_role", "set_pieces"],
      "latest_source_checked_at": "2026-08-20T18:00:00+00:00",
      "notes": null
    }
  ],
  "evidence": [],
  "discoveries": [],
  "limitations": []
}
```

Evidence items use exactly these fields. Supporting and conflicting entries
use source_name, source_url, published_at, source_tier and fact_summary.

```json
{
  "evidence_id": "evidence-1",
  "issue_id": null,
  "source_player_id": "123",
  "priority": "starting_xi",
  "selected_player_status": "selected",
  "evidence_type": "manager_quote",
  "fact_summary": "Concise fact, not a recommendation.",
  "source_name": "Official club press conference",
  "source_url": "https://example.invalid/direct-source",
  "published_at": "2026-08-20T16:00:00+00:00",
  "source_tier": "official",
  "confidence": "high",
  "model_area": "informational",
  "suggested_adjustment": null,
  "adjustment_basis": null,
  "adjustment_support": "informational",
  "requires_decision": true,
  "decision_question": "Does this resolve the availability concern?",
  "expiry": "2026-08-21T17:30:00+00:00",
  "context_scope": "current_window",
  "supporting_evidence": [],
  "conflicting_evidence": [],
  "unresolved_uncertainty": null,
  "resolution_event": null,
  "confidence_after_conflict": null
}
```

Discoveries must contain discovery_id, source_player_id (or null),
identity_status, discovery_type, fact_summary, source_name, source_url,
published_at, source_tier and decision_relevance. Limitations is an array of
strings. Do not add prose after the JSON.
