# Team strength: the opponent-adjusted model

**Implementation date:** 2026-08-01
**Production incumbent:** `rates-rules-corrected-v4` — **unchanged**
**Challenger configuration:** `config/model_candidates/opponent-adjusted-team-strength-v1.json`
**Module:** `src/fpl_engine/team_strength.py`
**Report and evaluation:** `src/fpl_engine/team_strength_report.py`
**Relates to:** `09_ModellingAndEvaluationPlan.md` (Stage 3),
`11_ComponentModelsAndForwardQualification.md` (forward qualification),
`07_FootballAssumptionAudit.md`

---

## 1. What was wrong with the previous team strength

The production path rated a club by its raw goals for and against, shrunk toward
the league average by `team_prior_matches`. Four structural problems followed.

**Before the season, every club was identical.** With `team_strength_carry_forward`
off — the default — there are no completed fixtures to read, so every club sits
exactly on the league prior. The model could not tell the reigning champions from
a promoted side. The Stage 3a carry-forward option addressed this by carrying
previous-season *raw goal rates* across by club name.

**Nothing adjusted for who the goals came against.** A club that opened against
three promoted sides looked elite; a club that opened against the top three looked
poor. Over an eight-Gameweek horizon that is a large and entirely spurious signal,
and it is at its worst exactly when squad decisions matter most.

**There were two rival implementations that disagreed.** `_team_strengths` rated on
raw goals with a carry-forward prior. `_expected_goal_team_strengths`, reachable
only through `scoring_event_source="team_share_expected"`, rated on decayed expected
goals — with *no* opponent adjustment either, and no preseason prior at all. The two
could not be combined and gave different answers.

**Player scoring double-counted club quality.** For every configuration except
`team_share_expected`, a player's goal expectation was

```
rates["goals"] × minute_factor × (team_attack × opponent_defence × venue)
```

`rates["goals"]` is that player's historical goals per 90 — earned at that club,
against that club's fixtures, with that club's chance creation already inside it.
Multiplying it by the club's attack multiplier applies the club's quality a second
time. Good clubs' players were systematically overrated and poor clubs' underrated,
and the error grew with the strength of the multiplier.

Two further limitations had no mechanism at all: squad turnover between seasons did
not affect the rating or the confidence in it, and there was no way to record a
long-term injury, a manager change or a points deduction other than a blunt
`TeamStrengthOverride` that asserted a value with no dates and no expiry.

---

## 2. What the new model assumes

### The rating

A multiplicative Poisson model in the Dixon–Coles family. Expected goals in a
fixture are

```
lambda_home = league_average × attack_home × defence_away × home_factor
lambda_away = league_average × attack_away × defence_home × away_factor
```

Attack and defence are multipliers of the league average, renormalised to mean 1.0.
Venue is applied to the league average, never to the club rating, so venue effects
can never be mistaken for team quality.

The system is solved by iterating a fixed point to a tolerance of 1e-6, capped at
200 sweeps. In each sweep a club's attack is its goals scored divided by what an
average attack would have been expected to score against the defences it actually
faced. That division is the opponent adjustment, and it is the whole point: the
same two goals move the rating further when they came against a strong defence.

**The solve is ridged, and it has to be.** Each club carries
`solver_prior_matches = 3.0` pseudo-matches of exactly average performance,
appearing identically in numerator and denominator. Without it the fixed point does
not converge on an early-season schedule: after one Gameweek the fixture graph is
ten disconnected pairs, and within an isolated pair attack can be scaled up and
defence down without limit, so the iteration diverges. On 2023/24 an unridged solve
was still moving by 1.23 goals after 200 sweeps at GW2 and by 0.04 at GW4. A fixed
sweep count hid that by returning whatever the last sweep produced — which is
exactly the origin every preseason decision depends on. With the ridge every origin
from GW2 to GW38 converges in 9–14 sweeps.

Whether convergence was reached is reported per solve, along with the iteration
count, the largest remaining rating movement and any clubs pressed against the
multiplier bounds. A non-converged solve raises a limitation saying the ratings are
weakly determined rather than passing them off as settled.

