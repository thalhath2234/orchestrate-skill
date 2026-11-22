#!/usr/bin/env python3
"""dashboard.py: live, read-only, IDE-style web UI for orchestrate jobs.

  python3 dashboard.py [JOB] [--port 8765] [--host 127.0.0.1] [--allow-host NAME] [--open]

Serves http://127.0.0.1:8765/?job=<JOB>. It only READS job state from
$ORCHESTRATE_DIR (default ~/.local/share/orchestrate) and never touches a job,
so it is safe to run while a job is in progress, from any copy of the skill.
The page works before a job exists: it shows the plan from plans/<JOB>.json when
that file exists ("planned"), else "waiting for job <id> to start", and keeps
polling, so its link can be handed out right after planning.

With no request and no running job in its state dir for 12 hours, the dashboard exits
(a spawned one must not outlive its use by days): --idle-hours H or
ORCHESTRATE_DASHBOARD_IDLE_HOURS=H changes that, 0 turns it off. An open page polls,
so it keeps the dashboard alive.

A loopback-bound dashboard answers loopback Host names only (DNS-rebinding guard).
To reach it through a reverse proxy that keeps the browser's Host (tailscale serve,
an SSH or nginx proxy), allow that name: --allow-host NAME (repeatable) or
ORCHESTRATE_DASHBOARD_HOSTS=name1,name2 (read at start, so dashboards that
`orchestrate.py` spawns inherit it). A name starting with "." allows its subdomains.

Endpoints (all GET, JSON unless noted):
  /                              the page
  /api/ping                      {"orchestrate_dashboard": 1, "jobs_dir": ..., "pid": ..., "fingerprint": ...}
                                 fingerprint: router.code_fingerprint() of the code this process runs;
                                 orchestrate.py replaces a dashboard whose fingerprint differs, and an
                                 open page reloads itself when the server it talks to changes it
  /api/jobs                      job list
  /api/job/<jid>                 full payload (404 when the job does not exist; ETag / 304)
  /api/job/<jid>/log/<name>      a task log, _integration, _devserver, ... (text, tail ?n=, ?n=all)
  /api/job/<jid>/brief/<tid>     a task's brief (text)
  /api/job/<jid>/out             the detached run's output (text, tail ?n=, ?n=all)
  /api/job/<jid>/report          report.md (text)
  /api/job/<jid>/state           job.json as written (text)
  /api/job/<jid>/shot?f=<rel>    a screenshot (image/png), only inside screenshots_root
  /api/job/<jid>/file?path=<p>   a repo file a review finding points at (text, integration worktree)
Text tails carry X-Log-Size (the file's size) and X-Log-Start (where the tail starts,
at a line boundary), so the page can say what it is not showing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import router  # noqa: E402

# Which code this process runs (this file, the modules it imports, and where they are), read once
# at start: a running server keeps the code it started with.
FINGERPRINT = router.code_fingerprint()

JOBS = router.DIR / "jobs"
PLANS = router.DIR / "plans"
SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}$")
TASK_FIELDS = ("id", "title", "goal", "type", "tier", "owns", "reads", "depends_on", "done", "verify",
               "gate", "notes", "interfaces", "ui")
STATE_FIELDS = ("status", "started", "ended", "steps", "report", "cost_usd", "coord_cost_usd", "error", "slot",
                "result", "manager_retries", "added_by", "files", "final_message", "split_into", "skip_reason",
                "run", "worktree", "branch", "claimed", "merge_verify")
MAX_TEXT = 4_000_000
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
HOST_RE = re.compile(r"^(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?))(?::(\d{1,5}))?$")
HOSTS_ENV = "ORCHESTRATE_DASHBOARD_HOSTS"
IDLE_ENV = "ORCHESTRATE_DASHBOARD_IDLE_HOURS"
IDLE_HOURS = 12.0


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        return default


def _read_text(p: Path, cap: int = MAX_TEXT) -> str | None:
    try:
        with open(p, "rb") as fh:
            return fh.read(cap).decode("utf-8", "replace")
    except OSError:
        return None


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _status(s: dict) -> tuple[str, bool]:
    """The job's status, with "stopped" for a headless run whose process is gone.
    Orca mode has no long-lived process (it drops "pid"), so it is never marked stopped."""
    status = s.get("status") or "?"
    if not isinstance(status, str):
        status = "?"
    alive = _alive(s.get("pid"))
    if status == "running" and s.get("pid") is not None and not alive and s.get("mode") != "orca":
        status = "stopped"
    return status, alive or (status == "running" and s.get("mode") == "orca")


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x) -> list:
    return x if isinstance(x, list) else []


# ---- normalising what the page reads: plans and states are hand-edited, old, or partial.
# Every list the page iterates is a list here (a lone string becomes [string]), every object
# it reads fields of is a dict, so one odd value cannot blank a view.
def _as_list(x) -> list:
    if isinstance(x, list):
        return [v for v in x if v is not None]
    if isinstance(x, str):
        return [x] if x.strip() else []
    return []


def _strs(x) -> list[str]:
    return [v for v in _as_list(x) if isinstance(v, str)]


def _dicts(x) -> list[dict]:
    return [v for v in _as_list(x) if isinstance(v, dict)]


def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _opt_num(x):
    return None if x is None else _num(x, None)


def _finding(f: dict) -> dict:
    f = dict(f)
    for k in ("file", "issue", "suggestion", "severity", "confidence"):
        if f.get(k) is not None and not isinstance(f[k], str):
            f[k] = json.dumps(f[k]) if isinstance(f[k], (dict, list)) else str(f[k])
    return f


def _verdict(v) -> dict | None:
    if not isinstance(v, dict):
        return None
    return {**v, "findings": [_finding(f) for f in _dicts(v.get("findings"))],
            "acceptance": _dicts(v.get("acceptance")), "edge_cases": _dicts(v.get("edge_cases")),
            "raise_bar": _dicts(v.get("raise_bar")), "pages": _dicts(v.get("pages"))}


def _advice(a: dict) -> dict:
    return {**a, "findings": [_finding(f) for f in _dicts(a.get("findings"))], "raise_bar": _dicts(a.get("raise_bar"))}


def _round(r: dict, i: int) -> dict:
    r = dict(r)
    if not isinstance(r.get("round"), int):
        r["round"] = i + 1
    r["verdict"] = _verdict(r.get("verdict"))
    if isinstance(r.get("ui"), dict):
        r["ui"] = {**r["ui"], "verdict": _verdict(r["ui"].get("verdict")), "shots": _list(r["ui"].get("shots"))}
    else:
        r.pop("ui", None)
    if isinstance(r.get("advisor"), dict):
        r["advisor"] = _advice(r["advisor"])
    else:
        r.pop("advisor", None)
    if isinstance(r.get("fix"), dict):
        r["fix"] = {**r["fix"], "steps": _dicts(r["fix"].get("steps"))}
    else:
        r.pop("fix", None)
    r["bar_raised"] = _as_list(r.get("bar_raised"))
    if r.get("final_verify_tail") is not None and not isinstance(r["final_verify_tail"], str):
        r["final_verify_tail"] = str(r["final_verify_tail"])
    return r


def _manager_entry(m: dict) -> dict:
    m = dict(m)
    m["actions"] = _dicts(m.get("actions"))
    m["events"] = _strs(m.get("events"))
    m["rejected"] = [str(x) for x in _as_list(m.get("rejected"))] if m.get("rejected") else None
    st = m.get("second_try")
    if isinstance(st, dict):
        m["second_try"] = {**st, "actions": _dicts(st.get("actions")),
                           "rejected": [str(x) for x in _as_list(st.get("rejected"))] if st.get("rejected") else None}
    else:
        m.pop("second_try", None)
    if m.get("task") is not None and not isinstance(m["task"], str):
        m["task"] = _dict(m["task"]).get("id") if isinstance(_dict(m["task"]).get("id"), str) else None
    m["t"] = _opt_num(m.get("t"))
    return m


def _task_plan(t: dict) -> dict:
    out = {k: t.get(k) for k in TASK_FIELDS}
    for k in ("owns", "reads", "depends_on"):
        out[k] = _strs(t.get(k))
    for k in ("done", "verify", "notes"):
        out[k] = _as_list(t.get(k))
    out["ui"] = _dicts(t.get("ui"))
    for k in ("title", "goal", "type", "tier", "gate"):
        if out[k] is not None and not isinstance(out[k], str):
            out[k] = str(out[k])
    return out


def _task_state(st: dict) -> dict:
    out = {k: st.get(k) for k in STATE_FIELDS}
    out["steps"] = _dicts(st.get("steps"))
    out["files"] = _strs(st.get("files"))
    out["claimed"] = _strs(st.get("claimed"))
    out["split_into"] = _strs(st.get("split_into"))
    rep = st.get("report")
    if isinstance(rep, str):
        rep = {"summary": rep}
    if isinstance(rep, dict):
        out["report"] = {**rep, "open_issues": _as_list(rep.get("open_issues"))}
    else:
        out["report"] = None
    for k in ("started", "ended"):
        out[k] = _opt_num(st.get(k))
    for k in ("cost_usd", "coord_cost_usd"):
        out[k] = _num(st.get(k)) if st.get(k) is not None else None
    if out["status"] is not None and not isinstance(out["status"], str):
        out["status"] = str(out["status"])
    return out


def jobs_dir() -> str:
    return os.path.abspath(str(JOBS))


def list_jobs() -> list[dict]:
    out = []
    try:
        files = list(JOBS.glob("*/job.json"))
    except OSError:
        files = []
    for f in files:
        try:
            s = _read_json(f, {})
            if not isinstance(s, dict) or not s:
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                mtime = 0
            status, _ = _status(s)
            tasks = _dict(s.get("tasks"))
            out.append({"job": f.parent.name, "status": status, "cost_usd": round(_num(s.get("cost_usd")), 2),
                        "done": sum(_dict(t).get("status") == "done" for t in tasks.values()),
                        "total": sum(_dict(t).get("status") not in ("split",) for t in tasks.values()),
                        "mtime": mtime})
        except Exception:  # noqa: BLE001  (one odd job.json must not hide every other job)
            out.append({"job": f.parent.name, "status": "?", "cost_usd": 0, "done": 0, "total": 0, "mtime": 0})
    return sorted(out, key=lambda j: -j["mtime"])


def _ui_rounds(rounds: list) -> tuple[list[dict], dict[str, list[str]]]:
    """Each review round's UI review, normalised, plus task id -> shot ids."""
    ui_rounds, by_task = [], {}
    for i, r in enumerate(rounds):
        r = _dict(r)
        ui = r.get("ui")
        if not isinstance(ui, dict):
            continue
        shots = []
        for sh in _list(ui.get("shots")):
            if not isinstance(sh, dict) or not isinstance(sh.get("id"), str):
                continue
            sh = {**sh, "tasks": _strs(sh.get("tasks"))}
            for k in ("console", "page_errors", "failed_requests", "step_errors"):
                sh[k] = [str(x) for x in _as_list(sh.get(k))]
            for k in ("name", "path", "viewport", "step", "file", "title"):
                if sh.get(k) is not None and not isinstance(sh[k], str):
                    sh[k] = str(sh[k])
            if not isinstance(sh.get("status"), int):
                sh["status"] = None
            shots.append(sh)
            for t in sh["tasks"]:
                by_task.setdefault(t, []).append(sh["id"])
        ui_rounds.append({**ui, "round": r.get("round", i + 1), "shots": shots,
                          "verdict": _verdict(ui.get("verdict"))})
    return ui_rounds, by_task


def cli_prefix() -> str:
    """How to run orchestrate.py from a shell, for the page's copyable commands."""
    env = os.environ.get("ORCHESTRATE_DIR")
    pre = f"ORCHESTRATE_DIR={shlex.quote(env)} " if env else ""
    return pre + f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'orchestrate.py'))}"


def job_payload(jid: str) -> dict | None:
    jd = JOBS / jid
    s, p = _read_json(jd / "job.json"), _read_json(jd / "plan.json")
    if not isinstance(s, dict):
        s = None
    if not isinstance(p, dict):
        p = None
    plan_only = False
    if s is None and p is None:
        # Planned but not run yet: the plan the agent wrote (SKILL.md: plans/<job>.json).
        p = _read_json(PLANS / f"{jid}.json")
        if not isinstance(p, dict):
            return None
        plan_only = True
    s, p = s or {}, p or {}
    status, alive = _status(s) if s else ("planned", False)
    st_tasks = _dict(s.get("tasks"))
    tasks, seen = [], set()
    for t in _list(p.get("tasks")):
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or t["id"] in seen:
            continue
        seen.add(t["id"])
        tasks.append({**_task_plan(t), "state": _task_state(_dict(st_tasks.get(t["id"])))})
    extra = []
    for tid, st in st_tasks.items():
        if tid in seen:
            continue
        st = _dict(st)
        extra.append({**_task_plan({"id": tid, "title": st.get("title"), "type": st.get("type"), "tier": st.get("tier")}),
                      "state": _task_state(st)})
    m = _dict(s.get("manager"))
    try:
        proposals = len(router.pending())
    except Exception:  # noqa: BLE001
        proposals = 0
    rounds = [_round(r, i) for i, r in enumerate(_list(_dict(s.get("review")).get("rounds"))) if isinstance(r, dict)]
    ui_rounds, shots_by_task = _ui_rounds(rounds)
    logs = []
    try:
        for f in sorted((jd / "logs").glob("*.log")):
            st_ = f.stat()
            logs.append({"name": f.stem, "size": st_.st_size, "mtime": st_.st_mtime})
    except OSError:
        pass
    out_f = JOBS / f"{jid}.out"
    try:
        updated = (jd / "job.json").stat().st_mtime
    except OSError:
        updated = None
    advice = {k: [_advice(a) for a in _dicts(v)] for k, v in _dict(s.get("advice")).items() if isinstance(k, str)}
    ui_cfg = p.get("ui") if isinstance(p.get("ui"), dict) else None
    if ui_cfg is not None:
        ui_cfg = {**ui_cfg, "pages": _dicts(ui_cfg.get("pages")), "viewports": _strs(ui_cfg.get("viewports")) or None}
    cap = _dict(s.get("capacity")) or None
    if cap is not None:
        cap = {**cap, "limits": _dict(cap.get("limits"))}
    return {
        "job": jid, "status": status, "alive": alive, "now": time.time(), "updated": updated, "plan_only": plan_only,
        "mode": s.get("mode"), "goal": p.get("goal"), "definition_of_done": p.get("definition_of_done"),
        "acceptance": _as_list(p.get("acceptance")), "decisions": _as_list(p.get("decisions")),
        "non_goals": _as_list(p.get("non_goals")), "verify": _as_list(p.get("verify")),
        "quality_bar": _as_list(p.get("quality_bar")), "ui_config": ui_cfg,
        "repo": s.get("repo") or p.get("repo"), "branch": s.get("branch"), "base": s.get("base") or p.get("base"),
        "base_sha": str(s.get("base_sha") or "")[:10], "created": _opt_num(s.get("created")),
        "finished": _opt_num(s.get("finished")),
        "cost_usd": _num(s.get("cost_usd")), "budget_usd": _opt_num(p.get("budget_usd")), "capacity": cap,
        "max_parallel": p.get("max_parallel"), "review_rounds_max": p.get("review_rounds"),
        "message": s.get("message") if isinstance(s.get("message"), (str, type(None))) else json.dumps(s.get("message")),
        "human_question": s.get("human_question"),
        "approved": _strs(s.get("approved")), "warnings": _as_list(s.get("warnings")),
        "manager": {"enabled": _dict(p.get("manager")).get("enabled", True),
                    "interventions": m.get("interventions", 0),
                    "log": [_manager_entry(e) for e in _dicts(m.get("log"))],
                    "busy_s": m.get("busy_s")},
        "review": rounds, "review_rounds_used": sum(1 for r in rounds if not r.get("superseded")),
        "accepted_by_manager": _dict(s.get("review")).get("accepted_by_manager"),
        "owed_fix": _dict(s.get("review")).get("owed_fix"),
        "advice": advice, "advice_plan_ok": s.get("advice_plan_ok"),
        "skipped_checks": _skipped_checks(s, p), "baseline_skipped": bool(s.get("baseline_skipped")),
        "decisions_added": _strs(s.get("decisions_added")), "assumptions": _strs(p.get("assumptions")),
        "bar": _as_list(s.get("bar")), "cleanup": s.get("cleanup") if isinstance(s.get("cleanup"), dict) else None,
        "pending_events": len(_list(s.get("pending_events"))),
        "metrics": _metrics(s), "calls": _dicts(s.get("calls")),
        "tasks": tasks, "extra": extra, "pending_proposals": proposals,
        "ui_rounds": ui_rounds, "shots_by_task": shots_by_task,
        "screenshots_root": s.get("screenshots_root"), "dashboard": s.get("dashboard"),
        "logs": logs, "has_out": out_f.exists(), "has_report": (jd / "report.md").exists(),
        "has_state": (jd / "job.json").exists(),
        "integration_wt": s.get("integration_wt"), "cli": cli_prefix(),
        # report.md is fetched from /report when its tab is open, not re-sent on every poll
        "plan_raw": _read_text(jd / "plan.json") if not plan_only else _read_text(PLANS / f"{jid}.json"),
    }


def _skipped_checks(state: dict, plan: dict) -> list[str]:
    """The checks report.md lists under "Checks skipped or overridden": one source for both."""
    try:
        import orchestrate
        return [str(x) for x in orchestrate.skipped_checks(state, plan)]
    except Exception:  # noqa: BLE001  (old or partial state: the dashboard must still render)
        return []


def _metrics(state: dict) -> dict:
    try:
        import orchestrate
        m = _dict(orchestrate.job_metrics(state))
    except Exception:  # noqa: BLE001  (old or partial state: the dashboard must still render)
        return {}
    if isinstance(m.get("cost_by_role"), dict):  # str keys only: a null role must not give mixed key types
        m["cost_by_role"] = {str(r): v for r, v in m["cost_by_role"].items()}
    return m


def tail_info(path: Path, n: int | None) -> tuple[bytes, int, int]:
    """(the last n bytes from a line boundary, where they start, the file's size). n=None: all."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if n is None or n >= size:
                fh.seek(0)
                return fh.read(size), 0, size
            start = size - n
            fh.seek(start - 1)
            data = fh.read(n + 1)
            if data[:1] != b"\n":  # cut mid-line: start at the next full line, when there is one
                nl = data.find(b"\n")
                if 0 <= nl < len(data) - 1:
                    start += nl
                    return data[nl + 1:], start, size
            return data[1:], start, size
    except OSError:
        return b"", 0, 0


def tail(path: Path, n: int) -> str:
    return tail_info(path, n)[0].decode("utf-8", "replace")


def _inside(root: str, rel: str) -> str | None:
    """realpath(root/rel) when it is a file strictly inside realpath(root), else None."""
    try:
        if not root or not rel or "\0" in rel:
            return None
        rr = os.path.realpath(root)
        fp = os.path.realpath(os.path.join(rr, rel))
        if not fp.startswith(rr + os.sep) or not os.path.isfile(fp):
            return None
        return fp
    except (OSError, ValueError, TypeError):
        return None


def shot_file(jid: str, rel: str) -> str | None:
    """A screenshot's real path: must end in .png and resolve inside job.state["screenshots_root"]."""
    s = _read_json(JOBS / jid / "job.json")
    root = _dict(s).get("screenshots_root")
    if not isinstance(root, str) or not isinstance(rel, str) or not rel.endswith(".png"):
        return None
    fp = _inside(root, rel)
    return fp if fp and fp.endswith(".png") else None


def _rel(p: str) -> str:
    p = p.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def finding_file(jid: str, rel: str) -> str | None:
    """A file in the integration worktree, only when a review finding or a task's merged files name it."""
    s = _dict(_read_json(JOBS / jid / "job.json"))
    wt = s.get("integration_wt")
    if not isinstance(wt, str) or not rel or rel.startswith("/") or ".git" in Path(rel).parts:
        return None
    named = set()
    for r in _list(_dict(s.get("review")).get("rounds")):
        r = _dict(r)
        for f in _list(_dict(r.get("verdict")).get("findings")) + _list(_dict(_dict(r.get("ui")).get("verdict")).get("findings")):
            if isinstance(_dict(f).get("file"), str):
                named.add(_rel(f["file"].split(":")[0]))
    for a in [x for v in _dict(s.get("advice")).values() for x in _list(v)]:
        for f in _list(_dict(a).get("findings")):
            if isinstance(_dict(f).get("file"), str):
                named.add(_rel(f["file"].split(":")[0]))
    for st in _dict(s.get("tasks")).values():
        named.update(_rel(x) for x in _list(_dict(st).get("files")) if isinstance(x, str))
    if _rel(rel) not in named:
        return None
    fp = _inside(wt, rel)
    if fp and ".git" in Path(os.path.relpath(fp, os.path.realpath(wt))).parts:
        return None  # a symlink in the worktree that resolves into .git
    return fp


def env_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()}


def _norm_host(h: str) -> str:
    """An allow-list entry as a bare lower-case name ("name:port" and "[v6]" are accepted)."""
    h = h.strip().lower()
    m = HOST_RE.match(h.lstrip("."))
    if m:
        return ("." if h.startswith(".") else "") + (m.group(1) or m.group(2))
    return h


def host_allowed(host: str, allowed=frozenset()) -> bool:
    """True for a well-formed Host header naming loopback or an allowed name (".x" = any subdomain of x)."""
    m = HOST_RE.match(host or "")
    if not m or (m.group(3) is not None and not 0 < int(m.group(3)) < 65536):
        return False
    name = (m.group(1) or m.group(2)).lower()
    if m.group(1) is not None and name != "::1":
        return False
    if name in LOOPBACK or name in allowed:
        return True
    return any(a.startswith(".") and name.endswith(a) for a in allowed)


class Handler(BaseHTTPRequestHandler):
    default_job = None
    loopback_only = True
    allowed_hosts = frozenset(_norm_host(h) for h in env_hosts())
    server_version = "orchestrate-dashboard"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002  (quiet)
        pass

    def _headers(self, code: int, ctype: str, length: int | None, cache="no-store", extra=None):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype + ("" if ctype.startswith("image/") else "; charset=utf-8"))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if ctype == "text/html":
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                             "form-action 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _send(self, code: int, body, ctype="application/json", cache="no-store", head=False, extra=None):
        if isinstance(body, bytes):
            data = body
        elif ctype == "application/json":
            data = json.dumps(body).encode()
        else:
            data = str(body).encode("utf-8", "replace")
        self._headers(code, ctype, len(data), cache, extra)
        if not head:
            self.wfile.write(data)

    def _host_ok(self) -> bool:
        """Refuse DNS-rebinding requests: a loopback-bound dashboard answers loopback Host names
        (and the names allowed with --allow-host / $ORCHESTRATE_DASHBOARD_HOSTS) only."""
        if not self.loopback_only:
            return True
        hosts = self.headers.get_all("Host") or []
        if len(hosts) > 1:
            return False
        host = (hosts[0] if hosts else "").strip()
        return not host or host_allowed(host, self.allowed_hosts)

    def do_HEAD(self):  # noqa: N802
        self.do_GET(head=True)

    def do_GET(self, head=False):  # noqa: N802
        try:
            self._route(head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001  (answer, never drop the connection)
            try:
                self._send(500, {"error": f"dashboard error: {type(e).__name__}: {e}"[:500]}, head=head)
            except OSError:
                pass

    def _text_tail(self, path: Path, n: int | None, head: bool):
        if n is not None:
            data, start, size = tail_info(path, n)
            return self._send(200, data, "text/plain", head=head, extra={"X-Log-Size": size, "X-Log-Start": start})
        try:  # ?n=all: the whole file, streamed (logs can be hundreds of MB)
            fh = open(path, "rb")
        except OSError:
            return self._send(200, b"", "text/plain", head=head, extra={"X-Log-Size": 0, "X-Log-Start": 0})
        with fh:
            size = os.fstat(fh.fileno()).st_size
            self._headers(200, "text/plain", size, extra={"X-Log-Size": size, "X-Log-Start": 0})
            left = 0 if head else size
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        return None

    def _route(self, head=False):  # noqa: C901
        self.server.last_request = time.time()
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host",
                                    "hint": "this dashboard answers loopback names only; to reach it through a "
                                            "proxy, restart it with --allow-host NAME or set "
                                            f"{HOSTS_ENV}=NAME"}, head=head)
        u = urlparse(self.path)
        parts = [x for x in u.path.split("/") if x]
        q = parse_qs(u.query)

        def n_arg(default=200000):
            v = (q.get("n") or [str(default)])[0]
            if v == "all":
                return None
            try:
                return max(1, min(int(v), MAX_TEXT))
            except ValueError:
                return default

        if not parts:
            dj = json.dumps(self.default_job).replace("<", "\\u003c")
            page = PAGE.replace("__DEFAULT_JOB__", dj).replace("__FINGERPRINT__", json.dumps(FINGERPRINT))
            return self._send(200, page, "text/html", head=head)
        if parts == ["api", "ping"]:
            return self._send(200, {"orchestrate_dashboard": 1, "jobs_dir": jobs_dir(), "pid": os.getpid(),
                                    "fingerprint": FINGERPRINT}, head=head)
        if parts == ["api", "jobs"]:
            return self._send(200, list_jobs(), head=head)
        if len(parts) >= 3 and parts[:2] == ["api", "job"] and SAFE.match(parts[2]):
            jid = parts[2]
            jd = JOBS / jid
            if len(parts) == 3:
                data = job_payload(jid)
                if not data:
                    return self._send(404, {"error": f"no job {jid}"}, head=head)
                # no sort_keys: the payload is built in a fixed order, and odd state can give a dict
                # mixed key types (None and str), which sorting can't compare
                etag = '"' + hashlib.sha1(json.dumps({**data, "now": None}, default=str).encode()).hexdigest() + '"'
                if etag in [x.strip() for x in (self.headers.get("If-None-Match") or "").split(",")]:
                    self._headers(304, "", None, extra={"ETag": etag})
                    return None
                return self._send(200, data, head=head, extra={"ETag": etag})
            if not jd.is_dir():
                return self._send(404, {"error": f"no job {jid}"}, head=head)
            if len(parts) == 5 and parts[3] in ("log", "brief") and SAFE.match(parts[4]):
                sub = "logs" if parts[3] == "log" else "briefs"
                f = _inside(str(jd / sub), f"{parts[4]}.{'log' if parts[3] == 'log' else 'md'}")
                return self._text_tail(Path(f) if f else jd / sub / "-missing-", n_arg(), head)
            if len(parts) == 4 and parts[3] == "out":
                f = _inside(str(JOBS), f"{jid}.out")
                return self._text_tail(Path(f) if f else jd / "-missing-", n_arg(), head)
            if len(parts) == 4 and parts[3] in ("report", "state"):
                f = _inside(str(jd), "report.md" if parts[3] == "report" else "job.json")
                return self._send(200, (_read_text(Path(f)) if f else None) or "", "text/plain", head=head)
            if len(parts) == 4 and parts[3] == "shot":
                fp = shot_file(jid, (q.get("f") or [""])[0])
                if not fp:
                    return self._send(404, {"error": "no such screenshot"}, head=head)
                try:
                    with open(fp, "rb") as fh:
                        data = fh.read()
                except OSError:
                    return self._send(404, {"error": "no such screenshot"}, head=head)
                return self._send(200, data, "image/png", "private, max-age=300", head=head)
            if len(parts) == 4 and parts[3] == "file":
                fp = finding_file(jid, (q.get("path") or [""])[0])
                if not fp:
                    return self._send(404, {"error": "not a file a finding names"}, head=head)
                return self._send(200, _read_text(Path(fp), 2_000_000) or "", "text/plain", head=head)
        return self._send(404, {"error": "not found"}, head=head)


def make_server(host: str = "127.0.0.1", port: int = 8765, default_job: str | None = None,
                allow_hosts=()) -> ThreadingHTTPServer:
    allowed = frozenset(_norm_host(h) for h in set(allow_hosts) | env_hosts() if h and h.strip())
    handler = type("BoundHandler", (Handler,), {"default_job": default_job, "allowed_hosts": allowed,
                                                 "loopback_only": host in LOOPBACK})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    srv.last_request = time.time()
    return srv


def idle_hours() -> float:
    """$ORCHESTRATE_DASHBOARD_IDLE_HOURS, else 12; 0 (or less) means never exit when idle."""
    try:
        h = float(os.environ.get(IDLE_ENV, IDLE_HOURS))
    except ValueError:
        return IDLE_HOURS
    return h if h == h and h != float("inf") else IDLE_HOURS


def jobs_active(window_s: float) -> bool:
    """A job in this state dir is running: its process is alive, or (Orca mode, which has no
    long-lived process) it is "running" and its job.json changed within the window."""
    try:
        files = list(JOBS.glob("*/job.json"))
    except OSError:
        return False
    for f in files:
        try:
            s = _read_json(f, {})
            if not isinstance(s, dict):
                continue
            status, alive = _status(s)
            if s.get("mode") == "orca":
                if status == "running" and time.time() - f.stat().st_mtime < window_s:
                    return True
            elif status == "running" and alive:
                return True
        except Exception:  # noqa: BLE001  (an odd job.json counts as not running)
            continue
    return False


