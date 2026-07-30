# Modelling and evaluation plan (revised)

**Status:** Proposed
**Version:** 1.1
**Date:** 2026-07-30
**Relates to:** `06_StrongestModelRoute.md` (implemented state), `07_FootballAssumptionAudit.md` (assumption audit), `08_ModelEvaluation.md` (multi-horizon evaluation), `00_ProjectSpecification.md` §4, §11–13, §17

**Changes in 1.1:** revised in response to the corrected-v4 multi-horizon evaluation. MAE
demoted in the forecast gate (§8.1); paired block bootstrap promoted to the top remaining
diagnostic (§4.1); availability decay and xG/xA moved out of Stage 0 (§3.4); Stage 1
marked partially complete with measured results (§4); document renumbered from 08 to
avoid collision with `08_ModelEvaluation.md`.

---

## 1. Purpose and accepted thesis

This plan sets the route to a stronger forecast and decision model. It accepts the thesis
that the project's own records support:

> Invest in problem decomposition and evaluation infrastructure, not in a more powerful
> model trained directly on total FPL points.

The evidence has now accumulated from three independent directions:

1. A 100-trial robust rolling study improved top-100 MAE by 0.0025 while worsening
   top-100 absolute bias by 0.0991 (`06_StrongestModelRoute.md`).
2. The first learned total-points challenger reduced MAE only by underpredicting.
3. An expanding season-average baseline now beats the incumbent on MAE in **all five
   seasons** (`08_ModelEvaluation.md`).

Point 3 is the strongest confirmation yet that aggregate points MAE has stopped tracking
decision quality — and §8.1 explains why it was always the wrong gate.

---

## 2. Evidence budget — the binding constraint

Every sequencing decision below follows from how much independent evidence exists. This
is a smaller number than the row count suggests, and it shrank further when the
multi-horizon evaluation queried the previously locked validation season.

| Resource | Reality |
|---|---|
| Player-fixture rows | 138,707 across five seasons (2021/22–2025/26) |
| Multi-horizon evaluation samples | ~103,000 per horizon step, origins GW2–GW31 |
| Usable development folds | Three (2021/22 → 2022/23 → 2023/24, expanding) |
| 2024/25 | Was locked validation; **now inspected** by the horizon evaluation (run 358) |
| 2025/26 | GW26–38 consumed as the v2 held-out window; also inspected as audit season (run 359) |
| Defensive contributions | Rule exists only from 2025/26 — **one season** |
| xG/xA coverage | Complete from 2022/23; **absent in 2021/22** |
| Pre-deadline team news | Absent historically; obtainable prospectively only |
| Pre-deadline availability timing | All snapshots are `historical_reconstruction` with unknown timing — see §3.4 |
| Bookmaker odds | Not obtainable within the free-and-permitted source policy |
| Genuinely forward-untouched data | **None.** All five seasons have now been queried |

**Consequences:**

- Every remaining promotion decision must be qualified on genuinely forward 2026/27
  results. There is no untouched historical holdout left.
- The number of model-selection decisions the historical data can support is small.
  Prefer few pre-declared challengers over broad searches.
- Development-fold results are now for *design* evidence only, never promotion.

---

## 3. Stage 0 — repair units and assumptions (critical path)

### 3.1 Why this comes first

Oracle decomposition (§4.2) attributes error to subsystems. A dimensional error inside a
subsystem is misread as evidence that the subsystem needs a learned model, and the next
stage then builds one to fit around a broken constant.

### 3.2 Defensive contributions — measured over-correction, still open

Verified unchanged as of the evaluation-phase commit. The v4 `corrected_scoring` change
replaced the earlier linear-count bug with a threshold-Poisson, which is the correct
*shape*. The priors were never rescaled, so the component is now near zero:

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
   protecting a lead accumulates clearances — so a Poisson tail at threshold 10/12 is far
   too thin.

Net effect: the component moved from roughly **+4.40 points per match too high** to
**≈0.7 too low**, and now systematically undervalues defenders — the players the 2025/26
rule was introduced to promote.

**Two reasons this is now the critical path:**

- **It would pass the current audit gate.** The gate in `07_FootballAssumptionAudit.md`
  tests overall MAE, top-100 MAE, top-100 absolute bias and captain regret. Defenders
  rarely appear in the top-100 by projected points and are never captains, so a
  defender-only regression is invisible to it. See §8.3.
