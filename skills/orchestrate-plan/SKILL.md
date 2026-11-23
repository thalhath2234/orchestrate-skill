---
name: orchestrate-plan
description: "Use when writing an orchestrate plan.json. Splits a coding goal into subtasks with file ownership, tiers, dependencies and done criteria."
---

# Planning an orchestrate job

A good plan makes subagents independent. Each one gets a clear goal, its own files, a definition of done, and the context it needs, and never has to ask anyone anything.

## Process

1. **Start from the confirmed grilling summary** (the **orchestrate-grill** skill). The user's definition of done goes into `definition_of_done` exactly as they said it; its goal, acceptance criteria, non-goals and decisions go into `goal`, `acceptance`, `non_goals` and `decisions`, word for word. If a product decision turns up missing while you plan, go back and ask; don't guess one. A planner subagent can't ask, so it lists each choice it had to make that touches the architecture, a public interface or route, a new dependency, or stored data in `assumptions` (what it chose and the alternative), never in `decisions`. Validate warns until the user has confirmed each one and it has moved into `decisions`.
2. **Map the repo:** `orchestrate.py scan --repo <repo>`. It lists packages and their areas, suggested verify commands, hotspots (lockfiles and root configs), AGENTS.md files and the setup command. Read the root AGENTS.md and the AGENTS.md of every package you'll touch. Skim the code you'll change (entry points, an existing similar feature) so the plan follows existing patterns.
3. **Split by ownership, not by step.** Each task owns a set of paths that nobody running at the same time owns:
   - **Contract first.** Shared types, API schemas or a contract package come first (wave 1). Consumers depend on them.
   - **Then one task per package/area** (api, web, mobile…) in parallel, each owning that package's paths.
   - **Vertical inside a package** if it's big: split by feature folder, not "models", then "handlers", then "tests". Tests go with the code they test.
   - **Integration glue last** (route registration, navigation entries, feature flags), owned by the task that depends on all the others.
4. **Size each task** at roughly 15–45 minutes of agent work and 1–10 files. Merge trivial tasks into a neighbor. Split anything that would be T3. Aim for 2–8 tasks. More than 12 is a smell.
5. **Tier each task** on its own merits (see below). A T3 job is usually made of T1/T2 tasks.
6. **Write the definition of done: set the bar high.** `definition_of_done` (required) is the user's sentence; `acceptance` (required) is the job's checkable proof of it, and together the criteria must cover everything that sentence promises; each task's `done` is its own. Every criterion is checkable by reading code or running a test, and says what is observable: the behaviour, the error and edge paths, the test that proves it, how it connects to the rest of the job. "Works correctly" is not a criterion; "an expired token returns 404 (handler_test.go)" is. Validation needs at least 1/2/3/3 `done` criteria for T0/T1/T2/T3 tasks and warns about vague words. Every task is also held to a fixed bar (tests that fail without the change, verify run by the agent, no TODOs or dead code, errors handled at boundaries, repo conventions, everything wired up), so build on it instead of repeating it; add repo-wide standards of your own in `quality_bar`. Then write `verify` per task, the job-level `verify`, `setup` and gates.

   The definition of done keeps improving during the job. Subagents, the manager and reviewers add criteria from what they find; nothing removes or loosens one, so only the user ever lowers the bar.
7. **Validate:** `orchestrate.py validate plan.json`. Fix every ERROR and read every warning. Drive **ownership gap** warnings to zero: each names an unowned file in the same Go package as, or imported by, a file a task owns. Give it to the task that will need it. In a real 18-task job, gaps like these produced an open issue from 8 of 9 finished tasks and most of the manager's cost.

## Tier rubric (per subtask)

| Tier | Subtask looks like | Done criteria needed |
|---|---|---|
| T0 | Mechanical: rename, copy change, config value, add an export | ≥ 1 |
| T1 | Follows an existing pattern closely: new endpoint like the others, new screen from a template, add tests | ≥ 2 |
| T2 | Some design or several modules: new data flow, state management, non-trivial logic, migration plus model | ≥ 3 |
| T3 | Hard reasoning: concurrency, auth/security model, big refactor (try to split it instead) | ≥ 3 |

Every task, whatever its tier, starts on the worker model: Opus 5.5 at medium. Failures re-run at `high`; capability failures go to `xhigh`, then Fable 5.2, the top rung. Don't raise a tier to buy a stronger model: the ladder does that on evidence. Before the plan runs, the advisor (Fable 5.2) checks it against the definition of done and can stop the job, so a plan that misses part of done comes back to you.

Don't write a task's brief or a bug's reproduction into the plan: when the job runs, the briefer (Fable) reads the code and writes each subagent's brief (how to do it, how to test it, what done looks like), and for a `bugfix:` task the scenario and symptom the subagent reproduces before and after its fix. Put what it must know in the task's `goal`, `interfaces` and `done`.

