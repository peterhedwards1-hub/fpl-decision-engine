# FPL Decision Engine — Project Specification

**Status:** Draft for review  
**Version:** 0.1  
**Primary user:** Peter Edwards  
**Repository:** `peterhedwards1-hub/fpl-decision-engine`

---

## 1. Executive summary

The FPL Decision Engine is a private, data-driven decision-support system for one Fantasy Premier League team.

Its purpose is:

> **To maximise total season points by making the best available decision before each FPL deadline, using only information that was available at that time.**

The system is not intended to maximise novelty, entertainment, differential ownership, social-media engagement or mini-league psychology. Those factors matter only where they change expected season points.

The system will combine:

1. structured FPL, fixture and performance data;
2. an AI-assisted review of current squad and team news;
3. explicit models of expected minutes, attacking returns, defensive returns and uncertainty;
4. rolling-horizon optimisation of transfers, captaincy, starting XI, bench order and chips;
5. a browser-based dashboard and deadline report;
6. permanent storage of pre-deadline forecasts and recommendations for later validation.

The project will be built as an auditable software system rather than a collection of isolated notebooks. Every recommendation should be explainable, reproducible and testable.

---

## 2. Decisions already agreed

The following decisions are considered settled unless later evidence justifies changing them.

| Area | Decision |
|---|---|
| Primary user | One private user and one FPL team |
| Primary objective | Maximise total season points |
| Normal planning horizon | Rolling five-Gameweek horizon |
| Pre-season horizon | Eight Gameweeks |
| Replanning cadence | Re-run before every deadline as new information becomes available |
| Decision style | Maximise modelled expected season value rather than pursue a fixed conservative or aggressive identity |
| Uncertainty | Represent explicitly inside the model and simulations |
| News process | ChatGPT extended-thinking research session produces structured evidence and flags judgement calls |
| Human role | Review material or ambiguous news adjustments before final optimisation |
| Data cost | Use free and accessible data sources; no paid-source dependency |
| Interface | Hosted browser dashboard plus generated deadline report |
| Visibility | Private initially |
| Reproducibility | Store the exact information, models and outputs used before each deadline |

---

## 3. Project scope

### 3.1 In scope

The system should support the complete recurring decision cycle for the user's FPL team:

- import the current squad, purchase prices, selling prices, money in the bank, available transfers and chips;
- update fixtures, player data, team data and recent performance;
- collect and structure relevant injury, suspension, selection and tactical news;
- estimate expected minutes for each relevant player;
- estimate player point distributions over forthcoming Gameweeks;
- evaluate current squad quality;
- recommend transfers or recommend rolling;
- assess whether a points hit is justified;
- optimise the starting XI and bench order;
- recommend captain and vice-captain;
- maintain a provisional five-Gameweek route while recognising that it will be updated next week;
- support pre-season squad construction over an eight-Gameweek horizon;
- evaluate wildcard and other chip options;
- record forecasts and recommendations before deadlines;
- compare forecasts and decisions with subsequent outcomes.

### 3.2 Out of scope for the initial project

The first versions will not attempt to:

- provide accounts for multiple public users;
- optimise for social-media consensus or ownership for its own sake;
- recommend differentials merely because they are differentials;
- predict exact price-change thresholds as a core requirement;
- depend on paid data feeds;
- place FPL transfers automatically;
- publish an autonomous news judgement directly into the model without an audit trail;
- train an opaque machine-learning model before a reliable baseline and validation system exist;
- claim that a five-Gameweek plan is a fixed commitment.

### 3.3 Possible later extensions

- mini-league strategy as a separate layer over the core point-maximisation model;
- public multi-user deployment;
- paid or licensed data providers where they offer clear value;
- richer live-rank and effective-ownership analysis;
- automated alerts when material new information changes the recommendation;
- ensemble machine-learning models after enough clean historical snapshots exist.

---

## 4. Success definition

The project succeeds if it improves the quality of decisions taken for the user's team and thereby increases expected total season points.

This must be evaluated at two separate levels.

### 4.1 Forecast quality

The system should measure:

