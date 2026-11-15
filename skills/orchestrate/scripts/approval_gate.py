#!/usr/bin/env python3
"""Claude Code PreToolUse hook: approvals go through the human, not the agent (P1.1).

Gate approvals (`orchestrate.py resume --approve`), routing/AGENTS.md proposals
(`router.py approve`) and answers to the manager (`orchestrate.py resume --answer`) are
decisions that belong to the user. The skill tells the agent so, but a prompt is not
enforcement: this hook makes Claude Code show the exact command, including the relayed answer,
in its own permission prompt, so the user approves it there. An approval typed in chat
approves nothing.

Installed automatically with the plugin (hooks/hooks.json). For `npx skills` installs, add the
snippet printed by `orchestrate.py guard-hook` to ~/.claude/settings.json.
"""
import json
import re
import sys

RULES = [
    (re.compile(r"orchestrate\.py\b[^|;&]*\bresume\b[^|;&]*--approve\b"),
     "approves a gated orchestrate task"),
    (re.compile(r"router\.py\b[^|;&]*\bapprove\b"),
     "approves an orchestrate routing or AGENTS.md proposal"),
    (re.compile(r"orchestrate\.py\b[^|;&]*\bresume\b[^|;&]*--answer\b"),
     "sends your answer to the orchestrate manager"),
]


def decide(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash":
        return None
    cmd = str((payload.get("tool_input") or {}).get("command", ""))
    for rx, what in RULES:
        if rx.search(cmd):
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": f"This command {what}. Only you can make that decision: "
                                            "check the command (and any answer text) before allowing it."}}
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    out = decide(payload)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
