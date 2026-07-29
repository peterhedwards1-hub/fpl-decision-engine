# FPL Decision Engine — Implementation Roadmap

**Status:** Approved and canonical  
**Version:** 1.0  
**Scope:** Pre-Gameweek 1 build path through full decision-support system
**Architecture decisions:** See `03_ArchitectureDecisions.md`

---

## 1. Purpose

This document defines the recommended implementation sequence for the FPL Decision Engine.

The roadmap is ordered to deliver useful outputs as early as possible while preserving the architecture agreed in `00_ProjectSpecification.md` and the user experience defined in `01_FrontEndSpecification.md`.

The immediate project context is important:

> The system is being built before Gameweek 1 has been played.

That means the highest-priority near-term outcome is not a transfer recommender. It is a trustworthy opening-squad recommendation supported by current-season data collection, transparent player projections, legal squad optimisation, bench handling and a usable browser interface.

---

## 2. Roadmap principles

The implementation should follow these principles.

### 2.1 Establish rules before models

Scoring, squad legality, prices, chips and transfer rules must be season-configured and tested before optimisation logic depends on them.

### 2.2 Collect current-season snapshots immediately

Pre-deadline historical state cannot be reconstructed reliably after the fact. Current-season collection is therefore time-critical and should begin before the rest of the system is complete.

### 2.3 Build expected-points generation before selection

A squad optimiser is only as meaningful as the projections it consumes. Player projection is therefore a first-class milestone rather than an implementation detail of the picker.

### 2.4 Bring squad-state input and the front end forward

The system should be able to display and edit the current team early. This gives every later milestone an end-to-end path from data to user-visible output.

### 2.5 Deliver the opening-squad optimiser before the transfer engine

Before Gameweek 1, the most valuable product is a robust initial squad recommendation over the agreed eight-Gameweek horizon.

### 2.6 Validate continuously

Every model and optimisation step should be compared with transparent baselines. Complexity should not be adopted without evidence that it improves relevant out-of-sample performance.

---

## 3. Milestone overview

| Milestone | Name | Primary outcome |
|---|---|---|
| 0 | Rules, schemas and season configuration | Trusted legal and scoring foundation |
| 1 | Historical database | Queryable historical player, fixture and scoring data |
| 2 | Current-season collector and immutable snapshots | Up-to-date, timestamped data that can be refreshed |
| 3 | Squad-state input and basic front end | Current team can be selected, stored and displayed |
| 4 | Player projection baseline | Expected minutes and points for forthcoming Gameweeks |
| 5 | Best starting XI under a budget | Optimiser proof-of-concept for scoring players |
| 6 | Full 15-player squad and weekly lineup | Legal squad, bench, autosubs, captaincy and XI |
| 7 | Initial pre-season squad optimiser | Recommended Gameweek 1 squad over eight Gameweeks |
| 8 | Transfer recommender | Ranked roll and transfer routes from an existing squad |
| 9 | Two-pass weekly decision workflow | Provisional and final deadline recommendations |
| 10 | Full decision-support system | Complete system described in the project specification |

---

## 4. Milestone 0 — Rules, schemas and season configuration

### Objective

Create the trusted foundation on which ingestion, scoring, projections and optimisation depend.

### Scope

Define and version:

- squad size and positional quotas;
- legal formations;
- maximum players per club;
- budget rules;
- price units and selling-price rules;
- transfer-cost rules;
- free-transfer banking and cap;
- chip inventory, expiry and effects;
- scoring rules by position;
- defensive-contribution scoring;
- bonus scoring inputs where required;
- captain and vice-captain rules;
- autosub order and formation repair;
- Gameweek and deadline identifiers;
- stable internal identifiers for players, clubs and fixtures;
- snapshot and provenance fields.

Rules should live in explicit season configuration rather than being distributed through application code.

### Deliverables

- season configuration data files;
- validated Pydantic contracts or equivalent;
- legality validator for synthetic squads;
- scoring calculator;
- selling-price calculator;
- transfer-hit calculator;
- tests for all rule boundaries.

