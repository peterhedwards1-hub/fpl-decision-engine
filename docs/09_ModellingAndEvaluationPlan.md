# Modelling and evaluation plan (revised)

**Status:** Stages 0–4 and decision/promotion infrastructure implemented;
forward qualification remains open
**Version:** 1.3
**Date:** 2026-07-30
**Relates to:** `06_StrongestModelRoute.md` (implemented state),
`07_FootballAssumptionAudit.md` (assumption audit), `08_ModelEvaluation.md`
(multi-horizon evaluation), `10_StageOneDiagnostics.md` (implemented diagnostics),
`00_ProjectSpecification.md` §4, §11–13, §17

**Changes in 1.3:** implements the remaining engineering route without promoting a
historically designed challenger. Stage 2 now has chronological, calibrated four-part
hurdle models and live artifact application. Stage 3 has the coherent share-of-team-xG
challenger and latent-component oracles. Stage 4 has joint fixture/squad Monte Carlo,
proper-score validation and a constrained OOF ensemble. Stage 5 has immutable
pre-registration and executable forward gates. Track A now has continuous transfer
replay, empirical free-transfer option value and automatic chip timing. The genuinely
forward result is still unavailable.

**Changes in 1.2:** reconciles the plan with the implemented correctness work. Configured
appearance scoring and nullable defensive-contribution eligibility are complete; the
count-per-90 DC priors and correlated horizon-uncertainty aggregation were already
complete. Stage 1 now has a reproducible paired moving-block bootstrap, probability and
points calibration, core residual slices and component-backed appearance/minutes oracle
sensitivities. Prospective capture now has a fail-closed pre-deadline mode and a
Gameweek-level completeness report. Historical diagnostics remain design evidence only.

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

### 3.2 Defensive contributions — forward challenger delivered

The count-unit error is closed. `DEFENSIVE_CONTRIBUTION_COUNT_PRIORS` supplies
position-level count-per-90 priors to the threshold model. The remaining concern is the
distributional tail: 2025/26 defender appearances have mean 6.16 contributions and
variance 17.16, so a Poisson model is materially underdispersed. Observed threshold hit
rates are 20.8% for all defender appearances, 27.0% for appearances of at least 60
minutes, and 28.8% among defenders recording at least 2,800 season minutes. The previous
unqualified "nearer 35%" value is therefore withdrawn.

The one available season may calibrate a model for forward 2026/27 use, but it cannot
both estimate a fixed empirical prior and validate that prior on an earlier 2025/26
origin. A historical diagnostic must estimate any empirical rate chronologically using
only data before the origin. Otherwise the result is labelled in-sample calibration, not
validation.

`empirical_2025_minutes_band` is now an isolated challenger. It uses separate threshold
hit rates below 60 and from 60 minutes onward: DEF 5/924 and 816/3026, MID 2/2082 and
585/3265, and FWD 0/663 and 9/765. The normal backtest command rejects this configuration
for 2025/26 and earlier, making the leakage boundary executable rather than documentary.
Corrected v4 remains the incumbent until genuinely forward results exist.

**Why it remains important:**

- **It would pass the current audit gate.** The gate in `07_FootballAssumptionAudit.md`
  tests overall MAE, top-100 MAE, top-100 absolute bias and captain regret. Defenders
  rarely appear in the top-100 by projected points and are never captains, so a
  defender-only regression is invisible to it. See §8.3.
- **It affects forward defender valuation.** 2025/26 is the only season carrying the
  rule. Its data can design and calibrate a challenger, but the same season cannot then
  provide independent evidence that the challenger improved decisions.

### 3.3 Stage 0 work items

| # | Item | Rationale |
|---|---|---|
| 0.1 | Count-per-90 DC priors | **Complete** — separate position priors are used by the threshold model |
| 0.2 | Replace or calibrate the underdispersed Poisson tail | **Challenger complete** — minutes-band empirical hit rates; forward qualification open |
| 0.3 | Isolate DC calibration; do not broadly re-tune v4 on inspected seasons | Revised — retain the incumbent and qualify a separate forward challenger |
| 0.4 | Correlated horizon uncertainty aggregation | **Complete** — per-Gameweek uncertainty is added, not root-sum-of-squares |
| 0.5 | Config-driven appearance points | **Complete** |
| 0.6 | Nullable DC eligibility instead of `1000000` | **Complete** |

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
- A DC challenger is isolated from v4 and its 2025/26 calibration use is disclosed;
  promotion waits for forward 2026/27 evidence.

---

## 4. Stage 1 — diagnose the ceiling (infrastructure complete)

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

### 4.1 Paired moving-block bootstrap — delivered