### One source of truth for shared constants

`ProjectionModelConfig` owns every constant the two objects share — the home and
away factors, the multiplier bounds, the assist-per-goal prior and the form
half-life — and `TeamStrengthSettings.for_projection_config` copies them across
whenever a projection engine is built. `TeamStrengthSettings` owns only what is
genuinely the estimator's: prior weights, decay of the prior, promoted-club
priors, uncertainty, continuity regression and the solver ridge.

This is not tidiness. The declared default away factor is 0.92 and the candidate's
tuned value is 0.851, eight per cent apart; two copies would mean the historical
evaluation scoring a model the live forecast never runs. A test asserts the raw
defaults genuinely disagree, so the check is not vacuous.

**Every constant is declared, not fitted.** `TeamStrengthSettings` holds fourteen
of them. There is no six-dimensional parameter search here and there should not be
one — the project's evidence budget cannot identify that many parameters, and the
existing Stage 3a fit demonstrated the failure mode when it overturned its own
conclusion once the search bounds were widened.

### The preseason prior

The previous season is rated by the same solver, on **opponent-adjusted expected
goals** where the feed supports it and on goals where it does not, then regressed
toward the league mean by `prior_regression_matches` before being carried across.
Clubs are matched across seasons by normalised name, because the source reassigns
team numbers annually.

The fallback is explicit and reported. Expected goals are used only when the rows
cover at least `MINIMUM_EXPECTED_GOAL_COVERAGE` (80%) of the goals actually scored.
Below that, rows are missing rather than the league having underperformed, and the
clubs whose rows are absent would be rated as having created nothing.

The decision is league-wide and all-or-nothing on purpose: mixing rated and unrated
clubs in one solve is worse than falling back entirely, because the opponent
adjustment would propagate one club's missing rows to everyone who played them.
Coverage is still **measured per club and per venue side**, since a feed can clear
the league threshold while omitting a single club — and that club would then be
rated as creating nothing while the league-level check stays silent. The report
carries the league ratio, the home and away ratios, the fixture count with no
expected rows at all, the per-club ratio, and a named list of clubs below the
threshold. A club-level shortfall inside an otherwise usable feed, or a home/away
gap above ten points, each raise their own limitation.

Separately — and this matters more than it sounds — the *level* always comes from
goals actually scored, even when the *shape* comes from expected goals. Ratings are
renormalised to mean 1.0, so only the level is exposed to a patchy feed, and the
scoreline can always be trusted for the level. The imported 2022/23 season has
expected-goal coverage of about 70%; without this separation every club's forecast
would have been scaled down by a third because of a gap in someone else's data.

Promoted clubs take declared priors (`promoted_attack = 0.85`,
`promoted_defence = 1.18`) at reduced weight (`promoted_prior_matches = 5.0`) and
carry extra uncertainty. These are guesses, and the model records them as guesses
rather than dressing them as measurements.

### Current-season updating and the fade

Current-season fixtures are rated by the same solver with exponential chronological
decay (`current_half_life_gameweeks = 8.0`), then blended with the prior in
proportion to weight.

The prior's weight also decays, on the same clock. This is not bookkeeping. Because
current-season weight is decayed, it *saturates* at roughly
`half_life / ln 2` ≈ 11.5 matches however long the season runs. Against a prior of
fixed weight it would never win, and last season would still be shaping the rating
in May. Decaying the prior on the same clock makes
`prior_matches_intact_squad = 12.0` mean what it says: the prior is worth twelve
matches at the opener and halves every eight Gameweeks thereafter.

Only fixtures with `gameweeks.number < origin` are read, and `as_of` /
`maximum_ingestion_run_id` restrict further to observations recorded by a point in
time. The same origin always produces the same ratings.

---

## 3. What squad continuity can and cannot establish

Continuity compares the current registered squad against the previous season and
measures the retained share of minutes, expected goals, expected assists and
goalkeeper/defender minutes, summarised into one score.

**It can** tell you how much of last season's team is still here, and therefore how
much last season's performance is evidence about this season's. A heavily rebuilt
club gets a prior of lower weight, a prior pulled `rebuild_prior_regression` (50%)
of the way toward the league mean in proportion to how much changed, and a wider
uncertainty band.

