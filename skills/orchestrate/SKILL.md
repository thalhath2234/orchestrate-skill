---
name: orchestrate
description: "Use when a coding task should be routed, planned, split across parallel subagents, merged and reviewed. Grills the user first, then triages; small tasks run as one routed task, big ones as a job."
---

# Orchestrate

You are the **parent**. You decide *what* to do: grill the user, then triage, plan and split. The scripts do the mechanical work: capacity caps, worktrees, handoffs, retries, model escalation, merging, review and reports. Follow the steps below in order.

**Keep done in view.** The first thing you ask the user is what done looks like, in their words. That sentence (`definition_of_done`) goes verbatim into the plan, every subagent's brief, the manager's, reviewer's and advisor's prompts, and your final report. Judge every step against it.

**Models.** Advisor: Fable 5.2 at medium, re-asked at high when medium finds nothing. Planner: Fable 5.2 at medium. Manager, workers, reviewer, UI reviewer and cleanup: Opus 5.5 at medium (workers escalate on failure). Per plan: `"models": {"<role>": "model/effort"}`.

**The advisor is asked twice:** before a plan executes and before the result is handed over for pushing. Both happen inside a job automatically; for a single task you run them (Step 3a).

Scripts are in `scripts/` next to this file (Python 3.9+, stdlib only, macOS/Linux, `git` and `claude` CLIs on PATH). Set `S` once:

```bash
S=
for d in "${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/scripts" ~/.claude/skills/orchestrate/scripts \
  ~/.agents/skills/orchestrate/scripts "$(ls -d ~/.claude/plugins/cache/*/orchestrate/*/skills/orchestrate/scripts 2>/dev/null |
  python3 -c 'import re,sys; v=lambda p: [int(n) for n in re.findall(r"\d+", p.split("/")[-4])]; print(max(sys.stdin.read().splitlines(), key=v, default=""))')"; do
  [ -d "$d" ] && S=$d && break
done
echo "S=$S"
```

It takes the plugin you are running first, then a manual install, then the newest cached plugin version, and prints the copy it picked. Use that path for the rest of the run.

