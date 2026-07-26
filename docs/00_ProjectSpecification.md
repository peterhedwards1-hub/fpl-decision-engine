# FPL Decision Engine — Project Specification

**Status:** Draft for review  
**Version:** 0.2  
**Primary user:** Peter Edwards  
**Repository:** `peterhedwards1-hub/fpl-decision-engine`

---

## 1. Executive summary

The FPL Decision Engine is a private, data-driven decision-support system for one Fantasy Premier League team.

Its purpose is:

> **To maximise expected total season points by making the best available decision before each FPL deadline, using only information that was available at that time.**

The system is not intended to maximise novelty, differential ownership, social-media approval, entertainment value or mini-league psychology. Those factors matter only where they change expected season points.

The system will combine:

1. structured FPL, fixture and performance data;
2. an AI-assisted review of current squad and general team news;
3. explicit models of expected minutes, scoring components and uncertainty;
4. rolling-horizon optimisation of transfers, captaincy, starting XI, bench order and chips;
5. a browser-based dashboard and generated deadline report;
6. permanent storage of pre-deadline forecasts, recommendations and actual actions for later validation.

The system is a decision aid, not an autonomous team manager. It will surface important judgement calls and preserve a full audit trail.

---

## 2. Decisions already agreed

The following decisions are considered settled unless later evidence justifies changing them.

| Area | Decision |
|---|---|
| Primary user | One private user and one FPL team |
| Primary objective | Maximise expected total season points |
| Normal planning horizon | Rolling five-Gameweek horizon |
| Pre-season horizon | Eight Gameweeks |
| Exceptional horizon | Extend the horizon when a known blank, double, chip deadline or other material event lies just beyond the normal window |
| Replanning cadence | Re-run before every deadline as new information becomes available |
| Weekly process | Early provisional pass followed by a final post-team-news pass |
| Decision style | Risk-neutral expected-value maximisation rather than fixed conservative or aggressive personalities |
| Uncertainty | Represent explicitly and use where it can affect decisions or option value |
| News process | ChatGPT extended-thinking research session produces structured evidence and flags judgement calls |
| Human role | Review material or ambiguous news adjustments before final optimisation |
| Data cost | Use free, accessible and permitted data sources; no paid-source dependency |
| Private squad state | Use a simple manual browser input as the reliable initial method, with optional authenticated automation later |
| Interface | Browser dashboard plus generated deadline report |
| Visibility | Private initially |
| Reproducibility | Store the exact information, rules, models and outputs used before each deadline |

---

## 3. Project scope

### 3.1 In scope

The system should support the complete recurring decision cycle for the user's FPL team:

- load the current squad, purchase prices, selling prices, money in the bank, available transfers and chips;
- update fixtures, deadlines, player data, team data and recent performance;
- collect and structure relevant injury, suspension, selection and tactical news;
- permit explicit availability, role and expected-minutes overrides with rationale;
- estimate expected minutes for each relevant player;
- forecast each material FPL scoring component;
- estimate player point distributions over forthcoming Gameweeks;
- evaluate current squad quality;
- recommend transfers or recommend rolling;
- assess whether a points hit is justified;
- optimise the starting XI and bench order;
- recommend captain and vice-captain;
- maintain a provisional five-Gameweek route while recognising that it will be updated next week;
- support pre-season squad construction over an eight-Gameweek horizon;
- recognise wildcard and other chip options;
- record forecasts and recommendations before deadlines;
- record the action actually taken and any reason for deviating from the recommendation;
- compare forecasts and decisions with subsequent outcomes.

### 3.2 Out of scope for the initial project

The first versions will not attempt to:

- provide accounts for multiple public users;
- optimise for ownership, effective ownership or differentials for their own sake;
- optimise directly for mini-league position;
- predict exact price-change thresholds as a core requirement;
- depend on paid data feeds;
- place FPL transfers automatically;
- allow unreviewed AI news interpretation to silently alter model inputs;
- train an opaque machine-learning model before reliable baselines and validation exist;
- claim that a five-Gameweek plan is a fixed commitment;
- claim that a one-transfer search has ruled out every possible two-transfer route.

### 3.3 Possible later extensions

- mini-league strategy as a separate layer over the point-maximisation model;
- public multi-user deployment;
- paid or licensed data providers where they offer clear value;
- richer live-rank and effective-ownership analysis;
- automated alerts when material new information changes the recommendation;
- authenticated current-team collection if a stable and secure method is practical;
- ensemble or Bayesian models after enough clean point-in-time data exists.

---

## 4. Success definition

The project succeeds if it improves the expected quality of decisions taken for the user's team and thereby increases expected total season points.

Evaluation must separate forecast quality from decision quality.

### 4.1 Forecast quality

The system should measure:

