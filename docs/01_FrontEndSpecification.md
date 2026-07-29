# FPL Decision Engine — Front-End Specification

**Status:** Draft for review  
**Version:** 0.1  
**Parent document:** `docs/00_ProjectSpecification.md`

---

## 1. Purpose

The front end exists to make the system usable before every FPL deadline.

It must let the user quickly answer:

1. What is my current squad state?
2. What does the model currently recommend?
3. Why is it recommending that?
4. What are the best alternatives?
5. What information could still change the decision?
6. What final action did I actually take?

The first front end should prioritise clarity, trust and deadline usefulness over visual complexity.

---

## 2. Primary screen: My Team

The default screen should show the current 15-player squad in a recognisable FPL pitch layout.

### 2.1 Squad header

Show:

- current Gameweek and deadline;
- squad value;
- money in the bank;
- free transfers available;
- active and remaining chips;
- current total points and rank for context;
- timestamp of the latest data refresh;
- whether the view is provisional or final.

### 2.2 Pitch view

Display the recommended starting XI by formation, with substitutes shown below in bench order.

Each player tile should show at minimum:

- player name;
- club and position;
- current selling price;
- next opponent and venue;
- expected points for the next Gameweek;
- expected points over the planning horizon;
- expected minutes or start probability;
- captain, vice-captain or bench status;
- injury, suspension or news warning;
- incoming or outgoing transfer marker where relevant.

The display should make it immediately obvious:

- who starts;
- who is benched;
- who is captain and vice-captain;
- which players are flagged;
- which players the model proposes to sell or buy.

### 2.3 Current versus recommended state

The user should be able to switch between:

- **Current team** — the squad and lineup as presently entered;
- **Recommended team** — the squad after the proposed transfer action and optimised lineup;
- **Difference view** — only the changes between the two.

The difference view should clearly show:

- transfer out;
- transfer in;
- hit cost;
- captaincy change;
- lineup changes;
- bench-order changes;
- money remaining;
- change in projected value.

---

## 3. Recommendation panel

A prominent recommendation panel should sit beside or above the squad view.

It should state:

- **recommended action** — roll, transfer, hit or chip;
- exact player or players out and in;
- recommended timing;
- expected five-Gameweek gain;
- hit-adjusted gain;
- terminal-value effect;
- confidence or uncertainty summary;
- whether the recommendation is provisional or final.

Example:

```text
Recommended: Roll the transfer

Five-Gameweek advantage over the best immediate move: +1.8 expected points
Free transfers after the deadline: 2
Confidence: Medium

Why:
- no transfer produces a material immediate gain;
- two stronger routes may open next week;
- current injury risk is manageable;
- preserving flexibility has positive expected value.
```

The system must never present a recommendation without its comparison baseline.

---

## 4. Alternatives

The front end should show a small ranked set of realistic alternatives rather than only one answer.

For each alternative show:

- action;
- players out and in;
- hit cost;
- five-Gameweek expected gain;
- terminal-value difference;
- money left;
- main benefit;
- main risk;
- information that would make it preferable.

The initial release should include at least:

1. primary recommendation;
2. best alternative one-transfer route;
3. best no-transfer route;
4. a clear notice where two-transfer combinations have not yet been evaluated.

Alternatives should be ranked by expected season value, not labelled as artificial conservative or aggressive personalities.

---

## 5. Five-Gameweek outlook

A compact planner should show how the current and recommended squads project across the rolling horizon.

For each Gameweek show:

- projected squad points;
- likely captain;
- fixture difficulty or team expectation;
- players with uncertain minutes;
- known blanks or doubles;
- provisional transfer triggers.

The user should be able to compare:

- hold current squad;
- apply recommended action;
- apply one selected alternative.

The horizon should extend beyond five Gameweeks when a known blank, double or chip deadline materially affects the current decision.

---

## 6. News and assumptions

The front end should expose the evidence that materially affects the recommendation.

### 6.1 News summary

Show only decision-relevant items by default:

- affected player;
- factual summary;
- source tier;
- publication time;
- model area affected;
- accepted adjustment;
- confidence;
- whether a user decision is required.

### 6.2 Explicit decisions

Items requiring judgement should appear in a dedicated review queue with controls to:

- accept the proposed adjustment;
- reject it;
- edit expected minutes or availability;
- record a rationale.

