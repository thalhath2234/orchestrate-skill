#!/usr/bin/env python3
"""Tests for dashboard.py: every endpoint against a synthetic job, with real PNG screenshots.

Run from the repo root: python3 tests/test_dashboard.py -v
"""
from __future__ import annotations  # `dict | None` in signatures on Python 3.9

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from http.client import HTTPConnection
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "orchestrate" / "scripts"
TMP = tempfile.mkdtemp(prefix="orch-dash-")
os.environ["ORCHESTRATE_DIR"] = TMP  # before importing router/dashboard: they read it at import
os.environ["ORCHESTRATE_DASHBOARD"] = "0"
sys.path.insert(0, str(SCRIPTS))
import dashboard  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)

JOB = "share-links"


def png(w: int = 4, h: int = 3, rgb=(40, 120, 220)) -> bytes:
    """A real, minimal RGB PNG."""
    row = b"\x00" + bytes(rgb) * w
    raw = row * h

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_job(root: Path) -> dict:
    jd = root / "jobs" / JOB
    (jd / "logs").mkdir(parents=True)
    (jd / "briefs").mkdir()
    wt = jd / "wt" / "_integration"
    shots = wt / ".orchestrate" / "screenshots"
    (shots / "round-1").mkdir(parents=True)
    (wt / "apps" / "web").mkdir(parents=True)
    (wt / "apps" / "web" / "share.tsx").write_text("line1\nline2\nexport const x = 1\n")
    (wt / ".env").write_text("SECRET=1\n")
    for name in ("share-view--desktop", "share-view--mobile", "home--desktop", "share-view--desktop--after-click"):
        (shots / "round-1" / f"{name}.png").write_bytes(png())
    (shots / "round-1" / "notes.txt").write_text("not an image")
    outside = root / "outside.png"
    outside.write_bytes(png(rgb=(255, 0, 0)))
    (shots / "round-1" / "escape.png").symlink_to(outside)
    (shots / "round-1" / "passwd.png").symlink_to("/etc/passwd")
    plan = {
        "job": JOB, "goal": "Shareable read-only schedule links", "repo": str(root / "repo"),
        "definition_of_done": "A user can share a link <b>and</b> it opens read-only.",
        "acceptance": ["share link opens read-only", "revoked links 404"],
        "decisions": ["links are unguessable tokens"], "non_goals": ["editing via link"],
        "verify": ["make test"], "budget_usd": 20,
        "ui": {"dev": "pnpm --filter web dev", "url": "http://127.0.0.1:5173", "pages": [{"path": "/"}]},
        "tasks": [
            {"id": "api-share", "title": "API", "goal": "add share endpoints", "type": "feature:api", "tier": "T2",
             "owns": ["apps/api/**"], "done": ["a", "b"], "verify": ["make test"]},
            {"id": "web-share", "title": "Web <script>alert(1)</script>", "goal": "share page", "type": "feature:web",
             "tier": "T2", "owns": ["apps/web/**"], "depends_on": ["api-share"], "done": ["a", "b"],
             "verify": ["make test"], "ui": [{"path": "/share/demo", "name": "share-view"}]},
            "not-a-task",
        ],
    }
    now = time.time()
    shot = lambda sid, name, path, vp, step=None, tasks=("web-share",), **kw: {  # noqa: E731
        "id": sid, "name": name, "path": path, "viewport": vp, "step": step, "file": sid + ".png",
        "status": 200, "title": "Share", "console": [], "page_errors": [], "failed_requests": [],
        "step_errors": [], "tasks": list(tasks), **kw}
    state = {
        "job": JOB, "repo": str(root / "repo"), "base": "main", "base_sha": "abcdef1234567890",
        "branch": f"orchestrate/{JOB}", "integration_wt": str(wt), "created": now - 600, "status": "running",
        "pid": os.getpid(), "mode": "headless", "cost_usd": 3.25, "approved": [],
        "screenshots_root": str(shots), "dashboard": f"http://127.0.0.1:8765/?job={JOB}",
        "tasks": {
            "api-share": {"status": "done", "title": "API", "type": "feature:api", "tier": "T2", "started": now - 500,
                          "ended": now - 300, "cost_usd": 1.2, "files": ["apps/api/share.go"],
                          "steps": [{"config": "sonnet/high", "ok": True, "cost_usd": 1.2}],
                          "report": {"summary": "added endpoints", "open_issues": [{"kind": "fyi", "text": "x"}]}},
            "web-share": {"status": "running", "title": "Web", "type": "feature:web", "tier": "T2",
                          "started": now - 200, "slot": 0},
            "old-task": {"status": "split", "title": "Old", "type": "feature:web", "tier": "T3",
                         "split_into": ["web-share"]},
        },
        "calls": [{"t": now - 400, "role": "subagent", "task": "api-share", "config": "sonnet/high", "cost_usd": 1.2},
                  {"t": now - 100, "role": "ui_reviewer", "task": None, "config": "opus/medium", "cost_usd": 0.4}],
        "manager": {"interventions": 1, "log": [{"t": now - 250, "event": "task_failed", "task": "web-share",
                                                 "note": "retry with notes", "actions": [{"action": "retry", "task": "web-share"}]}]},
        "review": {"rounds": [{
            "round": 1, "final_verify_ok": True, "final_verify_tail": "$ make test\nok\n",
            "verdict": {"verdict": "changes_requested", "summary": "one blocker",
                        "acceptance": [{"criterion": "share link opens read-only", "evidence": "share.tsx:3", "met": True}],
                        "findings": [{"severity": "blocker", "issue": "[UI] button overlaps on mobile",
                                      "file": "apps/web/share.tsx", "line": 3, "confidence": "high"}]},
            "ui": {"ran": True, "skipped": None, "error": None, "url": "http://127.0.0.1:5173",
                   "dir": str(shots / "round-1"),
                   "shots": [shot("round-1/share-view--desktop", "share-view", "/share/demo", "desktop"),
                             shot("round-1/share-view--mobile", "share-view", "/share/demo", "mobile",
                                  console=["warn: x"], page_errors=["TypeError: y"]),
                             shot("round-1/share-view--desktop--after-click", "share-view", "/share/demo", "desktop",
                                  step="after-click", step_errors=["#copy never appeared"]),
                             shot("round-1/home--desktop", "home", "/", "desktop", tasks=()),
                             "garbage"],
                   "verdict": {"verdict": "changes_requested", "summary": "mobile overlap",
                               "pages": [{"shot": "round-1/share-view--mobile", "observations": "overlap", "ok": False}],
                               "findings": [{"severity": "blocker", "issue": "button overlaps on mobile",
                                             "confidence": "high"}]}},
        }]},
    }
    (jd / "plan.json").write_text(json.dumps(plan))
    (jd / "job.json").write_text(json.dumps(state))
    (jd / "logs" / "api-share.log").write_text("\x1b[32mstarting\x1b[0m\n" + "x" * 5000 + "\nEND\n")
    (jd / "logs" / "_integration.log").write_text("merged api-share\n")
    (jd / "logs" / "_devserver.log").write_text("VITE ready on 5173\n")
    (jd / "briefs" / "api-share.md").write_text("# Brief\nDo the thing.\n")
    (jd / "report.md").write_text("# Report\nAll good.\n")
    (root / "jobs" / f"{JOB}.out").write_text("detached run output\nline 2\n")
    # an old, partial job: state without plan fields the dashboard expects
    (root / "jobs" / "old-job").mkdir()
    (root / "jobs" / "old-job" / "job.json").write_text(json.dumps({"status": "done", "tasks": {"a": {"status": "done"}},
                                                                     "review": {"rounds": [{"round": 1}]}}))
    # a broken job
    (root / "jobs" / "broken").mkdir()
    (root / "jobs" / "broken" / "job.json").write_text("{not json")
    return state


def write_job(root: Path, name: str, plan: dict | None, state: dict | None) -> Path:
    jd = root / "jobs" / name
    (jd / "logs").mkdir(parents=True, exist_ok=True)
    if plan is not None:
        (jd / "plan.json").write_text(json.dumps(plan))
    if state is not None:
        (jd / "job.json").write_text(json.dumps(state))
    return jd


