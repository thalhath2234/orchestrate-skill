# How an orchestrate job works inside

Reference for the parent when a user asks how something works, or when a job behaves unexpectedly. The single source of truth for numbers is the code (`orchestrate.py --help`, `router.py --help`, and the constants at the top of each script); this file explains the shape.

## Who does what

| Role | Who | Does |
|---|---|---|
| **Parent** | the conversation that loaded the orchestrate skill | grills the user, triages, plans, launches, relays the manager's questions, reports |
| **Manager** | one persistent Opus session per job | supervises every subagent: diagnoses failures, retries with better instructions, splits tasks that are too big, turns open issues and review findings into targeted tasks, asks the user when a decision is needed |
| **Subagents** | `claude -p` workers in their own worktrees | do one task each, on the model routing picks |
| **Integrator / reviewers** | fresh sessions | resolve merge conflicts; review the merged result (per area plus an integration reviewer when the diff is large) |
| `orchestrate.py` | script | capacity caps, worktrees, briefs, merging, file claims, rule-based triage, and validating and applying the manager's actions |

## The manager

It is woken only when judgment is needed:
- **a task fails**, after routing has already escalated models
- **a finished subagent reports `open_issues`** that no rule settles: `fyi` issues are just listed, and `needs_file` issues whose files belong to one not-started task amend that task directly
- **the reviewer requests changes**
- **the user answers its question**

It decides in a background thread. The scheduler keeps launching tasks the decision can't affect, and events that arrive meanwhile reach it as one `batch`. It replies (as schema-checked structured output) with actions: `retry`, `split`, `add_task`, `amend`, `skip`, `fix_round`, `accept`, `ask_human` or `abort`. The script validates them as one batch (ownership, the dependency graph, verify not weakened, migrations gated, limits) and applies all or none. A reply that can't be parsed is re-asked once, then turned into a question for the user. Defaults: at most 12 interventions per job and 2 manager retries per task. Every decision is listed in `report.md`. `"manager": {"enabled": false}` falls back to plain stop-on-failure and one generic fix round.

Subagent-written text (reports, open issues, log tails) reaches the manager, reviewer and integrator only inside `<<<UNTRUSTED ...>>>` fences, sanitized and size-capped.

## The parallelism cap

`capacity` takes the minimum of:
- **memory:** available RAM, including cgroup/container limits, × 0.8 ÷ `mem_per_agent_gb` (default 1.5)
- **CPU:** free CPUs (cores − 1-minute load) ÷ `cpu_per_agent` (default 1)
- **hard max:** 6, for API rate limits
- `plan.max_parallel`, if set

It re-checks free memory before every launch. For heavy monorepos (tsc, Go and Gradle builds in verify), set `"mem_per_agent_gb": 2.5` or more in the plan. Check the numbers with `orchestrate.py capacity`.

## What each subagent receives

A brief in two parts. The first part is identical for every task of the job, so parallel subagents share a cached prompt prefix: the rules, the report format, acceptance, decisions, non-goals, and the repo's root glossary and AGENTS.md inlined when small. The second part is the task: its goal, the paths it **owns**, what to **read first**, what its **upstream tasks** did, notes from the manager, and **done looks like** (its `done` criteria plus `verify` commands).

Its verify steps start with a scope check. A change to a path another task owns, a hotspot or a migration fails the attempt and is fed back. A change to a file that **no task owns** is granted to the task instead, recorded in `claims.json` and added to its `owns`. The report is structured output with typed `open_issues`.

## Review

After every task has merged: the job's final verify, then a reviewer with a fresh context. When the diff exceeds `review_diff_chars`, one reviewer per area runs in parallel and an integration reviewer judges acceptance and cross-area wiring from their verdicts. Every acceptance criterion needs evidence (`file:line` or a test); a criterion that is unassessed, unmet or unevidenced becomes a blocker. A reviewer that returns no verdict ends the job as `review_failed`, never `done`.

## Routing and escalation (inside every task)

Type is `<category>:<area>`: categories `rename docs config bugfix feature refactor test migration review research`; area is the package's area from `scan`, or `cross`.

The tier sets the starting model and effort on the ladder `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/high → opus/xhigh`. Fable or anything stronger is used only with the user's say-so. Check one decision with `router.py route --type bugfix:api --tier T1`.

Each config gets 2 attempts. API overloads and rate limits are retried with backoff first and never count against a model. Then the failure is classified:

| Cause | This task | Future tasks |
|---|---|---|
| environment (services down, a verify command that can't run) | stop | nothing |
| context (missing knowledge) | retry with a hint | proposal: AGENTS.md note |
| effort (cut corners, ran out of turns) | same model, more effort | evidence for a floor raise |
| capability (tried, still wrong) | next model | evidence for a floor raise |

Learning, all as proposals the user approves:
- A floor raise is proposed after ≥2 failed tasks at a config for a type within 30 days, with a failure rate above 50%. Approved floors expire after 30 days or when a new model version ships. About 10% of tasks probe one rung cheaper, so floors can come back down.
- After each job, a cheap extractor reads what the manager, subagents and reviewer wrote and proposes AGENTS.md notes.
- `router.py status` shows the cost per completed task including the coordination it caused.

## Budget

`budget_usd` is passed to every model call as `--max-budget-usd` (the remaining amount), and the scheduler stops launching tasks once it is spent.