**It cannot** tell you which direction a rebuild went. Nothing in the available data
distinguishes a club that replaced its squad well from one that replaced it badly.
The model therefore changes only *confidence* and *distance from average* — never
the direction.

### Why transfer direction is not inferred from fees

It would be easy to read a transfer fee, or a player's price, or a reputation, and
conclude that a club improved. It would also be wrong often enough to be dangerous.
Fees reflect contract length, selling-club leverage, agent economics, amortisation
strategy and market timing at least as much as they reflect quality. A model that
learns "expensive signing → stronger club" learns the transfer market's *pricing*,
not its *outcomes*, and it does so from a sample of at most a few dozen meaningful
transfers per season. The project's evidence budget cannot support that inference,
and the failure mode — confidently rating a club by what it spent — is exactly the
kind of plausible-sounding error that survives review.

So: no fee, no reputation, no price is read anywhere in `team_strength.py`.

### The identity trap

Continuity depends on a stable player code linking the two seasons. The source
reassigns its own player ids annually, so keying continuity on `source_player_id`
reports every settled squad as a total rebuild — quietly stripping the preseason
prior from every club in the division while the ratings still look plausible. The
module keys on the database's own player identity instead.

Where no stable code exists at all, continuity is reported as **unmeasured**, not as
zero. That distinction is deliberate: zero is a finding, and the model does not have
one. `MINIMUM_LEAGUE_RETENTION` (10%) detects the broken-link case, since no real
division turns over ninety per cent of its minutes at once.

---

## 4. How contextual adjustments should be reviewed

`ContextualAdjustment` is the structured path for information match data cannot
know: a major transfer, a long-term injury to a structurally important player, a
goalkeeper change, a manager change, a tactical shift, a points deduction.

Each one carries separate attack and defence multipliers, an effective-from
Gameweek, an optional expiry, a category, a rationale, a source description and a
confidence. Multipliers are bounded to [0.70, 1.40] — a reviewed note may move a
club meaningfully but must never be able to invent a different team. **An adjustment
without a rationale is refused at construction.** Every applied adjustment appears
in the club's rationale, in the report, and in the projection run's persisted
assumptions, so none can be applied silently.

Review guidance:

- **Enter the event, not the conclusion.** "First-choice striker out until December"
  is reviewable; "attack down 15%" is not.
- **Date it.** An injury with a known return date should expire. An adjustment with
  no expiry keeps applying for the rest of the season, which is almost never right
  for anything except a points deduction.
- **Prefer low confidence and a small multiplier** to a confident large one. The
  rating already carries uncertainty; an adjustment should nudge it, not replace it.
- **Do not enter a transfer as an adjustment** to encode that a signing is good.
  That is precisely the inference §3 refuses. Enter it only where the *structure*
  changed in a way continuity cannot see — a first-choice goalkeeper leaving, say.
- **Nothing scrapes anything.** There is no news interpreter behind this. It is
  deliberately inert until a person enters something.

`TeamStrengthOverride` still works and still wins outright where supplied. The
distinction is that an override is an operator asserting a value, while an
adjustment is a bounded, dated, explained nudge to a derived one.

---

## 5. Coherent player allocation

The challenger uses `scoring_event_source="team_share_expected"`, so a player's goal
expectation is the team's goal expectation for that fixture times that player's
share of it. Club quality enters exactly once, through the team total.

A player's share weight is their expected goal involvement in one fixture: a shrunk
per-90 rate scaled by expected minutes, built from historical expected goals and
assists (falling back to actual events), recent evidence weighted by
`scoring_recent_evidence_weight`, a position prior for small samples, and the
cold-start price factor applied to the prior term only — so a player with no minutes
at this club is not assumed to be a reserve, and the adjustment fades automatically
as real minutes accumulate.

Shares are normalised within a club, so player goal expectations reconcile exactly
to the team's.