def make_more(root: Path, share_state: dict) -> None:
    """Odd and rich jobs: hand-edited plans, wrong-typed state, a plan not run yet, a busy job."""
    now = time.time()
    # `done`, `owns`, `depends_on`, `verify` as strings: `orchestrate.py validate` lets a string done through
    jd = write_job(root, "strdone", {
        "job": "strdone", "goal": "health", "acceptance": "health endpoint answers",
        "tasks": [{"id": "db", "title": "DB", "goal": "g", "owns": "apps/db/**", "done": "DB-DONE-STRING", "verify": "make db"},
                  {"id": "health", "title": "Health", "goal": "g", "owns": ["apps/api/**"], "depends_on": "db",
                   "done": "HEALTH-DONE-STRING", "verify": "make test", "notes": "a note", "ui": "nope"}]},
        {"status": "running", "pid": os.getpid(), "mode": "headless", "created": now - 100, "cost_usd": 1,
         "tasks": {"db": {"status": "done", "started": now - 90, "ended": now - 50},
                   "health": {"status": "running", "started": now - 40, "steps": "x", "files": "a.go"}}})
    # a 1.2 MB log: the page loads its tail and says so
    (jd / "logs" / "db.log").write_text("".join(f"line {i:06d} " + "y" * 60 + "\n" for i in range(18000)) + "LAST-LINE\n")
    # wrong types everywhere the page iterates
    write_job(root, "badtypes", {
        "job": "badtypes", "goal": "x", "tasks": [{"id": "a", "title": "A", "depends_on": "b", "done": ["d"]},
                                                  {"id": "b", "title": "B", "done": ["d"], "owns": 7}]},
        {"status": "running", "pid": os.getpid(), "cost_usd": "abc", "created": "soon",
         "tasks": {"a": {"status": "done", "report": "plain text report", "steps": {"x": 1}}, "b": "garbage"},
         "manager": {"log": [{"t": "x", "event": "task_failed", "actions": {"action": "retry"}}, "junk"]},
         "advice": {"plan": {"verdict": "proceed"}, "final": ["junk"]},
         "calls": "none",
         "review": {"rounds": [{"round": 1, "verdict": {"verdict": "changes_requested", "findings": {"a": 1},
                                                      "acceptance": "x"}, "ui": {"shots": "x", "verdict": 3}},
                               "junk"]}})
    # planned, not run: only plans/<job>.json exists
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "planned-job.json").write_text(json.dumps({
        "job": "planned-job", "goal": "Planned goal", "definition_of_done": "users can X",
        "tasks": [{"id": "only", "title": "Only task", "goal": "g", "owns": ["a/**"], "done": ["d1", "d2"]}]}))
    # a rich running job for the browser: gates, advisor, two rounds (the second without a verdict or time),
    # many calls, many manager entries, a wide screenshot
    plan = json.loads((root / "jobs" / JOB / "plan.json").read_text())
    plan["job"] = "xjob"
    plan["tasks"][1]["gate"] = "deploy"
    st = json.loads(json.dumps(share_state))
    st["job"] = "xjob"
    shots = Path(st["screenshots_root"])
    (shots / "round-1" / "wide--desktop.png").write_bytes(png(1600, 120, (250, 250, 250)))
    st["review"]["rounds"][0]["ui"]["shots"].append({"id": "round-1/wide--desktop", "name": "wide", "path": "/wide",
        "viewport": "desktop", "file": "round-1/wide--desktop.png", "status": 200, "tasks": ["web-share"]})
    st["tasks"]["web-share"].update(status="awaiting_approval", started=None)
    st["advice"] = {"plan": [{"verdict": "revise", "summary": "ownership gap", "done_restated": "PLAN-RESTATED-DOD",
                              "config": "fable/medium", "escalated": False, "t": now - 590,
                              "findings": [{"severity": "major", "issue": "web-share does not own share.css",
                                            "file": "apps/web/share.css", "line": 3, "confidence": "high",
                                            "suggestion": "Add it to web-share owns"}]}],
                    "final": [{"verdict": "proceed", "summary": "fine", "done_restated": "FINAL-RESTATED-DOD",
                               "config": "fable/high", "escalated": True, "findings": []}]}
    st["review"]["rounds"].append({"round": 2, "final_verify_ok": False, "final_verify_tail": "$ make test\nFAIL\n",
                                   "verdict": {"verdict": "changes_requested", "summary": "final verify failed", "findings": []}})
    st["calls"] = [{"t": now - 500 + i, "role": "subagent", "task": "api-share", "config": "sonnet/high", "cost_usd": 0.01}
                   for i in range(400)]
    st["manager"]["log"].append({"t": now - 240, "event": "task_failed", "task": "api-share", "note": "second look",
                                 "actions": [{"action": "split", "task": "api-share", "into": [{"id": "api-a"}, {"id": "api-b"}]}],
                                 "rejected": ["unknown task zzz"]})
    write_job(root, "xjob", plan, st)


def make_checks(root: Path) -> None:
    """Jobs whose checks did not all run, that wait on the user, or hold odd state the page must survive."""
    now = time.time()
    plan = {"job": "overridden", "goal": "Share links", "definition_of_done": "links work",
            "decisions": ["links are tokens", "tokens expire after 7 days (decided by the user during job overridden)"],
            "tasks": [{"id": "db-share-tokens", "title": "DB", "goal": "g", "owns": ["db/**"], "done": ["d"],
                       "gate": "migration"},
                      {"id": "api", "title": "API", "goal": "g", "owns": ["api/**"], "done": ["d"]}]}
    write_job(root, "overridden", plan, {
        "job": "overridden", "status": "done", "mode": "headless", "created": now - 900, "finished": now - 60,
        "branch": "orchestrate/overridden", "base": "main", "base_sha": "abcdef1234", "cost_usd": 2,
        "advice_plan_ok": "approved by user", "baseline_skipped": True,
        "approved": ["db-share-tokens", "advisor:plan", "advisor:final"],
        "decisions_added": ["tokens expire after 7 days (decided by the user during job overridden)"],
        "tasks": {"db-share-tokens": {"status": "done", "merge_verify": {
                      "cmds": ["make db-test"], "ok": True, "files": ["db/schema.sql"]}},
                  "api": {"status": "failed", "merge_verify": {
                      "cmd": "make api-test", "ok": False, "tail": "MERGE-VERIFY-TAIL: 2 tests failed\n",
                      "files": ["api/routes.go"]}}},
        "advice": {"plan": [{"verdict": "revise", "summary": "PLAN-SAYS-REVISE", "done_restated": "x",
                             "findings": [{"severity": "blocker", "issue": "no index"}]}]},
        "review": {"rounds": [{"round": 1, "final_verify_ok": True,
                               "verdict": {"verdict": "approve", "summary": "ok", "findings": []},
                               "advisor": {"overridden_by_user": True}}]}})
    # waits on a question and a gate, with a warning: three notices for a phone screen
    write_job(root, "asks", {"job": "asks", "goal": "g", "tasks": [
        {"id": "deploy", "title": "Deploy", "goal": "g", "owns": ["d/**"], "done": ["d"], "gate": "prod"}]}, {
        "job": "asks", "status": "awaiting_human", "mode": "headless", "created": now - 100,
        "human_question": "Keep the old links working?", "warnings": ["ASKS-WARNING: disk almost full"],
        "tasks": {"deploy": {"status": "awaiting_approval"}}})
    # a call with no role: the metrics key it gives must not break the ETag or the page
    write_job(root, "nullrole", {"job": "nullrole", "goal": "g", "tasks": []}, {
        "job": "nullrole", "status": "running", "pid": os.getpid(), "mode": "headless", "created": now - 50,
        "calls": [{"t": now - 40, "role": None, "cost_usd": 0.5}, {"t": now - 30, "role": "subagent", "cost_usd": 1}]})
    # absurdly nested JSON: json.loads raises RecursionError, not ValueError
    write_job(root, "deep", None, None)
    (root / "jobs" / "deep" / "job.json").write_text("[" * 200000)
    # a planned job with choices the planner made for the user
    (root / "plans" / "assumed-job.json").write_text(json.dumps({
        "job": "assumed-job", "goal": "g", "assumptions": ["ASSUMED: links live in sqlite, not redis"],
        "tasks": [{"id": "only", "title": "Only", "goal": "g", "owns": ["a/**"], "done": ["d"]}]}))
    # symlinks out of the state dir, and into the worktree's .git
    secret = root / "secret-outside.txt"
    secret.write_text("TOP-SECRET-OUTSIDE\n")
    jd = write_job(root, "symjob", None, None)
    wt = jd / "wt"
    (wt / ".git").mkdir(parents=True)
    (wt / ".git" / "config").write_text("[remote] url = https://user:TOKEN@example.com/x\n")
    (wt / "apps").mkdir()
    (wt / "apps" / "ok.txt").write_text("plain file\n")
    (wt / "apps" / "gitcfg").symlink_to(wt / ".git" / "config")
    (jd / "briefs").mkdir()
    (jd / "logs" / "evil.log").symlink_to(secret)
    (jd / "briefs" / "evil.md").symlink_to(secret)
    (jd / "report.md").symlink_to(secret)
    (root / "jobs" / "symjob.out").symlink_to(secret)
    (jd / "plan.json").write_text(json.dumps({"job": "symjob", "goal": "g", "tasks": []}))
    (jd / "job.json").write_text(json.dumps({"job": "symjob", "status": "done", "integration_wt": str(wt),
                                             "tasks": {"t": {"status": "done", "files": ["apps/gitcfg", "apps/ok.txt"]}}}))