### Acceptance criteria

Given a synthetic squad and event outcome, the system can:

1. determine whether the squad is legal;
2. identify legal formations;
3. calculate selling prices correctly;
4. calculate transfer costs correctly;
5. calculate player, captain and chip scoring correctly;
6. apply autosub rules correctly;
7. fail clearly when configuration is incomplete or inconsistent.

---

## 5. Milestone 1 — Historical database

### Objective

Create a queryable historical store containing the information required for baseline modelling and evaluation.

### Scope

The database should include, where available:

- seasons;
- Gameweeks and deadlines;
- players and teams by season;
- positions and prices;
- fixtures and results;
- starts and minutes;
- goals and assists;
- clean sheets and goals conceded;
- saves;
- cards and own goals;
- bonus points;
- defensive contributions;
- official expected goals, expected assists and expected goal involvement;
- ownership and transfer activity;
- promoted and relegated teams;
- source and field-definition metadata.

Historical data should be stored at the lowest practical grain, normally player-fixture and team-fixture, with Gameweek aggregates derived rather than treated as the only source.

### Deliverables

- historical source assessment;
- ingestion adapters for selected permitted sources;
- DuckDB schema and migrations;
- canonical identity mappings;
- data dictionary;
- repeatable historical import command;
- data-quality report.

### Acceptance criteria

The system can query any supported historical player-Gameweek and recover:

- relevant player and team state;
- fixture context;
- realised FPL scoring components;
- source provenance;
- definitions used for each field.

### Limitations to record

Historical datasets will not fully reproduce all information known before a deadline, particularly:

- injuries and press-conference information;
- predicted lineups;
- current-week private squad state;
- price-change timing;
- pre-deadline expected-minutes judgements.

Historical data is therefore suitable for performance modelling and partial replay, but not a perfect substitute for live point-in-time snapshots.

---

## 6. Milestone 2 — Current-season collector and immutable snapshots

### Objective

Build an updateable current-season database and begin preserving point-in-time data before Gameweek 1.

### Scope

Collect and retain:

- official players and teams;
- current prices and positions;
- fixtures and deadlines;
- ownership and transfers;
- official status, news and chance-of-playing fields;
- official xG, xA and xGI fields where exposed;
- current-season scoring statistics;
- raw provider responses;
- collection timestamp and ingestion metadata.

### Required behaviours

- support manual refresh from the browser or command line;
- support scheduled refresh later;
- retain immutable raw snapshots;
- normalise snapshots into the core database;
- detect stale or incomplete data;
- preserve fixture changes rather than overwriting history;
- expose collection health to the front end.

### Deliverables

- official FPL collector;
- raw snapshot store;
- current-season staging and core tables;
- refresh command;
- data freshness checks;
- snapshot browser or status panel;
- automated tests with recorded fixtures.

### Acceptance criteria

A user can trigger an update and receive:

1. a complete timestamped current-season snapshot;
2. a clear success or failure result;
3. a list of stale or missing data;
4. a database state that preserves previous snapshots.

### Time-critical commitment

This collector should be deployed and run as early as possible. Every missed pre-deadline snapshot is validation data that may never be recoverable.

---

## 7. Milestone 3 — Squad-state input and basic front end

### Objective

Allow the user to define, store and inspect a current FPL squad through a browser interface.

### Scope

The initial version should support manual entry because private pre-deadline squad state may not be reliably available through public endpoints.

The user should be able to:

- select 15 players;
- enter or confirm purchase and selling prices;
- enter money in the bank;
- enter available free transfers;
- select remaining chips;
- select captain and vice-captain where recording actual state;
- save and reload the team state;
- identify the snapshot and deadline to which the state applies.

### Front-end requirements

The initial interface should show:

- the squad in a pitch-style layout;
- substitutes separately;
- current budget and bank;
- free-transfer count;
- chip state;
- current Gameweek and deadline;
- validation errors;
- current data freshness.

### Later extension