- **It confounds the newest results.** 2025/26 is the only season carrying the
  defensive-contribution rule, and it is also the season where the incumbent looks worst
  against the baseline on both regret measures (captain 13.18 vs 11.40; top-15 122.96 vs
  118.90). No causal claim is made — captains and top-15 picks are rarely defenders — but
  that season must be re-run after the units fix before its results are treated as
  evidence that the regret advantage is unstable.

### 3.3 Stage 0 work items

| # | Item | Rationale |
|---|---|---|
| 0.1 | Rescale DC priors to count-per-90 units, measured by position from 2025/26 | Prior and data must share units before shrinkage is meaningful |
| 0.2 | Replace the Poisson threshold tail with negative binomial, or an empirical position hit-rate | Poisson understates an overdispersed tail at threshold 10/12 |
| 0.3 | Re-tune v3/v4 parameters after 0.1–0.2, and re-run the 2025/26 evaluation | `player_rate_prior_minutes = 1776.7` was selected partly to suppress the original DC over-projection; it is fitted to a bug |
| 0.4 | Stop combining horizon uncertainty as root-sum-of-squares, or mark it explicitly as a known-wrong placeholder | Dominant uncertainties (is he a starter, is he good) persist week to week; RSS understates horizon uncertainty |
| 0.5 | Drive appearance points from `scoring.appearance_under_60` / `appearance_60_or_more` | Currently hard-coded as `P(app) + P(60)`, correct only because config happens to be 1 and 2 |
| 0.6 | Replace the GK DC sentinel `1000000` with an absent/null threshold | Magic number encoding "ineligible" |

Already closed and requiring no further work: the £100.0m squad-value crash
(`check_budget=False`), the goals-conceded 60-minute gate, E[goals]/2 → E[floor(X/2)],
and persistence of `appearance_probability` / `sixty_probability` through to the optimiser.

### 3.4 Items moved out of Stage 0 in version 1.1

| Was | Now | Reason |
|---|---|---|
| Availability decay across the horizon | **Track B (§10)** | The availability gate in `08_ModelEvaluation.md` establishes that all historical snapshots are `historical_reconstruction` with unknown timing, so a recovery model cannot be honestly backtested. It ships either as an explicitly unvalidatable structural assumption or after live 2026/27 capture |
| Use xG/xA in the rate estimates | **Stage 3 (§6.1)** | Framed in 1.0 as a cheap repair. The `expected_with_actual_fallback` challenger shows naive substitution improves MAE but fails the predeclared bias and decision gates. It is a design task, not a fix |

### 3.5 Exit criteria

- Every scoring component's units are asserted by test against `calculate_player_points`.
- Position-sliced calibration exists and is recorded (becomes a gate in §8.3).
- v3/v4 re-tuned, and the 2025/26 season re-evaluated with the DC change isolated.

---

## 4. Stage 1 — diagnose the ceiling (partially complete)

### 4.0 Already delivered

The evaluation phase closed three of this stage's five items.

**Multi-horizon evaluation** — delivered beyond specification. Version 1.0 asked for h=1
and h=5; the implementation covers h=1 to h=8 across all five seasons, origins GW2–GW31.
It confirms monotonic degradation and drift toward optimism:

| Horizon | Samples | Points MAE | Points bias |
|---|---:|---:|---:|
| GW+1 | 103,057 | 1.2117 | −0.0078 |
| GW+8 | 102,718 | 1.2776 | −0.0227 |

This validates keeping horizon steps separate in model health and decision reports.

**Baselines** — the `00_ProjectSpecification.md` §17.4 gap is closed: expanding
season-average points per fixture, recent-four points per fixture, season points per 90
using model minutes, and a position average.

**Legal squad regret** — `evaluate-squad-regret` replays each origin as an exact £100m
persistent-squad, legal-XI and captain problem. This is ahead of the 1.0 schedule, which
gated squad regret behind decision-layer repair.

### 4.1 Paired block bootstrap — now the top remaining item

Promoted to first in version 1.1. The incumbent's entire justification now rests on
regret, and that evidence is thin: against the season-average baseline it wins captain
regret in **2 of 5** seasons and top-15 regret in **3 of 5**, with margins of roughly 0.4
and 2 points. With five seasons and differences that small, signal cannot be separated
from noise, so this determines whether there is any measured justification for the model
over a season average at all.

Resample **whole Gameweeks** (or season–Gameweek blocks), never player rows: players
within a Gameweek share fixtures and conditions. For every comparison report mean paired
difference, median paired difference, 80% and 95% intervals, percentage of Gameweeks won,
worst-season difference, and per-position differences.

### 4.2 Oracle decomposition

