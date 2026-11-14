# orchestrate

A multi-agent coding orchestrator for Claude Code, for single repos and monorepos. The parent agent triages the request. Small tasks run as one routed task. Big ones become a **job**:

1. Plan the job.
2. Split it into subtasks, each with its own files.
3. Run Claude subagents in parallel, with the count capped by what the machine can handle.
4. Merge their work.
5. Have a fresh Opus reviewer check it, and fix what it finds.

Every subagent starts on the cheapest model likely to work and escalates on failure. Routing learns per task type, but only through proposals that you approve.

```
request ─► triage ─┬─ T0/T1 ─► run_task (route → attempt ×2 → classify → escalate) ─► done
                   └─ T2/T3 ─► plan.json ─► validate (DAG + file ownership) ─► run
                                 ┌──────────── waves, ≤ cap in parallel ────────────┐
                                 │ worktree + setup ─► brief ─► run_task ─► scope ✓ │ per task
                                 └──────────────────────┬───────────────────────────┘
                                   commit ─► merge into orchestrate/<job> (integrator on conflict)
                                 ─► final verify ─► Opus review ─► fix round ─► report
```

## Install

```bash
# Any agent that supports Agent Skills (Claude Code, Codex, Cursor, …)
npx skills add thalhath2234/orchestrate-skill -g

# or as a Claude Code plugin (v2.1.292+)
claude plugin install orchestrate --marketplace thalhath2234/orchestrate-skill
```

This installs three skills:

| Skill | For |
|---|---|
| `orchestrate` | The parent's playbook: triage, single task or job, run, wait, handle outcomes |
| `orchestrate-plan` | How to plan: split by file ownership, contract first, tier each subtask, done criteria, monorepo rules, `plan.json` schema |
| `orchestrate-review` | What the integrator and reviewer subagents check, severities, and output format |

Requirements: Python 3.9+ (stdlib only), macOS or Linux, and `git` and `claude` on PATH.

## Use

In Claude Code, T3 Code and similar tools, type `/orchestrate` followed by what you want, for example:

```
/orchestrate add shareable read-only schedule links across api, web and mobile
```

The parent scans the repo, decides between a single task and a job, writes and validates a plan, shows it to you, runs it detached and reports back. The result is a branch `orchestrate/<job>`. **It never merges into your branch.**

To run the scripts by hand (`S=skills/orchestrate/scripts`):

```bash
python3 $S/orchestrate.py scan --repo .                 # packages/areas, verify cmds, hotspots, setup
python3 $S/orchestrate.py capacity                      # how many subagents this machine can take
python3 $S/orchestrate.py plan --repo . --goal "..."    # optional: Opus drafts plan.json
python3 $S/orchestrate.py validate plan.json            # waves, ownership conflicts, cap
python3 $S/orchestrate.py brief plan.json <task-id>     # exact handoff a subagent will get
python3 $S/orchestrate.py run plan.json --detach        # then:
python3 $S/orchestrate.py wait <job> --timeout 540      # 0 done · 5 needs approval · 4 failed · 3 env · 6 review
python3 $S/orchestrate.py resume <job> --approve <task> # after gates or fixes; done tasks are kept
python3 $S/orchestrate.py clean <job> [--branches]

python3 $S/run_task.py --repo . --type bugfix:api --tier T1 --verify "make test" "Fix …"   # single task
python3 $S/router.py proposals | approve <id> | reject <id> | status                       # routing (you)
```

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

MIT licensed.
