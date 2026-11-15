# Evals

Snapshot evals for the four model judgments orchestrate depends on: the **manager**, the **reviewer**, the **failure classifier** (and, later, the planner). The harness tests in `tests/` use a fake `claude` and prove the plumbing; these use the real one and measure the prompts.

Each case **constructs a state** (fixture repo, plan, job state, event or diff), runs the real prompt builder and parser, and **grades the outcome**: the manager's action batch, the reviewer's verdict and findings, the classifier's cause. The path the model took isn't graded.

```bash
make eval-quick                         # core cases, 1 trial, fails below 90%
make eval                               # every case, 3 trials
python3 evals/run.py --role manager --case open-issue --trials 3
python3 evals/run.py --role manager --config opus/medium --config sonnet/high   # sweep
```

The output has one row per case with its pass rate, its **consistency** (share of trials that agree on the label) and its cost, plus a total per role and config. Runs use a scratch `ORCHESTRATE_DIR`.

## Case format

`cases/<role>/<id>.json`:

| Field | Roles | Meaning |
|---|---|---|
| `core` | all | in the CI set (`--core`) |
| `pair` | all | id of its negative counterpart. Every "should do X" has a "should not" twin. |
| `about` | all | why the case exists, and which incident it came from |
| `files` | manager, classifier | fixture repo |
| `plan`, `state`, `event` | manager | plan, job state per task, and the event to decide on |
| `base_files`, `head_files`, `reports` | reviewer | the diff is base→head |
| `task`, `final_message`, `verify_output`, `diff_files`, `verify` | classifier | a failed attempt |
| `expect` | all | see below |

`expect`:
- **manager**: `any_action` (at least one of these kinds), `no_action` (none of these), `touches` (task ids acted on), `mentions` (strings in the decision), `valid` (default true: the batch must pass `apply_actions`)
- **reviewer**: `verdict`, `finding_mentions` (in some blocker/major finding), `finding_file`
- **classifier**: `cause`

## Adding cases

The best cases are real incidents: every `rejected_actions` in a job's manager log, every human override, every classifier verdict you disagree with. Build the state that led up to it, including messy history (long sessions, contradictory notes), not only the clean start. Write the negative twin at the same time. Aim for 50–100 cases per role over time.

## Baseline results

Recorded on 2026-11-15 with Claude Code 2.1.325. See `docs/IMPROVEMENT-PLAN.md` for the table.