Support skills, which you load when you reach that step:
- **orchestrate-grill**: the interview that comes before any plan (Matt Pocock's grill-with-docs).
- **orchestrate-plan**: how to plan, split, tier, assign file ownership and write `plan.json`.
- **orchestrate-manage**: the playbook of the **manager agent** (one Opus 5.5 session per job that supervises the subagents). Load it to understand or second-guess its decisions.
- **orchestrate-review**: what the integrator, reviewer and UI reviewer subagents check. Load it if you need to judge a review.

Who does what inside a job, and how the manager, the parallelism cap, subagent briefs and routing work: [references/internals.md](references/internals.md). You rarely need it to run a job.

## Step 1: Grill (always, first)

First run `python3 $S/orchestrate.py status --repo <repo> --open`. If it lists jobs, show them to the user and ask whether to resume one (then follow the outcome-table row for its status) or start new (they may `clean` the old one). Never answer a gate, a manager question or an advisor override for them.

Load the **orchestrate-grill** skill and follow it before any planning:

1. Gather facts. Run `python3 $S/orchestrate.py scan --repo <repo>` (packages/areas, verify commands, hotspots, setup), read the glossary, ADRs and AGENTS.md, and skim the relevant code.
2. **Ask what done looks like** as the first question of the first round, with no suggested answer: the definition must be in the user's own words. Keep their answer verbatim; "yes" or "all recommended" doesn't answer it, so ask again.
3. Grill the user round by round. Each round asks the whole frontier as numbered questions, each with your recommended answer (except the done question). Write glossary terms and ADRs as they settle.
4. **If the repo has a web UI** (`scan` returns a `ui_suggestion`) and the change touches it, settle which pages and flows prove the UI is done (for example "the schedule page shows a Share button; clicking it shows the copied toast"). Ask in the grill if the answer isn't obvious from the request. These become the plan's UI pages.
5. Post the closing summary (definition of done, goal, acceptance, non-goals, decisions, terms, UI pages if any, proposed route) and **wait for the user to confirm it**.

Size the session to the request: one short round for a trivial change, as many as the design tree needs for a feature. Skip it only if the user says *skip grilling*; even then, ask what done looks like. Planning and editing start once the user confirms.

## Step 2: Triage

Classify the confirmed request:

| Tier | Signals | Route |
|---|---|---|
| T0 | One file, mechanical, describable exactly | **single task** |
| T1 | Clear scope, ≤ ~5 files, one package, follows existing patterns | **single task** |
| T1x | ≤ ~10 files across 2–3 packages, contract already clear, no migration | **single task**: `--type feature:cross --tier T2` |
| T2 | More than that across several modules or packages, or needs some design | **job** (plan + parallel subagents) |
| T3 | Architecture, auth, migrations, cross-cutting refactor, unclear scope | **job**, with gates and the user's sign-off on the plan |

Every handoff between subagents loses context, and a job adds a manager and a reviewer on top. Take the single-task route whenever one agent can hold the whole change. The UI review (screenshots checked by a UI reviewer) runs only in jobs: when a small UI change should be checked on screen, run it as a one-task job (Step 3b).

## Step 3a: Single task (T0/T1/T1x)

Inside Orca, run a single task as a one-task job instead (Step 3b with a plan of one task), so it runs as an Orca worker with the same checks, merge and final advisor check. The steps below are for headless use.

1. **Ask the advisor first.** Record the base commit (`git rev-parse HEAD`), then:
   ```bash
   python3 $S/orchestrate.py advise --repo <repo> --goal "<task description>" \
     --done "<the user's definition of done, verbatim>" --acceptance "<criterion>" ... --verify "<cmd>"
   ```
   Exit 0: proceed. Exit 9: it found blocker/major problems; fix the task description, criteria or verify commands and ask again, or resolve the finding as the advisor rule below says. Exit 1: the advisor gave no usable answer, so the check has not happened; ask again, and don't proceed without it. Adopt its `raise_bar` criteria.
2. **Run the task:**
   ```bash
   python3 $S/run_task.py --repo <repo> --type <category>:<area> --tier T1 \
     --verify "<command that proves it works>" "<precise task description, including the definition of done, the confirmed decisions and non-goals>"
   ```
   It starts on the worker model (Opus 5.5 at medium), retries, escalates and records the outcome. Exit codes: 0 ok, 3 environment broken, 4 gave up, 7 budget, 9 declined by a safety classifier (`refusal_category` in the output says which; rephrasing the task or the user's verification program is the way forward, not a stronger model). Use `--baseline "<cmd>"` when `--verify` includes a test that is supposed to fail right now.
3. **Ask the advisor before the result is pushed:** the same `advise` command with `--final --base <base commit>`. Exit 9: fix the findings (another `run_task` with them as the task) and ask again. Exit 1: no usable answer, so ask again; don't hand over unchecked. Then report the result against the definition of done.

## Step 3b: Job (T2/T3)

1. **Plan.** Load the **orchestrate-plan** skill and write `plan.json` (for example `~/.local/share/orchestrate/plans/<job>.json`) from the confirmed grilling summary: copy the user's definition of done verbatim into `definition_of_done`, and its goal, acceptance, `non_goals` and `decisions` into the plan. Set the bar high: every `done` and acceptance criterion must be checkable, and the bar only rises from there (subagents, the manager and reviewers add criteria during the job; nothing removes one). If you lack the repo context to plan well, let a planner subagent (Fable 5.2 at medium) draft it:
   ```bash
   python3 $S/orchestrate.py plan --repo <repo> --goal "<goal>" --done "<the user's definition of done>" \
     --acceptance "<criterion>" --decision "<decision>" --non-goal "<non-goal>"   # one flag per item, repeat as needed
   ```
   It keeps those verbatim. Then review and adjust what it wrote. It can't ask the user, so it lists the choices it made for them in `assumptions`: put each one to the user, move an accepted one into `decisions` word for word, re-grill a rejected one, then delete the field.
   - **Web UI.** When `scan` returned a `ui_suggestion` and the job changes the UI, put a `ui` block in the plan (start from the suggestion's `dev`, `url` and `paths`, and check the dev command and port against the repo), and give each task that changes the UI a `ui` list of the pages that show its change, with steps for the flows settled in the grill. The review then screenshots those pages in Chrome and a UI reviewer checks them. See the **orchestrate-plan** skill.
   - **Commit the docs.** If grilling changed the glossary or ADRs, ask the user to commit those files before running (jobs start from the last commit). Subagents read the glossary automatically.
2. **Validate** until it is clean and has no ownership-gap or `unconfirmed:` warnings left:
   ```bash
   python3 $S/orchestrate.py validate plan.json
   ```
3. **Give the user the dashboard link, always,** once the plan validates and before `run`, even when you don't wait for their OK:
   ```bash
   python3 $S/orchestrate.py dashboard <job>     # prints the URL; starts the dashboard or reuses a running one
   ```
   Paste the URL it prints. Exit 1 with nothing on stdout means it is turned off (`ORCHESTRATE_DASHBOARD=0`) or could not start; stderr says which, so tell the user that instead. The page waits for the job and shows it from the first step.

   **Show the user** the plan in the same message: tasks, tiers, waves, gates, the final verify and, if any, the UI pages to be screenshotted. Proceed on their OK. A T3 job or any gated task always waits for that OK.
4. **Run it.** Before any task starts, the job asks the advisor about the plan; after the reviewer passes, a cleanup pass tidies the diff and the advisor gives the final check before the job can end `done`. How the workers run depends on where you are:

   **Inside Orca** (the default when `ORCA_AGENT_SESSION_ID` is set): you are the Orca coordinator, and each task runs as an Orca worker in its own child worktree, where the user can watch it and talk to it. The script keeps the brain: briefs, routing, checks, merges, the manager, review and the advisor all run in it, one step per command.
   ```bash
   python3 $S/orchestrate.py run plan.json      # exit 10: workers started, end your turn
   ```
   Then **end your turn**. Orca wakes you with each worker's mail. Handle only this job's workers this way:
   - **`worker_done`:** run `python3 $S/orchestrate.py worker-done <job> --dispatch <dispatchId> --report-path <reportPath>`, with the two values copied from the message's Payload, in the background (Bash `run_in_background`, timeout 2 hours): it checks and merges the work and starts what became ready, and the last one runs the review, which can take many minutes. When it exits: 10 means workers are still running, so end your turn; any other code goes to the outcome table below.
   - **An `escalation` with a `stopReason`, or a worker that `exited` or is `lost`** (also when `status` shows a task running whose worker is gone; a resume keeps running tasks and waits for their reports): run `python3 $S/orchestrate.py worker-lost <job> --dispatch <dispatch id> --reason "<what Orca said>"` the same way.
   - **A `question` from a worker:** answer it with `orca orchestration reply --id <message id> --body "..."` when the plan, its decisions or the brief answer it. If it needs a product decision, ask the user first and reply with their words.
   - Leave the job's workers to the script: it starts them, sends failed checks back to them, starts stronger models in the same worktree, and releases them after merging. Don't start, steer or stop them yourself, and don't poll; Orca wakes you.

   **Outside Orca** (or with `--workers headless`): one detached process runs every worker as `claude -p`, because jobs outlive tool-call timeouts:
   ```bash
   python3 $S/orchestrate.py run plan.json --detach
   python3 $S/orchestrate.py wait <job> --timeout 540     # repeat while exit code is 75
   ```
   Use `status <job>` for a progress table, `metrics <job>` for the cost split, and `brief plan.json <task-id>` to preview a subagent's handoff.

   `run` and `resume` log `dashboard: <url>` on stderr (with `--detach`, the `dashboard` key of their JSON). If it differs from the link you gave, give the user the new one.
5. **Handle the outcome** (exit code of `wait`, or of the Orca step that ended the job):

| Code | Status | What you do |
|---|---|---|
| 0 | done | Summarize `report.md` for someone who hasn't watched the job: start with whether the user's definition of done is met, in plain words, then the acceptance evidence and how the definition of done was raised during the job. Leave out task ids and labels the user hasn't seen, or say what they are. The work is on branch `orchestrate/<job>`. Give the user the diff and merge commands; the user merges. |
| 5 | awaiting_approval | Ask the user about each gated task. If they agree, run `resume <job> --approve <id> --detach` (Claude Code asks them to allow it), then wait again. In Orca mode `--detach` is ignored: the resume starts the workers and exits 10, so end your turn. |
| 10 | workers_running | Orca mode only: workers are running. End your turn; Orca wakes you when one reports. |
| 3 | environment | Baseline or setup failed, or the plan has a `ui` block and no Chrome was found, or the UI preflight failed (something already serves `ui.url`'s port, `ui.dev` doesn't bring the app up, or Chrome can't take a screenshot). Show the error, fix the environment or the plan's `baseline`/`setup`/`ui` (install Chrome or Chromium, or point `$ORCHESTRATE_CHROME` at it; stop the server on that port; correct the dev command; dropping the `ui` block is the user's call), then `resume <job>`. |
| 4 | failed | Read `~/.local/share/orchestrate/jobs/<job>/logs/<task>.log` and the report (in Orca mode, the failed task's worker stays open: read its chat and worktree). Usually the fix is to split the task, add `reads`, sharpen `done`, or correct `verify`. Lowering a `done` criterion is the user's call: ask them, because the job is judged by the bar they agreed to. Edit `jobs/<job>/plan.json`, then `resume <job>`. Done tasks are kept. |
| 6 | review_failed | Show the reviewer's or the advisor's findings, or say that one produced no verdict (then `resume <job>` reviews again). If the summary says "UI review could not run: <why>" or that the UI reviewer did not assess some screenshots, fix that (the dev command, a port already in use, Chrome; `logs/_devserver.log` has the server's output) and `resume <job>`; that round doesn't count toward `review_rounds`. Otherwise ask the user whether to fix by hand, add a task and resume, or accept. The advisor's final "revise" is overridden only by the user: `resume <job> --approve advisor:final` (Claude Code asks them to allow it). |
| 8 | awaiting_human | The **manager** needs a decision. Put its question (with its recommended answer) to the user word for word, then relay the user's own answer: `resume <job> --answer "<their answer>" --detach` (Claude Code asks them to allow it). Wait again. |
| 9 | advisor_blocked | The advisor says the plan won't reach done (or gave no usable answer). The message shows `The advisor read done as: ...`: compare it with the user's definition of done first. If it misreads done, treat the block as suspect: tighten `definition_of_done` or `acceptance` in `jobs/<job>/plan.json` and resume rather than override. Show its findings. Fix `jobs/<job>/plan.json` and `resume <job>` (it asks again), or resolve the finding as the advisor rule below says. Only if the user decides to run the plan as is: `resume <job> --approve advisor:plan --detach` (Claude Code asks them to allow it). |
| 7 | budget | Ask the user to raise `budget_usd` or stop. |

`wait` exit 1 means the detached job process died: `resume <job> --detach`, then wait again. If it says `no job <job>`, the run never started: read the `log` file named in `run`'s JSON. `run` exits 2 before starting anything when `plan.job` is already taken (a finished job, or a leftover `orchestrate/<job>` branch): pick a new `job` slug.

6. **Clean up** when the user has the branch: `python3 $S/orchestrate.py clean <job>`. It removes the worktrees and moves the screenshots to `jobs/<job>/screenshots`. The dashboard keeps running for later jobs; `python3 $S/orchestrate.py dashboard --stop` stops it.

## Rules

- **The user approves.** Routing and AGENTS.md proposals (`router.py proposals`), gates (`--approve`) and answers to the manager (`--answer`) are the user's calls: tell them what is pending and relay their words. The plugin's hook makes Claude Code ask the user before any of those commands runs. Run `python3 $S/orchestrate.py doctor --repo <repo>` once per session before the first job: stop and tell the user on an ERROR (exit 1); relay a WARNING (a missing approval hook prints the hook to add).
- The job's result stays on `orchestrate/<job>`. The user merges, pushes and deletes branches. If anything changes on the branch after the job ended `done` and before it is pushed, run `orchestrate.py advise <plan> --final --base <base commit> --repo <worktree>` again first.
- **Resolve each advisor finding.** Fix what it names, contest it with something the advisor can Read on the next ask (a file and symbol, an ADR, or a decision the user stated), or put it to the user. Wanting to disagree is not evidence. Add a decision only when the user said it, in their words: the advisor treats decisions as settled.
- Job worktrees belong to the job while it runs; inspect them read-only.
- Update or reinstall the orchestrate skills only when no job is running (`status` shows it). A running job fingerprints them and stops with a SECURITY failure if they change. To try new tools during a job, run them from the source repo.
- The job starts from the last **commit**. Tell the user if they have uncommitted work it won't see.
- State (learned routing, jobs, logs) lives in `~/.local/share/orchestrate`, or `$ORCHESTRATE_DIR`.
