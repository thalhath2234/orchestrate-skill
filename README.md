# orchestrate

A multi-agent coding orchestrator for Claude Code, for single repos and monorepos. It starts by **grilling you about what you actually want** (Matt Pocock's grill-with-docs), and writes the glossary and ADRs as decisions settle. Then the parent agent triages the request. Small tasks run as one routed task. Big ones become a **job**:

1. Plan the job.
2. Split it into subtasks, each with its own files.
3. Run Claude subagents in parallel, with the count capped by what the machine can handle.
4. Merge their work.
5. Have a fresh Opus reviewer check it, and fix what it finds.

A **manager agent** (one persistent Opus session per job) supervises the subagents throughout. When a task fails, a subagent reports something outside its scope, or the reviewer requests changes, the manager reads the logs and code and decides what to do:
- retry with better instructions or corrected ownership
- split a task that's too big
- add a follow-up task
- skip
- ask you

The script validates and applies its actions.

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
                                 ─► final verify ─► Opus review ─► report
        manager (Opus, persistent) ◄── task failed · open issues · review findings · your answers
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
python3 $S/orchestrate.py plan --repo . --goal "..."    # optional: Opus drafts plan.json
python3 $S/orchestrate.py validate plan.json            # waves, ownership conflicts, cap
python3 $S/orchestrate.py brief plan.json <task-id>     # exact handoff a subagent will get
python3 $S/orchestrate.py run plan.json --detach        # then:
python3 $S/orchestrate.py wait <job> --timeout 540      # 0 done · 5 approval · 8 manager asks you · 4 failed · 3 env · 6 review
python3 $S/orchestrate.py resume <job> --answer "..."   # answer the manager's question
python3 $S/orchestrate.py resume <job> --approve <task> # after gates or fixes; done tasks are kept
python3 $S/orchestrate.py clean <job> [--branches]

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
- **Header tiles:** progress, elapsed time, cost against the budget, parallel slots and what's capping them, manager interventions, models used.
- **Banners** with copyable commands, for gated tasks waiting on you and for manager questions.
- **Task drawer:** click any task for its goal, owned paths, done criteria, verify commands, the model path with failure causes, its report and open issues, the live log, and the exact brief it received.

It uses only the Python standard library and refreshes every 2 seconds. A dropdown switches between jobs.

## What each subagent gets

A generated brief containing:
- its goal
- the paths it **owns**, which are the only ones it may change
- what to read first: the nearest `AGENTS.md` files and the task's `reads`
- what its upstream tasks built (their summaries, interfaces, notes and files)
- **done looks like**: its criteria and verify commands
- the rules, and a JSON report format

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

Validation rejects plans where tasks that can run at the same time own overlapping paths. Planning puts shared contracts first and gives each hotspot to a single task.

## Safety

- **Edits stay in the worktree.** Subagents' file edits are confined to their worktree (`blockWritesOutsideWorkingDirectories`). A scope check rejects any change outside the task's owned paths and feeds the error back to the subagent. `"sandbox": true` in the plan adds an OS sandbox around their shell commands.
- **The orchestrator watches its own files.** If a subagent modifies the orchestrate skill files, the job stops.
- **The manager is bounded.** Its actions are validated as one all-or-nothing batch. It can't approve gates or change your decisions. It gets at most 12 interventions per job and 2 retries per task, then it asks you.
- **Risky tasks wait for you.** Gated tasks (for example migrations) pause the job until you approve them.
- **A job starts from your last commit and only writes to `orchestrate/<job>` branches.**
- **You stay in charge of routing.** Routing changes and AGENTS.md notes are proposals that you approve.

## How routing learns

The ladder is `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/high → opus/xhigh`. Each config gets 2 attempts. A failure is classified as one of:

| Cause | This task | Future tasks |
|---|---|---|
| environment | stop | nothing |
| context | retry with a hint | proposes an AGENTS.md note |
| effort | same model, more effort | evidence for a floor raise |
| capability | next model | evidence for a floor raise |

Floor raises need repeated evidence (≥2 failed tasks and more than half failing, within 30 days). They expire when the window ends or a new model version ships. About 10% of tasks probe one rung cheaper, so floors can come back down. State lives in `~/.local/share/orchestrate` (or `$ORCHESTRATE_DIR`).

## Develop

```bash
python3 tests/test_orchestrate.py -v   # e2e on a pnpm/Go monorepo with a fake `claude`
python3 tests/test_routing.py -v
claude plugin validate .
```

## Credits

`skills/orchestrate-grill` vendors Matt Pocock's [`grilling`](https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling) and [`domain-modeling`](https://github.com/mattpocock/skills/tree/main/skills/engineering/domain-modeling) skills (together, `grill-with-docs`) from [mattpocock/skills](https://github.com/mattpocock/skills) at commit `5863806`, under the MIT License, © 2026 Matt Pocock. See `skills/orchestrate-grill/LICENSE-mattpocock`. They're renamed so installing this repo never overwrites your own copies of his skills.

MIT licensed.