- expected-minutes error;
- start-probability calibration;
- probability of any appearance;
- expected-points calibration;
- clean-sheet probability calibration;
- goal and assist probability calibration;
- uncertainty calibration;
- ranking quality among realistic transfer targets.

### 4.2 Decision quality

The system should measure:

- realised and expected value of recommended transfers after hit costs;
- value of rolling versus transferring;
- captaincy decision performance;
- starting-XI and bench-order performance;
- wildcard and chip performance;
- comparison with simple baselines;
- comparison with the user's actual chosen action where it differed from the recommendation;
- regret: the value lost relative to the best decision that was reasonably available at the deadline.

Prediction accuracy is an input to decision quality, not the final purpose of the system.

---

## 5. Core principles

### 5.1 Optimise decisions, not headlines

The system exists to choose actions, not merely to publish player rankings.

### 5.2 Maximise season value, not one-week points

A move that gains points this week may still be poor if it damages squad structure, consumes a useful transfer, blocks a future move or leaves a weak squad beyond the visible horizon.

### 5.3 Replan as information arrives

The five-Gameweek route is provisional. The system should expect injuries, tactical changes, fixture rearrangements and new evidence to alter it.

### 5.4 Represent uncertainty explicitly

A player forecast should be a distribution of plausible outcomes, not just a single decimal point estimate.

### 5.5 Separate facts, judgements, models and decisions

The system must distinguish:

- observed facts;
- reported claims;
- human or AI interpretation;
- model parameters;
- optimiser outputs.

### 5.6 Preserve provenance

Every material input should retain its source, timestamp and processing history.

### 5.7 Prevent look-ahead leakage

Backtests may use only data that was genuinely available before the relevant deadline.

### 5.8 Prefer transparent baselines first

Complexity is justified only when it demonstrates better out-of-sample decisions.

### 5.9 Remain provider-independent

External data sources should sit behind replaceable adapters so that a source can be changed without rewriting the models.

### 5.10 Keep the user in the loop for ambiguous evidence

The system should surface material judgement calls rather than hide them.

---

## 6. Operating cadence

## 6.1 Pre-season process

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

Later Gameweeks should receive lower confidence and normally lower weight than the opening weeks.

The output should include:

- recommended 15-player squad;
- recommended starting XI for Gameweek 1;
- captain and vice-captain;
- key alternatives;
- provisional transfer triggers;
- identified structural risks;
- plausible first-wildcard windows;
- assumptions requiring review after the first matches.

The pre-season plan is a strong starting position, not a promise to retain players for eight weeks.

## 6.2 Weekly process

Each weekly cycle should follow this order:

1. **Snapshot the current state**
   - squad;
   - selling prices;
   - bank;
   - free transfers;
   - chips;
   - rank and points for record-keeping.

2. **Update structured data**
   - official player data;
   - fixtures and deadlines;
   - recent match data;
   - team and player performance features;
   - suspensions and availability flags.

3. **Run AI-assisted news collection**
   - search official club updates, manager comments and reliable reporting;
   - identify likely changes to availability, role or minutes;
   - distinguish fact from inference;
   - flag explicit decisions.

4. **Review material judgement calls**
   - accept, reject or amend proposed expected-minutes or role adjustments;
   - record the decision and rationale.

5. **Build forecasts**
   - expected minutes;
   - team scoring and conceding distributions;
   - player point distributions;
   - uncertainty by source.

6. **Run the optimiser**
   - roll or transfer;
   - one or more transfers;
   - hits;
   - starting XI;
   - bench order;
   - captain and vice-captain;
   - chip options.

7. **Generate the deadline report**
   - primary recommendation;
   - alternatives;
   - assumptions;
   - expected gain;
   - uncertainty;
   - events that would change the decision.

8. **Freeze a pre-deadline snapshot**
   - data version;
   - news decisions;
   - model version;
   - random seed;
   - forecasts;
   - recommendation.

9. **After the Gameweek**
   - ingest outcomes;
   - score forecasts;
   - score decisions;
   - record lessons without rewriting the historical snapshot.

---

## 7. System architecture

The system is divided into five logical layers.