Counterfactual backtests substituting actual values for predicted, one subsystem at a
time, to bound the improvement available from each:

1. actual **appearance** + predicted everything else;
2. actual **minutes given appearance** + predicted everything else;
3. actual **team goals** + predicted player allocation;
4. actual **player scoring rates** + predicted minutes and team goals;
5. actual minutes *and* actual team goals + predicted allocation (residual ceiling).

Appearance is kept separate from minutes-given-appearance (1 and 2) because the remedies
differ — news pipeline versus rotation modelling — and lumping them hides which to fund.
Run every counterfactual at h=1 and h=8, reusing the horizon harness that now exists.

### 4.3 Residual slicing

Automatic error tables by position; predicted-minutes band; starter vs substitute; DNP vs
played; home/away; fixture difficulty; price band; early/mid/late season; promoted vs
established club; new-club status; single vs double Gameweek; and top-15/50/100/all.

Purpose: detect an aggregate gain that conceals a damaging subgroup regression — exactly
the DC case in §3.2.

### 4.4 Calibration tables

Reliability by probability bin for appearance and 60-minute probabilities, with Brier
score and log loss. For points: predicted-decile versus realised mean, by position, and by
forecast horizon.

### 4.5 Exit criteria

Bootstrap intervals on every headline comparison already reported, plus a ranked,
quantified subsystem ceiling at both horizons and calibration tables in model health.

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
downstream effect on RMSE and captain regret — better-calibrated appearance probability
only matters if it moves decisions.

**Temporal weighting:** replace the fixed recent-Gameweek window with exponentially
decayed evidence, with a **separate decay rate per signal** (appearance and starts decay
fast; scoring skill slowly; team strength between; news fastest). Add explicit
change-point features: new club, returned from injury, new manager, first start after a
run of substitute appearances, position or role change.

---

## 6. Stage 3 — learned scoring components

Predict underlying events and apply the season's configured scoring rules, so rule
changes never require retraining the football models.

### 6.1 Expected events — share of team output, not substitution

Now the first item in this stage, promoted from Stage 0 with a corrected design. The
`expected_with_actual_fallback` challenger substituted xG/xA directly into the existing
player-rate model. Result: overall and top-100 MAE improved, but top-100 bias worsened by
+0.12 to +0.14 and the decision gates failed. The locked season was correctly not queried.

The failure has a structural cause the plan predicted: **a player's historical per-90 rate
already embeds their team's attacking strength**, so multiplying by
`team_strength["attack"]` counts it twice and biases players at strong clubs upward.
Swapping the input does not remove the double-count.

The correct design, which `08_ModelEvaluation.md` independently reaches:

1. estimate the player's **share** of team xG and xA;
2. estimate **opponent-adjusted team xG** for the fixture;
3. multiply share by team expectation.

Note that xG/xA are absent in 2021/22, so this challenger has two development folds, not
three.

### 6.2 Team and fixture model

Estimate expected home and away goals, clean-sheet probability, and attacking/defensive
strength from xG rather than realised goals, with opponent-strength adjustment,
promoted-team priors, learned form decay, rest days and congestion, and managerial change.
This subsystem supplies the team expectation that §6.1 requires.

### 6.3 Remaining components

Saves, bonus, cards and penalty events.

### 6.4 Defensive contributions — calibration, not learning

With one season of the rule (§2), per-player DC rates cannot be validated across folds.
Model DC from position-level threshold hit-rates measured on 2025/26 and treat it as a
calibration problem. Attempting to learn per-player DC rates on one season will overfit
invisibly.

### 6.5 Count families and coherence

Poisson as the default; negative binomial where variance exceeds the mean (DC counts,
saves); hurdle or zero-inflated forms for sparse events. Judge sparse outcomes with proper
probabilistic scores and calibration, not event-count MAE.

Coherence constraints to enforce and test:

- player goal expectations sum sensibly toward team expected goals;
- clean-sheet probability agrees across defenders from the same club;
- opposing teams' scoring and clean-sheet predictions are mutually consistent.

---

## 7. Stage 4 — joint distributions and correlation

**A first-class objective.** Correlation is the one thing no amount of per-player
modelling can supply, and it is what every remaining decision needs: Bench Boost, Triple
Captain, autosubs, bench valuation, transfer downside, and "probability squad A beats
squad B".

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

**Constrained ensemble (deliberately small):** strictly chronological out-of-fold
predictions from each component model, blended by non-negative least squares with weights
summing to one. Split weights by position or season phase only where the bootstrap
supports it. This must not become another large tuning exercise; a simple blend makes it
obvious which models contribute value.

