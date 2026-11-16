"""One way to call the `claude` CLI in print mode, shared by run_task.py and orchestrate.py.

* Structured output: pass `schema` (a dict) and the reply is validated by Claude Code
  (`--json-schema`); it arrives in `structured`. Without a schema, or when an older CLI or
  a fake ignores it, `structured` falls back to the last JSON object in the text.
* Budget: `budget_usd` becomes `--max-budget-usd`; hitting it is error_kind "budget".
* API overload / rate limits are retried with backoff here instead of being reported as a
  broken environment (ORCHESTRATE_API_RETRIES, ORCHESTRATE_API_BACKOFF_S).
* Every call returns `metrics` (turns, tokens incl. cache reads, duration) so jobs can be
  measured.

* A safety-classifier decline (stop_reason "refusal") is error_kind "refusal", with its category in
  `refusal_category`: not evidence about the model. A reply cut off at max_tokens is "cut_off" and
  its structured output is dropped even if it parses (Sonnet 5.5 prompting guide: "treat any
  response whose stop_reason is max_tokens as failed, even if its text holds valid JSON").

error_kind: None | timeout | budget | max_turns | overloaded | rate_limit | api | exec | unparseable
            | refusal | cut_off
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

OVERLOAD_RE = re.compile(r"overloaded_error|\b529\b|Overloaded", re.I)
RATE_RE = re.compile(r"rate_limit_error|\b429\b|rate limit", re.I)


def claude_bin() -> str:
    return os.environ.get("ROUTER_CLAUDE_BIN", "claude")


def extract_json(text):
    """Last fenced ```json block, else the outermost {...} in text."""
    text = text or ""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for cand in reversed(blocks):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


def _metrics(data: dict, wall: float) -> dict:
    u = data.get("usage") or {}
    return {"turns": data.get("num_turns"), "input": u.get("input_tokens"),
            "output": u.get("output_tokens"), "cache_read": u.get("cache_read_input_tokens"),
            "cache_creation": u.get("cache_creation_input_tokens"),
            "duration_ms": data.get("duration_ms"), "wall_s": round(wall, 1)}


def _kind(data: dict) -> str | None:
    if data.get("stop_reason") == "refusal":
        return "refusal"
    if data.get("stop_reason") == "max_tokens":
        return "cut_off"
    if not data.get("is_error") and data.get("subtype", "success") == "success":
        return None
    sub = str(data.get("subtype") or "")
    if "max_budget" in sub or data.get("terminal_reason") == "budget_exhausted":
        return "budget"
    if "max_turns" in sub:
        return "max_turns"
    status = data.get("api_error_status")
    text = str(data.get("result") or "") + " " + " ".join(map(str, data.get("errors") or []))
    if status == 529 or OVERLOAD_RE.search(text):
        return "overloaded"
    if status == 429 or RATE_RE.search(text):
        return "rate_limit"
    if status:
        return "api"
    return "exec"


def call(prompt: str, model: str, effort: str, cwd: Path, *, perm: str, tools: list[str],
         resume: str | None = None, timeout: int = 3600, settings: dict | None = None,
         schema: dict | None = None, budget_usd: float | None = None,
         extra_args: list[str] | None = None, retries: int | None = None) -> dict:
    cmd = [claude_bin(), "-p", prompt, "--model", model, "--effort", effort,
           "--output-format", "json", "--permission-mode", perm]
    if settings:
        cmd += ["--settings", json.dumps(settings)]
    if tools:
        cmd += ["--allowedTools", *tools]
    if resume:
        cmd += ["--resume", resume]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if budget_usd is not None:
        cmd += ["--max-budget-usd", f"{max(budget_usd, 0.01):.2f}"]
    cmd += extra_args or []
    tries = int(os.environ.get("ORCHESTRATE_API_RETRIES", "3")) if retries is None else retries
    backoff = float(os.environ.get("ORCHESTRATE_API_BACKOFF_S", "20"))
    data: dict = {}
    kind: str | None = "exec"
    t0 = time.time()
    for attempt in range(max(tries, 0) + 1):
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"is_error": True, "error_kind": "timeout", "timeout": True,
                    "result": f"claude timed out after {timeout}s", "structured": None,
                    "metrics": {"wall_s": round(time.time() - t0, 1)}}
        try:
            data = json.loads(p.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            data = {"is_error": True, "result": (p.stdout + p.stderr)[-2000:], "subtype": "unparseable"}
            kind = ("overloaded" if OVERLOAD_RE.search(data["result"]) else
                    "rate_limit" if RATE_RE.search(data["result"]) else "unparseable")
        else:
            kind = _kind(data)
        if kind in ("overloaded", "rate_limit") and attempt < tries:
            time.sleep(backoff * (2 ** attempt))
            continue
        break
    data["error_kind"] = kind
    if kind:
        data["is_error"] = True
    usage = data.get("modelUsage") or {}
    data["model_id"] = next((k for k in usage if model in k), next(iter(usage), None))
    data["metrics"] = _metrics(data, time.time() - t0)
    so = data.get("structured_output")
    data["structured"] = so if isinstance(so, dict) else extract_json(str(data.get("result", "")))
    if kind == "cut_off":
        data["structured"] = None  # a truncated reply is never a verdict
    if kind == "refusal":
        sd = data.get("stop_details") or {}
        data["refusal_category"] = sd.get("category") if isinstance(sd, dict) else None
        if not data["refusal_category"] and data.get("safety_stops"):
            data["refusal_category"] = str(data["safety_stops"])[:200]
    return data
