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

## Orca mode

`scripts/orca_mode.py`. The parent agent is the Orca coordinator; the script is called once per event and never waits:

- `run` (or `resume`): `prepare_job` (setup, baseline, the manager's pending answer, the advisor's plan check), an Orca Run (`run-create`), then `pump`.
- `pump`: settle queued manager events synchronously, start a worker for each ready task up to the parallel cap (`worker-start --spec-file <brief> --worktree new-child|new-top-level --base-branch orchestrate/<job> --model <id> --effort <e>`), and return exit 10 while workers run. When nothing runs and every task is done, `review_round` runs; if the manager queues nothing for blocking findings, they become a `review-fix-<n>` task that depends on every other task and runs as a worker too.
- `worker-done`: the checks run_task.py runs, on the worker's worktree: scope (with claims), verify, the report at `.orchestrate/report.json` (kept out of git by `info/exclude`), and every done id. Pass: route ledger, `commit_and_merge`, `worker-release`, `worktree rm --delete-branch`, raised criteria, open-issue triage. Fail: the first failure at a model goes back to the same worker with `send`; the second is classified, and the ladder picks the next model, started in the same worktree with a note that a weaker attempt left changes. Out of escalations, or an environment cause: the task fails, its worker stays open, and the manager gets the event.
- `worker-lost`: a worker that stopped without `worker_done` counts as a failed attempt; it is replaced in the same worktree.
- One step at a time per job (`.orca-step.lock`); the skill-files fingerprint is retaken on start and resume and checked at every `worker-done`.

## The manager

It is woken only when judgment is needed:
- **a task fails**, after routing has already escalated models
- **a finished subagent reports `open_issues`** that no rule settles: `fyi` issues are just listed, and `needs_file` issues whose files belong to one not-started task amend that task directly
- **the reviewer requests changes**
- **the user answers its question**

It decides in a background thread. The scheduler keeps launching tasks the decision can't affect, and events that arrive meanwhile reach it as one `batch`. It replies (as schema-checked structured output) with actions: `retry`, `split`, `add_task`, `amend`, `skip`, `fix_round`, `accept`, `ask_human` or `abort`. The script validates them as one batch (ownership, the dependency graph, verify not weakened, migrations gated, limits) and applies all or none. A reply that can't be parsed is re-asked once, then turned into a question for the user. Defaults: at most 12 interventions per job and 2 manager retries per task. Every decision is listed in `report.md`. `"manager": {"enabled": false}` falls back to plain stop-on-failure and one generic fix round.

Subagent-written text (reports, open issues, log tails, the diff) reaches the manager, reviewer, advisor and integrator only inside `<untrusted source="..." id="...">` tags. The opening and closing tags carry the same random id, generated per block, so text inside can't close the fence early. Prose is sanitized and size-capped; code and diffs keep `<<<` and `>>>` as written, and bidi control characters are shown as `[U+202E]` instead of being dropped.

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

Reviewers and the advisor report every finding with a `severity` and a `confidence`, and the harness decides what blocks (`blocking_findings`): blocker or major, unless the model marked it low confidence. The model's own `verdict` is kept as `model_verdict`, but the findings set the verdict. This follows Anthropic's code-review harness guidance: a reviewer told to be selective finds bugs and then leaves them out. An unhandled edge case the reviewer didn't list as a finding is added at the severity it gave the case. Judgment schemas put the evidence fields first and the verdict last, because structured output is generated in field order. Reviewer and advisor prompts put the material first (diff, reports, verify output, each fenced), then the playbook and the definition of done, then the task.

## Routing and escalation (inside every task)

Type is `<category>:<area>`: categories `rename docs config bugfix feature refactor test migration review research`; area is the package's area from `scan`, or `cross`.

Every task starts at the worker role (Opus 5.5 at medium) on the ladder `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/low → opus/medium → opus/high → opus/xhigh → fable/high`, whatever its tier; the tier sets how many done criteria it needs. `run_task.py --start` (from `models.worker`) moves the start, and learned floors only matter above it. An effort failure, or any failure below the model's default effort, re-runs on the same model at `high`; a capability failure moves to the next model at its default effort, with Opus going to `xhigh` before Fable. `plan.max_model` caps it. Fable calls carry `--fallback-model opus`, and a call that fell back is not routing evidence. `router.py ladder` prints the ladder and role defaults; `router.py route --type bugfix:api --tier T1` checks one decision.

**The bar.** Briefs number the definition of done: `[D#]` for the task's `done`, `[U#]` for `UNIVERSAL_BAR` plus `plan.quality_bar`. `run_task.py --done-ids` checks the report's `done_check` after verify passes, and any gap is a failed attempt. `raise_bar()` adopts criteria from subagent reports, the manager's `raise_bar` action, reviewers' `raise_bar` and blocking findings. Each goes into a not-started task's `done`, otherwise into `acceptance`; additions are deduplicated and capped. `apply_actions` rejects any change that drops a `done` or `acceptance` criterion. Every addition is logged in `job.json` → `bar`.

Role models live in `scripts/models.py`: advisor Fable 5.2 at medium (asked again at high when medium finds nothing), planner Fable 5.2 at medium, manager, worker, reviewer, area reviewers, cleanup and integrator Opus 5.5 at medium, failure classifier Haiku 5.5 at low, lessons Haiku at medium. Override per plan with `models`.

The advisor runs at two points. `plan_gate` asks it about `plan.json` before any task starts; a blocker/major finding or a `revise` verdict ends the job as `advisor_blocked` (exit 9), and `--approve advisor:plan` (through the approval hook) overrides it. After a review round passes, `final_gate` runs the cleanup subagent on the integration branch (kept only if it edits just the job's files and verify still passes), then asks the advisor for the final check; its blocking findings go to the manager as `review_changes_requested` with `source: advisor` and become criteria the next round must meet. An advisor with no usable answer fails closed. Outside a job, `orchestrate.py advise` runs the same checks for a single task.

Separately, `plan.advisor` turns on Claude Code's advisor tool inside each worker (`run_task.py --advisor`). The worker decides when to call it, so the prompt tells it when: before committing to an approach, when an error repeats, and before the final report (on an Anthropic benchmark Opus rarely consulted unprompted and scored 7 points below Opus alone). `max_model` caps the advisor, and an advisor ranked below the worker isn't passed. A launch refused because of the advisor (for example Fable's usage-credits consent) is re-run without it and recorded as `advisor_off`, not as a model failure. Each advisor call re-reads the whole worker session uncached, which is why it stays opt-in.

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
