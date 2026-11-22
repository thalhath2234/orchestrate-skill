# orchestrate

A multi-agent coding orchestrator for Claude Code, for single repos and monorepos. It starts by **asking you what done looks like**, then **grills you about what you actually want** (Matt Pocock's grill-with-docs), and writes the glossary and ADRs as decisions settle. Your definition of done, in your words, goes into every prompt of the run. Then the parent agent triages the request. Small tasks run as one routed task. Big ones become a **job**:

1. Plan the job.
2. Ask the **advisor** (Fable 5.2) whether the plan reaches done, before anything runs.
3. Split it into subtasks, each with its own files.
4. Run Claude subagents in parallel, with the count capped by what the machine can handle.
5. Merge their work.
6. Have a fresh reviewer check every edge case, and fix what it finds. For a web app, a UI reviewer also looks at screenshots of the running app, on desktop and mobile.
7. Clean up the diff, and ask the advisor for a final check before the branch is handed over.

A **manager agent** (one persistent Opus session per job) supervises the subagents throughout. Open issues that a rule can settle (an FYI, or a file that a not-yet-started task owns) are handled without it. When a task fails, a subagent reports something else outside its scope, or the reviewer requests changes, the manager reads the logs and code and decides what to do. It thinks in the background while other tasks keep running, and events that arrive meanwhile reach it as one batch:
- retry with better instructions or corrected ownership
- split a task that's too big
- add a follow-up task
- skip
- ask you

The script validates and applies its actions. Every model judgment (manager, reviewer, classifier, integrator, planner) is schema-checked structured output (`claude --json-schema`), and a judgment that can't be parsed never counts as a pass.

Every subagent starts on the cheapest model likely to work and escalates on failure. Routing learns per task type, but only through proposals that you approve.

```
request ─► grill (Q1: what does done look like? → rounds → glossary/ADRs → you confirm) ─► triage
                   ┌──────────┘
                   ├─ T0/T1 ─► advise ─► run_task (route → attempt ×2 → classify → escalate) ─► advise --final ─► done
                   └─ T2/T3 ─► plan.json ─► validate (DAG + file ownership) ─► run ─► advisor: plan ok?
                                 ┌──────────── waves, ≤ cap in parallel ────────────┐
                                 │ worktree + setup ─► brief ─► run_task ─► scope ✓ │ per task
                                 └──────────────────────┬───────────────────────────┘
                                   commit ─► merge into orchestrate/<job> (integrator on conflict)
                                 ─► final verify ─► Chrome shots (plan.ui): dev server, desktop + mobile
                                 ─► review, every edge case (per area + integration when big) ─► UI reviewer
                                 ─► cleanup ─► advisor: final check ─► report
        rules ◄── open issues: fyi → report · needs a pending task's file → amend it
        manager (Opus, background, batched) ◄── task failed · other open issues · review findings · your answers
                                   ──► retry · split · add_task · amend · skip · accept · ask you
```

## Install

```bash
# Claude Code is the supported host. Other Agent Skills hosts (Codex, Cursor, …) can load the
# playbooks, but every run needs the `claude` CLI and its login, and they don't get the approval hook.
npx skills add thalhath2234/orchestrate-skill -g

# or as a Claude Code plugin (v2.1.292+)
claude plugin install orchestrate --marketplace thalhath2234/orchestrate-skill
```

This installs five skills:

| Skill | For |
|---|---|
| `orchestrate` | The parent's playbook: grill, triage, single task or job, run, wait, handle outcomes |
| `orchestrate-grill` | The interview before any plan: design-tree rounds with recommended answers, glossary and ADRs written as terms and decisions settle, and a closing summary you confirm. Matt Pocock's `grilling` + `domain-modeling`, vendored (see Credits) |
| `orchestrate-plan` | How to plan: split by file ownership, contract first, tier each subtask, done criteria, monorepo rules, `plan.json` schema |
| `orchestrate-manage` | The manager agent's playbook: events, diagnosis, actions, and when to ask you |
| `orchestrate-review` | What the integrator, reviewer and UI reviewer subagents check, severities, and output format |

Requirements: Python 3.9+ (stdlib only), Linux (CI runs `make test` there with Python 3.9 and 3.13; macOS is not supported), and `git` and `claude` on PATH, and `bubblewrap` (`bwrap`) to confine workers. UI review also needs Google Chrome or Chromium (only for plans with a `ui` block).

## Update

Update only when no job is running (`orchestrate.py status <job>`): a running job fingerprints the skill files and stops if they change.

```bash
# npx skills install (what `npx skills add ... -g` set up)
npx skills update -g -y orchestrate orchestrate-grill orchestrate-plan orchestrate-manage orchestrate-review

# Claude Code plugin install (restart Claude Code afterwards)
claude plugin update orchestrate
```

`npx skills update` follows the repo's default branch. A plugin install only picks up a change when the `version` in `.claude-plugin/plugin.json` changes, so a fix pushed without a version bump doesn't reach it. After updating, run `python3 ~/.agents/skills/orchestrate/scripts/orchestrate.py doctor` (or the plugin's copy). It checks that `claude` is on PATH and that the approval hook is installed, and prints the hook to add when it isn't: an `npx skills` install doesn't include it.

## Use

In Claude Code, T3 Code and similar tools, type `/orchestrate` followed by what you want, for example:

```
/orchestrate add shareable read-only schedule links across api, web and mobile
```

The parent first lists this repo's unfinished jobs, if any, and asks whether to resume one. Then it scans the repo and **grills you** round by round until nothing is assumed. Every question comes with a recommended answer except the first, what done looks like, which you answer in your own words. You can answer the rest with "all recommended", or say *skip grilling* for trivial changes. Once you confirm the summary, it decides between a single task and a job, writes and validates a plan (your decisions and non-goals go into every subagent's brief and the reviewer's prompt), shows it to you with a link to the live dashboard, runs it detached and reports back. The result is a branch `orchestrate/<job>`. **It never merges into your branch.**

To run the scripts by hand (`S=skills/orchestrate/scripts`):

```bash
python3 $S/orchestrate.py scan --repo .                 # packages/areas, verify cmds, hotspots, setup
python3 $S/orchestrate.py capacity                      # how many subagents this machine can take
python3 $S/orchestrate.py doctor --repo .               # claude on PATH (exit 1 if not), approval hook installed (prints it if not)
python3 $S/orchestrate.py status --repo . --open        # this repo's unfinished jobs and the next step for each
python3 $S/orchestrate.py plan --repo . --goal "..." --done "..." --decision "..."   # optional: Fable drafts plan.json (also --acceptance, --non-goal)
python3 $S/orchestrate.py advise plan.json              # advisor's second opinion (exit 9 = revise · 1 = no usable answer, ask again)
python3 $S/orchestrate.py validate plan.json            # waves, ownership conflicts, cap
python3 $S/orchestrate.py brief plan.json <task-id>     # exact handoff a subagent will get
python3 $S/orchestrate.py run plan.json                 # inside Orca: starts Orca workers, exit 10
python3 $S/orchestrate.py worker-done <job> --dispatch <id> --report-path <path>   # when Orca wakes you with a worker_done
python3 $S/orchestrate.py run plan.json --detach        # outside Orca: headless, then:
python3 $S/orchestrate.py wait <job> --timeout 540      # 0 done · 5 approval · 8 manager asks you · 9 advisor says revise · 10 workers running (Orca) · 4 failed · 3 env · 6 review · 7 budget · 75 still running, wait again · 1 process gone, resume
python3 $S/orchestrate.py resume <job> --answer "..."   # answer the manager's question
python3 $S/orchestrate.py resume <job> --approve <task> # after gates or fixes; done tasks are kept
python3 $S/orchestrate.py metrics <job>                 # cost by role, cache reads, turns, first-pass rate, manager load, advisor re-check yield
python3 $S/orchestrate.py status <job>                  # progress table (no job: the newest; --repo R: that repo's newest)
python3 $S/orchestrate.py dashboard [<job>] [--port 8765] [--no-start | --stop]   # start (or reuse) the live dashboard and print its link
python3 $S/orchestrate.py clean <job> [--branches]   # removes the worktrees; screenshots move to jobs/<job>/screenshots
python3 $S/orchestrate.py guard-hook                    # print the approval hook, for npx-skills installs

python3 $S/run_task.py --repo . --type bugfix:api --tier T1 --verify "make test" "Fix …"   # single task
python3 $S/router.py proposals | approve <id> | reject <id> | status                       # routing (you)
```

## Workers: Orca or headless

**Inside Orca** (the default when the agent runs in an Orca session), the agent that runs `/orchestrate` becomes the Orca coordinator, and each task runs as an **Orca worker**: its own child worktree branched from `orchestrate/<job>`, its own chat you can watch and talk to, started on the model routing picked. The script keeps the brain and never waits. `run` prepares the job and starts the ready workers, then the agent ends its turn. When a worker sends `worker_done`, Orca wakes the agent, which runs `orchestrate.py worker-done`. The script checks that worker's worktree (scope, verify, every done criterion). If the work passes, it commits, merges and releases the worker. If not, it sends the failure back to the same worker, or starts a stronger model in the same worktree. Then it starts whatever became ready, and after the last merge it runs the review. A failed task's worker stays open so you can see what happened. In this mode `budget_usd` only covers the script's own model calls (manager, reviewer, advisor, classifier), because Orca workers' cost isn't visible to it, and the plan's `advisor` option (the advisor tool inside workers) isn't passed to Orca workers. They are sessions Orca starts with your own Claude Code settings, so an `advisorModel` you set with `/advisor` does apply to them, and adds a second model's cost to each.

**Headless** (outside Orca, or `--workers headless`), one detached process runs every task as `claude -p` itself, as in earlier versions.

## UI review

For a web app, the review also looks at the running app. Add a `ui` block to the plan (the parent proposes one from what `scan` detects) and list, on each task that changes the UI, the pages that show its change:

```jsonc
"ui": {
  "dev": "pnpm --filter web dev",          // serves the app, run in the integration worktree
  "url": "http://127.0.0.1:5173",          // what the dev command serves
  "paths": ["apps/web/**"]                 // optional: only when the diff touches these
},
"tasks": [{"id": "web-share", ...,
  "ui": [{"path": "/schedules/1", "name": "share",
          "steps": [{"click": "[data-test=share]"}, {"wait_for": ".toast"}, {"screenshot": "after-share"}]}]}]
```

Before any work starts, the job checks the dev command once (the preflight): nothing may already serve `ui.url`'s port, `ui.dev` must bring the app up on the code as it is, and Chrome must start and take one screenshot. If any of these fails, the job stops as `environment` before any tokens are spent.

Then each review round, only when the final verify passed:

1. The harness starts `ui.dev` in the integration worktree and waits for `ui.url` to answer (`ready_timeout_s`, default 180). This happens before the code review, so a capture that can't run costs no tokens.
2. Headless Chrome, driven over the DevTools protocol, opens every page at desktop (1440×900) and mobile (390×844, touch, mobile user agent), runs its steps (`click`, `type`, `press`, `wait_for`, `wait_ms`, `screenshot`), and saves screenshots. With no pages declared anywhere it captures `/`. For each shot it records the HTTP status, the title, console errors, uncaught exceptions, failed requests, failed steps, and any `alert`/`confirm`/`prompt` dialog (they are accepted, so a confirm-before-delete flow goes on). A step that fails stops that page's flow: the remaining steps are skipped and the page's final shot is taken. A page taller than 1.5 viewports is also saved as viewport-sized tiles for the reviewer. A page that stops responding (an endless loop in the app) gets no shot and becomes a blocker finding, and the capture goes on with the next page.
3. The **UI reviewer** (Opus 5.5) views every screenshot and checks it against the acceptance criteria and decisions. It must assess every shot: it is asked once more for any it skipped (findings from both answers count), and if it still skips one the round fails closed. Its findings join the round's review findings, prefixed `[UI]`, and go through the same fix round. A UI finding carries the screenshot's path and browser evidence into the fix brief, and the next round's UI reviewer gets it back and must answer whether it is fixed, from the new shot of the same page; one it doesn't answer, or answers not fixed, stays a finding (it is not turned into a "covered by a test" criterion). A page it marks not ok becomes a major finding unless one of its blocking findings names that shot. If the fix changes nothing, the round ends `review_failed` instead of asking a fresh reviewer for a second opinion on the same code.
4. The harness adds findings from the browser evidence that no reviewer can wave through: a page with no HTTP response or a 5xx and a page that stopped responding are blockers; a 4xx page, a failed step or an uncaught exception is major unless the UI reviewer lists that shot and kind under `expected` with a reason (a not-found page on purpose, a stale selector); then it is a minor note with that reason. A finding that merely names the shot waives nothing. Every shot's status, step errors, exceptions and failed requests reach the UI reviewer; only console messages are cut to fit, errors before warnings. A page is captured down to 4000 CSS px; `report.md` notes each shot that was cut there.
5. Cleanup, which runs after the reviewers pass, leaves alone the files the screenshots depend on (those under `ui.paths` and those owned by a task with `ui` pages; every changed file without `ui.paths`), so what ships is what was judged.

Screenshots go to `<integration worktree>/.orchestrate/screenshots/round-<n>/<page>--<viewport>[--<step>].png`. The harness writes `.orchestrate/.gitignore` containing `*`, so they never show in `git status`, scope checks or commits, and your own `.gitignore` is left alone. The dev server's output goes to `jobs/<job>/logs/_devserver.log`.

- **It needs Chrome.** It uses `$ORCHESTRATE_CHROME` when that is set (nothing else is tried); otherwise the first of `google-chrome-stable`, `google-chrome`, `chromium`, `chromium-browser` on PATH, then the macOS app. A plan with a `ui` block and no working Chrome stops as `environment` before any tokens are spent. Each Chrome gets a temporary profile, removed afterwards with the scratch folder Chrome links from it (`/tmp/com.google.Chrome.*`).
- **It fails closed.** If the UI review can't run (the dev server isn't ready in time, something already holds `ui.url`'s port before the dev command starts so the shots would show the wrong code, the dev server dies during the capture, or the browser crashes), the round is `review_failed` with "UI review could not run: <why>", never a pass. A UI reviewer whose answer can't be parsed, or that leaves a screenshot unassessed, counts the same way. Such a round doesn't use up `review_rounds`: fix the cause and `resume`. A dev command that exits before the app answers, after the preflight showed it works, is the job's bug: it becomes a blocker finding for the fix round.
- **One dev server at a time per state dir.** A lock (`devserver.lock` in the state dir) lets one job's dev server run at once. It runs in its own process group, which is killed afterwards, also when the orchestrator gets SIGTERM or SIGHUP; after a SIGKILL, the next run stops what was left (it records each process with its start time, so a reused pid is never hit). Keep `ui.url`'s port free.
- **The worktree stays clean.** Whatever the dev server writes in the integration worktree (a sqlite file, generated types, a rewritten `tsconfig.json`) is put back when it stops, so it never reaches a scope check or the job's branch. Ignored files (build caches) are left alone. The fix round also never commits a path outside the job's files.
- **`clean` keeps the screenshots.** It moves them to `jobs/<job>/screenshots` before removing the worktrees (merged with rounds an earlier `clean` kept), and the report and dashboard follow. A resumed job that captures again moves them back into the integration worktree first, so every round stays in one place.

## Watch a job

`run` and `resume` start the live dashboard (or reuse one already serving the same state dir) and log its link on stderr before any work starts (`--detach` also puts it in the JSON it prints, under `"dashboard"`):

```
[orchestrate 14:02:11] dashboard: http://127.0.0.1:8765/?job=share-links
```

The parent gives you that link with the plan, before the run starts (`python3 $S/orchestrate.py dashboard <job>` prints it on stdout). The page works before the job exists: it shows the plan when it is saved as `plans/<job>.json` in the state dir (else it says it's waiting for the job), and picks the job up when it starts. `--no-start` only prints the link of one already running. It binds 127.0.0.1, uses port 8765 or the next free one up to 8785, and is off when `ORCHESTRATE_DASHBOARD=0`. It answers only loopback host names; to open it through a proxy such as `tailscale serve`, set `ORCHESTRATE_DASHBOARD_HOSTS=<name>` before it starts. It's read-only, uses only the Python standard library, and is safe while a job runs.

The dashboard has no login. It listens on 127.0.0.1 only, but on a shared machine every local user can read the logs, plans, screenshots and files it shows: set `ORCHESTRATE_DASHBOARD=0` there.

The dashboard keeps running after the job; one serves every job in the state dir, and concurrent runs share it. It exits after 12 hours with no request and no running job (an open page keeps it up); set `ORCHESTRATE_DASHBOARD_IDLE_HOURS` to change that, 0 to turn it off. After a skill update, the next `run`, `resume` or `dashboard` restarts a dashboard it started itself on the same port, so the link keeps working with the new page, and an open tab reloads itself onto it. A dashboard it did not start (say, one you started by hand, or one from an older version that doesn't answer `/api/ping`) is left alone and the link moves to the next port. Stop the dashboards of this state dir with `python3 $S/orchestrate.py dashboard --stop`. It stops the ones it started or that run this `dashboard.py`, and names any other it leaves running (exit 1).

It's laid out like an IDE:
- **Explorer:** the job, its waves and tasks with live status and timers, plus `plan.json`, `report.md`, the detached run's output and every log (task logs, `_orchestrator` with every progress line in both modes, `_integration`, `_devserver`). In Orca mode a task's log holds what the harness did for it (worker started, checks and their output, merge); the worker itself runs in its Orca terminal. Other side views: Screenshots, Review, Manager & activity, Metrics and Plan.
- **Editor tabs:** the job overview (goal, definition of done, acceptance marked met or unmet by the latest review, decisions, non-goals, budget, a wave board of tasks and a timeline). A task tab has its overview, live log (follow, wrap, find), the exact brief it received, its report and open issues, the model path with each call's cost, and its screenshots.
- **Screenshots:** every UI review round's shots by page and viewport, filterable by task, viewport and round, each with its HTTP status, error counts and the UI reviewer's ok or issue. Click one for a lightbox with the browser errors, the reviewer's observation, and the same page side by side across rounds.
- **Bottom panel:** Problems (review findings; click one to see the code around the flagged line), Terminal (the detached output or the integration log), Activity (tasks starting and finishing, the manager's decisions with its reasoning, review rounds, UI reviews, advisor verdicts) and Output (the dev server log).
- **Status bar and banners:** job status, branch, problem counts, tasks done, slots in use, elapsed time and cost against the budget; banners with copyable commands for manager questions, gated tasks, an advisor "revise" and a stopped process.
- **Command palette** (Ctrl/Cmd+K) to jump to any task, file, log, screenshot or finding. Links to a view (`#task/<id>/log`, `#shot/...`) can be shared. Light and dark themes follow the system.

## What each subagent gets

A generated brief in two parts. The first is the same for every task of the job, so parallel subagents share a cached prompt prefix: the rules, the report format, acceptance, decisions, non-goals, and the root glossary and AGENTS.md inlined when small. The second is the task:
- its goal
- the paths it **owns**. A file no task owns may be edited if the task needs it; the scope check then assigns it to the task.
- what to read first: the nearest `AGENTS.md` files and the task's `reads`
- what its upstream tasks built (their summaries, interfaces, notes and files)
- **done looks like**: its criteria and verify commands

It reports as structured output, with typed open issues (`needs_file`, `needs_decision`, `fyi`).

It works in its own git worktree, on a branch made from the current integration head, so it sees its dependencies' merged work.

## Parallelism cap

`cap = min(available RAM × 0.8 ÷ mem_per_agent_gb, free CPUs ÷ cpu_per_agent, 6, plan.max_parallel)`

- Available RAM is cgroup-aware (containers, systemd scopes).
- Free CPUs is cores minus the 1-minute load.
- The cap is re-checked before every launch, so a busy machine runs fewer agents.
- Tune it with `mem_per_agent_gb` and `max_parallel` in the plan, or with `ORCHESTRATE_MEM_PER_AGENT_GB`, `ORCHESTRATE_CPU_PER_AGENT` and `ORCHESTRATE_HARD_MAX`.

## Monorepos

`scan` detects:
- pnpm, npm, yarn and bun workspaces, Go modules, Cargo, Python packages, turbo and nx
- per-package verify commands (Makefile targets like `test-api`, or `pnpm --filter <pkg> run test`)
- hotspots: lockfiles, root configs, `go.work`
- the setup command each worktree needs

Validation rejects plans where tasks that can run at the same time own overlapping paths, and tasks that own migrations without a gate. It warns about **ownership gaps**: unowned files in the same Go package as an owned file, or imported by one. Planning puts shared contracts first and gives each hotspot to a single task.

## Safety

- **Judgment roles are read-only.** The reviewers, the advisor, the manager and the planner are offered only Read, Grep and Glob, and the failure classifier and lessons extractor no tools at all. The integrator, which has to edit the conflict, also gets Edit and Write, but no shell or git. None of them gets your MCP servers, and your own allow rules can't add a tool back. Workers and the cleanup pass aren't restricted this way.
- **Approvals go through you.** The plugin installs a `PreToolUse` hook: any `--approve`, `--answer` or `router.py approve` command the agent runs shows up in Claude Code's own permission prompt for you to allow. An approval typed in chat approves nothing. (`npx skills` installs: add the hook printed by `orchestrate.py guard-hook`.) The hook reads the command text, so it catches the plain forms, not a command hidden in a script: confinement is what stops a worker editing job state.
- **Workers are confined (Linux, bubblewrap).** A worker, its verify commands, setup on merged work, the dev server and cleanup run on a read-only machine. What they can write: their own worktree, a `/tmp` of their own, and package caches of their own per worktree (so the first install in each downloads again). Other new files in the home directory vanish with the command. They can't `git add`, commit or touch history, can't see `/run/user` (session bus, agents) or sockets in your home, and can't signal processes outside. The network and localhost services work as usual. A tool that must write somewhere else fails with "read-only file system": list that path in `ORCHESTRATE_SANDBOX_WRITE` (`:`-separated) before starting the job. Worker transcripts are kept under `<state>/jail` until the worktree is removed, not in `~/.claude/projects`. Without a working `bwrap` the job still runs and says so in the log, `report.md` and `doctor`; `"sandbox": true` refuses to run instead, `false` turns it off. It is not a VM: workers can read what you can read and use the network, and the Docker socket and localhost services (an IDE's or agent's command port too) are reachable, any of which can act outside the sandbox. Orca workers are not confined, so their gates are only as strong as your own permission settings.
- **Edits stay in the task.** The scope check sees committed and uncommitted changes, and the merge checks again on exactly the commit it merges: a file another task owns, a hotspot or a migration is never merged.
- **A branch can't configure the harness.** The harness's own git never runs the repo's hooks (put lint in `verify`), and judgment calls don't load project `.claude` settings: they get your committed `CLAUDE.md` from the main checkout's HEAD.
- **Subagent text is evidence, not instructions.** Reports, open issues and logs reach the manager, reviewer and integrator inside fences, sanitized and size-capped.
- **The orchestrator watches its own files.** If a subagent modifies the orchestrate skill files, the job stops.
- **The manager is bounded.** Its actions are validated as one all-or-nothing batch, against the resulting plan. It can't approve gates, change your decisions, drop or trivialise a task's verify commands, or add an ungated migration. It gets at most 12 interventions per job and 2 retries per task, then it asks you.
- **Reviews fail closed.** Every acceptance criterion needs evidence (a `file:line` or a test). A reviewer that returns no verdict (after one re-ask) ends the job as `review_failed`, never `done`, and so does a configured UI review that can't run.
- **A conflict resolution must pass verify.** The integrator's resolution is committed only if the merging task's verify and that of every finished task owning a conflicted file still pass; otherwise the merge is aborted and the task fails with the output.
- **Nothing skipped reads as passed.** `report.md` lists every check that didn't run or that you overrode: a baseline skipped with `--skip-baseline`, an advisor check you overrode, gated tasks you approved.
- **The budget holds per call.** `budget_usd` is passed to every model call as `--max-budget-usd`.
- **Risky tasks wait for you.** Gated tasks (for example migrations) pause the job until you approve them.
- **A job starts from your last commit and only writes to `orchestrate/<job>` branches.**
- **You stay in charge of routing.** Routing changes and AGENTS.md notes are proposals that you approve.

## Which model does what

Defaults follow Anthropic's model guidance ([choosing a model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model), [cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence), the use-case guides) and this repo's evals:

| Role | Model / effort | What it does |
|---|---|---|
| Advisor | Fable 5.2 / medium, then high if medium finds nothing | Second opinion before the plan executes and before hand-over. A clean pass at medium is only believed after a second look at high |
| Planner | Fable 5.2 / medium | One call decides the whole job and writes the checkable proof of your definition of done |
| Manager | Opus 5.5 / medium | Supervises the subagents: retries, splits, follow-up tasks, questions to you |
| Worker | Opus 5.5 / medium | Every subtask, whatever its tier; failures re-run at high, then xhigh, then Fable 5.2 |
| Reviewer | Opus 5.5 / medium | Fresh-context review that lists every edge case of the diff and how each is handled (area reviewers too) |
| UI reviewer | Opus 5.5 / medium | Looks at every screenshot of the running app, desktop and mobile, against acceptance and decisions |
| Cleanup | Opus 5.5 / medium | Removes debug output, dead code and stray TODOs from the job's own files; reverted if verify fails |
| Integrator (conflicts) | Opus 5.5 / medium | Merge conflicts between subagent branches |
| Failure classifier | Haiku 5.5 / low | Sorts a failed attempt into environment / context / effort / capability |
| Lessons extractor | Haiku 5.5 / medium | Proposes AGENTS.md notes after a job |

Whether the advisor's second, high-effort look pays is measured: `report.md` has an `Advisor:` line (re-checks run, how often they found a blocking issue, their cost) and `router.py status` adds it up across jobs. If it stays near zero over many jobs, lower `ADVISOR_ESCALATE` in `scripts/models.py`.

`"advisor": "fable"` in the plan (off by default, even if your own `advisorModel` setting is on; Orca workers follow your setting) also gives each worker Claude Code's [advisor tool](https://code.claude.com/docs/en/advisor), with instructions on when to consult it; it pays off on long tasks that are hard in spots, and each call re-reads the whole session. Fable calls fall back to Opus when your account has no Fable access. `"max_model": "opus"` in the plan keeps everything at or below Opus (the advisor becomes Opus at high); `"models": {"<role>": "model/effort"}` overrides one role.

## The bar: what done looks like

Every step works against an explicit definition of done, and the definition only gets stricter:

- **You say it first.** The grill's first question is what done looks like. Your answer is kept verbatim as `definition_of_done` (required) and opens every brief, the manager's, reviewer's and advisor's prompts, and the report.
- **Your answers bind.** An answer you give the manager mid-job is added to the plan's `decisions`, so later briefs, the reviewers and the final advisor hold the work to it. `report.md` lists it under "Decided during the job".
- **The advisor checks it twice.** Before the plan runs it asks whether the plan reaches your definition of done (exit 9 stops the job until the plan is fixed or you override with `--approve advisor:plan`); before hand-over it checks the finished work against it. Its missing criteria raise the bar; its blocker and major findings go through a fix round. It also restates done in its own words (in the exit-9 message and `report.md`): a restatement that misreads done makes its findings suspect.
- **The planner writes it out.** `acceptance` (required) for the job and `done` for each task, as checkable criteria: at least 1/2/3/3 per T0/T1/T2/T3 task, with vague words flagged. Every task is also held to a fixed bar: tests that fail without the change, including an error path; verify run by the agent; no TODOs or dead code; errors handled at the boundaries it touched; repo conventions; everything wired up. `quality_bar` adds repo-wide standards.
- **Subagents prove it.** The brief numbers every criterion (`[D1]…`, `[U1]…`). The report's `done_check` must show each one met with evidence (file:line, a test, command output); a gap sends the task back like a failing test.
- **Other models raise it.** A subagent that finds something missing proposes a criterion for a later task or the job. The manager can `raise_bar`. Reviewers add criteria they find missing, and every blocker or major finding becomes a criterion the next review must show fixed. Additions are checked for duplicates and capped (3 per source, 40 per job).
- **Nothing lowers it.** Any manager action that drops or rewords a criterion is rejected, and a retry with new `done` items adds them to the old ones. Only you lower the bar, by editing the plan. `report.md` lists every criterion added during the job, who added it and why, and the lessons step can turn recurring ones into AGENTS.md rules.

## How routing learns

The ladder is `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/low → opus/medium → opus/high → opus/xhigh → fable/high`. Every task starts at the worker role, Opus 5.5 at medium (`"models": {"worker": ...}` or `ORCHESTRATE_MODEL_WORKER` moves it), and re-runs failures higher. Each config gets 2 attempts. A failure is classified as one of:

| Cause | This task | Future tasks |
|---|---|---|
| environment | stop | nothing |
| context | retry with a hint | proposes an AGENTS.md note |
| effort | same model at `high` (then `xhigh`) | evidence for a floor raise |
| capability | next model at its default effort; Opus tries `xhigh` before Fable | evidence for a floor raise |

Below a model's default effort, any failure first restores the effort.

API overloads and rate limits are retried with backoff and never count against a model, and a test about rate limits is code, not a broken environment.

Floor raises need repeated evidence (≥2 failed tasks and more than half failing, within 30 days). They expire when the window ends or a new model version ships. About 10% of tasks probe one rung cheaper, so floors can come back down. After each job, a cheap extractor turns what the agents learned into proposed AGENTS.md notes, and `router.py status` shows the cost per completed task including the coordination it caused. All of it is proposals you approve. State lives in `~/.local/share/orchestrate` (or `$ORCHESTRATE_DIR`).

## Develop

```bash
make test              # harness: e2e on a pnpm/Go monorepo with a fake `claude`, routing, eval runner, docs vs code
make eval-quick        # prompts: core snapshot evals against the real `claude` (costs money)
make eval-parent       # the parent skill and the UI reviewer alone (costs money)
make validate-plugin   # before a release, with release-check and live-smoke
make release-check     # fails if skills/, hooks/ or .claude-plugin changed since the last vX.Y.Z tag without a version bump
make live-smoke        # one Haiku call (about a cent): the plugin's approval hook really stops an --approve
```

CI runs `make test` on every pull request and push to main, on Linux with Python 3.9 and 3.13. Evals and the live smoke test never run in CI, because they cost tokens. Run `make live-smoke` before each release and after a Claude Code update that changes hooks or `-p`.

Any PR that changes `skills/`, `hooks/` or `.claude-plugin/` bumps `version` in `.claude-plugin/plugin.json`: plugin installs only update when it changes. Tag each release `vX.Y.Z`.

Every change to a `SKILL.md` or a prompt builder should ship with eval cases and pass `make eval-quick`. See [evals/README.md](evals/README.md).

## Credits

`skills/orchestrate-grill` vendors Matt Pocock's [`grilling`](https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling) and [`domain-modeling`](https://github.com/mattpocock/skills/tree/main/skills/engineering/domain-modeling) skills (together, `grill-with-docs`) from [mattpocock/skills](https://github.com/mattpocock/skills) at commit `5863806`, under the MIT License, © 2026 Matt Pocock. See `skills/orchestrate-grill/LICENSE-mattpocock`. They're renamed so installing this repo never overwrites your own copies of his skills.

Some of the release, packaging and docs-vs-code checks were inspired by Google's [Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model) (Apache-2.0); no text or code is copied from it.

MIT licensed.