_MADE = {}


def fixtures() -> dict:
    if not _MADE:
        root = Path(TMP)
        _MADE["state"] = make_job(root)
        make_more(root, _MADE["state"])
        make_checks(root)
    return _MADE["state"]


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(TMP)
        cls.state = fixtures()
        cls.srv = dashboard.make_server("127.0.0.1", 0, JOB)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path, host=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers={"Host": host} if host else {})
        r = c.getresponse()
        body = r.read()
        self.last_headers = dict(r.getheaders())
        c.close()
        return r.status, r.getheader("Content-Type") or "", body

    def json(self, path):
        code, ctype, body = self.get(path)
        self.assertEqual(code, 200, body[:300])
        self.assertIn("application/json", ctype)
        return json.loads(body)

    # ---- page
    def test_page_served_with_csp_and_default_job(self):
        code, ctype, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        html = body.decode()
        csp = self.last_headers.get("Content-Security-Policy", "")
        for part in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "img-src 'self' data:"):
            self.assertIn(part, csp)
        self.assertEqual(self.last_headers.get("Cross-Origin-Resource-Policy"), "same-origin")
        self.assertIn(f'const DEFAULT_JOB = "{JOB}"', html)
        self.assertNotIn("__DEFAULT_JOB__", html)
        self.assertIn("Waiting for job", html)  # the page handles a job that does not exist yet
        # self-contained: no external scripts, styles or fonts
        for bad in ("<script src", "https://", "@import", "fonts.googleapis"):
            self.assertNotIn(bad, html)

    def test_missing_job_page_still_served(self):
        code, _, body = self.get("/?job=not-started-yet")
        self.assertEqual(code, 200)
        self.assertIn(b"orchestrate", body)
        code, _, _ = self.get("/api/job/not-started-yet")
        self.assertEqual(code, 404)

    def test_default_job_is_escaped_in_page(self):
        srv = dashboard.make_server("127.0.0.1", 0, "</script><script>alert(1)</script>")
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", "/")
            html = c.getresponse().read().decode()
            c.close()
            self.assertNotIn("</script><script>alert(1)", html)
        finally:
            srv.shutdown()
            srv.server_close()

    # ---- ping / jobs
    def test_ping(self):
        d = self.json("/api/ping")
        self.assertEqual(d["orchestrate_dashboard"], 1)
        self.assertEqual(d["jobs_dir"], os.path.abspath(str(Path(TMP) / "jobs")))
        # the code this server runs, so orchestrate.py can replace a stale dashboard it did not start
        import router
        self.assertIn("dashboard.py", router.CODE_FILES)
        want = router.code_fingerprint()
        self.assertEqual(d["fingerprint"], want)
        self.assertIn(f'const FINGERPRINT = "{want}"', self.get("/")[2].decode())

    def test_jobs_list_skips_broken(self):
        jobs = {j["job"]: j for j in self.json("/api/jobs")}
        self.assertIn(JOB, jobs)
        self.assertIn("old-job", jobs)
        self.assertNotIn("broken", jobs)
        self.assertEqual(jobs[JOB]["done"], 1)
        self.assertEqual(jobs[JOB]["status"], "running")  # its pid (this process) is alive

    # ---- payload
    def test_payload_fields(self):
        d = self.json(f"/api/job/{JOB}")
        for k in ("ui_rounds", "calls", "screenshots_root", "dashboard", "plan_raw", "tasks", "extra", "review",
                  "metrics", "logs", "definition_of_done", "shots_by_task", "manager"):
            self.assertIn(k, d)
        self.assertEqual(d["screenshots_root"], self.state["screenshots_root"])
        self.assertEqual(d["dashboard"], self.state["dashboard"])
        self.assertEqual(json.loads(d["plan_raw"])["job"], JOB)
        self.assertEqual(len(d["calls"]), 2)
        self.assertEqual([t["id"] for t in d["tasks"]], ["api-share", "web-share"])  # "not-a-task" dropped
        self.assertEqual([t["id"] for t in d["extra"]], ["old-task"])
        self.assertEqual({l["name"] for l in d["logs"]}, {"api-share", "_integration", "_devserver"})
        self.assertTrue(d["has_out"])
        self.assertTrue(d["has_report"])

    def test_payload_maps_ui_shots_to_tasks(self):
        d = self.json(f"/api/job/{JOB}")
        self.assertEqual(len(d["ui_rounds"]), 1)
        r = d["ui_rounds"][0]
        self.assertEqual(r["round"], 1)
        self.assertEqual(len(r["shots"]), 4)  # the non-dict entry is dropped
        self.assertEqual(sorted(d["shots_by_task"]["web-share"]), sorted([
            "round-1/share-view--desktop", "round-1/share-view--mobile", "round-1/share-view--desktop--after-click"]))
        self.assertNotIn("api-share", d["shots_by_task"])
        mobile = next(s for s in r["shots"] if s["id"] == "round-1/share-view--mobile")
        self.assertEqual(mobile["page_errors"], ["TypeError: y"])
        self.assertEqual(r["verdict"]["pages"][0]["ok"], False)
        web = next(t for t in d["tasks"] if t["id"] == "web-share")
        self.assertEqual(web["ui"][0]["name"], "share-view")

    def test_unknown_job_404(self):
        for p in (f"/api/job/nope", "/api/job/nope/log/_integration", "/api/job/nope/out",
                  "/api/job/nope/report", "/api/job/nope/shot?f=round-1/home--desktop.png", "/api/nope"):
            code, _, _ = self.get(p)
            self.assertEqual(code, 404, p)
        code, _, _ = self.get("/api/job/..%2F..%2Fetc")
        self.assertEqual(code, 404)

    def test_old_partial_job_renders(self):
        d = self.json("/api/job/old-job")
        self.assertEqual(d["status"], "done")
        self.assertEqual(d["tasks"], [])
        self.assertEqual([t["id"] for t in d["extra"]], ["a"])
        self.assertEqual(d["ui_rounds"], [])
        self.assertIsNone(d["plan_raw"])

    def test_stopped_vs_orca(self):
        jd = self.root / "jobs" / "dead"
        jd.mkdir()
        (jd / "job.json").write_text(json.dumps({"status": "running", "pid": 2 ** 22 + 12345, "mode": "headless"}))
        self.assertEqual(self.json("/api/job/dead")["status"], "stopped")
        (jd / "job.json").write_text(json.dumps({"status": "running", "mode": "orca"}))  # orca drops the pid
        d = self.json("/api/job/dead")
        self.assertEqual(d["status"], "running")
        self.assertTrue(d["alive"])

    # ---- text endpoints
    def test_logs(self):
        code, ctype, body = self.get(f"/api/job/{JOB}/log/api-share")
        self.assertEqual(code, 200)
        self.assertIn("text/plain", ctype)
        self.assertTrue(body.endswith(b"END\n"))
        code, _, body = self.get(f"/api/job/{JOB}/log/api-share?n=4")
        self.assertEqual(body, b"END\n")
        code, _, body = self.get(f"/api/job/{JOB}/log/api-share?n=bogus")
        self.assertEqual(code, 200)
        self.assertIn(b"starting", body)
        for name, want in (("_integration", b"merged api-share\n"), ("_devserver", b"VITE ready on 5173\n")):
            code, _, body = self.get(f"/api/job/{JOB}/log/{name}")
            self.assertEqual((code, body), (200, want))
        code, _, body = self.get(f"/api/job/{JOB}/log/missing")
        self.assertEqual((code, body), (200, b""))
        code, _, _ = self.get(f"/api/job/{JOB}/log/..%2Fjob")
        self.assertEqual(code, 404)

    def test_brief_out_report(self):
        self.assertEqual(self.get(f"/api/job/{JOB}/brief/api-share")[2], b"# Brief\nDo the thing.\n")
        self.assertEqual(self.get(f"/api/job/{JOB}/out")[2], b"detached run output\nline 2\n")
        self.assertEqual(self.get(f"/api/job/{JOB}/out?n=7")[2], b"line 2\n")
        self.assertEqual(self.get(f"/api/job/{JOB}/report")[2], b"# Report\nAll good.\n")

    # ---- screenshots
    def test_shot_served(self):
        code, ctype, body = self.get(f"/api/job/{JOB}/shot?f=round-1/share-view--mobile.png")
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        code, _, _ = self.get(f"/api/job/{JOB}/shot?f=round-1%2Fhome--desktop.png")
        self.assertEqual(code, 200)

    def test_shot_rejects_traversal_symlinks_and_non_png(self):
        bad = ["../../job.json", "round-1/../../../job.json", "%2Fetc%2Fpasswd", "/etc/passwd",
               "round-1/escape.png",            # symlink to a png outside the root
               "round-1/passwd.png",            # symlink to /etc/passwd
               "round-1/notes.txt", "round-1/missing.png", "", "round-1", "..%2F..%2F..%2Foutside.png",
               "round-1/home--desktop.png%00.txt", str(self.root / "outside.png")]
        for f in bad:
            code, ctype, body = self.get(f"/api/job/{JOB}/shot?f={f}")
            self.assertEqual(code, 404, f)
            self.assertNotIn(b"\x89PNG", body, f)
        code, _, _ = self.get(f"/api/job/{JOB}/shot")
        self.assertEqual(code, 404)

    def test_shot_needs_screenshots_root(self):
        code, _, _ = self.get("/api/job/old-job/shot?f=round-1/home--desktop.png")
        self.assertEqual(code, 404)

    # ---- finding context
    def test_file_only_for_findings(self):
        code, _, body = self.get(f"/api/job/{JOB}/file?path=apps/web/share.tsx")
        self.assertEqual(code, 200)
        self.assertIn(b"export const x", body)
        for p in (".env", "../job.json", "/etc/passwd", ".git/config", "apps/web/../../.env"):
            code, _, _ = self.get(f"/api/job/{JOB}/file?path={p}")
            self.assertEqual(code, 404, p)

    # ---- dns rebinding
    def test_foreign_host_refused(self):
        code, _, _ = self.get("/api/ping", host="evil.example:8765")
        self.assertEqual(code, 403)
        code, _, _ = self.get("/api/ping", host="localhost:8765")
        self.assertEqual(code, 200)

    def raw_get(self, path, hosts, port=None):
        c = HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        c.putrequest("GET", path, skip_host=True)
        for hv in hosts:
            c.putheader("Host", hv)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_malformed_and_duplicate_hosts_refused(self):
        for bad in ("[::1].evil.example", f"127.0.0.1:{self.port}@evil.example", "localhost.", "evil.example",
                    "127.0.0.1.evil.example", "localhost:", "localhost:99999", "0.0.0.0:8765", "[::2]:80"):
            self.assertEqual(self.raw_get("/api/ping", [bad])[0], 403, bad)
        self.assertEqual(self.raw_get("/api/ping", ["127.0.0.1", "evil.example"])[0], 403)
        for ok in ("127.0.0.1", f"127.0.0.1:{self.port}", "LOCALHOST:1", "[::1]:8765", "[::1]"):
            self.assertEqual(self.raw_get("/api/ping", [ok])[0], 200, ok)

    def test_forbidden_host_names_the_option(self):
        code, body = self.raw_get("/api/ping", ["machine.tailnet.ts.net:8765"])
        self.assertEqual(code, 403)
        self.assertIn("--allow-host", json.loads(body)["hint"])

    def test_allow_host_for_a_reverse_proxy(self):
        """tailscale serve / an SSH or nginx proxy keeps the browser's Host: it can be allowed by name."""
        def serve(**kw):
            srv = dashboard.make_server("127.0.0.1", 0, JOB, **kw)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return srv
        srv = serve(allow_hosts=["machine.tailnet.ts.net"])
        try:
            p = srv.server_address[1]
            self.assertEqual(self.raw_get("/api/ping", ["machine.tailnet.ts.net:8765"], p)[0], 200)
            self.assertEqual(self.raw_get("/", ["Machine.Tailnet.ts.net"], p)[0], 200)
            self.assertEqual(self.raw_get("/api/ping", ["other.tailnet.ts.net"], p)[0], 403)
            self.assertEqual(self.raw_get("/api/ping", ["evil.example"], p)[0], 403)
        finally:
            srv.shutdown()
            srv.server_close()
        os.environ["ORCHESTRATE_DASHBOARD_HOSTS"] = " .tailnet.ts.net , proxy.lan:8080"
        try:
            srv = serve()  # what a dashboard spawned by orchestrate.py inherits
            p = srv.server_address[1]
            self.assertEqual(self.raw_get("/api/ping", ["a.tailnet.ts.net:443"], p)[0], 200)
            self.assertEqual(self.raw_get("/api/ping", ["proxy.lan"], p)[0], 200)
            self.assertEqual(self.raw_get("/api/ping", ["tailnet.ts.net.evil.example"], p)[0], 403)
            srv.shutdown()
            srv.server_close()
        finally:
            del os.environ["ORCHESTRATE_DASHBOARD_HOSTS"]

    # ---- odd data never takes a view (or the job list) down
    def test_payload_normalises_hand_edited_plan_fields(self):
        d = self.json("/api/job/strdone")
        db, health = d["tasks"]
        self.assertEqual(db["done"], ["DB-DONE-STRING"])
        self.assertEqual(db["owns"], ["apps/db/**"])
        self.assertEqual(db["verify"], ["make db"])
        self.assertEqual(health["depends_on"], ["db"])
        self.assertEqual(health["notes"], ["a note"])
        self.assertEqual(health["ui"], [])
        self.assertEqual(health["state"]["steps"], [])
        self.assertEqual(health["state"]["files"], ["a.go"])
        self.assertEqual(d["acceptance"], ["health endpoint answers"])

    def test_payload_normalises_wrong_typed_state(self):
        d = self.json("/api/job/badtypes")
        a, b = d["tasks"]
        self.assertEqual(a["depends_on"], ["b"])
        self.assertEqual(b["owns"], [])
        self.assertEqual(a["state"]["report"], {"summary": "plain text report", "open_issues": []})
        self.assertEqual(a["state"]["steps"], [])
        self.assertEqual(d["cost_usd"], 0)
        self.assertIsNone(d["created"])
        self.assertEqual(d["calls"], [])
        self.assertEqual(d["advice"], {"plan": [], "final": []})
        self.assertEqual(len(d["manager"]["log"]), 1)
        self.assertEqual(d["manager"]["log"][0]["actions"], [])
        self.assertIsNone(d["manager"]["log"][0]["t"])
        self.assertEqual(len(d["review"]), 1)
        v = d["review"][0]["verdict"]
        self.assertEqual((v["findings"], v["acceptance"]), ([], []))
        self.assertEqual(d["ui_rounds"][0]["shots"], [])
        self.assertIsNone(d["ui_rounds"][0]["verdict"])

    def test_jobs_list_survives_a_non_numeric_cost(self):
        jobs = {j["job"]: j for j in self.json("/api/jobs")}
        self.assertIn(JOB, jobs)
        self.assertEqual(jobs["badtypes"]["cost_usd"], 0)

    def test_handler_error_answers_json_500(self):
        orig = dashboard.job_payload
        dashboard.job_payload = lambda jid: 1 / 0
        try:
            code, ctype, body = self.get(f"/api/job/{JOB}")
        finally:
            dashboard.job_payload = orig
        self.assertEqual(code, 500)
        self.assertIn("application/json", ctype)
        self.assertIn("ZeroDivisionError", json.loads(body)["error"])
        self.assertEqual(self.get("/api/ping")[0], 200)  # the server is still up

    # ---- polling cost
    def test_job_etag_and_304(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", f"/api/job/{JOB}")
        r = c.getresponse()
        r.read()
        etag = r.getheader("ETag")
        self.assertTrue(etag and etag.startswith('"'))
        c.request("GET", f"/api/job/{JOB}", headers={"If-None-Match": etag})
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (304, b""))
        c.request("GET", f"/api/job/{JOB}", headers={"If-None-Match": '"stale"'})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        r.read()
        c.close()

    def test_report_is_not_resent_on_every_poll(self):
        d = self.json(f"/api/job/{JOB}")
        self.assertNotIn("report", d)
        self.assertTrue(d["has_report"])
        self.assertEqual(self.get(f"/api/job/{JOB}/report")[2], b"# Report\nAll good.\n")

    def test_state_endpoint_serves_job_json(self):
        code, ctype, body = self.get(f"/api/job/{JOB}/state")
        self.assertEqual(code, 200)
        self.assertIn("text/plain", ctype)
        self.assertEqual(json.loads(body)["job"], JOB)

    # ---- logs: the page can say what it is not showing
    def test_log_tail_starts_at_a_line_and_reports_its_window(self):
        code, _, body = self.get("/api/job/strdone/log/db?n=1000")
        size = int(self.last_headers["X-Log-Size"])
        start = int(self.last_headers["X-Log-Start"])
        self.assertEqual(size, (self.root / "jobs/strdone/logs/db.log").stat().st_size)
        self.assertEqual(start + len(body), size)
        self.assertTrue(body.startswith(b"line "), body[:40])  # not mid-line
        self.assertTrue(body.endswith(b"LAST-LINE\n"))
        code, _, body = self.get("/api/job/strdone/log/db?n=all")
        self.assertEqual((len(body), int(self.last_headers["X-Log-Start"])), (size, 0))
        self.assertTrue(body.startswith(b"line 000000 "))
        code, _, body = self.get(f"/api/job/{JOB}/log/api-share")
        self.assertEqual(self.last_headers["X-Log-Start"], "0")

    # ---- the link handed out with the plan shows the plan
    def test_planned_job_shows_its_plan_before_the_run(self):
        d = self.json("/api/job/planned-job")
        self.assertEqual(d["status"], "planned")
        self.assertTrue(d["plan_only"])
        self.assertEqual([t["id"] for t in d["tasks"]], ["only"])
        self.assertEqual(d["definition_of_done"], "users can X")
        self.assertEqual(json.loads(d["plan_raw"])["job"], "planned-job")
        self.assertEqual(self.get("/api/job/planned-job/log/x")[0], 404)  # no job dir yet

    def test_copyable_commands_run_as_pasted(self):
        cli = self.json(f"/api/job/{JOB}")["cli"]
        self.assertIn(sys.executable, cli)
        self.assertIn(str(SCRIPTS / "orchestrate.py"), cli)
        self.assertTrue(cli.startswith(f"ORCHESTRATE_DIR={TMP} "))

    # ---- checks that did not run, answers given during the job, the planner's own choices
    def test_skipped_and_overridden_checks_are_in_the_payload(self):
        import orchestrate
        d = self.json("/api/job/overridden")
        self.assertTrue(d["baseline_skipped"])
        sk = d["skipped_checks"]
        self.assertIn("advisor plan check: approved by user", sk)
        self.assertTrue(any(x.startswith("advisor final check: overridden by the user") for x in sk), sk)
        self.assertIn("job baseline: not run (--skip-baseline)", sk)
        self.assertIn("gate db-share-tokens: approved by the user", sk)
        jd = self.root / "jobs" / "overridden"  # one source with report.md's list
        self.assertEqual(sk, orchestrate.skipped_checks(json.loads((jd / "job.json").read_text()),
                                                        json.loads((jd / "plan.json").read_text())))
        self.assertEqual(self.json(f"/api/job/{JOB}")["skipped_checks"], [])

    def test_merge_verify_is_in_the_payload(self):
        tasks = {t["id"]: t["state"] for t in self.json("/api/job/overridden")["tasks"]}
        self.assertEqual(tasks["db-share-tokens"]["merge_verify"],
                         {"cmds": ["make db-test"], "ok": True, "files": ["db/schema.sql"]})
        self.assertEqual(tasks["api"]["merge_verify"]["cmd"], "make api-test")
        self.assertFalse(tasks["api"]["merge_verify"]["ok"])

    def test_answers_and_assumptions_are_in_the_payload(self):
        d = self.json("/api/job/overridden")
        self.assertEqual(d["decisions_added"], ["tokens expire after 7 days (decided by the user during job overridden)"])
        d = self.json("/api/job/assumed-job")
        self.assertEqual(d["status"], "planned")
        self.assertEqual(d["assumptions"], ["ASSUMED: links live in sqlite, not redis"])

    # ---- odd state must not take the job view down
    def test_a_call_without_a_role_does_not_break_the_job_view(self):
        d = self.json("/api/job/nullrole")
        self.assertEqual(set(d["metrics"]["cost_by_role"]), {"None", "subagent"})
        self.assertTrue(self.last_headers.get("ETag"))

    def test_etag_survives_mixed_key_types(self):
        orig = dashboard.job_payload
        dashboard.job_payload = lambda jid: {"job": jid, "now": 1, "m": {None: 1, "a": 2}}
        try:
            code, _, body = self.get(f"/api/job/{JOB}")
        finally:
            dashboard.job_payload = orig
        self.assertEqual(code, 200, body[:300])
        self.assertTrue(self.last_headers.get("ETag"))

    def test_deeply_nested_json_is_unreadable_not_a_crash(self):
        self.assertEqual(dashboard._read_json(self.root / "jobs" / "deep" / "job.json", "dflt"), "dflt")
        self.assertEqual(self.get("/api/job/deep")[0], 404)
        self.assertIn("xjob", {j["job"] for j in self.json("/api/jobs")})

    # ---- symlinks
    def test_text_endpoints_do_not_follow_symlinks_out_of_the_state_dir(self):
        for p in ("/api/job/symjob/log/evil", "/api/job/symjob/log/evil?n=all", "/api/job/symjob/brief/evil",
                  "/api/job/symjob/report", "/api/job/symjob/out", "/api/job/symjob/out?n=all"):
            code, _, body = self.get(p)
            self.assertNotIn(b"TOP-SECRET", body, p)

    def test_file_refuses_a_symlink_into_git(self):
        code, _, body = self.get("/api/job/symjob/file?path=apps/ok.txt")
        self.assertEqual((code, body), (200, b"plain file\n"))
        code, _, body = self.get("/api/job/symjob/file?path=apps/gitcfg")
        self.assertEqual(code, 404)
        self.assertNotIn(b"TOKEN", body)


