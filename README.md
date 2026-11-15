# orchestrate

A multi-agent coding orchestrator for Claude Code, for single repos and monorepos. It starts by **grilling you about what you actually want** (Matt Pocock's grill-with-docs), and writes the glossary and ADRs as decisions settle. Then the parent agent triages the request. Small tasks run as one routed task. Big ones become a **job**:

1. Plan the job.
2. Split it into subtasks, each with its own files.
3. Run Claude subagents in parallel, with the count capped by what the machine can handle.
4. Merge their work.
5. Have a fresh Fable reviewer check it, and fix what it finds.

A **manager agent** (one persistent Opus session per job) supervises the subagents throughout. Open issues that a rule can settle (an FYI, or a file that a not-yet-started task owns) are handled without it. When a task fails, a subagent reports something else outside its scope, or the reviewer requests changes, the manager reads the logs and code and decides what to do. It thinks in the background while other tasks keep running, and events that arrive meanwhile reach it as one batch:
- retry with better instructions or corrected ownership
- split a task that's too big
- add a follow-up task
- skip
- ask you

The script validates and applies its actions. Every model judgment (manager, reviewer, classifier, integrator, planner) is schema-checked structured output (`claude --json-schema`), and a judgment that can't be parsed never counts as a pass.

Every subagent starts on the cheapest model likely to work and escalates on failure. Routing learns per task type, but only through proposals that you approve.

```
request ─► grill (rounds of questions → glossary/ADRs → you confirm) ─► triage
                   ┌──────────┘
                   ├─ T0/T1 ─► run_task (route → attempt ×2 → classify → escalate) ─► done
                   └─ T2/T3 ─► plan.json ─► validate (DAG + file ownership) ─► run
                                 ┌──────────── waves, ≤ cap in parallel ────────────┐
                                 │ worktree + setup ─► brief ─► run_task ─► scope ✓ │ per task
                                 └──────────────────────┬───────────────────────────┘
                                   commit ─► merge into orchestrate/<job> (integrator on conflict)
                                 ─► final verify ─► Fable review (Opus per area + Fable integration when big) ─► report
        rules ◄── open issues: fyi → report · needs a pending task's file → amend it
        manager (Opus, background, batched) ◄── task failed · other open issues · review findings · your answers
                                   ──► retry · split · add_task · amend · skip · accept · ask you
```

## Install

```bash
# Any agent that supports Agent Skills (Claude Code, Codex, Cursor, …)
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
| `orchestrate-review` | What the integrator and reviewer subagents check, severities, and output format |

Requirements: Python 3.9+ (stdlib only), macOS or Linux, and `git` and `claude` on PATH.

## Use

In Claude Code, T3 Code and similar tools, type `/orchestrate` followed by what you want, for example:

```
/orchestrate add shareable read-only schedule links across api, web and mobile
```

The parent scans the repo and **grills you** round by round until nothing is assumed. You can answer "all recommended", or say *skip grilling* for trivial changes. Once you confirm the summary, it decides between a single task and a job, writes and validates a plan (your decisions and non-goals go into every subagent's brief and the reviewer's prompt), shows it to you, runs it detached and reports back. The result is a branch `orchestrate/<job>`. **It never merges into your branch.**

To run the scripts by hand (`S=skills/orchestrate/scripts`):

```bash
python3 $S/orchestrate.py scan --repo .                 # packages/areas, verify cmds, hotspots, setup
python3 $S/orchestrate.py capacity                      # how many subagents this machine can take
python3 $S/orchestrate.py plan --repo . --goal "..."    # optional: Fable drafts plan.json
python3 $S/orchestrate.py validate plan.json            # waves, ownership conflicts, cap
python3 $S/orchestrate.py brief plan.json <task-id>     # exact handoff a subagent will get
python3 $S/orchestrate.py run plan.json --detach        # then:
python3 $S/orchestrate.py wait <job> --timeout 540      # 0 done · 5 approval · 8 manager asks you · 4 failed · 3 env · 6 review
python3 $S/orchestrate.py resume <job> --answer "..."   # answer the manager's question
python3 $S/orchestrate.py resume <job> --approve <task> # after gates or fixes; done tasks are kept
python3 $S/orchestrate.py metrics <job>                 # cost by role, cache reads, turns, first-pass rate, manager load
python3 $S/orchestrate.py clean <job> [--branches]
python3 $S/orchestrate.py guard-hook                    # the approval hook, for npx-skills installs

python3 $S/run_task.py --repo . --type bugfix:api --tier T1 --verify "make test" "Fix …"   # single task
python3 $S/router.py proposals | approve <id> | reject <id> | status                       # routing (you)
```

## Watch a job

```bash
python3 $S/dashboard.py <job> --open        # http://127.0.0.1:8765, read-only, safe while a job runs
```

It's a live web dashboard:
- **Task graph:** tasks grouped by wave, with dependency edges, live status, model path, timer and cost. Manager-added tasks and retries are badged.
- **Timeline** of when each task ran.
- **Activity feed:** tasks starting and finishing, the manager's decisions with its reasoning, and review rounds.
- **Header tiles:** progress, elapsed time, cost against the budget, the cost split by role (manager share, cache reads), parallel slots and what's capping them, manager interventions and how many issues were settled by rule, models used.
- **Banners** with copyable commands, for gated tasks waiting on you and for manager questions.
- **Task drawer:** click any task for its goal, owned paths, done criteria, verify commands, the model path with failure causes, its report and open issues, the live log, and the exact brief it received.

It uses only the Python standard library and refreshes every 2 seconds. A dropdown switches between jobs.

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

- **Approvals go through you.** The plugin installs a `PreToolUse` hook: any `--approve`, `--answer` or `router.py approve` command the agent runs shows up in Claude Code's own permission prompt for you to allow. An approval typed in chat approves nothing. (`npx skills` installs: add the hook printed by `orchestrate.py guard-hook`.)
- **Edits stay in the worktree.** Subagents' file edits are confined to their worktree (`blockWritesOutsideWorkingDirectories`). A scope check rejects changes to paths another task owns, hotspots and migrations, and feeds the error back to the subagent. `"sandbox": true` in the plan adds an OS sandbox around their shell commands.
- **Subagent text is evidence, not instructions.** Reports, open issues and logs reach the manager, reviewer and integrator inside fences, sanitized and size-capped.
- **The orchestrator watches its own files.** If a subagent modifies the orchestrate skill files, the job stops.
- **The manager is bounded.** Its actions are validated as one all-or-nothing batch, against the resulting plan. It can't approve gates, change your decisions, drop or trivialise a task's verify commands, or add an ungated migration. It gets at most 12 interventions per job and 2 retries per task, then it asks you.
- **Reviews fail closed.** Every acceptance criterion needs evidence (a `file:line` or a test). A reviewer that returns no verdict ends the job as `review_failed`, never `done`.
- **The budget holds per call.** `budget_usd` is passed to every model call as `--max-budget-usd`.
- **Risky tasks wait for you.** Gated tasks (for example migrations) pause the job until you approve them.
- **A job starts from your last commit and only writes to `orchestrate/<job>` branches.**
- **You stay in charge of routing.** Routing changes and AGENTS.md notes are proposals that you approve.

## Which model does what

Defaults follow Anthropic's model guidance ([choosing a model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model), [cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence), the use-case guides) and this repo's evals:

| Role | Model / effort | Why |
|---|---|---|
| Planner | Fable 5.2 / high | One call decides the whole job; Fable is for "demanding reasoning" and "analysis carried through to a finished document" |
| Final reviewer | Fable 5.2 / high | Gates the merge. On Anthropic's defect-search benchmark Fable 5.2 beat Opus 5.5 at high by 1.6–5.9 points |
| Area reviewers (big diffs) | Opus 5.5 / high | Accuracy-critical reading; Opus lost 34 points at low effort on defect search |
| Manager | Opus 5.5 / medium | "For most agent workloads, start with Opus 5.5 at its default effort"; scored 13/13 on our evals, same as high |
| Integrator (conflicts) | Sonnet 5.5 / high | Everyday coding at Sonnet's default |
| Failure classifier | Haiku 5.5 / low | The ticket-routing guide's pick for classification; tied Sonnet on our evals |
| Lessons extractor | Haiku 5.5 / medium | Extraction |

Fable calls fall back to Opus when your account has no Fable access. `"max_model": "opus"` in the plan keeps everything at or below Opus; `"models": {"<role>": "model/effort"}` overrides one role.

## How routing learns

The ladder is `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/low → opus/medium → opus/high → opus/xhigh → fable/high`. Tasks start low (T0 haiku/medium, T1 sonnet/medium, T2 opus/low, T3 opus/medium) and re-run failures higher: Anthropic measured Opus 5.5 at low with failures re-run at high solving about 97% of SWE-bench Pro tasks for $0.17 each, against 95.3% for $0.29 with everything at high. Each config gets 2 attempts. A failure is classified as one of:

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
make test              # harness: e2e on a pnpm/Go monorepo with a fake `claude`, routing, eval runner
make eval-quick        # prompts: core snapshot evals against the real `claude` (costs money)
make validate-plugin
```

Every change to a `SKILL.md` or a prompt builder should ship with eval cases and pass `make eval-quick`. See [evals/README.md](evals/README.md).

## Credits

`skills/orchestrate-grill` vendors Matt Pocock's [`grilling`](https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling) and [`domain-modeling`](https://github.com/mattpocock/skills/tree/main/skills/engineering/domain-modeling) skills (together, `grill-with-docs`) from [mattpocock/skills](https://github.com/mattpocock/skills) at commit `5863806`, under the MIT License, © 2026 Matt Pocock. See `skills/orchestrate-grill/LICENSE-mattpocock`. They're renamed so installing this repo never overwrites your own copies of his skills.

MIT licensed.