**What this does not solve.** No penalty-taker or set-piece data exists in this
schema. Share is inferred from output alone, which means a designated penalty taker
is recognised only through the goals they have already scored — slowly, and not at
all before their first. A reviewed role override remains the only way to assert one.
Own goals are not deducted from the team total; they stay in the per-player
deduction where they already lived. Not every goal is assisted, which is handled by
a single league-wide assist-per-goal ratio rather than a per-club one, because
club-level assist rates are noisy and the spread between clubs is small next to the
spread between players.

---

## 6. Defensive coherence

One opponent-goal expectation per fixture drives everything defensive: the
defensive vulnerability in the rating, clean-sheet probability as `exp(-lambda)`,
the goalkeeper and defender goals-conceded deduction, and the squad-level
simulation, which reads the same `team_strengths` dictionary. Two defenders at the
same club facing the same opponent receive the same underlying clean-sheet
probability; their points differ only through their 60-minute probabilities.

---

## 7. Historical design results

Full reports: `data/team-strength-evaluation-2022-23.json`,
`data/team-strength-evaluation-2023-24.json`. Both were produced by
`fpl-history evaluate-team-strength`, which drives every model — the challenger
included — through `RatesProjectionModel.fixture_expected_goals`, the same public
component the live forecast uses. It scores the *rating* directly: expected goals
against goals actually scored, for both sides of every fixture, at every origin,
never reading a player projection. That is what separates a gain in team totals from
a change in player allocation.

> **These figures supersede an earlier set.** The first version of this evaluation
> recreated the fixture expectation inside the evaluator with its own copy of the
> venue constants — 0.92 away where the candidate actually runs 0.851. It was
> therefore scoring a model nothing ran. Every number below is the corrected one,
> and the corrected 2022/23 result is *better* for the challenger, not worse.

Team-goal accuracy, 760 team-fixtures per season:

| Season | Model | RMSE | MAE | Bias | Clean-sheet Brier |
|---|---|---:|---:|---:|---:|
| 2023/24 | **opponent-adjusted** | **1.2250** | **0.9636** | **−0.1181** | **0.1542** |
| 2023/24 | flat preseason (incumbent) | 1.2588 | 0.9876 | −0.1390 | 0.1585 |
| 2023/24 | raw-goals carry-forward | 1.2501 | 0.9640 | −0.2450 | 0.1611 |
| 2023/24 | existing team-share xG | 1.2781 | 1.0013 | −0.1858 | 0.1585 |
| 2022/23 | opponent-adjusted | 1.2462 | 0.9584 | **−0.0743** | **0.1918** |
| 2022/23 | flat preseason (incumbent) | 1.2540 | 0.9648 | −0.0771 | 0.1924 |
| 2022/23 | raw-goals carry-forward | **1.2394** | **0.9515** | −0.0915 | **0.1918** |
| 2022/23 | existing team-share xG | 1.4335 | 1.0508 | −0.5894 | 0.2420 |

2023/24 breakdowns, challenger against the incumbent:

| Slice | n | New RMSE | New bias | Incumbent RMSE | Incumbent bias |
|---|---:|---:|---:|---:|---:|
| Early season | 240 | 1.1799 | −0.1043 | 1.2618 | −0.1023 |
| Middle season | 260 | 1.2550 | −0.1661 | 1.2842 | −0.1803 |
| Late season | 260 | 1.2356 | −0.0828 | 1.2301 | −0.1316 |
| Promoted clubs | 114 | 0.9676 | +0.0173 | 0.9955 | +0.0757 |
| Established clubs | 646 | 1.2651 | −0.1420 | 1.2998 | −0.1769 |
| Easy schedule | 373 | 1.2740 | −0.1399 | 1.3050 | −0.1850 |
| Hard schedule | 387 | 1.1759 | −0.0971 | 1.2126 | −0.0946 |
| Large turnover | 76 | 1.2887 | −0.2865 | 1.3114 | −0.1895 |
| Small turnover | 570 | 1.2619 | −0.1227 | 1.2982 | −0.1752 |
| Home | 380 | 1.2453 | −0.1105 | 1.2945 | −0.1335 |
| Away | 380 | 1.2045 | −0.1256 | 1.2221 | −0.1444 |