Promoted to first in version 1.1. The incumbent's entire justification now rests on
regret, and that evidence is thin: against the season-average baseline it wins captain
regret in **2 of 5** seasons and top-15 regret in **3 of 5**, with margins of roughly 0.4
and 2 points. With five seasons and differences that small, signal cannot be cleanly
separated from noise. The bootstrap quantifies sensitivity; it cannot manufacture
independent seasons or restore a holdout.

`stage-one-diagnostics` resamples seasons and circular moving blocks of consecutive
target Gameweeks, never player rows. All origins and horizons for a target Gameweek stay
together. It reports mean and median paired block differences, 80% and 95% intervals,
percentage of target-Gameweek blocks won, worst-season and every-season differences, and
position RMSE differences. Leave-one-season interpretation remains important because five
seasons cannot provide a precise between-season interval.

### 4.2 Oracle sensitivity — partially delivered

Schema 14 persists point-component expectations for new backtests. The diagnostic can
therefore calculate two explicit, interacting sensitivity bounds on single-fixture rows:

1. actual **appearance** + predicted conditional points;
2. actual **appearance and minutes**, scaling linear components and applying the realised
   60-minute clean-sheet gate.

Team-goal and player-event sensitivities are now available for new share-xG backtests.
Their origin-time team expectation and player shares are persisted inside the component
record. These counterfactuals are sensitivity bounds, not additive attribution; ordering
and subsystem interactions are reported explicitly.

Appearance is kept separate from minutes-given-appearance (1 and 2) because the remedies
differ — news pipeline versus rotation modelling — and lumping them hides which to fund.
Run every counterfactual at h=1 and h=8, reusing the horizon harness that now exists.

### 4.3 Residual slicing — core implementation delivered

Automatic error tables now cover position, predicted-minutes band, DNP/played,
single/double Gameweek, early/mid/late season, forecast horizon and
top-15/16–50/51–100/outside-100 bands, with a configurable minimum sample count.
Home/away, fixture difficulty, price, promoted-club and role/change-point slices remain
open until those origin-time attributes are persisted with each prediction.

Purpose: detect an aggregate gain that conceals a damaging subgroup regression — exactly
the DC case in §3.2.

### 4.4 Calibration tables — delivered

Reliability by probability bin is recorded for appearance and 60-minute probabilities,
with Brier score and log loss. Multi-fixture rows are excluded because their persisted
probabilities mean "at least one appearance". Points calibration reports predicted
decile versus realised mean overall, by position and by forecast horizon.

### 4.5 Exit criteria

Bootstrap intervals on every headline comparison already reported, plus calibration,
core residual slices and all four implemented sensitivities: appearance, minutes,
team goals and player goals/assists.

---

## 5. Stage 2 — learned playing time (hurdle model; implemented challenger)

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

### 5.1 Implemented result

`train-playing-time-hurdle` fits appearance, start, 60-minutes-given-appearance and
conditional-minutes models. Logistic and histogram-gradient families are supported.
Calibration is isotonic and fitted only to strictly chronological OOF predictions. The
artifact stores its feature contract, signal-specific half-lives and calibration models;
`learned_hurdle` projection mode recreates its inputs from pre-origin rows.

On the already-inspected 2024/25 design season, the selected logistic challenger reduced
appearance Brier from 0.123705 to 0.099424 and expected-minutes RMSE from 28.0331 to
24.0093. Component sensitivity reduced points RMSE by 0.1441 and top-15 regret by 9.62,
but worsened global top-one regret by 0.324. The histogram model improved probability
scores slightly further but worsened top-one regret more. Logistic is therefore the one
predeclared forward candidate; neither result is promotion evidence.

---

## 6. Stage 3 — learned scoring components (coherent challenger implemented)

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

### 6.1a Preseason team strength and cold starts — implemented challenger

`_team_strengths` reads only completed fixtures in the target season. Before Gameweek 1
there are none, so every club returned `attack = defence = 1.0` and the model could not
distinguish Manchester City's attack from a promoted side's, nor penalise a defender facing
an elite forward line. Venue effects survived, because the home and away multipliers are
configured constants; club and opponent identity did not.

`team_strength_carry_forward` seeds each club's prior from the previous season's goals for
and against, regressed toward that season's league average by
`carry_forward_regression_matches`. Clubs are matched across seasons by name, because the
source reassigns team numbering annually; the join resolves 17 of 20 clubs in every season
pair from 2021/22 onward, with the remaining three correctly identified as promoted.
Promoted clubs take `promoted_team_attack_multiplier` and
`promoted_team_defence_multiplier` instead. The prior decays automatically: it enters the
existing shrinkage as pseudo-matches, so its weight falls as real fixtures accumulate. It
reads only a season that finished before the target season began and therefore cannot leak
into any origin within it.

