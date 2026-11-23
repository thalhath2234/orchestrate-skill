"""Which model and effort each role uses, and why. One place, so every default has a source.

Defaults follow Anthropic's published guidance (platform.claude.com, checked 2026-11-16):

  [CHOOSE]  about-claude/models/choosing-a-model     (selection matrix, effort defaults)
  [COST]    about-claude/models/optimizing-for-cost-and-intelligence
  [EFFORT]  build-with-claude/effort
  [ROUTE]   use-case guide: ticket routing           (classification)
  [LEGAL]   use-case guide: legal summarization      (accuracy-critical reading)
  [ADVISOR] code.claude.com/docs/en/advisor

and this repo's own evals (`make eval-sweep-*`). Override any role per plan with
`"models": {"<role>": "<model>/<effort>"}`, or per machine with ORCHESTRATE_MODEL_<ROLE>.

Claude Code aliases resolve to: haiku -> claude-haiku-5-5, sonnet -> claude-sonnet-5-5,
opus -> claude-opus-5-5, fable -> claude-fable-5-2 (checked with CLI 2.1.325).
"""
from __future__ import annotations

import os
import re

MODEL_ORDER = ["haiku", "sonnet", "opus", "fable"]          # cheapest -> most capable
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
# Each model's API default effort [EFFORT]: "Opus 5.5 and Haiku 5.5 default to medium",
# Sonnet 5.5 and Fable 5.2 to high. "Start at the default and adjust on evals" [CHOOSE].
DEFAULT_EFFORT = {"haiku": "medium", "sonnet": "high", "opus": "medium", "fable": "high"}
# Provider model ids, for launchers that take an exact id (Orca's worker-start --model).
MODEL_IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
             "fable": "claude-fable-5-2"}
# Fable needs account access (and on some plans a usage-credits consent); without it the
# call falls back instead of failing the job.
FALLBACK = {"fable": "opus"}

ROLES: dict[str, tuple[str, str]] = {
    # The user's model assignment (2026-11-16): advisor Fable/medium (high when medium finds
    # nothing), planner Fable/medium, manager, worker, reviewer and cleanup Opus/medium.
    #
    # Second opinion at the two points of no return: before the plan executes and before the
    # job is handed over. Fable 5.2 at medium first; a clean medium pass is re-checked at high
    # (ADVISOR_ESCALATE), so "nothing found" is only believed after the stronger look.
    # [CHOOSE] Fable 5.2 is the most capable model; [ADVISOR] an advisor ranks above the workers.
    "advisor": ("fable", "medium"),
    # One call that decides the whole job: decomposition, ownership, the definition of done.
    # Fable 5.2 at medium (the user's choice, 2026-11-16). [CHOOSE] Fable 5.2 is the most capable
    # model; the Fable 5.2 prompting guide: "early testers had good results with Claude Fable 5.2
    # as the planner and orchestrator for other models". Falls back to Opus without Fable access.
    "planner": ("fable", "medium"),
    # Retries, splits and steers the tasks. Fable 5.2 at medium (the user's choice, 2026-11-23:
    # Fable delegates). Was Opus 5.5 at medium, where our sweep had 13/13.
    "manager": ("fable", "medium"),
    # Writes each subagent's brief: what to do, how to test it, what done looks like, and for a
    # bug fix the scenario that shows the bug. Fable 5.2 at medium (the user's choice, 2026-11-23).
    "briefer": ("fable", "medium"),
    # Every subagent starts here, whatever its tier, and escalates on failure
    # (opus/high -> opus/xhigh -> fable/high). Opus 5.5 at its default effort.
    "worker": ("opus", "medium"),
    # Final reviewer, told to hunt every edge case. Opus 5.5 at its default effort; the
    # advisor's final check (Fable) runs after it.
    "reviewer": ("opus", "medium"),
    # Per-area reviewers on big diffs (the final reviewer still sees everything).
    "area_reviewer": ("opus", "medium"),
    # Tidies the merged diff before the final check: dead code, debug output, stray TODOs.
    "cleanup": ("opus", "medium"),
    # Merge-conflict resolution. Opus 5.5 at its default effort (the user's choice, 2026-11-18:
    # no Sonnet in the defaults).
    "integrator": ("opus", "medium"),
    # Failure-cause classification. [ROUTE] "Claude Haiku 5.5 ... at low effort handles
    # simple, high-volume requests such as classification". Our sweep: haiku/low = sonnet/low.
    "classifier": ("haiku", "low"),
    # Extract AGENTS.md notes from reports: extraction is Haiku's job [CHOOSE], default effort.
    "lessons": ("haiku", "medium"),
    # Looks at the job's screenshots (desktop and mobile) when plan.ui is set: vision plus judgment
    # against the definition of done. Opus 5.5 at its default effort, like the code reviewer.
    "ui_reviewer": ("opus", "medium"),
}
# When the advisor at its role effort finds nothing, ask again at this effort before believing it.
ADVISOR_ESCALATE = "high"


