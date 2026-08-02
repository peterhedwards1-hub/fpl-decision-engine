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

The system is solved by five fixed-point sweeps rather than a fitted optimiser. In
each sweep a club's attack is its goals scored divided by what an average attack
would have been expected to score against the defences it actually faced. That
division is the opponent adjustment, and it is the whole point: the same two goals
move the rating further when they came against a strong defence.

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
`fpl-history evaluate-team-strength`, which scores the *rating* directly — expected
goals against goals actually scored, for both sides of every fixture, at every
origin — and never reads a player projection. That is what separates a gain in team
totals from a change in player allocation.

Team-goal accuracy, 760 team-fixtures per season:

| Season | Model | RMSE | MAE | Bias | Clean-sheet Brier |
|---|---|---:|---:|---:|---:|
| 2023/24 | **opponent-adjusted** | **1.2143** | **0.9634** | **−0.0468** | **0.1543** |
| 2023/24 | flat preseason (incumbent) | 1.2588 | 0.9876 | −0.1390 | 0.1585 |
| 2023/24 | raw-goals carry-forward | 1.2501 | 0.9640 | −0.2450 | 0.1611 |
| 2023/24 | existing team-share xG | 1.2781 | 1.0013 | −0.1858 | 0.1585 |
| 2022/23 | opponent-adjusted | 1.2620 | 0.9715 | **−0.0258** | 0.1943 |
| 2022/23 | flat preseason (incumbent) | 1.2540 | 0.9648 | −0.0771 | 0.1924 |
| 2022/23 | raw-goals carry-forward | **1.2394** | **0.9515** | −0.0915 | **0.1918** |
| 2022/23 | existing team-share xG | 1.4335 | 1.0508 | −0.5894 | 0.2420 |

2023/24 breakdowns, challenger against the incumbent:

| Slice | n | New RMSE | New bias | Incumbent RMSE | Incumbent bias |
|---|---:|---:|---:|---:|---:|
| Early season | 240 | 1.1571 | −0.0428 | 1.2618 | −0.1023 |
| Middle season | 260 | 1.2528 | −0.0912 | 1.2842 | −0.1803 |
| Late season | 260 | 1.2266 | −0.0061 | 1.2301 | −0.1316 |
| Promoted clubs | 114 | 0.9660 | −0.0428 | 0.9955 | +0.0757 |
| Established clubs | 646 | 1.2530 | −0.0475 | 1.2998 | −0.1769 |
| Easy schedule | 374 | 1.2520 | −0.0331 | 1.3034 | −0.1836 |
| Hard schedule | 386 | 1.1766 | −0.0601 | 1.2141 | −0.0957 |
| Large turnover | 76 | 1.2876 | −0.2484 | 1.3114 | −0.1895 |
| Small turnover | 570 | 1.2483 | −0.0207 | 1.2982 | −0.1752 |
| Home | 380 | 1.2317 | −0.0865 | 1.2945 | −0.1335 |
| Away | 380 | 1.1966 | −0.0072 | 1.2221 | −0.1444 |

**Read this honestly.** In 2023/24 the challenger wins every cell, and the largest
gain is early season — the case it was built for. In 2022/23 it does *not*: it is
marginally worse on RMSE and Brier and clearly better only on bias, and the
raw-goals carry-forward beats both.

The difference between the two seasons is expected-goal evidence. 2022/23's prior is
2021/22, which has no expected-goal rows at all, so both the prior and the
current-season ratings fall back to goals — and the opponent adjustment on a goals
series buys much less than it does on an expected-goals series. **The challenger's
advantage is conditional on the feed, and the one season where the feed is absent is
the one season it does not win.** That is a limitation, not a rounding error.

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
  Osula (Sheffield Utd, FWD), Mings (Aston Villa, DEF), Fabianski (West Ham, GK).

Cross-valuation over the horizon:

| | under incumbent | under challenger |
|---|---:|---:|
| Incumbent squad | 364.67 | 348.55 |
| Challenger squad | 360.45 | 354.98 |

Each model prefers its own squad — by 4.2 points for the incumbent and 6.4 for the
challenger — which is what cross-valuation is supposed to show and is not evidence
either way. What it does establish is that the difference is material: six of
fifteen places change, and the challenger drops two Burnley players the incumbent's
flat preseason prior could not distinguish from anyone else's.

---

## 8. Remaining uncertainties

1. **Two seasons is not enough evidence.** The 2022/23 and 2023/24 results point in
   different directions and neither is a large sample. Nothing here is a
   qualification.
2. **The advantage may be an expected-goals advantage rather than an
   opponent-adjustment advantage.** These two are confounded across the available
   seasons and cannot be separated with the data present.
3. **Fourteen declared constants are unvalidated individually.** They were chosen to
   be interpretable, not fitted. Sensitivity has not been profiled parameter by
   parameter as `profile-preseason-prior` does for Stage 3a.
4. **Squad continuity was measurable for one season transition only** in the imported
   data, and the large-turnover cell has 76 observations.
5. **Contextual adjustments have never been exercised on real information.** The
   mechanism is tested; the judgement it depends on is not.
6. **Player-points accuracy, squad regret, captain regret and transfer regret are not
   measured here.** They require a full backtest under the candidate configuration
   and are deliberately kept separate so a team-total gain is not confused with an
   allocation change.

---

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

Given §7, the specific thing forward capture has to settle is whether the advantage
survives when the expected-goal feed is complete — which it is for 2026/27 — and
whether it shows up in decisions rather than only in team totals.

---

## 10. Commands

```powershell
# Explain every club's rating at one origin, with the three rival models.
fpl-history --database data/fpl.sqlite3 team-strength-report 2023-24 `
  --gameweek 1 --rules config/seasons/2025-26.json `
  --output data/team-strength-2023-24-gw1.json

# With reviewed contextual adjustments.
fpl-history --database data/fpl.sqlite3 team-strength-report 2023-24 `
  --gameweek 12 --adjustments config/team_strength_adjustments.json

# Score the rating against realised goals, with every breakdown.
fpl-history --database data/fpl.sqlite3 evaluate-team-strength 2023-24 `
  --origin-start 1 --origin-end 38 `
  --output data/team-strength-evaluation-2023-24.json

# Compare the opening squads the two configurations pick.
fpl-history --database data/fpl.sqlite3 compare-opening-squads 2023-24 `
  --first-label incumbent `
  --first-config config/model_candidates/preseason-priors-v1-incumbent.json `
  --second-label opponent-adjusted `
  --second-config config/model_candidates/opponent-adjusted-team-strength-v1.json
```

The adjustments file is a JSON list of `ContextualAdjustment` fields:

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
    "confidence": "high"
  }
]
```
