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
- **orchestrate-manage**: the playbook of the **manager agent** (one Opus 5.5 session per job that supervises the subagents). Load it to understand or second-guess its decisions.
- **orchestrate-review**: what the integrator and reviewer subagents check. Load it if you need to judge a review.

Who does what inside a job, and how the manager, the parallelism cap, subagent briefs and routing work: [references/internals.md](references/internals.md). You rarely need it to run a job.

## Step 1: Grill (always, first)

Load the **orchestrate-grill** skill and follow it before any planning:

1. Gather facts. Run `python3 $S/orchestrate.py scan --repo <repo>` (packages/areas, verify commands, hotspots, setup), read the glossary, ADRs and AGENTS.md, and skim the relevant code.
2. Grill the user round by round. Each round asks the whole frontier as numbered questions, each with your recommended answer. Write glossary terms and ADRs as they settle.
3. Post the closing summary (goal, acceptance, non-goals, decisions, terms, proposed route) and **wait for the user to confirm it**.

Size the session to the request: one short round for a trivial change, as many as the design tree needs for a feature. Skip it only if the user says *skip grilling*. Planning and editing start once the user confirms.

## Step 2: Triage

Classify the confirmed request:

| Tier | Signals | Route |
|---|---|---|
| T0 | One file, mechanical, describable exactly | **single task** |
| T1 | Clear scope, ≤ ~5 files, one package, follows existing patterns | **single task** |
| T1x | ≤ ~10 files across 2–3 packages, contract already clear, no migration | **single task**: `--type feature:cross --tier T2` |
| T2 | More than that across several modules or packages, or needs some design | **job** (plan + parallel subagents) |
| T3 | Architecture, auth, migrations, cross-cutting refactor, unclear scope | **job**, with gates and the user's sign-off on the plan |

Every handoff between subagents loses context, and a job adds a manager and a reviewer on top. Take the single-task route whenever one agent can hold the whole change.

## Step 3a: Single task (T0/T1/T1x)

```bash
python3 $S/run_task.py --repo <repo> --type <category>:<area> --tier T1 \
  --verify "<command that proves it works>" "<precise task description, including the confirmed decisions and non-goals>"
```
It picks the model and effort, retries, escalates and records the outcome. Exit codes: 0 ok, 3 environment broken, 4 gave up, 7 budget. Report the result. Use `--baseline "<cmd>"` when `--verify` includes a test that is supposed to fail right now.

## Step 3b: Job (T2/T3)

1. **Plan.** Load the **orchestrate-plan** skill and write `plan.json` (for example `~/.local/share/orchestrate/plans/<job>.json`) from the confirmed grilling summary: copy its goal, acceptance, `non_goals` and `decisions` into the plan. Set the bar high: every `done` and acceptance criterion must be checkable, and the bar only rises from there (subagents, the manager and reviewers add criteria during the job; nothing removes one). If you lack the repo context to plan well, let a planner subagent draft it with `python3 $S/orchestrate.py plan --repo <repo> --goal "<goal + decisions>"`. Then review and adjust what it wrote.
   - **Commit the docs.** If grilling changed the glossary or ADRs, ask the user to commit those files before running (jobs start from the last commit). Subagents read the glossary automatically.
2. **Validate** until it is clean and has no ownership-gap warnings left:
   ```bash
   python3 $S/orchestrate.py validate plan.json
   ```
3. **Show the user** the plan: tasks, tiers, waves, gates and the final verify. Proceed on their OK. A T3 job or any gated task always needs this step.
4. **Run detached**, because jobs outlive tool-call timeouts:
   ```bash
   python3 $S/orchestrate.py run plan.json --detach
   python3 $S/orchestrate.py wait <job> --timeout 540     # repeat while exit code is 75
   ```
   Use `status <job>` for a progress table, `metrics <job>` for the cost split, and `brief plan.json <task-id>` to preview a subagent's handoff. **Offer the user the live dashboard:** start `python3 $S/dashboard.py <job> --port 8765` in the background and open http://127.0.0.1:8765. It shows the task graph, a timeline, the manager's reasoning, per-task detail and the cost split, and it is read-only and safe while a job runs.
5. **Handle the outcome** (exit code of `wait`):

| Code | Status | What you do |
|---|---|---|
| 0 | done | Summarize `report.md`, including the acceptance evidence and how the definition of done was raised during the job. The work is on branch `orchestrate/<job>`. Give the user the diff and merge commands; the user merges. |
| 5 | awaiting_approval | Ask the user about each gated task. If they agree, run `resume <job> --approve <id> --detach` (Claude Code asks them to allow it), then wait again. |
| 3 | environment | Baseline or setup failed. Show the error, fix the environment or the plan's `baseline`/`setup`, then `resume <job>`. |
| 4 | failed | Read `~/.local/share/orchestrate/jobs/<job>/logs/<task>.log` and the report. Usually the fix is to split the task, add `reads`, sharpen `done`, or correct `verify`. Lowering a `done` criterion is the user's call, never yours. Edit `jobs/<job>/plan.json`, then `resume <job>`. Done tasks are kept. |
| 6 | review_failed | Show the reviewer's findings, or say that the reviewer produced no verdict (then `resume <job>` reviews again). Ask the user whether to fix by hand, add a task and resume, or accept. |
| 8 | awaiting_human | The **manager** needs a decision. Put its question (with its recommended answer) to the user word for word, then relay the user's own answer: `resume <job> --answer "<their answer>" --detach` (Claude Code asks them to allow it). Wait again. |
| 7 | budget | Ask the user to raise `budget_usd` or stop. |

6. **Clean up** when the user has the branch: `python3 $S/orchestrate.py clean <job>`.

## Rules

- **The user approves.** Routing and AGENTS.md proposals (`router.py proposals`), gates (`--approve`) and answers to the manager (`--answer`) are the user's calls: tell them what is pending and relay their words. The plugin's hook makes Claude Code ask the user before any of those commands runs. On an `npx skills` install, offer them `orchestrate.py guard-hook`, which prints the hook to add.
- The job's result stays on `orchestrate/<job>`. The user merges, pushes and deletes branches.
- Job worktrees belong to the job while it runs; inspect them read-only.
- Update or reinstall the orchestrate skills only when no job is running (`status` shows it). A running job fingerprints them and stops with a SECURITY failure if they change. To try new tools during a job, run them from the source repo.
- The job starts from the last **commit**. Tell the user if they have uncommitted work it won't see.
- State (learned routing, jobs, logs) lives in `~/.local/share/orchestrate`, or `$ORCHESTRATE_DIR`.