def rank(model: str) -> int:
    return MODEL_ORDER.index(model) if model in MODEL_ORDER else len(MODEL_ORDER)


def parse(spec: str) -> tuple[str, str]:
    m, _, e = spec.strip().partition("/")
    if m not in MODEL_ORDER:
        raise ValueError(f"unknown model {m!r}: expected one of {MODEL_ORDER}")
    e = e or DEFAULT_EFFORT[m]
    if e not in EFFORT_ORDER:
        raise ValueError(f"unknown effort {e!r}: expected one of {EFFORT_ORDER}")
    return m, e


def clamp(model: str, effort: str, max_model: str | None) -> tuple[str, str]:
    """Cap a config at `max_model`. Fable capped at Opus becomes opus/high: [COST] xhigh buys
    about 1.4 points over high for 2.5x the cost, so high is the better stand-in."""
    if not max_model or rank(model) <= rank(max_model):
        return model, effort
    return (max_model, "high") if max_model != "haiku" else (max_model, DEFAULT_EFFORT[max_model])


def role(name: str, plan: dict | None = None) -> tuple[str, str]:
    """(model, effort) for a role: plan override > env override > default, then max_model."""
    plan = plan or {}
    spec = (plan.get("models") or {}).get(name) or os.environ.get(f"ORCHESTRATE_MODEL_{name.upper()}")
    model, effort = parse(spec) if spec else ROLES[name]
    return clamp(model, effort, plan.get("max_model"))


def fallback_args(model: str) -> list[str]:
    fb = FALLBACK.get(model)
    return ["--fallback-model", fb] if fb else []


def advisor_args(model: str, advisor: str | None, max_model: str | None = None) -> list[str]:
    """`--advisor X` for a worker running `model`, or [] when the pairing can't work.

    The advisor tool (claude --advisor) is off by default, whatever the user's `advisorModel`
    setting says (claude_cli.child_env). [COST] measured it buying about what
    more effort buys (+1.7 points for 2.1x), and on another benchmark Opus rarely consulted it
    and scored 7 points lower. It is opt-in per plan, for long tasks that are hard in spots.
    `max_model` caps the advisor like any role (Fable capped at Opus -> an Opus advisor).
    [ADVISOR] An advisor must rank at or above the main model, else Claude Code silently drops it."""
    if not advisor or advisor not in MODEL_ORDER or advisor == "haiku":
        return []
    if max_model and rank(advisor) > rank(max_model):
        advisor = max_model
    if advisor == "haiku" or rank(advisor) < rank(model):
        return []
    return ["--advisor", advisor]


# A launch that fails because of the advisor, not the model: Fable's usage-credits consent not
# accepted, an advisor the account or allowlist can't use, or an invalid pairing [ADVISOR].
ADVISOR_LAUNCH_FAIL = re.compile(r"advisor|usage[- ]credits|/model fable|availableModels", re.I)


def fell_back(model: str, model_id: str | None) -> bool:
    return bool(model_id) and model in FALLBACK and model not in model_id


def validate_plan(plan: dict) -> list[str]:
    errs = []
    for r, spec in (plan.get("models") or {}).items():
        if r not in ROLES:
            errs.append(f"models: unknown role {r!r}; roles are {sorted(ROLES)}")
            continue
        try:
            parse(spec)
        except (ValueError, AttributeError) as e:
            errs.append(f"models.{r}: {e}")
    if plan.get("max_model") and plan["max_model"] not in MODEL_ORDER:
        errs.append(f"max_model must be one of {MODEL_ORDER}")
    if plan.get("advisor") and plan["advisor"] not in ("sonnet", "opus", "fable"):
        errs.append("advisor must be sonnet, opus or fable")
    return errs
