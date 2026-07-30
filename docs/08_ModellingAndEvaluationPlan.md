# Modelling and evaluation plan (revised)

**Status:** Proposed
**Version:** 1.0
**Date:** 2026-07-30
**Relates to:** `06_StrongestModelRoute.md` (implemented state), `07_FootballAssumptionAudit.md` (current audit), `00_ProjectSpecification.md` §4, §11–13, §17

---

## 1. Purpose and accepted thesis

This plan supersedes the proposed analysis roadmap for improving forecast and decision
quality. It accepts that roadmap's central thesis, which the project's own records
already support:

> Invest in problem decomposition and evaluation infrastructure, not in a more powerful
> model trained directly on total FPL points.

The supporting evidence is in `06_StrongestModelRoute.md`: a 100-trial robust rolling
study improved top-100 MAE by 0.0025 while worsening top-100 absolute bias by 0.0991,
and the first learned total-points challenger reduced MAE only by underpredicting. Both
outcomes indicate that further hyperparameter search on an aggregate points target has
stopped tracking decision quality.

### What this revision changes

Four material changes from the proposed roadmap:

1. **A Stage 0 units-and-assumptions repair is inserted before the diagnostic work.**
   Oracle decomposition attributes error to subsystems; a unit error inside a subsystem
   is misread as evidence that the subsystem needs a learned model. Section 3 documents a
   live instance.
2. **The oracle decomposition runs at horizon 1 and horizon 5.** The entire evaluation
   stack is currently horizon 1, while transfers, chips and terminal value consume 5–8
   Gameweek sums. Error attribution is expected to reorder between the two.
3. **Regret-based promotion gates are sequenced behind decision-layer repair**, with
   captain regret as the primary decision gate because it is nearly optimiser-free.
4. **Scope is cut and refocused.** Learning-to-rank, NGBoost and conformal prediction are
   removed from the near term. Outcome correlation is promoted from a sub-bullet to a
   first-class objective.

---

## 2. Evidence budget — the binding constraint

Every sequencing decision below follows from how much independent evidence exists. This
is a smaller number than the row count suggests.

| Resource | Reality |
|---|---|
| Player-fixture rows | 138,707 across five seasons (2021/22–2025/26) |
| Usable development folds | Three (2021/22 → 2022/23 → 2023/24, expanding) |
| 2024/25 | Locked validation, already inspected by the robust rolling study |
| 2025/26 | GW26–38 already consumed as the v2 held-out window |
| Defensive contributions | Rule exists only from 2025/26 — **one season** |
| Pre-deadline team news | Absent historically; obtainable prospectively only |
| Bookmaker odds | Not obtainable within the free-and-permitted source policy |
| Genuinely forward-untouched data | Effectively none until 2026/27 begins |

**Consequence:** the number of model-selection decisions this data can support is small.
Prefer a few strong, pre-declared challengers over broad searches. Every additional
tuning surface consumes evidence that cannot be replaced.

---

## 3. Stage 0 — repair units and assumptions (prerequisite, days not weeks)

### 3.1 Why this comes first

Oracle decomposition answers "which subsystem limits accuracy". If a subsystem contains a
dimensional error, the analysis will report that subsystem as the bottleneck and Stage 2
or 3 will build a learned model to fit around a broken constant.

### 3.2 Defensive contributions — measured over-correction

The v4 `corrected_scoring` change replaced the earlier linear-count bug with a
threshold-Poisson, which is the correct *shape*. The priors were not rescaled, and the
resulting component is now near zero:

| Defender (true rate ≈ 7.0 DC per 90) | v4 DC points per match |
|---|---:|
| Established centre-back, 2,800 min | **0.007** |
| Squad centre-back, 900 min | 0.000 |
| New signing, 270 min | 0.000 |
| Realistic value | **≈ 0.70** |

Two compounding causes:

1. **Prior units.** `POSITION_PRIORS[DEF]["defensive_contributions"] = 0.30` is expressed
   in threshold-hit-*probability* units while the data it shrinks toward is a *count*
   (≈7 per 90) — roughly 23× off. With `player_rate_prior_minutes = 1776.7`, shrinkage
   drags a true 7.0/90 to 4.40 for an established player and 1.18 for a new signing.