```text
External sources
      │
      ▼
1. Facts and evidence layer
      │
      ▼
2. Feature and judgement layer
      │
      ▼
3. Forecasting and simulation layer
      │
      ▼
4. Optimisation layer
      │
      ▼
5. Presentation and review layer
```

## 7.1 Facts and evidence layer

Stores observations without converting them directly into recommendations.

Examples:

- official prices;
- fixture dates;
- minutes played;
- goals and assists;
- manager quotations;
- injury reports;
- predicted line-ups;
- team formations;
- cards and suspensions.

## 7.2 Feature and judgement layer

Converts raw evidence into model-ready information.

Examples:

- rolling attacking rates;
- team strength estimates;
- fixture congestion;
- expected role;
- penalty and set-piece responsibility;
- accepted expected-minutes adjustment from team news;
- uncertainty classification.

## 7.3 Forecasting and simulation layer

Produces probability distributions for future outcomes.

Examples:

- probability of starting;
- expected minutes conditional on starting or benching;
- team goal distribution;
- clean-sheet probability;
- player goal and assist probability;
- save and bonus distributions;
- complete FPL point distribution.

## 7.4 Optimisation layer

Chooses actions from forecasts and current game state.

It should not contain hidden news interpretation or subjective player opinions.

## 7.5 Presentation and review layer

Provides:

- squad dashboard;
- player explorer;
- transfer planner;
- captain comparison;
- five-Gameweek outlook;
- news review screen;
- model-health reporting;
- generated deadline report.

---

## 8. Data-source policy

## 8.1 Source requirements

Initial sources must be:

- free to access;
- technically usable without paid credentials;
- sufficiently stable for the intended purpose;
- used in a manner consistent with their published access terms;
- replaceable through an adapter;
- timestamped on collection.

The project must not depend on a source that prohibits the intended automated access.

## 8.2 Source hierarchy

### Authoritative game-state sources

Preferred for:

- players and teams;
- official prices;
- FPL positions;
- ownership and transfers;
- fixtures and deadlines;
- current squad and chip state;
- official scoring outcomes.

The official FPL data interface should be the primary source where available.

### Official football sources

Preferred for:

- club announcements;
- manager press conferences;
- injuries;
- suspensions;
- returns to training;
- fixture changes.

### Free performance-data sources

Used for richer features such as:

- expected goals;
- expected assists;
- shots and key passes;
- penalty-area involvement;
- team expected goals and expected goals conceded;
- set pieces and penalties where available.

A feasibility task must select one or more free sources after checking accessibility, coverage, historical depth and terms of use.

### Community historical datasets

Useful for initial backtesting and model prototyping, but each field must be assessed for:

- timestamp availability;
- missing periods;
- definition changes;
- possible post-deadline leakage;
- player-identity consistency.

### Predicted-line-up and team-news sources

May inform expected minutes, but should be treated as forecasts rather than facts. Their accuracy should be measured by club and over time.

### Betting data

Free and accessible market data may be incorporated where legally and technically available. The initial release must not require it.

## 8.3 Source registry

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

## 9. Data architecture and provenance

## 9.1 Storage approach

The initial system should use DuckDB for analytical storage, with timestamped raw snapshots retained separately.

Suggested storage groups:

- `raw/` — immutable source responses;
- `staging/` — normalised source tables;
- `core/` — canonical teams, players, fixtures and matches;
- `features/` — model-ready features;
- `forecasts/` — player and team distributions;
- `decisions/` — optimiser inputs and outputs;
- `evaluation/` — outcomes and model scores.

## 9.2 Core entities

At minimum:

- season;
- Gameweek/event;
- deadline;
- team;
- player;
- fixture;
- match appearance;
- FPL player snapshot;
- manager squad snapshot;
- purchase and selling price;
- free-transfer state;
- chip state;
- news evidence item;
- judgement decision;
- expected-minutes forecast;
- player-points forecast;
- optimisation run;
- recommended action;
- actual action;
- realised outcome;
- model version.

## 9.3 Temporal requirements

Every mutable observation must have:

