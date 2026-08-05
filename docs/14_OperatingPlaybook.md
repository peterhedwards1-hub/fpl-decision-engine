# Operating playbook

The engine is a decision aid, not a promise of realised points. Its production objective is
to maximise expected points through repeated, auditable decisions while keeping unqualified
model experiments out of live advice.

## Before Gameweek 1

1. Collect a current exact pre-deadline official snapshot.
2. Validate the preseason team-strength model and generate the projection it selects:

   ```powershell
   fpl-history --database data/fpl.sqlite3 validate-preseason-strength 2026-27 `
     --horizon 8 --candidate-pool-size 8 `
     --output data/models/preseason-strength-validation-2026-27.json `
     --comparison-output data/models/preseason-squad-comparison-2026-27.json `
     --markdown-output data/models/preseason-strength-validation-2026-27.md
   ```

   This is step two, not an optional extra. Before GW1 the incumbent team-strength path has
   no target-season fixtures to read, so every club sits on the same league average and an
   opening squad built on it cannot tell a trip to the champions from a home game against a
   promoted side. See `15_PreseasonTeamStrength.md`.
3. Run the one-command readiness report:

   ```powershell
   fpl-history --database data/fpl.sqlite3 preseason-readiness 2026-27 `
     --horizon 8 --candidate-pool-size 8 `
     --output data/models/preseason-readiness-2026-27.json
   ```

   At GW1 the readiness report prefers the validated preseason run when the validation
   artifact records a pass. Check `decision_context` — it must read
   `preseason_opening_squad`. If it reads `in_season_live_projection`, the flat model is
   still driving the squad and a blocker says so.
4. Review the primary squad, two structural alternatives, all eight lineup plans, bench
   availability and rerun triggers.
5. Complete the provisional/final team-news cycle. Freeze the final decision only after the
   last reliable press conferences, injuries, transfers and predicted roles have been
   reviewed.
6. Re-run `preseason-readiness`. Submit only when `ready_to_submit` is true and the named
   projection is the intended post-news incumbent run.

The report may apply an evidence-backed mean-appearance floor with
`--appearance-floor`. Do not choose that threshold from the current squad; qualify it with
the historical policy evaluator first.

## Every Gameweek

1. Refresh official data before the deadline.
2. Save the exact manager state: squad, purchase/selling prices, bank, free transfers and
   remaining chips.
3. Generate at least a five-Gameweek incumbent projection. The app extends this horizon when
   a nearby blank, double or chip expiry sits just beyond it.
4. Run the provisional team-news pass, review evidence, and generate the revised projection.
5. Compare the legal XI, bench order, captain/vice pair, roll and multi-transfer routes.
6. Treat chip timing as provisional whenever the projection does not reach that chip set's
   expiry.
7. Freeze the final recommendation, record the action actually taken, and later score the
   decision after the Gameweek finishes.

## Model policy

- `rates-rules-corrected-v4` remains the production incumbent for every in-season decision.
- `rates-rules-corrected-v4-preseason-carry-forward` drives the GW1 opening-squad decision
  and nothing else, and only while the validation artifact records a gate pass. It is
  deliberately outside the incumbent family the in-season selector accepts, so the generic
  newest-qualified-run rule can never pick it up in October.
- The learned playing-time, coherent scoring and defensive-contribution variants are forward
  candidates. Historical design evidence is useful, but none drives live advice until its
  immutable 2026/27 gates pass.
- Never select the newest projection merely because it is newest. Production surfaces require
  an allowed incumbent/review suffix and enough horizon.
- Reconstructed historical availability cannot reproduce exact pre-deadline injury knowledge.
  Prospective capture is therefore mandatory and irrecoverable once a deadline passes.

## Periodic evidence jobs

The expensive jobs are intentionally suitable for unattended or overnight execution:

```powershell
fpl-history --database data/fpl.sqlite3 compile-squad-policy-evaluation `
  355 356 357 358 359 --origins 2 14 26 --candidate-pool-size 2 `
  --appearance-floors 0.6 --output data/models/squad-policy-evaluation-v1.json

fpl-history --database data/fpl.sqlite3 compile-transfer-policy-evaluation `
  355 356 357 358 359 --max-transfers-per-week 2 `
  --output data/models/transfer-policy-evaluation-v1.json
```

Re-run these only when the declared policy or incumbent changes. Do not tune thresholds on
the live 2026/27 outcomes and then describe those same outcomes as forward validation.
The transfer job is a qualification audit, not a promise that it will emit a usable policy:
an estimate concentrated at the search cap is rejected, and only a report with
`qualified: true` is loaded by the app. The current hindsight design fails this gate, so
saved-transfer value must be learned from prospective actions rather than invented.

## What remains uncertain

- Realised FPL points have high variance; a sound decision can lose in a single season.
- Independent appearance states are still used by the exact autosub calculation until the
  joint simulator is qualified.
- Terminal value is explicitly zero unless supplied and historically qualified. The rolling
  horizon and saved-transfer option value reduce, but do not eliminate, horizon-edge risk.
- Wildcard and Free Hit season-level timing are not historically solved by the local scoring-
  chip replay. Their app values are planning-horizon comparisons and should be reviewed as
  such.