2. **Distributional tail.** Even with a correctly scaled prior, Poisson λ=5.6 gives
   P(X ≥ 10) = 0.059 → 0.12 points, against a real defender hit-rate nearer 35%. CBIT
   counts are strongly overdispersed because they are game-state dependent — a team
   protecting a lead accumulates clearances — so a Poisson tail at threshold 10 is far
   too thin.

Net effect: the component moved from roughly **+4.40 points per match too high** to
**≈0.7 too low**, and now systematically undervalues defenders — the players the 2025/26
rule was introduced to promote.

**This would pass the current audit gate.** The gate in `07_FootballAssumptionAudit.md`
tests overall MAE, top-100 MAE, top-100 absolute bias and captain regret. Defenders
rarely appear in the top-100 by projected points and are never captains, so a
defender-only regression is invisible to it. See §8.2.

### 3.3 Stage 0 work items

| # | Item | Rationale |
|---|---|---|
| 0.1 | Rescale DC priors to count-per-90 units, measured by position from 2025/26 | Prior and data must share units before shrinkage is meaningful |
| 0.2 | Replace the Poisson threshold tail with negative binomial, or an empirical position hit-rate | Poisson understates an overdispersed tail at threshold 10/12 |
| 0.3 | Re-tune v3/v4 parameters after 0.1–0.2 | `player_rate_prior_minutes = 1776.7` was selected partly to suppress the original DC over-projection; it is fitted to a bug |
| 0.4 | Add per-Gameweek availability decay across the horizon | `chance_of_playing_next_round` is applied flat to all 5–8 Gameweeks, so a 25% player is projected at 25% for the whole horizon |
| 0.5 | Use xG/xA in the rate estimates | `expected_goals`/`expected_assists` are collected and stored but unused in `projections.py` |
| 0.6 | Stop combining horizon uncertainty as root-sum-of-squares, or mark it explicitly as a known-wrong placeholder | Dominant uncertainties (is he a starter, is he good) persist week to week; RSS understates horizon uncertainty |
| 0.7 | Drive appearance points from `scoring.appearance_under_60` / `appearance_60_or_more` | Currently hard-coded as `P(app) + P(60)`, correct only because config happens to be 1 and 2 |
| 0.8 | Replace the GK DC sentinel `1000000` with an absent/null threshold | Magic number encoding "ineligible" |

Already closed and requiring no further work: the £100.0m squad-value crash
(`check_budget=False`), the goals-conceded 60-minute gate, E[goals]/2 → E[floor(X/2)],
and persistence of `appearance_probability` / `sixty_probability` through to the optimiser.

### 3.4 Exit criteria

- Every scoring component's units are asserted by test against `calculate_player_points`.
- Position-sliced calibration exists and is recorded (becomes a gate in §8.2).
- v3/v4 re-tuned and re-reported on the same folds, with the DC change isolated.

---

## 4. Stage 1 — diagnose the ceiling

### 4.1 Oracle decomposition

Counterfactual backtests substituting actual values for predicted, one subsystem at a
time, to bound the improvement available from each:

1. actual **appearance** + predicted everything else;
2. actual **minutes given appearance** + predicted everything else;
3. actual **team goals** + predicted player allocation;
4. actual **player scoring rates** + predicted minutes and team goals;
5. actual minutes *and* actual team goals + predicted allocation (residual ceiling).

Two refinements over the proposed version:

- **Split appearance from minutes-given-appearance** (1 and 2 above). They have different
  remedies — news pipeline versus rotation modelling — and lumping them hides which to
  fund.
- **Run every counterfactual at horizon 1 and horizon 5.** Attribution is expected to
  reorder: at h=1 appearance and news dominate; at h=5 availability is largely
  unknowable and team/fixture modelling dominates. Investing on h=1 evidence alone risks
  funding the minutes model when horizon decisions hinge on team strength.

### 4.2 Paired block bootstrap

Resample **whole Gameweeks** (or season–Gameweek blocks), not player rows: players within
a Gameweek share fixtures and conditions. For every model comparison report mean paired
difference, median paired difference, 80% and 95% intervals, percentage of Gameweeks won,
worst-season difference, and per-position differences.

