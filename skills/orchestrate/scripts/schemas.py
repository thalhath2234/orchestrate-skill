"""JSON Schemas for every model judgment the harness parses (passed via `claude --json-schema`).

Keep them forgiving where the harness is forgiving: the code still validates semantics
(ownership, the DAG, gates, verify weakening) after the schema has checked the shape.
"""
from __future__ import annotations

CATEGORIES = ["rename", "docs", "config", "bugfix", "feature", "refactor", "test", "migration",
              "review", "research"]
TYPE_PATTERN = "^(" + "|".join(CATEGORIES) + r"):[a-z0-9][a-z0-9-]{0,31}$"
ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,40}$"

_STRS = {"type": "array", "items": {"type": "string"}}

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
            _action("skip", ["task", "reason"], {"task": {"type": "string"}, "reason": {"type": "string"}}),
            _action("fix_round", ["reason"], {"reason": {"type": "string"}}),
            _action("accept", ["reason"], {"reason": {"type": "string"}}),
            _action("ask_human", ["question"], {"question": {"type": "string"}}),
            _action("abort", ["reason"], {"reason": {"type": "string"}}),
            _action("none", [], {}),
        ]}},
    },
}

FINDING = {
    "type": "object",
    "required": ["severity", "issue"],
    "properties": {
        "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
        "file": {"type": "string"}, "line": {"type": "integer"},
        "issue": {"type": "string"}, "suggestion": {"type": "string"},
    },
}

REVIEW = {
    "type": "object",
    "required": ["verdict", "summary", "acceptance", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
        "summary": {"type": "string"},
        "acceptance": {"type": "array", "items": {
            "type": "object", "required": ["criterion", "met", "evidence"],
            "properties": {"criterion": {"type": "string"}, "met": {"type": "boolean"},
                           "evidence": {"type": "string"}}}},
        "findings": {"type": "array", "items": FINDING},
    },
}

AREA_REVIEW = {
    "type": "object",
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": FINDING},
    },
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

REPORT = {
    "type": "object",
    "required": ["summary", "interfaces", "notes_for_dependents", "open_issues"],
    "properties": {
        "summary": {"type": "string"},
        "interfaces": {"type": "string"},
        "notes_for_dependents": {"type": "string"},
        "open_issues": {"type": "array", "items": OPEN_ISSUE},
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
    "required": ["job", "goal", "verify", "tasks"],
    "properties": {
        "job": {"type": "string", "pattern": ID_PATTERN},
        "goal": {"type": "string"},
        "acceptance": _STRS, "non_goals": _STRS, "decisions": _STRS,
        "setup": _STRS, "verify": {**_STRS, "minItems": 1}, "baseline": _STRS,
        "max_parallel": {"type": "integer"}, "mem_per_agent_gb": {"type": "number"},
        "budget_usd": {"type": "number"}, "review_rounds": {"type": "integer"},
        "area": {"type": "string"},
        "tasks": {"type": "array", "items": TASK, "minItems": 1},
    },
}