---

## 8. Stage 5 — promotion gates

### 8.1 Why MAE is not the primary forecast gate

Revised in version 1.1, prompted by the season-average baseline beating the incumbent on
MAE in all five seasons.

FPL points are heavily zero-inflated and right-skewed, and **MAE is minimised by the
conditional median, not the conditional mean.** A smooth low-variance baseline sits near
that median and wins on MAE. A model that correctly targets the conditional mean must
project higher for haul-capable players and pays MAE on every week they blank.

So the baseline's MAE advantage is substantially a **metric artefact**, not evidence that
the model is worse for decisions. It is the same median-versus-mean trap that killed the
`absolute_error` learned challenger, now appearing in the baseline comparison rather than
in a challenger. The optimiser requires calibrated expected values; a median-optimal
forecast is the wrong object regardless of how well it scores.

MAE is therefore **reported but never decisive** — the status already assigned to minutes
MAE.

This does not excuse the incumbent. It means the open question is whether the model beats
the baseline on mean-appropriate scores and on decisions, which §4.1 must answer with
intervals before either conclusion is drawn.

### 8.2 Two-tier gate

A challenger is promoted only if it passes both tiers, reported with bootstrap intervals
(§4.1).

**Forecast tier — in priority order**

1. **RMSE**, overall and top-100 — minimised by the conditional mean, already computed in
   `evaluation.py`;
2. points calibration by predicted decile, and by position;
3. bias by horizon step;
4. appearance and 60-minute calibration (Brier, log loss, reliability);
5. proper probabilistic scores once Stage 4 produces distributions;
6. MAE and minutes MAE — reported, never decisive.

**Decision tier**

- captain regret — **primary**, because captaincy involves no budget, formation, club or
  transfer logic and therefore measures the forecast rather than the optimiser;
- legal-XI and legal-squad regret — available now via `evaluate-squad-regret`;
- transfer regret — **added only after §9 completes**, since transfer continuity, hits and
  retained free transfers are not yet modelled in the replay.

### 8.3 Position-sliced calibration is a gate, not a diagnostic

No challenger is promoted if it materially worsens calibration for any position,
regardless of aggregate improvement. The current gate would have passed a defender-only
regression of roughly 0.7 points per match.

### 8.4 Held-out discipline

All five historical seasons have now been queried (§2). Development folds provide *design*
evidence only. **Promotion requires genuinely forward 2026/27 results.** No further
historical holdout exists to spend.

---

## 9. Parallel track A — decision-layer repair

The Stage 5 transfer-regret gate cannot be trusted until these are fixed, because it would
score the optimiser rather than the forecast.

| Item | Current behaviour |
|---|---|
| Transfer gain measured across all 15 players | Bench depth overvalued, XI upgrades undervalued; `route_score` should weight starters |
| Free-transfer flexibility now `0.0` | The arbitrary `next_free * 1.0` was correctly removed, but zero is the opposite bias — a saved transfer is worth nothing, so the optimiser always prefers to spend. Needs a state-dependent, empirically derived value (the cap of 5 makes it state-dependent by construction) |
| Chip values not comparable | Wildcard returns a horizon total, Free Hit a one-Gameweek total, Bench Boost a bench sum, Triple Captain a true increment — ranking on `expected_incremental_points` always plays a wildcard |
| Bench Boost overstates | True increment is E[all 15] − E[XI with autosubs]; `expected_bench_contribution` already computes the adjustment |
| Triple Captain has no opportunity cost | Specification §14.4 requires comparison against plausible future opportunities |
| `recommend_chip` unreachable | `chips.py` is not called from the UI or reports |
| Terminal value is the horizon's last Gameweek | Already inside the horizon sum, so it double-counts at 1.10×, and carries no information about squad quality *after* the horizon, money, retained transfers or flexibility |
| MILP objective is a surrogate | `0.15·robust_horizon + 0.85·GW starters + captain + 0.05·vice` with undocumented weights, while a different quantity is reported |
| Squad-regret replay lacks continuity | Hindsight selects a fresh perfect squad at every origin; transfer continuity, hits and bench autosubs are the stated later extensions |

Stage 4's joint simulation supplies the correct inputs for the chip and bench items, so
this track should land alongside it.

---

## 10. Parallel track B — prospective capture (starts immediately)

Independent of modelling progress, and unrecoverable if deferred. With no untouched
historical holdout remaining (§2), this track is now the **only** source of
promotion-grade evidence.