- expected-minutes error;
- start-probability calibration;
- probability-of-appearance calibration;
- expected-points calibration;
- goal and assist probability calibration;
- clean-sheet probability calibration;
- defensive-contribution probability calibration;
- save and bonus calibration;
- uncertainty calibration;
- ranking quality among realistic transfer targets.

These metrics provide thousands of player-Gameweek observations and therefore have much greater statistical power than season-level realised decision outcomes.

### 4.2 Decision quality

The system should measure:

- expected and realised value of recommended transfers after hit costs;
- value of rolling versus transferring;
- captaincy decision performance;
- starting-XI and bench-order performance;
- wildcard and chip performance;
- comparison with simple baselines;
- comparison with the user's actual chosen action where it differed;
- regret relative to the best action that was reasonably available at the deadline.

A single season supplies only about 38 weekly decision samples. Realised transfer gain and regret are therefore useful for narrative review and long-term accumulation, but should not drive rapid model churn on their own. Model selection should rely more heavily on forecast calibration, historical replay and out-of-sample comparison.

### 4.3 Baseline discipline

No added complexity should be adopted merely because it sounds sophisticated. A new model or data source must demonstrate relevant out-of-sample improvement over transparent alternatives.

---

## 5. Core principles

### 5.1 Optimise decisions, not headlines

The system exists to choose actions, not merely to produce player rankings.

### 5.2 Maximise season value, not one-week points

A move that gains points this week may still be poor if it damages squad structure, consumes useful flexibility, blocks a future move or leaves a weak squad beyond the visible horizon.

### 5.3 Replan as information arrives

The five-Gameweek route is provisional. The system should expect injuries, tactical changes, fixture rearrangements and new evidence to alter it.

### 5.4 Represent uncertainty honestly

Player forecasts should include plausible ranges and relevant probabilities rather than presenting a decimal mean as certainty.

### 5.5 Use distributions where they earn their keep

For a static decision with a genuinely linear points objective, maximising the expectation can often be solved directly from mean forecasts. Full simulation is not automatically necessary.

Distributions are valuable where decisions are nonlinear or sequential, including:

- captaincy and Triple Captain multipliers;
- bench order and autosub rules;
- Bench Boost;
- appearance thresholds and clean-sheet minute thresholds;
- correlated team and fixture outcomes;
- future information and the option to react later;
- wildcard and transfer-flexibility valuation;
- reporting blank, haul and downside probabilities.

The project should not spend computation on Monte Carlo simulation where an exact expectation or mathematical formulation is sufficient.

### 5.6 Separate facts, judgements, models and decisions

The system must distinguish:

- observed facts;
- reported claims;
- human or AI interpretation;
- accepted overrides;
- model parameters;
- optimiser outputs;
- the user's actual action.

### 5.7 Preserve provenance

Every material input should retain its source, timestamp and processing history.

### 5.8 Prevent look-ahead leakage

Backtests may use only data genuinely available before the relevant deadline.

### 5.9 Prefer transparent baselines first

Complexity is justified only when it demonstrates better out-of-sample decisions or calibration.

### 5.10 Remain provider-independent

External data sources should sit behind replaceable adapters.

### 5.11 Keep the user in the loop

Material judgement calls should be surfaced rather than hidden.

### 5.12 Collect irreplaceable data immediately

Point-in-time snapshots, news states, price states and pre-deadline forecasts cannot be recreated reliably later. Snapshot collection is time-critical even when the rest of the application is unfinished.

---

## 6. Current-season rules and configuration

FPL rules change between seasons. Scoring, chip inventory and transfer rules must be data-driven configuration, not assumptions embedded in model code.

### 6.1 Rule configuration

Each season should have versioned configuration files covering at least:

- squad size and position quotas;
- club limits;
- budget;
- legal formations;
- transfer costs;
- free-transfer accumulation cap;
- selling-price rules;
- scoring by position;
- appearance thresholds;
- goal, assist, clean-sheet, save and card scoring;
- defensive-contribution thresholds and caps;
- Bonus Points System rules;
- chip inventory, availability windows, expiry and interaction rules;
- any exceptional transfer grants;
- deadlines and phase boundaries.

### 6.2 2026/27 baseline

The initial 2026/27 configuration should reflect the published rules, including:

- up to five banked free transfers;
- two sets of Wildcard, Free Hit, Triple Captain and Bench Boost chips, one set for each half of the season;
- first-half chips expiring at the Gameweek 19 deadline;
- defensive contribution points remaining in the game;
- two defensive-contribution points for defenders reaching the configured CBIT threshold;
- two defensive-contribution points for midfielders and forwards reaching the configured CBIRT threshold;
- current-season Bonus Points System changes.

The application must validate the live official rules before every season and retain the exact configuration used for each historical run.

---

