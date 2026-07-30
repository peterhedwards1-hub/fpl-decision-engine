# FPL team-news research prompt v2

Research material team news for the supplied FPL season, Gameweek and deadline.
Consider only information published after the previous deadline and no later than the
current research time.

## Inputs supplied by the operator

- season and target Gameweek;
- deadline and research timestamps;
- official FPL player IDs, names and clubs;
- current squad and plausible transfer targets;
- the previous research-run identifier, when available.

## Research requirements

1. Prioritise official club statements and manager press conferences.
2. Then consider reliable direct reporting and established predicted-lineup sources.
3. Treat rumours as low-confidence evidence and never restate them as facts.
4. Check injuries, suspensions, training, predicted line-ups, recent starts and
   substitutions, congestion, transfers, tactical roles, penalties and set pieces.
5. Give every non-trivial item a direct source URL and publication timestamp.
6. Separate factual evidence from the proposed model adjustment.
7. Use only official FPL player IDs supplied in the input. Use `null` when identity is
   genuinely general or unresolved; never guess an ID.
8. Suggest an `expected_minutes_delta` only where the evidence supports a material
   change. Otherwise return `null`.
9. Set an expiry no later than the target deadline or the next expected material update.
10. Return JSON only, matching schema version 2 exactly. Do not add prose or fields.

## Output contract

```json
{
  "schema_version": 2,
  "prompt_version": "fpl-team-news-v2",
  "research_run_id": "unique identifier for this research pass",
  "generated_at": "timezone-aware ISO-8601 timestamp",
  "evidence": [
    {
      "evidence_type": "injury",
      "fact_summary": "Concise factual evidence",
      "source_name": "Source or publication name",
      "source_url": "Direct URL or null",
      "published_at": "timezone-aware ISO-8601 timestamp",
      "source_tier": "official",
      "confidence": "high",
      "source_player_id": "official FPL element ID or null",
      "model_area": "minutes",
      "suggested_adjustment": {
        "kind": "expected_minutes_delta",
        "value": -30.0
      },
      "adjustment_basis": "Why this delta follows from the evidence",
      "requires_decision": true,
      "decision_question": "Accept the proposed minutes adjustment?",
      "expiry": "timezone-aware ISO-8601 timestamp or null"
    }
  ]
}
```

Allowed `evidence_type` values:

- `injury`
- `suspension`
- `training`
- `manager_quote`
- `predicted_lineup`
- `tactical_role`
- `transfer`
- `other`

Allowed `source_tier` values:

- `official`
- `strong_reporting`
- `predicted_lineup`
- `rumour`

Allowed `model_area` values:

- `minutes`
- `role`
- `availability`
- `set_pieces`
- `fixture`
- `none`

If there is no material evidence, return an empty `evidence` array while retaining all
four root fields.
