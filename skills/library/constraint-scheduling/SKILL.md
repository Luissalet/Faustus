---
name: constraint-scheduling
description: State a scheduling or product-configuration problem with explicit constraints, solve it, verify the result against every constraint and explain infeasibility. Use when tasks, resources, deadlines or product options must fit together and the answer must be provably valid.
version: 1.0.0
category: planning
tags: [scheduling, constraints, planning, configuration, resources, deadlines, optimization, verification, planificacion, calendario, restricciones, configurador]
apps: [dantzig]
status: published
source: imported
---

## When to Use

You have jobs that need people, machines or rooms, with durations, order and deadlines, or a product whose options are limited by compatibility rules. The answer must be a plan or a configuration that is correct, not just plausible. This procedure does not cover forecasting demand or estimating how long things take; those inputs must come from the person.

## Procedure

1. Read the app's tool list. Write the problem in plain lists before touching tools: the tasks with durations, the resources with their calendars (working hours, holidays, capacity), which resource kinds can do which task, the order dependencies, the deadlines, and what counts as a better plan (earlier finish, fewer changes, balanced load).
2. Ask about every number you had to guess. A duration given as "about a day" is a range; record the value you will use and say it is assumed.
3. Enter the problem as data, one constraint per entry, with a name that a human can read in an error message. Separate hard constraints (must hold) from preferences (try to satisfy).
4. Solve. For a first run, solve the hard constraints only, then add preferences, so that when the problem becomes infeasible you know which addition caused it.
5. Verify the result yourself with the app's verification, and also spot-check by hand: no resource used twice at the same time, every task after its predecessors, every task inside a working period, every deadline met, quantities within capacity. For a configuration, check each compatibility rule against the chosen options.
6. If there is no solution, do not loosen things silently. Ask the solver to explain, then name the smallest set of conflicting constraints in plain words and offer concrete relaxations with their cost: extend a deadline by N days, add a shift, allow an alternative resource, drop an option.
7. When the situation changes (a machine is down, a task grows), re-solve from the stored problem and compare with the previous plan. Report what moved, because people have already planned around the old one.
8. Hand over the plan as a readable table plus the assumptions list and the verification result.

## Pitfalls

- Hiding a preference inside a hard constraint, which turns a flexible case into an infeasible one.
- Time units mixed between hours, working days and calendar days.
- Forgetting calendars: a four-day task that starts on a Thursday does not end on Sunday.
- Accepting a solution because the solver returned one, without checking the data was entered as intended.
- Optimizing one number (finish date) and ignoring a cost the person cares about.
- Presenting "infeasible" without the reason.

## Verification

- Each constraint of the written problem is present in the stored problem and each is shown as satisfied.
- Assumed values are listed separately from given ones.
- An infeasible case ends with a named conflict set and at least one costed way out.
- A changed plan comes with a list of tasks that moved.
