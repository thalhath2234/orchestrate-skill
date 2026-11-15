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

MODEL_ORDER = ["haiku", "sonnet", "opus", "fable"]          # cheapest -> most capable
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
# Each model's API default effort [EFFORT]: "Opus 5.5 and Haiku 5.5 default to medium",
# Sonnet 5.5 and Fable 5.2 to high. "Start at the default and adjust on evals" [CHOOSE].
DEFAULT_EFFORT = {"haiku": "medium", "sonnet": "high", "opus": "medium", "fable": "high"}
# Fable needs account access (and on some plans a usage-credits consent); without it the
# call falls back instead of failing the job.
FALLBACK = {"fable": "opus"}

ROLES: dict[str, tuple[str, str]] = {
    # One call that decides the whole job: decomposition, ownership, the definition of done.
    # Opus 5.5 (the user's choice): [CHOOSE] "complex agentic coding and enterprise work ...
    # large-scale refactoring, complex systems engineering". high, not the default medium: the
    # plan sets the bar for every later step, and [COST] measured high above medium on coding.
    "planner": ("opus", "high"),
    # Small decisions in a long job. [COST] "For most agent workloads, start with Claude
    # Opus 5.5 at its default effort (medium)". Our sweep: opus/medium 13/13 (= opus/high).
    "manager": ("opus", "medium"),
    # The final review gates the merge, so it gets the strongest model. [COST] defect search:
    # Fable 5.2 alone scored 1.6-5.9 points above Opus 5.5 at high, for 1.5-2.4x the cost.
    "reviewer": ("fable", "high"),
    # Per-area reviewers on big diffs (the final reviewer still sees everything).
    # [COST] defect search: Opus 5.5 lost 34 points at low vs medium; [LEGAL] Opus 5.5 where
    # accuracy is required. high, not the default medium: review is where effort pays.
    "area_reviewer": ("opus", "high"),
    # Merge-conflict resolution: everyday coding [CHOOSE] at Sonnet's default effort.
    "integrator": ("sonnet", "high"),
    # Failure-cause classification. [ROUTE] "Claude Haiku 5.5 ... at low effort handles
    # simple, high-volume requests such as classification". Our sweep: haiku/low = sonnet/low.
    "classifier": ("haiku", "low"),
    # Extract AGENTS.md notes from reports: extraction is Haiku's job [CHOOSE], default effort.
    "lessons": ("haiku", "medium"),
}


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


def advisor_args(model: str, advisor: str | None) -> list[str]:
    """`--advisor X` when the pairing is valid: an advisor must rank at or above the main
    model [ADVISOR]. Off by default: [COST] measured an advisor buying about what more effort
    buys (+1.7 points for 2.1x), so it is opt-in per plan, for long tasks hard in spots."""
    if not advisor or advisor not in MODEL_ORDER or advisor == "haiku" or rank(advisor) < rank(model):
        return []
    return ["--advisor", advisor]


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