- `observed_at`;
- `effective_from` where relevant;
- `source_id`;
- `ingestion_run_id`;
- raw snapshot reference.

The database must preserve historical values rather than overwrite them.

## 9.4 Player identity

Players must use an internal stable identifier distinct from any provider-specific identifier. Mapping tables should record provider IDs and identity confidence.

This is essential across:

- season changes;
- transfers between clubs;
- spelling variations;
- duplicate names;
- promoted teams;
- external data providers.

---

## 10. AI-assisted news review

## 10.1 Purpose

The AI news process is not a general FPL news summary. Its job is to identify information that could materially change:

- availability;
- expected minutes;
- starting probability;
- position or tactical role;
- penalty or set-piece responsibility;
- near-term fixture outlook;
- transfer timing;
- chip decisions.

## 10.2 Research prompt contract

A reusable extended-thinking prompt should instruct ChatGPT to:

1. identify the current Gameweek and deadline;
2. review developments since the previous deadline;
3. prioritise official club sources and direct manager quotations;
4. check relevant injuries, suspensions, training updates and rotation clues;
5. consider domestic and European minutes, tactical changes and new signings;
6. separate confirmed facts, reported claims and inference;
7. assess source reliability and recency;
8. identify affected players in the current squad and realistic transfer targets;
9. propose explicit model adjustments only where justified;
10. flag every material item that requires human judgement;
11. return structured output suitable for ingestion;
12. include citations or source references for every non-trivial claim.

## 10.3 Required output fields

Each news item should contain:

| Field | Meaning |
|---|---|
| `player` | Canonical player name |
| `team` | Current club |
| `evidence_type` | Injury, suspension, manager quote, training, predicted line-up, tactical role, transfer, other |
| `fact_summary` | Concise factual statement |
| `source` | Source name and reference |
| `published_at` | Publication timestamp where available |
| `source_tier` | Official, strong reporting, predicted line-up, rumour |
| `confidence` | High, medium or low |
| `model_area` | Minutes, role, availability, set pieces, fixture, none |
| `suggested_adjustment` | Proposed model change, if any |
| `adjustment_basis` | Why the change follows from the evidence |
| `requires_decision` | Boolean |
| `decision_question` | Explicit question for the user |
| `expiry` | When the evidence should be reviewed again |

## 10.4 Decision examples

Examples of items requiring explicit review:

- a manager says a player is “a doubt” without a probability estimate;
- two credible predicted line-ups disagree;
- a player has returned to training but match fitness is unclear;
- a recent tactical change may alter the player's position;
- an early transfer could avoid a price change but increases injury exposure;
- a player may be rested after European minutes.

## 10.5 Governance

The news process must never silently overwrite a model parameter.

Each accepted adjustment should store:

- original model value;
- proposed value;
- accepted value;
- decision maker;
- timestamp;
- rationale;
- linked evidence.

---

## 11. Forecasting models

## 11.1 Expected-minutes model

Expected minutes are the most important player-level input.

The model should estimate a distribution covering:

- probability of starting;
- minutes if starting;
- probability of substitute appearance;
- minutes if used as substitute;
- probability of no appearance.

Inputs may include:

- recent starts and minutes;
- substitutions;
- injuries and returns;
- fixture congestion;
- European and cup participation;
- competition for position;
- manager selection patterns;
- predicted line-ups;
- accepted news adjustments;
- historical recovery and rotation patterns where useful.

Uncertainty should increase where:

- the player is newly signed;
- the manager has recently changed;
- the player is returning from injury;
- tactical roles are unstable;
- predicted sources disagree;
- data is sparse.

## 11.2 Team-strength model

Separate estimates should exist for:

- attacking strength;
- defensive strength;
- home advantage;
- opponent adjustment;
- recent-form uncertainty;
- promoted-team uncertainty.

The initial model should be transparent and dynamically updated. More complex Bayesian or state-space approaches may follow after the baseline is validated.

## 11.3 Player attacking model

Estimate goal and assist involvement using free available features such as:

- expected goals;
- expected assists;
- shots;
- shots in the box;
- key passes;
- penalty-area touches;
- set-piece role;
- penalty responsibility;
- team attacking expectation;
- expected minutes;
- historical finishing and creation, with shrinkage.