This makes claims like "0.0025 top-100 MAE better" self-evidently non-decision-worthy.

### 4.3 Residual slicing

Automatic error tables by position; predicted-minutes band; starter vs substitute; DNP vs
played; home/away; fixture difficulty; price band; early/mid/late season; promoted vs
established club; new-club status; single vs double Gameweek; and top-15/50/100/all.

Purpose: detect an aggregate gain that conceals a damaging subgroup regression — exactly
the DC case in §3.2.

### 4.4 Calibration tables

Reliability by probability bin for appearance and 60-minute probabilities. For points:
predicted-decile versus realised mean, by position, and **by forecast horizon**.

### 4.5 Baselines

Implement the `00_ProjectSpecification.md` §17.4 baselines, none of which currently exist:
no transfers; highest recent FPL points; highest official form; highest simple expected
points; captain the highest projection; always avoid hits; simple fixture ticker.

Without these, the specification's rule — no complexity without out-of-sample improvement
over simpler alternatives — has no machinery behind it.

### 4.6 Exit criteria

A ranked, quantified ceiling per subsystem at both horizons, with bootstrap intervals,
plus baseline and calibration tables in model health.

---

## 5. Stage 2 — learned playing time (hurdle model)

Treat playing time as related questions rather than one regression:

1. will the player appear;
2. will they start;
3. conditional on appearing, will they reach 60 minutes;
4. conditional on appearing, how many minutes.

**Challengers to compare:** regularised logistic regression as the interpretable
baseline; histogram gradient boosting; CatBoost where categorical features (club,
position, role archetype) earn their place; a hierarchical/mixed-effects logistic model
as the statistical challenger.

**Hierarchical partial pooling** applies here first, with pooling levels league →
position → club → player. This is the principled fix for the pattern §3.2 exposed: a
single global shrinkage weight cannot serve both a slow-moving skill signal and a
fast-moving usage signal. Empirical-Bayes shrinkage is an acceptable first version; a
full Bayesian implementation is not required to start.

**Calibration is a deliverable, not a diagnostic.** Fit calibrators on chronological
out-of-fold predictions strictly independent of the underlying model's training rows.
Record Brier score, log loss and reliability diagrams alongside accuracy, and report the
downstream points-level effect — a better-calibrated appearance probability only matters
if it moves expected points and captain regret.

**Temporal weighting:** replace the fixed recent-Gameweek window with exponentially
decayed evidence, with a **separate decay rate per signal** (appearance and starts decay
fast; scoring skill slowly; team strength between; news fastest). Add explicit
change-point features: new club, returned from injury, new manager, first start after a
run of substitute appearances, position or role change.

---

## 6. Stage 3 — learned scoring components

Predict underlying events and apply the season's configured scoring rules, so rule
changes never require retraining the football models.

**Order within the stage:**

1. **xG/xA first.** Already collected, already stored, currently unused — the cheapest
   accuracy available. Actual goals over roughly six effective matches is far noisier.
2. **Team and fixture model.** Estimate expected home and away goals, clean-sheet
   probability, and attacking/defensive strength from xG rather than realised goals, with
   opponent-strength adjustment, promoted-team priors, learned form decay, rest days and
   congestion, and managerial change.
   This also fixes a current double-count: a player's historical per-90 rate already
   embeds their team's attacking strength, so multiplying by `team_strength["attack"]`
   counts it twice and biases players at strong clubs upward. Replace with
   *share-of-team-output* allocation — estimate the player's share, then multiply by the
   team's expected goals for that fixture.
3. **Remaining components:** saves, bonus, cards, penalty events.
4. **Defensive contributions — calibration, not learning.** With one season of data
   (§2), per-player DC rates cannot be validated across folds. Model DC from
   position-level threshold hit-rates measured on 2025/26 and treat it as a calibration
   problem. Attempting to learn per-player DC rates on one season will overfit invisibly.

**Count families:** Poisson as the default; negative binomial where variance exceeds the
mean (DC counts, saves); hurdle or zero-inflated forms for sparse events. Judge sparse
outcomes with proper probabilistic scores and calibration, not event-count MAE.

**Coherence constraints to enforce and test:**