## 7. Operating cadence

### 7.1 Pre-season process

The pre-season optimiser uses an eight-Gameweek horizon.

It should consider:

- opening fixtures;
- expected early-season minutes;
- promoted-team and transferred-player uncertainty;
- prices and squad constraints;
- captaincy coverage;
- bench quality;
- probable transfer routes;
- likely wildcard windows;
- residual squad value after Gameweek 8.

Later Gameweeks should receive lower confidence and normally lower weight. The output should include:

- recommended 15-player squad;
- recommended starting XI for Gameweek 1;
- captain and vice-captain;
- key alternatives;
- provisional transfer triggers;
- structural risks;
- plausible first-wildcard windows;
- assumptions requiring review after early matches.

### 7.2 Weekly two-pass process

The weekly cycle should be treated as two connected decision passes.

#### Pass A — provisional planning

Normally run early in the Gameweek after the previous round is substantially complete.

1. Snapshot current squad and financial state.
2. Update structured player, team, fixture and price data.
3. Build provisional forecasts.
4. Run provisional optimisation.
5. Produce:
   - likely transfer routes;
   - price and affordability risks;
   - players whose status needs monitoring;
   - explicit decision triggers;
   - a list of information that could change the decision.

This pass supports planning. It is not the final recommendation.

#### Pass B — final deadline decision

Normally run after the main press conferences and as close to the deadline as practical.

1. Refresh structured data.
2. Run the AI-assisted news review.
3. Review material judgement calls.
4. Apply accepted manual availability, role or expected-minutes overrides.
5. Rebuild forecasts.
6. Run the final optimiser.
7. Generate the deadline report.
8. Freeze the final pre-deadline snapshot.

### 7.3 After the deadline

The system should record:

- the action actually taken;
- captain, vice-captain, XI and bench order actually selected;
- any deviation from the recommendation;
- the user's rationale for the deviation;
- whether the deviation was caused by new information arriving after the model run.

After the Gameweek it should ingest outcomes and score forecasts and decisions without rewriting the frozen snapshot.

---

## 8. System architecture

The system is divided into five logical layers.

```text
External sources
      │
      ▼
1. Facts and evidence
      │
      ▼
2. Features, rules and accepted judgements
      │
      ▼
3. Forecasting and scenario generation
      │
      ▼
4. Optimisation
      │
      ▼
5. Presentation, review and action capture
```

### 8.1 Facts and evidence

Stores observations without converting them directly into recommendations.

Examples:

- official prices and positions;
- fixtures and deadlines;
- minutes, goals, assists and defensive contributions;
- official xG, xA and related FPL fields;
- manager quotations;
- injury reports;
- predicted line-ups;
- cards and suspensions.

### 8.2 Features, rules and accepted judgements

Converts raw evidence into model-ready information.

Examples:

- rolling attacking rates;
- team strength estimates;
- fixture congestion;
- expected tactical role;
- penalty and set-piece responsibility;
- accepted expected-minutes override;
- current season scoring configuration;
- uncertainty classification.

### 8.3 Forecasting and scenario generation

Produces expected values and, where useful, distributions for:

- probability of starting;
- minutes if starting or benched;
- probability of no appearance;
- team goals and clean sheets;
- player goals and assists;
- defensive contributions;
- saves and bonus;
- full FPL points.

### 8.4 Optimisation

Chooses actions from stable forecast contracts and current game state. It must not contain hidden news interpretation.

A linear expected-value core should normally use exact constrained optimisation, such as mixed-integer linear programming, where the formulation permits it. Simulation should support nonlinear scoring, sequential option value and reporting rather than replacing exact optimisation unnecessarily.

### 8.5 Presentation, review and action capture

Provides:

- squad dashboard;
- player explorer;
- transfer planner;
- captain comparison;
- five-Gameweek outlook;
- news review screen;
- model-health reporting;
- deadline report;
- actual-action and deviation capture.

---

## 9. Data-source policy

### 9.1 Source requirements

Initial sources must be:

- free to access;
- technically usable without paid credentials;
- sufficiently stable for the intended purpose;
- used consistently with their published terms;
- replaceable through an adapter;
- timestamped on collection.

The project must not depend on scraping a source that prohibits the intended automated access.

### 9.2 Official FPL data

The official FPL data interface should be the primary source for:

- players, teams and positions;
- prices, ownership and transfer activity;
- fixtures, events and deadlines;
- current-season performance fields;
- official xG, xA, xGI and expected-goals-conceded fields where exposed;
- status, news and chance-of-playing fields;
- live and final FPL scoring outcomes;
- public manager history and post-deadline picks.

Because the official interface is not a guaranteed public developer contract, collectors must cache raw responses, use modest request rates and fail safely when schemas change.

### 9.3 Official football sources