try:
    import ui_review  # noqa: E402  (its stdlib CDP driver runs the page in a real browser)
    CHROME = ui_review.find_chrome()
except Exception:  # noqa: BLE001
    CHROME = None


@unittest.skipUnless(CHROME, "no Chrome or Chromium: the page's JavaScript is not exercised")
class PageInBrowserTest(unittest.TestCase):
    """Runs the page itself in headless Chrome against the fixture jobs: every view renders, nothing throws."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(TMP)
        cls.state = fixtures()
        cls.srv = dashboard.make_server("127.0.0.1", 0, JOB)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.chrome = ui_review.Chrome(CHROME).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tab = ui_review.Tab(self.chrome.cdp, "desktop")

    def tearDown(self):
        self.tab.close()

    def js(self, expr):
        return self.tab.eval(expr)

    def until(self, expr, timeout=12.0, what=None):
        end = time.monotonic() + timeout
        while True:
            try:
                v = self.js(expr)
            except Exception:  # noqa: BLE001
                v = None
            if v:
                return v
            if time.monotonic() > end:
                self.fail(f"timed out waiting for {what or expr}; page errors {self.tab.page_errors}")
            self.tab.wait(0.1)

    def open_page(self, query, hash_=""):
        self.tab.navigate(f"{self.base}/{query}{hash_}")
        self.until("typeof D !== 'undefined' && D !== null && document.querySelectorAll('.pane.on').length === 1", what="job loaded")

    def text(self):
        return self.js("document.body.innerText")

    def test_tab_overflow_lists_every_open_tab(self):
        self.open_page("?job=xjob")
        self.assertTrue(self.js("$('#tabs-more').hidden"), "no overflow button while the tabs fit")
        n = self.js("""(() => { for(const t of allTasks()) openTab('task', t.id);
          for(const f of ['plan','state','report','out','log:_integration']) openTab('file', f);
          openTab('metrics', ''); openTab('shots', ''); openTab('advisor', 'plan'); openTab('advisor', 'final');
          document.querySelector('.work').style.width = '420px'; tabsOverflow(); return ORDER.length; })()""")
        self.until("!$('#tabs-more').hidden", what="the overflow button shows")
        self.js("$('#tabs-more').click(); true")
        self.until("!$('#palette').hidden && $$('#palette .it').length > 0", what="the open-tabs list")
        self.assertEqual(self.js("$$('#palette .it').length"), n)
        self.js("$$('#palette .it')[0].click(); true")  # the first open tab: Overview
        self.until("$('#palette').hidden && ACTIVE === 'overview:'", what="the chosen tab activated")
        self.assert_clean()

    def test_page_reloads_itself_when_the_server_code_changes(self):
        self.open_page("?job=xjob", "#overview")
        self.js("window.__BEFORE = 1; true")
        self.tab.wait(6)  # several code checks against an unchanged server: no reload
        self.assertEqual(self.js("window.__BEFORE"), 1)
        old = dashboard.FINGERPRINT
        dashboard.FINGERPRINT = "0123456789abcdef"  # as if the server restarted on other code
        try:
            self.until("typeof window.__BEFORE === 'undefined' && FINGERPRINT === '0123456789abcdef'",
                       timeout=15, what="the page reloaded on the new fingerprint")
            self.until("location.search === '?job=xjob' && location.hash === '#overview'", what="URL kept")
        finally:
            dashboard.FINGERPRINT = old

    def assert_clean(self):
        self.tab.wait(0.3)
        errs = list(self.tab.page_errors) + [c for c in self.tab.console if not c.startswith("[network")]
        self.assertEqual(errs, [], "the page logged errors")
        self.assertEqual(self.js("RENDER_ERRS.size"), 0, self.js("JSON.stringify([...RENDER_ERRS])"))
        self.assertFalse(self.js("NETERR"))

    def test_every_view_renders(self):
        self.open_page("?job=xjob")
        self.js("""(() => {
          for(const v of ['explorer','shots','review','manager','metrics','plan']){ VIEW = v; renderSide(); }
          for(const p of ['problems','terminal','activity','output']) showPanel(p);
          const tabs = [['task','api-share'],['task','web-share'],['review','0'],['review','1'],['advisor','plan'],['advisor','final'],
            ['finding','0:0'],['file','plan'],['file','state'],['file','report'],['file','out'],['file','log:_integration'],['file','brief:api-share'],
            ['metrics',''],['shots',''],['overview','']];
          for(const [k, a] of tabs) openTab(k, a);
          for(const s of SUBS) openTab('task', 'api-share', {sub: s[0]});
          openShot('round-1/share-view--mobile', '1'); LB.cmp = true; renderLB(); closeLB();
          return true; })()""")
        self.tab.wait(1.0)
        self.assert_clean()

    def test_string_done_renders_the_task_overview(self):
        self.open_page("?job=strdone", "#task/health")
        self.until("ACTIVE === 'task:health'")
        txt = self.until("document.querySelector('.pane.on .subpane.on') && document.querySelector('.pane.on .subpane.on').innerText")
        self.assertIn("HEALTH-DONE-STRING", txt)
        self.assertIn("1 criterion", txt)
        self.js("openTab('task', 'db')")
        self.assertIn("DB-DONE-STRING", self.until("document.querySelector('.pane.on .subpane.on').innerText"))
        self.assert_clean()

    def test_wrong_types_render_and_are_not_a_lost_connection(self):
        self.open_page("?job=badtypes")
        self.until("!!document.querySelector('.tile')")
        self.js("for(const v of ['explorer','shots','review','manager','metrics','plan']){ VIEW = v; renderSide(); }"
                "for(const p of ['problems','activity']) showPanel(p); openTab('task','a'); openTab('metrics',''); true")
        self.assertNotIn("Lost contact", self.text())
        self.assert_clean()

    def test_a_view_that_throws_is_reported_as_a_render_error(self):
        self.open_page(f"?job={JOB}")
        self.js("KINDS.metrics.update = () => { throw new Error('boom-render'); }; openTab('metrics', ''); renderBanners(); true")
        txt = self.text()
        self.assertIn("Could not render the tab view", txt)
        self.assertIn("boom-render", txt)
        self.assertNotIn("Lost contact", txt)
        self.assertFalse(self.js("NETERR"))

    def test_malformed_hash_does_not_kill_the_page(self):
        for h in ("#100%", "#task/50%off", "#file/log%3A_integration", "#shot/1/round-1%2Fshare-view--mobile"):
            self.open_page(f"?job={JOB}", h)
            self.until("!!document.querySelector('.tile') || LB.open", what=h)
            self.assertEqual(self.tab.page_errors, [], h)
        self.assertTrue(self.js("LB.open"))

    def test_back_and_forward_move_between_tabs_and_shots(self):
        self.open_page("?job=xjob")
        self.js("document.querySelector('.tcard[data-arg=\"api-share\"]').click(); true")
        self.until("location.hash === '#task/api-share'")
        self.js("openShot('round-1/share-view--mobile', '1'); true")
        self.until("location.hash.startsWith('#shot/1/')")
        self.js("history.back(); true")
        self.until("!LB.open && ACTIVE === 'task:api-share'", what="back closes the lightbox")
        self.js("history.back(); true")
        self.until("ACTIVE === 'overview:'", what="back to the overview")
        self.js("history.forward(); true")
        self.until("ACTIVE === 'task:api-share'", what="forward")
        self.js("location.hash = '#review/0'; true")
        self.until("ACTIVE === 'review:0'", what="an edited hash")
        self.assert_clean()

    def test_lightbox_zoom_grows_past_the_stage_and_scrolls_from_the_left(self):
        self.open_page("?job=xjob")
        self.js("openShot('round-1/wide--desktop', '1'); true")
        self.until("!!document.querySelector('#lightbox .lb-stage img') && document.querySelector('#lightbox .lb-stage img').complete")
        for _ in range(4):
            self.js("lbZoom(1); true")
            self.tab.wait(0.15)
        r = self.js("""(() => { const st = document.querySelector('#lightbox .lb-stage'), img = st.querySelector('img'); st.scrollLeft = 0;
          return {want: img.naturalWidth * LB.zoom, got: img.getBoundingClientRect().width, left: img.getBoundingClientRect().left,
                  stLeft: st.getBoundingClientRect().left, stW: st.clientWidth, sw: st.scrollWidth}; })()""")
        self.assertGreater(r["want"], r["stW"])
        self.assertAlmostEqual(r["got"], r["want"], delta=2)
        self.assertGreaterEqual(r["left"], r["stLeft"])  # the left edge is reachable
        self.assertGreater(r["sw"], r["stW"])

    def test_advisor_checks_are_shown_in_full(self):
        self.open_page("?job=xjob")
        self.js("VIEW = 'review'; renderSide(); document.querySelector('#side-body .row[data-kind=\"advisor\"][data-arg=\"plan\"]').click(); true")
        self.until("ACTIVE === 'advisor:plan'")
        txt = self.text()
        for want in ("PLAN-RESTATED-DOD", "A user can share a link", "Add it to web-share owns", "apps/web/share.css:3", "fable/medium"):
            self.assertIn(want, txt)
        self.js("openTab('advisor', 'final'); true")
        txt = self.until("ACTIVE === 'advisor:final' && document.body.innerText")
        self.assertIn("FINAL-RESTATED-DOD", txt)
        self.assertIn("re-asked at high", txt)
        self.assert_clean()

    def test_acceptance_says_why_the_latest_round_has_none(self):
        self.open_page("?job=xjob")
        txt = self.until("document.querySelector('.pane.on .grid2') && document.querySelector('.pane.on .grid2').innerText")
        self.assertIn("1/2 met · round 1 · round 2: final verify failed", txt)
        self.assertNotIn("not reviewed yet", txt)

    def test_undated_review_rounds_sort_where_they_happened(self):
        self.open_page("?job=xjob")
        order = self.js("events().map(e => e.h)")
        r2 = next(i for i, x in enumerate(order) if x.startswith("Review round 2"))
        self.assertLess(order.index("manager · task failed · web-share"), r2)  # not above newer events
        self.assertLess(r2, order.index("api-share done"))

    def test_log_tail_is_labelled_and_the_whole_file_is_reachable(self):
        self.open_page("?job=strdone", "#file/log:db")
        self.until("ACTIVE === 'file:log:db'")
        note = self.until("(() => { const n = document.querySelector('.pane.on .lognote'); return n && !n.hidden && n.innerText; })()")
        self.assertIn("Showing the last", note)
        self.assertTrue(self.js("document.querySelector('.pane.on pre.code').classList.contains('nonum')"))
        self.assertIn("n=all", self.js("document.querySelector('.pane.on .lognote a.raw').href"))
        self.assertTrue(self.js("document.querySelector('.pane.on pre.code').innerText.startsWith('line ')"))
        self.js("document.querySelector('.pane.on .lognote [data-k=more]').click(); true")
        self.until("document.querySelector('.pane.on .lognote').hidden", what="whole log loaded")
        self.assertTrue(self.js("document.querySelector('.pane.on pre.code').innerText.startsWith('line 000000')"))

    def test_open_details_and_focus_survive_a_live_update(self):
        self.open_page("?job=xjob")
        self.js("VIEW = 'manager'; renderSide(); true")
        self.until("!!document.querySelector('#side-body details[data-key]')")
        self.js("const d = document.querySelector('#side-body details[data-key]'); d.open = true; window.KEY = d.dataset.key;"
                "document.querySelector('#side-body .ev.click[data-arg=\"web-share\"]').focus(); true")
        jf = self.root / "jobs" / "xjob" / "job.json"
        st = json.loads(jf.read_text())
        before = self.js("document.querySelector('#side-body')._h")
        st["manager"]["log"].append({"t": time.time(), "event": "task_done", "task": "api-share", "note": "new", "actions": []})
        jf.write_text(json.dumps(st))
        self.until(f"document.querySelector('#side-body')._h !== {json.dumps(before)}", what="the poll re-rendered the side bar")
        self.assertTrue(self.js("document.querySelector(`#side-body details[data-key=\"${CSS.escape(KEY)}\"]`).open"))
        self.assertEqual(self.js("document.activeElement.dataset.arg"), "web-share")

    def test_switching_jobs_resets_gallery_filters(self):
        self.open_page("?job=xjob")
        self.js("SF.round = '1'; SF.task = 'web-share'; switchJob('strdone'); true")
        self.until("D && D.job === 'strdone'")
        self.assertEqual(self.js("[SF.round, SF.task]"), ["all", "all"])
        self.js("SF.round = '9'; true")
        self.assertEqual(self.js("filteredShots().length >= 0 && SF.round"), "all")  # a stale value is dropped

    def test_copy_commands_use_the_absolute_cli(self):
        self.open_page("?job=xjob")
        cmd = self.until("(() => { const b = document.querySelector('#banners [data-act=copy]'); return b && b.dataset.text; })()")
        self.assertTrue(cmd.startswith(f"ORCHESTRATE_DIR={TMP} {sys.executable} "), cmd)
        self.assertIn("resume xjob --approve web-share", cmd)

    def test_calls_table_is_capped(self):
        self.open_page("?job=xjob", "#metrics")
        self.until("ACTIVE === 'metrics:'")
        self.assertLessEqual(self.js("document.querySelectorAll('.pane.on tbody tr').length"), 300 + 10)
        self.js("document.querySelector('.pane.on [data-act=calls-all]').click(); true")
        self.until("document.querySelectorAll('.pane.on tbody tr').length > 400")

    def test_keyboard_reaches_rows_and_tabs(self):
        self.open_page("?job=xjob")
        self.js("document.querySelector('#side-body .row[data-kind=\"overview\"]').focus(); true")
        self.tab.key("ArrowDown")
        self.assertEqual(self.js("document.activeElement.dataset.act"), "toggle")
        self.js("document.querySelector('.tlrow[data-arg=\"api-share\"]').focus(); true")
        self.tab.key("Enter")
        self.until("ACTIVE === 'task:api-share'")
        self.assertGreater(self.js("document.querySelectorAll('.tab[tabindex=\"0\"]').length"), 1)

    def test_problems_count_the_latest_round(self):
        self.open_page("?job=xjob")
        self.js("showPanel('problems'); true")
        txt = self.until("document.querySelector('#panel-body .ppane.on') && document.querySelector('#panel-body .ppane.on').innerText")
        self.assertIn("Review round 2", txt)
        self.assertIn("no findings", txt)
        self.assertIn("earlier round", txt)
        self.assertEqual(self.js("document.querySelectorAll('#panel-body .ppane.on .prob').length"), 0)  # collapsed
        self.assertEqual(self.js("document.querySelector('#panel-tabs [data-panel=problems] .n')"), None)

    def test_planned_job_page_shows_the_plan(self):
        self.open_page("?job=planned-job")
        txt = self.until("document.querySelector('.tcard') && document.body.innerText")
        self.assertIn("Plan ready", txt)
        self.assertIn("only", txt)
        self.assert_clean()

    def test_skipped_and_overridden_checks_are_shown(self):
        self.open_page("?job=overridden")
        txt = self.until("document.querySelector('.tile') && document.body.innerText")
        self.assertIn("checks skipped or overridden", txt.lower())
        self.assertIn("job baseline: not run (--skip-baseline)", txt)
        banner = self.js("$('#banners').innerText")
        self.assertIn("Done.", banner)
        self.assertIn("Not checked:", banner)
        self.assertIn("advisor plan check: approved by user", banner)
        self.js("openTab('advisor', 'plan'); true")
        txt = self.until("ACTIVE === 'advisor:plan' && document.querySelector('.pane.on').innerText")
        self.assertIn("Overridden by you", txt)
        self.assertIn("--approve advisor:plan", txt)
        self.assertIn("PLAN-SAYS-REVISE", txt)  # the advice it overrode is still there
        self.js("openTab('advisor', 'final'); true")
        txt = self.until("ACTIVE === 'advisor:final' && document.querySelector('.pane.on').innerText")
        self.assertIn("Overridden by you", txt)
        self.assertNotIn("No final check yet", txt)
        self.assert_clean()

    def test_answers_during_the_job_and_planner_choices_are_marked(self):
        self.open_page("?job=overridden")
        txt = self.until("document.querySelector('.grid2') && document.querySelector('.grid2').innerText")
        self.assertIn("your answer during the job", txt)
        self.assertIn("1 answer added during the job", txt)
        self.open_page("?job=assumed-job")
        txt = self.until("document.querySelector('.tile') && document.body.innerText")
        self.assertIn("unconfirmed (the planner chose for you)", txt.lower())
        self.assertIn("ASSUMED: links live in sqlite", txt)
        self.js("VIEW = 'plan'; renderSide(); true")
        self.assertIn("ASSUMED: links live in sqlite", self.js("$('#side-body').innerText"))
        self.assert_clean()

    def test_task_tab_shows_how_a_merge_conflict_resolution_was_verified(self):
        self.open_page("?job=overridden", "#task/db-share-tokens")
        txt = self.until("ACTIVE === 'task:db-share-tokens' && document.querySelector('.pane.on').innerText")
        self.assertIn("merge conflict resolved by the integrator", txt.lower())
        self.assertIn("db/schema.sql", txt)
        self.assertIn("verified with make db-test", txt)
        self.js("openTab('task', 'api'); true")
        txt = self.until("ACTIVE === 'task:api' && document.querySelector('.pane.on').innerText")
        self.assertIn("api/routes.go", txt)
        self.assertIn("failed verify: make api-test", txt)
        self.assertIn("MERGE-VERIFY-TAIL: 2 tests failed", txt)
        self.assert_clean()

    def test_overview_advisor_card_shows_the_latest_final_check(self):
        self.open_page("?job=xjob")
        txt = self.until("document.querySelector('.pane.on .doc') && document.querySelector('.pane.on .doc').innerText")
        self.assertIn("latest final check", txt.lower())
        self.assertIn("FINAL-RESTATED-DOD", txt)

    def test_a_server_error_is_not_a_lost_connection(self):
        orig = dashboard.job_payload
        dashboard.job_payload = lambda jid: orig(jid) if jid != "overridden" else 1 / 0
        try:
            self.tab.navigate(f"{self.base}/?job=overridden")
            txt = self.until("/could not read this job/.test($('#banners').innerText) && $('#banners').innerText",
                             what="the server error banner")
        finally:
            dashboard.job_payload = orig
        self.assertIn("ZeroDivisionError", txt)
        self.assertNotIn("Cannot reach", txt)
        self.assertFalse(self.js("NETERR"))
        self.until("D && D.job === 'overridden' && !/could not read/.test($('#banners').innerText)",
                   what="the job loads once the server can read it")

    def test_phone_shows_every_notice_that_needs_the_user(self):
        self.tab.close()
        self.tab = ui_review.Tab(self.chrome.cdp, "mobile")
        self.open_page("?job=asks")
        self.until("$$('#banners .banner').length > 0")
        self.assertEqual(self.js("$$('#banners .banner').length"), 1)
        more = self.js("$('#banners .banner-more').innerText")
        self.assertIn("2 more notices need you", more)
        fits = "(() => { const b = $('#banners').getBoundingClientRect(); return $$('#banners .banner, #banners .banner-more')" \
               ".every(e => { const r = e.getBoundingClientRect(); return r.top >= b.top - 1 && r.bottom <= b.bottom + 1; }); })()"
        self.assertTrue(self.js(fits), "the first notice and the button are fully visible")
        self.js("$('#banners .banner-more').click(); true")
        self.until("$$('#banners .banner').length === 3")
        txt = self.js("$('#banners').innerText")
        for want in ("The manager is asking you", "Waiting for your approval", "ASKS-WARNING"):
            self.assertIn(want, txt)
        self.assertLessEqual(self.js("document.documentElement.scrollWidth"), 390)
        self.assert_clean()


class IdleExitTest(unittest.TestCase):
    """A dashboard nobody uses exits by itself, but not while a job of its state dir runs."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="orch-idle-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old_jobs = dashboard.JOBS
        dashboard.JOBS = self.dir / "jobs"
        self.addCleanup(setattr, dashboard, "JOBS", self.old_jobs)

    def job(self, **state):
        d = self.dir / "jobs" / "j"
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps({"job": "j", **state}))

    def test_idle_hours_reads_the_environment(self):
        for val, want in ((None, 12.0), ("0.5", 0.5), ("0", 0.0), ("junk", 12.0), ("nan", 12.0)):
            env = {k: v for k, v in os.environ.items() if k != dashboard.IDLE_ENV}
            if val is not None:
                env[dashboard.IDLE_ENV] = val
            old = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(env)
                self.assertEqual(dashboard.idle_hours(), want, val)
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_a_running_job_keeps_it_up_then_it_exits(self):
        self.job(status="running", pid=os.getpid(), mode="headless")
        self.assertTrue(dashboard.jobs_active(60))
        srv = dashboard.make_server("127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            dashboard.watch_idle(srv, 0.4 / 3600)
            time.sleep(1.2)
            self.assertTrue(th.is_alive(), "a running job keeps the dashboard up")
            self.job(status="done", pid=os.getpid(), mode="headless")
            th.join(5)
            self.assertFalse(th.is_alive(), "idle and no running job: it exits")
        finally:
            if th.is_alive():
                srv.shutdown()
            srv.server_close()
        self.job(status="running", pid=2 ** 22 + 12345, mode="headless")  # its process is gone
        self.assertFalse(dashboard.jobs_active(60))
        self.job(status="running", mode="orca")
        self.assertTrue(dashboard.jobs_active(60))
        os.utime(self.dir / "jobs/j/job.json", (time.time() - 120, time.time() - 120))
        self.assertFalse(dashboard.jobs_active(60), "an Orca job that stopped writing state is not running")

    def test_an_unreadable_job_file_does_not_stop_the_idle_exit(self):
        d = self.dir / "jobs" / "deep"
        d.mkdir(parents=True)
        (d / "job.json").write_text("[" * 200000)
        self.assertFalse(dashboard.jobs_active(60))
        orig = dashboard.jobs_active
        dashboard.jobs_active = lambda w: 1 / 0  # whatever goes wrong reading state, the watcher lives on
        self.addCleanup(setattr, dashboard, "jobs_active", orig)
        srv = dashboard.make_server("127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            w = dashboard.watch_idle(srv, 0.4 / 3600)
            th.join(5)
            self.assertFalse(th.is_alive(), "idle with unreadable state: it still exits")
            w.join(5)  # the watcher returns from srv.shutdown() just after the server thread ends
            self.assertFalse(w.is_alive())
        finally:
            if th.is_alive():
                srv.shutdown()
            srv.server_close()

    def test_requests_keep_it_up(self):
        srv = dashboard.make_server("127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            dashboard.watch_idle(srv, 0.6 / 3600)
            end = time.time() + 1.5
            while time.time() < end:
                c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                c.request("GET", "/api/ping")
                self.assertEqual(c.getresponse().status, 200)
                c.close()
                time.sleep(0.1)
            self.assertTrue(th.is_alive())
            th.join(5)
            self.assertFalse(th.is_alive())
        finally:
            if th.is_alive():
                srv.shutdown()
            srv.server_close()

    def test_a_spawned_dashboard_exits_when_idle(self):
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port = so.getsockname()[1]
        env = {**os.environ, "ORCHESTRATE_DIR": str(self.dir), dashboard.IDLE_ENV: str(1 / 3600)}
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "dashboard.py"), "--port", str(port)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("an idle dashboard kept running")
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("no request and no running job", out)


if __name__ == "__main__":
    unittest.main()