- player goal expectations sum sensibly toward team expected goals;
- clean-sheet probability agrees across defenders from the same club;
- opposing teams' scoring and clean-sheet predictions are mutually consistent.

---

## 7. Stage 4 — joint distributions and correlation

**Promoted to a first-class objective.** Correlation is the one thing no amount of
per-player modelling can supply, and it is what every remaining decision needs: Bench
Boost, Triple Captain, autosubs, bench valuation, transfer downside, and "probability
squad A beats squad B".

There is a concrete existing inconsistency to fix: defenders from the same club derive
clean-sheet probability from the same λ, so their outcomes are near-perfectly correlated
in reality, yet `_expected_weekly_score` multiplies appearance probabilities and treats
players as independent.

**Approach — Monte Carlo with shared team-level latent factors:**

1. simulate team scorelines from the team model (shared factor per club per fixture);
2. simulate appearance and minutes per player from the Stage 2 hurdle model;
3. allocate goals, assists and defensive events conditional on the team scoreline;
4. apply the configured FPL scoring rules;
5. resolve autosubs and captain fallback;
6. score complete squad outcomes.

This yields calibrated joint uncertainty, correct bench and captaincy risk, comparable
chip values, and squad-versus-squad probabilities — replacing the current heuristic
`1.25 + 3.5/√(matches+1) + 0.12·offset` uncertainty and the RSS horizon combination.

**Constrained ensemble (kept, deliberately small):** strictly chronological out-of-fold
predictions from each component model, blended by non-negative least squares with weights
summing to one. Split weights by position or season phase only where the bootstrap
supports it. This must not become another large tuning exercise; a simple blend makes it
obvious which models contribute value.

---

## 8. Stage 5 — promotion gates

### 8.1 Two-tier gate

A challenger is promoted only if it passes both tiers, reported with bootstrap intervals.

**Forecast tier**
- top-100 and top-15 points MAE;
- top-100 absolute bias;
- appearance and 60-minute calibration (Brier, log loss, reliability);
- points calibration by predicted decile;
- calibration by forecast horizon (h=1 and h=5);
- minutes MAE, reported but never decisive on its own.

**Decision tier**
- captain regret — **primary**, because captaincy involves no budget, formation, club or
  transfer logic and therefore measures the forecast rather than the optimiser;
- legal-XI regret;
- squad and transfer regret — **added to the gate only after §9 completes**.

### 8.2 Position-sliced calibration is a gate, not a diagnostic

Added in response to §3.2. No challenger is promoted if it materially worsens calibration
for any position, regardless of aggregate improvement. The current gate would have passed
a defender-only regression of roughly 0.7 points per match.

### 8.3 Held-out discipline

2024/25 and 2025/26 GW26–38 are already inspected and must not be reused for model
selection. Final qualification uses genuinely forward 2026/27 results. Model selection on
development folds, qualification forward.

---

## 9. Parallel track A — decision-layer repair

The Stage 5 decision tier cannot be trusted until these are fixed, because transfer and
squad regret currently score the optimiser rather than the forecast.

| Item | Current behaviour |
|---|---|
| Transfer gain measured across all 15 players | Bench depth overvalued, XI upgrades undervalued; `route_score` should weight starters |
| Free-transfer flexibility now `0.0` | The arbitrary `next_free * 1.0` was correctly removed, but zero is the opposite bias — a saved transfer is worth nothing, so the optimiser always prefers to spend. Needs a state-dependent, empirically derived value (and the cap of 5 makes it state-dependent by construction) |
| Chip values not comparable | Wildcard returns a horizon total, Free Hit a one-Gameweek total, Bench Boost a bench sum, Triple Captain a true increment — ranking on `expected_incremental_points` always plays a wildcard |
| Bench Boost overstates | True increment is E[all 15] − E[XI with autosubs]; `expected_bench_contribution` already computes the adjustment |
| Triple Captain has no opportunity cost | Specification §14.4 requires comparison against plausible future opportunities |
| `recommend_chip` unreachable | `chips.py` is not called from the UI or reports |
| Terminal value is the horizon's last Gameweek | Already inside the horizon sum, so it double-counts at 1.10×, and carries no information about squad quality *after* the horizon, money, retained transfers or flexibility |
| MILP objective is a surrogate | `0.15·robust_horizon + 0.85·GW starters + captain + 0.05·vice` with undocumented weights, while a different quantity is reported |