Preferred for:

- club announcements;
- manager press conferences;
- injuries;
- suspensions;
- returns to training;
- fixture changes.

### 9.4 Free performance data

The first models should use official FPL performance fields before introducing fragile third-party dependencies. Richer free providers may be added only after checking:

- automated-access permission;
- coverage;
- historical depth;
- update timing;
- player identity quality;
- continued availability.

A third-party xG provider is therefore optional rather than a blocker for the first useful release.

### 9.5 Community historical datasets

Useful for prototyping and initial replay, but each field must be assessed for:

- timestamp availability;
- missing periods;
- definition changes;
- possible post-deadline leakage;
- player-identity consistency.

Historical datasets generally cannot reconstruct pre-deadline news, predicted line-ups or exact private squad state. Backtests that omit these inputs must be labelled as partial or optimistic.

### 9.6 Predicted-line-up sources

Predicted line-ups may inform expected minutes, but are forecasts rather than facts. The system should measure accuracy by provider, club and time horizon.

### 9.7 Betting data

Free and permitted market data may be incorporated where practical. The initial release must not require it.

### 9.8 Source registry

Every provider should have a registry entry containing:

- provider name;
- data category;
- access method;
- update cadence;
- source priority;
- licence or terms note;
- known limitations;
- retention policy;
- adapter implementation;
- active/inactive status.

---

## 10. Private squad-state acquisition

### 10.1 Practical constraint

Public manager endpoints do not reliably expose every piece of private pre-deadline state required by the optimiser, including exact current picks, selling prices, available free transfers and chip state. Authenticated endpoints rely on private session credentials and may be fragile or unsuitable for routine hosted automation.

### 10.2 Initial decision

The first release will use a small browser-based squad-state form as the authoritative pre-deadline input for:

- current 15-player squad;
- purchase price and selling price;
- money in the bank;
- available free transfers;
- available and used chips;
- current captain, vice-captain and bench order where useful.

The form should be quick to update, validate squad legality and prefill whatever can be inferred from public data.

This approach is deliberately less automated but more reliable and secure than storing an authentication cookie.

### 10.3 Later automation

An authenticated adapter may be added later if it can be implemented securely and maintained reliably. It must remain optional and must never commit cookies or credentials to the repository.

### 10.4 Actual-action capture

After the deadline, public picks may be used to verify the final squad and selection. The system should reconcile them with the recorded intended action and flag discrepancies.

---

## 11. Data architecture and provenance

### 11.1 Storage approach

Use DuckDB for analytical storage, with immutable timestamped raw snapshots retained separately.

Suggested storage groups:

- `raw/` — immutable source responses;
- `staging/` — normalised source tables;
- `core/` — canonical teams, players, fixtures and matches;
- `features/` — model-ready features;
- `forecasts/` — expected values and distributions;
- `decisions/` — optimiser inputs and outputs;
- `evaluation/` — outcomes and scores.

### 11.2 Core entities

At minimum:

- season and season-rule version;
- Gameweek/event and deadline;
- team and player;
- provider-player mapping;
- fixture and match appearance;
- FPL player snapshot;
- manager squad snapshot;
- purchase and selling price;
- free-transfer state;
- chip state;
- news evidence item;
- accepted judgement or override;
- expected-minutes forecast;
- component and total point forecast;
- optimisation run;
- recommended action;
- actual action and deviation reason;
- realised outcome;
- model, prompt and optimiser versions.

### 11.3 Temporal requirements

Every mutable observation must include:

- observation timestamp;
- effective timestamp where relevant;
- source identifier;
- ingestion-run identifier;
- raw snapshot reference.

Historical values must be preserved rather than overwritten.

### 11.4 Player identity

Players must use an internal stable identifier distinct from provider IDs. Mapping tables should record provider identifiers and identity confidence.

---

## 12. AI-assisted news review

### 12.1 Purpose

The AI news process is not a general FPL digest. Its job is to identify information that could materially change:

- availability;
- expected minutes;
- starting probability;
- tactical role;
- penalties or set pieces;
- near-term fixture outlook;
- transfer timing;
- chip decisions.

### 12.2 Research prompt contract

A reusable extended-thinking prompt should instruct ChatGPT to:

1. identify the Gameweek and deadline;
2. review developments since the previous deadline;
3. prioritise official club sources and direct quotations;
4. check injuries, suspensions, training updates and rotation clues;
5. consider domestic, European and cup minutes;
6. separate facts, reported claims and inference;
7. assess source reliability and recency;
8. identify affected current-squad players and realistic targets;
9. propose model adjustments only where justified;
10. flag material items requiring human judgement;
11. return validated structured output;
12. cite every non-trivial claim.

### 12.3 Required output fields

Each item should include:

| Field | Meaning |
|---|---|
| `internal_player_id` | Stable project player identifier |
| `provider_player_id` | Current official FPL identifier where available |
| `player_name` | Human-readable canonical name |
| `team` | Current club |
| `evidence_type` | Injury, suspension, manager quote, training, predicted line-up, tactical role, transfer or other |
| `fact_summary` | Concise factual statement |
| `source` | Source name and reference |
| `published_at` | Publication timestamp where available |
| `source_tier` | Official, strong reporting, predicted line-up or rumour |
| `confidence` | High, medium or low |
| `model_area` | Minutes, role, availability, set pieces, fixture or none |
| `suggested_adjustment` | Proposed model change, if any |
| `adjustment_basis` | Why the change follows from the evidence |
| `requires_decision` | Boolean |
| `decision_question` | Explicit question for the user |
| `expiry` | When the evidence should be reviewed again |
| `prompt_version` | Version of the research prompt used |
| `research_run_id` | Identifier linking all items from one session |

Unmatched or ambiguous player identities must fail validation rather than being guessed.

### 12.4 Governance

The news process must never silently overwrite a model parameter. Each accepted adjustment should store:

- original model value;
- proposed value;
- accepted value;
- decision maker;
- timestamp;
- rationale;
- linked evidence;
- prompt version.

### 12.5 Evaluation

The news layer should be evaluated by checking:

- identity-matching errors;
- missed material updates;
- unsupported adjustments;
- accuracy of availability and starting implications;
- calibration of source confidence;
- whether accepted overrides improved expected-minutes forecasts.

---

## 13. Forecasting models

### 13.1 Expected minutes

Estimate a distribution covering:

- probability of starting;
- minutes if starting;
- probability of substitute appearance;
- minutes if used as substitute;
- probability of no appearance.

Inputs may include recent starts, substitutions, injuries, congestion, competition for position, manager patterns, predicted line-ups and accepted overrides.

A minimal manual expected-minutes or availability override is required before the first usable decision engine is considered trustworthy.

### 13.2 Team strength

Maintain separate transparent estimates for:

- attacking strength;
- defensive strength;
- home advantage;
- opponent adjustment;
- recent-form uncertainty;
- promoted-team uncertainty.

### 13.3 Attacking returns

Estimate goals and assists using available features such as:

- official expected goals and expected assists;
- shots and key passes where permitted;
- penalty-area involvement where permitted;
- set-piece and penalty role;
- team attacking expectation;
- expected minutes;
- finishing and creation history with shrinkage.

### 13.4 Defensive scoring

Estimate all material defensive scoring components, including:

- clean-sheet probability;
- goals-conceded distribution;
- goalkeeper saves;
- defender attacking involvement;
- card and own-goal risk where useful;
- probability of reaching the applicable defensive-contribution threshold.

Defensive contributions are a first-class scoring component, not a minor adjustment.

### 13.5 Bonus

The initial bonus model may use transparent historical relationships with scoring events, clean sheets, saves, defensive actions, current BPS rules, expected minutes and player role. It must be versioned by season because the BPS can change.

### 13.6 Price and transfer timing

Initially the system should record current, purchase and selling prices; show affordability risks; distinguish “make now” from “wait for news”; and avoid recommending poor transfers solely for team value.

A dedicated price-change predictor is not required for the first useful release.

---

## 14. Uncertainty and simulation

### 14.1 Types of uncertainty

Distinguish:

- availability uncertainty;
- selection uncertainty;
- role uncertainty;
- performance uncertainty;
- team-strength uncertainty;
- fixture uncertainty;
- model uncertainty.

### 14.2 Outputs

Where useful, provide:

- mean expected points;
- median and percentile range;
- blank and haul probability;
- start and appearance probability;
- expected minutes;
- principal uncertainty drivers.

### 14.3 Decision treatment

The default objective is expected season value. Do not add an arbitrary fear penalty for variance.

Weak evidence may justify shrinkage toward conservative priors, but that is an estimation choice rather than a preference for “safe” players.

For linear static decisions, exact expected values may be sufficient. Scenario simulation should be introduced where it changes decisions, estimates recourse or option value, represents correlations, or improves communication.

---

## 15. Optimisation objective

### 15.1 Overall objective

> Maximise expected total FPL points from the current point to the end of the season.

Because detailed forecasts for the entire remaining season are not credible, use rolling-horizon optimisation.

### 15.2 Weekly rolling objective

For each candidate action, estimate:

```text
Decision value
= expected points inside the active horizon
+ differential residual squad value beyond the horizon
+ value of retained money and transfer flexibility
+ option value of remaining chips
- transfer-hit costs
- structural penalties
```

Residual value must not double-count points or fixture effects already included inside the active horizon.

### 15.3 Horizon

