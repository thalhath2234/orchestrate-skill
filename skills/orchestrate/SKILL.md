---
name: orchestrate
description: Use when choosing a Claude model/effort for a coding task or subagent, or running a task through the orchestrator. Starts cheap, escalates on failure, learns per task type; policy changes are proposals the user approves.
---

# Orchestrate

Pick the **starting** model and effort for a coding task from its tier, raised by any learned floor the user has **approved** for its task type. Escalate within a task when it fails. Never change routing for future tasks yourself: the orchestrator files proposals and the user approves them.

Scripts live in `scripts/` next to this file (Python 3.9+, stdlib only, macOS/Linux). Below, `$S` means `<this skill's base directory>/scripts`. If you don't know the base directory, find it with `ls -d ~/.agents/skills/orchestrate/scripts ~/.claude/skills/orchestrate/scripts ~/.claude/plugins/cache/*/orchestrate/*/skills/orchestrate/scripts 2>/dev/null | head -1`.

## Task type and tier

- **Task type** = `<category>:<area>`
  - category: `rename docs config bugfix feature refactor test migration review research`
  - area: a short lowercase slug for the part of the codebase, e.g. `api`, `web`, `mobile`, `infra`, or `cross` for changes spanning several. Keep areas coarse and use the same names every time.
- **Tier** → default start:

| Tier | Meaning | Start |
|---|---|---|
| T0 | One-file mechanical edit you could describe exactly | haiku/low |
| T1 | Clear scope, few files, follows existing patterns | sonnet/medium |
| T2 | Several files or modules, some design | sonnet/high (plan with `opusplan`) |
| T3 | Architecture, migrations, auth, cross-cutting refactor | opus/high |

Ladder (cheapest → strongest): `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/high → opus/xhigh`. Anything stronger (e.g. Fable) only with the user's say-so.

## Get a routing decision

```bash
python3 $S/router.py route --type bugfix:api --tier T1
# {"model": "sonnet", "effort": "medium", "source": "tier-default|learned|probe", ...}
```

Use the result for `claude -p --model … --effort …` workers and for a subagent's `model:`.

## Run a task through the orchestrator

```bash
python3 $S/run_task.py --repo /path/to/repo --type bugfix:api --tier T1 \
  --verify "make test" "Fix the 500 on GET /tasks when due_date is null"
```

- `--verify` (repeatable) is the definition of done. `--baseline` is the pre-task health check. It defaults to `--verify`; set it when `--verify` includes a test that should fail right now.
- Each config gets 2 attempts; the 2nd resumes the same session with the failing output. Then the failure is classified and the task escalates:

| Cause | Meaning | This task | Future tasks |
|---|---|---|---|
| environment | broken services/tooling, flaky or impossible checks | stop (exit 3) | nothing |
| context | missing or stale repo knowledge | retry same config with a hint | proposal: AGENTS.md note |
| effort | skipped files/tests, stopped partway | same model, more effort | evidence for a floor proposal |
| capability | had context, tried, still wrong | next model | evidence for a floor proposal |

- One failure is recorded **per task per config**; retries don't add evidence.
- A floor raise is proposed only after ≥2 task failures at a config for a task type within 30 days **and** a failure rate above 50%.
- Approved floors expire after 30 days, or as soon as the model alias resolves to a new model version.
- About 10% of tasks with a learned floor probe one rung lower. 3 clean probe wins propose lowering the floor.
- Long T2/T3 runs can outlast a tool-call timeout. Run them in a terminal or in the background.
- Run in a git worktree if the checkout has uncommitted work.

## Review proposals (user only)

```bash
python3 $S/router.py proposals
python3 $S/router.py approve <id>
python3 $S/router.py reject <id> --why "flaky test, not the model"
python3 $S/router.py status      # floors, per-config success/cost, state dir
```

If `run_task.py` reports pending proposals, tell the user. **Never approve them yourself.**

State (ledger, approved floors, proposals) is kept in `$ORCHESTRATE_DIR`, else `~/.local/share/orchestrate`, so updates and reinstalls keep what was learned.
