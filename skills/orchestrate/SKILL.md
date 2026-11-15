---
name: orchestrate
description: "Use when a coding task should be routed, planned, split across parallel subagents, merged and reviewed. Grills the user first, then triages; small tasks run as one routed task, big ones as a job."
---

# Orchestrate

You are the **parent**. You decide *what* to do: grill the user, then triage, plan and split. The scripts do the mechanical work: capacity caps, worktrees, handoffs, retries, model escalation, merging, review and reports. Follow the steps below in order.

Scripts are in `scripts/` next to this file (Python 3.9+, stdlib only, macOS/Linux, `git` and `claude` CLIs on PATH). Set `S` once:

```bash
S=$(ls -d ~/.agents/skills/orchestrate/scripts ~/.claude/skills/orchestrate/scripts \
  ~/.claude/plugins/cache/*/orchestrate/*/skills/orchestrate/scripts 2>/dev/null | head -1)
```

Support skills, which you load when you reach that step:
- **orchestrate-grill**: the interview that comes before any plan (Matt Pocock's grill-with-docs).
- **orchestrate-plan**: how to plan, split, tier, assign file ownership and write `plan.json`.
- **orchestrate-manage**: the playbook of the **manager agent** that supervises a running job. Load it to understand or second-guess its decisions.
- **orchestrate-review**: what the integrator and reviewer subagents check. Load it if you need to judge a review.

## Who does what

| Role | Who | Does |
|---|---|---|
| **You** (the parent) | this conversation | grill the user, triage, plan, launch, relay the manager's questions, report |
| **Manager** | one persistent Opus session per job | supervises every subagent: diagnoses failures, retries with better instructions, splits tasks that are too big, turns open issues and review findings into targeted tasks, asks the user when a decision is needed |
| **Subagents** | `claude -p` workers in their own worktrees | do one task each, on the model routing picks |
| **Integrator / reviewer** | fresh sessions | resolve merge conflicts, review the merged result |
| `orchestrate.py` | script | capacity caps, worktrees, briefs, merging, and validating and applying the manager's actions |

## Step 1: Grill (always, first)

Load the **orchestrate-grill** skill and follow it before any planning:

1. Gather facts. Run `python3 $S/orchestrate.py scan --repo <repo>` (packages/areas, verify commands, hotspots, setup), read the glossary, ADRs and AGENTS.md, and skim the relevant code.
2. Grill the user round by round. Each round asks the whole frontier as numbered questions, each with your recommended answer. Write glossary terms and ADRs as they settle.
3. Post the closing summary (goal, acceptance, non-goals, decisions, terms, proposed route) and **wait for the user to confirm it**.

Size the session to the request: one short round for a trivial change, as many as the design tree needs for a feature. Skip it only if the user says *skip grilling*. Never plan or edit code before the user confirms.

## Step 2: Triage

Classify the confirmed request:

| Tier | Signals | Route |
|---|---|---|
| T0 | One file, mechanical, describable exactly | **single task** |
| T1 | Clear scope, ≤ ~5 files, one package, follows existing patterns | **single task** |
| T2 | Several modules, or 2+ packages, or needs some design | **job** (plan + parallel subagents) |
| T3 | Architecture, auth, migrations, cross-cutting refactor, unclear scope | **job**, with gates and the user's sign-off on the plan |

Also make it a **job** whenever the work crosses package boundaries in a monorepo (for example API, web and mobile), even if each part is small.

## Step 3a: Single task (T0/T1)

```bash
python3 $S/run_task.py --repo <repo> --type <category>:<area> --tier T1 \
  --verify "<command that proves it works>" "<precise task description, including the confirmed decisions and non-goals>"
```
It picks the model and effort, retries, escalates and records the outcome. Exit codes: 0 ok, 3 environment broken, 4 gave up. Report the result. Use `--baseline "<cmd>"` when `--verify` includes a test that is supposed to fail right now.

## Step 3b: Job (T2/T3)

1. **Plan.** Load the **orchestrate-plan** skill and write `plan.json` (for example `~/.local/share/orchestrate/plans/<job>.json`) from the confirmed grilling summary: copy its goal, acceptance, `non_goals` and `decisions` into the plan. If you lack the repo context to plan well, let a planner subagent draft it with `python3 $S/orchestrate.py plan --repo <repo> --goal "<goal + decisions>"`. Then review and adjust what it wrote.
   - **Commit the docs.** If grilling changed the glossary or ADRs, ask the user to commit those files before running (jobs start from the last commit). Subagents read the glossary automatically.
2. **Validate** until clean:
   ```bash
   python3 $S/orchestrate.py validate plan.json
   ```
   It checks the schema, the dependency graph and file ownership (tasks that can run at the same time must not own the same paths). It prints the waves and the parallel cap for this machine.
3. **Show the user** the plan: tasks, tiers, waves, gates and the final verify. Proceed on their OK. A T3 job or any gated task always needs this step.
4. **Run detached**, because jobs outlive tool-call timeouts:
   ```bash
   python3 $S/orchestrate.py run plan.json --detach
   python3 $S/orchestrate.py wait <job> --timeout 540     # repeat while exit code is 75
   ```
   Use `status <job>` for a progress table. Preview a subagent's handoff with `brief plan.json <task-id>`.
   **Offer the user the live dashboard.** Start it in the background: `python3 $S/dashboard.py <job> --port 8765`, then open http://127.0.0.1:8765. It shows:
   - the task graph by wave, with live status, model paths, timers and costs
   - a timeline and an activity feed, including the manager's reasoning
   - per-task detail: goal, owned files, done criteria, the report, the live log and the brief
   - banners for approvals and manager questions

   It is read-only and safe while a job runs.
5. **Handle the outcome** (exit code of `wait`):

| Code | Status | What you do |
|---|---|---|
| 0 | done | Summarize `report.md`. The work is on branch `orchestrate/<job>`. Give the user the diff and merge commands. **Never merge into their branch yourself.** |
| 5 | awaiting_approval | Ask the user about each gated task. If they agree: `resume <job> --approve <id> --detach`, then wait again. |
| 3 | environment | Baseline or setup failed. Show the error, fix the environment or the plan's `baseline`/`setup`, then `resume <job>`. |
| 4 | failed | Read `~/.local/share/orchestrate/jobs/<job>/logs/<task>.log` and the report. Usually the fix is to split the task, add `reads`, clarify `done`, or correct `verify`. Edit `jobs/<job>/plan.json`, then `resume <job>`. Done tasks are kept. |
| 6 | review_failed | Show the reviewer's findings. Ask the user whether to fix by hand, add a task and resume, or accept. |
| 8 | awaiting_human | The **manager** needs a decision. The question (with its recommended answer) is in the report and in `status`. Put it to the user word for word, then pass their answer back: `resume <job> --answer "<their answer>" --detach`, and wait again. Don't answer it yourself. |
| 7 | budget | Ask the user to raise `budget_usd` or stop. |

6. **Clean up** when the user has the branch: `python3 $S/orchestrate.py clean <job>`.

## The manager (automatic)

The manager is woken only when judgment is needed, so it stays cheap:
- **a task fails**, after routing has already escalated models
- **a finished subagent reports `open_issues`** it couldn't fix inside its scope
- **the reviewer requests changes**
- **the user answers its question**

It replies with actions: `retry` (with instructions or corrected owns/reads/verify), `split`, `add_task`, `amend`, `skip`, `fix_round`, `accept`, `ask_human` or `abort`. The script validates them as one batch (ownership, the dependency graph, limits) and applies all or none. Gates stay with the user. The defaults are at most 12 interventions per job and 2 manager retries per task; past that, it asks the user. Every decision is listed in `report.md`. Turn it off with `"manager": {"enabled": false}`, which falls back to plain stop-on-failure and one generic fix round.

## How parallelism is capped (automatic)

`capacity` takes the minimum of:
- **memory:** available RAM, including cgroup/container limits, × 0.8 ÷ `mem_per_agent_gb` (default 1.5)
- **CPU:** free CPUs (cores − 1-minute load) ÷ `cpu_per_agent` (default 1)
- **hard max:** 6, for API rate limits
- `plan.max_parallel`, if set

It re-checks free memory before every launch. Don't override it unless the user asks. For heavy monorepos (tsc, Go and Gradle builds in verify), set `"mem_per_agent_gb": 2.5` or more in the plan. Check the numbers with `python3 $S/orchestrate.py capacity`.

## What each subagent receives (automatic)

A brief generated from its task:
- its **goal** and the job goal
- the paths it **owns**, which are the only ones it may change
- what to **read first**: the nearest AGENTS.md files and the task's `reads`
- what its **upstream tasks** did (summary, interfaces, notes, files)
- **done looks like**: its `done` criteria plus `verify` commands
- the rules, and a JSON **report** format

Its verify steps always start with a scope check, so out-of-scope edits fail the attempt and are fed back to it. It runs in its own git worktree, on the model and effort chosen by routing, and escalates on failure.

## Routing and escalation (inside every task)

Type is `<category>:<area>`:
- categories: `rename docs config bugfix feature refactor test migration review research`
- area: the package's area from `scan`, or `cross`

The tier sets the starting model and effort. The ladder is `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/high → opus/xhigh`. Fable or anything stronger is used only with the user's say-so. Check a single decision with `python3 $S/router.py route --type bugfix:api --tier T1`.

Each config gets 2 attempts. Then the failure is classified:

| Cause | This task | Future tasks |
|---|---|---|
| environment (services down, flaky check) | stop | nothing |
| context (missing knowledge) | retry with a hint | proposal: AGENTS.md note |
| effort (cut corners) | same model, more effort | evidence for a floor raise |
| capability (tried, still wrong) | next model | evidence for a floor raise |

How learning works:
- A floor raise is proposed after ≥2 failed tasks at a config for a type within 30 days, with a failure rate above 50%.
- Approved floors expire after 30 days or when a new model version ships.
- About 10% of tasks probe one rung cheaper, so floors can come back down.

## Rules

- Never approve routing proposals. Tell the user they exist: `router.py proposals | approve <id> | reject <id> | status` are for the user.
- Never merge `orchestrate/<job>` into the user's branch, push, or delete their branches.
- Don't edit files in job worktrees while a job is running.
- The job starts from the last **commit**. Tell the user if they have uncommitted work it won't see.
- State (learned routing, jobs, logs) lives in `~/.local/share/orchestrate`, or `$ORCHESTRATE_DIR`.