Type is `<category>:<area>`. Categories: `rename docs config bugfix feature refactor test migration review research`. The area is the package's area from `scan` (for example `api`, `web`, `mobile`, `contract`), or `cross`. Keep areas consistent across jobs, because routing learns per type.

## Monorepo rules

- **Hotspots** (lockfiles, root `package.json`, `go.work`, root Makefile/tsconfig/CI): at most **one** task owns each. If several tasks need new dependencies, create a small first task (`config:infra`, T0/T1) that adds them all and owns the lockfile, and make the others depend on it. Otherwise, forbid new dependencies.
- **Migrations:** one task owns the migrations folder pattern for the job, with a `gate` (validation rejects a task that owns migrations without one). Use the repo's migration tool and naming from its docs.
- **Generated code** (codegen, OpenAPI clients) is owned by the task that runs the generator.
- **Verify per package:** use the package's own commands from `scan.packages[].verify_suggestions` (for example `make test-api`, `pnpm --filter web run typecheck`). Use the repo-wide suite only in job-level `verify`.
- **Setup:** set `setup` from `scan.suggested_setup` (for example `pnpm install --frozen-lockfile --prefer-offline`). It runs in every worktree.
- **Resource weight:** if verify runs heavy builds (tsc on a large app, Gradle, Go race tests), set `"mem_per_agent_gb": 2.5` (or more) so the parallel cap stays safe.
- **Ports:** keep dev-server commands out of verify, because parallel worktrees would fight over the same ports. If an e2e check needs a server, give it its own port offset with `$ORCHESTRATE_SLOT`. The plan's `ui.dev` is the exception: the harness runs it alone, in the integration worktree, one dev server per state dir (`$ORCHESTRATE_DIR`) at a time.

## Ownership patterns

- `dir/` or `dir` owns everything under it.
- `dir/**/*.ts` matches `.ts` files at any depth.
- `dir/*.go` matches only direct children.
- An exact path owns one file.

Be as narrow as practical but include everything the task must create, including its tests and any docs it should update. `reads` lists context to read, which can't be edited. A `reads` path must exist now or be owned by a task this one depends on; validate warns otherwise.

## UI pages (web apps)

When `scan` returns a `ui_suggestion` and the job changes the UI, add a `ui` block so the review screenshots the running app and a UI reviewer checks the shots. Start from the suggestion (`dev`, `url`, `paths`) and check it against the repo: `dev` must serve the app from the repo root without prompts, and `url` must be what it serves. Validation rejects a task `ui` list when the plan has no `ui` block.

- **Each task that changes the UI lists the pages that show its change** in its own `ui` field. Pages declared by several tasks are captured once, and the reviewer sees which tasks declared each. Job-level `ui.pages` are for pages no single task owns (a home page that every change must not break). With no pages anywhere, only `/` is captured.
- **Pick pages that prove done.** Use the flows settled in the grill: the page where the new thing appears, plus the states acceptance names (empty, error, logged out). A page needs a URL that works on a fresh dev server: seed data the app ships with, or a fixture route. If a page needs login or data the dev server doesn't have, say so in `decisions` or add a task that provides it.
- **Steps exercise the flow,** not just load the page: click Share, then `wait_for` the toast, then `screenshot` it. A selector that never becomes visible (no box, `display:none`, `visibility:hidden` or opacity 0) is a failed step, and so is a click on an element that is covered or off-screen: the harness reports it as a major finding unless the UI reviewer lists it as expected, with a reason. A failed step stops that page's flow: the remaining steps (and their `screenshot`s) are skipped, and only the page's final shot is taken. An `alert`, `confirm` or `prompt` the flow opens is accepted, so a confirm-before-delete step works. Prefer stable selectors (`[data-testid=...]`, roles, ids) and put them in the task's `done` so the worker adds them. The page's main shot shows the state after its last step; to keep the page as it first loads, start the steps with `{"screenshot": "before"}`.
- **`paths`** skips the UI review when the job's diff touches no UI file and no finished task declared `ui` pages. Leave it out to always run. Validation warns when `paths` overlaps no task's `owns` (a typo, or a UI change made through a shared package outside `paths`).
- **`url`'s port must be free:** before any work, the job starts `dev` once and stops as `environment` if something already listens there, the app doesn't come up, or Chrome can't take a screenshot.
- **Viewports:** keep both unless the app is not meant for one.

## Done criteria and verify