## 11.4 Defensive model

Estimate:

- clean-sheet probability;
- goals conceded distribution;
- goalkeeper save distribution;
- defender attacking involvement;
- card and own-goal risk where useful.

## 11.5 Bonus model

The initial bonus model may use transparent historical relationships with:

- scoring events;
- clean sheets;
- saves;
- baseline bonus-point-system rates;
- expected minutes;
- player role.

It should later be tested against simulation of match events.

## 11.6 Price and transfer-timing model

Price changes are secondary to expected points but can influence future affordability.

Initially the system should:

- record current price, purchase price and selling price;
- show likely affordability risks;
- distinguish “make now” from “wait for news” considerations;
- avoid recommending an otherwise poor transfer solely for team value.

A dedicated price-prediction model is not required for the first useful release.

---

## 12. Uncertainty and simulation

## 12.1 Types of uncertainty

The system should distinguish:

- **availability uncertainty** — injury, illness or suspension;
- **selection uncertainty** — start, bench or no appearance;
- **role uncertainty** — position, penalties, set pieces or tactical function;
- **performance uncertainty** — normal match-to-match variation;
- **team-strength uncertainty** — uncertainty in attack and defence estimates;
- **fixture uncertainty** — postponements, blanks, doubles or scheduling changes;
- **model uncertainty** — uncertainty caused by sparse or poor-quality data.

## 12.2 Simulation output

For each player and squad decision, the system should provide:

- mean expected points;
- median points;
- useful percentile range;
- blank probability;
- haul probability;
- start probability;
- expected minutes;
- principal uncertainty drivers.

## 12.3 Decision treatment

The default objective is expected season value. Uncertainty should normally affect that expectation through the simulated outcomes rather than through an arbitrary additional fear penalty.

However, model uncertainty may justify shrinkage toward conservative priors where evidence is weak.

The interface should display uncertainty even where it does not change the optimiser's chosen action.

---

## 13. Optimisation objective

## 13.1 Overall objective

The theoretical objective is:

> Maximise expected total FPL points from the current point to the end of the season.

It is neither practical nor credible to forecast every remaining Gameweek in detail. The system will therefore use rolling-horizon optimisation.

## 13.2 Weekly rolling objective

For each candidate action, estimate:

```text
Decision value
= expected points during the next five Gameweeks
+ residual squad value after Gameweek 5
+ value of retained money and transfer flexibility
+ option value of remaining chips
- transfer-hit costs
- structural penalties
```

This is an approximation of season value, not merely five-Gameweek points.

## 13.3 Five-Gameweek weighting

The model may give later weeks lower weight or higher uncertainty. Exact weights should be configurable and validated rather than permanently fixed in the specification.

The default design assumption is:

- Gameweek 1 has the highest confidence;
- confidence decreases progressively through Gameweek 5;
- known fixtures remain relevant even when precise player forecasts become less certain.

## 13.4 Residual or terminal value

A five-Gameweek optimiser can become shortsighted. The terminal-value model should therefore consider:

- projected squad quality immediately after the horizon;
- age of planned transfers only where relevant to minutes or value;
- money in the bank;
- purchase and selling value;
- number of free transfers retained;
- future fixture quality;
- captaincy coverage;
- squad flexibility;
- remaining chips;
- concentration of uncertain or injury-prone players;
- expected need for corrective transfers.

The first version may use transparent heuristic values. Later versions should estimate these values through historical replay and simulation.

## 13.5 Transfer flexibility

A saved transfer is an option, not a fixed number of points.

Initially, the engine may assign a provisional value based on:

- probability that new information creates a useful move next week;
- value of combining transfers;
- ability to repair injuries without a hit;
- known fixture swings;
- available bank and squad structure.

The project should avoid burying a permanent arbitrary “free transfer value” in code.

## 13.6 Candidate actions

The optimiser should compare at least:

- roll;
- one free transfer;
- multiple free transfers where available;
- one or more transfers for a hit;
- wildcard;
- Free Hit;
- Bench Boost;
- Triple Captain;
- no-chip baseline.