The default weekly horizon is five Gameweeks. Exact weighting should be configurable and validated.

The horizon may extend when a known blank, double, chip expiry or major fixture swing falls just beyond Gameweek 5 and materially affects current decisions.

### 15.4 Residual or terminal value

The optimiser only needs a defensible estimate of the **difference** in residual value between candidate actions, not a perfect absolute valuation of every squad.

Consider:

- relative projected squad quality after the horizon;
- money in the bank;
- purchase and selling value;
- free transfers retained;
- future fixture quality not already counted;
- captaincy coverage;
- squad flexibility;
- remaining chips;
- injury and minutes risk;
- expected need for corrective transfers.

The first version may use transparent heuristics. Later versions should estimate differences through historical replay and sequential simulation.

### 15.5 Transfer flexibility

A saved transfer is an option whose marginal value depends on state, including:

- current number of free transfers;
- the five-transfer storage cap;
- likelihood that new information creates a useful move;
- value of combining transfers;
- ability to repair injuries without a hit;
- known fixture swings;
- bank and squad structure.

Rolling from four free transfers to five may still have meaningful value; it is not assumed to be near zero. When already at five, rolling creates no sixth transfer, but preserves the existing five and avoids spending one. The model should estimate these marginal values rather than apply a blanket rule.

### 15.6 Candidate actions

The mature optimiser should compare:

- roll;
- one free transfer;
- multiple free transfers where available;
- transfers for a hit;
- Wildcard;
- Free Hit;
- Bench Boost;
- Triple Captain;
- no-chip baseline.

The first useful release may restrict transfer search to rolling and one transfer, but its report must explicitly state that unsearched multi-transfer routes have not been ruled out.

### 15.7 Optimisation method

Use exact constrained optimisation where the objective and constraints are linear or can be represented cleanly as a mixed-integer programme. Use scenario or stochastic methods only for genuinely nonlinear, correlated or sequential components.

---

## 16. Chip modelling

Chip inventory and expiry are season configuration.

### 16.1 Wildcard

Search over a longer horizon than an ordinary transfer and compare immediate value with waiting, forthcoming fixture swings, expected information and future flexibility.

### 16.2 Free Hit

Treat as a one-Gameweek squad optimisation plus the opportunity cost of losing the chip for a later blank or double.

### 16.3 Bench Boost

Account for bench minutes, bench quality, doubles, transfers required and the opportunity cost of maintaining an unusually expensive bench.

### 16.4 Triple Captain

Compare the current captain distribution with plausible future opportunities and their uncertainty.

### 16.5 Initial implementation

The first useful release may display chip state and warnings without claiming a solved season-level chip schedule.

---

## 17. Recommendation outputs

Every final deadline report should contain:

### 17.1 Executive recommendation

- recommended transfer action;
- recommended chip action;
- captain and vice-captain;
- starting XI;
- bench order;
- recommended timing.

### 17.2 Decision value

- expected gain over the active horizon;
- hit-adjusted value;
- terminal-value difference;
- uncertainty summary;
- affordability effect;
- transfer-flexibility effect.

### 17.3 Alternatives

- best alternative transfer route within the searched action space;
- best no-transfer route;
- best lower-cost or structure-preserving route where relevant;
- clear disclosure of any action classes not searched.

### 17.4 Assumptions

- expected-minutes assumptions;
- accepted news judgements;
- uncertain fixtures;
- penalties and set pieces;
- price assumptions;
- model, rules and prompt versions.

### 17.5 Decision triggers

State what new information would materially change the recommendation.

---

## 18. Dashboard requirements

The browser dashboard should eventually include:

- **My Team:** squad, bank, selling prices, expected points, minutes, XI, bench, captaincy and news flags;
- **Transfer Planner:** roll comparison, transfer routes, horizon gains, affordability, hits and residual effects;
- **Player Explorer:** projections, uncertainty, role, fixtures, performance features and evidence;
- **Fixture Planner:** team forecasts, swings, blanks, doubles and uncertainty;
- **News Review:** evidence, proposed adjustments, accept/reject/edit controls and audit history;
- **Model Health:** calibration, minutes accuracy, data freshness, replay and version comparison;
- **Deadline Report:** mobile-readable report linked to the frozen run;
- **Action Capture:** record what was actually done and why it differed.

Streamlit is suitable for the first version. Replacing it is a later concern only if the interface outgrows it.

---

## 19. Reproducibility and backtesting

### 19.1 Reproducible run

A historical recommendation must be reproducible from:

- data snapshot identifiers;
- squad-state snapshot;
- accepted news decisions;
- feature and model versions;
- optimiser version;
- prompt version;
- configuration and season rules;
- random seed where used;
- run timestamp.

### 19.2 Immutable deadline snapshots

