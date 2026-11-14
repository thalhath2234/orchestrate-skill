---
name: orchestrate-grill
description: "Use at the start of every orchestrate run, before planning. Grills the user about the goal until shared understanding, sharpening domain terms and writing the glossary and ADRs as decisions land."
---

# Grill before you plan

> **Credit.** This is Matt Pocock's `grill-with-docs`, which is his `grilling` skill plus his `domain-modeling` skill, adapted for orchestrate.
> - Source: https://github.com/mattpocock/skills at commit `5863806dd4e15c63174bee4e7515a4256792ecfa`
> - License: MIT, © 2026 Matt Pocock (see [LICENSE-mattpocock](./LICENSE-mattpocock))
>
> The **Grilling** and **Domain modeling** sections below are his text, verbatim except for heading levels. [GLOSSARY-FORMAT.md](./GLOSSARY-FORMAT.md) and [ADR-FORMAT.md](./ADR-FORMAT.md) are his files, unchanged. Sections marked *(orchestrate)* are additions.

## How this fits an orchestrate run *(orchestrate)*

1. **Facts first.** Run `orchestrate.py scan`, read the root AGENTS.md, the glossary (see below) and `docs/adr/`, and skim the code the request touches. Use subagents for deeper lookups. Never ask the user for something you can look up.
2. **Then grill** with the rules in **Grilling**, applying **Domain modeling** as you go. Work through the design tree until the frontier is empty, and visit every branch below that applies:
   - **Outcome:** what the user can observe when this is done, and the acceptance criteria. Push for testable statements.
   - **Non-goals:** what is explicitly out of scope for this job.
   - **Domain terms:** new or fuzzy words. Settle them, then write them to the glossary.
   - **Data:** new entities and fields, migrations, backfills, deletion and retention.
   - **Contracts between packages:** API shapes, shared types, events, and who owns each.
   - **Behavior at the edges:** empty, invalid, unauthorized, concurrent, offline, time zones, limits.
   - **Security and permissions:** who may do what.
   - **Done and verify:** which existing commands prove it works, and which new tests should exist.
   - **Risk:** what needs a human gate (migrations, deletes, auth, payments), the budget, and the rollout.
3. **Scale the session to the request.** A rename or a typo gets one short round ("here's what I'll do and how I'll verify it, yes?"). A cross-package feature gets as many rounds as its tree needs. If the user says *skip grilling* or *no grill*, skip it.
4. **Find the glossary.** Use `GLOSSARY.md` / `GLOSSARY-MAP.md` if they exist. If not, use `CONTEXT.md` / `CONTEXT-MAP.md` if they exist (the older name for the same file). If neither exists, create `GLOSSARY.md` lazily. Wherever **Domain modeling** says `GLOSSARY.md`, read it as the file this step found.
5. **Close the session.** When the frontier is empty, post a summary and ask the user to confirm it:
   - **Goal:** one sentence.
   - **Acceptance:** 3–7 testable bullets.
   - **Non-goals.**
   - **Decisions:** one line each, citing ADR files where they exist.
   - **Terms added or changed** in the glossary.
   - **Proposed route:** single task or job, and why.

   Don't plan until the user confirms. The confirmed summary becomes `goal`, `acceptance`, `non_goals` and `decisions` in `plan.json` (see **orchestrate-plan**). Subagents and the reviewer receive these verbatim.
6. **Commit the docs.** Glossary and ADR edits are written into the user's checkout, but jobs start from the last **commit**. Before running a job, show the user the doc changes and ask to commit them (for example `docs: glossary + ADRs for <job>`), so subagents see them. If the user declines, add the key decisions to `decisions` instead.

---

## Grilling

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

Format a round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Word each question so "yes" accepts your recommended answer.

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, etc.), dispatch a sub-agent with a fresh context window to find it; don't ask the user for anything you could look up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the sub-agent to report; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Do not act on it until the user confirms you have reached a shared understanding.

---

## Domain modeling

Actively build and sharpen the project's domain model as you design. This is the *active* discipline: challenging terms, inventing edge-case scenarios, and writing the glossary and decisions down the moment they crystallise. (Merely *reading* `GLOSSARY.md` for vocabulary is not this skill: that's a one-line habit any skill can do. This skill is for when you're changing the model, not just consuming it.)

### File structure

Most repos have a single context:

```
/
├── GLOSSARY.md
├── docs/
│   └── adr/
│       ├── 0001-event-sourced-orders.md
│       └── 0002-postgres-for-write-model.md
└── src/
```

If a `GLOSSARY-MAP.md` exists at the root, the repo has multiple contexts. The map points to where each one lives:

```
/
├── GLOSSARY-MAP.md
├── docs/
│   └── adr/                          ← system-wide decisions
├── src/
│   ├── ordering/
│   │   ├── GLOSSARY.md
│   │   └── docs/adr/                 ← context-specific decisions
│   └── billing/
│       ├── GLOSSARY.md
│       └── docs/adr/
```

Create files lazily: only when you have something to write. If no `GLOSSARY.md` exists, create one when the first term is resolved. If no `docs/adr/` exists, create it when the first ADR is needed.

### During the session

#### Challenge against the glossary

When the user uses a term that conflicts with the existing language in `GLOSSARY.md`, call it out immediately. "Your glossary defines 'cancellation' as X, but you seem to mean Y. Which is it?"

#### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical term. "You're saying 'account': do you mean the Customer or the User? Those are different things."

#### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios. Invent scenarios that probe edge cases and force the user to be precise about the boundaries between concepts.

#### Cross-reference with code

When the user states how something works, check whether the code agrees. If you find a contradiction, surface it: "Your code cancels entire Orders, but you just said partial cancellation is possible. Which is right?"

#### Update GLOSSARY.md inline

When a term is resolved, update `GLOSSARY.md` right there. Don't batch these up: capture them as they happen. Use the format in [GLOSSARY-FORMAT.md](./GLOSSARY-FORMAT.md).

`GLOSSARY.md` should be totally devoid of implementation details. Do not treat `GLOSSARY.md` as a spec, a scratch pad, or a repository for implementation decisions. It is a glossary and nothing else.

#### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse**: the cost of changing your mind later is meaningful
2. **Surprising without context**: a future reader will wonder "why did they do it this way?"
3. **The result of a real trade-off**: there were genuine alternatives and you picked one for specific reasons

If any of the three is missing, skip the ADR. Use the format in [ADR-FORMAT.md](./ADR-FORMAT.md).