The search space may be restricted intelligently for performance, but candidate generation must not exclude obvious realistic routes.

## 13.7 FPL constraints

The optimiser must enforce the rules applicable to the current season, including:

- squad size;
- formation;
- club limits;
- budget;
- position quotas;
- captain and vice-captain;
- transfer costs;
- free-transfer state;
- selling-price rules;
- chip rules;
- deadlines.

Rules must be configurable by season rather than assumed permanently.

---

## 14. Chip modelling

Chips affect season strategy and must be recognised even before the final advanced chip optimiser exists.

## 14.1 Wildcard

The wildcard optimiser should:

- search over a longer horizon than an ordinary transfer;
- account for current squad problems;
- compare immediate points with the value of waiting;
- consider forthcoming fixture swings and expected information;
- retain a residual value for future flexibility.

## 14.2 Free Hit

The Free Hit should be assessed as a one-Gameweek squad optimisation problem plus the opportunity cost of losing the chip for a later blank or double.

## 14.3 Bench Boost

Bench Boost value depends on:

- expected bench minutes;
- bench quality;
- double Gameweeks;
- opportunity cost of maintaining an unusually expensive bench;
- interaction with transfers or wildcard timing.

## 14.4 Triple Captain

Triple Captain should compare the current captain distribution with plausible future opportunities and the uncertainty around those opportunities.

## 14.5 Initial implementation

The first useful release may present chip comparisons without claiming a fully solved season-level chip schedule. All chip logic should be explicit about assumptions.

---

## 15. Recommendation outputs

Every deadline report should contain the following.

## 15.1 Executive recommendation

- recommended transfer action;
- recommended chip action;
- captain and vice-captain;
- starting XI;
- bench order;
- recommended timing, such as “wait for press conference” or “decision is stable”.

## 15.2 Decision value

- expected five-Gameweek gain against rolling or holding;
- hit-adjusted value;
- terminal-value effect;
- confidence or uncertainty summary;
- affordability effect.

## 15.3 Alternatives

At least:

- best alternative transfer route;
- best no-transfer route;
- best lower-cost or structure-preserving route where relevant.

These are alternatives ranked by expected season value, not artificial “safe” and “aggressive” personalities.

## 15.4 Assumptions

- expected-minutes assumptions;
- accepted news judgements;
- uncertain fixtures;
- penalty and set-piece assumptions;
- price assumptions;
- model version.

## 15.5 Decision triggers

The report should state what new information would materially change the recommendation.

Examples:

- “Buy only if the player is confirmed fit.”
- “Roll unless the predicted start probability falls below 60%.”
- “Transfer becomes unaffordable after a £0.1m rise.”
- “Wildcard becomes preferred if a second squad player is ruled out.”

---

## 16. Dashboard requirements

The browser dashboard should eventually include:

### My Team

- current squad;
- selling prices and bank;
- expected points by Gameweek;
- expected minutes;
- start probabilities;
- recommended XI and bench;
- captaincy;
- flagged news assumptions.

### Transfer Planner

- roll comparison;
- transfer-out candidates;
- transfer-in candidates;
- multi-transfer routes;
- five-Gameweek gains;
- affordability;
- hit analysis;
- terminal-value effects.

### Player Explorer

- projections;
- uncertainty distribution;
- expected minutes;
- role and set pieces;
- fixtures;
- performance features;
- news evidence.

### Fixture Planner

- team attack and defence forecasts;
- fixture swings;
- blanks and doubles;
- uncertainty in unconfirmed fixtures.

### News Review

- structured evidence;
- source tier;
- proposed adjustments;
- explicit accept, reject or edit decision;
- audit history.

### Model Health

- forecast calibration;
- minutes accuracy;
- decision backtests;
- model-version comparison;
- data freshness;
- failed or stale sources.

### Deadline Report

- human-readable report generated from the final optimisation run;
- exportable or viewable on mobile;
- linked to the frozen run and evidence.

---

## 17. Reproducibility and backtesting

## 17.1 Reproducible run