**Read this honestly.** The challenger now beats the incumbent on RMSE, MAE, bias
and Brier in *both* seasons, and wins every 2023/24 breakdown cell except late
season, where it is 0.005 worse on RMSE while being materially better on bias. The
largest gain is early season — the case it was built for.

But it still loses to **raw-goals carry-forward** in 2022/23 (1.2394 against 1.2462),
while beating it comfortably in 2023/24 (1.2501 against 1.2250). The difference
between the seasons is expected-goal evidence: 2022/23's prior is 2021/22, which has
no expected-goal rows at all, so both the prior and the current-season ratings fall
back to goals — and opponent adjustment on a goals series buys much less than on an
expected-goals series. **The challenger's advantage over the simpler carry-forward is
conditional on the feed.** That is a limitation, not a rounding error, and it is the
specific thing forward capture has to settle.

Two slices deserve their own note. **Large turnover** is the one cell where the
challenger's bias is clearly worse (−0.2865 against −0.1895): pulling a rebuilt
club's prior toward the mean makes the model expect fewer goals from clubs that had
in fact strengthened. With n=76 — Chelsea and Wolves — this is suggestive, not
conclusive, and it is the honest cost of refusing to infer transfer direction.
**Bias is now uniformly negative** across the challenger's slices, because the
candidate's tuned away multiplier of 0.851 is materially below a neutral split; that
is inherited from the v3 tuning, not introduced here, and it is visible in the
incumbent's numbers too.

The existing team-share xG path's −0.5894 bias in 2022/23 is a data defect, not a
model result: that import stores `expected_goals` as `0.0` rather than `NULL`, so
its COALESCE fallback to actual goals never fires. The same defect crashed that path
with a division by zero; a minimal guard now falls back to the league constant, which
changes no output that previously computed. The new model's coverage check catches
the same defect at the league level and falls back to goals deliberately.

### Where the difference is, and where it isn't

Attack-rank agreement with each rival, 2023/24 (Spearman, 20 clubs):

| Origin | vs flat preseason | vs raw-goals carry-forward | vs existing team-share xG |
|---|---:|---:|---:|
| GW1 | *(no ranking)* | 0.9955 | *(no ranking)* |
| GW12 | 0.9248 | 0.9143 | 0.9624 |
| GW25 | 0.8241 | 0.8150 | 0.9789 |

At GW1 the flat and team-share models give every club the same multiplier, so they
have no ranking to compare against — the report says so rather than correlating
against an alphabetical tie-break.

The important reading is that the **orderings agree**. At the opener the new model
ranks clubs almost identically to raw-goals carry-forward, and by GW25 it still
agrees with every rival at 0.82 or better. **The accuracy gains in the table above
therefore come from the magnitudes, the prior/current weighting and the uncertainty
— not from a materially different opinion about which clubs are good.** That is a
smaller claim than "a better ranking", and it is the one the evidence supports.

### Opening-squad difference

`data/team-strength-squad-comparison-2023-24.json`, GW1 with an eight-Gameweek
horizon, incumbent (`preseason-priors-v1-incumbent`) against the challenger:

- 9 of 15 players shared.
- Incumbent only: Taylor (Burnley, DEF), Weghorst (Burnley, FWD), Reed (Fulham, MID),
  Alexander-Arnold (Liverpool, DEF), Amissah (Sheffield Utd, GK), Kilman (Wolves, DEF).
- Challenger only: Henry (Brentford, DEF), Saka (Arsenal, MID), Ream (Fulham, DEF),
  Robinson (Fulham, DEF), Osula (Sheffield Utd, FWD), Fabianski (West Ham, GK).

Cross-valuation over the horizon:

| | under incumbent | under challenger |
|---|---:|---:|
| Incumbent squad | 364.67 | 347.48 |
| Challenger squad | 360.69 | 353.31 |

Each model prefers its own squad — by 4.0 points for the incumbent and 5.8 for the
challenger — which is what cross-valuation is supposed to show and is not evidence
either way. What it does establish is that the difference is material: six of
fifteen places change, and the challenger drops two Burnley players the incumbent's
flat preseason prior could not distinguish from anyone else's.