- paired pre-news and post-news projection runs every Gameweek of 2026/27;
- **timestamped pre-deadline availability**: status and chance-of-playing values, the
  subsequent appearance and minutes outcomes, and injury expiry or return estimates —
  enough multi-week cases to compare frozen, immediate-recovery and calibrated-decay
  assumptions (moved here from Stage 0 in version 1.1);
- timestamped pre-deadline squad and price snapshots;
- actual action taken, and the reason when it differed from the recommendation;
- realised outcomes for forecast and decision scoring.

Every week not captured is a week of validation evidence that cannot be reconstructed.
News-adjustment and availability-recovery calibration should be learned only once enough
timestamped examples exist.

---

## 11. Explicitly deprioritised

| Not now | Reason |
|---|---|
| Deep neural networks, transformers | 138,707 tabular rows across three usable folds; adds tuning freedom and removes interpretability without addressing the structural problems identified |
| Reinforcement learning for transfers/chips | No realistic season simulator and far too few independent seasons; exact optimisation over better forecasts is the stronger route |
| A larger direct total-points booster | The family has been tested sufficiently; more search will not fix a target-definition problem |
| Clustering as the main predictor | Archetype clustering may improve priors, but should feed the component models rather than replace them |
| Learning-to-rank (LambdaMART) | The optimiser needs calibrated expected-value *differences*, not an ordering; a ranker cannot replace calibrated points, so it adds a tuning surface for an unusable quantity |
| NGBoost | Monte Carlo from the Stage 3 components gives the same distributional output plus the joint structure NGBoost cannot provide |
| Conformal prediction | Per-player intervals are not the binding constraint; joint squad distributions are, and conformal does not supply them |

---

## 12. Recommended order

| Stage | Content | Status | Gate to proceed |
|---|---|---|---|
| 0 | DC units and tail; re-tune; re-run 2025/26; remaining unit items | **Critical path** | Component units asserted by test; position calibration recorded |
| 1 | Bootstrap intervals; oracle decomposition; residual slices; calibration | **Partially complete** — multi-horizon, baselines and squad regret delivered | Intervals on every headline comparison; ranked subsystem ceilings |
| 2 | Hurdle playing-time model; hierarchical pooling; learned decay | Not started | Beats v4 on the forecast tier, identical folds |
| 3 | Share-of-team xG/xA; team and fixture model; components; DC by calibration | Naive xG/xA challenger failed; redesign required | Coherence constraints hold; forecast tier improves |
| 4 | Joint Monte Carlo with shared team factors; constrained ensemble | Not started | Calibrated joint uncertainty; comparable chip values |
| 5 | Forward qualification on 2026/27 | Blocked until 2026/27 data exists | Both gate tiers, no position regression |
| A | Decision-layer repair | Squad-regret replay exists; continuity absent | Required before transfer regret joins the gate |
| B | Prospective capture | **Start now** | Continuous |

---

## 13. Risks and reconsideration triggers

Per `00_ProjectSpecification.md` §23:

| Trigger | Response |
|---|---|
| Bootstrap intervals on the model-versus-baseline regret difference straddle zero | Treat the incumbent as unjustified over a season average; redirect effort to Stages 2 and 3 rather than defending v4 |
| Stage 0 re-run materially changes the 2025/26 result | Re-open the "regret advantage is not stable" conclusion in `08_ModelEvaluation.md` |
| Stage 1 shows the appearance/minutes ceiling is small at both horizons | Redirect Stage 2 effort into the team and fixture model instead |
| Stage 1 attribution differs sharply between h=1 and h=8 | Split into horizon-specific configurations rather than one shared parameter set |
| Position-sliced calibration regresses while aggregates improve | Treat as a failed gate, not a trade-off |
| A share-based xG design still fails the bias gate | Suspect the team model, not the expected-event inputs |
| 2026/27 news or availability capture shows a large forecast uplift | Raise the priority of the news pipeline over further base-model work |

---

## 14. What this plan does not claim

- It does not claim five-season evaluation for model selection. Three development folds
  are usable, two for expected-event work, and every season has now been queried.
- It does not claim any historical holdout remains. Promotion requires forward 2026/27
  results.
- It does not claim defensive contributions can be validated across seasons. One season of
  the rule exists.
- It does not claim any historical measurement of news or availability-recovery uplift is
  possible.
- It does not claim the incumbent is currently justified over a season-average baseline on
  aggregate error. That question is open until §4.1 delivers intervals.
- It does not assume the transparent incumbent is the final model, nor that a learned
  challenger will replace it. Both remain candidates under the §8 gates.
