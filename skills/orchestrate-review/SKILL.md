---
name: orchestrate-review
description: Use when acting as the integrator or reviewer of an orchestrate job: resolving merge conflicts between subagent branches and reviewing the merged result.
---

# Integrating and reviewing an orchestrate job

Several subagents built parts of one goal in separate worktrees. Their branches are merged into `orchestrate/<job>`. You work from a fresh context: you have the plan, the subagents' reports and the diff, not their reasoning. Judge the result, not the effort.

## Merging (integrator)

You were given conflicted files and each side's task report.

- **Keep both intents.** A conflict between two tasks almost always means both changes are needed, for example two routes registered or two exports added. Combine them. Don't pick one side.
- Use the **interfaces** in the reports as the source of truth for names and signatures.
- Edit **only** the conflicted files, and remove every conflict marker. `git add` each resolved file. Don't commit.
- **Lockfiles:** don't hand-merge them. Take the side that includes the dependency-adding task's changes, and say in your notes that the lockfile needs regenerating.
- If you can't resolve a conflict without making a product decision, leave it, return `"resolved": false` and explain why.

## Reviewing (reviewer)

Read the actual diff (`git diff <base>..HEAD`, `git show`, open files). Check, in this order:

1. **Acceptance:** every job acceptance criterion is met, with evidence in the code or tests. Missing criteria are blockers.
2. **Integration across tasks:** this is what no single subagent could see.
   - Producers and consumers agree: types, field names, endpoints and error shapes match between packages.
   - Nothing is half-wired: routes registered, exports added, navigation entries present, flags on.
   - There is no duplicated logic where a task should have reused another's work.
3. **Each subtask's `done`** criteria hold.
4. **Correctness:** edge cases (empty, null, expired, unauthorized), error handling, concurrency, off-by-one, timezones.
5. **Security:** authorization on new endpoints and actions, input validation, injection, secrets or tokens in code or logs, unsafe defaults.
6. **Data:** migrations are reversible and match the models, there are no destructive changes without a gate, and indexes exist for new lookups.
7. **Tests:** they exercise the real behavior (not tautologies or mocks of the thing under test), and the core paths are covered.
8. **Scope and hygiene:** no unrelated rewrites, no debug leftovers, no commented-out code, and docs or AGENTS.md updated if behavior or commands changed.

Don't request style rewrites, refactors outside the job, or features nobody asked for.

### Severity

- **blocker:** an acceptance criterion isn't met, something is broken or not wired, a security hole, data loss risk, or a failing check.
- **major:** a likely bug on a real path, a producer/consumer mismatch, a missing test for core behavior, or an unsafe migration.
- **minor:** naming, small cleanups, or a nice-to-have test.

Use `changes_requested` **only** if there is at least one blocker or major finding. Give each finding a file and, where possible, a line, plus a concrete fix. A fix subagent acts on your words alone.

### Reviewer output

Reply with only this JSON object:

```json
{
  "verdict": "pass",
  "summary": "one or two sentences on overall state",
  "findings": [
    {"severity": "major", "file": "apps/web/src/share/ShareButton.tsx", "line": 42,
     "issue": "uses `expires_at` but the API returns `expiresAt`",
     "suggestion": "read `expiresAt` from the ShareLink type in packages/contract"}
  ]
}
```

Set `verdict` to `"pass"` or `"changes_requested"`.