**This comparison cannot attribute the change**, because the two configurations
differ in both team strength and allocation. §8b is what separates them.

---

## 8. Remaining uncertainties

1. **Two seasons is not enough evidence.** Neither is a large sample, and the
   challenger beats the simpler raw-goals carry-forward in one of them and loses in
   the other. Nothing here is a qualification.
2. **The advantage may be an expected-goals advantage rather than an
   opponent-adjustment advantage.** These two are confounded across the available
   seasons and cannot be separated with the data present. The season where the
   expected-goal feed is absent is the season the challenger does not win.
3. **Sixteen declared constants are unvalidated individually.** They were chosen to
   be interpretable, not fitted. Sensitivity has not been profiled parameter by
   parameter as `profile-preseason-prior` does for Stage 3a. The one that most
   deserves it is `solver_prior_matches`, because it is doing real work early in the
   season rather than merely regularising a well-posed problem.
4. **Squad continuity was measurable for one season transition only** in the imported
   data, and the large-turnover cell has 76 observations — where the challenger's
   bias is its worst.
5. **Contextual adjustments have never been exercised on real information.** The
   mechanism is tested and hashed; the judgement it depends on is not.
6. **The candidate inherits v3's tuned away multiplier of 0.851**, which is
   materially below a neutral venue split and shows up as uniformly negative bias.
   That was not introduced here and has not been re-examined.
7. **The decision result rests on one season and one transfer policy.** §8b's
   ordering is consistent across three measures but the margins are small, and the
   regret replays are sensitive to the fixed policy they assume.

### What the whole exercise established about where the error is

Splitting 2023/24 squared points error by how wrong the minutes forecast was, over
28,084 predictions:

| Minutes error | Share of rows | Share of squared error | RMSE |
|---|---:|---:|---:|
| within 15 minutes | 47% | **8%** | 0.89 |
| off by 15–45 | 38% | 48% | 2.38 |
| off by 45 or more | 15% | 44% | 3.55 |

**92% of the error sits on the 53% of players whose minutes were mis-called.** When
the model knows a player will play, it predicts their points very accurately. A
minutes oracle — rescaling each projection by actual over expected minutes — removes
**17.2% of overall RMSE and 15.0% within the top 5%**. Every variant in §8b sits
within 0.011 RMSE of every other.

That is the finding this work produced, and it is not the one it set out to test:
team strength and player allocation are close to exhausted as sources of forecast
error, and playing time is not. The already-built, already-measured
`playing-time-hurdle-logistic-v1` candidate (appearance Brier 0.124 → 0.099,
expected-minutes RMSE 28.0 → 24.0, minutes bias −10.3 → −0.53) has never been run
end-to-end into points, and on this evidence is where the next real gain is.

---

## 8a. The declaration is the whole model

A candidate declaration has to pin down everything that decides the forecast,
before the outcome. `ProjectionModelConfig` alone stopped doing that the moment
team strength grew its own constants and an adjustment manifest: both were
previously supplied at runtime from code defaults, so a candidate's hash could stay
identical while a later edit to a default changed every forecast it produced.
Persisting the derivation afterwards records what happened; it does not make a
preregistration immutable. Only hashing the inputs does.

`ModelDeclaration` (`src/fpl_engine/declaration.py`) is the hashed unit:

```json
{
  "declaration_version": 2,
  "model_config": { ... },
  "team_strength_settings": { ... },
  "contextual_adjustments": [ ... ]
}
```

- Every `TeamStrengthSettings` field is inside the digest. A test asserts that
  changing any of four representative constants moves it.
- The adjustment manifest is inside the digest, including `reviewed_at` and
  `reviewed_by`, which are now **required** — an adjustment carries a judgement no
  data supports, so a later reader must be able to find out who made it and when.
  `reviewed_at` must be timezone-aware, so the order against a deadline is
  unambiguous, and `adjustments_before(cutoff)` makes "this judgement predates the
  outcome" checkable rather than assumed.
- Manifest order does not affect the digest; every audit field does.
- **Legacy declarations keep their identity.** A declaration carrying nothing beyond
  the projection config serialises to the bare config dictionary, so the three
  candidates already registered against 2026/27 hash exactly as before.