Automatic import may be added if an authenticated integration proves reliable and secure. It should not block the first usable system.

### Deliverables

- browser team editor;
- squad-state schema;
- persistence layer;
- legal-squad validation;
- current-team view;
- manual financial-state controls.

### Acceptance criteria

The system can store and display the user's actual legal squad and financial state without requiring direct database editing.

---

## 8. Milestone 4 — Player projection baseline

### Objective

Produce transparent expected-minutes and expected-points forecasts for each relevant player over forthcoming Gameweeks.

### Scope

The baseline should estimate:

- probability of starting;
- expected minutes if starting;
- probability and minutes of substitute appearance;
- probability of no appearance;
- appearance points;
- goal expectation;
- assist expectation;
- clean-sheet expectation;
- defensive-contribution expectation;
- goalkeeper save expectation;
- bonus expectation;
- card and own-goal risk where useful;
- total expected points by Gameweek.

### Pre-Gameweek 1 inputs

Because no current-season matches have yet been played, the opening model should combine:

- previous-season performance;
- multi-season historical performance where useful;
- shrinkage toward role and position priors;
- current prices and FPL positions;
- opening fixtures;
- promoted-team priors;
- transferred-player priors;
- preseason appearances and minutes where available;
- expected role and starting probability;
- penalties and set pieces;
- official availability fields;
- manual expected-minutes overrides.

### Modelling approach

The first model should be transparent and auditable. A rates-based model with sensible shrinkage is preferred over an opaque machine-learning model for this milestone.

### Deliverables

- baseline expected-minutes model;
- baseline team attack and defence model;
- player scoring-component model;
- eight-Gameweek projection table;
- uncertainty and assumption fields;
- manual availability and minutes override mechanism;
- projection explorer in the front end.

### Acceptance criteria

Every available player has:

- expected minutes for each projected Gameweek;
- expected points for each projected Gameweek;
- an explanation of the major components;
- a record of assumptions and overrides;
- a stable model version.

---

## 9. Milestone 5 — Best starting XI under a budget

### Objective

Create the first exact optimiser and prove that projections can be converted into legal selection decisions.

### Scope

Optimise eleven scoring players subject to:

- legal starting formation;
- club limits;
- positional requirements;
- an assigned eleven-player budget;
- expected-points objective over a configurable horizon.

This is deliberately not yet a complete FPL squad. It is an optimiser proof-of-concept and a useful diagnostic for understanding where the model values budget.

### Recommended method

Use mixed-integer linear programming or another exact discrete optimisation method where the objective and constraints are linear.

### Deliverables

- starting-XI optimiser;
- legality tests;
- deterministic result for fixed inputs;
- explanation of selected and near-selected players;
- front-end view of the optimal XI.

### Acceptance criteria

Given a projection set and budget, the optimiser returns the highest-valued legal XI and proves that no higher-valued feasible XI was omitted.

---

## 10. Milestone 6 — Full 15-player squad and weekly lineup

### Objective

Select a complete legal FPL squad and determine its starting XI, substitutes, captain and vice-captain.

### Scope

The optimiser must select:

- two goalkeepers;
- five defenders;
- five midfielders;
- three forwards;
- legal starting XI;
- ordered bench;
- captain;
- vice-captain.

### Required considerations

- total budget;
- club limits;
- formation legality;
- autosub behaviour;
- probability that starters fail to appear;
- legal formation after substitutions;
- substitute expected minutes and points;
- cheap non-playing substitutes versus a balanced bench;
- concentration of funds in the starting XI;
- captaincy and vice-captain coverage;
- expected points over a configurable horizon.

The system should avoid treating the bench as either worthless or equivalent to starters. Bench value should arise from appearance probabilities, autosub order and scoring distributions.

### Deliverables

- complete squad optimiser;
- lineup and bench-order optimiser;
- captain and vice-captain selection;
- autosub simulation or exact equivalent;
- pitch view of recommended squad;
- comparison of bench structures.

### Acceptance criteria

The system returns a legal 15-player squad with:

- starting XI;
- bench order;
- captain;
- vice-captain;
- expected scoring contribution from starters and bench;
- explanation of major budget trade-offs.

This is the first genuinely useful pre-season product milestone.

---

## 11. Milestone 7 — Initial pre-season squad optimiser

### Objective

Recommend the best robust opening squad for Gameweek 1 using the agreed eight-Gameweek planning horizon.

### Scope

The pre-season optimiser should consider:

- projected points over the first eight Gameweeks;
- lower confidence in later Gameweeks;
- captaincy coverage;
- bench resilience;
- opening fixture swings;
- likely early transfer needs;
- affordability and money distribution;
- squad flexibility;
- residual value after Gameweek 8;
- known blanks, doubles or chip deadlines where relevant;
- plausible early wildcard periods;
- uncertainty in promoted teams, new signings and preseason roles.

### Outputs

The report should include:

- recommended 15-player opening squad;
- Gameweek 1 starting XI;
- captain and vice-captain;
- bench order;
- closest alternative squads;
- expected eight-Gameweek value;
- major assumptions;
- structural risks;
- likely early transfer triggers;
- what new preseason information would change the recommendation.

### Deliverables

- eight-Gameweek objective configuration;
- residual-value baseline;
- alternative-squad generation;
- opening-squad dashboard;
- preseason report;
- frozen final pre-deadline snapshot.

### Acceptance criteria

Before Gameweek 1, the system can generate a defensible opening-squad recommendation and show why it is preferred over realistic alternatives.

---

## 12. Milestone 8 — Transfer recommender

### Objective

Recommend whether to roll or transfer from an existing squad, using historical performance, current forecasts and upcoming fixtures.

### Scope

The initial engine should compare:

- roll;
- one free transfer;
- multiple free transfers when implemented;
- points hits where relevant;
- lineup, bench and captaincy after each route.

### Required considerations

- current squad and selling prices;
- money in the bank;
- free-transfer count and cap;
- expected points over the rolling horizon;
- upcoming fixtures;
- expected minutes;
- injury and rotation risk;
- squad structure;
- terminal value;
- future transfer flexibility;
- known fixture swings;
- affordability risks.

### Initial limitation

The first version may search only one-transfer routes. Where this is the case, the interface must state clearly that two-transfer combinations were not evaluated.

### Deliverables

- candidate transfer generator;
- roll baseline;
- transfer optimiser;
- hit-adjusted value calculation;
- transfer comparison view;
- ranked alternatives;
- explanation of expected gain and assumptions.

### Acceptance criteria

Given a stored current squad, the system returns ranked transfer actions and a clear comparison with holding.

---

## 13. Milestone 9 — Two-pass weekly decision workflow

### Objective

Turn the individual components into a repeatable weekly operating process.

### Pass 1 — Early-week provisional run

The system should:

- update structured data;
- load the current squad;
- generate provisional projections;
- produce provisional transfer, lineup and captaincy recommendations;
- identify decision triggers;
- show price or affordability concerns;
- state what new information would change the recommendation.

### Pass 2 — Final post-press-conference run

Near the deadline, the system should:

- refresh prices, fixtures and availability;
- ingest structured news evidence;
- apply reviewed expected-minutes or role overrides;
- rerun projections and optimisation;
- generate the final deadline report;
- freeze the complete pre-deadline snapshot.

### After the deadline

The system should record:

- the action actually taken;
- any deviation from the recommendation;
- the user's rationale for deviating;
- realised outcomes;
- forecast scores;
- decision-review notes.

### Deliverables

- provisional and final run modes;
- decision-trigger reporting;
- news and override review;
- immutable final snapshot;
- actual-action capture;
- post-Gameweek scoring job.

### Acceptance criteria

A complete weekly cycle can be executed from current squad state through final recommendation and later evaluation without rewriting historical inputs.

---

## 14. Milestone 10 — Full decision-support system

### Objective

Complete the system described in the authoritative project specification.

### Scope

Add and validate:

- structured ChatGPT news ingestion;
- human review of ambiguous evidence;
- richer expected-minutes models;
- improved team and player models;
- multi-transfer search;
- better transfer option value;
- configurable and dynamically extended horizons;
- uncertainty distributions;
- blank and double Gameweek handling;
- Wildcard optimisation;
- Free Hit optimisation;
- Bench Boost optimisation;
- Triple Captain optimisation;
- model-health dashboard;
- historical replay;
- model-version comparisons;
- hosted automation and private deployment where justified.

### Acceptance criteria

The system supports the full preseason and weekly decision cycle described in `00_ProjectSpecification.md`, with reproducible recommendations, clear provenance, validated models and a usable browser interface.

---

## 15. Pre-Gameweek 1 delivery target

The practical pre-Gameweek 1 target is completion of Milestones 0 through 7, plus the preseason parts of Milestone 9.

That should provide:

- season rules and legality checks;
- a historical database;
- live current-season collection;
- immutable pre-deadline snapshots;
- a browser view of the current or proposed team;
- player expected-minutes and expected-points forecasts;
- exact squad optimisation;
- bench and autosub handling;
- captain and vice-captain recommendations;
- an eight-Gameweek opening-squad recommendation;
- explicit assumptions and manual preseason overrides;
- a final frozen Gameweek 1 recommendation.

The transfer recommender should begin immediately afterwards. It may be prototyped before Gameweek 1 for testing, but it should not displace work required for a trustworthy opening squad.

---

## 16. Validation requirements across all milestones

Validation is not a final phase. Each milestone should include the relevant tests and baselines.

### Data validation

- source freshness;
- duplicate and missing identities;
- impossible minutes or prices;
- field-definition consistency;
- timestamp and leakage checks;
- snapshot completeness.

### Forecast validation

- expected-minutes error;
- start and appearance calibration;
- expected-points calibration;
- component-level scoring accuracy;
- ranking performance;
- comparison with naive baselines.

### Optimisation validation

- legality and feasibility;
- exactness where claimed;
- deterministic reproducibility;
- regression tests on synthetic and historical cases;
- comparison with simpler selection policies.

### Live-season interpretation

A single season provides only approximately 38 weekly decision observations. Realised transfer regret and captaincy outcomes are useful for review, but are not sufficient by themselves for model selection. Forecast calibration across thousands of player-Gameweek observations and leakage-controlled historical replay should carry more statistical weight.

---

## 17. Dependencies and critical path

The main dependency chain is:

```text
Season rules and schemas
          ↓
Historical and current data
          ↓
Expected-minutes and points projections
          ↓
Starting-XI optimisation
          ↓
Full-squad, bench and captaincy optimisation
          ↓
Preseason squad recommendation
          ↓
Transfer and weekly decision workflows
          ↓
Advanced news, chips, simulation and validation
```

The current-season collector should run in parallel as early as possible rather than waiting for all upstream design work to finish.

The front-end shell should also begin early, after the squad-state contract exists, so later models can be integrated into a stable user workflow rather than exposed only through notebooks or command-line output.

---

## 18. Definition of done for each milestone

A milestone is complete only when:

1. the feature works end to end;
2. inputs and outputs use validated contracts;
3. relevant rules and edge cases are tested;
4. results are reproducible for fixed inputs;
5. assumptions and limitations are visible to the user;
6. data and model provenance are stored;
7. the front end exposes the result where it is user-facing;
8. documentation is updated;
9. no later milestone is forced to bypass or duplicate the component.

---

## 19. Immediate next implementation tasks

Once this roadmap is approved, the recommended next work is:

1. define the 2026/27 season rules and scoring configuration;
2. define the canonical data contracts and DuckDB schema;
3. identify and test permitted historical data sources;
4. implement the official current-season collector;
5. begin immutable snapshot collection;
6. build the manual squad-state editor and pitch view;
7. specify the baseline expected-minutes and points model.

These tasks create the shortest credible route to a useful Gameweek 1 squad recommendation.
