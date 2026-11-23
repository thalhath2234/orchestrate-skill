"""JSON Schemas for every model judgment the harness parses (passed via `claude --json-schema`).

Keep them forgiving where the harness is forgiving: the code still validates semantics
(ownership, the DAG, gates, verify weakening) after the schema has checked the shape.
"""
from __future__ import annotations

import re

CATEGORIES = ["rename", "docs", "config", "bugfix", "feature", "refactor", "test", "migration",
              "review", "research"]
TYPE_PATTERN = "^(" + "|".join(CATEGORIES) + r"):[a-z0-9][a-z0-9-]{0,31}$"
ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,40}$"

_STRS = {"type": "array", "items": {"type": "string"}}

# A proposed addition to a definition of done. The bar only goes up: criteria are added, never
# removed or loosened (only the user lowers it). scope: "job" (acceptance) or a task id.
BAR_ITEM = {
    "type": "object",
    "required": ["scope", "criterion", "why"],
    "properties": {"scope": {"type": "string"}, "criterion": {"type": "string"}, "why": {"type": "string"}},
}

# A page the UI review screenshots: an app path, an optional slug name and optional flow steps,
# each one of {"click": css}, {"type": [css, text]}, {"press": key}, {"wait_for": css},
# {"wait_ms": n}, {"screenshot": name}. ui_review.validate_ui checks the details.
UI_PAGE = {
    "type": "object", "required": ["path"],
    "properties": {"path": {"type": "string"}, "name": {"type": "string"},
                   "steps": {"type": "array", "items": {"type": "object"}}},
}

# plan.ui: how to serve the app for the UI review (see ui_review.py).
UI = {
    "type": "object",
    "required": ["dev", "url"],
    "properties": {
        "dev": {"type": "string"},                       # command that serves the app (integration worktree)
        "url": {"type": "string"},                       # its base URL, e.g. http://127.0.0.1:5173
        "ready_timeout_s": {"type": "number"},
        "paths": _STRS,                                  # repo globs: review only when the diff touches one
        "viewports": {"type": "array", "items": {"type": "string", "enum": ["desktop", "mobile"]}},
        "pages": {"type": "array", "items": UI_PAGE},   # pages for the whole job
    },
}

TASK = {
    "type": "object",
    "required": ["id", "title", "goal", "type", "tier", "owns", "done", "verify"],
    "properties": {
        "id": {"type": "string", "pattern": ID_PATTERN},
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "type": {"type": "string", "pattern": TYPE_PATTERN},
        "tier": {"type": "string", "enum": ["T0", "T1", "T2", "T3"]},
        "owns": {**_STRS, "minItems": 1},
        "reads": _STRS,
        "depends_on": _STRS,
        "interfaces": {"type": "string"},
        "done": {**_STRS, "minItems": 1},
        "verify": {**_STRS, "minItems": 1},
        "gate": {"type": "string"},
        # pages the UI review screenshots for this task (needs plan.ui); a split keeps them
        "ui": {"type": "array", "items": UI_PAGE},
    },
}


def _action(name: str, required: list[str], props: dict) -> dict:
    return {"type": "object", "required": ["action", *required],
            "properties": {"action": {"const": name}, **props}}


MANAGER = {
    "type": "object",
    "required": ["note", "actions"],
    "properties": {
        "note": {"type": "string"},
        "actions": {"type": "array", "items": {"anyOf": [
            _action("retry", ["task", "instructions"], {
                "task": {"type": "string"}, "instructions": {"type": "string"},
                "tier": {"type": "string", "enum": ["T0", "T1", "T2", "T3"]},
                "owns": _STRS, "reads": _STRS, "verify": _STRS, "done": _STRS}),
            _action("split", ["task", "into"], {
                "task": {"type": "string"}, "into": {"type": "array", "items": TASK, "minItems": 2}}),
            _action("add_task", ["task"], {"task": TASK}),
            _action("amend", ["task", "note"], {
                "task": {"type": "string"}, "note": {"type": "string"}, "reads": _STRS}),
            _action("raise_bar", ["scope", "criteria", "why"], {
                "scope": {"type": "string"}, "criteria": {**_STRS, "minItems": 1}, "why": {"type": "string"}}),
            _action("skip", ["task", "reason"], {"task": {"type": "string"}, "reason": {"type": "string"}}),
            _action("fix_round", ["reason"], {"reason": {"type": "string"}}),
            _action("accept", ["reason"], {"reason": {"type": "string"}}),
            _action("ask_human", ["question"], {"question": {"type": "string"}}),
            _action("abort", ["reason"], {"reason": {"type": "string"}}),
            _action("none", [], {}),
        ]}},
    },
}