def watch_idle(srv: ThreadingHTTPServer, hours: float):
    """Shut the server down once it has had no request and no running job for `hours`."""
    if hours <= 0:
        return None
    limit = hours * 3600

    def run():
        step = min(60.0, max(0.2, limit / 4))
        while True:
            time.sleep(step)
            if time.time() - srv.last_request < limit:
                continue
            try:  # one odd job file must not end the thread (the server would then never exit)
                idle = not jobs_active(limit)
            except Exception:  # noqa: BLE001  (unreadable state counts as no running job, as in jobs_active)
                idle = True
            if idle:
                print(f"orchestrate dashboard: no request and no running job for {hours:g}h; exiting", flush=True)
                srv.shutdown()
                return
    t = threading.Thread(target=run, name="idle-exit", daemon=True)
    t.start()
    return t


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>orchestrate</title>
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%232f6fec'/%3E%3Cg stroke='white' stroke-width='2.4' fill='none' stroke-linecap='round'%3E%3Cpath d='M9 10h6m-6 12h6m2-6h6M15 10l2 6-2 6'/%3E%3C/g%3E%3C/svg%3E">
<style>
:root{
  --font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --side-w:280px;--panel-h:240px;
  --bg:#1f2126;--bg-side:#191b1f;--bg-act:#141518;--bg-title:#141518;--bg-panel:#191b1f;--bg-elev:#24272d;--bg-card:#22252a;
  --bg-hover:#2a2e35;--bg-active:#2c3442;--bg-input:#121316;--bg-code:#16181c;--bg-tab:#191b1f;--bg-tab-on:#1f2126;
  --line:#2c2f36;--line-soft:#25282e;--text:#d8dbe1;--mute:#a4aab4;--dim:#8f95a0;--accent:#4c8dff;--accent-soft:rgba(76,141,255,.16);
  --focus:#4c8dff;--sel:rgba(76,141,255,.22);--shadow:0 12px 40px rgba(0,0,0,.45);
  --ok:#3fb950;--err:#f85149;--warn:#d29922;--info:#58a6ff;--run:#4c8dff;--gate:#e3b341;--split:#a371f7;--skip:#8b949e;
  --sb-bg:#2a2d33;--sb-fg:#e6e8eb;
  --mark:rgba(227,179,65,.35);--mark-cur:rgba(227,179,65,.75);
}
:root[data-theme="light"]{
  --bg:#ffffff;--bg-side:#f7f8fa;--bg-act:#eceef2;--bg-title:#eceef2;--bg-panel:#f7f8fa;--bg-elev:#ffffff;--bg-card:#ffffff;
  --bg-hover:#eaedf1;--bg-active:#dce6f8;--bg-input:#ffffff;--bg-code:#f6f8fa;--bg-tab:#eef0f3;--bg-tab-on:#ffffff;
  --line:#dadde3;--line-soft:#e8eaee;--text:#1f2328;--mute:#4f5964;--dim:#5f6873;--accent:#2563eb;--accent-soft:rgba(37,99,235,.10);
  --focus:#2563eb;--sel:rgba(37,99,235,.14);--shadow:0 12px 40px rgba(20,30,50,.18);
  --ok:#1a7f37;--err:#cf222e;--warn:#9a6700;--info:#0969da;--run:#2563eb;--gate:#9a6700;--split:#8250df;--skip:#6e7781;
  --sb-bg:#e3e6eb;--sb-fg:#1f2328;--mark:rgba(212,167,44,.35);--mark-cur:rgba(212,167,44,.8);
}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){
  --bg:#ffffff;--bg-side:#f7f8fa;--bg-act:#eceef2;--bg-title:#eceef2;--bg-panel:#f7f8fa;--bg-elev:#ffffff;--bg-card:#ffffff;
  --bg-hover:#eaedf1;--bg-active:#dce6f8;--bg-input:#ffffff;--bg-code:#f6f8fa;--bg-tab:#eef0f3;--bg-tab-on:#ffffff;
  --line:#dadde3;--line-soft:#e8eaee;--text:#1f2328;--mute:#4f5964;--dim:#5f6873;--accent:#2563eb;--accent-soft:rgba(37,99,235,.10);
  --focus:#2563eb;--sel:rgba(37,99,235,.14);--shadow:0 12px 40px rgba(20,30,50,.18);
  --ok:#1a7f37;--err:#cf222e;--warn:#9a6700;--info:#0969da;--run:#2563eb;--gate:#9a6700;--split:#8250df;--skip:#6e7781;
  --sb-bg:#e3e6eb;--sb-fg:#1f2328;--mark:rgba(212,167,44,.35);--mark-cur:rgba(212,167,44,.8);
}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:13px/1.5 var(--font);-webkit-font-smoothing:antialiased;overflow:hidden}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
::selection{background:var(--sel)}
:focus-visible{outline:1px solid var(--focus);outline-offset:-1px}
code,.mono,kbd,pre{font-family:var(--mono);font-size:12px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
svg{flex:none}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:color-mix(in srgb,var(--mute) 28%,transparent);border-radius:10px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background-color:color-mix(in srgb,var(--mute) 45%,transparent)}::-webkit-scrollbar-corner{background:transparent}
#app{display:grid;grid-template-rows:38px minmax(0,1fr) 24px;grid-template-columns:minmax(0,1fr);height:100vh;height:100dvh}
/* title bar */
.titlebar{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,520px) minmax(0,1fr);align-items:center;gap:12px;padding:0 8px 0 12px;background:var(--bg-title);border-bottom:1px solid var(--line)}
.tb-left,.tb-right{display:flex;align-items:center;gap:8px;min-width:0}.tb-right{justify-content:flex-end}
.brand{display:flex;align-items:center;gap:8px;font-weight:600;letter-spacing:.01em;white-space:nowrap}
.brand .mark{width:20px;height:20px;border-radius:6px;background:linear-gradient(135deg,#3b82f6,#7c5cff);display:grid;place-items:center;color:#fff}
.jobpick{position:relative;display:flex;align-items:center;min-width:0}
.jobpick select{appearance:none;-webkit-appearance:none;background:var(--bg-input);border:1px solid var(--line);border-radius:6px;padding:3px 26px 3px 9px;height:26px;max-width:340px;min-width:0;text-overflow:ellipsis;font-family:var(--mono);font-size:12px}
.jobpick svg{position:absolute;right:7px;pointer-events:none;color:var(--mute)}
.cmdcenter{display:flex;align-items:center;gap:8px;height:26px;padding:0 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg-input);color:var(--mute);width:100%;min-width:0;justify-content:center}
.cmdcenter:hover{border-color:color-mix(in srgb,var(--accent) 50%,var(--line));color:var(--text)}
.cmdcenter span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
kbd{font-size:10.5px;padding:1px 5px;border-radius:4px;border:1px solid var(--line);background:var(--bg-elev);color:var(--mute)}
.iconbtn{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:6px;border:0;background:none;color:var(--mute)}
.iconbtn:hover{background:var(--bg-hover);color:var(--text)}.iconbtn.on{color:var(--text);background:var(--bg-active)}
.iconbtn.sm{width:22px;height:22px;border-radius:4px}
/* main */
.main{display:flex;min-height:0;min-width:0;position:relative}
.activity{width:48px;flex:none;background:var(--bg-act);border-right:1px solid var(--line);display:flex;flex-direction:column;align-items:center;padding-top:4px;gap:2px}
.activity button{position:relative;width:48px;height:46px;border:0;background:none;color:var(--dim);display:grid;place-items:center}
.activity button:hover{color:var(--text)}
.activity button.on{color:var(--text)}
.activity button.on::before{content:"";position:absolute;left:0;top:9px;bottom:9px;width:2px;border-radius:0 2px 2px 0;background:var(--accent)}
.activity .badge{position:absolute;right:7px;bottom:7px;min-width:16px;height:16px;border-radius:8px;padding:0 4px;font-size:10px;font-weight:700;line-height:16px;background:var(--accent);color:#fff;text-align:center}
.activity .badge.err{background:var(--err)}
.activity .spacer{flex:1}
.sidebar{width:var(--side-w);flex:none;background:var(--bg-side);display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
.side-head{height:35px;display:flex;align-items:center;justify-content:space-between;padding:0 8px 0 18px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);flex:none}
.side-body{flex:1;overflow:auto;padding-bottom:12px}
.sash{flex:none;position:relative;z-index:5}
.sash.v{width:4px;margin-left:-2px;margin-right:-2px;cursor:col-resize}
.sash.h{height:4px;margin-top:-2px;margin-bottom:-2px;cursor:row-resize}
.sash:hover,.sash.drag{background:var(--accent)}
.work{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0}
/* tree */
.sect{border-top:1px solid var(--line-soft)}.sect:first-child{border-top:0}
.sect-h{display:flex;align-items:center;gap:4px;height:24px;padding:0 8px 0 4px;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text);cursor:pointer;user-select:none}
.sect-h .cnt{margin-left:auto;font-weight:600;color:var(--dim);letter-spacing:0;text-transform:none}
.sect-h:hover{background:var(--bg-hover)}
.side-probs .prob{padding:3px 12px 3px 18px}.side-probs .prob .pl{display:block;margin-left:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row{display:flex;align-items:center;gap:6px;height:24px;padding:0 8px 0 calc(6px + var(--d,0) * 14px);cursor:pointer;white-space:nowrap;color:var(--text);user-select:none;position:relative}
.row:hover{background:var(--bg-hover)}
.row.inert{cursor:default}.row.inert:hover{background:none}
.row.sel{background:var(--bg-active)}
.row.sel::after{content:"";position:absolute;inset:0;border:1px solid color-mix(in srgb,var(--accent) 55%,transparent);pointer-events:none}
.twist{width:16px;height:16px;display:grid;place-items:center;color:var(--mute);flex:none;transition:transform .12s}
.twist.open{transform:rotate(90deg)}.twist.none{visibility:hidden}
.row .lbl{overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto}
.row .desc{flex:1 1 0}
.row .desc{color:var(--dim);overflow:hidden;text-overflow:ellipsis;min-width:0;font-size:12px}
.row .meta{margin-left:auto;color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums;padding-left:8px;flex:none}
.row .ico{display:grid;place-items:center;width:16px;height:16px;flex:none}
.side-note{padding:10px 18px;color:var(--dim);font-size:12px}
.side-pad{padding:6px 14px 10px}
.kvs{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:12px}.kvs dt{color:var(--dim)}.kvs dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.filters{display:grid;grid-template-columns:1fr;gap:6px;padding:4px 14px 10px}
.filters label{display:grid;grid-template-columns:64px 1fr;align-items:center;gap:8px;font-size:12px;color:var(--mute)}
.sel-in{background:var(--bg-input);border:1px solid var(--line);border-radius:5px;height:24px;padding:0 6px;min-width:0;font-size:12px}
/* status icons */
.st{color:var(--c,var(--dim))}
.spin{animation:spin 1s linear infinite;transform-origin:50% 50%}@keyframes spin{to{transform:rotate(360deg)}}
/* banners */
#banners:empty{display:none}
#banners{max-height:38vh;overflow:auto;flex:none}
.banner{display:flex;gap:10px;align-items:flex-start;padding:8px 14px;border-bottom:1px solid var(--line);font-size:12.5px;background:color-mix(in srgb,var(--c) 10%,var(--bg));}
.banner .bi{color:var(--c);margin-top:1px}.banner b{font-weight:600}
.banner .bt{flex:1;min-width:0}
.banner pre{margin:4px 0 0;white-space:pre-wrap;word-break:break-word;max-height:120px;overflow:auto;font-size:11.5px;color:var(--mute)}
.cmd{display:inline-flex;align-items:center;gap:8px;margin-top:5px;max-width:100%;padding:3px 4px 3px 9px;border:1px solid var(--line);border-radius:5px;background:var(--bg-input);font-family:var(--mono);font-size:11.5px}
.cmd span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmd button{border:0;background:var(--bg-hover);border-radius:4px;padding:1px 7px;font-size:11px;font-family:var(--font);color:var(--text)}
.cmd button:hover{background:var(--bg-active)}
.banner-more{display:block;width:100%;text-align:left;padding:6px 14px;border:0;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--gate) 14%,var(--bg));color:var(--text);font-size:12px;font-weight:600}
/* editor */
.editor{flex:1;min-height:0;display:flex;flex-direction:column;background:var(--bg)}
.tabs{display:flex;height:35px;flex:none;background:var(--bg-tab);overflow-x:auto;overflow-y:hidden;scrollbar-width:none;border-bottom:1px solid var(--line)}
.tabs::-webkit-scrollbar{display:none}
.tabbar{display:flex;flex:none;min-width:0}.tabbar .tabs{flex:1;min-width:0}
.tabs-more{flex:none;width:34px;height:35px;border:0;border-left:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--bg-tab);color:var(--mute);font-size:16px;line-height:1}
.tabs-more:hover{color:var(--text);background:var(--bg-hover)}.tabs-more[hidden]{display:none}
.tab{display:flex;align-items:center;gap:6px;padding:0 6px 0 12px;height:35px;border-right:1px solid var(--line);color:var(--mute);background:var(--bg-tab);white-space:nowrap;cursor:pointer;position:relative;flex:none;max-width:240px;margin-bottom:-1px}
.tab:hover{color:var(--text)}
.tab.on{background:var(--bg-tab-on);color:var(--text);border-bottom:1px solid var(--bg-tab-on)}
.tab.on::before{content:"";position:absolute;left:0;right:0;top:0;height:1.5px;background:var(--accent)}
.tab .tl{overflow:hidden;text-overflow:ellipsis}
.tab .x{width:20px;height:20px;border-radius:4px;border:0;background:none;color:inherit;display:grid;place-items:center;opacity:0}
.tab:hover .x,.tab.on .x,.tab .x:focus-visible,.tab:focus-within .x{opacity:.8}.tab .x:hover{background:var(--bg-hover);opacity:1}
.tab .x.pin{opacity:0;pointer-events:none;width:6px}
.editor-body{flex:1;min-height:0;position:relative}
.pane{position:absolute;inset:0;overflow:auto;display:none}
.pane.on{display:block}
.pane.flex.on{display:flex;flex-direction:column}
.crumbs{display:flex;align-items:center;gap:4px;height:24px;padding:0 16px;color:var(--mute);font-size:12px;border-bottom:1px solid var(--line-soft);white-space:nowrap;overflow:hidden;flex:none;background:var(--bg)}
.crumbs .c{cursor:pointer}.crumbs .c:hover{color:var(--text)}.crumbs svg{color:var(--dim)}
.doc{max-width:1180px;margin:0 auto;padding:20px 28px 48px}
.doc-h{display:flex;align-items:flex-start;gap:14px;justify-content:space-between;flex-wrap:wrap;margin-bottom:14px}
.doc-h h1{font-size:20px;font-weight:600;margin:0;letter-spacing:-.01em;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.doc-h .sub{color:var(--mute);margin-top:4px;max-width:820px}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:5px;height:20px;padding:0 7px;border-radius:4px;font-size:11.5px;background:var(--bg-elev);border:1px solid var(--line);color:var(--mute);white-space:nowrap;max-width:100%}
.chip.mono{font-family:var(--mono);font-size:11px}
.chip.c{color:var(--c);border-color:color-mix(in srgb,var(--c) 40%,var(--line));background:color-mix(in srgb,var(--c) 10%,transparent)}
.chip.link{cursor:pointer}.chip.link:hover{border-color:var(--accent);color:var(--text)}
.pill{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:11px;font-size:11.5px;font-weight:600;color:var(--c);background:color-mix(in srgb,var(--c) 13%,transparent);border:1px solid color-mix(in srgb,var(--c) 38%,transparent);letter-spacing:.02em;white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.live .dot{animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,currentColor 65%,transparent)}70%{box-shadow:0 0 0 6px transparent}100%{box-shadow:0 0 0 0 transparent}}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:4px 0 18px}
.tile{background:var(--bg-card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0}
.tile .k{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:600}
.tile .v{font-size:19px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .v small{font-size:13px;color:var(--dim);font-weight:500}
.tile .s{color:var(--mute);font-size:11.5px;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meter{height:4px;border-radius:2px;background:var(--line);overflow:hidden;display:flex;margin-top:8px}.meter i{display:block;height:100%}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.card{background:var(--bg-card);border:1px solid var(--line);border-radius:8px;min-width:0}
.card-h{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line-soft);font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--mute)}
.card-h .r{margin-left:auto;font-weight:500;letter-spacing:0;text-transform:none;color:var(--dim)}
.card-b{padding:10px 12px}
.card-b.flush{padding:0}
.stack>*+*{margin-top:14px}
.quote{border-left:3px solid var(--accent);padding:8px 12px;background:var(--accent-soft);border-radius:0 6px 6px 0;white-space:pre-wrap;word-break:break-word}
ul.clean{list-style:none;margin:0;padding:0}
ul.clean li{display:flex;gap:8px;padding:5px 0;border-bottom:1px solid var(--line-soft);align-items:flex-start}
ul.clean li:last-child{border-bottom:0}
ul.clean li .i{margin-top:2px;flex:none}
ul.clean li .t{min-width:0;word-break:break-word}
.ev-txt{color:var(--mute);font-size:12px;margin-top:2px;word-break:break-word}
.sect-lbl{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--mute);margin:0 0 4px}
.clickable{cursor:pointer}.clickable:hover{background:var(--bg-hover)}
.act-d{margin-top:4px;padding-left:8px;border-left:2px solid color-mix(in srgb,var(--split) 45%,transparent)}
.muted{color:var(--mute)}.dim{color:var(--dim)}.small{font-size:12px}
.empty{color:var(--dim);padding:14px 12px;font-size:12.5px}
.code-inline{font-family:var(--mono);font-size:11.5px;background:var(--bg-code);border:1px solid var(--line-soft);border-radius:4px;padding:0 4px;word-break:break-all}
pre.block{margin:0;background:var(--bg-code);border:1px solid var(--line-soft);border-radius:6px;padding:10px 12px;overflow:auto;max-height:360px;white-space:pre-wrap;word-break:break-word}
table.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;font-weight:600;color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0;background:var(--bg-card)}
.tbl td{padding:5px 10px;border-bottom:1px solid var(--line-soft);vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl tr.click{cursor:pointer}.tbl tr.click:hover td{background:var(--bg-hover)}
.tblwrap{overflow:auto;max-height:520px}
/* wave board */
.board{display:flex;gap:12px;overflow-x:auto;padding-bottom:4px}
.wave{flex:0 0 230px;min-width:0}
.wave-h{font-size:10.5px;font-weight:700;letter-spacing:.08em;color:var(--dim);text-transform:uppercase;margin:0 0 6px 2px;display:flex;justify-content:space-between}
.tcard{display:block;width:100%;text-align:left;background:var(--bg-card);border:1px solid var(--line);border-left:3px solid var(--c);border-radius:6px;padding:7px 9px;margin-bottom:8px;color:var(--text)}
.tcard:hover{border-color:color-mix(in srgb,var(--accent) 50%,var(--line));border-left-color:var(--c)}
.tcard .tt{display:flex;align-items:center;gap:6px;font-weight:600;font-size:12.5px}
.tcard .tt span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tcard .ts{color:var(--mute);font-size:11.5px;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tcard .tf{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;margin-top:5px;font-variant-numeric:tabular-nums;gap:6px}
.tcard .tf span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* timeline */
.tl{padding:8px 12px}
.tlrow{display:grid;grid-template-columns:160px 1fr 64px;gap:10px;align-items:center;height:22px;font-size:12px;cursor:pointer;border-radius:4px}
.tlrow:hover{background:var(--bg-hover)}
.tlname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--mute);padding-left:4px}
.tltrack{position:relative;height:8px;background:var(--line-soft);border-radius:4px}
.tlbar{position:absolute;top:0;height:8px;border-radius:4px;min-width:3px;background:var(--c)}
.tlbar.running{background:repeating-linear-gradient(45deg,var(--c),var(--c) 5px,color-mix(in srgb,var(--c) 60%,transparent) 5px,color-mix(in srgb,var(--c) 60%,transparent) 10px);background-size:14px 14px;animation:slide 1s linear infinite}
@keyframes slide{to{background-position:14px 0}}
.tld{color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}
/* subtabs */
.subtabs{display:flex;gap:2px;padding:0 12px;border-bottom:1px solid var(--line);flex:none;overflow-x:auto;scrollbar-width:none;background:var(--bg)}
.subtabs::-webkit-scrollbar{display:none}
.subtabs button{border:0;background:none;padding:8px 10px 7px;color:var(--mute);border-bottom:2px solid transparent;white-space:nowrap;display:flex;gap:6px;align-items:center}
.subtabs button:hover{color:var(--text)}
.subtabs button.on{color:var(--text);border-bottom-color:var(--accent)}
.subtabs .n{font-size:10.5px;background:var(--bg-elev);border:1px solid var(--line);border-radius:8px;padding:0 5px;color:var(--mute)}
.subbody{flex:1;min-height:0;position:relative}
.subpane{position:absolute;inset:0;overflow:auto;display:none}.subpane.on{display:block}.subpane.flex.on{display:flex;flex-direction:column}
.taskhead{padding:14px 20px 10px;border-bottom:1px solid var(--line-soft);flex:none}
.taskhead h1{margin:0;font-size:17px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.taskhead .sub{color:var(--mute);margin-top:2px}
.taskhead .row2{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;align-items:center}
.taskhead .row2 .sp{flex:1}
/* log view */
.logview{display:flex;flex-direction:column;min-height:0;height:100%}
.logbar{display:flex;align-items:center;gap:6px;padding:4px 10px;border-bottom:1px solid var(--line-soft);flex:none;flex-wrap:wrap;background:var(--bg)}
.logbar .find{display:flex;align-items:center;gap:4px;background:var(--bg-input);border:1px solid var(--line);border-radius:5px;height:24px;padding:0 4px 0 7px;min-width:0;flex:0 1 260px}
.logbar .find:focus-within{border-color:var(--focus)}
.logbar .find input{border:0;background:none;outline:none;min-width:0;flex:1;font-size:12px;height:22px}
.logbar .cnt{font-size:11px;color:var(--dim);white-space:nowrap;font-variant-numeric:tabular-nums}
.logbar .sp{flex:1}
.tog{display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 8px;border-radius:5px;border:1px solid var(--line);background:none;color:var(--mute);font-size:12px;white-space:nowrap}
.tog:hover{color:var(--text);background:var(--bg-hover)}.tog.on{color:var(--text);border-color:color-mix(in srgb,var(--accent) 60%,var(--line));background:var(--accent-soft)}
.lognote{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:4px 12px;font-size:12px;color:var(--text);background:color-mix(in srgb,var(--warn) 12%,var(--bg));border-bottom:1px solid var(--line-soft);flex:none}
.lognote[hidden]{display:none}.lognote a{white-space:nowrap}
.logscroll{flex:1;min-height:0;overflow:auto;background:var(--bg-code)}
pre.code{margin:0;padding:6px 0 18px;counter-reset:ln;font-size:12px;line-height:1.55;white-space:pre;min-width:max-content}
pre.code.wrap{white-space:pre-wrap;word-break:break-word;min-width:0}
pre.code .l{display:block;padding:0 14px 0 62px;position:relative;min-height:1.55em}
pre.code .l::before{counter-increment:ln;content:counter(ln);position:absolute;left:0;width:46px;text-align:right;color:var(--dim);opacity:.7;user-select:none}
pre.code.nonum .l{padding-left:14px}pre.code.nonum .l::before{display:none}
pre.code .l:hover{background:color-mix(in srgb,var(--bg-hover) 60%,transparent)}
pre.code .l.hl{background:color-mix(in srgb,var(--warn) 16%,transparent)}
pre.code .e{color:var(--err)}pre.code .w{color:var(--warn)}pre.code .ok{color:var(--ok)}pre.code .cmdl{color:var(--info);font-weight:600}pre.code .dimx{color:var(--dim)}
mark{background:var(--mark);color:inherit;border-radius:2px}mark.cur{background:var(--mark-cur);outline:1px solid var(--warn)}
/* markdown */
.md{line-height:1.6;max-width:900px;word-break:break-word}
.md h1,.md h2,.md h3,.md h4{margin:1.2em 0 .4em;line-height:1.3;font-weight:600}
.md h1{font-size:19px;padding-bottom:6px;border-bottom:1px solid var(--line-soft)}.md h2{font-size:16px;padding-bottom:4px;border-bottom:1px solid var(--line-soft)}.md h3{font-size:14px}.md h4{font-size:13px}
.md>:first-child{margin-top:0}
.md p{margin:.5em 0}.md ul,.md ol{margin:.4em 0;padding-left:22px}.md li{margin:2px 0}
.md code{font-family:var(--mono);font-size:11.5px;background:var(--bg-code);border:1px solid var(--line-soft);border-radius:4px;padding:0 4px}
.md pre{background:var(--bg-code);border:1px solid var(--line-soft);border-radius:6px;padding:10px 12px;overflow:auto}
.md pre code{border:0;padding:0;background:none}
.md blockquote{margin:.6em 0;padding:2px 12px;border-left:3px solid var(--line);color:var(--mute)}
.md table{border-collapse:collapse;margin:.6em 0;font-size:12px}.md th,.md td{border:1px solid var(--line);padding:4px 8px;text-align:left}
.md hr{border:0;border-top:1px solid var(--line);margin:1em 0}
/* json */
.json{font-family:var(--mono);font-size:12px;line-height:1.6;padding:10px 16px 30px}
.json details{padding-left:0}.json details>summary{list-style:none;cursor:pointer;display:block;border-radius:3px}
.json details>summary::-webkit-details-marker{display:none}
.json details>summary::before{content:"";display:inline-block;width:0;height:0;border-left:4px solid var(--mute);border-top:4px solid transparent;border-bottom:4px solid transparent;margin:0 6px 1px -12px;transition:transform .1s}
.json details[open]>summary::before{transform:rotate(90deg)}
.json summary:hover{background:var(--bg-hover)}
.json .jb{padding-left:18px;border-left:1px solid var(--line-soft);margin-left:2px}
.json .jl{padding-left:12px}
.json details>summary .jc{display:none}.json details:not([open])>summary .jc{display:inline}
.jk{color:#7aa7ff}.js{color:#86c38a}.jn{color:#e0a96d}.jbool{color:#c792ea}.jnull{color:var(--dim)}.jc{color:var(--dim);font-style:italic;margin-left:6px}
:root[data-theme="light"] .jk{color:#0b5bd3}:root[data-theme="light"] .js{color:#1a7f37}:root[data-theme="light"] .jn{color:#b35900}:root[data-theme="light"] .jbool{color:#8250df}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]) .jk{color:#0b5bd3}:root:not([data-theme="dark"]) .js{color:#1a7f37}:root:not([data-theme="dark"]) .jn{color:#b35900}:root:not([data-theme="dark"]) .jbool{color:#8250df}}
.toolbar{display:flex;align-items:center;gap:6px;padding:6px 12px;border-bottom:1px solid var(--line-soft);flex:none;flex-wrap:wrap;background:var(--bg)}
.toolbar .sp{flex:1}.toolbar .ttl{font-size:12px;color:var(--mute)}
/* gallery */
.gal{padding:14px 18px 40px}
.gal-round{margin-bottom:22px}
.gal-rh{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 10px}
.gal-rh h2{font-size:13px;margin:0;font-weight:600}
.gal-page{margin:0 0 14px}
.gal-ph{font-size:12px;color:var(--mute);margin:0 0 6px;display:flex;gap:4px 8px;align-items:center;flex-wrap:wrap}
.gal-ph b{color:var(--text);font-weight:600}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.shot{background:var(--bg-card);border:1px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column;text-align:left;padding:0;color:var(--text);min-width:0}
.shot:hover{border-color:color-mix(in srgb,var(--accent) 60%,var(--line));box-shadow:0 4px 16px -8px rgba(0,0,0,.35)}
.shot .th{aspect-ratio:16/10;background:var(--bg-code) center top/cover no-repeat;border-bottom:1px solid var(--line-soft);position:relative;overflow:hidden}
.shot .th img{width:100%;height:100%;object-fit:cover;object-position:top;display:block}
.shot .th.mobile img{object-fit:contain;background:repeating-conic-gradient(var(--bg-code) 0 25%,var(--bg-side) 0 50%) 0 0/12px 12px}
.shot .ok-badge{position:absolute;top:6px;right:6px}
.shot .sb{padding:7px 9px;display:flex;flex-direction:column;gap:5px;min-width:0}
.shot .sn{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:600;min-width:0}
.shot .sn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.shot .ss{display:flex;gap:4px;flex-wrap:wrap}
.shot .chip{height:18px;font-size:10.5px;padding:0 5px}
/* lightbox */
.lightbox{position:fixed;inset:0;z-index:50;background:rgba(8,10,14,.86);display:flex;flex-direction:column;backdrop-filter:blur(2px)}
.lightbox[hidden]{display:none}
.lb-top{display:flex;align-items:center;gap:8px;padding:8px 12px;color:#e6e8eb;background:rgba(12,14,18,.94);border-bottom:1px solid rgba(255,255,255,.08);flex-wrap:wrap}
.lb-top .ttl{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1}
.lb-top .iconbtn{color:#c9ced6}.lb-top .iconbtn:hover{background:rgba(255,255,255,.1);color:#fff}
.lb-top .tog{color:#c9ced6;border-color:rgba(255,255,255,.18)}.lb-top .tog.on{color:#fff;background:rgba(76,141,255,.25)}
.lb-top .zoom{font-size:12px;color:#c9ced6;min-width:42px;text-align:center;font-variant-numeric:tabular-nums}
.lb-main{flex:1;min-height:0;display:flex}
.lb-stage{flex:1;min-width:0;overflow:auto;position:relative;display:flex;align-items:flex-start;justify-content:flex-start;padding:16px}
.lb-stage img{flex:none;margin:0 auto;display:block;box-shadow:0 10px 40px rgba(0,0,0,.6);background:#fff;border-radius:3px}
.lb-stage.fit img{max-width:100%;max-height:100%;object-fit:contain}
.lb-nav{position:absolute;top:50%;transform:translateY(-50%);width:40px;height:56px;border-radius:8px;border:0;background:rgba(12,14,18,.66);border:1px solid rgba(255,255,255,.22);box-shadow:0 2px 10px rgba(0,0,0,.35);color:#fff;display:grid;place-items:center;z-index:2}
.lb-nav:hover{background:rgba(12,14,18,.88)}.lb-nav.prev{left:10px}.lb-nav.next{right:10px}
.lb-side{width:340px;flex:none;background:var(--bg-side);color:var(--text);overflow:auto;border-left:1px solid var(--line)}
.lb-side .card{border-radius:0;border-width:0 0 1px 0;background:transparent}
.cmp{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(260px,1fr);gap:14px;width:100%;align-items:start}
.cmp.mobile{grid-auto-columns:minmax(200px,400px);justify-content:center}
.cmp figure{margin:0;color:#e6e8eb;min-width:0}.cmp figcaption{display:flex;gap:6px;align-items:center;margin-bottom:6px;font-size:12px;flex-wrap:wrap}
.cmp img{width:100%;height:auto}
.errlist{list-style:none;margin:0;padding:0}.errlist li{font-family:var(--mono);font-size:11.5px;padding:4px 0;border-bottom:1px solid var(--line-soft);word-break:break-word;color:var(--mute)}
.errlist li:last-child{border-bottom:0}
/* panel */
.panel{height:var(--panel-h);flex:none;display:flex;flex-direction:column;border-top:1px solid var(--line);background:var(--bg-panel);min-height:0}
.panel.closed{display:none}
.panel-tabs{display:flex;align-items:center;gap:2px;height:35px;padding:0 6px 0 10px;flex:none}
.panel-tabs .pt{border:0;background:none;color:var(--mute);font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:0 8px;height:35px;border-bottom:1px solid transparent;display:flex;align-items:center;gap:6px}
.panel-tabs .pt:hover{color:var(--text)}.panel-tabs .pt.on{color:var(--text);border-bottom-color:var(--text)}
.panel-tabs .n{font-size:10px;min-width:16px;height:16px;line-height:16px;border-radius:8px;background:var(--bg-elev);border:1px solid var(--line);text-align:center;padding:0 4px;letter-spacing:0}
.panel-tabs .sp{flex:1}
.panel-body{flex:1;min-height:0;position:relative}
.ppane{position:absolute;inset:0;overflow:auto;display:none}.ppane.on{display:block}.ppane.flex.on{display:flex;flex-direction:column}
.prob{display:flex;gap:8px;align-items:flex-start;padding:3px 14px;cursor:pointer;font-size:12.5px}
.prob:hover{background:var(--bg-hover)}
.prob .pi{margin-top:2px}.prob .pt2{min-width:0;flex:1}.prob .pl{color:var(--dim);margin-left:6px;font-size:11.5px;white-space:nowrap}
.prob-g{display:flex;align-items:center;gap:6px;padding:4px 10px;font-weight:600;font-size:12px;cursor:pointer;user-select:none}
.prob-g:hover{background:var(--bg-hover)}.prob-g .cnt{color:var(--dim);font-weight:500}
.feed .ev{display:grid;grid-template-columns:minmax(56px,auto) 18px 1fr;gap:6px;padding:5px 14px;border-bottom:1px solid var(--line-soft);font-size:12.5px;cursor:default}
.feed .ev.click{cursor:pointer}.feed .ev.click:hover{background:var(--bg-hover)}
.feed .ev .t{color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums;padding-top:1px}
.feed .ev .i{padding-top:2px}
.feed .ev .h{font-weight:600}
.feed .acts{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
.feed details summary{cursor:pointer;color:var(--accent);font-size:11.5px;margin-top:3px}
/* status bar */
.statusbar{display:flex;align-items:center;background:var(--sb-bg);color:var(--sb-fg);font-size:12px;padding:0 4px;gap:0;overflow:hidden;white-space:nowrap}
.statusbar.running{--sb-bg:#1f5bd6;--sb-fg:#fff}.statusbar.done{--sb-bg:#1a7f37;--sb-fg:#fff}.statusbar.bad{--sb-bg:#b42318;--sb-fg:#fff}.statusbar.attn{--sb-bg:#8a5a00;--sb-fg:#fff}.statusbar.waiting{--sb-bg:#5b4bc4;--sb-fg:#fff}
.sbi{display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 8px;border:0;background:none;color:inherit;font-size:12px}
button.sbi:hover{background:rgba(255,255,255,.14)}
.statusbar .sbi svg{color:currentColor !important}
.sbi .dot{width:7px;height:7px}
.sbi.live .dot{animation:pulse2 1.6s infinite}
@keyframes pulse2{0%{box-shadow:0 0 0 0 rgba(255,255,255,.7)}70%{box-shadow:0 0 0 6px rgba(255,255,255,0)}100%{box-shadow:0 0 0 0 rgba(255,255,255,0)}}
.sb-sp{flex:1}
.sbmeter{width:52px;height:4px;border-radius:2px;background:rgba(127,127,127,.35);overflow:hidden}.sbmeter i{display:block;height:100%;background:currentColor}
/* palette */
.palette{position:fixed;inset:0;z-index:60;display:flex;justify-content:center;align-items:flex-start;padding-top:9vh;background:rgba(0,0,0,.25)}
.palette[hidden]{display:none}
.pal{width:min(640px,94vw);background:var(--bg-elev);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column;max-height:70vh}
.pal input{border:0;border-bottom:1px solid var(--line);background:none;padding:12px 14px;font-size:14px;outline:none}
.pal .list{overflow:auto;padding:4px}
.pal .it{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:6px;cursor:pointer;min-width:0}
.pal .it.on{background:var(--accent-soft);outline:1px solid color-mix(in srgb,var(--accent) 45%,transparent)}
.pal .it .k{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;width:72px;flex:none}
.pal .it .lbl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pal .it .d{color:var(--dim);margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:10px;font-size:12px;max-width:45%}
.pal .hint{padding:6px 12px;border-top:1px solid var(--line);color:var(--dim);font-size:11.5px;display:flex;gap:14px}
.pal b.m{color:var(--accent);font-weight:700}
/* welcome / waiting */
.welcome{height:100%;display:grid;place-items:center;padding:24px}
.welcome .box{max-width:520px;text-align:center}
.welcome .logo{width:64px;height:64px;border-radius:16px;margin:0 auto 18px;background:linear-gradient(135deg,#3b82f6,#7c5cff);display:grid;place-items:center;color:#fff;box-shadow:0 10px 30px -10px #3b82f6}
.welcome h2{font-size:18px;margin:0 0 8px;font-weight:600}
.welcome p{color:var(--mute);margin:6px 0}
.dots{display:inline-flex;gap:5px;margin:14px 0}.dots i{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:bounce 1.2s infinite}.dots i:nth-child(2){animation-delay:.15s}.dots i:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-4px)}}
.joblinks{margin-top:18px;text-align:left;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.joblinks .row{height:30px;padding:0 12px}
#toast{position:fixed;bottom:36px;left:50%;transform:translateX(-50%);background:var(--bg-elev);border:1px solid var(--line);box-shadow:var(--shadow);padding:6px 12px;border-radius:6px;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:70}
#toast.on{opacity:1}
.vis-mobile{display:none}
@media (max-width:1100px){.grid2{grid-template-columns:1fr}}
@media (max-width:760px){
  .titlebar{grid-template-columns:auto minmax(0,1fr) auto;gap:6px;padding:0 4px 0 8px}
  .brand .name{display:none}
  .cmdcenter{width:34px;padding:0;justify-content:center;justify-self:end}.cmdcenter span,.cmdcenter kbd{display:none}
  .jobpick select{max-width:none;width:100%}
  .tb-left{min-width:0}.tb-left .jobpick{flex:1}
  .titlebar{grid-template-columns:minmax(0,1fr) auto auto}
  .activity{width:42px}.activity button{width:42px;height:42px}
  .sidebar{position:absolute;left:42px;top:0;bottom:0;z-index:20;width:min(300px,calc(100vw - 54px));box-shadow:var(--shadow);transform:translateX(-110%);transition:transform .18s;visibility:hidden}
  .sidebar.mopen{transform:none;visibility:visible}
  #sash-side{display:none}
  .doc{padding:14px 14px 40px}
  .tiles{grid-template-columns:repeat(2,minmax(0,1fr))}
  .tlrow{grid-template-columns:96px 1fr 52px}
  .lb-main{flex-direction:column}.lb-side{width:auto;max-height:42%;border-left:0;border-top:1px solid var(--line)}
  .lb-nav{width:32px;height:44px}
  .hide-m{display:none !important}.vis-mobile{display:initial}
  .panel{--panel-h:42vh}
  .shots{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .taskhead{padding:12px 14px 8px}
  .banner{padding:8px 10px}
  #banners{max-height:30vh}#banners.open{max-height:70vh}
  .tab{max-width:170px}
  .cmd{display:flex}.cmd span{overflow-x:auto;text-overflow:clip;flex:1;min-width:0}.cmd button{flex:none}
  .prob .pl{display:block;margin-left:0;white-space:normal;word-break:break-all}
  .panel-tabs{padding:0 2px 0 4px;gap:0}.panel-tabs .pt{padding:0 5px;letter-spacing:.02em;font-size:10.5px}
}
</style></head><body>
<div id="app">
  <header class="titlebar">
    <div class="tb-left">
      <div class="brand"><span class="mark" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 4h4M3 12h4M9 8h4M7 4l2 4-2 4"/></svg></span><span class="name">orchestrate</span></div>
      <label class="jobpick"><select id="jobsel" aria-label="Job"></select><svg width="10" height="10" viewBox="0 0 16 16"><path d="M3 6l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg></label>
    </div>
    <button class="cmdcenter" data-act="palette" aria-label="Search tasks, files and screenshots"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg><span>Search tasks, files, screenshots</span><kbd>Ctrl K</kbd></button>
    <div class="tb-right">
      <button class="iconbtn hide-m" data-act="toggle-side" title="Toggle side bar (Ctrl+B)" aria-label="Toggle side bar"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M6 2.5v11"/></svg></button>
      <button class="iconbtn" data-act="toggle-panel" title="Toggle panel (Ctrl+J)" aria-label="Toggle panel"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M1.5 9.5h13"/></svg></button>
      <button class="iconbtn" data-act="theme" title="Toggle light / dark theme" aria-label="Toggle theme" id="themebtn"></button>
    </div>
  </header>
  <div class="main">
    <nav class="activity" id="activity" aria-label="Views"></nav>
    <aside class="sidebar" id="sidebar"><div class="side-head" id="side-head"></div><div class="side-body" id="side-body"></div></aside>
    <div class="sash v" id="sash-side" role="separator" aria-orientation="vertical" aria-label="Resize side bar (arrow keys)" tabindex="0"></div>
    <section class="work">
      <div id="banners"></div>
      <div class="editor"><div class="tabbar"><div class="tabs" id="tabs" role="tablist"></div><button class="tabs-more" id="tabs-more" data-act="tabs-more" hidden title="All open tabs" aria-label="All open tabs" aria-haspopup="dialog">…</button></div><div class="editor-body" id="editor-body"></div></div>
      <div class="sash h" id="sash-panel" role="separator" aria-orientation="horizontal" aria-label="Resize panel (arrow keys)" tabindex="0"></div>
      <section class="panel" id="panel"><div class="panel-tabs" id="panel-tabs"></div><div class="panel-body" id="panel-body"></div></section>
    </section>
  </div>
  <footer class="statusbar" id="statusbar"></footer>
</div>
<div id="lightbox" class="lightbox" role="dialog" aria-modal="true" aria-label="Screenshot viewer" hidden></div>
<div id="palette" class="palette" hidden></div>
<div id="toast" role="status"></div>
<script>
'use strict';
const DEFAULT_JOB = __DEFAULT_JOB__;
const FINGERPRINT = __FINGERPRINT__; /* the dashboard.py this page came from */
/* ---------- safe html ---------- */
class Raw{constructor(s){this.s=s}toString(){return this.s}}
const raw = s => new Raw(String(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmt(v){ if(v instanceof Raw) return v.s; if(Array.isArray(v)) return v.map(fmt).join(''); if(v==null||v===false) return ''; return esc(v); }
function h(strs, ...vals){ let o = strs[0]; for(let i=0;i<vals.length;i++) o += fmt(vals[i]) + strs[i+1]; return new Raw(o); }
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const FOCUS_ATTRS = ['data-act','data-kind','data-arg','data-id','data-key','data-sub','data-panel','data-view','data-filter','data-mode','data-fold','data-z','data-d','data-k'];
function focusKey(el){ if(!el || !el.getAttribute) return null; const ps = FOCUS_ATTRS.filter(k => el.hasAttribute(k)).map(k => `[${k}="${CSS.escape(el.getAttribute(k))}"]`);
  return ps.length ? el.tagName.toLowerCase() + ps.join('') : null; }
function patch(el, html){ const s = fmt(html); if(el._h === s) return; el._h = s;
  const open = new Set(), shut = new Set(); el.querySelectorAll('details[data-key]').forEach(d => (d.open ? open : shut).add(d.dataset.key));
  const ae = document.activeElement, fk = ae && ae !== el && el.contains(ae) ? focusKey(ae) : null;
  el.innerHTML = s;
  if(open.size || shut.size) el.querySelectorAll('details[data-key]').forEach(d => { if(open.has(d.dataset.key)) d.open = true; else if(shut.has(d.dataset.key)) d.open = false; });
  if(fk){ let n = null; try{ n = el.querySelector(fk); }catch(e){} if(n) n.focus({preventScroll:true}); }
  tickIn(el); }
const store = { get(k, d){ try{ const v = localStorage.getItem('orch.'+k); return v==null ? d : JSON.parse(v); }catch(e){ return d; } },
  set(k, v){ try{ localStorage.setItem('orch.'+k, JSON.stringify(v)); }catch(e){} } };
/* ---------- formatting ---------- */
const money = v => '$' + (Number(v) || 0).toFixed(2);
function dur(s){ if(s==null||!isFinite(s)||s<0) return '–'; s=Math.round(s); const hh=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60;
  return hh ? `${hh}h ${String(m).padStart(2,'0')}m` : m ? `${m}m ${String(x).padStart(2,'0')}s` : `${x}s`; }
const clock = t => t ? new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
const clockShort = t => t ? new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
const nowS = () => D ? D.now + (Date.now()/1000 - FETCHED) : Date.now()/1000;
function ago(t){ if(!t) return '–'; const s = Math.max(0, Date.now()/1000 - t); return s < 5 ? 'just now' : s < 60 ? Math.round(s)+'s ago' : s < 3600 ? Math.round(s/60)+'m ago' : Math.round(s/3600)+'h ago'; }
const plural = (n, w) => `${n} ${w}${n===1?'':'s'}`;
const str = v => v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v);
/* every list read from plan or state goes through arr(): a lone string is a one-item list, anything else non-list is empty */
const arr = x => Array.isArray(x) ? x.filter(v => v != null) : (typeof x === 'string' && x.trim() ? [x] : []);
const pluralW = (n, one, many) => `${n} ${n===1 ? one : many}`;
function midTrunc(v, n){ const x = String(v ?? ''); n = n || 28; if(x.length <= n) return x; const a = Math.ceil((n-1)/2), b = Math.floor((n-1)/2); return x.slice(0, a) + '…' + x.slice(x.length - b); }
const bytes = n => n >= 1048576 ? (n/1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n/1024) + ' KB' : (n || 0) + ' B';
const human = s => String(s ?? '').replaceAll('_', ' ');
const shq = v => { const x = String(v ?? ''); return /^[A-Za-z0-9_.\/:=@%+,-]+$/.test(x) ? x : "'" + x.replace(/'/g, "'\\''") + "'"; };
const CLI = () => (D && D.cli) || 'orchestrate.py';
function feedTime(t){ if(!t) return ''; const d = new Date(t*1000); return d.toDateString() === new Date().toDateString() ? clockShort(t) : d.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + clockShort(t); }
const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;
function stripAnsi(t){ return String(t||'').replace(ANSI,'').split('\n').map(l => { const i = l.lastIndexOf('\r', l.length-2); return (i>=0 ? l.slice(i+1) : l).replace(/\r$/,''); }).join('\n'); }
/* ---------- icons (static svg) ---------- */
const P = (d, o='') => `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" ${o}>${d}</svg>`;
const I = {
  files: raw('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v12"/><path d="M9 7h7l4 4v8a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V7z"/><path d="M16 7v4h4"/></svg>'),
  images: raw('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="3" y="5" width="15" height="13" rx="2"/><path d="M21 8v11a2 2 0 0 1-2 2H7"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="M18 15l-4-4-8 7"/></svg>'),
  review: raw('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/><path d="M7.5 10.5l2 2 3.5-4"/></svg>'),
  manager: raw('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>'),
  metrics: raw('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 20V10M10 20V4M16 20v-7M21 20H3"/></svg>'),
  plan: raw('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="6" height="6" rx="1.2"/><rect x="15" y="3" width="6" height="6" rx="1.2"/><rect x="9" y="15" width="6" height="6" rx="1.2"/><path d="M6 9v2.5a1.5 1.5 0 0 0 1.5 1.5h9a1.5 1.5 0 0 0 1.5-1.5V9M12 13v2"/></svg>'),
  chev: raw(P('<path d="M6 4l4 4-4 4"/>')),
  chevl: raw(P('<path d="M10 4L6 8l4 4"/>')),
  close: raw(P('<path d="M4 4l8 8M12 4l-8 8"/>')),
  folder: raw(P('<path d="M1.8 4.2c0-.7.5-1.2 1.2-1.2h3l1.5 1.5H13c.7 0 1.2.5 1.2 1.2v6.1c0 .7-.5 1.2-1.2 1.2H3c-.7 0-1.2-.5-1.2-1.2z"/>', 'style="color:var(--warn)"')),
  json: raw(P('<path d="M5.5 2.5c-1.5 0-2 .6-2 1.8v1.6c0 .9-.5 1.6-1.5 2.1 1 .5 1.5 1.2 1.5 2.1v1.6c0 1.2.5 1.8 2 1.8M10.5 2.5c1.5 0 2 .6 2 1.8v1.6c0 .9.5 1.6 1.5 2.1-1 .5-1.5 1.2-1.5 2.1v1.6c0 1.2-.5 1.8-2 1.8"/>', 'style="color:var(--warn)"')),
  md: raw(P('<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="M4 10.5v-5l2 2.5 2-2.5v5M11 5.5v5M9.5 9l1.5 1.5L12.5 9"/>', 'style="color:var(--info)"')),
  log: raw(P('<path d="M3 3.5h10M3 6.5h10M3 9.5h7M3 12.5h5"/>', 'style="color:var(--mute)"')),
  term: raw(P('<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M4.5 6l2 2-2 2M8 10.5h3.5"/>')),
  home: raw(P('<path d="M2.5 7.5L8 3l5.5 4.5M4 6.5V13h8V6.5"/>')),
  layers: raw(P('<path d="M8 2.5l6 3-6 3-6-3z"/><path d="M2 8.5l6 3 6-3"/><path d="M2 11l6 3 6-3" opacity=".6"/>')),
  image: raw(P('<rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="6" cy="6.5" r="1.1"/><path d="M14 11l-3.5-3.5L4 13"/>', 'style="color:var(--split)"')),
  desktop: raw(P('<rect x="1.5" y="2.5" width="13" height="8.5" rx="1"/><path d="M5.5 14h5M8 11v3"/>')),
  mobile: raw(P('<rect x="4.5" y="1.5" width="7" height="13" rx="1.5"/><path d="M7.3 12.3h1.4"/>')),
  search: raw(P('<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/>')),
  up: raw(P('<path d="M4 10l4-4 4 4"/>')), down: raw(P('<path d="M4 6l4 4 4-4"/>')),
  follow: raw(P('<path d="M8 2.5v9M4.5 8L8 11.5 11.5 8M3.5 13.5h9"/>')),
  wrap: raw(P('<path d="M2.5 4h11M2.5 8h9a2 2 0 0 1 0 4H8m1.5-1.5L8 12l1.5 1.5M2.5 12h3"/>')),
  copy: raw(P('<rect x="5.5" y="5.5" width="8" height="8" rx="1.3"/><path d="M10.5 5.5V3.8c0-.7-.6-1.3-1.3-1.3H3.8c-.7 0-1.3.6-1.3 1.3v5.4c0 .7.6 1.3 1.3 1.3h1.7"/>')),
  zin: raw(P('<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14M5 7h4M7 5v4"/>')),
  zout: raw(P('<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14M5 7h4"/>')),
  fit: raw(P('<path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"/>')),
  compare: raw(P('<rect x="1.5" y="3" width="5.5" height="10" rx="1"/><rect x="9" y="3" width="5.5" height="10" rx="1"/>')),
  ext: raw(P('<path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M12 9.5v3c0 .6-.4 1-1 1H3.5c-.6 0-1-.4-1-1V5c0-.6.4-1 1-1h3"/>')),
  sun: raw(P('<circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1"/>')),
  moon: raw(P('<path d="M13.5 9.5A5.5 5.5 0 0 1 6.5 2.5a5.5 5.5 0 1 0 7 7z"/>')),
  branch: raw(P('<circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="5.5" r="1.5"/><path d="M4.5 5v6M11.5 7c0 2.5-3 2.5-6.2 4.6"/>')),
  clock: raw(P('<circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/>')),
  coin: raw(P('<circle cx="8" cy="8" r="6"/><path d="M9.8 5.8C9.4 5.2 8.8 5 8 5c-1.1 0-1.9.6-1.9 1.5 0 2 3.8 1 3.8 3 0 .9-.8 1.5-1.9 1.5-.9 0-1.6-.3-2-1M8 3.8V5M8 11v1.2"/>')),
  tasks: raw(P('<path d="M2.5 4l1.2 1.2L6 3M2.5 8.5l1.2 1.2L6 7.5M2.5 13l1.2 1.2L6 12M8.5 4.5h5M8.5 9h5M8.5 13.5h5"/>')),
  slots: raw(P('<rect x="2" y="2.5" width="12" height="4" rx="1"/><rect x="2" y="9.5" width="12" height="4" rx="1"/><path d="M4.5 4.5h.01M4.5 11.5h.01"/>')),
  sync: raw(P('<path d="M13 5.5A5.5 5.5 0 0 0 3.2 5M3 10.5A5.5 5.5 0 0 0 12.8 11M3 2.5V5h2.5M13 13.5V11h-2.5"/>')),
  bell: raw(P('<path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3zM6.5 14h3"/>')),
  brain: raw(P('<path d="M6 2.8a2 2 0 0 0-2 2v.3a2 2 0 0 0-1.5 2.9A2 2 0 0 0 4 11.3a2 2 0 0 0 2 1.9h0a1.5 1.5 0 0 0 2-1.3V4.2A1.5 1.5 0 0 0 6 2.8zM10 2.8a2 2 0 0 1 2 2v.3a2 2 0 0 1 1.5 2.9 2 2 0 0 1-1.5 3.3 2 2 0 0 1-2 1.9 1.5 1.5 0 0 1-2-1.3"/>', 'style="color:var(--split)"')),
  play: raw(P('<path d="M5 3.5v9l7-4.5z"/>', 'style="color:var(--run)"')),
  flag: raw(P('<path d="M3.5 14V2.5M3.5 3h8l-1.5 3 1.5 3h-8"/>')),
  owl: raw(P('<circle cx="5.5" cy="7" r="2"/><circle cx="10.5" cy="7" r="2"/><path d="M3 4.5L5.5 5M13 4.5L10.5 5M8 9.5v1.5M4 11c1 2 7 2 8 0"/>', 'style="color:var(--gate)"')),
  ui: raw(P('<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M1.5 5.5h13M4 4h.01M6 4h.01"/>', 'style="color:var(--split)"')),
};
const SEV = {
  blocker: {c:'var(--err)', label:'blocker', i: raw(P('<circle cx="8" cy="8" r="6"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/>','style="color:var(--err)"'))},
  major: {c:'var(--warn)', label:'major', i: raw(P('<path d="M8 2l6.3 11.2H1.7z"/><path d="M8 6.5v3M8 11.4h.01"/>','style="color:var(--warn)"'))},
  minor: {c:'var(--info)', label:'minor', i: raw(P('<circle cx="8" cy="8" r="6"/><path d="M8 7.3v4M8 5h.01"/>','style="color:var(--info)"'))},
};
const sev = s => SEV[s] || SEV.minor;
/* ---------- status ---------- */
const STI = {
  circle: '<circle cx="8" cy="8" r="5"/>', spin: '<path d="M8 2.5a5.5 5.5 0 1 1-5.5 5.5" class="spin"/>',
  check: '<circle cx="8" cy="8" r="6" fill="currentColor" stroke="none" opacity=".18"/><path d="M5.2 8.2l1.9 1.9 3.8-4"/>',
  x: '<circle cx="8" cy="8" r="6" fill="currentColor" stroke="none" opacity=".18"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/>',
  lock: '<rect x="3.5" y="7" width="9" height="6.5" rx="1.2"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/>',
  block: '<circle cx="8" cy="8" r="5.5"/><path d="M4.2 11.8l7.6-7.6"/>',
  skip: '<path d="M3.5 4l5 4-5 4zM10.5 4v8"/>', split: '<path d="M8 13.5V8M8 8L4 3.5M8 8l4-4.5M2.5 5V3h2M13.5 5V3h-2"/>',
  pause: '<circle cx="8" cy="8" r="6"/><path d="M6.5 5.8v4.4M9.5 5.8v4.4"/>', q: '<circle cx="8" cy="8" r="6"/><path d="M6.3 6.3a1.8 1.8 0 1 1 2.4 1.7c-.5.2-.7.6-.7 1.1v.3M8 11.4h.01"/>',
  dot: '<circle cx="8" cy="8" r="3" fill="currentColor"/>',
};
const STATUS = {
  pending:{c:'var(--dim)',i:'circle',l:'pending'}, running:{c:'var(--run)',i:'spin',l:'running'}, done:{c:'var(--ok)',i:'check',l:'done'},
  failed:{c:'var(--err)',i:'x',l:'failed'}, blocked:{c:'var(--warn)',i:'block',l:'blocked'}, awaiting_approval:{c:'var(--gate)',i:'lock',l:'needs approval'},
  skipped:{c:'var(--skip)',i:'skip',l:'skipped'}, split:{c:'var(--split)',i:'split',l:'split'}, stopped:{c:'var(--err)',i:'pause',l:'stopped'},
  awaiting_human:{c:'var(--gate)',i:'q',l:'needs you'}, environment:{c:'var(--err)',i:'x',l:'environment'}, review_failed:{c:'var(--err)',i:'x',l:'review failed'},
  budget:{c:'var(--warn)',i:'block',l:'budget'}, advisor_blocked:{c:'var(--gate)',i:'q',l:'advisor: revise'}, interrupted:{c:'var(--warn)',i:'pause',l:'interrupted'},
  waiting:{c:'var(--split)',i:'spin',l:'waiting'}, planned:{c:'var(--dim)',i:'circle',l:'planned'},
};
const stt = s => STATUS[s] || {c:'var(--dim)', i:'dot', l: String(s||'?')};
const sicon = s => raw(`<svg class="st" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="--c:${stt(s).c}">${STI[stt(s).i]}</svg>`);
const pill = (s, live, label) => h`<span class="pill ${live?'live':''}" style="--c:${raw(stt(s).c)}"><span class="dot"></span>${label || stt(s).l}</span>`;
const okPill = ok => pill(ok ? 'done' : 'failed', false, ok ? 'looks right' : 'issue');
const verdictPill = v => pill(v === 'pass' ? 'done' : 'failed', false, v === 'pass' ? 'pass' : 'changes requested');
const okIcon = ok => ok ? sicon('done') : sicon('failed');
/* ---------- state ---------- */
const qp = new URLSearchParams(location.search);
let JOB = qp.get('job') || DEFAULT_JOB || null;
let D = null, FETCHED = Date.now()/1000, SIG = '', MISSING = false, JOBLIST = [], LASTOK = 0, NETERR = false;
let VIEW = store.get('view', 'explorer');
const OPEN = new Set(store.get('open', ['job','waves','files','logs','w0','w1','w2','w3','w4','w5']));
const SF = {round:'all', viewport:'all', task:'all'};
let PANEL = store.get('panelTab', 'problems');
let TERMSRC = 'out';
/* ---------- derived data ---------- */
const allTasks = () => D ? D.tasks.concat(D.extra || []) : [];
const taskById = id => allTasks().find(t => t.id === id);
function levels(){
  const T = Object.fromEntries(D.tasks.map(t => [t.id, t])), memo = {};
  const lv = (id, seen=new Set()) => { if(id in memo) return memo[id]; if(seen.has(id)) return 0; seen.add(id);
    const deps = arr(T[id].depends_on).filter(d => T[d]); return memo[id] = deps.length ? 1 + Math.max(...deps.map(d => lv(d, seen))) : 0; };
  D.tasks.forEach(t => lv(t.id)); return memo;
}
function waves(){ if(!D) return []; const L = levels(), n = D.tasks.length ? Math.max(...Object.values(L)) + 1 : 0, w = Array.from({length:n}, () => []);
  D.tasks.forEach(t => w[L[t.id]].push(t)); return w; }
function allShots(){ const out = []; arr(D && D.ui_rounds).forEach(r => arr(r.shots).forEach(s => { if(s && typeof s === 'object') out.push({...s, round: r.round, _r: r}); })); return out; }
const shotUrl = s => `/api/job/${encodeURIComponent(JOB)}/shot?f=${encodeURIComponent(s.file || '')}`;
const shotErrs = s => arr(s.console).length + arr(s.page_errors).length + arr(s.failed_requests).length + arr(s.step_errors).length;
function shotObs(s){ const v = s._r && s._r.verdict; if(!v) return null; return arr(v.pages).find(p => p && p.shot === s.id) || null; }
const shotReviewed = s => { const v = s._r && s._r.verdict; return !!(v && arr(v.pages).length); };
function validSF(){ /* a filter left over from another job or round matches nothing: fall back to "all" */
  const R = new Set(arr(D && D.ui_rounds).map(r => String(r.round))), T = new Set(allShots().flatMap(s => arr(s.tasks)));
  if(SF.round !== 'all' && !R.has(String(SF.round))) SF.round = 'all'; if(SF.task !== 'all' && !T.has(SF.task)) SF.task = 'all'; if(!['all','desktop','mobile'].includes(SF.viewport)) SF.viewport = 'all'; }
function filteredShots(extra){ validSF(); return allShots().filter(s => (SF.round==='all' || String(s.round)===String(SF.round)) && (SF.viewport==='all' || s.viewport===SF.viewport)
  && (SF.task==='all' || arr(s.tasks).includes(SF.task)) && (!extra || extra(s))); }
function latestReview(){ const r = arr(D && D.review); for(let i=r.length-1;i>=0;i--) if(r[i] && r[i].verdict) return r[i]; return null; }
function findings(){ const out = []; arr(D && D.review).forEach((r, ri) => { const v = r.verdict || {}, vf = arr(v.findings).filter(f => f && typeof f === 'object');
  vf.forEach((f, fi) => out.push({...f, _round: r.round || ri+1, _key: `${ri}:${fi}`}));
  const u = r.ui && r.ui.verdict; if(u) arr(u.findings).forEach((f, fi) => { if(!f || typeof f !== 'object') return; if(!vf.some(x => x.issue === '[UI] ' + f.issue || x.issue === f.issue)) out.push({...f, issue: String(f.issue||'').startsWith('[UI]') ? f.issue : '[UI] ' + (f.issue||''), _round: r.round || ri+1, _key: `${ri}:u${fi}`}); }); });
  return out; }
function globRe(p){ let r = ''; for(let i=0;i<p.length;i++){ const c = p[i]; if(c==='*'){ if(p[i+1]==='*'){ r += '.*'; i++; if(p[i+1]==='/') i++; } else r += '[^/]*'; } else if(c==='?') r += '[^/]'; else r += c.replace(/[.+^${}()|[\]\\]/g, '\\$&'); } return new RegExp('^' + r + '$'); }
function owns(path, pat){ path = String(path||'').replace(/^\.\//,''); if(!/[*?]/.test(pat)) return path === pat || path.startsWith(pat.replace(/\/$/,'') + '/'); try{ return globRe(pat).test(path); }catch(e){ return false; } }
const ownersOf = path => D.tasks.filter(t => arr(t.owns).some(p => owns(path, p)) || arr(t.state.files).includes(path)).map(t => t.id);
function taskCalls(id){ return arr(D.calls).filter(c => c && c.task && String(c.task).split(',').includes(id)); }
/* ---------- events (activity) ---------- */
function actionDetail(a){
  const rows = [], tid = typeof a.task === 'string' ? a.task : a.task && a.task.id;
  const add = (k, v) => { if(v == null || v === '' || (Array.isArray(v) && !v.length)) return; rows.push(h`<div class="ev-txt" style="white-space:pre-wrap"><b>${k}:</b> ${Array.isArray(v) ? v.map(str).join(', ') : str(v)}</div>`); };
  if(a.action === 'split') add('into', arr(a.into).map(x => (x && x.id) || str(x)));
  if(a.action === 'add_task' && a.task && typeof a.task === 'object'){ add('title', a.task.title); add('goal', a.task.goal); add('owns', a.task.owns); add('depends on', a.task.depends_on); }
  ['instructions','note','reason','question','scope','criteria','why','tier','owns','reads','verify','done'].forEach(k => add(k, a[k]));
  return h`<div class="act-d"><span class="chip c" style="--c:var(--split)">${human(a.action)}${tid ? ' ' + tid : ''}</span>${rows}</div>`;
}
function managerDetail(m){
  const st = m.second_try;
  return h`${arr(m.events).length ? h`<div class="ev-txt"><b>batched events:</b> ${arr(m.events).join(', ')}</div>` : ''}${m.answer ? h`<div class="ev-txt"><b>your answer:</b> ${str(m.answer)}</div>` : ''}
    ${m.note ? h`<div class="ev-txt" style="white-space:pre-wrap"><b>reasoning:</b> ${str(m.note)}</div>` : ''}${arr(m.actions).map(actionDetail)}
    ${m.rejected ? h`<div class="ev-txt" style="color:var(--warn)"><b>first proposal rejected:</b> ${arr(m.rejected).join('; ')}</div>` : ''}
    ${st ? h`<div class="ev-txt" style="margin-top:6px"><b>second try:</b> ${str(st.note || '')}</div>${arr(st.actions).map(actionDetail)}${st.rejected ? h`<div class="ev-txt" style="color:var(--err)"><b>second try rejected too:</b> ${arr(st.rejected).join('; ')}</div>` : ''}` : ''}`;
}
const mKey = m => `m:${m.t}|${m.event}|${m.task || ''}`;
function events(){
  if(!D) return [];
  const ev = [];
  if(D.created) ev.push({t:D.created, i:I.flag, h:'Job started', n:D.goal, c:'var(--accent)', key:'start'});
  allTasks().forEach(t => { const s = t.state || {};
    if(s.started) ev.push({t:s.started, i:I.play, h:`${t.id} started`, n:[t.type, t.tier, s.slot!=null ? 'slot '+s.slot : ''].filter(Boolean).join(' · '), go:['task', t.id], key:'s:' + t.id});
    if(s.ended && s.status !== 'running') ev.push({t:s.ended, i:sicon(s.status), h:`${t.id} ${stt(s.status).l}`, n:(s.report && s.report.summary) || s.skip_reason || s.error || '', c:stt(s.status).c, go:['task', t.id], key:'e:' + t.id,
      acts:arr(s.steps).map(x => h`<span class="chip mono c" style="--c:${raw(x.ok?'var(--ok)':'var(--err)')}">${x.config}${x.cause ? ' · ' + x.cause : ''}</span>`)});
  });
  arr(D.manager && D.manager.log).forEach(m => { if(!m || typeof m !== 'object') return; const acts = arr((m.second_try || m).actions);
    ev.push({t:m.t, i:I.brain, h:`manager · ${human(m.event)}${m.task ? ' · ' + m.task : ''}${m.auto ? ' (rule)' : ''}`, n:m.note || '', c:'var(--split)', key: mKey(m), detail: managerDetail(m),
      acts:[...acts.map(a => h`<span class="chip c" style="--c:var(--split)">${human(a.action)} ${typeof a.task === 'string' ? a.task : (a.task && a.task.id) || ''}</span>`),
        m.rejected ? h`<span class="chip c" style="--c:var(--warn)">first proposal rejected</span>` : '', m.answer ? h`<span class="chip">your answer: ${String(m.answer).slice(0,80)}</span>` : ''],
      go: m.task && typeof m.task === 'string' ? ['task', m.task] : null}); });
  const rv = arr(D.calls).filter(c => c && /^review/.test(c.role||'')).map(c => c.t).sort((a,b)=>a-b), clusters = [];
  rv.forEach(t => { if(!clusters.length || t - clusters[clusters.length-1][1] > 180) clusters.push([t,t]); else clusters[clusters.length-1][1] = t; });
  /* a round with no time of its own sorts where it happened (after the work before it), not to the top */
  let prevT = Math.max(D.created || 0, ...allTasks().map(t => (t.state && t.state.ended) || 0));
  arr(D.review).forEach((r, i) => { const v = r.verdict || {}, t = r.t || (clusters[i] ? clusters[i][1] : null), st = t || prevT + 0.01 * (i + 1); prevT = Math.max(prevT, st);
    ev.push({t, st, i:I.review, h:`Review round ${r.round || i+1}: ${v.verdict ? human(v.verdict) : r.final_verify_ok===false ? 'final verify failed' : v.reviewer_error ? 'no verdict' : '…'}`, n:v.summary || '', c: v.verdict==='pass' ? 'var(--ok)' : v.verdict || r.final_verify_ok===false ? 'var(--err)' : 'var(--dim)', go:['review', String(i)], key:'r:' + i,
      acts:arr(v.findings).slice(0,5).map(f => h`<span class="chip c" style="--c:${raw(sev(f.severity).c)}">${f.severity}: ${String(f.issue||'').slice(0,64)}</span>`)});
    if(r.ui){ const u = r.ui, uv = u.verdict || {}; ev.push({t: t ? t + 1 : null, st: st + 0.001, i:I.ui, h:`UI review round ${r.round || i+1}: ${u.ran ? (uv.verdict ? human(uv.verdict) : 'no verdict') : u.skipped ? 'skipped' : u.error ? 'could not run' : '…'}`,
      n: uv.summary || u.skipped || u.error || '', c: uv.verdict==='pass' ? 'var(--ok)' : (u.error || uv.verdict) ? 'var(--err)' : 'var(--dim)', go:['shots', ''], key:'u:' + i}); }
    if(r.advisor){ const a = r.advisor; ev.push({t: t ? t + 2 : null, st: st + 0.002, i:I.owl, h:`Advisor final check: ${a.overridden_by_user ? 'overridden by you' : a.error ? 'no usable answer' : a.verdict || '…'}`, n:a.summary || '', c: a.verdict==='proceed' ? 'var(--ok)' : 'var(--gate)', go:['advisor', 'final'], key:'a:' + i}); }
  });
  arr((D.advice||{}).plan).forEach((a, i) => { if(!a || typeof a !== 'object') return; ev.push({t:a.t || D.created, i:I.owl, h:`Advisor plan check: ${a.error ? 'no usable answer' : a.verdict || '…'}`, n:a.summary || a.cause || '', c: a.verdict==='proceed' ? 'var(--ok)' : 'var(--gate)', long:true, go:['advisor', 'plan'], key:'ap:' + i,
    acts:arr(a.findings).slice(0,4).map(f => h`<span class="chip c" style="--c:${raw(sev(f.severity).c)}">${f.severity}: ${String(f.issue||'').slice(0,64)}</span>`)}); });
  if(D.finished && D.status !== 'running') ev.push({t:D.finished, i:sicon(D.status), h:`Job ${stt(D.status).l}`, n:D.message || '', c:stt(D.status).c, key:'end'});
  ev.forEach(e => { if(e.st == null) e.st = e.t; });
  ev.sort((a,b) => (b.st || 0) - (a.st || 0));
  return ev;
}
function feedHtml(ev, limit){
  if(!ev.length) return h`<div class="empty">No activity yet.</div>`;
  return h`<div class="feed">${ev.slice(0, limit || 400).map(e => h`<div class="ev ${e.go?'click':''}" ${e.go ? raw(`data-act="open" data-kind="${esc(e.go[0])}" data-arg="${esc(e.go[1])}" tabindex="0" role="button"`) : ''}>
    <div class="t" title="${e.t ? new Date(e.t*1000).toLocaleString() : 'no time recorded'}">${e.t ? feedTime(e.t) : ''}</div><div class="i" style="color:${raw(e.c||'var(--mute)')}">${e.i}</div>
    <div><div class="h">${e.h}</div>${e.n ? (String(e.n).length > 220 ? h`<div class="ev-txt">${String(e.n).slice(0,220)}…</div>` : h`<div class="ev-txt">${String(e.n).slice(0,600)}</div>`) : ''}
    ${e.acts && e.acts.filter(Boolean).length ? h`<div class="acts">${e.acts}</div>` : ''}
    ${e.detail ? h`<details data-key="${'f:' + e.key}"><summary>show details</summary>${e.detail}</details>` : e.n && String(e.n).length > 220 ? h`<details data-key="${'f:' + e.key}"><summary>show all</summary><div class="ev-txt" style="white-space:pre-wrap">${e.n}</div></details>` : ''}</div></div>`)}</div>`;
}
/* ---------- markdown (escape first, then a fixed set of tags) ---------- */
function inl(s){
  const codes = [], keep = x => { codes.push(x); return `\u0000${codes.length-1}\u0000`; };
  let t = esc(s).replace(/`([^`]+)`/g, (_, c) => keep(`<code>${c}</code>`));
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, url) => /^(https?:\/\/|#)/i.test(url.replace(/&amp;/g,'&')) ? keep(`<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`) : txt)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[^\w*])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>').replace(/(^|\W)_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[+i]);
}
function md(src, depth){ depth = depth || 0;
  const L = String(src || '').replace(/\r/g, '').split('\n'), out = []; let i = 0;
  const isBlock = l => /^(```|#{1,6}\s|\s*([-*+]|\d+[.)])\s+|>|\s*\|.*\|\s*$|(-{3,}|\*{3,})\s*$)/.test(l);
  while(i < L.length){
    const l = L[i];
    if(/^```/.test(l)){ const b = []; i++; while(i < L.length && !/^```/.test(L[i])) b.push(L[i++]); i++; out.push(`<pre><code>${esc(b.join('\n'))}</code></pre>`); continue; }
    let m = l.match(/^(#{1,6})\s+(.*)$/); if(m){ const n = m[1].length; out.push(`<h${n}>${inl(m[2])}</h${n}>`); i++; continue; }
    if(/^(-{3,}|\*{3,})\s*$/.test(l)){ out.push('<hr>'); i++; continue; }
    if(/^>/.test(l)){ const b = []; while(i < L.length && /^>/.test(L[i])) b.push(L[i++].replace(/^>\s?/, '')); out.push(depth >= 8 ? `<blockquote><p>${esc(b.join('\n'))}</p></blockquote>` : `<blockquote>${md(b.join('\n'), depth + 1)}</blockquote>`); continue; }
    if(/^\s*\|.*\|\s*$/.test(l) && i+1 < L.length && /^\s*\|?\s*:?-{2,}/.test(L[i+1])){
      const row = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const head = row(l); i += 2; const rows = []; while(i < L.length && /^\s*\|.*\|\s*$/.test(L[i])) rows.push(row(L[i++]));
      out.push(`<table><thead><tr>${head.map(c => `<th>${inl(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${inl(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`); continue; }
    if(/^\s*([-*+]|\d+[.)])\s+/.test(l)){
      const ordered = /^\s*\d/.test(l), items = [];
      while(i < L.length && (/^\s*([-*+]|\d+[.)])\s+/.test(L[i]) || (/^\s{2,}\S/.test(L[i]) && items.length))){
        if(/^\s*([-*+]|\d+[.)])\s+/.test(L[i])){ const ind = L[i].match(/^\s*/)[0].length; items.push({ind, t: L[i].replace(/^\s*([-*+]|\d+[.)])\s+/, '')}); } else items[items.length-1].t += ' ' + L[i].trim(); i++; }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map(it => `<li${it.ind >= 2 ? ` style="margin-left:${Math.min(it.ind, 12) * 7}px"` : ''}>${inl(it.t.replace(/^\[( |x)\]\s*/i, (_, x) => x.trim() ? '☑ ' : '☐ '))}</li>`).join('')}</${tag}>`); continue; }
    if(!l.trim()){ i++; continue; }
    const p = []; while(i < L.length && L[i].trim() && !isBlock(L[i])) p.push(L[i++]); if(!p.length) p.push(L[i++]);
    out.push(`<p>${p.map(inl).join('<br>')}</p>`);
  }
  return out.join('\n');
}
/* ---------- json viewer ---------- */
function jsonView(v, key, depth, last){
  const k = key != null ? h`<span class="jk">${JSON.stringify(String(key))}</span>: ` : '';
  const comma = last ? '' : ',';
  if(v === null) return h`<div class="jl">${k}<span class="jnull">null</span>${comma}</div>`;
  if(typeof v === 'string') return h`<div class="jl">${k}<span class="js">${JSON.stringify(v)}</span>${comma}</div>`;
  if(typeof v === 'number') return h`<div class="jl">${k}<span class="jn">${String(v)}</span>${comma}</div>`;
  if(typeof v === 'boolean') return h`<div class="jl">${k}<span class="jbool">${String(v)}</span>${comma}</div>`;
  const arr = Array.isArray(v), ents = arr ? v.map((x, i) => [null, x]) : Object.entries(v), [o, c] = arr ? ['[', ']'] : ['{', '}'];
  if(!ents.length) return h`<div class="jl">${k}${o}${c}${comma}</div>`;
  return h`<details class="jl" ${depth < 2 ? raw('open') : ''}><summary>${k}${o}<span class="jc">${arr ? plural(ents.length, 'item') : plural(ents.length, 'key')} ${c}${comma}</span></summary><div class="jb">${ents.map(([kk, x], i) => jsonView(x, kk, depth+1, i === ents.length-1))}</div><div>${c}${comma}</div></details>`;
}
/* ---------- log view component ---------- */
const LOG_MAX = 4000000;
function LogView(src, opts={}){
  const el = document.createElement('div'); el.className = 'logview';
  el.innerHTML = `<div class="logbar"><div class="find">${I.search}<input type="search" placeholder="Find in log" aria-label="Find in log"><span class="cnt"></span>
    <button class="iconbtn sm" data-k="prev" title="Previous match (Shift+Enter)" aria-label="Previous match">${I.up}</button><button class="iconbtn sm" data-k="next" title="Next match (Enter)" aria-label="Next match">${I.down}</button></div>
    <span class="extra"></span><span class="sp"></span><span class="cnt lines"></span>
    <button class="tog" data-k="wrap" title="Wrap long lines">${I.wrap}<span>Wrap</span></button><button class="tog on" data-k="follow" title="Keep scrolled to the newest line">${I.follow}<span>Follow</span></button></div>
    <div class="lognote" hidden><span class="nt"></span><button class="tog" data-k="more">Load earlier</button><a class="raw" target="_blank" rel="noopener">Open the whole file</a></div>
    <div class="logscroll"><pre class="code"></pre></div>`;
  const st = {text:null, q:'', follow: opts.follow !== false, wrap: store.get('wrap', true), cur: 0, src, dirty:false, nb: opts.n || 400000, size: 0, start: 0};
  const pre = $('pre', el), sc = $('.logscroll', el), inp = $('input', el), cnt = $('.find .cnt', el), lines = $('.lines', el);
  const fb = $('[data-k=follow]', el), wb = $('[data-k=wrap]', el), note = $('.lognote', el), more = $('[data-k=more]', el), rawA = $('a.raw', el);
  const setFollow = f => { st.follow = f; fb.classList.toggle('on', f); if(f) sc.scrollTop = sc.scrollHeight; };
  const setWrap = w => { st.wrap = w; wb.classList.toggle('on', w); pre.classList.toggle('wrap', w); store.set('wrap', w); };
  setFollow(st.follow); setWrap(st.wrap);
  fb.onclick = () => setFollow(!st.follow); wb.onclick = () => setWrap(!st.wrap);
  more.onclick = () => { st.nb = Math.min(LOG_MAX, st.nb * 4); setFollow(false); load(true); };
  let lastTop = 0;
  sc.addEventListener('scroll', () => { const atBottom = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 24;
    if(sc.scrollTop < lastTop - 4 && !atBottom && st.follow) setFollow(false); else if(atBottom && !st.follow && sc.scrollTop > lastTop) setFollow(true); lastTop = sc.scrollTop; });
  function cls(l){ return /^\s*\$ /.test(l) ? 'cmdl' : /\b(error|failed|fail|traceback|exception|fatal|panic)\b|✗/i.test(l) ? 'e' : /\bwarn(ing)?\b/i.test(l) ? 'w' : /\b(pass(ed)?|ok|done|success)\b|✓/i.test(l) ? 'ok' : ''; }
  const url = n => { const u = st.src(); return u ? u + (u.includes('?') ? '&' : '?') + 'n=' + n : null; };
  function paint(){
    const sel = window.getSelection(); if(sel && !sel.isCollapsed && pre.contains(sel.anchorNode)){ st.dirty = true; return; }
    st.dirty = false;
    const text = stripAnsi(st.text || ''), ls = text.replace(/\n$/, '').split('\n'), cut = st.start > 0;
    let re = null; if(st.q){ try{ re = new RegExp(st.q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'); }catch(e){} }
    let n = 0; const parts = [];
    for(const l of ls){ let body;
      if(re){ re.lastIndex = 0; let last = 0, m, b = ''; while((m = re.exec(l))){ b += esc(l.slice(last, m.index)) + `<mark data-m="${n++}">` + esc(m[0]) + '</mark>'; last = m.index + m[0].length; if(!m[0].length) re.lastIndex++; } body = b + esc(l.slice(last)); }
      else body = esc(l);
      const c = cls(l); parts.push(`<span class="l${c ? ' ' + c : ''}">${body}</span>`); }
    const top = sc.scrollTop;
    pre.classList.toggle('nonum', cut); /* line numbers would restart at 1 on a tail: none rather than wrong ones */
    pre.innerHTML = st.text ? parts.join('') : `<span class="l dimx">${esc(opts.empty || '(empty)')}</span>`;
    lines.textContent = st.text ? plural(ls.length, 'line') + (cut ? ' shown' : '') : '';
    note.hidden = !cut;
    if(cut){ $('.nt', note).textContent = `Showing the last ${bytes(st.size - st.start)} of ${bytes(st.size)}; earlier lines are not loaded${st.q ? ' (Find searches the loaded part only)' : ''}.`;
      more.hidden = st.nb >= LOG_MAX; rawA.href = url('all') || '#'; }
    st.n = n; if(st.cur >= n) st.cur = 0;
    cnt.textContent = st.q ? (n ? `${st.cur+1} of ${n}` : cut ? 'No results in the loaded part' : 'No results') : '';
    if(st.q && n){ const mk = pre.querySelector(`mark[data-m="${st.cur}"]`); if(mk){ mk.classList.add('cur'); if(st.jump){ mk.scrollIntoView({block:'center'}); st.jump = false; } } }
    if(st.follow && !(st.q && n)) sc.scrollTop = sc.scrollHeight; else sc.scrollTop = top;
  }
  function go(d){ if(!st.n) return; st.cur = (st.cur + d + st.n) % st.n; st.jump = true; setFollow(false); paint(); }
  let qt; inp.addEventListener('input', () => { clearTimeout(qt); qt = setTimeout(() => { st.q = inp.value; st.cur = 0; st.jump = true; if(st.q) setFollow(false); paint(); }, 120); });
  inp.addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); go(e.shiftKey ? -1 : 1); } if(e.key === 'Escape'){ inp.value = ''; st.q = ''; paint(); e.stopPropagation(); } });
  $('[data-k=prev]', el).onclick = () => go(-1); $('[data-k=next]', el).onclick = () => go(1);
  let busy = false;
  async function load(force){
    const u = url(st.nb); if(!u){ if(st.text !== '') { st.text = ''; st.start = 0; st.size = 0; paint(); } return; }
    if(busy && !force) return; busy = true;
    try{ const r = await fetch(u, {cache:'no-store'}); const t = r.ok ? await r.text() : '';
      const size = +(r.headers.get('X-Log-Size') || 0), start = +(r.headers.get('X-Log-Start') || 0);
      if(t !== st.text || st.dirty || start !== st.start || size !== st.size || force){ st.text = t; st.size = size; st.start = start; paint(); } }
    catch(e){} finally{ busy = false; }
  }
  return {el, load, st, reset(){ st.text = null; st.cur = 0; st.nb = opts.n || 400000; st.start = 0; st.size = 0; load(); }, extra: $('.extra', el)};
}
/* ---------- tabs ---------- */
/* a view that throws is reported on the page (and in the console), never as a lost connection */
const RENDER_ERRS = new Map(); window.RENDER_ERRS = RENDER_ERRS;
function safe(name, f){ try{ f(); RENDER_ERRS.delete(name); }catch(e){ console.error('could not render ' + name, e); RENDER_ERRS.set(name, String((e && e.message) || e)); } }
const TABS = new Map(); let ORDER = [], ACTIVE = null;
const KINDS = {};
function tabId(kind, arg){ return kind + ':' + (arg ?? ''); }
function runUpdate(t){ safe('tab', () => { const r = KINDS[t.kind].update(t); if(r && typeof r.catch === 'function') r.catch(e => { console.error('could not render tab', e); RENDER_ERRS.set('tab', String((e && e.message) || e)); safe('banners', renderBanners); }); }); }
function openTab(kind, arg, opts={}){
  if(!KINDS[kind]) return; const id = tabId(kind, arg);
  let t = TABS.get(id);
  if(!t){ t = {id, kind, arg, el: document.createElement('div'), sub: opts.sub}; t.el.className = 'pane' + (KINDS[kind].flex ? ' flex' : ''); $('#editor-body').appendChild(t.el);
    TABS.set(id, t); const ai = ORDER.indexOf(ACTIVE); ORDER.splice(ai >= 0 ? ai+1 : ORDER.length, 0, id); KINDS[kind].mount(t); }
  else if(opts.sub && t.setSub) { t.sub = opts.sub; }
  activate(id); if(isMobile()) closeMobileSide();
}
function activate(id, push){
  ACTIVE = id; TABS.forEach(t => t.el.classList.toggle('on', t.id === id));
  const t = TABS.get(id); if(t && D) runUpdate(t);
  safe('tabs', renderTabs); safe('side', renderSide); syncHash(push !== false); const el = $(`.tab[data-id="${CSS.escape(id)}"]`); if(el) el.scrollIntoView({block:'nearest', inline:'nearest'});
}
/* deep links: #task/<id>/<sub>, #file/<arg>, #review/<i>, #advisor/<stage>, #shots, #metrics, #shot/<round>/<id>; extras &panel=<tab> &side=<view> &cmp=1.
   Each segment is URI-encoded; a malformed one is read as is, never thrown. Tabs and shots push history, so Back and Forward move between them. */
const dec = x => { try{ return decodeURIComponent(x); }catch(e){ return x; } };
const encSeg = x => encodeURIComponent(String(x)).replace(/%3A/gi, ':');
let PENDING_HASH = location.hash.slice(1), APPLYING = false, NAV_LAST = null;
function curHash(){
  if(LB.open){ const sh = LB.list[LB.idx]; if(sh) return '#shot/' + encSeg(sh.round) + '/' + encSeg(sh.id) + (LB.cmp ? '&cmp=1' : ''); }
  const t = TABS.get(ACTIVE);
  return t && t.id !== 'overview:' ? '#' + t.kind + (t.arg ? '/' + encSeg(t.arg) : '') + (t.kind === 'task' && t.sub && t.sub !== 'overview' ? '/' + t.sub : '') : '';
}
function syncHash(push){
  if(PENDING_HASH) return; const hs = curHash(); if(hs === location.hash || (!hs && !location.hash)) return;
  NAV_LAST = hs.slice(1);
  try{ history[push && !APPLYING ? 'pushState' : 'replaceState'](null, '', location.pathname + location.search + hs); }catch(e){}
}
function applyHash(){
  const hs = PENDING_HASH; PENDING_HASH = ''; if(!hs) return;
  APPLYING = true;
  try{
    const [route, ...kv] = hs.split('&'), opt = Object.fromEntries(kv.map(x => { const i = x.indexOf('='); return i < 0 ? [dec(x), ''] : [dec(x.slice(0, i)), dec(x.slice(i + 1))]; }));
    if(opt.side && SIDE[opt.side]){ VIEW = opt.side; if(isMobile()) $('#sidebar').classList.add('mopen'); renderSide(); }
    if(opt.panel && PTABS.some(p => p[0] === opt.panel)) showPanel(opt.panel);
    const seg = route.split('/').map(dec), kind = seg[0];
    if(kind !== 'shot' && LB.open) closeLB(true);
    if(kind === 'task' && seg[1]) openTab('task', seg[1], {sub: SUBS.some(x => x[0] === seg[2]) ? seg[2] : 'overview'});
    else if(kind === 'file' && seg[1]) openTab('file', seg.slice(1).join('/'));
    else if(kind === 'review' && seg[1]) openTab('review', seg[1]);
    else if(kind === 'advisor') openTab('advisor', seg[1] === 'final' ? 'final' : 'plan');
    else if(kind === 'finding' && seg[1]) openTab('finding', seg[1]);
    else if(kind === 'shots' || kind === 'metrics') openTab(kind, '');
    else if(kind === 'shot' && seg[1]){ openTab('shots', ''); openShot(seg.slice(2).join('/'), seg[1]); if(opt.cmp && LB.open) { LB.cmp = true; renderLB(); } }
    else if(!kind) activate('overview:');
    if(opt.side && isMobile()) $('#sidebar').classList.add('mopen');
    if(opt.palette != null) openPalette(opt.palette);
  } finally { APPLYING = false; }
  NAV_LAST = location.hash.slice(1);
}
function onNav(){ /* Back / Forward / an edited hash */
  const hs = location.hash.slice(1); if(hs === NAV_LAST) return; NAV_LAST = hs;
  if(!D){ PENDING_HASH = hs; return; }
  if(!hs){ APPLYING = true; try{ if(LB.open) closeLB(true); activate('overview:'); } finally { APPLYING = false; } return; }
  PENDING_HASH = hs; applyHash();
}
addEventListener('popstate', onNav); addEventListener('hashchange', onNav);
function closeTab(id){
  const t = TABS.get(id); if(!t || id === 'overview:') return;
  t.el.remove(); TABS.delete(id); const i = ORDER.indexOf(id); ORDER.splice(i, 1);
  if(ACTIVE === id) activate(ORDER[Math.min(i, ORDER.length-1)] || 'overview:', false); else renderTabs();
}
function renderTabs(){
  patch($('#tabs'), ORDER.map(id => { const t = TABS.get(id), k = KINDS[t.kind];
    return h`<div class="tab ${id===ACTIVE?'on':''}" role="tab" tabindex="0" aria-selected="${id===ACTIVE}" data-act="tab" data-id="${id}" title="${k.tip ? k.tip(t.arg) : k.title(t.arg)}">${k.icon(t.arg)}<span class="tl">${k.title(t.arg)}</span>
      ${id === 'overview:' ? h`<span class="x pin"></span>` : h`<button class="x" data-act="close-tab" data-id="${id}" aria-label="Close tab">${I.close}</button>`}</div>`; }));
  tabsOverflow();
}
/* the tab strip scrolls when it overflows; then a "..." button lists every open tab */
function tabsOverflow(){ const t = $('#tabs'), b = $('#tabs-more'); if(t && b) b.hidden = !(t.scrollWidth > t.clientWidth + 1); }
function openTabItems(){ return ORDER.map(id => { const t = TABS.get(id), k = KINDS[t.kind]; return {k: id === ACTIVE ? 'active' : 'tab', l: String(k.title(t.arg)), d: k.tip ? String(k.tip(t.arg)) : '', icon: k.icon(t.arg), tab: id}; }); }
/* ---------- tab kinds ---------- */
function crumbs(parts){ return h`<div class="crumbs">${parts.map((p, i) => h`${i ? I.chev : ''}<span class="${p.go?'c':''}" ${p.go ? raw(`data-act="open" data-kind="${esc(p.go[0])}" data-arg="${esc(p.go[1]||'')}" tabindex="0" role="link"`) : ''}>${p.t}</span>`)}</div>`; }
function region(parent, cls){ const d = document.createElement('div'); if(cls) d.className = cls; parent.appendChild(d); return d; }
function card(title, body, right, flush){ return h`<div class="card"><div class="card-h">${title}${right ? h`<span class="r">${right}</span>` : ''}</div><div class="card-b ${flush?'flush':''}">${body}</div></div>`; }
const listOr = (xs, f) => { xs = arr(xs); return xs.length ? h`<ul class="clean">${xs.map(f)}</ul>` : h`<div class="dim small">None</div>`; };
const taskChip = id => { const t = taskById(id); return h`<span class="chip link" data-act="open" data-kind="task" data-arg="${id}" tabindex="0" role="link" title="${id}">${sicon(t ? t.state.status : 'pending')}${midTrunc(id, 40)}</span>`; };
const li = (x, icon) => h`<li>${icon ? h`<span class="i">${icon}</span>` : ''}<span class="t">${str(x)}</span></li>`;
function acceptSource(){
  const R = arr(D && D.review), last = R[R.length-1]; let src = null;
  for(let i=R.length-1;i>=0;i--){ const v = R[i] && R[i].verdict; if(v && arr(v.acceptance).length){ src = R[i]; break; } }
  let why = '';
  if(last && last !== src){ const v = last.verdict; why = `round ${last.round}: ` + (last.final_verify_ok === false ? 'final verify failed, acceptance not assessed'
    : !v ? 'review in progress' : v.reviewer_error ? 'the reviewer produced no verdict' : 'no acceptance rows in its verdict'); }
  return {src, why};
}
function acceptRows(){
  const lr = acceptSource().src, rows = arr(lr && lr.verdict && lr.verdict.acceptance).filter(r => r && typeof r === 'object');
  const sim = (a, b) => { a = String(a).toLowerCase(); b = String(b).toLowerCase(); if(a === b) return 1; if(a.includes(b) || b.includes(a)) return .9;
    const A = new Set(a.split(/\W+/).filter(Boolean)), B = new Set(b.split(/\W+/).filter(Boolean)); let n = 0; A.forEach(x => { if(B.has(x)) n++; }); return n / Math.max(1, Math.max(A.size, B.size)); };
  return arr(D.acceptance).map(c => { let best = null, s = 0; rows.forEach(r => { const x = sim(c, r && r.criterion || ''); if(x > s){ s = x; best = r; } }); return {c, r: s >= .5 ? best : null, round: lr && lr.round}; });
}
function costMeter(){ const b = D.budget_usd, c = D.cost_usd || 0; return b ? h`<div class="meter"><i style="width:${Math.min(100, 100*c/b).toFixed(1)}%;background:${raw(c > b*.85 ? 'var(--err)' : 'var(--accent)')}"></i></div>` : ''; }
function counts(){ const ts = D.tasks, by = s => ts.filter(t => t.state.status === s).length;
  return {total: ts.length, done: by('done'), running: by('running'), failed: by('failed'), blocked: by('blocked'), gated: by('awaiting_approval'), pending: by('pending'), skipped: by('skipped')}; }
function elapsed(){ const end = D.finished && D.status !== 'running' ? D.finished : nowS(); return D.created ? end - D.created : null; }

KINDS.overview = {
  title: () => 'Overview', icon: () => I.home, flex: false,
  mount(t){ t.r = {}; const doc = region(t.el, 'doc'); ['head','tiles','checks','dod','adv','grid','board','timeline','ctx'].forEach(k => t.r[k] = region(doc, k === 'grid' ? 'grid2' : '')); t.r.grid.style.marginBottom = '14px'; t.r.board.style.marginBottom = '14px'; t.r.timeline.style.marginBottom = '14px'; },
  update(t){
    const c = counts(), live = D.status === 'running' && D.alive, cap = D.capacity || {}, M = D.metrics || {};
    patch(t.r.head, h`<div class="doc-h"><div><h1>${D.job} ${pill(D.status, live)}</h1><div class="sub">${D.goal || ''}</div>
      <div class="chips" style="margin-top:8px">${D.branch ? h`<span class="chip mono">${I.branch}${D.branch}</span>` : ''}${D.base ? h`<span class="chip mono">base ${D.base}${D.base_sha ? ' @ ' + D.base_sha : ''}</span>` : ''}
      ${D.mode ? h`<span class="chip">${D.mode === 'orca' ? 'Orca workers' : 'headless'}</span>` : ''}${D.repo ? h`<span class="chip mono" title="${D.repo}">${String(D.repo).split('/').slice(-2).join('/')}</span>` : ''}</div></div></div>`);
    const seg = (n, col) => h`<i style="width:${(100*n/Math.max(c.total,1)).toFixed(1)}%;background:${raw(col)}"></i>`;
    patch(t.r.tiles, h`<div class="tiles">
      <div class="tile"><div class="k">Progress</div><div class="v">${c.done}<small> / ${c.total}</small></div><div class="s">${c.running} running · ${c.failed + c.blocked} failed/blocked</div><div class="meter">${seg(c.done,'var(--ok)')}${seg(c.running,'var(--run)')}${seg(c.failed,'var(--err)')}${seg(c.blocked,'var(--warn)')}${seg(c.gated,'var(--gate)')}</div></div>
      <div class="tile"><div class="k">Elapsed</div><div class="v"><span data-from="${D.created || ''}" data-to="${D.finished && D.status !== 'running' ? D.finished : ''}"></span></div><div class="s">${D.created ? 'started ' + clockShort(D.created) : 'not started'}</div></div>
      <div class="tile"><div class="k">Cost</div><div class="v">${money(D.cost_usd)}</div><div class="s">${D.budget_usd ? 'of ' + money(D.budget_usd) + ' budget' : 'no budget set'}</div>${costMeter()}</div>
      <div class="tile"><div class="k">Parallel</div><div class="v">${c.running}<small> / ${cap.cap ?? D.max_parallel ?? '–'}</small></div><div class="s">${cap.limited_by ? 'capped by ' + human(cap.limited_by) : 'slots in use'}${cap.mem_available_gb != null ? ' · ' + cap.mem_available_gb + ' GB free' : ''}</div></div>
      <div class="tile"><div class="k">Manager</div><div class="v">${D.manager.enabled ? String(D.manager.interventions || 0) : 'off'}</div><div class="s">${D.manager.enabled ? 'interventions' + (M.auto_triaged ? ' · ' + M.auto_triaged + ' by rule' : '') : 'manager disabled'}</div></div>
      <div class="tile"><div class="k">Review</div><div class="v">${D.review_rounds_used ?? (D.review||[]).length}<small> / ${D.review_rounds_max ?? 2}</small></div><div class="s">${(() => { const r = latestReview(); return r ? 'last: ' + human((r.verdict||{}).verdict || '…') : 'no round yet'; })()}</div></div>
    </div>`);
    patch(t.r.dod, D.definition_of_done ? h`<div class="card" style="margin-bottom:14px"><div class="card-h">Definition of done <span class="r">the user's own words</span></div><div class="card-b"><div class="quote">${str(D.definition_of_done)}</div></div></div>` : '');
    const ap = advEntries('plan'), af = advEntries('final'), lastA = af[af.length-1] || ap[ap.length-1];
    patch(t.r.adv, ap.length || af.length ? h`<div class="card" style="margin-bottom:14px"><div class="card-h">${I.owl} Advisor<span class="r"><span class="chip link" data-act="open" data-kind="advisor" data-arg="plan" tabindex="0" role="link">plan check</span> <span class="chip link" data-act="open" data-kind="advisor" data-arg="final" tabindex="0" role="link">final check${af.length ? '' : ' (not yet)'}</span></span></div><div class="card-b">
      ${lastA ? h`<div class="sect-lbl">${af.length ? 'Latest final check' : 'Plan check'}</div><div style="margin-bottom:6px">${advVerdictPill(lastA)} <span class="muted">${lastA.summary || lastA.cause || ''}</span></div>${lastA.done_restated ? h`<div class="sect-lbl">Its reading of your definition of done</div><div class="quote" style="border-left-color:var(--gate)">${lastA.done_restated}</div>` : ''}
        ${arr(lastA.findings).length ? h`<div class="ev-txt" style="margin-top:6px">${plural(arr(lastA.findings).length, 'finding')}: open the ${af.length ? 'final' : 'plan'} check for files and suggestions.</div>` : ''}` : h`<div class="dim small">No plan check recorded.</div>`}</div></div>` : '');
    const sk = arr(D.skipped_checks), asm = arr(D.assumptions);
    patch(t.r.checks, h`${sk.length ? h`<div class="card warn-card" style="margin-bottom:14px;border-color:var(--warn)"><div class="card-h" style="color:var(--warn)">${sev('major').i} Checks skipped or overridden<span class="r">not run: don't read them as passed</span></div><div class="card-b">${listOr(sk, x => li(x))}</div></div>` : ''}
      ${asm.length ? h`<div class="card" style="margin-bottom:14px;border-color:var(--gate)"><div class="card-h" style="color:var(--gate)">Unconfirmed (the planner chose for you)<span class="r">put each to the user; run refuses the plan until they are settled</span></div><div class="card-b">${listOr(asm, x => li(x))}</div></div>` : ''}`);
    const acc = acceptRows(), accSrc = acceptSource();
    const accHtml = acc.length ? h`<ul class="clean">${acc.map(a => h`<li title="${a.r ? a.r.evidence || '' : ''}"><span class="i">${a.r ? okIcon(a.r.met) : sicon('pending')}</span><span class="t">${a.c}${a.r && a.r.evidence ? h`<div class="ev-txt">${String(a.r.evidence).slice(0,240)}</div>` : ''}</span></li>`)}</ul>` : h`<div class="dim small">None</div>`;
    const met = acc.filter(a => a.r && a.r.met).length;
    patch(t.r.grid, h`${card('Acceptance', accHtml, acc.some(a => a.r) ? `${met}/${acc.length} met · round ${acc[0].round}${accSrc.why ? ' · ' + accSrc.why : ''}` : accSrc.why || 'not reviewed yet')}
      <div class="stack">${card('Decisions', listOr(D.decisions, x => arr(D.decisions_added).includes(x) ? h`<li><span class="t">${str(x)} <span class="chip c" style="--c:var(--gate)">your answer during the job</span></span></li>` : li(x)), arr(D.decisions_added).length ? plural(arr(D.decisions_added).length, 'answer') + ' added during the job' : '')}${card('Non-goals', listOr(D.non_goals, x => li(x)))}</div>
      ${card('Final verify', listOr(D.verify, x => h`<li><span class="t"><span class="code-inline">${x}</span></span></li>`))}
      ${card('Capacity & budget', h`<dl class="kvs">${[['parallel cap', cap.cap ?? D.max_parallel ?? '–'], ['limited by', cap.limited_by || '–'], ['memory free', cap.mem_available_gb != null ? cap.mem_available_gb + ' GB' : '–'], ['cpus', cap.cpus ?? '–'],
        ...Object.entries(cap.limits || {}).map(([k, v]) => ['limit: ' + human(k), str(v)]),
        ['budget', D.budget_usd ? money(D.budget_usd) : 'none'], ['spent', money(D.cost_usd)], ['left', D.budget_usd ? money(Math.max(0, D.budget_usd - (D.cost_usd||0))) : '–'], ['branch', D.branch || '–'], ['base', (D.base || '–') + (D.base_sha ? ' @ ' + D.base_sha : '')],
        ['approved', arr(D.approved).join(', ') || 'none'], ['screenshots', D.screenshots_root || '–']].map(([k, v]) => h`<dt>${k}</dt><dd title="${v}">${v}</dd>`)}</dl>`)}`);
    const W = waves();
    patch(t.r.board, card('Task graph', W.length ? h`<div class="board">${W.map((w, i) => h`<div class="wave"><div class="wave-h"><span>Wave ${i+1}</span><span>${w.filter(x => x.state.status === 'done').length}/${w.length}</span></div>${w.map(tcard)}</div>`)}
      ${(D.extra||[]).length ? h`<div class="wave"><div class="wave-h"><span>Replaced / skipped</span></div>${D.extra.map(tcard)}</div>` : ''}</div>` : h`<div class="dim small">No tasks in the plan.</div>`, `${plural(c.total, 'task')} in ${plural(W.length, 'wave')}`));
    const started = allTasks().filter(x => x.state.started).sort((a, b) => a.state.started - b.state.started);
    const t0 = D.created || (started[0] && started[0].state.started) || nowS(), t1 = Math.max(nowS(), ...started.map(x => x.state.ended || 0)), span = Math.max(t1 - t0, 1);
    patch(t.r.timeline, card('Timeline', started.length ? h`<div class="tl">${started.map(x => { const s = x.state, a = s.started, e = s.ended && s.status !== 'running' ? s.ended : null;
      return h`<div class="tlrow" data-act="open" data-kind="task" data-arg="${x.id}" tabindex="0" role="button"><div class="tlname" title="${x.id}">${midTrunc(x.id, 24)}</div><div class="tltrack"><div class="tlbar ${s.status==='running'?'running':''}" style="--c:${raw(stt(s.status).c)};left:${(100*(a-t0)/span).toFixed(2)}%;width:${raw(e ? (100*(e-a)/span).toFixed(2) + '%' : `calc(100% - ${(100*(a-t0)/span).toFixed(2)}%)`)}"></div></div><div class="tld"><span data-from="${a}" data-to="${e || ''}"></span></div></div>`; })}</div>` : h`<div class="empty">Nothing has started yet.</div>`, null, true));
    const cl = D.cleanup;
    patch(t.r.ctx, h`${arr(D.quality_bar).length || arr(D.bar).length ? h`<div style="margin-bottom:14px">${card('Quality bar', h`${arr(D.quality_bar).length ? listOr(D.quality_bar, x => li(x)) : ''}${arr(D.bar).length ? h`<div class="sect-lbl" style="margin-top:8px">Raised during the job</div>${barList(D.bar)}` : ''}`)}</div>` : ''}
      ${cl ? card('Cleanup pass', h`<dl class="kvs"><dt>model</dt><dd>${cl.config || '–'}</dd><dt>result</dt><dd>${cl.reverted ? 'reverted' : str(cl.result || '–')}</dd>${cl.head ? h`<dt>head</dt><dd class="mono">${String(cl.head).slice(0, 10)}</dd>` : ''}</dl>
        ${cl.reverted ? h`<div class="ev-txt" style="color:var(--warn);white-space:pre-wrap">${str(cl.reverted)}</div>` : ''}${cl.report && cl.report.summary ? h`<div class="ev-txt" style="white-space:pre-wrap">${str(cl.report.summary)}</div>` : ''}`) : ''}`);
  }
};
function tcard(t){ const s = t.state || {}, st = s.status || 'pending', steps = arr(s.steps);
  return h`<button class="tcard" style="--c:${raw(stt(st).c)}" data-act="open" data-kind="task" data-arg="${t.id}" title="${t.id}"><div class="tt">${sicon(st)}<span>${midTrunc(t.id, 30)}</span></div><div class="ts">${t.title || ''}</div>
    <div class="tf"><span>${steps.length ? steps.map(x => x.config).join(' → ') : [t.tier, t.type].filter(Boolean).join(' · ')}</span><span>${s.started ? h`<span data-from="${s.started}" data-to="${s.status==='running' ? '' : s.ended || ''}"></span>` : ''}${s.cost_usd ? ' · ' + money(s.cost_usd) : ''}</span></div></button>`; }

const SUBS = [['overview','Overview'],['log','Live log'],['brief','Brief'],['report','Report & issues'],['calls','Model path & calls'],['shots','Screenshots']];
KINDS.task = {
  title: a => midTrunc(a, 32), icon: a => { const t = taskById(a); return sicon(t ? t.state.status : 'pending'); }, tip: a => { const t = taskById(a); return t ? `${a} — ${t.title || ''}` : a; }, flex: true,
  mount(t){
    t.head = region(t.el); t.subtabs = region(t.el, 'subtabs'); const body = region(t.el, 'subbody'); t.p = {};
    SUBS.forEach(([k]) => { t.p[k] = region(body, 'subpane' + (k === 'log' ? ' flex' : '')); });
    t.log = LogView(() => `/api/job/${encodeURIComponent(JOB)}/log/${encodeURIComponent(t.arg)}`, {empty: 'No log yet. It appears once the task starts.'});
    t.p.log.appendChild(t.log.el);
    t.brief = {text: null}; t.sub = t.sub || 'overview';
    t.setSub = s => { t.sub = s; runUpdate(t); syncHash(true); };
    t.subtabs.addEventListener('click', e => { const b = e.target.closest('button[data-sub]'); if(b) t.setSub(b.dataset.sub); });
  },
  update(t){
    const x = taskById(t.arg);
    if(!x){ patch(t.head, h`<div class="taskhead"><h1>${t.arg}</h1><div class="sub">This task is not in the job (any more).</div></div>`); return; }
    const s = x.state || {}, rep = (s.report && typeof s.report === 'object') ? s.report : {}, L = levels(), shots = allShots().filter(z => arr(z.tasks).includes(x.id));
    patch(t.head, h`${crumbs([{t: D.job, go:['overview','']}, {t: L[x.id] != null ? 'Wave ' + (L[x.id]+1) : 'Replaced / skipped'}, {t: x.id}])}
      <div class="taskhead"><h1>${sicon(s.status || 'pending')}${x.id} ${pill(s.status || 'pending', s.status === 'running')}</h1><div class="sub">${x.title || ''}</div>
      <div class="row2">${x.tier ? h`<span class="chip">${x.tier}</span>` : ''}${x.type ? h`<span class="chip mono">${x.type}</span>` : ''}${x.gate ? h`<span class="chip c" style="--c:var(--gate)">gate: ${x.gate}</span>` : ''}
      ${s.added_by === 'manager' ? h`<span class="chip c" style="--c:var(--split)">added by manager</span>` : ''}${s.manager_retries ? h`<span class="chip c" style="--c:var(--split)">manager retry ×${s.manager_retries}</span>` : ''}
      ${s.slot != null ? h`<span class="chip">slot ${s.slot}</span>` : ''}<span class="sp"></span>${s.started ? h`<span class="chip">${I.clock}<span data-from="${s.started}" data-to="${s.status === 'running' ? '' : s.ended || ''}"></span></span>` : ''}<span class="chip">${I.coin}${money(s.cost_usd)}${s.coord_cost_usd ? ' + ' + money(s.coord_cost_usd) + ' coord' : ''}</span></div>
      ${s.status === 'awaiting_approval' ? h`<div class="small" style="margin-top:8px">Waiting for your approval${x.gate ? ' (' + x.gate + ')' : ''}. ${cmdBox(`${CLI()} resume ${shq(D.job)} --approve ${shq(x.id)}`)}</div>` : ''}</div>`);
    const nIss = arr(rep.open_issues).length, nCalls = taskCalls(x.id).length || arr(s.steps).length;
    patch(t.subtabs, SUBS.map(([k, l]) => h`<button data-sub="${k}" class="${t.sub===k?'on':''}">${l}${k==='report' && nIss ? h`<span class="n">${nIss}</span>` : ''}${k==='calls' && nCalls ? h`<span class="n">${nCalls}</span>` : ''}${k==='shots' && shots.length ? h`<span class="n">${shots.length}</span>` : ''}</button>`));
    Object.entries(t.p).forEach(([k, el]) => el.classList.toggle('on', k === t.sub));
    if(t._shownSub !== t.sub){ t._shownSub = t.sub; requestAnimationFrame(() => { const b = $('button.on', t.subtabs); if(b) t.subtabs.scrollLeft = Math.max(0, b.offsetLeft - (t.subtabs.clientWidth - b.offsetWidth) / 2); }); }
    if(t.sub === 'overview'){
      const chipsOf = (xs, mono) => { xs = arr(xs); return xs.length ? h`<div class="chips">${xs.map(v => h`<span class="chip ${mono?'mono':''}">${str(v)}</span>`)}</div>` : h`<div class="dim small">None</div>`; };
      const deps = arr(x.depends_on), done = arr(x.done);
      patch(t.p.overview, h`<div class="doc"><div class="grid2">
        <div class="stack">${card('Goal', h`<div style="white-space:pre-wrap">${str(x.goal || '')}</div>`)}${card('Done looks like', listOr(done, d => li(d, sicon(s.status === 'done' ? 'done' : 'pending'))), pluralW(done.length, 'criterion', 'criteria'))}
          ${card('Verify', listOr(x.verify, v => h`<li><span class="t"><span class="code-inline">${str(v)}</span></span></li>`))}
          ${arr(x.notes).length ? card('Notes from the manager', listOr(x.notes, n => li(n, I.brain))) : ''}
          ${s.skip_reason ? card('Skipped', h`<div style="white-space:pre-wrap">${str(s.skip_reason)}</div>`) : ''}
          ${s.error ? card('Error', h`<pre class="block">${stripAnsi(str(s.error))}</pre>`) : ''}</div>
        <div class="stack">${card('Owns', chipsOf(x.owns, true))}${arr(x.reads).length ? card('Reads first', chipsOf(x.reads, true)) : ''}
          ${arr(s.split_into).length ? card('Split into', h`<div class="chips">${arr(s.split_into).map(taskChip)}</div>`, 'replaced by the manager') : ''}
          ${card('Depends on', deps.length ? h`<div class="chips">${deps.map(taskChip)}</div>` : h`<div class="dim small">Nothing — starts in wave 1</div>`)}
          ${(() => { const dn = D.tasks.filter(o => arr(o.depends_on).includes(x.id)); return dn.length ? card('Unblocks', h`<div class="chips">${dn.map(o => taskChip(o.id))}</div>`) : ''; })()}
          ${x.interfaces ? card('Interfaces', h`<div style="white-space:pre-wrap" class="small">${str(x.interfaces)}</div>`) : ''}
          ${arr(x.ui).length ? card('UI pages', h`<div class="chips">${arr(x.ui).map(p => h`<span class="chip mono">${(p && (p.name || p.path)) || ''}</span>`)}</div>`) : ''}
          ${card('Timing', h`<dl class="kvs"><dt>started</dt><dd>${s.started ? clock(s.started) : '–'}</dd><dt>ended</dt><dd>${s.ended ? clock(s.ended) : '–'}</dd><dt>result</dt><dd>${str(s.result || '–')}</dd><dt>slot</dt><dd>${s.slot ?? '–'}</dd></dl>`)}
          ${arr(s.files).length ? card('Files merged', listOr(s.files, f => h`<li><span class="t mono small">${f}</span></li>`), String(arr(s.files).length)) : ''}
          ${arr(s.claimed).length ? card('Files claimed', chipsOf(s.claimed, true), 'no task owned them') : ''}
          ${s.merge_verify && typeof s.merge_verify === 'object' ? card('Merge conflict resolved by the integrator', h`${arr(s.merge_verify.files).length ? chipsOf(s.merge_verify.files, true) : ''}<div class="small" style="margin-top:6px">${s.merge_verify.ok ? (arr(s.merge_verify.cmds).length ? h`verified with ${arr(s.merge_verify.cmds).map((c, i) => h`${i ? ', ' : ''}<span class="code-inline">${str(c)}</span>`)}` : 'no verify command to run') : h`<span style="color:var(--err)">failed verify: <span class="code-inline">${str(s.merge_verify.cmd || '')}</span></span>${s.merge_verify.tail ? h`<pre class="block">${stripAnsi(str(s.merge_verify.tail))}</pre>` : ''}`}</div>`, s.merge_verify.ok ? 'verified' : 'failed') : ''}</div></div></div>`);
    }
    if(t.sub === 'log') t.log.load();
    if(t.sub === 'brief') loadBrief(t);
    if(t.sub === 'report'){
      const rel = findings().filter(f => f.file && arr(x.owns).some(p => owns(f.file, p)));
      const known = ['kind','type','issue','text','description','summary','question','file','path','files','blocks_acceptance'];
      const issue = o => { if(!o || typeof o !== 'object') return li(o, sev('minor').i); const kind = o.kind || o.type || 'issue', blocks = !!o.blocks_acceptance;
        return h`<li><span class="i">${blocks ? sev('blocker').i : kind === 'needs_decision' || kind === 'needs_file' ? sev('major').i : sev('minor').i}</span><span class="t"><span class="chip mono">${human(kind)}</span>${blocks ? h` <span class="chip c" style="--c:var(--err)">blocks acceptance</span>` : ''} ${str(o.issue || o.text || o.description || o.summary || o.question || '')}${o.file || o.path ? h` <span class="code-inline">${str(o.file || o.path)}</span>` : ''}
          ${arr(o.files).length ? h`<div class="chips" style="margin-top:4px">${arr(o.files).map(f => h`<span class="chip mono">${str(f)}</span>`)}</div>` : ''}
          ${Object.entries(o).filter(([k]) => !known.includes(k)).map(([k, v]) => h`<div class="ev-txt"><b>${human(k)}:</b> ${str(v)}</div>`)}</span></li>`; };
      patch(t.p.report, h`<div class="doc"><div class="stack">
        ${card('Report', rep.summary ? h`<div style="white-space:pre-wrap">${str(rep.summary)}</div>` : h`<div class="dim small">${s.status === 'running' ? 'The task is still running.' : 'No report.'}</div>`)}
        ${rep.interfaces ? card('Interfaces', h`<div style="white-space:pre-wrap" class="small">${str(rep.interfaces)}</div>`) : ''}
        ${card('Open issues', listOr(rep.open_issues, issue), String(nIss))}
        ${rel.length ? card('Review findings in its files', h`<div>${rel.map(probRow)}</div>`, String(rel.length), true) : ''}
        ${s.final_message ? card('Final message', h`<div class="md">${raw(md(str(s.final_message)))}</div>`) : ''}
        ${Object.keys(rep).filter(k => !['summary','interfaces','open_issues'].includes(k)).length ? card('Other report fields', h`<div class="json">${jsonView(Object.fromEntries(Object.entries(rep).filter(([k]) => !['summary','interfaces','open_issues'].includes(k))), null, 0, true)}</div>`, null, true) : ''}
      </div></div>`);
    }
    if(t.sub === 'calls'){
      const calls = taskCalls(x.id), steps = arr(s.steps).filter(z => z && typeof z === 'object');
      patch(t.p.calls, h`<div class="doc"><div class="stack">
        ${card('Model path', steps.length ? h`<ul class="clean">${steps.map((st, i) => h`<li><span class="i">${okIcon(st.ok)}</span><span class="t"><b class="mono">${i+1}. ${st.config}</b> ${st.ok ? h`<span class="muted">passed</span>` : st.cause ? h`<span class="chip c" style="--c:var(--err)">${human(st.cause)}</span>` : ''}
          ${st.evidence ? h`<div class="ev-txt">${String(st.evidence).slice(0,400)}</div>` : ''}<div class="ev-txt">${[st.cost_usd != null ? money(st.cost_usd) : '', st.classifier_cost_usd ? 'classifier ' + money(st.classifier_cost_usd) : '', st.metrics && st.metrics.turns != null ? st.metrics.turns + ' turns' : '', st.duration_s ? dur(st.duration_s) : ''].filter(Boolean).join(' · ')}</div></span></li>`)}</ul>` : h`<div class="dim small">No attempts yet.</div>`, money(s.cost_usd))}
        ${card('Model calls', callsTable(calls, 'task:' + x.id), plural(calls.length, 'call') + ' · ' + money(calls.reduce((a, c) => a + (Number(c.cost_usd) || 0), 0)), true)}</div></div>`);
    }
    if(t.sub === 'shots') patch(t.p.shots, shots.length ? gallery(shots, true) : h`<div class="empty" style="padding:24px">No screenshots for this task. ${D.ui_config ? 'They appear after a review round runs the UI review.' : 'This plan has no "ui" section, so no UI review runs.'}</div>`);
  }
};
async function loadBrief(t){
  if(t.brief.busy) return; t.brief.busy = true;
  try{ const r = await fetch(`/api/job/${encodeURIComponent(JOB)}/brief/${encodeURIComponent(t.arg)}`, {cache:'no-store'}); const txt = r.ok ? await r.text() : '';
    if(txt !== t.brief.text || !t.p.brief._h){ t.brief.text = txt;
      safe('brief', () => patch(t.p.brief, txt ? h`<div class="doc"><div class="md">${raw(md(txt))}</div></div>` : h`<div class="empty" style="padding:24px">No brief yet. The brief is written when the task starts.</div>`)); } }
  catch(e){} finally{ t.brief.busy = false; }
}
const CALLS_CAP = 300, CALLS_ALL = new Set();
function callsTable(calls, key){
  calls = arr(calls).filter(c => c && typeof c === 'object');
  if(!calls.length) return h`<div class="empty">No calls recorded.</div>`;
  const tok = v => v == null ? '' : v >= 1e6 ? (v/1e6).toFixed(1) + 'M' : v >= 1e3 ? (v/1e3).toFixed(1) + 'k' : String(v);
  const all = CALLS_ALL.has(key) || calls.length <= CALLS_CAP, shown = all ? calls.slice().reverse() : calls.slice(-CALLS_CAP).reverse();
  return h`<div class="tblwrap"><table class="tbl"><thead><tr><th>time</th><th>role</th><th>task</th><th>model</th><th class="n">cost</th><th class="n">turns</th><th class="n">input</th><th class="n">cache read</th><th>result</th></tr></thead><tbody>
    ${shown.map(c => h`<tr ${c.task && taskById(String(c.task).split(',')[0]) ? raw(`class="click" data-act="open" data-kind="task" data-arg="${esc(String(c.task).split(',')[0])}" tabindex="0"`) : ''}><td class="n">${clock(c.t)}</td><td>${human(c.role)}</td><td class="mono small">${c.task || ''}</td><td class="mono small">${c.config || c.model_id || ''}</td><td class="n">${money(c.cost_usd)}</td><td class="n">${c.turns ?? ''}</td><td class="n">${tok(c.input)}</td><td class="n">${tok(c.cache_read)}</td>
    <td>${c.error ? h`<span class="chip c" style="--c:var(--err)">${str(c.error)}</span>` : c.ok === false ? h`<span class="chip c" style="--c:var(--err)">failed</span>` : h`<span class="dim">ok</span>`}</td></tr>`)}</tbody></table></div>
    ${all ? '' : h`<div class="side-pad" style="padding-top:8px"><button class="tog" data-act="calls-all" data-key="${key}">Showing the latest ${CALLS_CAP}. Show all ${calls.length} calls</button></div>`}`;
}
/* gallery */
function gallery(shots, compact){
  if(!shots.length) return h`<div class="empty" style="padding:24px">No screenshots match the filters.</div>`;
  const byRound = new Map(); shots.forEach(s => { if(!byRound.has(s.round)) byRound.set(s.round, []); byRound.get(s.round).push(s); });
  const rounds = Array.from(byRound.keys()).sort((a, b) => b - a);
  return h`<div class="gal">${rounds.map(rn => { const ss = byRound.get(rn), R = ss[0]._r || {}, v = R.verdict || {}, pages = new Map();
    ss.forEach(s => { const k = s.name || s.path || '?'; if(!pages.has(k)) pages.set(k, []); pages.get(k).push(s); });
    return h`<section class="gal-round"><div class="gal-rh"><h2>Round ${rn}</h2>${v.verdict ? verdictPill(v.verdict) : R.error ? h`<span class="chip c" style="--c:var(--err)">could not run</span>` : h`<span class="chip">no UI verdict</span>`}<span class="dim small">${plural(ss.length, 'shot')}${R.url ? ' · ' + R.url : ''}</span></div>
      ${v.summary && !compact ? h`<div class="muted small" style="margin:-4px 0 10px;max-width:900px">${v.summary}</div>` : ''}
      ${Array.from(pages.entries()).map(([pg, list]) => h`<div class="gal-page"><div class="gal-ph">${I.image}<b>${pg}</b><span class="mono dim">${list[0].path || ''}</span>${arr(list[0].tasks).length ? h`<span class="dim">· ${arr(list[0].tasks).join(', ')}</span>` : ''}</div>
        <div class="shots">${(r => list.sort((a, b) => (r(a) - r(b)) || String(a.viewport).localeCompare(String(b.viewport))))(stepRank(list)).map(shotCard)}</div></div>`)}</section>`; })}</div>`;
}
function shotCard(s){
  const ob = shotObs(s), st = s.status, stc = st == null ? 'var(--dim)' : st >= 400 ? 'var(--err)' : st >= 300 ? 'var(--warn)' : 'var(--ok)';
  const cnt = (n, l, c) => n ? h`<span class="chip c" style="--c:${raw(c)}" title="${l}">${n} ${l}</span>` : '';
  const e = shotErrs(s), np = arr(s.page_errors).length, nr = arr(s.failed_requests).length, ns = arr(s.step_errors).length;
  return h`<button class="shot" data-act="shot" data-id="${s.id}" data-round="${s.round}" title="${s.id}"><div class="th ${s.viewport === 'mobile' ? 'mobile' : ''}"><img loading="lazy" alt="${s.name || ''} ${s.viewport || ''}" src="${shotUrl(s)}">${ob ? h`<span class="ok-badge">${okPill(ob.ok)}</span>` : shotReviewed(s) ? h`<span class="ok-badge"><span class="chip c" style="--c:var(--warn)" title="The UI reviewer's verdict does not mention this shot">unreviewed</span></span>` : ''}</div>
    <div class="sb"><div class="sn">${s.viewport === 'mobile' ? I.mobile : I.desktop}<span>${s.viewport || ''}${s.step ? ' · ' + s.step : ''}</span></div>
    <div class="ss"><span class="chip c mono" style="--c:${raw(stc)}">${st == null ? 'no status' : 'HTTP ' + st}</span>${cnt(arr(s.console).length, 'console', 'var(--warn)')}${cnt(np, np === 1 ? 'page error' : 'page errors', 'var(--err)')}${cnt(nr, nr === 1 ? 'request' : 'requests', 'var(--err)')}${cnt(ns, ns === 1 ? 'step' : 'steps', 'var(--err)')}${!e ? h`<span class="chip c" style="--c:var(--ok)">clean</span>` : ''}</div></div></button>`;
}
function shotFilters(labels){
  validSF();
  const R = arr(D.ui_rounds).map(r => r.round), T = Array.from(new Set(allShots().flatMap(s => arr(s.tasks)))).sort();
  const opt = (v, l, cur) => h`<option value="${v}" ${String(cur) === String(v) ? raw('selected') : ''}>${l}</option>`;
  const sels = [h`<select class="sel-in" data-filter="round" aria-label="Round">${opt('all','All rounds',SF.round)}${R.map(r => opt(r, 'Round ' + r, SF.round))}</select>`,
    h`<select class="sel-in" data-filter="viewport" aria-label="Viewport">${opt('all','All viewports',SF.viewport)}${opt('desktop','Desktop',SF.viewport)}${opt('mobile','Mobile',SF.viewport)}</select>`,
    h`<select class="sel-in" data-filter="task" aria-label="Task">${opt('all','All tasks',SF.task)}${T.map(x => opt(x, x, SF.task))}</select>`];
  if(labels) return h`<label><span>Round</span>${sels[0]}</label><label><span>Viewport</span>${sels[1]}</label><label><span>Task</span>${sels[2]}</label>`;
  return h`${sels}`;
}
KINDS.shots = {
  title: () => 'Screenshots', icon: () => I.image, flex: true,
  mount(t){ t.bar = region(t.el, 'toolbar'); t.body = region(t.el); t.body.style.cssText = 'flex:1;overflow:auto;min-height:0'; },
  update(t){
    const all = allShots(), f = filteredShots(), filtered = SF.round !== 'all' || SF.viewport !== 'all' || SF.task !== 'all';
    patch(t.bar, h`<span class="ttl">${plural(f.length, 'shot')}${f.length !== all.length ? ' of ' + all.length : ''}</span>${filtered ? h`<button class="tog" data-act="clear-filters">Clear filters</button>` : ''}<span class="sp"></span>${shotFilters()}`);
    patch(t.body, all.length ? gallery(f) : uiEmpty());
  }
};
function uiEmpty(){
  const rs = arr(D.ui_rounds), last = rs[rs.length-1];
  return h`<div class="welcome"><div class="box"><div class="logo">${I.images}</div><h2>No screenshots yet</h2>
    <p>${!D.ui_config ? 'This plan has no "ui" section, so the UI review does not run.' : last && last.skipped ? 'The last UI review was skipped: ' + last.skipped : last && last.error ? 'The last UI review could not run: ' + last.error : 'Screenshots are taken during each review round, once every task has merged and final verify passes.'}</p>
    ${D.screenshots_root ? h`<p>Folder (git-ignored): ${cmdBox(D.screenshots_root)}</p>` : ''}</div></div>`;
}
/* advisor */
const advEntries = stage => arr(D && D.advice && D.advice[stage]).filter(a => a && typeof a === 'object');
const advVerdictPill = a => a.error ? pill('failed', false, 'no usable answer') : pill(a.verdict === 'proceed' ? 'done' : 'awaiting_approval', false, a.verdict || '?');
function findingFull(f){ const s = sev(f.severity), loc = f.file ? f.file + (f.line ? ':' + f.line : '') : '';
  return h`<li><span class="i">${s.i}</span><span class="t"><b>${s.label}</b> ${f.issue || ''}${loc ? h` <span class="code-inline">${loc}</span>` : ''}${f.confidence ? h` <span class="dim small">· ${f.confidence} confidence</span>` : ''}${f.suggestion ? h`<div class="ev-txt" style="white-space:pre-wrap"><b>Suggestion:</b> ${f.suggestion}</div>` : ''}</span></li>`; }
function barList(xs){ xs = arr(xs); return xs.length ? h`<ul class="clean">${xs.map(b => b && typeof b === 'object' ? h`<li><span class="t">${str(b.criterion || b.text || b)}${b.scope ? h` <span class="chip mono">${str(b.scope)}</span>` : ''}${b.why ? h`<div class="ev-txt">${str(b.why)}</div>` : ''}${b.source ? h`<div class="ev-txt">from ${str(b.source)}</div>` : ''}</span></li>` : li(b))}</ul>` : h`<div class="dim small">None</div>`; }
function adviceCard(a, i, stage, n){
  const title = (stage === 'plan' ? 'Plan check' : 'Final check') + (n > 1 ? ' ' + (i + 1) + ' of ' + n : '');
  if(a.error) return card(title, h`<div>${advVerdictPill(a)} <span class="muted">${str(a.cause || (a.error === true ? '' : a.error))}</span></div>${a.t ? h`<div class="ev-txt">at ${clock(a.t)}</div>` : ''}`, a.config || '');
  const fs = arr(a.findings).filter(f => f && typeof f === 'object');
  return card(title, h`<div style="margin-bottom:8px">${advVerdictPill(a)} <span class="muted">${a.summary || ''}</span></div>
    <dl class="kvs" style="margin-bottom:10px"><dt>model</dt><dd>${a.config || '–'}</dd><dt>escalated</dt><dd>${a.escalated ? 'yes: re-asked at high after a clean pass at medium' : 'no'}</dd>${a.t ? h`<dt>at</dt><dd>${clock(a.t)}</dd>` : ''}</dl>
    <div class="grid2"><div><div class="sect-lbl">Your definition of done</div><div class="quote">${str(D.definition_of_done || '(none in the plan)')}</div></div>
      <div><div class="sect-lbl">The advisor's restatement</div><div class="quote" style="border-left-color:var(--gate)">${a.done_restated || '(not given)'}</div></div></div>
    <div class="sect-lbl" style="margin-top:12px">Findings (${fs.length})</div>${fs.length ? h`<ul class="clean">${fs.map(findingFull)}</ul>` : h`<div class="dim small">None</div>`}
    ${arr(a.raise_bar).length ? h`<div class="sect-lbl" style="margin-top:12px">Raised the bar</div>${barList(a.raise_bar)}` : ''}`, a.config || '');
}
KINDS.advisor = {
  title: a => a === 'final' ? 'Advisor: final check' : 'Advisor: plan check', icon: () => I.owl, flex: false,
  mount(t){ t.body = region(t.el, 'doc'); },
  update(t){
    const stage = t.arg === 'final' ? 'final' : 'plan', es = advEntries(stage), other = stage === 'plan' ? 'final' : 'plan';
    const rounds = arr(D.review).map((r, i) => [r, i]).filter(([r]) => r && r.advisor);
    const planOverride = typeof D.advice_plan_ok === 'string' && /^(approved by user|skipped:)/.test(D.advice_plan_ok);
    const finalOverride = rounds.length > 0 && rounds.every(([r]) => r.advisor.overridden_by_user);
    patch(t.body, h`<div class="doc-h"><div><h1>${I.owl} Advisor: ${stage === 'plan' ? 'plan check' : 'final check'}</h1><div class="sub">${stage === 'plan' ? 'A second opinion on the plan before any work starts.' : 'A second opinion on the merged work before hand-over.'} Compare its restatement of done with yours to spot a misreading.</div></div>
      <div class="chips"><span class="chip link" data-act="open" data-kind="advisor" data-arg="${other}" tabindex="0" role="link">${I.owl} ${other} check</span></div></div>
      <div class="stack">${planOverride && stage === 'plan' ? banner('var(--warn)', sev('major').i, h`<b>Overridden by you</b> (--approve advisor:plan): the plan ran without the advisor's sign-off. ${str(D.advice_plan_ok)}`) : ''}
      ${finalOverride && stage === 'final' ? banner('var(--warn)', sev('major').i, h`<b>Overridden by you</b> (--approve advisor:final): the work was handed over without the advisor's final check.`) : ''}
      ${es.length ? es.map((a, i) => adviceCard(a, i, stage, es.length)) : card(stage === 'plan' ? 'Plan check' : 'Final check', h`<div class="dim small">${stage === 'plan' ? (D.advice_plan_ok ? 'Plan check: ' + str(D.advice_plan_ok) : 'The advisor has not checked the plan yet.') : finalOverride ? 'No final check ran: you overrode it.' : 'No final check yet. It runs after the reviewer passes.'}</div>`)}
      ${stage === 'final' && rounds.length ? card('By review round', h`<ul class="clean">${rounds.map(([r, i]) => h`<li class="clickable" data-act="open" data-kind="review" data-arg="${String(i)}" tabindex="0" role="button"><span class="i">${I.owl}</span><span class="t">Round ${r.round}: ${r.advisor.overridden_by_user ? 'overridden by you' : r.advisor.error ? 'no usable answer' : r.advisor.verdict || '…'}${r.advisor.escalated ? ' (escalated)' : ''}<div class="ev-txt">${r.advisor.summary || ''}</div></span></li>`)}</ul>`) : ''}</div>`);
  }
};
KINDS.review = {
  title: a => `Review round ${(D && arr(D.review)[+a] && D.review[+a].round) || (+a + 1)}`, icon: () => I.review, flex: false,
  mount(t){ t.body = region(t.el, 'doc'); },
  update(t){
    const R = arr(D.review), r = R[+t.arg]; if(!r){ patch(t.body, h`<div class="empty">This review round does not exist.</div>`); return; }
    const v = r.verdict || {}, u = r.ui, a = r.advisor, acc = arr(v.acceptance).filter(x => x && typeof x === 'object'), ecs = arr(v.edge_cases).filter(x => x && typeof x === 'object');
    const fs = findings().filter(f => f._key.startsWith(t.arg + ':')), inProgress = !r.verdict && +t.arg === R.length - 1 && D.status === 'running';
    patch(t.body, h`<div class="doc-h"><div><h1>${I.review} Review round ${r.round || +t.arg + 1} ${v.verdict ? verdictPill(v.verdict) : r.final_verify_ok === false ? pill('failed', false, 'final verify failed') : inProgress ? pill('running', true, 'in progress') : pill('failed', false, 'no verdict')}</h1><div class="sub">${v.summary || (v.reviewer_error ? 'The reviewer produced no usable verdict.' : '')}</div></div></div>
      <div class="stack">
      ${card('Final verify', h`<div style="margin-bottom:8px">${r.final_verify_ok ? h`<span class="chip c" style="--c:var(--ok)">passed</span>` : h`<span class="chip c" style="--c:var(--err)">failed</span>`}</div>${r.final_verify_tail ? h`<pre class="block">${stripAnsi(r.final_verify_tail)}</pre>` : ''}`)}
      ${acc.length ? card('Acceptance', h`<ul class="clean">${acc.map(x => h`<li><span class="i">${okIcon(x.met)}</span><span class="t">${x.criterion}<div class="ev-txt">${x.evidence || ''}</div></span></li>`)}</ul>`, `${acc.filter(x => x.met).length}/${acc.length} met`) : ''}
      ${card('Findings', fs.length ? h`<div>${fs.map(f => probRow(f, true))}</div>` : h`<div class="empty">No findings.</div>`, String(fs.length), true)}
      ${ecs.length ? card('Edge cases', h`<div class="tblwrap"><table class="tbl"><thead><tr><th></th><th>case</th><th>evidence</th></tr></thead><tbody>${ecs.map(e => h`<tr><td>${okIcon(e.handled)}</td><td>${e.case}${e.file ? h`<div class="mono dim small">${e.file}</div>` : ''}</td><td class="small muted">${e.evidence}</td></tr>`)}</tbody></table></div>`, `${ecs.filter(e => !e.handled).length} unhandled`, true) : ''}
      ${u ? card('UI review', h`${u.ran ? '' : h`<div class="small" style="margin-bottom:8px">${u.skipped ? 'Skipped: ' + u.skipped : u.error ? 'Could not run: ' + u.error : inProgress ? 'Running…' : 'Not run.'}</div>`}
        ${u.verdict ? h`<div style="margin-bottom:8px">${verdictPill(u.verdict.verdict)} <span class="muted">${u.verdict.summary || ''}</span></div>
          <ul class="clean">${arr(u.verdict.pages).filter(p => p && typeof p === 'object').map(p => { const s = allShots().find(z => z.id === p.shot); return h`<li ${s ? raw(`class="clickable" data-act="shot" data-id="${esc(s.id)}" data-round="${esc(s.round)}" tabindex="0" role="button"`) : raw('title="this screenshot is not in the job state"')}><span class="i">${okIcon(p.ok)}</span><span class="t"><span class="mono small">${p.shot}</span><div class="ev-txt">${p.observations}</div></span></li>`; })}</ul>` : ''}
        ${arr(u.shots).length ? h`<div style="margin-top:8px"><span class="chip link" data-act="open" data-kind="shots" data-arg="" tabindex="0" role="link">${I.image} open ${plural(arr(u.shots).length, 'screenshot')}</span></div>` : ''}`, u.url || '') : ''}
      ${a ? card('Advisor final check', h`<div>${a.overridden_by_user ? 'Overridden by you (--approve advisor:final).' : a.error ? 'The advisor produced no usable answer.' : h`${advVerdictPill(a)} <span class="muted">${a.summary || ''}</span>${a.escalated ? h` <span class="chip">escalated to high</span>` : ''}`}</div>
        ${arr(a.findings).length ? h`<ul class="clean" style="margin-top:8px">${arr(a.findings).filter(f => f && typeof f === 'object').map(findingFull)}</ul>` : ''}
        <div style="margin-top:8px"><span class="chip link" data-act="open" data-kind="advisor" data-arg="final" tabindex="0" role="link">${I.owl} open the advisor's final check</span></div>`, a.config || '') : ''}
      ${r.fix ? card('Fix round', h`<div class="small">result: <b>${str(r.fix.result || '–')}</b></div>${arr(r.fix.steps).length ? h`<div class="chips" style="margin-top:6px">${arr(r.fix.steps).map(s => h`<span class="chip mono c" style="--c:${raw(s && s.ok?'var(--ok)':'var(--err)')}">${s && s.config}</span>`)}</div>` : ''}`) : ''}
      ${arr(r.bar_raised).length ? card('Bar raised', barList(r.bar_raised)) : ''}
      </div>`);
  }
};
function probRow(f, full){
  const s = sev(f.severity), loc = f.file ? f.file + (f.line ? ':' + f.line : '') : '';
  return h`<div class="prob" ${f._key ? raw(`data-act="open" data-kind="finding" data-arg="${esc(f._key)}" tabindex="0" role="button"`) : ''} title="${f.suggestion || ''}"><span class="pi">${s.i}</span><span class="pt2">${f.issue || ''}${loc ? h`<span class="pl mono">${loc}</span>` : ''}${f.confidence ? h`<span class="pl">${f.confidence} confidence</span>` : ''}${full && f.suggestion ? h`<div class="ev-txt">Suggestion: ${f.suggestion}</div>` : ''}</span></div>`;
}
KINDS.finding = {
  title: a => { const f = findings().find(x => x._key === a); return f && f.file ? f.file.split('/').pop() + (f.line ? ':' + f.line : '') : 'Finding'; }, icon: a => { const f = findings().find(x => x._key === a); return sev(f && f.severity).i; }, flex: false,
  mount(t){ t.body = region(t.el, 'doc'); t.code = {text: null, path: null}; },
  update(t){
    const f = findings().find(x => x._key === t.arg); if(!f){ patch(t.body, h`<div class="empty">This finding is no longer in the job state.</div>`); return; }
    const s = sev(f.severity), own = f.file ? ownersOf(f.file) : [];
    if(f.file && t.code.path !== f.file){ t.code.path = f.file; fetch(`/api/job/${encodeURIComponent(JOB)}/file?path=${encodeURIComponent(f.file.split(':')[0])}`).then(r => r.ok ? r.text() : null).then(x => { t.code.text = x; runUpdate(t); }).catch(() => {}); }
    let codeHtml = '';
    if(t.code.text != null){ const ls = t.code.text.split('\n'), ln = +f.line || 1, a = Math.max(0, ln - 12), b = Math.min(ls.length, ln + 12);
      codeHtml = card('Context', h`<div class="logscroll" style="max-height:420px"><pre class="code" style="counter-reset:ln ${a}">${ls.slice(a, b).map((l, i) => h`<span class="l ${a + i + 1 === ln ? 'hl' : ''}">${l}</span>`)}</pre></div>`, `${f.file} · integration worktree`, true); }
    patch(t.body, h`<div class="doc-h"><div><h1>${s.i} ${s.label} finding</h1><div class="sub">Review round ${f._round}${f.confidence ? ' · ' + f.confidence + ' confidence' : ''}</div></div></div>
      <div class="stack">${card('Issue', h`<div style="white-space:pre-wrap">${f.issue || ''}</div>`)}
      ${f.file ? card('Location', h`<div class="chips"><span class="chip mono">${f.file}${f.line ? ':' + f.line : ''}</span>${own.length ? h`<span class="dim small">owned by</span>${own.map(taskChip)}` : h`<span class="dim small">no task owns this file</span>`}</div>`) : ''}
      ${f.suggestion ? card('Suggestion', h`<div style="white-space:pre-wrap">${f.suggestion}</div>`) : ''}${f.evidence ? card('Evidence', h`<pre class="block">${stripAnsi(str(f.evidence))}</pre>`) : ''}${codeHtml}</div>`);
  }
};
const FILES = {
  plan: {t: 'plan.json', i: I.json}, state: {t: 'job.json', i: I.json}, report: {t: 'report.md', i: I.md}, out: {t: 'detached output', i: I.term},
};
function fileMeta(a){ if(FILES[a]) return FILES[a]; if(a.startsWith('log:')) return {t: a.slice(4) + '.log', i: I.log}; if(a.startsWith('brief:')) return {t: a.slice(6) + ' brief.md', i: I.md}; return {t: a, i: I.log}; }
KINDS.file = {
  title: a => fileMeta(a).t, icon: a => fileMeta(a).i, flex: true,
  mount(t){
    const a = t.arg, J = () => encodeURIComponent(JOB);
    t.crumb = region(t.el);
    if(a === 'out' || a.startsWith('log:')){ t.log = LogView(() => a === 'out' ? `/api/job/${J()}/out` : `/api/job/${J()}/log/${encodeURIComponent(a.slice(4))}`, {empty: a === 'out' ? 'No detached output (the job was not started with --detach, or has not written anything yet).' : 'This log is empty or does not exist yet.'}); t.el.appendChild(t.log.el); t.log.el.style.flex = '1'; }
    else { t.bar = region(t.el, 'toolbar'); t.body = region(t.el); t.body.style.cssText = 'flex:1;overflow:auto;min-height:0'; t.mode = 'pretty';
      t.bar.addEventListener('click', e => { const b = e.target.closest('[data-mode]'); if(b){ t.mode = b.dataset.mode; t.body._h = null; runUpdate(t); }
        const x = e.target.closest('[data-fold]'); if(x) $$('details', t.body).forEach(d => d.open = x.dataset.fold === 'open'); }); }
  },
  async update(t){
    const a = t.arg, J = encodeURIComponent(JOB); patch(t.crumb, crumbs([{t: D.job, go:['overview','']}, {t: a.startsWith('log:') ? 'logs' : a.startsWith('brief:') ? 'briefs' : 'files'}, {t: fileMeta(a).t}]));
    if(t.log){ t.log.load(); return; }
    let text = '';
    const get = async p => { try{ const r = await fetch(`/api/job/${J}/${p}`, {cache:'no-store'}); return r.ok ? await r.text() : ''; }catch(e){ return ''; } };
    if(a === 'plan') text = D.plan_raw || '';
    else if(a === 'report') text = D.has_report ? await get('report') : '';
    else if(a === 'state') text = await get('state');
    else if(a.startsWith('brief:')) text = await get('brief/' + encodeURIComponent(a.slice(6)));
    if(!TABS.has(t.id)) return;
    const isJson = a === 'plan' || a === 'state', modes = isJson ? [['pretty','Tree'],['raw','Raw']] : [['pretty','Preview'],['raw','Source']];
    patch(t.bar, h`<span class="ttl">${fileMeta(a).t} · ${plural(text ? text.split('\n').length : 0, 'line')}</span><span class="sp"></span>${isJson && t.mode === 'pretty' ? h`<button class="tog" data-fold="open">Expand all</button><button class="tog" data-fold="close">Collapse all</button>` : ''}
      ${modes.map(([m, l]) => h`<button class="tog ${t.mode === m ? 'on' : ''}" data-mode="${m}">${l}</button>`)}`);
    if(!text){ patch(t.body, h`<div class="empty" style="padding:24px">${a === 'report' ? 'No report yet. It is written when the job finishes.' : a === 'state' ? 'No job.json yet. The run writes it when it starts.' : 'Nothing here yet.'}</div>`); return; }
    if(t.mode === 'raw'){ patch(t.body, h`<pre class="code wrap">${text.replace(/\n$/,'').split('\n').map(l => h`<span class="l">${l}</span>`)}</pre>`); return; }
    if(isJson){ let v; try{ v = JSON.parse(text); }catch(e){ patch(t.body, h`<pre class="block">${text}</pre>`); return; } patch(t.body, h`<div class="json">${jsonView(v, null, 0, true)}</div>`); return; }
    patch(t.body, h`<div class="doc"><div class="md">${raw(md(text))}</div></div>`);
  }
};
KINDS.metrics = {
  title: () => 'Metrics', icon: () => raw(String(I.metrics).replace('width="24" height="24"', 'width="16" height="16"')), flex: false,
  mount(t){ t.body = region(t.el, 'doc'); },
  update(t){
    const M = D.metrics || {}, calls = arr(D.calls).filter(c => c && typeof c === 'object'), byRole = (M.cost_by_role && typeof M.cost_by_role === 'object') ? M.cost_by_role : {}, maxR = Math.max(0.0001, ...Object.values(byRole).map(Number).filter(isFinite));
    const total = x => (Number(x.state.cost_usd) || 0) + (Number(x.state.coord_cost_usd) || 0);
    const tasks = allTasks().filter(x => total(x)).sort((a, b) => total(b) - total(a)), maxT = Math.max(0.0001, ...tasks.map(total));
    const models = {}; calls.forEach(c => { const m = String(c.config || c.model_id || '?').split('/')[0]; models[m] = models[m] || {n:0, cost:0}; models[m].n++; models[m].cost += Number(c.cost_usd) || 0; });
    const bar = (l, v, max, col, go) => h`<div class="tlrow" ${go ? raw(`data-act="open" data-kind="task" data-arg="${esc(go)}" tabindex="0" role="button"`) : raw('style="cursor:default"')}><div class="tlname" title="${l}">${midTrunc(l, 24)}</div><div class="tltrack"><div class="tlbar" style="--c:${raw(col)};left:0;width:${(100*v/max).toFixed(1)}%"></div></div><div class="tld">${money(v)}</div></div>`;
    const pct = v => v == null ? '–' : Math.round(v * 100) + '%';
    patch(t.body, h`<div class="doc-h"><div><h1>Metrics</h1><div class="sub">Cost split, cache use, first-pass rate and coordination load for ${D.job}.</div></div></div>
      <div class="tiles"><div class="tile"><div class="k">Total cost</div><div class="v">${money(D.cost_usd)}</div><div class="s">${D.budget_usd ? 'of ' + money(D.budget_usd) : 'no budget'}</div>${costMeter()}</div>
        <div class="tile"><div class="k">Manager share</div><div class="v">${pct(M.manager_share)}</div><div class="s">${M.manager_wakes ?? 0} wakes · ${M.auto_triaged ?? 0} by rule</div></div>
        <div class="tile"><div class="k">Cache reads</div><div class="v">${pct(M.cache_read_share)}</div><div class="s">of input tokens</div></div>
        <div class="tile"><div class="k">First pass</div><div class="v">${pct(M.first_pass_rate)}</div><div class="s">${M.tasks_done ?? 0} tasks done</div></div>
        <div class="tile"><div class="k">Turns / step</div><div class="v">${M.turns_per_subagent_step ?? '–'}</div><div class="s">subagent average</div></div>
        <div class="tile"><div class="k">Open issues</div><div class="v">${M.open_issues_per_done_task ?? '–'}</div><div class="s">per done task</div></div></div>
      <div class="grid2" style="margin-bottom:14px">${card('Cost by role', Object.keys(byRole).length ? h`<div class="tl">${Object.entries(byRole).map(([k, v]) => bar(human(k), Number(v) || 0, maxR, 'var(--accent)'))}</div>` : h`<div class="empty">No cost recorded yet.</div>`, null, true)}
        ${card('Cost by task', tasks.length ? h`<div class="tl">${tasks.map(x => bar(x.id, total(x), maxT, stt(x.state.status).c, x.id))}</div>` : h`<div class="empty">No task cost yet.</div>`, 'task + coordination', true)}</div>
      <div class="grid2" style="margin-bottom:14px">${card('Models', Object.keys(models).length ? h`<table class="tbl"><thead><tr><th>model</th><th class="n">calls</th><th class="n">cost</th></tr></thead><tbody>${Object.entries(models).sort((a, b) => b[1].cost - a[1].cost).map(([m, v]) => h`<tr><td class="mono">${m}</td><td class="n">${v.n}</td><td class="n">${money(v.cost)}</td></tr>`)}</tbody></table>` : h`<div class="empty">No calls yet.</div>`, null, true)}
        ${card('Coordination', h`<dl class="kvs">${[['manager wakes', M.manager_wakes ?? '–'], ['wakes per done task', M.manager_wakes_per_done_task ?? '–'], ['rejected proposals', M.rejected_proposals ?? '–'], ['manager busy', M.manager_busy_min != null ? M.manager_busy_min + ' min' : '–'], ['review rounds', M.review_rounds ?? arr(D.review).length], ['routing proposals pending', D.pending_proposals || 0]].map(([k, v]) => h`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`)}</div>
      ${card('All model calls', callsTable(calls, 'all'), plural(calls.length, 'call'), true)}`);
  }
};
/* ---------- side bar ---------- */
const VIEWS = [['explorer','Explorer',I.files],['shots','Screenshots',I.images],['review','Review',I.review],['manager','Manager & activity',I.manager],['metrics','Metrics',I.metrics],['plan','Plan',I.plan]];
function renderActivity(){
  const lr = D && latestReview(), fs = lr ? findings().filter(f => f._round === lr.round) : [], nb = fs.filter(f => f.severity === 'blocker' || f.severity === 'major').length, ns = allShots().length, nm = D ? arr(D.manager.log).length : 0;
  patch($('#activity'), h`${VIEWS.map(([k, l, ic]) => h`<button class="${VIEW===k && !SIDE_HIDDEN ?'on':''}" data-act="view" data-view="${k}" title="${l}${k==='review' && nb ? ' (' + nb + ' blocker/major findings in the latest round)' : k==='manager' && nm ? ' (' + plural(nm, 'decision') + ')' : ''}" aria-label="${l}">${ic}${k==='shots' && ns ? h`<span class="badge">${ns > 99 ? '99+' : ns}</span>` : ''}${k==='review' && nb ? h`<span class="badge err">${nb}</span>` : ''}${k==='manager' && nm ? h`<span class="badge">${nm > 99 ? '99+' : nm}</span>` : ''}</button>`)}`);
}
let SIDE_HIDDEN = store.get('sideHidden', false);
function treeRow(o){ const live = !!o.attrs && /data-act=/.test(o.attrs);
  return h`<div class="row ${o.sel?'sel':''} ${live ? '' : 'inert'}" style="--d:${o.d||0}" ${raw(o.attrs || '')} ${live ? raw('tabindex="0" role="button"') : ''} ${o.folder && live ? raw(`aria-expanded="${!!o.open}"`) : ''} title="${o.tip || ''}"><span class="twist ${o.folder ? (o.open ? 'open' : '') : 'none'}">${o.folder ? I.chev : ''}</span>${o.icon ? h`<span class="ico">${o.icon}</span>` : ''}<span class="lbl">${o.label}</span>${o.desc ? h`<span class="desc">${o.desc}</span>` : ''}${o.meta ? h`<span class="meta">${o.meta}</span>` : ''}</div>`; }
const act = (a, kind, arg) => `data-act="${a}" data-kind="${esc(kind)}" data-arg="${esc(arg ?? '')}"`;
const tog = k => `data-act="toggle" data-key="${esc(k)}"`;
const isSel = (kind, arg) => ACTIVE === tabId(kind, arg);
function taskRow(t, d){ const s = t.state || {};
  return treeRow({d, icon: sicon(s.status || 'pending'), label: midTrunc(t.id, 30), tip: `${t.id} — ${t.title || ''} (${stt(s.status).l})`, attrs: act('open','task',t.id), sel: isSel('task', t.id),
    meta: s.started ? h`<span data-from="${s.started}" data-to="${s.status === 'running' ? '' : s.ended || ''}"></span>` : s.status === 'awaiting_approval' ? 'gate' : ''}); }
function sideExplorer(){
  if(!D) return h`<div class="side-note">${MISSING ? h`Waiting for job <b>${JOB}</b> to start…` : 'Loading…'}</div>`;
  const W = waves(), c = counts(), rows = [];
  rows.push(treeRow({d:0, folder:true, open:OPEN.has('job'), icon:I.layers, label:D.job, desc: `${c.done}/${c.total}`, attrs: tog('job')}));
  if(OPEN.has('job')){
    rows.push(treeRow({d:1, icon:I.home, label:'Overview', attrs: act('open','overview',''), sel: isSel('overview','')}));
    W.forEach((w, i) => { const k = 'w' + i, done = w.filter(t => t.state.status === 'done').length, run = w.some(t => t.state.status === 'running');
      rows.push(treeRow({d:1, folder:true, open:OPEN.has(k), icon: run ? sicon('running') : done === w.length ? sicon('done') : I.folder, label:`Wave ${i+1}`, meta:`${done}/${w.length}`, attrs: tog(k)}));
      if(OPEN.has(k)) w.forEach(t => rows.push(taskRow(t, 2))); });
    if(arr(D.extra).length){ rows.push(treeRow({d:1, folder:true, open:OPEN.has('extra'), icon:I.folder, label:'Replaced / skipped', meta: String(D.extra.length), attrs: tog('extra')}));
      if(OPEN.has('extra')) D.extra.forEach(t => rows.push(taskRow(t, 2))); }
  }
  const files = [];
  files.push(treeRow({d:0, folder:true, open:OPEN.has('files'), icon:null, label:'Job files', attrs: tog('files')}));
  if(OPEN.has('files')){
    files.push(treeRow({d:1, icon:I.json, label:'plan.json', desc: D.plan_only ? 'planned, not run yet' : '', attrs: act('open','file','plan'), sel: isSel('file','plan')}));
    files.push(treeRow({d:1, icon:I.json, label:'job.json', desc: D.has_state ? 'live state' : 'not yet', attrs: act('open','file','state'), sel: isSel('file','state')}));
    files.push(treeRow({d:1, icon:I.md, label:'report.md', desc: D.has_report ? '' : 'not yet', attrs: act('open','file','report'), sel: isSel('file','report')}));
    files.push(treeRow({d:1, icon:I.term, label:'detached output', desc: D.has_out ? D.job + '.out' : 'none', attrs: act('open','file','out'), sel: isSel('file','out')}));
    const logs = arr(D.logs), special = ['_integration', '_devserver'];
    files.push(treeRow({d:1, folder:true, open:OPEN.has('logs'), icon:I.folder, label:'logs', meta: String(logs.length), attrs: tog('logs')}));
    if(OPEN.has('logs')){
      special.forEach(n => files.push(treeRow({d:2, icon:I.log, label: n + '.log', desc: logs.some(l => l.name === n) ? '' : 'empty', attrs: act('open','file','log:'+n), sel: isSel('file','log:'+n)})));
      /* sizes are rounded so a growing log does not re-render (and unfocus) the tree on every poll */
      logs.filter(l => !special.includes(l.name)).forEach(l => files.push(treeRow({d:2, icon:I.log, label: midTrunc(l.name, 30) + '.log', tip: l.name + '.log · ' + bytes(l.size), meta: l.size >= 1048576 ? (l.size/1048576).toFixed(1) + ' MB' : l.size >= 102400 ? Math.round(l.size/102400) * 100 + ' KB' : l.size >= 1024 ? Math.round(l.size/1024) + ' KB' : 'small', attrs: act('open','file','log:'+l.name), sel: isSel('file','log:'+l.name)})));
    }
  }
  return h`<div class="sect">${rows}</div><div class="sect" style="margin-top:6px">${files}</div>`;
}
function sideShots(){
  if(!D) return h`<div class="side-note">No job yet.</div>`;
  const shots = filteredShots(), rows = [];
  const byR = new Map(); shots.forEach(s => { if(!byR.has(s.round)) byR.set(s.round, new Map()); const m = byR.get(s.round), k = s.name || s.path; if(!m.has(k)) m.set(k, []); m.get(k).push(s); });
  Array.from(byR.keys()).sort((a, b) => b - a).forEach(rn => { const k = 'r' + rn, open = !OPEN.has('x' + k);
    rows.push(treeRow({d:0, folder:true, open, icon:I.folder, label:`Round ${rn}`, meta: String(Array.from(byR.get(rn).values()).reduce((a, l) => a + l.length, 0)), attrs: `data-act="toggle-inv" data-key="${esc(k)}"`}));
    if(open) byR.get(rn).forEach((list, pg) => { rows.push(treeRow({d:1, icon:I.image, label: pg, desc: list[0].path || ''}));
      list.forEach(s => { const e = shotErrs(s), ob = shotObs(s); rows.push(treeRow({d:2, icon: s.viewport === 'mobile' ? I.mobile : I.desktop, label: s.viewport + (s.step ? ' · ' + s.step : ''), attrs: `data-act="shot" data-id="${esc(s.id)}" data-round="${esc(s.round)}"`,
        meta: h`${s.status != null ? h`<span style="color:${raw(s.status >= 400 ? 'var(--err)' : 'var(--dim)')}">${s.status}</span>` : ''}${e ? h` <span style="color:var(--err)">· ${plural(e, 'error')}</span>` : ''}${ob ? h` ${okIcon(ob.ok)}` : shotReviewed(s) ? h` <span style="color:var(--warn)" title="not mentioned by the UI reviewer">unreviewed</span>` : ''}`})); }); }); });
  const R = arr(D.ui_rounds), last = R[R.length-1];
  return h`<div class="filters">${shotFilters(true)}</div>
    <div class="side-pad" style="padding-top:0"><button class="tog" style="width:100%;justify-content:center" data-act="open" data-kind="shots" data-arg="">${I.image} Open gallery</button></div>
    ${D.ui_config ? h`<div class="side-pad small muted" style="padding-top:0">${D.ui_config.url ? h`<div class="mono">${D.ui_config.url}</div>` : ''}${last ? (last.ran ? `Last UI review: ${human((last.verdict||{}).verdict || 'no verdict')}` : last.skipped ? 'Last UI review skipped: ' + last.skipped : last.error ? 'Last UI review could not run: ' + last.error : '') : 'No UI review yet.'}
      ${D.screenshots_root ? h`<div style="margin-top:6px">Folder (git-ignored):</div>${cmdBox(D.screenshots_root)}` : ''}</div>` : h`<div class="side-note">No "ui" section in the plan.</div>`}
    <div class="sect">${rows.length ? rows : h`<div class="side-note">No screenshots${allShots().length ? ' match the filters' : ' yet'}.</div>`}</div>`;
}
function sect(key, title, count, body){ const open = !OPEN.has('xs-' + key);
  return h`<div class="sect"><div class="sect-h" data-act="toggle-inv" data-key="${'s-' + key}" role="button" tabindex="0" aria-expanded="${open}"><span class="twist ${open ? 'open' : ''}">${I.chev}</span><span>${title}</span>${count != null && count !== '' ? h`<span class="cnt">${count}</span>` : ''}</div>${open ? body : ''}</div>`; }
function sideReview(){
  if(!D) return h`<div class="side-note">No job yet.</div>`;
  const rows = [], R = arr(D.review);
  R.forEach((r, i) => { const v = r.verdict || {}, fs = arr(v.findings), inProgress = !r.verdict && i === R.length - 1 && D.status === 'running';
    rows.push(treeRow({d:0, icon: v.verdict ? okIcon(v.verdict === 'pass') : inProgress ? sicon('running') : sicon('failed'), label: `Round ${r.round || i+1}`, desc: v.verdict ? human(v.verdict) : r.final_verify_ok === false ? 'final verify failed' : inProgress ? 'in progress' : 'no verdict', meta: fs.length ? plural(fs.length, 'finding') : '', attrs: act('open','review',String(i)), sel: isSel('review', String(i))}));
    rows.push(treeRow({d:1, icon: okIcon(r.final_verify_ok), label: 'Final verify', desc: r.final_verify_ok ? 'passed' : 'failed', attrs: act('open','review',String(i))}));
    if(r.ui) rows.push(treeRow({d:1, icon: r.ui.verdict ? okIcon(r.ui.verdict.verdict === 'pass') : r.ui.error ? sicon('failed') : sicon('skipped'), label: 'UI review', desc: r.ui.verdict ? human(r.ui.verdict.verdict) : r.ui.skipped ? 'skipped' : r.ui.error ? 'could not run' : '…', meta: arr(r.ui.shots).length ? plural(arr(r.ui.shots).length, 'shot') : '', attrs: act('open','shots',''), tip: r.ui.skipped || r.ui.error || ''}));
    if(r.advisor) rows.push(treeRow({d:1, icon: I.owl, label: 'Advisor', desc: r.advisor.overridden_by_user ? 'overridden by you' : r.advisor.error ? 'no usable answer' : r.advisor.verdict || '', attrs: act('open','advisor','final'), sel: isSel('advisor','final')}));
  });
  const ap = advEntries('plan'), af = advEntries('final');
  const fs = findings(), lr = latestReview(), cur = lr ? fs.filter(f => f._round === lr.round) : [];
  const advRow = (a, stage, i, n) => treeRow({d:0, icon:I.owl, label: (stage === 'plan' ? 'Plan check' : 'Final check') + (n > 1 ? ' ' + (i+1) : ''), desc: a.error ? 'no usable answer' : a.verdict || '', meta: a.config || '', tip: a.summary || a.cause || '', attrs: act('open','advisor',stage), sel: isSel('advisor', stage)});
  return h`${sect('rounds', 'Rounds', `${D.review_rounds_used ?? R.length} / ${D.review_rounds_max ?? 2}`, rows.length ? rows : h`<div class="side-note">No review round yet. Review starts once every task has merged.</div>`)}
    ${sect('advisor', 'Advisor', ap.length + af.length || '', ap.length || af.length ? h`${ap.map((a, i) => advRow(a, 'plan', i, ap.length))}${af.map((a, i) => advRow(a, 'final', i, af.length))}` : h`<div class="side-note">${D.advice_plan_ok ? 'Plan check: ' + str(D.advice_plan_ok) : 'No advisor check yet.'}</div>`)}
    ${sect('latest', 'Latest findings', cur.length, cur.length ? h`<div class="side-probs">${cur.map(f => probRow(f))}</div>` : h`<div class="side-note">No findings.</div>`)}`;
}
function sideManager(){
  if(!D) return h`<div class="side-note">No job yet.</div>`;
  const M = D.metrics || {}, log = arr(D.manager.log).filter(m => m && typeof m === 'object').slice().reverse();
  return h`${sect('mstats', 'Manager', D.manager.enabled ? 'on' : 'off', h`<div class="side-pad"><dl class="kvs"><dt>interventions</dt><dd>${D.manager.interventions || 0}</dd><dt>decisions logged</dt><dd>${log.length}</dd><dt>settled by rule</dt><dd>${M.auto_triaged ?? 0}</dd><dt>busy</dt><dd>${M.manager_busy_min != null ? M.manager_busy_min + ' min' : '–'}</dd><dt>queued events</dt><dd>${D.pending_events || 0}</dd></dl></div>`)}
    ${sect('decisions', 'Decisions', log.length, log.length ? h`<div class="feed">${log.map(m => { const acts = arr((m.second_try || m).actions), go = m.task && typeof m.task === 'string';
      return h`<div class="ev ${go ? 'click' : ''}" style="grid-template-columns:62px 1fr" ${go ? raw(act('open', 'task', m.task) + ' tabindex="0" role="button"') : ''}><div class="t" title="${m.t ? new Date(m.t*1000).toLocaleString() : ''}">${feedTime(m.t)}</div><div><div class="h">${human(m.event)}${m.task ? h` <span class="mono small dim">${m.task}</span>` : ''}</div>
        <div class="acts">${acts.map(a => h`<span class="chip c" style="--c:var(--split)">${human(a.action)} ${typeof a.task === 'string' ? a.task : (a.task && a.task.id) || ''}</span>`)}${m.auto ? h`<span class="chip">rule</span>` : ''}${m.rejected ? h`<span class="chip c" style="--c:var(--warn)">rejected once</span>` : ''}</div>
        <details data-key="${mKey(m)}"><summary>details</summary>${managerDetail(m)}</details></div></div>`; })}</div>` : h`<div class="side-note">The manager has not been woken yet.</div>`)}
    <div class="side-pad" style="padding-top:10px"><button class="tog" style="width:100%;justify-content:center" data-act="panel" data-panel="activity">${I.manager} Full activity feed</button></div>`;
}
function sideMetrics(){
  if(!D) return h`<div class="side-note">No job yet.</div>`;
  const M = D.metrics || {}, br = (M.cost_by_role && typeof M.cost_by_role === 'object') ? M.cost_by_role : {};
  return h`<div class="side-pad" style="padding-top:2px"><button class="tog" style="width:100%;justify-content:center" data-act="open" data-kind="metrics" data-arg="">${I.metrics} Open metrics</button></div>
    ${sect('cost', 'Cost', money(D.cost_usd) + (D.budget_usd ? ' / ' + money(D.budget_usd) : ''), h`<div class="side-pad"><dl class="kvs">${Object.entries(br).map(([k, v]) => h`<dt>${human(k)}</dt><dd>${money(v)}</dd>`)}</dl>${costMeter()}</div>`)}
    ${sect('quality', 'Quality', '', h`<div class="side-pad"><dl class="kvs"><dt>first-pass rate</dt><dd>${M.first_pass_rate != null ? Math.round(M.first_pass_rate*100) + '%' : '–'}</dd><dt>cache reads</dt><dd>${M.cache_read_share != null ? Math.round(M.cache_read_share*100) + '%' : '–'}</dd><dt>turns / step</dt><dd>${M.turns_per_subagent_step ?? '–'}</dd><dt>model calls</dt><dd>${arr(D.calls).length}</dd></dl></div>`)}`;
}
function sidePlan(){
  if(!D) return h`<div class="side-note">No plan yet.</div>`;
  const W = waves(), uc = D.ui_config;
  return h`<div class="side-pad" style="padding-top:2px"><button class="tog" style="width:100%;justify-content:center" data-act="open" data-kind="file" data-arg="plan">${I.json} Open plan.json</button></div>
    ${sect('goal', 'Goal', '', h`<div class="side-pad small">${str(D.goal || '')}</div>`)}
    ${D.definition_of_done ? sect('dod', 'Definition of done', '', h`<div class="side-pad small muted" style="white-space:pre-wrap">${str(D.definition_of_done)}</div>`) : ''}
    ${arr(D.assumptions).length ? sect('asm', 'Unconfirmed (the planner chose)', arr(D.assumptions).length, h`<div class="side-pad">${listOr(D.assumptions, x => h`<li class="small"><span class="t">${str(x)}</span></li>`)}</div>`) : ''}
    ${sect('acc', 'Acceptance', arr(D.acceptance).length, h`<div class="side-pad">${listOr(acceptRows(), a => h`<li class="small"><span class="i">${a.r ? okIcon(a.r.met) : sicon('pending')}</span><span class="t">${str(a.c)}</span></li>`)}</div>`)}
    ${sect('waves', 'Tasks by wave', D.tasks.length, W.map((w, i) => h`${treeRow({d:0, icon:I.folder, label:'Wave ' + (i+1), meta: String(w.length)})}${w.map(t => treeRow({d:1, icon: sicon(t.state.status), label: midTrunc(t.id, 28), desc: t.tier, attrs: act('open','task',t.id), sel: isSel('task', t.id), tip: t.id + ' — ' + (t.title || '')}))}`))}
    ${uc ? sect('ui', 'UI review', '', h`<div class="side-pad small"><dl class="kvs"><dt>dev</dt><dd class="mono" title="${str(uc.dev || '')}">${str(uc.dev || '')}</dd><dt>url</dt><dd class="mono">${str(uc.url || '')}</dd><dt>viewports</dt><dd>${(arr(uc.viewports).length ? arr(uc.viewports) : ['desktop','mobile']).join(', ')}</dd><dt>pages</dt><dd>${arr(uc.pages).length + D.tasks.reduce((a, t) => a + arr(t.ui).length, 0)}</dd></dl></div>`) : ''}`;
}
const SIDE = {explorer: sideExplorer, shots: sideShots, review: sideReview, manager: sideManager, metrics: sideMetrics, plan: sidePlan};
function renderSide(){
  const v = VIEWS.find(x => x[0] === VIEW) || VIEWS[0];
  patch($('#side-head'), h`<span>${v[1]}</span>${VIEW === 'explorer' && D ? h`<button class="iconbtn sm" data-act="collapse-all" title="Collapse folders" aria-label="Collapse folders">${raw(P('<path d="M4 6l4 4 4-4" transform="rotate(180 8 8)"/><path d="M3 13.5h10"/>'))}</button>` : ''}`);
  const sb = $('#side-body'), top = sb.scrollTop; patch(sb, SIDE[VIEW] ? SIDE[VIEW]() : ''); sb.scrollTop = top;
  renderActivity();
}
/* ---------- banners, status bar, panel ---------- */
function cmdBox(c){ return h`<span class="cmd"><span title="${c}">${c}</span><button data-act="copy" data-text="${c}">Copy</button></span>`; }
function banner(color, icon, body){ return h`<div class="banner" style="--c:${raw(color)}"><span class="bi">${icon}</span><div class="bt">${body}</div></div>`; }
let BANNERS_OPEN = false;
const RESUMABLE = ['failed','review_failed','budget','interrupted','environment','stopped'];
function renderBanners(){
  const errs = Array.from(RENDER_ERRS.entries()).map(([k, m]) => banner('var(--err)', sicon('failed'), h`<b>Could not render the ${k} view:</b> ${m}. The server is answering; this is a display bug in the page, the job itself is unaffected.`));
  const serr = SERVERR ? [banner('var(--err)', sicon('failed'), h`<b>The dashboard could not read this job:</b> ${SERVERR}. The server is answering; the job's state files hold something it can't show.`)] : [];
  errs.unshift(...serr);
  if(!D){ patch($('#banners'), h`${MISSING && JOB ? banner('var(--split)', sicon('waiting'), h`<b>Waiting for job ${JOB} to start.</b> This page refreshes by itself.`) : NETERR ? banner('var(--err)', sicon('failed'), h`<b>Cannot reach the dashboard server.</b> Retrying…`) : ''}${errs}`); return; }
  const ts = D.tasks, b = [...errs], resume = extra => `${CLI()} resume ${shq(D.job)}${extra || ''}`;
  if(NETERR) b.push(banner('var(--err)', sicon('failed'), h`<b>Lost contact with the dashboard server.</b> Showing the last state from ${ago(LASTOK)}; retrying.`));
  if(D.plan_only) b.push(banner('var(--split)', sicon('waiting'), h`<b>Plan ready; the run has not started yet.</b> This is the plan as written. The page switches to the live job by itself once the run starts.`));
  if(D.human_question) b.push(banner('var(--gate)', sicon('awaiting_human'), h`<b>The manager is asking you:</b> ${str(D.human_question)}<div>${cmdBox(resume(' --answer "..."'))}</div>`));
  const waiting = ts.filter(t => t.state.status === 'awaiting_approval');
  if(waiting.length) b.push(banner('var(--gate)', sicon('awaiting_approval'), h`<b>Waiting for your approval:</b> ${waiting.map((t, i) => h`${i ? ', ' : ''}<span class="code-inline">${t.id}</span>${t.gate ? ' (' + t.gate + ')' : ''}`)}<div>${cmdBox(resume(' ' + waiting.map(t => '--approve ' + shq(t.id)).join(' ')))}</div>`));
  if(D.status === 'advisor_blocked'){ const a = advEntries('plan').slice(-1)[0];
    b.push(banner('var(--gate)', I.owl, h`<b>${a && a.error ? 'The advisor gave no usable answer on the plan.' : 'The advisor asks to revise the plan.'}</b> <span class="chip link" data-act="open" data-kind="advisor" data-arg="plan" tabindex="0" role="link">open the plan check</span>
      ${D.message ? h`<pre>${stripAnsi(str(D.message))}</pre>` : ''}
      <div class="small" style="margin-top:6px">${a && a.error ? 'Try again:' : 'Fix plan.json, then resume:'}</div><div>${cmdBox(resume())}</div>
      <div class="small" style="margin-top:6px">Or override the advisor and run the plan as it is:</div><div>${cmdBox(resume(' --approve advisor:plan'))}</div>`)); }
  if(D.status === 'stopped') b.push(banner('var(--err)', sicon('stopped'), h`<b>The job process is gone</b> but the job never finished.<div>${cmdBox(resume())}</div>`));
  const sk = arr(D.skipped_checks);
  if(D.status === 'done') b.push(banner(sk.length ? 'var(--warn)' : 'var(--ok)', sicon('done'), h`<b>Done.</b> Review the branch <span class="code-inline">${D.branch || ''}</span> and merge it when you're happy.${sk.length ? h`<div><b>Not checked:</b> ${sk.map((x, i) => h`${i ? '; ' : ''}${str(x)}`)}.</div>` : ''}${D.message ? h`<pre>${str(D.message)}</pre>` : ''}`));
  else if(D.message && !D.human_question && !['running','advisor_blocked','stopped'].includes(D.status)) b.push(banner('var(--err)', sicon(D.status), h`<b>${stt(D.status).l}</b><pre>${stripAnsi(str(D.message))}</pre>${RESUMABLE.includes(D.status) ? h`<div>${cmdBox(resume())}</div>` : ''}`));
  arr(D.warnings).forEach(w => b.push(banner('var(--warn)', sev('major').i, h`${str(w)}`)));
  /* a phone shows the first banner and a button for the rest: a strip that scrolls with no cue hides what needs the user */
  const box = $('#banners'), fold = isMobile() && b.length > 1 && !BANNERS_OPEN;
  box.classList.toggle('open', isMobile() && BANNERS_OPEN);
  patch(box, fold ? h`${b[0]}<button class="banner-more" data-act="banners">${pluralW(b.length - 1, 'more notice needs you', 'more notices need you')}: show</button>` : h`${b}${isMobile() && b.length > 1 ? h`<button class="banner-more" data-act="banners">show fewer</button>` : ''}`);
}
function renderStatus(){
  const sb = $('#statusbar');
  if(!D){ sb.className = 'statusbar ' + (MISSING ? 'waiting' : ''); patch(sb, h`<span class="sbi ${MISSING ? 'live' : ''}"><span class="dot"></span>${MISSING ? 'waiting for ' + JOB : JOB ? 'loading' : 'no job'}</span><span class="sb-sp"></span><span class="sbi">${I.sync}${NETERR ? 'offline' : 'polling every 2s'}</span>`); return; }
  const c = counts(), live = D.status === 'running' && D.alive, cap = D.capacity || {}, fs = findings(), lr = latestReview(), cur = lr ? fs.filter(f => f._round === lr.round) : [];
  const ne = cur.filter(f => f.severity === 'blocker').length, nw = cur.filter(f => f.severity === 'major').length, ni = cur.filter(f => f.severity === 'minor').length;
  sb.className = 'statusbar ' + (D.status === 'running' ? (D.alive ? 'running' : '') : D.status === 'done' ? 'done' : D.status === 'planned' ? 'waiting' : ['awaiting_approval','awaiting_human','advisor_blocked'].includes(D.status) ? 'attn' : ['failed','stopped','environment','review_failed'].includes(D.status) ? 'bad' : '');
  patch(sb, h`<span class="sbi ${live ? 'live' : ''}" title="job status"><span class="dot"></span>${stt(D.status).l}</span>
    ${D.branch ? h`<span class="sbi hide-m" title="branch">${I.branch}${D.branch}</span>` : ''}
    <button class="sbi" data-act="panel" data-panel="problems" title="Findings in the latest review round: blockers, majors, minors">${SEV.blocker.i}${ne} ${SEV.major.i}${nw}<span class="hide-m">${SEV.minor.i}${ni}</span></button>
    <span class="sb-sp"></span>
    <span class="sbi" title="tasks done / total">${I.tasks}${c.done}/${c.total}</span>
    <span class="sbi hide-m" title="parallel slots in use / cap">${I.slots}${c.running}/${cap.cap ?? D.max_parallel ?? '–'}</span>
    <span class="sbi" title="elapsed">${I.clock}<span data-from="${D.created || ''}" data-to="${D.finished && D.status !== 'running' ? D.finished : ''}"></span></span>
    <span class="sbi" title="cost vs budget">${I.coin}${money(D.cost_usd)}${D.budget_usd ? h`<span class="hide-m"> / ${money(D.budget_usd)}</span><span class="sbmeter hide-m"><i style="width:${Math.min(100, 100*(D.cost_usd||0)/D.budget_usd).toFixed(0)}%"></i></span>` : ''}</span>
    <span class="sbi hide-m" title="last state change">${I.sync}<span data-ago="${D.updated || ''}"></span></span>`);
}
const PTABS = [['problems','Problems'],['terminal','Terminal'],['activity','Activity'],['output','Output']];
let PANEL_OPEN = store.get('panelOpen', !matchMedia('(max-width:760px)').matches);
const PP = {};
function mountPanel(){
  const body = $('#panel-body');
  PTABS.forEach(([k]) => PP[k] = region(body, 'ppane' + (k === 'terminal' || k === 'output' ? ' flex' : '')));
  PP.term = LogView(() => JOB ? (TERMSRC === 'out' ? `/api/job/${encodeURIComponent(JOB)}/out` : `/api/job/${encodeURIComponent(JOB)}/log/${encodeURIComponent(TERMSRC)}`) : null, {empty: 'No output yet.', n: 300000});
  PP.terminal.appendChild(PP.term.el);
  PP.term.extra.innerHTML = `<select class="sel-in" aria-label="Terminal source"><option value="out">detached output</option><option value="_integration">integration log</option></select>`;
  $('select', PP.term.extra).onchange = e => { TERMSRC = e.target.value; PP.term.reset(); };
  PP.out = LogView(() => JOB ? `/api/job/${encodeURIComponent(JOB)}/log/_devserver` : null, {empty: 'No dev server output. It appears when a UI review starts the app.', n: 300000});
  PP.output.appendChild(PP.out.el);
  PP.feed = region(PP.activity); PP.probs = region(PP.problems);
}
function renderPanel(){
  const p = $('#panel'); p.classList.toggle('closed', !PANEL_OPEN); $('#sash-panel').style.display = PANEL_OPEN ? '' : 'none';
  $$('[data-act="toggle-panel"]').forEach(b => b.classList.toggle('on', PANEL_OPEN));
  const fs = findings(), lr = D && latestReview(), lastRound = lr ? lr.round : null, ncur = lr ? fs.filter(f => f._round === lastRound).length : 0;
  patch($('#panel-tabs'), h`${PTABS.map(([k, l]) => h`<button class="pt ${PANEL===k?'on':''}" data-act="panel" data-panel="${k}" ${k === 'problems' ? raw('title="Findings in the latest review round (earlier rounds are listed, collapsed)"') : ''}>${l}${k === 'problems' && ncur ? h`<span class="n">${ncur}</span>` : ''}</button>`)}<span class="sp"></span>
    <button class="iconbtn sm" data-act="toggle-panel" title="Close panel" aria-label="Close panel">${I.close}</button>`);
  Object.entries(PP).forEach(([k, el]) => { if(el && el.classList) el.classList.toggle('on', k === PANEL); });
  if(!PANEL_OPEN || !D) { if(PANEL_OPEN && !D){ patch(PP.probs, h`<div class="empty">No job state yet.</div>`); patch(PP.feed, h`<div class="empty">No activity yet.</div>`); } return; }
  if(PANEL === 'problems'){
    const byR = new Map(); fs.forEach(f => { if(!byR.has(f._round)) byR.set(f._round, []); byR.get(f._round).push(f); });
    const order = {blocker:0, major:1, minor:2}, newest = lr ? lr.round : Math.max(...Array.from(byR.keys()).map(Number).filter(isFinite), -Infinity);
    const clean = lr && !byR.has(lr.round) ? h`<div class="prob-g" style="cursor:default"><span class="twist none"></span>Review round ${lr.round}<span class="cnt">no findings</span></div>` : '';
    patch(PP.probs, fs.length ? h`${clean}${Array.from(byR.keys()).sort((a, b) => b - a).map(rn => { const latest = rn === newest, k = 'p' + rn, open = latest ? !OPEN.has('x' + k) : OPEN.has('o' + k), l = byR.get(rn).sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3));
      return h`<div class="prob-g" ${raw(latest ? `data-act="toggle-inv" data-key="${k}"` : `data-act="toggle" data-key="o${k}"`)} tabindex="0" role="button" aria-expanded="${open}"><span class="twist ${open ? 'open' : ''}">${I.chev}</span>Review round ${rn}${latest ? '' : ' · earlier round'}<span class="cnt">${plural(l.length, 'finding')}</span></div>${open ? l.map(f => probRow(f)) : ''}`; })}` : h`<div class="empty">No problems have been detected in the job.</div>`);
  }
  if(PANEL === 'activity') patch(PP.feed, feedHtml(events()));
  if(PANEL === 'terminal') PP.term.load();
  if(PANEL === 'output') PP.out.load();
}
/* ---------- lightbox ---------- */
const LB = {open:false, list:[], idx:0, zoom:0, cmp:false, ret:null};
function stepRank(list){ /* a page's shots in flow order: named steps as they were taken, the final state last */
  const m = new Map(), key = s => s.round + '|' + (s.name || s.path) + '|' + (s.step || '');
  list.forEach(s => { if(!m.has(key(s))) m.set(key(s), s.step ? m.size : 1e9); });
  return s => m.get(key(s));
}
function orderShots(list){ /* the gallery's visual order: newest round first, pages as they first appear, then flow step, viewport */
  const pg = new Map(), key = s => s.round + '|' + (s.name || s.path), r = stepRank(list); list.forEach(s => { if(!pg.has(key(s))) pg.set(key(s), pg.size); });
  return list.slice().sort((a, b) => (b.round - a.round) || (pg.get(key(a)) - pg.get(key(b))) || (r(a) - r(b)) || String(a.viewport).localeCompare(String(b.viewport)));
}
function openShot(id, round){
  const list = orderShots(filteredShots()); let idx = list.findIndex(s => s.id === id && String(s.round) === String(round));
  let L = list; if(idx < 0){ L = orderShots(allShots()); idx = L.findIndex(s => s.id === id && String(s.round) === String(round)); if(idx < 0) idx = L.findIndex(s => s.id === id); }
  if(idx < 0) return; const was = LB.open; if(!was) LB.ret = document.activeElement;
  LB.list = L; LB.idx = idx; LB.zoom = 0; LB.cmp = false; LB.open = true; $('#lightbox').hidden = false; renderLB(true);
  const c = $('#lightbox [data-act="lb-close"]'); if(c) c.focus({preventScroll:true});
  syncHash(!was);
}
function closeLB(quiet){ if(!LB.open) return; LB.open = false; $('#lightbox').hidden = true;
  const r = LB.ret; LB.ret = null; if(r && r.isConnected && r.focus) r.focus({preventScroll:true});
  if(!quiet) syncHash(false); }
function renderLB(full){
  const el = $('#lightbox'), s = LB.list[LB.idx]; if(!s){ closeLB(); return; }
  if(full || !el._shell){ el._shell = true; el.innerHTML = `<div class="lb-top"></div><div class="lb-main"><div class="lb-stage"></div><aside class="lb-side"></aside></div>`; }
  const ob = shotObs(s), key = x => [x.name || x.path, x.viewport, x.step || ''].join('|'), same = allShots().filter(x => key(x) === key(s)).sort((a, b) => a.round - b.round);
  patch($('.lb-top', el), h`<span class="ttl">${s.name || s.path} · ${s.viewport}${s.step ? ' · ' + s.step : ''} <span class="dim" style="color:#9aa1ad;font-weight:400">· round ${s.round} · ${LB.idx+1} / ${LB.list.length}</span></span>
    ${LB.cmp ? '' : h`<button class="iconbtn" data-act="lb-zoom" data-z="-1" title="Zoom out (-)" aria-label="Zoom out">${I.zout}</button><span class="zoom">${LB.zoom ? Math.round(LB.zoom*100) + '%' : 'Fit'}</span><button class="iconbtn" data-act="lb-zoom" data-z="1" title="Zoom in (+)" aria-label="Zoom in">${I.zin}</button><button class="iconbtn" data-act="lb-zoom" data-z="0" title="Fit (0)" aria-label="Fit">${I.fit}</button>`}
    <button class="tog ${LB.cmp ? 'on' : ''}" data-act="lb-cmp" title="Compare this page across rounds (C)" ${same.length < 2 ? raw('disabled style="opacity:.5;cursor:default"') : ''}>${I.compare}<span class="hide-m">Compare rounds</span>${same.length > 1 ? ' (' + same.length + ')' : ''}</button>
    <a class="iconbtn" href="${shotUrl(s)}" target="_blank" rel="noopener" title="Open image in a new tab" aria-label="Open image">${I.ext}</a>
    <button class="iconbtn" data-act="lb-close" title="Close (Esc)" aria-label="Close">${I.close}</button>`);
  const stage = $('.lb-stage', el);
  if(LB.cmp) patch(stage, h`<div class="cmp ${s.viewport === 'mobile' ? 'mobile' : ''}">${same.map(x => { const o = shotObs(x), ne = shotErrs(x); return h`<figure><figcaption><b>Round ${x.round}</b>${x.status != null ? h`<span class="chip c mono" style="--c:${raw(x.status >= 400 ? 'var(--err)' : 'var(--ok)')}">HTTP ${x.status}</span>` : ''}${ne ? h`<span class="chip c" style="--c:var(--err)">${pluralW(ne, 'error', 'errors')}</span>` : ''}${o ? okPill(o.ok) : ''}</figcaption><img src="${shotUrl(x)}" alt="round ${x.round}"></figure>`; })}</div>`);
  else { patch(stage, h`<button class="lb-nav prev" data-act="lb-step" data-d="-1" aria-label="Previous (Left arrow)">${I.chevl}</button><img alt="${s.id}" src="${shotUrl(s)}"><button class="lb-nav next" data-act="lb-step" data-d="1" aria-label="Next (Right arrow)">${I.chev}</button>`);
    stage.classList.toggle('fit', !LB.zoom); const img = $('img', stage); if(img){ const apply = () => { img.style.width = LB.zoom ? (img.naturalWidth * LB.zoom) + 'px' : ''; img.style.maxWidth = LB.zoom ? 'none' : ''; img.style.maxHeight = LB.zoom ? 'none' : ''; }; if(img.complete) apply(); else img.onload = apply; } }
  const errs = (t, xs, c) => arr(xs).length ? card(t, h`<ul class="errlist">${arr(xs).map(x => h`<li style="color:${raw(c)}">${x}</li>`)}</ul>`, String(arr(xs).length)) : '';
  patch($('.lb-side', el), h`${card('Shot', h`<dl class="kvs"><dt>page</dt><dd class="mono">${s.path || ''}</dd><dt>viewport</dt><dd>${s.viewport || ''}</dd>${s.step ? h`<dt>step</dt><dd>${s.step}</dd>` : ''}<dt>HTTP</dt><dd>${s.status ?? '–'}</dd><dt>title</dt><dd title="${s.title || ''}">${s.title || '–'}</dd><dt>round</dt><dd>${s.round}</dd></dl>
      ${arr(s.tasks).length ? h`<div class="chips" style="margin-top:8px">${arr(s.tasks).map(x => h`<span class="chip link" data-act="lb-task" data-arg="${x}" tabindex="0" role="link">${sicon(taskById(x) ? taskById(x).state.status : 'pending')}${x}</span>`)}</div>` : ''}`)}
    ${card('UI reviewer', ob ? h`<div style="margin-bottom:6px">${okPill(ob.ok)}</div><div style="white-space:pre-wrap" class="small">${ob.observations || ''}</div>` : h`<div class="dim small">${s._r && s._r.verdict ? 'Unreviewed: the reviewer said nothing about this shot.' : 'No UI reviewer verdict for this round.'}</div>`)}
    ${errs('Step errors', s.step_errors, 'var(--err)')}${errs('Page errors', s.page_errors, 'var(--err)')}${errs('Console', s.console, 'var(--warn)')}${errs('Failed requests', s.failed_requests, 'var(--err)')}
    ${!shotErrs(s) ? card('Errors', h`<div class="small" style="color:var(--ok)">No console, page, request or step errors.</div>`) : ''}`);
}
function lbStep(d){ if(!LB.list.length) return; LB.idx = (LB.idx + d + LB.list.length) % LB.list.length; LB.cmp = false; LB.zoom = 0; $('.lb-stage', $('#lightbox')).scrollTop = 0; renderLB(); syncHash(false); }
function lbZoom(z){ if(z === 0) LB.zoom = 0; else { const img = $('#lightbox .lb-stage img'); const base = LB.zoom || (img && img.naturalWidth ? img.clientWidth / img.naturalWidth : 1); LB.zoom = Math.max(.1, Math.min(4, base * (z > 0 ? 1.25 : .8))); } renderLB(); }
/* ---------- command palette ---------- */
const PAL = {items:[], sel:0, q:''};
function palItems(){
  const it = [];
  it.push({k:'view', l:'Overview', d:D ? D.job : '', go:['overview','']});
  if(!D) return it;
  allTasks().forEach(t => it.push({k:'task', l:t.id, d:t.title || '', icon: sicon(t.state.status), go:['task', t.id]}));
  it.push({k:'file', l:'plan.json', go:['file','plan'], icon:I.json}, {k:'file', l:'job.json', go:['file','state'], icon:I.json}, {k:'file', l:'report.md', go:['file','report'], icon:I.md}, {k:'file', l:'detached output', go:['file','out'], icon:I.term});
  const ln = new Set(arr(D.logs).map(l => l.name).concat(['_integration','_devserver'])); ln.forEach(n => it.push({k:'log', l:n + '.log', go:['file','log:' + n], icon:I.log}));
  allTasks().forEach(t => it.push({k:'brief', l:t.id + ' brief', go:['file','brief:' + t.id], icon:I.md}));
  allShots().forEach(s => it.push({k:'shot', l:`${s.name || s.path} · ${s.viewport}${s.step ? ' · ' + s.step : ''}`, d:'round ' + s.round, icon: s.viewport === 'mobile' ? I.mobile : I.desktop, shot:s}));
  arr(D.review).forEach((r, i) => it.push({k:'review', l:'Review round ' + (r.round || i+1), d:human((r.verdict||{}).verdict || ''), go:['review', String(i)], icon:I.review}));
  it.push({k:'advisor', l:'Advisor: plan check', go:['advisor','plan'], icon:I.owl}, {k:'advisor', l:'Advisor: final check', go:['advisor','final'], icon:I.owl});
  findings().forEach(f => it.push({k:'finding', l:f.issue, d:f.file || '', icon:sev(f.severity).i, go:['finding', f._key]}));
  it.push({k:'view', l:'Screenshots gallery', go:['shots',''], icon:I.image}, {k:'view', l:'Metrics', go:['metrics','']});
  it.push({k:'command', l:'Toggle theme', cmd:'theme'}, {k:'command', l:'Toggle panel', cmd:'toggle-panel'}, {k:'command', l:'Show problems', cmd:'problems'}, {k:'command', l:'Show terminal', cmd:'terminal'});
  return it;
}
function fuzzy(q, s){ s = String(s||''); if(!q) return {score:0, hl: esc(s)}; const lq = q.toLowerCase(), ls = s.toLowerCase(); const at = ls.indexOf(lq);
  if(at >= 0) return {score: 1000 - at - s.length*.1, hl: esc(s.slice(0, at)) + '<b class="m">' + esc(s.slice(at, at+q.length)) + '</b>' + esc(s.slice(at+q.length))};
  let j = 0, out = '', score = 0, last = -2; for(let i=0;i<s.length;i++){ if(j < lq.length && ls[i] === lq[j]){ out += '<b class="m">' + esc(s[i]) + '</b>'; score += i === last+1 ? 8 : 2; last = i; j++; } else out += esc(s[i]); }
  return j === lq.length ? {score: score - s.length*.1, hl: out} : null; }
function openPalette(prefix, items, placeholder){
  const p = $('#palette'); if(p.hidden) PAL.ret = document.activeElement; p.hidden = false; PAL.items = items || palItems(); PAL.sel = 0;
  p.innerHTML = `<div class="pal" role="dialog" aria-modal="true" aria-label="Command palette"><input type="text" placeholder="${esc(placeholder || 'Go to a task, file, screenshot, finding or command…')}" aria-label="Search"><div class="list" role="listbox"></div><div class="hint"><span><kbd>↑</kbd> <kbd>↓</kbd> navigate</span><span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span></div></div>`;
  const inp = $('input', p); inp.value = prefix || ''; inp.focus(); inp.oninput = () => { PAL.sel = 0; renderPal(); };
  inp.onkeydown = e => { e.stopPropagation(); if(e.key === 'ArrowDown'){ PAL.sel++; renderPal(); e.preventDefault(); } else if(e.key === 'ArrowUp'){ PAL.sel--; renderPal(); e.preventDefault(); } else if(e.key === 'Enter'){ const x = PAL.shown[PAL.sel]; if(x) palGo(x); e.preventDefault(); } else if(e.key === 'Escape'){ closePalette(); }
    else if((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k'){ e.preventDefault(); closePalette(); } };
  p.onclick = e => { if(e.target === p) closePalette(); const r = e.target.closest('.it'); if(r) palGo(PAL.shown[+r.dataset.i]); };
  renderPal();
}
function renderPal(){
  const q = $('#palette input').value.trim(); const res = [];
  PAL.items.forEach(x => { const a = fuzzy(q, x.l), b = q ? fuzzy(q, x.k + ' ' + x.l + ' ' + (x.d||'')) : null; const m = a || (b && {score: b.score - 500, hl: esc(x.l)}); if(m) res.push({...x, score: m.score, hl: m.hl}); });
  if(q) res.sort((a, b) => b.score - a.score);
  PAL.shown = res.slice(0, 80); PAL.sel = Math.max(0, Math.min(PAL.sel, PAL.shown.length - 1));
  const list = $('#palette .list');
  list.innerHTML = PAL.shown.length ? PAL.shown.map((x, i) => `<div class="it ${i === PAL.sel ? 'on' : ''}" data-i="${i}" role="option" aria-selected="${i === PAL.sel}"><span class="k">${esc(x.k)}</span>${x.icon ? fmt(x.icon) : ''}<span class="lbl">${x.hl}</span>${x.d ? `<span class="d">${esc(x.d)}</span>` : ''}</div>`).join('') : '<div class="empty">No matches.</div>';
  const on = $('.it.on', list); if(on) on.scrollIntoView({block:'nearest'});
}
function closePalette(){ const p = $('#palette'); if(p.hidden) return; p.hidden = true; const r = PAL.ret; PAL.ret = null; if(r && r.isConnected && r.focus) r.focus({preventScroll:true}); }
function palGo(x){ closePalette(); if(x.tab){ if(TABS.has(x.tab)) activate(x.tab); } else if(x.go) openTab(x.go[0], x.go[1]); else if(x.shot) openShot(x.shot.id, x.shot.round); else if(x.cmd === 'theme') toggleTheme(); else if(x.cmd === 'toggle-panel'){ PANEL_OPEN = !PANEL_OPEN; store.set('panelOpen', PANEL_OPEN); renderPanel(); } else if(['problems','terminal'].includes(x.cmd)) showPanel(x.cmd); }
/* ---------- theme, layout ---------- */
function curTheme(){ return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'); }
function applyTheme(t){ if(t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; $('#themebtn').innerHTML = fmt(curTheme() === 'dark' ? I.sun : I.moon); }
function toggleTheme(){ const t = curTheme() === 'dark' ? 'light' : 'dark'; store.set('theme', t); applyTheme(t); }
applyTheme(qp.get('theme') || store.get('theme', null));
const isMobile = () => matchMedia('(max-width:760px)').matches;
function closeMobileSide(){ $('#sidebar').classList.remove('mopen'); }
function applySide(){ const sb = $('#sidebar'); if(isMobile()){ sb.style.display = ''; } else { sb.style.display = SIDE_HIDDEN ? 'none' : ''; $('#sash-side').style.display = SIDE_HIDDEN ? 'none' : ''; } }
document.documentElement.style.setProperty('--side-w', store.get('sideW', 280) + 'px');
document.documentElement.style.setProperty('--panel-h', store.get('panelH', 240) + 'px');
function setSide(w){ w = Math.max(170, Math.min(600, w)); document.documentElement.style.setProperty('--side-w', w + 'px'); store.set('sideW', w); }
function setPanelH(hh){ const r = $('.work').getBoundingClientRect(); hh = Math.max(90, Math.min(r.height - 120, hh)); document.documentElement.style.setProperty('--panel-h', hh + 'px'); store.set('panelH', hh); }
function sash(el, axis){
  el.addEventListener('pointerdown', e => { e.preventDefault(); el.classList.add('drag'); el.setPointerCapture(e.pointerId);
    const mv = ev => { if(axis === 'x') setSide(ev.clientX - 48); else setPanelH($('.work').getBoundingClientRect().bottom - ev.clientY); };
    const up = () => { el.classList.remove('drag'); el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up); };
    el.addEventListener('pointermove', mv); el.addEventListener('pointerup', up); });
  el.addEventListener('keydown', e => { const d = e.shiftKey ? 64 : 16;
    if(axis === 'x' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')){ e.preventDefault(); setSide($('#sidebar').getBoundingClientRect().width + (e.key === 'ArrowRight' ? d : -d)); }
    if(axis === 'y' && (e.key === 'ArrowUp' || e.key === 'ArrowDown')){ e.preventDefault(); setPanelH($('#panel').getBoundingClientRect().height + (e.key === 'ArrowUp' ? d : -d)); } });
}
sash($('#sash-side'), 'x'); sash($('#sash-panel'), 'y');
function showPanel(k){ PANEL = k; PANEL_OPEN = true; store.set('panelTab', k); store.set('panelOpen', true); renderPanel(); }
/* ---------- events ---------- */
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('on'); clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('on'), 1400); }
async function copyText(s){ try{ await navigator.clipboard.writeText(s); toast('Copied'); }catch(e){ const ta = document.createElement('textarea'); ta.value = s; document.body.appendChild(ta); ta.select(); try{ document.execCommand('copy'); toast('Copied'); }catch(_){ toast('Copy failed'); } ta.remove(); } }
function refreshActive(){ const t = TABS.get(ACTIVE); if(t && D) runUpdate(t); safe('side', renderSide); safe('panel', renderPanel); }
const ACTIONS = {
  open: d => openTab(d.kind, d.arg),
  tab: d => activate(d.id),
  'close-tab': (d, e) => { e.stopPropagation(); closeTab(d.id); },
  'tabs-more': () => openPalette('', openTabItems(), 'Switch to an open tab…'),
  toggle: d => { OPEN.has(d.key) ? OPEN.delete(d.key) : OPEN.add(d.key); store.set('open', Array.from(OPEN)); renderSide(); renderPanel(); },
  'toggle-inv': d => { const k = 'x' + d.key; OPEN.has(k) ? OPEN.delete(k) : OPEN.add(k); store.set('open', Array.from(OPEN)); renderSide(); renderPanel(); },
  'collapse-all': () => { Array.from(OPEN).forEach(k => { if(/^w\d|^extra$|^logs$/.test(k)) OPEN.delete(k); }); store.set('open', Array.from(OPEN)); renderSide(); },
  view: d => { const sb = $('#sidebar');
    if(isMobile()){ if(VIEW === d.view && sb.classList.contains('mopen')) sb.classList.remove('mopen'); else { VIEW = d.view; sb.classList.add('mopen'); } }
    else if(VIEW === d.view){ SIDE_HIDDEN = !SIDE_HIDDEN; store.set('sideHidden', SIDE_HIDDEN); applySide(); } else { VIEW = d.view; SIDE_HIDDEN = false; store.set('sideHidden', false); applySide(); }
    store.set('view', VIEW); renderSide();
    if(d.view === 'shots' && allShots().length && !isMobile()) openTab('shots', ''); if(d.view === 'metrics' && !isMobile()) openTab('metrics', ''); },
  'toggle-side': () => { SIDE_HIDDEN = !SIDE_HIDDEN; store.set('sideHidden', SIDE_HIDDEN); applySide(); renderActivity(); },
  'toggle-panel': () => { PANEL_OPEN = !PANEL_OPEN; store.set('panelOpen', PANEL_OPEN); renderPanel(); },
  panel: d => showPanel(d.panel),
  theme: () => toggleTheme(),
  palette: () => openPalette(),
  copy: d => copyText(d.text),
  banners: () => { BANNERS_OPEN = !BANNERS_OPEN; safe('banners', renderBanners); },
  shot: d => openShot(d.id, d.round),
  'calls-all': d => { CALLS_ALL.add(d.key); refreshActive(); },
  'clear-filters': () => { SF.round = SF.viewport = SF.task = 'all'; refresh(); },
  'lb-close': () => closeLB(), 'lb-step': d => lbStep(+d.d), 'lb-zoom': d => lbZoom(+d.z),
  'lb-cmp': () => { LB.cmp = !LB.cmp; renderLB(); syncHash(false); }, 'lb-task': d => { closeLB(true); openTab('task', d.arg, {sub:'shots'}); },
};
document.addEventListener('click', e => { if(e.target.closest('summary')) return; const a = e.target.closest('[data-act]'); if(!a || a.disabled) return; const f = ACTIONS[a.dataset.act]; if(f){ if(a.tagName === 'A') return; f(a.dataset, e, a); } });
document.addEventListener('auxclick', e => { if(e.button === 1){ const t = e.target.closest('.tab'); if(t){ e.preventDefault(); closeTab(t.dataset.id); } } });
document.addEventListener('change', e => { const f = e.target.closest('[data-filter]'); if(f){ SF[f.dataset.filter] = f.value; refresh(); } });
$('#tabs').addEventListener('wheel', e => { const el = e.currentTarget; if(el.scrollWidth > el.clientWidth && Math.abs(e.deltaY) > Math.abs(e.deltaX)){ el.scrollLeft += e.deltaY; e.preventDefault(); } }, {passive:false});
const typing = el => !!el && (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable);
function moveFocus(cur, d){ /* arrow keys walk the focusable rows of the list the focus is in */
  const box = cur.closest('#side-body, #panel-body, .feed, #tabs, .lb-side'); if(!box) return false;
  const items = $$('[tabindex="0"]', box).filter(x => x.offsetParent !== null); const i = items.indexOf(cur); if(i < 0) return false;
  const n = items[i + d]; if(n){ n.focus(); n.scrollIntoView({block:'nearest', inline:'nearest'}); } return true; }
document.addEventListener('keydown', e => {
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase(), palOpen = !$('#palette').hidden;
  if(mod && k === 'k'){ e.preventDefault(); palOpen ? closePalette() : openPalette(); return; }
  if(palOpen) return; /* the palette's input handles its own keys */
  if(LB.open){
    if(e.key === 'Tab'){ const f = $$('#lightbox button:not([disabled]), #lightbox a[href], #lightbox [tabindex="0"]').filter(x => x.offsetParent !== null); if(f.length){ const i = f.indexOf(document.activeElement), n = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i < 0 || i >= f.length - 1 ? 0 : i + 1); f[n].focus(); e.preventDefault(); } return; }
    if(typing(e.target) || mod) return;
    if(e.key === 'Escape') closeLB(); else if(e.key === 'ArrowLeft') lbStep(-1); else if(e.key === 'ArrowRight') lbStep(1); else if(e.key === '+' || e.key === '=') lbZoom(1); else if(e.key === '-') lbZoom(-1); else if(e.key === '0') lbZoom(0); else if(k === 'c') { LB.cmp = !LB.cmp; renderLB(); syncHash(false); }
    else if((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[data-act][tabindex="0"]:not(button):not(a)')){ e.preventDefault(); e.target.click(); }
    return;
  }
  if(mod && k === 'j'){ e.preventDefault(); ACTIONS['toggle-panel'](); return; }
  if(mod && k === 'b'){ e.preventDefault(); ACTIONS['toggle-side'](); return; }
  if(e.key === 'Escape'){ if($('#sidebar').classList.contains('mopen')) closeMobileSide(); return; }
  if(mod && k === 'w' && ACTIVE && ACTIVE !== 'overview:'){ e.preventDefault(); closeTab(ACTIVE); return; }
  if(typing(e.target) || mod) return;
  const t = e.target;
  if((e.key === 'Enter' || e.key === ' ') && t.matches && t.matches('[data-act]:not(button):not(a):not(select)')){ e.preventDefault(); t.click(); return; }
  if((e.key === 'ArrowDown' || e.key === 'ArrowUp') && t.matches && t.matches('[tabindex="0"]') && moveFocus(t, e.key === 'ArrowDown' ? 1 : -1)) e.preventDefault();
  if((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && t.matches && t.matches('.tab') && moveFocus(t, e.key === 'ArrowRight' ? 1 : -1)) e.preventDefault();
  if(e.key === 'Delete' && t.matches && t.matches('.tab') && t.dataset.id !== 'overview:'){ e.preventDefault(); closeTab(t.dataset.id); }
});
$('#lightbox').addEventListener('click', e => { if(e.target.classList.contains('lb-stage') || e.target.classList.contains('lb-main')) closeLB(); });
$('#jobsel').addEventListener('change', e => switchJob(e.target.value));
document.addEventListener('pointerdown', e => { if(isMobile() && $('#sidebar').classList.contains('mopen') && !e.target.closest('#sidebar') && !e.target.closest('#activity')) closeMobileSide(); });
/* ---------- ticking timers ---------- */
function tickIn(root){ const n = nowS();
  $$('[data-from]', root).forEach(el => { const a = parseFloat(el.dataset.from); if(!a){ el.textContent = '–'; return; } const b = parseFloat(el.dataset.to); el.textContent = dur((b || n) - a); });
  $$('[data-ago]', root).forEach(el => { el.textContent = ago(parseFloat(el.dataset.ago)); }); }
setInterval(() => { $$('[data-from]').forEach(el => { if(!el.dataset.to) { const a = parseFloat(el.dataset.from); if(a) el.textContent = dur(nowS() - a); } }); $$('[data-ago]').forEach(el => { el.textContent = ago(parseFloat(el.dataset.ago)); }); }, 1000);
/* ---------- data loading ---------- */
function renderJobSel(){
  const opts = JOBLIST.slice(); if(JOB && !opts.some(j => j.job === JOB)) opts.unshift({job: JOB, status: 'waiting', done: 0, total: 0});
  patch($('#jobsel'), opts.length ? opts.map(j => h`<option value="${j.job}" ${j.job === JOB ? raw('selected') : ''}>${j.job} · ${stt(j.status).l}${j.total ? ' · ' + j.done + '/' + j.total : ''}</option>`) : h`<option value="">no jobs yet</option>`);
}
async function loadJobs(){ try{ const r = await fetch('/api/jobs', {cache:'no-store'}); if(!r.ok) throw new Error('HTTP ' + r.status); const j = await r.json(); JOBLIST = Array.isArray(j) ? j.filter(x => x && typeof x.job === 'string') : []; NETERR = false; }catch(e){ NETERR = true; }
  if(!JOB && JOBLIST.length){ JOB = JOBLIST[0].job; history.replaceState(null, '', '?job=' + encodeURIComponent(JOB) + location.hash); } safe('jobs', renderJobSel); }
function switchJob(j){ if(!j || j === JOB) return; JOB = j; history.replaceState(null, '', '?job=' + encodeURIComponent(JOB)); D = null; SIG = ''; ETAG = null; MISSING = false;
  SF.round = SF.viewport = SF.task = 'all'; CALLS_ALL.clear(); RENDER_ERRS.clear();
  Array.from(TABS.keys()).forEach(id => { if(id !== 'overview:'){ TABS.get(id).el.remove(); TABS.delete(id); } }); ORDER = ['overview:']; ACTIVE = 'overview:'; closeLB(true);
  [PP.term, PP.out].forEach(x => x && x.reset()); renderJobSel(); load(); }
function signature(d){ const c = {...d}; delete c.now; return JSON.stringify(c); }
let ETAG = null, SERVERR = null;
async function load(){
  if(!JOB){ await loadJobs(); if(!JOB){ refresh(); return; } }
  const job = JOB; let r, d = null;
  try{ /* the network: a failure here is "lost contact"; a rendering failure below is not */
    r = await fetch('/api/job/' + encodeURIComponent(job), {cache:'no-store', headers: ETAG ? {'If-None-Match': ETAG} : {}});
    if(r.status >= 500){ let why = 'HTTP ' + r.status; try{ const j = await r.json(); if(j && j.error) why = String(j.error); }catch(_){}
      if(job !== JOB) return; NETERR = false; SERVERR = why; LASTOK = Date.now()/1000; safe('banners', renderBanners); safe('status', renderStatus); return; }
    if(r.status !== 404 && r.status !== 304 && !r.ok) throw new Error('HTTP ' + r.status);
    SERVERR = null;
    if(r.ok) d = await r.json();
    NETERR = false; LASTOK = Date.now()/1000;
  }catch(e){ if(job !== JOB) return; NETERR = true; safe('banners', renderBanners); safe('status', renderStatus); return; }
  if(job !== JOB) return;
  if(r.status === 404){ ETAG = null; MISSING = true; D = null; SIG = ''; refresh(); return; }
  if(r.status === 304 && D){ refreshLive(); return; }
  if(!d || typeof d !== 'object'){ ETAG = null; return; }
  ETAG = r.headers.get('ETag'); MISSING = false; FETCHED = Date.now()/1000;
  const sig = signature(d); D = d;
  if(sig !== SIG){ SIG = sig; refresh(); } else refreshLive();
}
function refreshLive(){ /* unchanged state: only live logs */ const t = TABS.get(ACTIVE);
  if(t && t.log && (t.kind !== 'task' || t.sub === 'log')) t.log.load();
  if(t && t.kind === 'task' && t.sub === 'brief' && !t.brief.text) loadBrief(t);
  if(PANEL_OPEN && PANEL === 'terminal') PP.term.load(); if(PANEL_OPEN && PANEL === 'output') PP.out.load(); safe('status', renderStatus); safe('banners', renderBanners); }
function refresh(){
  safe('title', () => { document.title = D ? `${counts().done}/${counts().total} · ${D.job} · orchestrate` : JOB ? `waiting · ${JOB} · orchestrate` : 'orchestrate'; });
  safe('status', renderStatus); safe('side', renderSide); safe('panel', renderPanel); safe('tabs', renderTabs);
  const ov = TABS.get('overview:');
  if(!D){ patch(ov.el, waitingHtml()); ov.el._waiting = true; safe('banners', renderBanners); return; }
  if(ov.el._waiting){ ov.el._waiting = false; ov.el.innerHTML = ''; ov.el._h = null; KINDS.overview.mount(ov); }
  const t = TABS.get(ACTIVE); if(t) runUpdate(t);
  if(PENDING_HASH) applyHash();
  if(LB.open) safe('lightbox', () => { const cur = LB.list[LB.idx]; const fresh = allShots(); const i = cur ? fresh.findIndex(s => s.id === cur.id && s.round === cur.round) : -1; if(i >= 0){ LB.list = LB.list.map(s => fresh.find(f => f.id === s.id && f.round === s.round) || s); renderLB(); } });
  safe('banners', renderBanners);
}
function waitingHtml(){
  if(!JOB) return h`<div class="welcome"><div class="box"><div class="logo">${I.plan}</div><h2>No jobs yet</h2><p>Start one with <span class="code-inline">orchestrate.py run plan.json</span>. This page picks it up by itself.</p><div class="dots"><i></i><i></i><i></i></div></div></div>`;
  return h`<div class="welcome"><div class="box"><div class="logo">${I.plan}</div><h2>Waiting for job <span class="mono">${JOB}</span> to start</h2>
    <p>The run has not written its state yet, and there is no plan at plans/${JOB}.json to show. This page checks every 2 seconds and switches to the live view the moment the job starts.</p><div class="dots"><i></i><i></i><i></i></div>
    ${JOBLIST.length ? h`<div class="joblinks">${JOBLIST.slice(0, 6).map(j => h`<div class="row" data-act="job" data-job="${j.job}" tabindex="0" role="button">${sicon(j.status)}<span class="lbl mono">${j.job}</span><span class="meta">${stt(j.status).l}${j.total ? ' · ' + j.done + '/' + j.total : ''}</span></div>`)}</div>` : ''}</div></div>`;
}
ACTIONS.job = d => switchJob(d.job);
/* The server was restarted on newer (or older) dashboard.py: this page's code is stale, so reload it.
   The URL keeps ?job= and the #hash, so the view comes back. Only reload on a known, different value. */
async function checkCode(){ try{ const r = await fetch('/api/ping', {cache:'no-store'}); if(!r.ok) return; const p = await r.json();
  if(FINGERPRINT && p && typeof p.fingerprint === 'string' && p.fingerprint && p.fingerprint !== FINGERPRINT) location.reload(); }catch(e){} }
/* ---------- boot ---------- */
(function boot(){
  const ov = {id:'overview:', kind:'overview', arg:'', el: document.createElement('div')}; ov.el.className = 'pane'; $('#editor-body').appendChild(ov.el);
  TABS.set(ov.id, ov); ORDER = [ov.id]; ACTIVE = ov.id; ov.el.classList.add('on'); KINDS.overview.mount(ov);
  mountPanel(); applySide(); safe('tabs', renderTabs); safe('side', renderSide); safe('panel', renderPanel); safe('status', renderStatus);
  loadJobs().then(load); setInterval(load, 2000); setInterval(loadJobs, 10000); setInterval(checkCode, 5000);
  addEventListener('resize', () => { applySide(); tabsOverflow(); });
  if(window.ResizeObserver) new ResizeObserver(() => tabsOverflow()).observe($('#tabs')); /* side bar drags too */
})();
</script></body></html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", nargs="?", help="job to show first (default: most recent)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (keep 127.0.0.1 unless you mean it)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help=f"also answer this Host name, e.g. a reverse proxy's (repeatable; ${HOSTS_ENV} too)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    ap.add_argument("--idle-hours", type=float, default=None, metavar="H",
                    help=f"exit after H hours with no request and no running job (default ${IDLE_ENV} or "
                         f"{IDLE_HOURS:g}; 0 = never)")
    a = ap.parse_args(argv)
    try:
        srv = make_server(a.host, a.port, a.job, a.allow_host)
    except OSError as e:
        print(f"orchestrate dashboard: cannot listen on {a.host}:{a.port}: {e}", file=sys.stderr, flush=True)
        return 1
    port = srv.server_address[1]
    url = f"http://{a.host}:{port}/" + (f"?job={a.job}" if a.job else "")
    print(f"orchestrate dashboard: {url}  (state: {JOBS})", flush=True)
    if a.open:
        webbrowser.open(url)
    watch_idle(srv, idle_hours() if a.idle_hours is None else a.idle_hours)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
