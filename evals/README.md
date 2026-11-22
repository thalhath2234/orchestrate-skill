# Evals

Snapshot evals for the model judgments orchestrate depends on: the **manager**, the **reviewer**, the **failure classifier**, the **advisor** (plan and final checks), the **planner**, the **worker** prompt (one real coding session on a tiny repo), the **UI reviewer** (fixture screenshots), and the **parent** (the orchestrate and grill skills themselves). The harness tests in `tests/` use a fake `claude` and prove the plumbing; these use the real one and measure the prompts.

Each case **constructs a state** (fixture repo, plan, job state, event or diff), runs the real prompt builder and parser, and **grades the outcome**: the manager's action batch, the reviewer's verdict and findings, the classifier's cause. The path the model took isn't graded.

The **parent** role is the exception, because its rules live in `skills/orchestrate/SKILL.md` and `skills/orchestrate-grill/SKILL.md`, not in a prompt builder. A case copies the plugin under a unique name, swaps `orchestrate.py` and `run_task.py` for a stub that logs each call and replays the case's canned output (so no job runs and no subagent costs money), and pins the skill's `S=` to that copy. Then it runs one or two `claude -p --plugin-dir <copy> --setting-sources project,local --output-format stream-json` turns in a fixture repo. It grades the tool calls first (Bash commands, written files, the stub's call log) and the reply second, with narrow checks only.

```bash
make eval-quick                         # core cases, 1 trial, fails below 90% or when a role is below 50%
make eval                               # every case, 3 trials
python3 evals/run.py --role manager --case open-issue --trials 3
python3 evals/run.py --role manager --config opus/medium --config sonnet/high   # sweep
```

The output opens with a `run:` line (git commit, dirty tree, `claude --version`, date, hermetic), then has one row per case with its pass rate, its **consistency** (share of trials that agree on the label) and its cost, plus a total per role and config. Runs use a scratch `ORCHESTRATE_DIR`.

`--min-pass` gates on the mean over every valid row, and also on each role: a role whose mean is below `--min-role-pass` (default 0.5) fails the run, so a whole role can't fail behind a good overall mean.

Runs are **hermetic** by default: `run.py` sets `ORCHESTRATE_HERMETIC=1`, so every model call skips your MCP servers and hooks (`--strict-mcp-config`, an empty `--mcp-config`, `disableAllHooks`). CLAUDE.md and other setting sources still load, but never your `advisorModel`: calls made through `claude_cli` turn the advisor tool off unless the case asks for it. `--no-hermetic` measures under your own setup instead (judgment roles still get no MCP servers, as in jobs); `--json` records which in `meta.hermetic`. The parent role is separate: it always runs with `--setting-sources project,local`.

A trial answered by a fallback model (Fable was asked for, but the account has no Fable, so Opus answered) is **INVALID**: it is not counted in the pass rate or consistency, the row says so, and a role and config where every trial fell back prints a warning and fails `--min-pass`. Otherwise a Fable sweep on such an account would compare Opus with Opus under Fable's name. `--json` also writes `meta`: the git commit, whether the tree was dirty, `claude --version` and the UTC date. `dirty` ignores eval outputs (the `--json` file, `evals/runs/`, `evals/last-run.json`), so saving a run doesn't mark the next one dirty.

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
| `stage` (`plan` or `final`), plus the reviewer or manager fixture fields | advisor | what the advisor is asked to judge |
| `goal`, `done`, `files`, optional `acceptance`, `decisions`, `non_goals` | planner | the request, the user's definition of done, and what the grill settled (passed as `plan --acceptance/--decision/--non-goal`) |
| `files`, `plan` (one task) | worker | the repo and the task; the brief is built by the real `build_brief` |
| `base_files`, `head_files`, `plan` (with `ui`), `shots` | ui_reviewer | the diff, and the screenshots the review step sees: each shot has `png` (a file in `cases/ui_reviewer/fixtures/`), `name`, `path`, `viewport` and any browser evidence (`status`, `console`, `page_errors`, `step_errors`, ...) |
| `files`, `stub`, `turns`, `model`, `budget_usd` | parent | the fixture repo; `stub` maps a script subcommand (`wait`, `scan`, `advise`, ..., or `run_task`) to `{"exit", "stdout", "stderr"}`, and anything unlisted exits 0 with `{}`; `turns` holds one or two `{"user", "expect"}` (the first is sent as `/<plugin>:orchestrate <user>`, the second resumes the session); `model` defaults to `opus/medium`, `budget_usd` (per turn) to 3 |
| `from`, `plan_patch` | advisor, planner, reviewer, ui_reviewer | reuse another case's fixture (`"from": "reviewer/route-not-registered"`); `plan_patch` replaces top-level plan keys, and `"tasks": {id: {...}}` edits tasks |
| `expect` | all | see below |

`expect`:
- **manager**: `any_action` (at least one of these kinds), `no_action` (none of these), `touches` (task ids acted on, matched exactly: an `add_task` whose new task owns `apps/api/...` doesn't touch task `api`), `mentions` (strings in the actions, such as a retry's instructions; the free-text `note` doesn't count), `valid` (default true: the batch must pass `apply_actions`)
- **reviewer**: `verdict`, `finding_mentions` (in some blocker/major finding), `no_finding_mentions` (in none of them), `finding_file`
- **classifier**: `cause`
- **advisor**: `verdict` (`revise` when anything blocks, else `proceed`), `finding_mentions` (in a blocking finding or a raised criterion)
- **planner**: `owned` (paths some task must own), `criteria_mention`, `min_acceptance`, `min_tasks`, `max_tasks`, `assumption_matches` / `no_assumption_matches` (regexes, case-insensitive, over the plan's `assumptions`), `decisions` (the exact list the plan's `decisions` must hold); the plan must validate and keep `definition_of_done` verbatim
- **worker**: `ok` (default true: verify and the done check pass), `file_contains`, `file_lacks`, `unchanged`, `issue_mentions` (in the report's open issues)
- **ui_reviewer**: `verdict` (after the harness's own checks), `finding_mentions` / `no_finding_mentions` (in a blocker/major UI finding), `finding_shot` (a blocker/major UI finding names this shot, such as `share--mobile`), `finding_severity` (that finding's severity), `expected` (`[shot, kind]` pairs the UI reviewer must list under `expected` with a reason, such as a 404 page on purpose) and `no_expected` (shots it must not list there)
- **parent**, per turn: `bash_includes` / `bash_lacks` (regexes over the Bash commands it tried, including one the approval hook then stopped), `writes_lack` (regexes over the paths it wrote or edited), `calls_include` / `calls_lack` (regexes over the stub's log, such as `orchestrate wait share-links` or `run_task --repo ...`), `calls_before` (`[first, then]` regex pairs: a call matching `then` needs an earlier call matching `first`), `reply_includes` (literal text, case-sensitive: for "word for word"), `reply_matches` / `reply_lacks` (regexes over the final reply, case-insensitive; anchor with `\A` for "the reply opens with"), `said_includes` / `said_matches` / `said_lacks` (the same, over everything the turn wrote, not only the final reply). Every turn needs at least one check that only passes when the turn did something (`calls_include`, `bash_includes`, `calls_before`, `reply_includes`, `reply_matches`, `said_includes` or `said_matches`): a turn graded only on what it must not do would pass a session where the skill never loaded. A session that ends in an error (`is_error`, or a non-zero exit) fails the turn.

## Fixtures

The UI reviewer's screenshots were taken with real Chrome from the pages in `cases/ui_reviewer/fixtures/site/`, the way a job takes them:

```bash
python3 skills/orchestrate/scripts/ui_review.py capture --url http://127.0.0.1:18741 \
  --dev "python3 -m http.server 18741 --bind 127.0.0.1" --cwd evals/cases/ui_reviewer/fixtures/site \
  --out /tmp/shots --page /share.html --page /share-overflow.html --page /share-error.html \
  --page /share-gone.html --page /share/missing
```

`/share/missing` has no file, so it is `http.server`'s own 404 page (`share-not-found--desktop.png`). The cases set each shot's `status` themselves.

Then copy the shots you need into `fixtures/` and name them in the case's `shots`.

## Adding cases

The best cases are real incidents: every `rejected_actions` in a job's manager log, every human override, every classifier verdict you disagree with. Build the state that led up to it, including messy history (long sessions, contradictory notes), not only the clean start. Write the negative twin at the same time. Aim for 50–100 cases per role over time.

`tests/test_evals.py` checks that every prompt the scripts build (a string that opens with an `ORCHESTRATE_*` or `FAILURE_CLASSIFIER` tag) maps to a role with cases here, or is listed there as exempt with a reason. The integrator (`ORCHESTRATE_MERGE`), cleanup, lessons and the per-area review have no cases yet. A new prompt fails that test until it gets cases or an exemption.

## Baseline results

Recorded on 2026-11-15 with Claude Code 2.1.325. See `docs/IMPROVEMENT-PLAN.md` for the table. The P9 runs (baseline, after, sweeps) are kept in `evals/runs/p9-*` with their full JSON.

No real-model run has been recorded yet for the UI reviewer and parent roles, the advisor, planner and reviewer cases added on 2026-11-21, or the parent dashboard pair. Run `make eval-quick` and `make eval-parent` (both save their JSON under `evals/runs/`) and record the baseline here.

On 2026-11-21 the manager grader was tightened (`touches` matches task ids exactly, `mentions` searches the actions and not the note). The manager numbers above were graded the looser way and may be inflated. Re-run the cases that use them (`--role manager --case scope-failure --case open-issue-needs-pending-owner --trials 3`) and record the new baseline here.

`reviewer/report-claims-met-falsely` checks whether a confident, structured `done_check` claim in a worker's report (with evidence that doesn't exist) talks the reviewer into a pass; `decision-respected-passes` carries truthful claims as its control. If it fails across trials, first tighten the reviewer's instruction to open every cited file:line before accepting a claim, and measure again. Removing `done_check` from the reviewer's context is a separate decision, made with a cost comparison.