# Field order is generation order under structured output: the evidence comes before the
# judgment (Sonnet 5.5 prompting guide, "Reasoning tasks with JSON output"). Reviewers report every
# finding with a confidence; the harness filters (blocking_findings), not the model.
FINDING = {
    "type": "object",
    "required": ["issue", "confidence", "severity"],
    "properties": {
        "file": {"type": "string"}, "line": {"type": "integer"},
        "issue": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
        "suggestion": {"type": "string"},
    },
}

# One edge case the reviewer checked. Every reviewer enumerates the edge cases of what changed
# (empty, null, boundaries, errors, concurrency, auth...) and says how each is handled; one marked
# unhandled without a matching finding becomes a finding in code, at the severity (major when it
# gave none) and confidence the reviewer gave it: like any finding, only `low` doesn't block.
EDGE_CASE = {
    "type": "object",
    "required": ["case", "evidence", "handled"],
    "properties": {"case": {"type": "string"}, "file": {"type": "string"},
                   "evidence": {"type": "string"}, "handled": {"type": "boolean"},
                   # for an unhandled case: how much it matters (default major)
                   "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                   # for an unhandled case: how sure the reviewer is (missing blocks, like high)
                   "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
}

# Each task's brief from the briefer: its words for the subagent, and for a bug fix how to see the bug.
BRIEFS = {
    "type": "object",
    "required": ["briefs"],
    "properties": {"briefs": {"type": "array", "items": {
        "type": "object", "required": ["id", "brief"],
        "properties": {"id": {"type": "string"}, "brief": {"type": "string"},
                       "repro": {"type": "object", "required": ["scenario", "symptom"],
                                 "properties": {"scenario": {"type": "string"}, "symptom": {"type": "string"}}}}}}},
}

REVIEW = {
    "type": "object",
    "required": ["acceptance", "edge_cases", "findings", "summary", "verdict"],
    "properties": {
        "acceptance": {"type": "array", "items": {
            "type": "object", "required": ["criterion", "evidence", "met"],
            "properties": {"criterion": {"type": "string"}, "evidence": {"type": "string"},
                           "met": {"type": "boolean"}}}},
        "edge_cases": {"type": "array", "items": EDGE_CASE},
        "findings": {"type": "array", "items": FINDING},
        "raise_bar": {"type": "array", "items": BAR_ITEM},
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
    },
}

# The UI reviewer's judgment of the job's screenshots (plan.ui). One `pages` entry per shot id,
# observations before ok (evidence first). Its findings join the round's review as "[UI] ...".
UI_REVIEW = {
    "type": "object",
    "required": ["pages", "findings", "summary", "verdict"],
    "properties": {
        "pages": {"type": "array", "items": {
            "type": "object", "required": ["shot", "observations", "ok"],
            "properties": {"shot": {"type": "string"}, "observations": {"type": "string"},
                           "ok": {"type": "boolean"}}}},
        "findings": {"type": "array", "items": FINDING},
        # browser evidence the reviewer says is expected (a 404 page on purpose, a step whose
        # selector is stale): only an entry here waives the harness's own finding for it
        "expected": {"type": "array", "items": {
            "type": "object", "required": ["shot", "kind", "why"],
            "properties": {"shot": {"type": "string"},
                           "kind": {"type": "string", "enum": ["http_4xx", "step_error", "page_error"]},
                           "why": {"type": "string"}}}},
        # one answer per earlier UI finding listed in the prompt: still there or fixed
        "earlier": {"type": "array", "items": {
            "type": "object", "required": ["id", "fixed", "observations"],
            "properties": {"id": {"type": "string"}, "observations": {"type": "string"},
                           "fixed": {"type": "boolean"}}}},
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
    },
}

AREA_REVIEW = {
    "type": "object",
    "required": ["edge_cases", "findings", "summary", "verdict"],
    "properties": {
        "edge_cases": {"type": "array", "items": EDGE_CASE},
        "findings": {"type": "array", "items": FINDING},
        "raise_bar": {"type": "array", "items": BAR_ITEM},
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
    },
}

# The advisor's second opinion, before the plan runs ("plan") and before hand-over ("final").
# done_restated: the definition of done in the advisor's own words, so a misreading shows.
ADVICE = {
    "type": "object",
    "required": ["done_restated", "findings", "summary", "verdict"],
    "properties": {
        "done_restated": {"type": "string"},
        "findings": {"type": "array", "items": FINDING},
        "raise_bar": {"type": "array", "items": BAR_ITEM},
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["proceed", "revise"]},
    },
}

CLEANUP = {
    "type": "object",
    "required": ["changed", "removed", "notes"],
    "properties": {"changed": {"type": "boolean"}, "removed": _STRS, "notes": {"type": "string"}},
}