`cold_start_prior = "position_price"` scales the goals, assists and bonus priors by a
player's price relative to their position's median. Disciplinary, goalkeeping and
clean-sheet priors are unscaled, the latter because team strength now supplies that signal.
Because it scales only the prior term, the adjustment fades as real minutes accumulate.

On the 2026/27 preseason snapshot the two options separate cleanly. Carry-forward does
almost all of the work: Haaland +1.20 and Saka +1.28 expected points at Gameweek 1, against
−0.27 for a Newcastle defender and −0.12 for an Ipswich defender. The cold-start prior
moves at most +0.18, because the 2026/27 cold-start pool tops out at £6.5m and contains no
marquee signing; the mechanism matters more in a season that has one.

**All parameter values are declared, not fitted.** No inspected season was used to choose
them. The package is registered as the single candidate `preseason-priors-v1`
(`config/model_candidates/preseason-priors-v1.json`) and is gated as one unit under §8.
`DEFAULT_MODEL_CONFIG` is unchanged, and a regression test asserts that the incumbent still
returns flat preseason strengths.

Not covered: the `team_share_expected` scoring path has its own strength estimator
(`_expected_goal_team_strengths`) and does not yet carry forward.

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

### 6.6 Implemented result

`team_share_expected` estimates exponentially decayed team xG for/against, shrinks new
teams to the league prior, opponent-adjusts fixture expectations and allocates goals and
assists through normalised player shares. Tests assert that player goal shares sum to one
and reconcile to the team expectation.

The 2024/25 design run improved MAE and absolute bias but worsened RMSE by 0.006 and
materially underpredicted the top 100. It remains a registered forward challenger rather
than replacing v4. New latent oracles show RMSE 2.0604 for the model, 2.0023 with actual
team goals and 1.3352 with actual player goals/assists. This redirects the next scoring
iteration toward player allocation/rates rather than a broader team-model search.

---

## 7. Stage 4 — joint distributions and correlation (infrastructure implemented)

**A first-class objective.** Correlation is the one thing no amount of per-player
modelling can supply, and it is what every remaining decision needs: Bench Boost, Triple
Captain, autosubs, bench valuation, transfer downside, and "probability squad A beats
squad B".

Correlation does not change the expected sum of player points, so shared clean-sheet
outcomes do not invalidate the current expected-value XI objective. It becomes necessary
for outcome variance, downside risk, squad-versus-squad probabilities and nonlinear
states such as correlated appearances and autosubs. `_expected_weekly_score` currently
assumes independent appearances when valuing those autosub states.

**Approach — Monte Carlo with shared team-level latent factors:**

1. simulate team scorelines from the team model (shared factor per club per fixture);
2. simulate appearance and minutes per player from the Stage 2 hurdle model;
3. allocate goals, assists and defensive events conditional on the team scoreline;
4. apply the configured FPL scoring rules;
5. resolve autosubs and captain fallback;
6. score complete squad outcomes.

This yields a joint uncertainty distribution, bench and captaincy risk, comparable chip
values, and squad-versus-squad probabilities — replacing the current heuristic
`1.25 + 3.5/√(matches+1) + 0.12·offset` uncertainty. Current horizon aggregation already
assumes persistence by adding per-Gameweek uncertainty.
The simulation is not calibrated merely because it is coherent: proper-score, coverage
and probability-integral-transform checks remain an explicit qualification gate.

**Constrained ensemble (deliberately small):** strictly chronological out-of-fold
predictions from each component model, blended by non-negative least squares with weights
summing to one. Split weights by position or season phase only where the bootstrap
supports it. This must not become another large tuning exercise; a simple blend makes it
obvious which models contribute value.

`simulate_squads` now implements the six-step shared-outcome route, including
Gamma-Poisson defensive counts, configured scoring, autosubs, captain fallback and
scoring chips. Its validation helper reports empirical CRPS, 50/80/95% coverage,
threshold Brier scores and PIT bins. `fit_constrained_ensemble` fits chronological OOF
least-squares weights projected onto the non-negative unit simplex and rejects any row
whose component training cutoff reaches its target period. These tools are not calibrated
merely by existing; forward outcomes must still pass their distribution gates.

---

## 8. Stage 5 — promotion gates (executable; awaiting outcomes)

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

- global top-one regret — **primary forecast-ranking diagnostic**. The persisted
  compatibility field remains named `captain_regret`, but it chooses from the entire
  player pool and is not realistic owned-squad captaincy;
- legal-XI, legal-squad and owned-squad captain regret — the realistic decision metrics;
- transfer regret — **added only after §9 completes**, since transfer continuity, hits and
  retained free transfers are not yet modelled in the replay.