`register_forward_candidate`, `capture_gameweek_forecasts`,
`run_forward_candidate_pair`, `ProjectionBacktester` and the CLI's candidate loader
all carry the settings and the manifest through, so a capture or a backtest built
from a declaration is the declared model rather than a truncation of it.

## 8b. Separating team strength from allocation

The candidate changes two things at once — how much a club is expected to score,
and how that expectation reaches individual players — so a squad comparison against
the incumbent cannot attribute the difference. `evaluate-allocation-variants` runs a
two-by-two design:

| Variant | Team strength | Allocation | |
|---|---|---|---|
| A | existing | player rate | the production incumbent |
| B | opponent-adjusted | player rate | **structurally unsound** |
| C | existing | team share | the control for D |
| D | opponent-adjusted | team share | the candidate |

**B is unsound and is measured anyway.** The rate path multiplies a player's
historical per-90 rate — which already embeds the strength of the club they earned
it at — by that club's strength multiplier, so a better team rating makes the
double-count larger, not smaller. It is included because "we could not run it" and
"we ran it and it is worse" are different claims and only the second is evidence.

**D against C is the contrast that matters**: it holds allocation fixed at the
coherent share route and moves only the team-strength model, which is the marginal
contribution of opponent adjustment. Each variant is scored on player-points RMSE,
MAE and bias, top-player calibration at every rank cut, legal-squad regret,
owned-captain regret and transfer regret, over identical origins through the same
backtester and evaluators the promotion gate uses.

### Results: 2023/24, origins 2–38

`data/allocation-variants-2023-24.json`, 28,084 scored predictions per variant.

| Variant | MAE | Bias | Top-15 MAE | Top-15 bias | Squad regret | Captain regret | Transfer regret | Policy points |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A incumbent | 1.1364 | −0.005 | 3.333 | **+0.231** | **98.54** | 5.811 | 25.42 | 2065 |
| B opp-adj + rate | 1.1325 | −0.002 | 3.386 | +0.258 | 98.76 | 5.676 | **24.25** | 2084 |
| C existing + share | 1.1279 | +0.024 | **3.246** | +0.740 | 101.22 | **5.432** | 25.08 | **2095** |
| D candidate | **1.1244** | +0.020 | 3.304 | +0.498 | 100.92 | 6.000 | 25.89 | 2027 |

**The candidate has the best MAE and the worst decisions.** D leads on aggregate
accuracy and is last or joint-last on captain regret, transfer regret and continuous
policy points. This is precisely the failure the project has already documented
twice — an accuracy gain that does not survive contact with the decision layer — and
precisely why the gate says *do not promote on MAE alone*.

The isolating contrast, D against C, moves only the team-strength model:

| Measure | D − C | Reading |
|---|---:|---|
| Player-points MAE | −0.0035 | slightly better |
| Top-15 bias | −0.2422 | **materially better** |
| Legal-squad regret | −0.2973 | slightly better |
| Owned-captain regret | +0.5676 | **worse** |
| Transfer regret | +0.8056 | **worse** |
| Continuous policy points | −68 | **worse** (−3.3%) |

So opponent adjustment does what it was built to do — it repairs about a third of
the top-player bias the share allocator introduces — and still leaves the combined
model making worse weekly decisions than the incumbent.

**Variant B, the structurally unsound one, is not the disaster the reasoning
predicted.** It is mildly better than A on MAE, transfer regret and policy points.
That does not make the double-count acceptable: B's top-15 MAE is the worst of the
four and its top-15 bias is worse than A's, which is where a double-count would show
up. But the measured cost is smaller than the argument implies, and reporting that
is the point of having run it.

Two cautions before reading too much into any of this. One season and 37 origins is a
small sample, and the regret replays run a single fixed transfer policy — a 3%
difference in policy points is not far outside what a different policy might produce.
The direction is nonetheless consistent across three independent decision measures.

## 9. Forward qualification requirement