Once the deadline passes, the snapshot used for the recommendation must not be altered. Corrections create a new record while preserving the original.

### 19.3 Historical replay limitations

Historical replay should use information available at the time, but community datasets often lack complete pre-deadline news, exact price timing and private squad state. Replay results must record which evidence classes were unavailable.

### 19.4 Baselines

Useful baselines include:

- make no transfers;
- choose highest recent FPL points;
- choose highest official form;
- choose highest simple expected points;
- captain the highest projected player;
- always avoid hits;
- simple fixture-ticker strategy.

### 19.5 Snapshot priority

Collectors should start as soon as the repository foundation exists, even if the modelling and dashboard are incomplete. Each missed deadline is validation data that cannot be fully recovered.

---

## 20. Technical architecture

### 20.1 Proposed repository structure

```text
fpl-decision-engine/
├── config/
│   ├── seasons/
│   ├── sources/
│   └── models/
├── docs/
│   ├── 00_ProjectSpecification.md
│   ├── 01_Roadmap.md
│   ├── 02_DataSources.md
│   ├── 03_DataModel.md
│   ├── 04_Modelling.md
│   ├── 05_Optimisation.md
│   └── 06_Backtesting.md
├── data/
│   ├── raw/
│   ├── snapshots/
│   └── local/
├── src/fpl_engine/
│   ├── collectors/
│   ├── database/
│   ├── news/
│   ├── features/
│   ├── models/
│   ├── simulation/
│   ├── optimiser/
│   ├── reports/
│   └── dashboard/
├── tests/
├── scripts/
├── notebooks/
├── .github/workflows/
├── pyproject.toml
├── README.md
└── .env.example
```

### 20.2 Tooling

Initial stack:

- Python;
- DuckDB;
- pandas or Polars;
- Pydantic;
- a mixed-integer optimisation library selected during implementation;
- Streamlit;
- pytest;
- Ruff;
- type checking where practical;
- GitHub Actions;
- GitHub Codespaces.

### 20.3 Module boundaries

Collectors must not contain modelling logic. Models must not perform web research. The optimiser must consume stable forecast contracts. The dashboard must not recalculate hidden values. Reports must reference stored run outputs.

---

## 21. Deployment and privacy

### 21.1 First useful workflow

The first useful release must be runnable from a browser-based development environment such as GitHub Codespaces with a documented one-command launch. It should not require the user to install Python locally.

Fully unattended scheduling and private hosted deployment are desirable, but they must not delay the first real deadline run.

### 21.2 Mature workflow

```text
GitHub Actions or hosted scheduler
          │
          ▼
Data update and model run
          │
          ▼
Private hosted dashboard
          │
          ▼
Browser or mobile access
```

### 21.3 Privacy

Do not commit credentials, cookies, secrets or unnecessary personal data. Use GitHub secrets or hosted environment variables. The initial manual squad-state form should store only the information necessary for the project.

---

## 22. Testing and quality standards

### 22.1 Unit tests

Required for:

- season scoring rules, including defensive contributions;
- chip inventory and expiry;
- free-transfer accumulation and cap;
- selling-price and hit calculations;
- squad legality and formations;
- captain, vice-captain and autosubs;
- probability aggregation;
- terminal-value calculations;
- source parsers.

### 22.2 Integration tests

Required for:

- official data ingestion;
- season-rule loading;
- database migrations;
- squad-state form validation;
- override application;
- forecast generation;
- optimisation from a known state;
- deadline snapshot creation;
- report generation.

### 22.3 Regression tests

A fixed set of historical or synthetic Gameweeks should detect unexplained changes in forecasts, transfers, captaincy, feasibility and report content.

### 22.4 Data-quality checks

Include duplicate players, missing fixtures, impossible minutes, stale snapshots, unmapped identities, invalid prices, invalid probability totals, look-ahead timestamps, untraceable overrides and unknown rule versions.

### 22.5 CI policy

Every pull request should run formatting, linting, unit tests, credential-free integration tests, schema validation and documentation checks where practical.

---

## 23. Development roadmap

### Phase 0 — Specification and repository foundation

- approve the project specification;
- establish repository structure, coding standards and CI;
- create the development environment;
- add initial 2026/27 season-rule configuration;
- establish issue and pull-request workflow.

### Phase 1 — Time-critical collection and squad state

- official FPL collector;
- fixtures, teams, players, events and official performance fields;
- immutable raw snapshots from day one;
- DuckDB schema;
- manual browser-based manager-state input;
- public manager reconciliation where available;
- data freshness checks;
- tests.

This phase should begin immediately because missed point-in-time data cannot be recovered fully.

### Phase 2 — Baseline five-Gameweek projections

- transparent team-strength model;
- baseline expected-minutes model;
- component expected-points model, including defensive contributions;
- uncertainty fields;
- fixture projections;
- stored forecasts.