A historical recommendation must be reproducible from:

- data snapshot identifiers;
- accepted news decisions;
- feature version;
- model version;
- optimiser version;
- configuration;
- season rules;
- random seed;
- run timestamp.

## 17.2 Immutable deadline snapshots

Once the deadline passes, the snapshot used for the recommendation must not be altered. Corrections should create a new record while preserving the original.

## 17.3 Historical replay

The project should support replaying past Gameweeks using information available at the time.

Historical replay should compare:

- current model version;
- previous model version;
- simple baselines;
- actual user decision;
- hindsight optimum, clearly labelled as unavailable at the time.

## 17.4 Baselines

Useful baselines include:

- make no transfers;
- choose highest recent FPL points;
- choose highest official form;
- choose highest simple expected points;
- captain the highest projected player;
- always avoid hits;
- simple fixture-ticker strategy.

A complex model should not be adopted unless it improves relevant out-of-sample metrics over simpler alternatives.

---

## 18. Technical architecture

## 18.1 Proposed repository structure

```text
fpl-decision-engine/
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
│   ├── unit/
│   ├── integration/
│   ├── regression/
│   └── fixtures/
├── scripts/
├── notebooks/
├── .github/workflows/
├── pyproject.toml
├── README.md
└── .env.example
```

## 18.2 Language and tooling

Initial recommendation:

- Python;
- DuckDB;
- pandas or Polars where appropriate;
- Pydantic for validated data contracts;
- Streamlit for the first dashboard;
- pytest;
- Ruff or equivalent linting;
- mypy or equivalent type checking where practical;
- GitHub Actions;
- GitHub Codespaces for development.

Dependencies should be kept modest and justified.

## 18.3 Module boundaries

Collectors must not contain modelling logic.  
Models must not perform web research.  
The optimiser must consume stable forecast contracts.  
The dashboard must not recalculate hidden model values.  
Reports must reference stored run outputs.

---

## 19. Automation, deployment and privacy

## 19.1 Normal user experience

The user should not need a local Python installation for routine use.

Preferred flow:

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

## 19.2 Automation cadence

Likely stages:

- daily structured-data refresh;
- additional refresh near the deadline;
- manual or prompted AI news-review session;
- final run after user decisions on ambiguous news;
- post-Gameweek scoring run.

Exact schedules should be configurable because deadline times vary.

## 19.3 Privacy

The repository remains private initially.

Do not commit:

- private credentials;
- authentication cookies;
- secrets;
- private manager settings that do not belong in source control.

Use GitHub secrets or hosted environment variables.

Public FPL manager data should still be treated as personal project data and stored only where necessary.

---

## 20. Testing and quality standards

## 20.1 Unit tests

Required for:

- FPL scoring rules;
- selling-price calculations;
- transfer-hit calculations;
- squad legality;
- formation constraints;
- captain and vice-captain scoring;
- chip effects;
- probability aggregation;
- terminal-value calculations;
- source parsers.

## 20.2 Integration tests

Required for:

- official data ingestion;
- database migrations;
- forecast generation;
- optimisation from a known squad state;
- deadline snapshot creation;
- report generation.

## 20.3 Regression tests

A fixed set of historical or synthetic Gameweeks should detect unexpected changes in:

- forecasts;
- recommended transfers;
- captaincy;
- optimiser feasibility;
- report content.

Changes are allowed, but unexplained changes should fail review.

## 20.4 Data-quality checks

Examples:

- duplicate players;
- missing fixtures;
- impossible minutes;
- stale snapshots;
- unmapped provider identities;
- negative prices;
- invalid probability totals;
- post-deadline timestamps entering pre-deadline features;
- untraceable news adjustments.

## 20.5 CI policy

Every pull request should run:

- formatting and linting;
- type checks where enabled;
- unit tests;
- integration tests that do not require private credentials;
- schema validation;
- documentation-link checks where practical.

---

## 21. Development roadmap

## Phase 0 — Specification and repository foundation

Deliverables:

- approved project specification;
- repository structure;
- coding standards;
- CI;
- development environment;
- issue and pull-request workflow.

