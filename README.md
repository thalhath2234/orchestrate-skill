# orchestrate

Pick the right Claude model and effort for each coding task. Tasks start at the cheapest level that usually works, escalate when they fail, and the skill learns per task type which starting point to use. **It never changes its own policy.** Failures turn into proposals that you approve or reject.

- **Router:** `route` returns the starting `(model, effort)` for a task type (`bugfix:api`) and tier (T0–T3).
- **Orchestrator:** `run_task.py` runs `claude -p` workers against your verify command. Each config gets 2 attempts. A failure is classified as *environment*, *context*, *effort* or *capability*, and then the task escalates the right way.
- **Learning with guardrails:**
  - Floor raises need repeated evidence.
  - Floors expire when the model version changes.
  - About 10% of tasks probe one level cheaper, so floors can come back down.
  - Every change is a proposal for you to review.

## Install

**Claude Code plugin** (Claude Code v2.1.292+):

```bash
claude plugin install orchestrate --marketplace thalhath2234/orchestrate-skill
```

**Any agent that supports Agent Skills** (Claude Code, Codex, Cursor, …):

```bash
npx skills add thalhath2234/orchestrate-skill -g
```

Requirements: Python 3.9+ (stdlib only), macOS or Linux, and the `claude` CLI on `PATH` for the orchestrator.

## Use

Start a session and type `/orchestrate` (as a plugin: `/orchestrate:orchestrate`), or just ask "which model should I use for this?" or "run this through the orchestrator".

To run the scripts directly from a clone:

```bash
S=skills/orchestrate/scripts
python3 $S/router.py route --type bugfix:api --tier T1
python3 $S/run_task.py --repo ~/code/app --type bugfix:api --tier T1 \
  --verify "make test" "Fix the 500 on GET /tasks when due_date is null"
python3 $S/router.py proposals         # what it wants to change
python3 $S/router.py approve <id>      # or: reject <id> --why "..."
python3 $S/router.py status
```

State lives in `$ORCHESTRATE_DIR`, else `~/.local/share/orchestrate`, so updates keep what was learned.

## How escalation works

| Cause | This task | Future tasks |
|---|---|---|
| environment (services down, flaky/impossible check) | stop | nothing, no model is blamed |
| context (missing or stale repo knowledge) | retry same model with a hint | proposes an AGENTS.md note |
| effort (skipped tests, stopped partway) | same model, more effort | evidence for a floor raise |
| capability (tried properly, still wrong) | next model | evidence for a floor raise |

The ladder is `haiku/low → haiku/medium → sonnet/medium → sonnet/high → opus/high → opus/xhigh`. A floor raise is proposed after at least 2 failed tasks at the same config for that task type within 30 days, with a failure rate above 50%.

## Develop

```bash
python3 tests/test_routing.py -v     # end-to-end with a fake `claude`
claude plugin validate .
```

MIT licensed.