The final optimisation must not run silently past unresolved material decisions unless the user explicitly chooses to proceed.

### 6.3 Decision triggers

The front end should clearly state what could change the recommendation before the deadline.

Examples:

- player confirmed fit;
- expected start probability falls below a threshold;
- price rise makes the move unaffordable;
- second squad injury occurs;
- fixture is rearranged.

---

## 7. Manual squad-state input

Because private pre-deadline squad state may not be reliably available from public endpoints, the first front end should provide a simple browser form for:

- 15-player squad;
- purchase or selling prices where required;
- money in the bank;
- number of free transfers;
- chip inventory and expiry state;
- current captain and vice-captain;
- current bench order.

The form should:

- validate squad legality;
- prevent impossible prices or transfer counts;
- highlight missing values;
- save a timestamped manager snapshot;
- allow the previous week's state to be reused and amended;
- make manual entry quick enough for routine weekly use.

Authenticated import can be added later, but the first useful release must not depend on fragile session-cookie automation.

---

## 8. Provisional and final modes

The interface should make the two-pass weekly workflow explicit.

### Provisional mode

Used early in the week for:

- transfer planning;
- price awareness;
- identifying news questions;
- seeing what would change the recommendation.

### Final mode

Used after press conferences and final news review for:

- accepted expected-minutes adjustments;
- final transfer recommendation;
- final captaincy and lineup;
- frozen deadline report;
- recording actual action.

The visual state must clearly distinguish provisional forecasts from the frozen pre-deadline recommendation.

---

## 9. Actual action and override capture

After making the FPL decision, the user should record:

- actual transfer action;
- actual chip action;
- captain and vice-captain;
- starting XI and bench order;
- whether the recommendation was followed;
- reason for any deviation.

Suggested override reasons include:

- disagreed with expected minutes;
- received late team news;
- affordability changed;
- preferred a route not searched by the optimiser;
- manual judgement;
- accidental or operational difference.

This creates an important year-one dataset for comparing model recommendations with real decisions.

---

## 10. Mobile and usability requirements

The deadline view must work on a phone.

The mobile design should prioritise:

1. recommendation;
2. transfer details;
3. captain and vice-captain;
4. starting XI and bench;
5. unresolved news decisions;
6. deadline and data freshness.

Detailed charts and model diagnostics may collapse into expandable sections.

The interface should avoid:

- wide tables as the only way to understand recommendations;
- hidden assumptions;
- unexplained confidence scores;
- excessive navigation before reaching the current recommendation;
- presenting stale data without a warning.

---

## 11. Initial navigation

The first useful interface should have four primary pages or tabs:

### My Team

Current squad, recommended squad, lineup, captaincy and headline action.

### Transfers

Primary recommendation, alternatives, expected gains and five-Gameweek comparison.

### News Review

Structured evidence, manual overrides and unresolved decisions.

### Model Health

Data freshness, forecast status and basic post-Gameweek scoring.

Player Explorer and advanced Fixture Planner views can follow after the weekly decision flow is reliable.

---

## 12. First useful front-end acceptance criteria

The front end is ready for the first useful release when it can:

1. load or manually enter the user's complete current squad state;
2. show the 15-player squad in a clear pitch and bench layout;
3. show the current lineup, captain and vice-captain;
4. show the recommended transfer action prominently;
5. show the resulting recommended squad and lineup;
6. show the expected gain and comparison baseline;
7. show at least two realistic alternatives;
8. expose expected minutes, uncertainty and material assumptions;
9. flag unresolved news decisions;
10. accept manual expected-minutes or availability overrides with rationale;
11. distinguish provisional and final runs;
12. freeze and display the final pre-deadline recommendation;
13. record the actual action and reason for deviation;
14. work from a browser without requiring a local Python installation;
15. remain usable on a mobile screen.

---

## 13. Implementation approach

Streamlit is suitable for the first version because it can provide:

- browser-based forms;
- squad and recommendation views;
- rapid iteration;
- deployment from the same Python repository;
- access through Codespaces before full hosting is implemented.

The front end must consume stored data contracts and optimisation results. It must not hide separate modelling logic inside interface code.

A later framework may replace Streamlit if the interface grows beyond its practical limits, but that is not an initial concern.