### 8.3 Position-sliced calibration is a gate, not a diagnostic

No challenger is promoted if it materially worsens calibration for any position,
regardless of aggregate improvement. Before a forward comparison, "material" must be
predeclared with minimum samples and an uncertainty threshold so small cells and multiple
comparisons do not create arbitrary vetoes.

### 8.4 Held-out discipline

All five historical seasons have now been queried (§2). Development folds provide *design*
evidence only. **Promotion requires genuinely forward 2026/27 results.** No further
historical holdout exists to spend.

Schema 15 adds immutable candidate declarations containing the model version, canonical
config hash and full gate policy. The evaluator accepts only matched, completed,
`pre_deadline_only` runs after registration, rejects historical seasons and config drift,
and applies RMSE/bootstrap, bias, probability, every-position and decision gates. A
failure is not finalised while evidence is still accumulating unless explicitly
requested.

---

## 9. Parallel track A — decision-layer repair

The engineering gaps are closed. Transfer regret still cannot qualify a model until
prospective actions provide enough observations to estimate the transfer-need
distribution and score continuous replays.

| Item | Current behaviour |
|---|---|
| Free-transfer flexibility | `free_transfer_option_value` prices expected hits avoided from a prospectively estimated transfer-need distribution; it is state-dependent and respects the five-transfer cap. It stays zero when evidence is not supplied rather than inventing a constant |
| Future chip opportunity cost | `recommend_chip_timing` values every supplied future Gameweek and automatically subtracts the best later opportunity |
| Squad-regret replay continuity | `replay_transfer_continuity` persists squad, bank and free transfers, applies hits, exact bench autosubs and captain fallback, and compares each action with a same-state hindsight action |

Already closed: legal-XI/captain transfer gain, comparable chip increments, Bench Boost
autosub adjustment, UI integration, removal of terminal double-counting and removal of
the surrogate MILP weights.

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

The implementation now supports:

```powershell
fpl-collect --database data/fpl.sqlite3 --season-code 2026-27 `
  --require-pre-deadline

fpl-history --database data/fpl.sqlite3 `
  prospective-capture-status 2026-27
```

The first command fails before archive or ingestion if it is no longer pre-deadline. The
second reports, per Gameweek, exact pre-deadline snapshots, completed-fixture captures
with cumulative player outcomes, manager state, paired news projections, frozen
decisions, actual actions and evaluation gaps. The GitHub collection workflow runs on
Monday, Thursday and Friday during season months; missed evidence remains visible and
machine-checkable.

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
| 0 | Scoring units and semantics; isolated DC tail calibration | **Complete; forward challenger unqualified** | Compare Poisson and empirical DC forecasts on genuinely forward data |
| 1 | Bootstrap intervals; oracle sensitivity; residual slices; calibration | **Complete for current components** | Regenerate diagnostics as new forward outcomes and candidate runs arrive |
| 2 | Hurdle playing-time model; hierarchical pooling; learned decay | **Implemented; logistic candidate registered** | Beats v4 on genuinely forward forecast and decision tiers |
| 3 | Share-of-team xG/xA; team and fixture model; components; DC by calibration | **Coherent design implemented; historical design result failed RMSE/top-player bias** | Forward gate, or redesign player allocation using oracle evidence |
| 3a | Preseason team-strength carry-forward, promoted priors, price-aware cold starts | **Implemented and registered as `preseason-priors-v1`; parameters declared, not fitted; gate now reachable via `backtest-forward-candidate` against the committed control** | Both gate tiers on genuinely forward 2026/27 results; owned-captain and transfer regret producers still missing |
| 4 | Joint Monte Carlo with shared team factors; constrained ensemble | **Infrastructure implemented** | Accumulate forward CRPS, coverage, PIT and decision evidence |
| 5 | Forward qualification on 2026/27 | **Executable and predeclared; outcomes pending** | Both gate tiers, no position regression |
| A | Decision-layer repair | **Continuity, chip timing and empirical option-value infrastructure implemented** | Accumulate actual actions to estimate option value and transfer regret |
| B | Prospective capture | **Operational; run continuously** | No missed deadline evidence |

---

## 13. Risks and reconsideration triggers

Per `00_ProjectSpecification.md` §23:

| Trigger | Response |
|---|---|
| Bootstrap intervals on the model-versus-baseline regret difference straddle zero | Treat the incumbent as unjustified over a season average; redirect effort to Stages 2 and 3 rather than defending v4 |
| Forward DC calibration materially changes defender selection | Re-open position-level conclusions; do not reinterpret 2025/26 as a fresh holdout |
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