Stage 4's joint simulation supplies the correct inputs for the chip and bench items, so
this track should land alongside it.

---

## 10. Parallel track B — prospective capture (starts immediately)

Independent of modelling progress, and unrecoverable if deferred. `06_StrongestModelRoute.md`
already states the constraint: historical reconstructed data cannot measure news uplift
because exact pre-deadline news is absent.

- paired pre-news and post-news projection runs every Gameweek of 2026/27;
- timestamped pre-deadline squad, price and availability snapshots;
- actual action taken, and the reason when it differed from the recommendation;
- realised outcomes for forecast and decision scoring.

Every week not captured is a week of validation evidence that cannot be reconstructed.
News-adjustment calibration should be learned only once enough timestamped examples exist.

---

## 11. Explicitly deprioritised

| Not now | Reason |
|---|---|
| Deep neural networks, transformers | 138,707 tabular rows across three usable folds; adds tuning freedom and removes interpretability without addressing the structural problems identified |
| Reinforcement learning for transfers/chips | No realistic season simulator and far too few independent seasons; exact optimisation over better forecasts is the stronger route |
| A larger direct total-points booster | The family has been tested sufficiently; more search will not fix a target-definition problem |
| Clustering as the main predictor | Archetype clustering may improve priors, but should feed the component models rather than replace them |
| Learning-to-rank (LambdaMART) | The optimiser needs calibrated expected-value *differences*, not an ordering; the proposed roadmap already concedes a ranker cannot replace calibrated points, so it adds a tuning surface for an unusable quantity |
| NGBoost | Monte Carlo from the Stage 3 components gives the same distributional output plus the joint structure NGBoost cannot provide |
| Conformal prediction | Per-player intervals are not the binding constraint; joint squad distributions are, and conformal does not supply them |

---

## 12. Recommended order

| Stage | Content | Gate to proceed |
|---|---|---|
| 0 | Units and assumptions repair; re-tune | Component units asserted by test; position calibration recorded |
| 1 | Oracle decomposition at h=1 and h=5; bootstrap; residual slices; calibration; baselines | Ranked, quantified subsystem ceilings with intervals |
| 2 | Hurdle playing-time model; hierarchical pooling; learned decay | Beats v3/v4 on identical rolling folds, forecast tier |
| 3 | xG/xA; team and fixture model; remaining components; DC by calibration | Coherence constraints hold; forecast tier improves |
| 4 | Joint Monte Carlo with shared team factors; constrained ensemble | Calibrated joint uncertainty; comparable chip values |
| 5 | Forward qualification on 2026/27 | Both gate tiers, no position regression |
| A | Decision-layer repair | Required before squad/transfer regret joins the gate |
| B | Prospective capture | Starts now, runs continuously |

---

## 13. Risks and reconsideration triggers

Per `00_ProjectSpecification.md` §23, each decision here should be revisited if:

| Trigger | Response |
|---|---|
| Stage 1 shows the appearance/minutes ceiling is small at both horizons | Redirect Stage 2 effort into the team and fixture model instead |
| Stage 1 attribution differs sharply between h=1 and h=5 | Split the model into horizon-specific configurations rather than one shared parameter set |
| Bootstrap intervals on a challenger's gain straddle zero | Do not promote, regardless of point-estimate improvement |
| Position-sliced calibration regresses while aggregates improve | Treat as a failed gate, not a trade-off |
| 2026/27 news capture shows a large forecast uplift | Raise the priority of the news pipeline over further base-model work |
| A free, permitted xG source materially exceeds the official fields | Revisit the source registry; the official FPL fields are the current baseline by policy, not by measurement |

## 14. What this plan does not claim

- It does not claim five-season evaluation. Three development folds are usable; 2024/25
  and 2025/26 GW26–38 are burned; see §2.
- It does not claim defensive contributions can be validated across seasons. One season
  of the rule exists.
- It does not claim any historical measurement of news uplift is possible.
- It does not assume the transparent incumbent is the final model, nor that a learned
  challenger will replace it. Both remain candidates under the §8 gates.