Historical seasons are **design evidence only**. `DEFAULT_MODEL_CONFIG` is unchanged
and the challenger is not promoted, not registered by default, and not reachable
without explicitly selecting `team_strength_model="opponent_adjusted"`.

Genuine qualification requires prospective 2026/27 evidence through the existing
route in `11_ComponentModelsAndForwardQualification.md`: immutable pre-registration
with a config SHA-256, matched incumbent and candidate runs from the same
pre-deadline snapshot, and the predeclared forecast, distribution and
decision gates. The imported database currently holds 2021/22–2023/24 only, so no
2026/27 origin exists to register against yet.

### Recommendation: do not register this candidate yet

The spec says to register as a forward challenger *only if the implementation and
historical diagnostics are sound*. The implementation is sound. The diagnostics are
not — §8b shows the candidate has the best aggregate accuracy and the worst weekly
decisions of the four variants, which is the exact pattern the promotion gate exists
to catch. Registering it now would spend a scarce 2026/27 capture slot on a
configuration whose own design evidence says it makes decisions worse.

What the evidence actually supports doing first, in order:

1. **Fix the top-player bias the share allocator introduces** (+0.231 → +0.740 at
   top-15 when share allocation is switched on). Opponent adjustment repairs a third
   of it; the other two thirds are the allocator's problem. Until that is fixed, the
   coherent route carries a known defect into every decision.
2. **Re-run the variants on 2022/23** to see whether the decision ordering is stable
   or a single-season artefact. One season and one transfer policy is thin evidence
   for a conclusion this consequential.
3. **Then register `C`-with-fixed-allocation and `D`-with-fixed-allocation as a
   pair**, so forward capture measures opponent adjustment against a control that is
   not itself broken.

The team-strength model should be kept. It is better on team goals in both seasons,
it converges, it is auditable, and it demonstrably repairs part of the allocator's
bias. It is the allocator, not the rating, that is currently costing decisions.

---

## 10. Commands

```powershell
# Explain every club's rating at one origin, with the three rival models.
fpl-history --database data/fpl.sqlite3 team-strength-report 2023-24 `
  --gameweek 1 --output data/team-strength-2023-24-gw1.json

# With reviewed contextual adjustments.
fpl-history --database data/fpl.sqlite3 team-strength-report 2023-24 `
  --gameweek 12 --adjustments config/team_strength_adjustments.json

# Score the rating against realised goals, with every breakdown.
fpl-history --database data/fpl.sqlite3 evaluate-team-strength 2023-24 `
  --origin-start 1 --origin-end 38 `
  --output data/team-strength-evaluation-2023-24.json

# Separate the team-strength change from the allocation change.
fpl-history --database data/fpl.sqlite3 evaluate-allocation-variants 2023-24 `
  --origin-start 2 --origin-end 38 --horizon 1 `
  --output data/allocation-variants-2023-24.json

# Compare the opening squads the two configurations pick.
fpl-history --database data/fpl.sqlite3 compare-opening-squads 2023-24 `
  --first-label incumbent `
  --first-config config/model_candidates/preseason-priors-v1-incumbent.json `
  --second-label opponent-adjusted `
  --second-config config/model_candidates/opponent-adjusted-team-strength-v1.json
```

Every command defaults `--rules` to `config/seasons/<season>.json`. Passing another
season's rules is rejected by the regret evaluators, and silently changes the
scoring rules everywhere else — so leave it alone unless you mean it.

The adjustments file is a JSON list of `ContextualAdjustment` fields. Every field
below except `effective_to_gameweek` is required; construction fails without them.

```json
[
  {
    "source_team_id": "14",
    "category": "long_term_injury",
    "attack_multiplier": 0.88,
    "effective_from_gameweek": 5,
    "effective_to_gameweek": 18,
    "rationale": "First-choice striker out until December; club statement 3 Sep.",
    "source": "club statement",
    "confidence": "high",
    "reviewed_at": "2026-09-03T14:20:00+00:00",
    "reviewed_by": "p.edwards"
  }
]
```

To register a candidate whose adjustments are part of its preregistration, put the
same list inside the declaration's `contextual_adjustments` array rather than
passing it at the command line — only what is in the declaration is hashed.
