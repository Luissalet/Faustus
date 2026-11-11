---
name: pokemon-team-and-battle-analysis
description: Build a team, run a series of simulated battles, read win rates and what decided the games, and adjust the team. Use when someone wants a Pokemon team tested against opponents, with evidence of what works.
version: 1.0.0
category: testing
tags: [pokemon, battles, simulator, team-building, win-rate, analysis, series, strategy, equipos, combates, simulador, estrategia]
apps: [silver]
status: published
source: imported
---

## When to Use

A player has a team idea, a format (generation, singles or doubles, rules) and wants to know how it performs against realistic opposition and why it wins or loses. The procedure covers building, running series and reading results. It does not cover predicting online ladder results, and one battle proves almost nothing.

## Procedure

1. Read the app's tool list. Fix the format first: generation, singles or doubles, level cap, banned items and clauses. A team legal in one format may be illegal in another, and the simulator will reject or silently change it.
2. Build the team around a role plan: how it wins (offence, stall, weather, speed control), who handles each common threat, which type weaknesses are shared. List the three Pokemon that scare the team most and check there is an answer for each.
3. Validate the team in the simulator: moves legal in the generation, abilities, items without duplicates where the format forbids it, EV and nature choices that fit the plan. Fix every complaint.
4. Choose opponents deliberately: a mix of teams representing the common archetypes of the format, not only your favourite. Pick the AI level and style for each opponent; a weak AI makes any team look good.
5. Run a series with enough battles for the result to mean something. Ten battles can swing widely by luck; a few dozen per matchup begin to be informative. Alternate sides if the format has an advantage, and note the series settings.
6. Read the series: overall win rate, win rate per opponent archetype, and the best-performing members. Then open a sample of lost battles and read what decided them: a speed tie, a missing answer to one threat, a critical hit, an item choice, a lead that is unlucky. Distinguish a pattern repeated across losses from a one-off.
7. Change one thing at a time (a move, an item, a member), rerun against the same opponents and compare. If several changes are made at once you will not know which helped. Keep the old version to compare against, and stop when changes stop improving results beyond the noise.
8. Report the team, the format, the series size and results per matchup, the main reason for losses, and what you changed.

## Pitfalls

- Testing against one opponent and generalising.
- Stopping at the win rate and never reading a lost battle.
- Overfitting the team to the opponents used in tuning; confirm with fresh opponents.
- Forgetting that the AI's behaviour differs from a human's in prediction.
- Counting a lucky streak as a trend.
- Mixing rules between series.

## Verification

- The team validates in the stated format.
- Each series reports number of battles, opponent set and AI settings.
- Every claimed weakness is backed by at least two lost battles that show it.
- A change is accompanied by before and after results on the same opponents, plus a fresh set.