CLASSIFIER = {
    "type": "object",
    "required": ["evidence", "cause"],
    # evidence before cause: on Haiku at low effort, reasoning written out first helps the
    # label (ticket-routing guide: "this prompt is written for Claude Haiku 5.5 at low effort").
    "properties": {
        "evidence": {"type": "string"},
        "cause": {"type": "string", "enum": ["environment", "context", "effort", "capability"]},
        "agents_md": {"anyOf": [{"type": "null"}, {
            "type": "object", "required": ["file", "text"],
            "properties": {"file": {"type": "string"}, "text": {"type": "string"}}}]},
    },
}

INTEGRATOR = {
    "type": "object",
    "required": ["resolved", "notes"],
    "properties": {"resolved": {"type": "boolean"}, "notes": {"type": "string"}},
}

OPEN_ISSUE = {
    "type": "object",
    "required": ["issue", "kind", "blocks_acceptance"],
    "properties": {
        "issue": {"type": "string"},
        "kind": {"type": "string", "enum": ["needs_file", "needs_decision", "fyi"]},
        "blocks_acceptance": {"type": "boolean"},
        "files": _STRS,
    },
}

DONE_CHECK = {
    "type": "object",
    "required": ["id", "met", "evidence"],
    "properties": {"id": {"type": "string", "pattern": r"^[DU][0-9]+$"}, "met": {"type": "boolean"},
                   "evidence": {"type": "string"}},
}

RUN = {"type": "object", "required": ["command", "output"],
       "properties": {"command": {"type": "string"}, "output": {"type": "string"}}}
REPRO = {"type": "object", "required": ["reproduced", "before"],
         "properties": {"reproduced": {"type": "boolean"}, "before": RUN, "after": RUN}}

REPORT = {
    "type": "object",
    "required": ["summary", "interfaces", "notes_for_dependents", "open_issues", "done_check", "raise_bar"],
    "properties": {
        "summary": {"type": "string"},
        "interfaces": {"type": "string"},
        "notes_for_dependents": {"type": "string"},
        "open_issues": {"type": "array", "items": OPEN_ISSUE},
        "done_check": {"type": "array", "items": DONE_CHECK},   # one per [D#]/[U#] in the brief
        "raise_bar": {"type": "array", "items": BAR_ITEM},
        "repro": REPRO,   # bug fixes: the scenario run before the fix and again after it
    },
}

LESSONS = {
    "type": "object",
    "required": ["lessons"],
    "properties": {"lessons": {"type": "array", "items": {
        "type": "object", "required": ["file", "text", "evidence"],
        "properties": {"file": {"type": "string"}, "text": {"type": "string"},
                       "evidence": {"type": "string"}}}}},
}

PLAN = {
    "type": "object",
    "required": ["job", "goal", "definition_of_done", "acceptance", "verify", "tasks"],
    "properties": {
        "job": {"type": "string", "pattern": ID_PATTERN},
        "goal": {"type": "string"},
        "definition_of_done": {"type": "string"},
        "acceptance": {**_STRS, "minItems": 1}, "non_goals": _STRS, "decisions": _STRS,
        # choices the planner subagent made that the user hasn't confirmed (it can't ask): each
        # names the choice and the alternative. validate warns until the user moves them into
        # decisions; no subagent, reviewer or manager prompt ever sees them.
        "assumptions": {**_STRS, "maxItems": 5},
        "quality_bar": _STRS,
        "setup": _STRS, "verify": {**_STRS, "minItems": 1}, "baseline": _STRS,
        "max_parallel": {"type": "integer"}, "mem_per_agent_gb": {"type": "number"},
        "budget_usd": {"type": "number"}, "review_rounds": {"type": "integer"},
        "area": {"type": "string"},
        "ui": UI,
        "tasks": {"type": "array", "items": TASK, "minItems": 1},
    },
}


NO_EVIDENCE = frozenset((
    "none", "n/a", "na", "nil", "null", "nothing", "no evidence", "not applicable", "unknown", "tbd", "todo",
    "yes", "true", "okay", "pass", "passed", "passes", "done", "works", "working", "verified", "checked",
    "tested", "confirmed", "complete", "completed", "implemented", "handled", "fixed", "fine", "good",
    "correct", "looks good", "looks fine", "all good", "it works", "see above", "as above", "same as above",
    "see diff", "see code", "evidence", "lgtm", "ok ok", "trust me", "none found", "nothing found", "see the diff",
    "see the code", "by inspection", "code inspection"))


def has_evidence(value) -> bool:
    """Something to check (a command, file:line, test, output), not a placeholder word."""
    text = " ".join(str(value or "").lower().split())
    text = re.sub(r"^\W+|\W+$", "", text)
    return len(text) >= 4 and text not in NO_EVIDENCE