### Phase 3 — First usable decision engine

- minimal manual availability, role and expected-minutes overrides with rationale;
- squad optimiser;
- starting XI and bench order;
- captain and vice-captain;
- roll versus one-transfer comparison;
- basic hit assessment;
- transparent residual-value approximation;
- browser dashboard runnable in Codespaces;
- deadline report with search-scope disclosure.

This is the first release that should materially support the user's weekly team.

### Phase 4 — Full news workflow

- news evidence schema;
- reusable versioned ChatGPT research prompt;
- structured import and identity validation;
- judgement-review interface;
- decision triggers;
- news-layer evaluation.

### Phase 5 — Multi-transfer and exact optimisation

- mixed-integer optimiser for broader candidate search;
- multi-transfer routes;
- improved transfer-flexibility valuation;
- better residual-value model.

### Phase 6 — Scenario modelling

- distributions and correlated scenarios where useful;
- nonlinear captain, bench and chip treatment;
- sequential information and recourse experiments;
- probability reporting.

### Phase 7 — Pre-season and wildcard optimisation

- eight-Gameweek pre-season optimiser;
- wildcard comparison;
- structural squad metrics;
- wildcard-window analysis.

### Phase 8 — Chip planning

- Free Hit, Bench Boost and Triple Captain models;
- future-opportunity valuation;
- season-level chip option value.

### Phase 9 — Richer permitted data

- provider feasibility assessment;
- adapters only for permitted and stable sources;
- model comparisons against the official-data baseline;
- provider-quality monitoring.

### Phase 10 — Continuous validation and hosting

- historical replay with evidence-availability labels;
- forecast calibration;
- recommendation scoring;
- model-version comparisons;
- automated model-health reports;
- fully hosted private workflow if worthwhile.

---

## 24. First useful release acceptance criteria

The first useful release is complete when it can reliably:

1. load the user's current legal squad and financial state through a browser form;
2. update official FPL players, teams, fixtures and deadlines;
3. retain immutable timestamped snapshots;
4. load versioned current-season scoring, transfer and chip rules;
5. project expected minutes and component expected points for five Gameweeks;
6. include defensive-contribution scoring;
7. accept a manual availability, role or expected-minutes override with rationale;
8. show uncertainty and key assumptions;
9. recommend XI, bench order, captain and vice-captain;
10. compare rolling with realistic one-transfer options;
11. state clearly that two-transfer combinations have not yet been exhausted;
12. account for transfer costs, selling prices and the free-transfer cap;
13. include a transparent initial residual-value difference;
14. generate a readable deadline report;
15. freeze the recommendation before the deadline;
16. record the action actually taken and any deviation reason;
17. score forecasts after the Gameweek;
18. run from Codespaces or another browser environment without local Python installation;
19. pass automated tests for game rules and optimiser constraints.

Fully hosted unattended operation, complete multi-transfer search, advanced chip planning and machine learning are not required for this milestone.

---

## 25. Remaining design work

The main philosophy and scope are settled. The following should be resolved through implementation research and validation:

1. exact Gameweek weighting;
2. dynamic horizon-extension rules;
3. initial residual-value formula and double-counting safeguards;
4. state-dependent transfer-flexibility valuation;
5. expected-minutes baseline specification;
6. mixed-integer optimisation library and formulation;
7. where scenario simulation materially improves decisions;
8. format of the manual squad-state input;
9. format used to import ChatGPT news review output;
10. exact update and deadline schedule;
11. hosted platform after the first useful release;
12. whether any richer free provider improves the official-data baseline enough to justify its maintenance risk.

Each design decision should record alternatives, rationale, limitations, validation and reconsideration triggers.

---

## 26. Review notes incorporated in version 0.2

This revision incorporates external review concerning:

- 2026/27 chips, free transfers, defensive contributions and season-configured rules;
- the authenticated squad-state problem and the initial manual-entry decision;
- official FPL expected-stat fields;
- time-critical snapshot collection;
- a two-pass weekly process;
- capture of actual actions and deviations;
- an early manual news/minutes override;
- dynamic horizons around blanks and doubles;
- differential rather than absolute terminal value;
- prevention of terminal-value double-counting;
- the small live-season decision sample;
- stable player IDs and prompt versioning in AI news output;
- exact linear optimisation and selective use of simulation;
- separating browser-based first use from later full hosting.

Two review claims were refined rather than copied literally:

1. Distributions are not needed for every linear static decision, but they can matter beyond chips and autosubs when future information, recourse, correlations or option value are modelled.
2. Transfer flexibility is state-dependent, but rolling from four free transfers to five is not assumed to be near zero; only the accumulation of an additional transfer is blocked once already at the five-transfer cap.
