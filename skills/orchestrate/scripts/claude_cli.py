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

* The advisor tool is off unless the call passes `--advisor`: a user's `advisorModel` setting would
  otherwise give every role a second paid model. CLAUDE_CODE_DISABLE_ADVISOR_TOOL is the switch
  (an empty `advisorModel` in --settings doesn't work), set per child because it also disables
  `--advisor`.
* `only` is the complete tool set of a call (`--tools`, and no MCP servers). `tools` is
  `--allowedTools`, which only pre-approves: without `only` every other tool is still offered, and
  the user's own allow rules apply to it.

* ORCHESTRATE_HERMETIC=1 runs the call without the user's MCP servers and hooks
  (`--strict-mcp-config` with an empty `--mcp-config`, `disableAllHooks` merged into `--settings`).
  The evals set it so a pass rate does not depend on whose machine runs them. CLAUDE.md and other
  setting sources still load: the user's rules can be part of their definition of done.

error_kind: None | timeout | budget | max_turns | overloaded | rate_limit | usage_limit | api | exec | unparseable
            | refusal | cut_off
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import jail
import router

OVERLOAD_RE = re.compile(r"overloaded_error|\b529\b|Overloaded", re.I)
RATE_RE = re.compile(r"rate_limit_error|\b429\b|rate limit", re.I)
# The plan's own limit, in the CLI's words: retrying in seconds can't help, waiting for the reset can.
# Not "requires usage credits": that is one model's, and --fallback-model handles it.
USAGE_RE = re.compile(r"^\W*(?:You've hit your|You've reached your|You're out of (?:extra usage|usage credits)"
                      r"|Your org is out of usage)")
RESET_RE = re.compile(r"resets (?:([A-Z][a-z]{2}) (\d{1,2}), )?(\d{1,2})(?::(\d\d))?([ap]m) \(([\w/+-]+)\)")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
LIMIT_WAIT_H = 6.0  # a 5-hour window is waited out; a weekly one stops the job, resumable
stopping = threading.Event()  # set on an interrupt: nothing keeps waiting for a reset


# Linux refuses a single argument of 128 KiB or more (MAX_ARG_STRLEN, in bytes): longer prompts go on stdin.
ARG_PROMPT_MAX = 100_000
NO_ADVISOR = "CLAUDE_CODE_DISABLE_ADVISOR_TOOL"
READ_ONLY = ["Read", "Grep", "Glob"]


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


def _cost_of_this_call(data: dict, resume: str | None) -> None:
    """On --resume the CLI reports the session's running total (its usage, and --max-budget-usd,
    count this process alone), so the total last reported is taken off. It's kept on disk: a resumed
    job's manager continues a session another process started. A total never seen counts in full."""
    sid, total = data.get("session_id"), data.get("total_cost_usd")
    if not isinstance(sid, str) or not re.fullmatch(r"[\w-]{1,100}", sid) or not isinstance(total, (int, float)):
        return
    seen = router.DIR / "sessions" / sid
    used = data.get("modelUsage") or {}
    out = (data.get("usage") or {}).get("output_tokens")
    # a CLI that reports per call: its per-model totals are this call's usage, not the session's
    per_call = bool(out) and bool(used) and sum(u.get("outputTokens") or 0 for u in used.values()) <= out
    if resume == sid and not per_call:
        with contextlib.suppress(OSError, ValueError):
            before = float(seen.read_text())
            if total >= before:  # less would be a CLI that reports per call
                data["total_cost_usd"] = total - before
    with contextlib.suppress(OSError):
        seen.parent.mkdir(parents=True, exist_ok=True)
        tmp = seen.with_name(f"{sid}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(repr(float(total)))
        os.replace(tmp, seen)


INTERRUPT_GRACE_S = 10


def _interrupt(pid: int) -> None:
    """SIGINT to claude itself. In the jail that is the first process under the bwraps (bwrap doesn't
    pass signals on, and killing it loses the reply)."""
    with contextlib.suppress(OSError, ValueError):
        while Path(f"/proc/{pid}/comm").read_text().strip() == "bwrap":
            kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            if not kids:
                return
            pid = int(kids[0])
        os.kill(pid, signal.SIGINT)


def _run(cmd: list[str], cwd: Path, env: dict, piped: str | None, timeout: float) -> tuple[str, str, bool]:
    """(stdout, stderr, timed out). At the timeout claude is interrupted first: it then prints its
    result with what the call cost, which a kill loses. It is killed if that doesn't stop it."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=None if piped is None else subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    try:
        try:
            out, err = p.communicate(piped, timeout=timeout)
            return out, err, False
        except subprocess.TimeoutExpired:
            _interrupt(p.pid)
        try:
            out, err = p.communicate(timeout=INTERRUPT_GRACE_S)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                out, err = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # something it started holds the pipes
                out, err = "", ""
        return out, err, True
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def reset_wait(text: str, now: float | None = None) -> float | None:
    """Seconds until the reset a limit message names ("resets 7am (Asia/Tokyo)", "resets Nov 21,
    2am (...)"); 0 when it just passed, None when it names none we can read."""
    m = RESET_RE.search(text or "")
    if not m:
        return None
    mon, day, hour, minute, half, zone = m.groups()
    try:
        import zoneinfo
        here = datetime.datetime.fromtimestamp(time.time() if now is None else now, zoneinfo.ZoneInfo(zone))
        at = here.replace(hour=int(hour) % 12 + (12 if half == "pm" else 0), minute=int(minute or 0),
                          second=0, microsecond=0)
        if mon:
            at = at.replace(month=MONTHS.index(mon) + 1, day=int(day))
            if (here - at).days > 180:
                at = at.replace(year=at.year + 1)
        late = here.timestamp() - at.timestamp()
        if late > 600 and not mon:
            at += datetime.timedelta(days=1)
        return max(at.timestamp() - here.timestamp(), 0.0)
    except (ImportError, KeyError, ValueError, OSError):
        return None


def pause(seconds: float) -> bool:
    """Sleep; False when an interrupt cut it short."""
    return not stopping.wait(seconds)


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
    if USAGE_RE.match(str(data.get("result") or "")):
        return "usage_limit"
    if status == 529 or OVERLOAD_RE.search(text):
        return "overloaded"
    if status == 429 or RATE_RE.search(text):
        return "rate_limit"
    if status:
        return "api"
    return "exec"


def _parse(stdout: str):
    """The CLI's JSON reply: all of stdout, else its last line. Lines split on LF only: Node writes
    U+2028/U+2029 raw inside strings, and str.splitlines() would cut the JSON there."""
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        d = json.loads(stdout.strip().split("\n")[-1])
    if not isinstance(d, dict):
        raise ValueError(f"the reply is JSON but not an object: {type(d).__name__}")
    return d


RULES_MAX_BYTES = 60_000  # one argument, so well under ARG_PROMPT_MAX


def project_rules(cwd: Path) -> str:
    """The repo's committed CLAUDE.md (or .claude/CLAUDE.md) and the files it imports with an
    `@path` line, from the main checkout's HEAD: the user's own rules, never the copy on a branch
    workers wrote. "" when there is none."""
    def git(at, *args):
        try:
            p = subprocess.run([*jail.GIT, *args], cwd=at, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else ""

    main = git(cwd, "worktree", "list", "--porcelain").split("\n", 1)[0]
    if not main.startswith("worktree ") or not Path(main[9:]).is_dir():
        return ""
    root = Path(main[9:])
    for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
        text = git(root, "show", f"HEAD:{name}")
        if text:
            parts = [text]
            for line in text.splitlines():
                ref = line.strip()
                if re.fullmatch(r"@[\w./-]+", ref) and ".." not in ref and not ref.startswith("@/"):
                    body = git(root, "show", f"HEAD:{ref[1:].lstrip('./') if ref.startswith('@./') else ref[1:]}")
                    if body:
                        parts.append(f"## {ref[1:]}\n{body}")
            body = "\n\n".join(parts).replace("\x00", "").encode("utf-8")[:RULES_MAX_BYTES].decode("utf-8", "ignore")
            return "The repository's project instructions (the user's committed CLAUDE.md):\n\n" + body
    return ""


REFRESH_S = 240  # the CLI refreshes a login in its last 5 minutes


def refresh_login() -> None:
    """The jail can't save a refreshed login, so a free command refreshes it out here; the jailed
    CLI reads the replaced file before its next request. Output (the account's email) is discarded,
    and the cwd is the home: a worktree's settings could run hooks."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([claude_bin(), "auth", "status"], cwd=Path.home(), env=child_env(),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


@contextlib.contextmanager
def login_kept_fresh(on: bool):
    if not on:
        yield
        return
    done = threading.Event()

    def keep() -> None:
        while not done.wait(REFRESH_S):
            refresh_login()

    refresh_login()
    threading.Thread(target=keep, daemon=True).start()
    try:
        yield
    finally:
        done.set()


def child_env(extra_args: list[str] | None = None) -> dict:
    """The environment for one claude child: no advisor tool unless this call asked for one."""
    env = dict(os.environ)
    if "--advisor" in (extra_args or []):
        env.pop(NO_ADVISOR, None)
    else:
        env[NO_ADVISOR] = "1"
    return env


def call(prompt: str, model: str, effort: str, cwd: Path, *, perm: str, tools: list[str],
         resume: str | None = None, timeout: int = 3600, settings: dict | None = None,
         schema: dict | None = None, budget_usd: float | None = None,
         extra_args: list[str] | None = None, retries: int | None = None,
         only: list[str] | None = None, confine: Path | None = None, wait: bool = True) -> dict:
    """One model call. A plan usage limit that resets within ORCHESTRATE_LIMIT_WAIT_H hours is
    waited out here, for every role, and the session continued; a longer one is `usage_limit`."""
    try:
        left = float(os.environ.get("ORCHESTRATE_LIMIT_WAIT_H", LIMIT_WAIT_H)) * 3600
    except ValueError:
        left = LIMIT_WAIT_H * 3600
    kw = dict(perm=perm, tools=tools, timeout=timeout, settings=settings, schema=schema, budget_usd=budget_usd,
              extra_args=extra_args, retries=retries, only=only, confine=confine)
    if not wait:
        left = 0.0
    paid = 0.0
    while True:
        data = _call(prompt, model, effort, cwd, resume=resume, **kw)
        if data.get("error_kind") == "usage_limit":
            secs = reset_wait(str(data.get("result")))
            if secs is not None and secs + 60 <= left:
                secs += 60  # past the minute it names
                print(f"[claude] usage limit: {str(data.get('result'))[:160]}; waiting {secs / 60:.0f} min "
                      "for the reset", file=sys.stderr, flush=True)
                left -= secs
                if pause(secs):
                    spent = data.get("total_cost_usd") or 0.0
                    paid += spent
                    if kw["budget_usd"] is not None:
                        kw["budget_usd"] -= spent
                    if data.get("session_id") and (data.get("num_turns") or 0) > 1:
                        resume = data["session_id"]
                        prompt = ("A usage limit interrupted you; it has reset. Continue where you stopped and "
                                  "finish the task, replying as first asked.")
                    continue
        if paid:
            data["total_cost_usd"] = paid + (data.get("total_cost_usd") or 0.0)
        return data


def _call(prompt: str, model: str, effort: str, cwd: Path, *, perm: str, tools: list[str],
          resume: str | None, timeout: int, settings: dict | None, schema: dict | None,
          budget_usd: float | None, extra_args: list[str] | None, retries: int | None,
          only: list[str] | None, confine: Path | None) -> dict:
    prompt = prompt.replace("\x00", "\ufffd")  # no argument can hold a NUL
    piped = prompt if len(prompt.encode("utf-8", "replace")) > ARG_PROMPT_MAX else None
    cmd = [claude_bin(), "-p", *([prompt] if piped is None else []), "--model", model, "--effort", effort,
           "--output-format", "json", "--permission-mode", perm]
    hermetic = os.environ.get("ORCHESTRATE_HERMETIC") == "1"
    if hermetic:
        settings = {**(settings or {}), "disableAllHooks": True}
    if only is not None:
        # these roles judge text workers wrote, which can tell them to open any file the user can.
        # Reads stay in the cwd and the --add-dir dirs. Not --restricted: that drops the user's
        # settings, where a provider or login helper may be set.
        perms = {**(settings or {}).get("permissions", {}), "blockReadsOutsideWorkingDirectories": True}
        settings = {**(settings or {}), "permissions": perms}
    if settings:
        cmd += ["--settings", json.dumps(settings)]
    if only is not None:
        cmd += ["--tools", ",".join(only)]  # "" is no built-in tools; --json-schema still works
        # the cwd is a tree workers wrote: its .claude/settings.json could run hooks in this call
        cmd += ["--setting-sources", "user"]
        rules = project_rules(cwd) if only else ""  # that flag also drops the project's CLAUDE.md
        if rules:
            cmd += ["--append-system-prompt", rules]
    if hermetic or only is not None:  # --tools doesn't cover MCP tools
        cmd += ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}})]
    if tools:
        cmd += ["--allowedTools", *tools]
    if resume:
        cmd += ["--resume", resume]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if budget_usd is not None:
        cmd += ["--max-budget-usd", f"{max(budget_usd, 0.01):.2f}"]
    cmd += extra_args or []
    env = child_env(extra_args)
    if confine:
        cmd = jail.wrap(cmd, cwd, confine)
    tries = int(os.environ.get("ORCHESTRATE_API_RETRIES", "3")) if retries is None else retries
    backoff = float(os.environ.get("ORCHESTRATE_API_BACKOFF_S", "20"))
    data: dict = {}
    kind: str | None = "exec"
    paid = 0.0  # by attempts that were retried
    t0 = time.time()
    for attempt in range(max(tries, 0) + 1):
        t0 = time.time()
        with login_kept_fresh(bool(confine)):
            out, err, timed_out = _run(cmd, cwd, env, piped, timeout)
        if timed_out:
            res = {"is_error": True, "error_kind": "timeout", "timeout": True,
                   "result": f"claude timed out after {timeout}s", "structured": None,
                   "metrics": {"wall_s": round(time.time() - t0, 1)}}
            with contextlib.suppress(ValueError, IndexError):  # nothing to parse: it had to be killed
                last = _parse(out)
                _cost_of_this_call(last, resume)
                if isinstance(last.get("total_cost_usd"), (int, float)):
                    res["total_cost_usd"] = last["total_cost_usd"]
            if paid:
                res["total_cost_usd"] = paid + (res.get("total_cost_usd") or 0.0)
            return res
        try:
            data = _parse(out)
        except (ValueError, IndexError):
            data = {"is_error": True, "result": (out + err)[-2000:], "subtype": "unparseable"}
            kind = ("usage_limit" if USAGE_RE.match(data["result"]) else
                    "overloaded" if OVERLOAD_RE.search(data["result"]) else
                    "rate_limit" if RATE_RE.search(data["result"]) else "unparseable")
        else:
            kind = _kind(data)
        _cost_of_this_call(data, resume)
        if kind in ("overloaded", "rate_limit") and attempt < tries:
            paid += data.get("total_cost_usd") or 0.0
            time.sleep(backoff * (2 ** attempt))
            continue
        break
    data["error_kind"] = kind
    if kind:
        data["is_error"] = True
    usage = data.get("modelUsage") or {}
    data["model_id"] = next((k for k in usage if model in k), next(iter(usage), None))
    data["metrics"] = _metrics(data, time.time() - t0)
    if paid:
        data["total_cost_usd"] = paid + (data.get("total_cost_usd") or 0.0)
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