- **`done`:** observable outcomes, not activities. Write "GET /share/:token returns 404 for expired tokens (test added)", not "implement endpoint".
- **`verify`:** commands that exit 0 when the task is right, run from the repo root. They must already be runnable before the change (the job baseline runs them). New tests can live inside an existing test target.
- **Job `verify`:** repo-level lint, typecheck and test targets that cover every touched package. It runs before the job (baseline) and after merging. If it can't pass before the change, set `"baseline"` to commands that do.

## Schema

```json
{
  "job": "share-links",                       // slug; becomes branch orchestrate/share-links
  "goal": "Users can share a schedule via a public read-only link",
  "repo": "/abs/path/to/repo",
  "base": "main",                             // optional, default: current branch
  "definition_of_done": "I can open a shared link in a private window and see the schedule, read-only",
                                              // required: the user's own words, verbatim (grill Q1)
  "acceptance": ["...", "..."],               // required: checkable criteria that prove definition_of_done
  "non_goals": ["no expiry UI in this job"],  // optional, from grilling: subagents must not build these
  "decisions": ["Tokens are opaque random strings, not JWTs (docs/adr/0007-share-tokens.md)"],
                                              // optional, from grilling: settled, not to be revisited
  "assumptions": ["Links live in store.KV, not a new table"],  // planner subagent only: choices nobody
                                              //   confirmed; validate warns until each is in decisions
  "quality_bar": ["Every API error response uses the shared ErrorBody type"],  // optional: repo-wide
                                              //   standards added to the fixed bar for every task
  "setup": ["pnpm install --frozen-lockfile --prefer-offline"],   // per worktree, and again on the merged tree:
                                              //   make it repeatable; git-ignore what it creates
  "verify": ["make lint", "make typecheck", "make test-api"],     // final; also the baseline
  "baseline": ["make lint"],                  // optional override of the pre-check
  "max_parallel": 4,                          // optional cap (machine cap still applies)
  "mem_per_agent_gb": 2.5,                    // optional, heavier verify -> bigger
  "budget_usd": 15,                           // optional cap; tasks started together split what is left
  // "commit_hook": true,                     // optional: the main checkout's pre-commit hook as a check on
                                              //   every task (run in a scratch git dir). Offer it when the repo
                                              //   has one; a hook that writes outside the worktree fails there
  "review_rounds": 2,                         // review -> fix -> review (default 2)
  "manager": {"enabled": true, "max_interventions": 12, "max_task_retries": 2},  // optional; defaults
  "models": {"reviewer": "opus/high"},        // optional per-role override, "<model>/<effort>". Roles and
                                              //   defaults: advisor fable/medium (then high if it finds
                                              //   nothing), planner, briefer and manager fable/medium,
                                              //   worker, reviewer, area_reviewer, ui_reviewer, cleanup and
                                              //   integrator opus/medium,
                                              //   classifier haiku/low, lessons haiku/medium
                                              //   (scripts/models.py has the sources)
  "max_model": "opus",                        // optional: nothing above this model, roles or ladder
                                              //   (no Fable: the advisor becomes opus/high)
  "advisor": "fable",                         // optional, off by default: workers get the advisor tool
                                              //   (claude --advisor) and are told to consult it before
                                              //   choosing an approach, on a repeated error and before
                                              //   reporting. Worth it for long tasks hard in spots, not
                                              //   short ones. Capped by max_model; must rank >= the
                                              //   worker (Opus workers: fable or opus). If the launch is
                                              //   refused (e.g. Fable usage-credits consent) the task
                                              //   runs on without it. Not the plan/final advisor checks,
                                              //   which always run (models.advisor)
                                              //   Headless workers only: Orca workers follow
                                              //   the user's own advisorModel setting.
  "max_escalations": 2,                       // optional; model escalations per task before the manager steps in
  "area": "cross",                            // area used for review-fix routing
  "extra_tools": ["Bash(docker compose *)"],  // optional extra allowed tools for subagents
  "auto_claim": true,                         // optional (default true): a subagent may edit a file no
                                              //   task owns; the scope check assigns it to that task
  "review_diff_chars": 120000,                // optional: above this, review per area + integration
  "lessons": {"enabled": true},               // optional: after the job,
                                              //   propose AGENTS.md notes from what agents learned
  "exclude_dynamic_prompt": true,             // optional (default true): per-worktree details move out of
                                              //   the system prompt so subagents share its cache
  // "sandbox": true,                        // optional; leave it out. Workers are confined with
                                              //   bubblewrap wherever it works. true: refuse to run without it,
                                              //   and add Claude Code's sandbox (no localhost services); Orca
                                              //   can't confine workers, so inside Orca they run headless. false
                                              //   turns confinement off: the user's call, never set it yourself
  "ui": {                                     // optional: screenshot the running app (before the code
                                              //   review) and have the UI reviewer check the shots after it
    "dev": "pnpm --filter web dev",           // required: serves the app; run in the integration worktree
    "url": "http://127.0.0.1:5173",           // required: http(s) base URL that command serves; must be
                                              //   free before it starts (else the review fails closed)
    "ready_timeout_s": 180,                   // optional: how long to wait for url to answer
    "paths": ["apps/web/**"],                 // optional: run only if the diff touches these (owns-style
                                              //   patterns); default: every round
    "viewports": ["desktop", "mobile"],       // optional subset; default both. desktop 1440x900,
                                              //   mobile 390x844 (DPR 2, touch, mobile user agent)
    "pages": [{"path": "/", "name": "home"}]  // optional job-level pages; with no pages anywhere, "/"
  },
  "tasks": [
    {
      "id": "contract-share-types",
      "title": "Share link types in the contract package",
      "type": "feature:contract",
      "tier": "T1",
      "goal": "Add ShareLink and ShareLinkCreate types/zod schemas used by api, web and mobile.",
      "owns": ["packages/contract/src/share/**", "packages/contract/src/index.ts"],
      "reads": ["packages/contract/src/tasks/"],
      "depends_on": [],
      "interfaces": "Export ShareLink {token, scheduleId, expiresAt} and shareLinkCreateSchema.",
      "done": ["Types and schemas exported from the package index", "Unit test for schema validation"],
      "verify": ["pnpm --filter @app/contract run test"]
    },
    {
      "id": "web-share",
      "title": "Share button on the schedule page",
      "type": "feature:web",
      "tier": "T1",
      "goal": "A Share button on the schedule page creates a link and copies it, with a toast.",
      "owns": ["apps/web/src/share/**", "apps/web/src/schedule/SchedulePage.tsx"],
      "depends_on": ["contract-share-types"],
      "done": ["Clicking [data-testid=share] copies the link and shows a 'Link copied' toast (test)",
               "The button fits on a 390px-wide screen without wrapping (UI review)"],
      "verify": ["pnpm --filter web run test"],
      "ui": [                                 // optional: pages that show this task's change; needs plan "ui"
        {"path": "/schedules/demo",           // starts with "/", relative to ui.url
         "name": "schedule-share",            // optional slug for the screenshot files (default: from path);
                                              //   a-z0-9 words, single dashes, max 41 chars (no "--")
         "steps": [                           // optional, run in order before/between screenshots:
           {"click": "[data-testid=share]"},  //   click a CSS selector; fails if covered or off-screen
           {"wait_for": "[role=status]"},     //   wait up to 10 s for it to be visible
           {"screenshot": "after-share"}      //   extra named screenshot mid-flow
         ]}                                   //   also {"type": ["css", "text"]}, {"press": "Enter"},
      ]                                       //   {"wait_ms": 500}
    },
    {
      "id": "api-share",
      "title": "Share token model, migration and endpoints",
      "type": "feature:api",
      "tier": "T2",
      "goal": "POST /schedules/:id/share creates a token; GET /share/:token returns the read-only schedule.",
      "owns": ["apps/api/internal/share/**", "apps/api/migrations/*share*", "apps/api/internal/http/routes.go"],
      "reads": ["apps/api/MIGRATIONS.md", "apps/api/internal/schedules/"],
      "depends_on": ["contract-share-types"],
      "gate": "Creates a DB migration",
      "done": ["Migration up then down leaves the schema unchanged (test)", "Expired/unknown token -> 404 (tests)", "Only the owner can create links (test)"],
      "verify": ["make test-api"]
    }
  ]
}
```

Every subagent's brief includes `non_goals`, `decisions` and the repo glossary (`GLOSSARY.md` or `CONTEXT.md`), and so does the reviewer's prompt. Write decisions so a subagent can act on them alone. When the grill weighed an alternative, write the decision as 'X, not Y, because Z', so a retry or a later subagent doesn't reach for Y again.

Each subagent reports `open_issues` as `{issue, kind: needs_file | needs_decision | fyi, blocks_acceptance, files}`. `fyi` issues and `needs_file` issues whose files belong to one not-started task are handled by rule; only the rest wake the manager.

Comments above are for explanation only. Real `plan.json` must be plain JSON. Required per task: `id, title, goal, type, tier, owns, done, verify`. Optional: `reads, depends_on, interfaces, gate, baseline, ui`.

## Anti-patterns that validation or reality will punish

- Two parallel tasks touching the same file (`routes.go`, `index.ts`, lockfile). Give it to one task, or serialize with `depends_on`.
- Horizontal splits (one task writes models, another handlers, another tests) that need each other's half-finished work.
- `done` that can't be checked, or `verify` that doesn't exercise the change.
- One giant T3 task. That's just an unplanned job.
- Planning features the user didn't ask for.