## Phase 1 — Official data and immutable snapshots

Deliverables:

- official FPL collector;
- fixtures, teams, players and events;
- manager squad snapshot;
- DuckDB schema;
- timestamped raw storage;
- data freshness dashboard;
- tests.

## Phase 2 — Baseline five-Gameweek projections

Deliverables:

- transparent team-strength model;
- baseline expected-minutes model;
- baseline player expected-points model;
- uncertainty fields;
- fixture projections;
- stored forecasts.

## Phase 3 — First usable decision engine

Deliverables:

- squad optimiser;
- starting XI and bench order;
- captain and vice-captain;
- roll versus one-transfer comparison;
- basic hit assessment;
- terminal-value approximation;
- browser dashboard;
- deadline report.

This is the first release that should materially support the user's weekly team.

## Phase 4 — News workflow

Deliverables:

- news evidence schema;
- reusable ChatGPT research prompt;
- import process;
- judgement-review interface;
- expected-minutes overrides with audit trail;
- decision triggers in reports.

## Phase 5 — Richer free performance data

Deliverables:

- provider feasibility assessment;
- one or more free data adapters;
- xG/xA and richer player features;
- improved team and player models;
- provider-quality monitoring.

## Phase 6 — Simulation and multi-transfer planning

Deliverables:

- Monte Carlo match/player simulation;
- multi-transfer search;
- better transfer-flexibility valuation;
- improved terminal value;
- full probability distributions.

## Phase 7 — Pre-season and wildcard optimisation

Deliverables:

- eight-Gameweek pre-season optimiser;
- wildcard comparison;
- structural squad metrics;
- wildcard-window analysis.

## Phase 8 — Chip planning

Deliverables:

- Free Hit, Bench Boost and Triple Captain models;
- future-opportunity valuation;
- season-level chip option value.

## Phase 9 — Continuous validation

Deliverables:

- historical replay;
- forecast calibration;
- recommendation scoring;
- model-version comparisons;
- automated weekly model-health report.

---

## 22. First useful release acceptance criteria

The first useful release is complete when it can reliably:

1. load the user's current legal squad and financial state;
2. update official FPL players, teams, fixtures and deadlines;
3. retain immutable timestamped snapshots;
4. project expected minutes and expected points for five Gameweeks;
5. show uncertainty and key assumptions;
6. recommend the starting XI, bench order, captain and vice-captain;
7. compare rolling with realistic one-transfer options;
8. account for transfer costs and selling prices;
9. include a transparent initial terminal value;
10. generate a readable deadline report;
11. freeze the recommendation before the deadline;
12. score the forecast after the Gameweek;
13. run without requiring the user to install Python locally;
14. pass automated tests for game rules and optimiser constraints.

It does not need perfect forecasts, advanced chips, public hosting or machine learning to satisfy this milestone.

---

## 23. Remaining design work

The main philosophy and scope are settled. The following items should be resolved through implementation research or validation rather than prolonged abstract debate:

1. exact five-Gameweek weighting;
2. initial terminal-value formula;
3. initial value of transfer flexibility;
4. best free and permitted performance-data provider or providers;
5. expected-minutes baseline specification;
6. simulation method and number of runs;
7. hosted dashboard platform;
8. exact deadline automation schedule;
9. current-season FPL rule configuration;
10. format used to import the ChatGPT news review.

Each decision should be documented with:

- alternatives considered;
- reason for selection;
- known limitations;
- validation plan;
- conditions that would trigger reconsideration.

---

## 24. Review checklist

The reviewer should confirm whether this specification correctly captures:

- the single-user scope;
- the season-points objective;
- the eight-Gameweek pre-season horizon;
- the rolling five-Gameweek weekly horizon;
- the provisional nature of future transfer plans;
- explicit uncertainty;
- AI-assisted but human-reviewed news interpretation;
- free and accessible data sources;
- the browser dashboard and deadline report;
- reproducibility and pre-deadline snapshots;
- the proposed first useful release.

Comments should focus first on incorrect goals, missing decisions or inappropriate scope. Detailed modelling choices can be refined in the later technical documents.
