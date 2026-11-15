---
name: orchestrate-plan
description: "Use when writing an orchestrate plan.json. Splits a coding goal into subtasks with file ownership, tiers, dependencies and done criteria."
---

# Planning an orchestrate job

A good plan makes subagents independent. Each one gets a clear goal, its own files, a definition of done, and the context it needs, and never has to ask anyone anything.

## Process

1. **Start from the confirmed grilling summary** (the **orchestrate-grill** skill). Its goal, acceptance criteria, non-goals and decisions go into `goal`, `acceptance`, `non_goals` and `decisions`, word for word. If a product decision turns up missing while you plan, go back and ask; don't guess one.
2. **Map the repo:** `orchestrate.py scan --repo <repo>`. It lists packages and their areas, suggested verify commands, hotspots (lockfiles and root configs), AGENTS.md files and the setup command. Read the root AGENTS.md and the AGENTS.md of every package you'll touch. Skim the code you'll change (entry points, an existing similar feature) so the plan follows existing patterns.
3. **Split by ownership, not by step.** Each task owns a set of paths that nobody running at the same time owns:
   - **Contract first.** Shared types, API schemas or a contract package come first (wave 1). Consumers depend on them.
   - **Then one task per package/area** (api, web, mobile…) in parallel, each owning that package's paths.
   - **Vertical inside a package** if it's big: split by feature folder, not "models", then "handlers", then "tests". Tests go with the code they test.
   - **Integration glue last** (route registration, navigation entries, feature flags), owned by the task that depends on all the others.
4. **Size each task** at roughly 15–45 minutes of agent work and 1–10 files. Merge trivial tasks into a neighbor. Split anything that would be T3. Aim for 2–8 tasks. More than 12 is a smell.
5. **Tier each task** on its own merits (see below). A T3 job is usually made of T1/T2 tasks.
6. **Write `done` and `verify`** for each task. Then the job-level `verify`, `setup` and gates.
7. **Validate:** `orchestrate.py validate plan.json`. Fix every ERROR and read every warning.

## Tier rubric (per subtask)

| Tier | Subtask looks like | Starts at |
|---|---|---|
| T0 | Mechanical: rename, copy change, config value, add an export | haiku/low |
| T1 | Follows an existing pattern closely: new endpoint like the others, new screen from a template, add tests | sonnet/medium |
| T2 | Some design or several modules: new data flow, state management, non-trivial logic, migration plus model | sonnet/high |
| T3 | Hard reasoning: concurrency, auth/security model, big refactor | opus/high (try to split it instead) |

Type is `<category>:<area>`. Categories: `rename docs config bugfix feature refactor test migration review research`. The area is the package's area from `scan` (for example `api`, `web`, `mobile`, `contract`), or `cross`. Keep areas consistent across jobs, because routing learns per type.

## Monorepo rules

- **Hotspots** (lockfiles, root `package.json`, `go.work`, root Makefile/tsconfig/CI): at most **one** task owns each. If several tasks need new dependencies, create a small first task (`config:infra`, T0/T1) that adds them all and owns the lockfile, and make the others depend on it. Otherwise, forbid new dependencies.
- **Migrations:** one task owns the migrations folder pattern for the job. Add a `gate`, because migrations need human approval. Use the repo's migration tool and naming from its docs.
- **Generated code** (codegen, OpenAPI clients) is owned by the task that runs the generator.
- **Verify per package:** use the package's own commands from `scan.packages[].verify_suggestions` (for example `make test-api`, `pnpm --filter web run typecheck`). Use the repo-wide suite only in job-level `verify`.
- **Setup:** set `setup` from `scan.suggested_setup` (for example `pnpm install --frozen-lockfile --prefer-offline`). It runs in every worktree.
- **Resource weight:** if verify runs heavy builds (tsc on a large app, Gradle, Go race tests), set `"mem_per_agent_gb": 2.5` (or more) so the parallel cap stays safe.
- **Ports:** never put dev-server commands in verify. If an e2e check needs a server, give it its own port offset with `$ORCHESTRATE_SLOT`.

## Ownership patterns

- `dir/` or `dir` owns everything under it.
- `dir/**/*.ts` matches `.ts` files at any depth.
- `dir/*.go` matches only direct children.
- An exact path owns one file.

Be as narrow as practical but include everything the task must create, including its tests and any docs it should update. `reads` lists context to read, which can't be edited.

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
  "acceptance": ["...", "..."],               // whole-job, testable (from grilling)
  "non_goals": ["no expiry UI in this job"],  // optional, from grilling: subagents must not build these
  "decisions": ["Tokens are opaque random strings, not JWTs (docs/adr/0007-share-tokens.md)"],
                                              // optional, from grilling: settled, not to be revisited
  "setup": ["pnpm install --frozen-lockfile --prefer-offline"],   // per worktree
  "verify": ["make lint", "make typecheck", "make test-api"],     // final; also the baseline
  "baseline": ["make lint"],                  // optional override of the pre-check
  "max_parallel": 4,                          // optional cap (machine cap still applies)
  "mem_per_agent_gb": 2.5,                    // optional, heavier verify -> bigger
  "budget_usd": 15,                           // optional, stop launching tasks past this
  "review_rounds": 2,                         // review -> fix -> review (default 2)
  "manager": {"enabled": true, "model": "opus", "effort": "high",
              "max_interventions": 12, "max_task_retries": 2},   // optional; these are the defaults
  "max_escalations": 2,                       // optional; model escalations per task before the manager steps in
  "area": "cross",                            // area used for review-fix routing
  "extra_tools": ["Bash(docker compose *)"],  // optional extra allowed tools for subagents
  "sandbox": false,                           // optional OS sandbox for subagents' shell (bwrap/
                                              //   seatbelt); safest, but tools that write caches
                                              //   outside the repo (Go, Gradle) may break
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
      "id": "api-share",
      "title": "Share token model, migration and endpoints",
      "type": "feature:api",
      "tier": "T2",
      "goal": "POST /schedules/:id/share creates a token; GET /share/:token returns the read-only schedule.",
      "owns": ["apps/api/internal/share/**", "apps/api/migrations/*share*", "apps/api/internal/http/routes.go"],
      "reads": ["apps/api/MIGRATIONS.md", "apps/api/internal/schedules/"],
      "depends_on": ["contract-share-types"],
      "gate": "Creates a DB migration",
      "done": ["Migration up/down works", "Expired/unknown token -> 404 (tests)", "Only the owner can create links (test)"],
      "verify": ["make test-api"]
    }
  ]
}
```

Every subagent's brief includes `non_goals`, `decisions` and the repo glossary (`GLOSSARY.md` or `CONTEXT.md`), and so does the reviewer's prompt. Write decisions so a subagent can act on them alone.

Comments above are for explanation only. Real `plan.json` must be plain JSON. Required per task: `id, title, goal, type, tier, owns, done, verify`. Optional: `reads, depends_on, interfaces, gate, baseline`.

## Anti-patterns that validation or reality will punish

- Two parallel tasks touching the same file (`routes.go`, `index.ts`, lockfile). Give it to one task, or serialize with `depends_on`.
- Horizontal splits (one task writes models, another handlers, another tests) that need each other's half-finished work.
- `done` that can't be checked, or `verify` that doesn't exercise the change.
- One giant T3 task. That's just an unplanned job.
- Planning features the user didn't ask for.
