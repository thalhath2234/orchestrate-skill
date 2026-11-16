#!/usr/bin/env python3
"""dashboard.py: live, read-only web UI for orchestrate jobs.

  python3 dashboard.py [JOB] [--port 8765] [--host 127.0.0.1] [--open]

Serves http://127.0.0.1:8765/?job=<JOB>. It only READS job state from
$ORCHESTRATE_DIR (default ~/.local/share/orchestrate) and never touches a job,
so it is safe to run while a job is in progress, from any copy of the skill.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import router  # noqa: E402

JOBS = router.DIR / "jobs"
SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}$")
TASK_FIELDS = ("id", "title", "goal", "type", "tier", "owns", "reads", "depends_on", "done", "verify",
               "gate", "notes", "interfaces")
STATE_FIELDS = ("status", "started", "ended", "steps", "report", "cost_usd", "error", "slot", "result",
                "manager_retries", "added_by", "files", "final_message", "split_into", "skip_reason")


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _alive(pid) -> bool:
    import os
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def list_jobs() -> list[dict]:
    out = []
    for f in JOBS.glob("*/job.json"):
        s = _read_json(f, {})
        if not s:
            continue
        status = s.get("status", "?")
        if status == "running" and not _alive(s.get("pid")):
            status = "stopped"
        tasks = s.get("tasks", {})
        out.append({"job": f.parent.name, "status": status, "cost_usd": round(s.get("cost_usd", 0), 2),
                    "done": sum(t.get("status") == "done" for t in tasks.values()),
                    "total": sum(t.get("status") not in ("split",) for t in tasks.values()),
                    "mtime": f.stat().st_mtime})
    return sorted(out, key=lambda j: -j["mtime"])


def job_payload(jid: str) -> dict | None:
    jd = JOBS / jid
    s, p = _read_json(jd / "job.json"), _read_json(jd / "plan.json")
    if not s or not p:
        return None
    status = s.get("status")
    alive = _alive(s.get("pid"))
    if status == "running" and not alive:
        status = "stopped"
    tasks, seen = [], set()
    for t in p.get("tasks", []):
        st = s.get("tasks", {}).get(t["id"], {})
        seen.add(t["id"])
        tasks.append({**{k: t.get(k) for k in TASK_FIELDS},
                      "state": {k: st.get(k) for k in STATE_FIELDS}})
    extra = [{"id": tid, "title": st.get("title"), "type": st.get("type"), "tier": st.get("tier"),
              "state": {k: st.get(k) for k in STATE_FIELDS}}
             for tid, st in s.get("tasks", {}).items() if tid not in seen]
    m = s.get("manager") or {}
    try:
        proposals = len(router.pending())
    except Exception:  # noqa: BLE001
        proposals = 0
    return {
        "job": jid, "status": status, "alive": alive, "now": time.time(),
        "goal": p.get("goal"), "acceptance": p.get("acceptance", []), "decisions": p.get("decisions", []),
        "non_goals": p.get("non_goals", []), "verify": p.get("verify", []),
        "repo": s.get("repo"), "branch": s.get("branch"), "base": s.get("base"),
        "base_sha": (s.get("base_sha") or "")[:10], "created": s.get("created"), "finished": s.get("finished"),
        "cost_usd": s.get("cost_usd", 0), "budget_usd": p.get("budget_usd"), "capacity": s.get("capacity"),
        "message": s.get("message"), "human_question": s.get("human_question"),
        "approved": s.get("approved", []), "warnings": s.get("warnings", []),
        "manager": {"enabled": (p.get("manager") or {}).get("enabled", True),
                    "interventions": m.get("interventions", 0), "log": m.get("log", [])},
        "review": (s.get("review") or {}).get("rounds", []),
        "metrics": _metrics(s),
        "tasks": tasks, "extra": extra, "pending_proposals": proposals,
        "report": (jd / "report.md").read_text() if (jd / "report.md").exists() else None,
    }


def _metrics(state: dict) -> dict:
    try:
        import orchestrate
        return orchestrate.job_metrics(state)
    except Exception:  # noqa: BLE001  (old or partial state: the dashboard must still render)
        return {}


def tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    default_job = None

    def log_message(self, format, *args):  # noqa: A002  (quiet)
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        parts = [x for x in u.path.split("/") if x]
        q = parse_qs(u.query)
        if not parts:
            return self._send(200, PAGE.replace("__DEFAULT_JOB__", json.dumps(self.default_job)), "text/html")
        if parts == ["api", "jobs"]:
            return self._send(200, list_jobs())
        if len(parts) >= 3 and parts[:2] == ["api", "job"] and SAFE.match(parts[2]):
            jid = parts[2]
            if len(parts) == 3:
                data = job_payload(jid)
                return self._send(200, data) if data else self._send(404, {"error": f"no job {jid}"})
            if len(parts) == 5 and parts[3] in ("log", "brief") and SAFE.match(parts[4]):
                n = min(int((q.get("n") or ["20000"])[0]), 200000)
                f = JOBS / jid / ("logs" if parts[3] == "log" else "briefs") / f"{parts[4]}.{'log' if parts[3] == 'log' else 'md'}"
                return self._send(200, tail(f, n), "text/plain")
        return self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>orchestrate</title>
<style>
:root{--bg:#0a0c11;--panel:#10141c;--panel2:#141a24;--line:#1e2633;--text:#e6ebf3;--mute:#8b96a8;--dim:#5b6577;
--pending:#5b6577;--running:#4f8cff;--done:#2fbf71;--failed:#ff5d6c;--blocked:#ff9f43;--awaiting_approval:#f5c542;
--skipped:#7c8799;--split:#a97bff;--accent:#7aa2ff;--r:12px}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Inter,Roboto,sans-serif}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
a{color:var(--accent)}
.wrap{max-width:1500px;margin:0 auto;padding:22px 26px 60px}
header{display:flex;gap:18px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.goal{color:var(--mute);max-width:900px}
.meta{color:var(--dim);font-size:12.5px;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;
text-transform:uppercase;letter-spacing:.04em;border:1px solid currentColor;background:color-mix(in srgb,currentColor 12%,transparent)}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.live .dot{animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,currentColor 60%,transparent)}70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
.banner{margin-top:16px;border-radius:var(--r);padding:12px 16px;border:1px solid;display:flex;gap:12px;align-items:flex-start}
.banner.warn{border-color:#5a4a16;background:#1d1708;color:#f5d77a}.banner.err{border-color:#5c1e26;background:#1d0b0e;color:#ffb3bb}
.banner.ok{border-color:#174a31;background:#08180f;color:#9be3bd}.banner .cmd{margin-top:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px}
.tile .k{color:var(--dim);font-size:11.5px;text-transform:uppercase;letter-spacing:.06em}
.tile .v{font-size:22px;font-weight:650;margin-top:4px;letter-spacing:-.01em}
.tile .s{color:var(--mute);font-size:12px;margin-top:2px}
.bar{height:6px;background:var(--line);border-radius:6px;overflow:hidden;margin-top:9px;display:flex}
.bar>i{display:block;height:100%}
section{margin-top:22px}
h2{font-size:13px;color:var(--mute);text-transform:uppercase;letter-spacing:.08em;margin:0 0 10px;font-weight:600}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r)}
.dag{position:relative;overflow:auto;padding:18px}
.cols{display:flex;gap:34px;position:relative;z-index:1;width:max-content}
.col{display:flex;flex-direction:column;gap:12px;width:212px}
.colh{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
svg.edges{position:absolute;left:0;top:0;pointer-events:none;z-index:0}
.card{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--c);border-radius:10px;padding:10px 12px;cursor:pointer;transition:transform .12s,border-color .12s}
.card:hover{transform:translateY(-1px);border-color:#2b3546;border-left-color:var(--c)}
.card.sel{outline:2px solid var(--accent);outline-offset:1px}
.card.running{box-shadow:0 0 0 1px color-mix(in srgb,var(--running) 40%,transparent),0 0 24px -8px var(--running)}
.ct{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.cid{font-weight:600;font-size:13px;word-break:break-word}
.ctitle{color:var(--mute);font-size:12px;margin-top:2px}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.b{font-size:11px;padding:1px 7px;border-radius:6px;background:#1a2130;color:#aab4c5;border:1px solid #232c3b}
.b.tier{color:#d6ddff;background:#1a2040;border-color:#283160}
.b.gate{color:#f5c542;border-color:#5a4a16;background:#1d1708}
.b.mgr{color:#c9b1ff;border-color:#3d2d66;background:#1b1430}
.st{font-size:11px;font-weight:650;color:var(--c);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;display:flex;align-items:center;gap:5px}
.st .dot{width:7px;height:7px}
.cfoot{display:flex;justify-content:space-between;color:var(--dim);font-size:11.5px;margin-top:8px}
.grid2{display:grid;grid-template-columns:1.25fr 1fr;gap:18px}
@media(max-width:1100px){.grid2{grid-template-columns:1fr}}
.tl{padding:14px 16px}
.tlrow{display:grid;grid-template-columns:210px 1fr 70px;gap:10px;align-items:center;height:24px;font-size:12px}
.tlname{color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tltrack{position:relative;height:10px;background:#0c1017;border-radius:5px}
.tlbar{position:absolute;top:0;height:10px;border-radius:5px;min-width:3px;background:var(--c)}
.tlbar.running{background:repeating-linear-gradient(45deg,var(--running),var(--running) 6px,#3a6fd8 6px,#3a6fd8 12px);animation:slide 1s linear infinite;background-size:17px 17px}
@keyframes slide{to{background-position:17px 0}}
.tld{color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}
.feed{max-height:520px;overflow:auto;padding:6px 0}
.ev{display:grid;grid-template-columns:62px 1fr;gap:10px;padding:9px 16px;border-bottom:1px solid #161c27}
.ev:last-child{border-bottom:0}.ev .t{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.ev .h{font-weight:600;font-size:13px}.ev .n{color:var(--mute);font-size:12.5px;margin-top:2px}
.ev .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;padding:16px}
.kv ul{margin:6px 0 0;padding-left:18px;color:var(--mute)}.kv li{margin:3px 0}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(640px,96vw);background:#0d1118;border-left:1px solid var(--line);
transform:translateX(102%);transition:transform .2s ease;z-index:20;display:flex;flex-direction:column;box-shadow:-30px 0 60px -30px #000}
.drawer.open{transform:none}
.dh{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px}
.db{padding:16px 20px;overflow:auto;flex:1}
.db h3{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin:18px 0 6px}
.db h3:first-child{margin-top:0}
.x{background:none;border:1px solid var(--line);color:var(--mute);border-radius:8px;width:32px;height:32px;cursor:pointer;font-size:16px}
pre{background:#070a0f;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;max-height:340px;white-space:pre-wrap;word-break:break-word;margin:0}
.steps{display:flex;flex-direction:column;gap:6px}
.step{display:flex;gap:8px;align-items:center;font-size:12.5px}
.chip{font-family:ui-monospace,monospace;font-size:11.5px;padding:2px 8px;border-radius:6px;background:#1a2130;border:1px solid #273042}
.chip.ok{color:#9be3bd;border-color:#1d4a32}.chip.bad{color:#ffb3bb;border-color:#5c1e26}
.tabs{display:flex;gap:6px;margin-bottom:8px}.tab{background:none;border:1px solid var(--line);color:var(--mute);padding:4px 10px;border-radius:7px;cursor:pointer;font:inherit;font-size:12px}
.tab.on{color:var(--text);border-color:var(--accent)}
.muted{color:var(--mute)}.dim{color:var(--dim)}
.empty{color:var(--dim);padding:16px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:12px;margin:-2px 0 10px}
.legend span{display:inline-flex;align-items:center;gap:5px}.legend i{width:9px;height:9px;border-radius:3px;display:inline-block}
.copy{cursor:pointer;border-bottom:1px dashed currentColor}
</style></head><body>
<div class="wrap">
  <header>
    <div>
      <h1><span id="jobname">orchestrate</span> <span id="pill"></span></h1>
      <div class="goal" id="goal"></div>
      <div class="meta" id="meta"></div>
    </div>
    <div><select id="jobsel" title="Switch job"></select></div>
  </header>
  <div id="banners"></div>
  <div class="tiles" id="tiles"></div>
  <section><h2>Task graph</h2>
    <div class="legend" id="legend"></div>
    <div class="panel dag" id="dag"><svg class="edges" id="edges"></svg><div class="cols" id="cols"></div></div>
  </section>
  <div class="grid2">
    <section><h2>Timeline</h2><div class="panel tl" id="timeline"></div></section>
    <section><h2>Activity</h2><div class="panel feed" id="feed"></div></section>
  </div>
  <section><h2>Agreed with you</h2><div class="panel kv" id="agreed"></div></section>
</div>
<aside class="drawer" id="drawer"><div class="dh"><div id="dtitle"></div><button class="x" onclick="closeDrawer()">✕</button></div><div class="db" id="dbody"></div></aside>
<script>
const DEFAULT_JOB = __DEFAULT_JOB__;
const COLORS = {pending:'--pending',running:'--running',done:'--done',failed:'--failed',blocked:'--blocked',
  awaiting_approval:'--awaiting_approval',skipped:'--skipped',split:'--split',stopped:'--failed',awaiting_human:'--awaiting_approval',
  environment:'--failed',review_failed:'--failed',budget:'--blocked',advisor_blocked:'--awaiting_approval',interrupted:'--blocked'};
const LABEL = {awaiting_approval:'needs approval',awaiting_human:'needs you',review_failed:'review failed',advisor_blocked:'advisor: revise plan'};
let JOB = new URLSearchParams(location.search).get('job') || DEFAULT_JOB;
let D = null, SEL = null, TAB = 'overview', sig = '';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const col = st => `var(${COLORS[st] || '--pending'})`;
const money = v => '$' + (v || 0).toFixed(2);
function dur(s){ if(s==null||s<0) return '–'; s=Math.round(s); const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60;
  return h? `${h}h ${String(m).padStart(2,'0')}m` : m? `${m}m ${String(x).padStart(2,'0')}s` : `${x}s`; }
const clock = t => t ? new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
const nowS = () => D ? D.now + (Date.now()/1000 - D._fetched) : Date.now()/1000;
function pill(st, live){ return `<span class="pill ${live?'live':''}" style="color:${col(st)}"><span class="dot"></span>${esc(LABEL[st]||st)}</span>`; }

async function loadJobs(){
  const jobs = await (await fetch('/api/jobs')).json();
  if(!JOB && jobs.length) JOB = jobs[0].job;
  $('#jobsel').innerHTML = jobs.map(j => `<option value="${esc(j.job)}" ${j.job===JOB?'selected':''}>${esc(j.job)} · ${esc(LABEL[j.status]||j.status)} · ${j.done}/${j.total}</option>`).join('');
}
$('#jobsel').onchange = e => { JOB = e.target.value; history.replaceState(null,'','?job='+encodeURIComponent(JOB)); SEL=null; closeDrawer(); sig=''; load(); };

async function load(){
  if(!JOB){ await loadJobs(); if(!JOB){ $('#goal').textContent='No jobs yet.'; return; } }
  try{
    const r = await fetch('/api/job/' + encodeURIComponent(JOB));
    if(!r.ok){ $('#goal').textContent = 'Job not found: ' + JOB; return; }
    D = await r.json(); D._fetched = Date.now()/1000;
    const s = JSON.stringify([D.status, D.cost_usd, D.tasks.map(t=>[t.id,t.state.status,t.state.ended,(t.state.steps||[]).length]), D.manager.interventions, D.review.length, D.human_question, D.extra.length]);
    if(s !== sig){ sig = s; render(); } else tick();
    if(SEL) renderDrawer(false);
  }catch(e){ console.error(e); }
}

function levels(){
  const T = Object.fromEntries(D.tasks.map(t=>[t.id,t])), memo = {};
  const lv = id => { if(id in memo) return memo[id]; memo[id]=0; const deps=(T[id].depends_on||[]).filter(d=>T[d]);
    return memo[id] = deps.length ? 1 + Math.max(...deps.map(lv)) : 0; };
  D.tasks.forEach(t=>lv(t.id)); return memo;
}

function render(){
  const ts = D.tasks, by = st => ts.filter(t=>t.state.status===st).length;
  const done = by('done'), total = ts.length, running = by('running'), failed = by('failed')+by('blocked');
  const live = D.status==='running' && D.alive;
  document.title = `${done}/${total} · ${D.job}`;
  $('#jobname').textContent = D.job; $('#pill').innerHTML = pill(D.status, live);
  $('#goal').textContent = D.goal || '';
  $('#meta').innerHTML = [`<span>branch <code>${esc(D.branch)}</code></span>`, `<span>base <code>${esc(D.base)} @ ${esc(D.base_sha)}</code></span>`,
    `<span>repo <code>${esc(D.repo)}</code></span>`].join('');
  // banners
  let b = '';
  if(D.human_question) b += `<div class="banner warn"><div>🙋</div><div><b>The manager is asking you:</b> ${esc(D.human_question)}<div class="cmd mono copy" onclick="copy(this)">orchestrate.py resume ${esc(D.job)} --answer "…"</div></div></div>`;
  const waiting = ts.filter(t=>t.state.status==='awaiting_approval');
  if(waiting.length) b += `<div class="banner warn"><div>🔒</div><div><b>Waiting for your approval:</b> ${waiting.map(t=>`<code>${esc(t.id)}</code> (${esc(t.gate)})`).join(', ')}<div class="cmd mono copy" onclick="copy(this)">orchestrate.py resume ${esc(D.job)} ${waiting.map(t=>'--approve '+esc(t.id)).join(' ')}</div></div></div>`;
  if(D.status==='stopped') b += `<div class="banner err"><div>⚠️</div><div><b>The job process is gone</b> but the job never finished. Continue with <span class="mono copy" onclick="copy(this)">orchestrate.py resume ${esc(D.job)}</span></div></div>`;
  if(D.status==='done') b += `<div class="banner ok"><div>✅</div><div><b>Done.</b> Review the branch <code>${esc(D.branch)}</code> and merge it when you're happy.</div></div>`;
  else if(D.message && !D.human_question && D.status!=='running') b += `<div class="banner err"><div>⛔</div><div><pre style="background:none;border:0;padding:0;max-height:160px">${esc(D.message)}</pre></div></div>`;
  (D.warnings||[]).forEach(w => b += `<div class="banner warn"><div>⚠️</div><div>${esc(w)}</div></div>`);
  $('#banners').innerHTML = b;
  // tiles
  const cap = D.capacity || {}, el = (D.finished && D.status!=='running' ? D.finished : nowS()) - (D.created||nowS());
  const budget = D.budget_usd, cost = D.cost_usd || 0, M = D.metrics || {};
  const seg = st => `<i style="width:${100*by(st)/Math.max(total,1)}%;background:${col(st)}"></i>`;
  const models = {}; ts.forEach(t=>(t.state.steps||[]).forEach(s=>{const m=(s.config||'').split('/')[0]; if(m) models[m]=(models[m]||0)+1;}));
  $('#tiles').innerHTML = [
    tile('Progress', `${done}<span class="dim" style="font-size:15px"> / ${total}</span>`, `${running} running · ${failed} failed/blocked`, `<div class="bar">${seg('done')}${seg('running')}${seg('failed')}${seg('blocked')}${seg('awaiting_approval')}</div>`),
    tile('Elapsed', `<span id="elapsed">${dur(el)}</span>`, D.created ? 'started ' + clock(D.created) : ''),
    tile('Cost', money(cost), budget ? `of ${money(budget)} budget` : 'no budget set', budget ? `<div class="bar"><i style="width:${Math.min(100,100*cost/budget)}%;background:${cost>budget*.85?'var(--failed)':'var(--accent)'}"></i></div>` : ''),
    tile('Parallel', `${running}<span class="dim" style="font-size:15px"> / ${cap.cap ?? '–'}</span>`, cap.limited_by ? `capped by ${esc(cap.limited_by)}` + (cap.mem_available_gb!=null ? ` · ${cap.mem_available_gb} GB free` : '') : ''),
    tile('Manager', D.manager.enabled ? String(D.manager.interventions) : 'off', D.manager.enabled ? `interventions${M.auto_triaged ? ` · ${M.auto_triaged} settled by rule` : ''}` : 'manager disabled'),
    tile('Cost split', M.cost_by_role ? Object.entries(M.cost_by_role).slice(0,3).map(([k,v])=>`<span style="font-size:15px">${esc(k)} <span class="dim">${money(v)}</span></span>`).join('<br>') : '–',
         [M.manager_share!=null ? `manager ${Math.round(100*M.manager_share)}% of spend` : '', M.cache_read_share!=null ? `cache reads ${Math.round(100*M.cache_read_share)}%` : ''].filter(Boolean).join(' · ')),
    tile('Models used', Object.keys(models).length ? Object.entries(models).map(([m,n])=>`<span style="font-size:15px">${esc(m)}<span class="dim">×${n}</span></span>`).join(' ') : '–', D.pending_proposals ? `${D.pending_proposals} routing proposal(s) pending` : 'routing: no proposals'),
  ].join('');
  // legend
  $('#legend').innerHTML = ['pending','running','done','failed','blocked','awaiting_approval','skipped'].map(s=>`<span><i style="background:${col(s)}"></i>${esc(LABEL[s]||s)}</span>`).join('');
  // DAG
  const L = levels(), ncol = Math.max(0, ...Object.values(L)) + 1, cols = Array.from({length:ncol}, ()=>[]);
  ts.forEach(t => cols[L[t.id]].push(t));
  $('#cols').innerHTML = cols.map((c,i) => `<div class="col"><div class="colh">Wave ${i+1}</div>${c.map(card).join('')}</div>`).join('')
    + (D.extra.length ? `<div class="col"><div class="colh">Replaced / skipped</div>${D.extra.map(card).join('')}</div>` : '');
  requestAnimationFrame(edges);
  // timeline
  const t0 = D.created || Math.min(...ts.map(t=>t.state.started||Infinity)), t1 = Math.max(nowS(), ...ts.map(t=>t.state.ended||0)), span = Math.max(t1-t0, 1);
  const started = ts.filter(t=>t.state.started).sort((a,b)=>a.state.started-b.state.started);
  $('#timeline').innerHTML = started.length ? started.map(t => {
    const s=t.state, a=s.started, e=s.ended && s.status!=='running' ? s.ended : nowS();
    return `<div class="tlrow" onclick="openTask('${esc(t.id)}')" style="cursor:pointer"><div class="tlname" title="${esc(t.id)}">${esc(t.id)}</div>
      <div class="tltrack"><div class="tlbar ${s.status==='running'?'running':''}" data-tl="${esc(t.id)}" style="--c:${col(s.status)};left:${100*(a-t0)/span}%;width:${100*(e-a)/span}%"></div></div>
      <div class="tld" data-dur="${esc(t.id)}">${dur(e-a)}</div></div>`; }).join('') : '<div class="empty">Nothing has started yet.</div>';
  // feed
  const ev = [];
  ts.concat(D.extra).forEach(t => { const s=t.state;
    if(s.started) ev.push({t:s.started, h:`▶ ${t.id} started`, n:`${t.type||''} ${t.tier||''}${s.slot!=null?' · slot '+s.slot:''}`});
    if(s.ended && s.status!=='running') ev.push({t:s.ended, h:`${s.status==='done'?'✓':'✗'} ${t.id} ${s.status}`, n:(s.report&&s.report.summary)||s.error||'', c:s.status,
      acts:(s.steps||[]).map(x=>`<span class="chip ${x.ok?'ok':'bad'}">${esc(x.config)}${x.cause?' · '+esc(x.cause):''}</span>`).join('')}); });
  D.manager.log.forEach(m => { const acts = ((m.second_try||m).actions||[]);
    ev.push({t:m.t, h:`🧠 manager · ${m.event.replaceAll('_',' ')}${m.task?' · '+m.task:''}`, n:m.note||'', c:'split',
      acts: acts.map(a=>`<span class="b mgr">${esc(a.action)} ${esc(typeof a.task==='string'?a.task:(a.task&&a.task.id)||'')}</span>`).join('') + (m.rejected?`<span class="b gate">first proposal rejected</span>`:'')}); });
  D.review.forEach(r => { const v=r.verdict||{}; ev.push({t:null, h:`🔍 review round ${r.round}: ${v.verdict||'…'}`, n:v.summary||'', c: v.verdict==='pass'?'done':'failed',
      acts:(v.findings||[]).slice(0,6).map(f=>`<span class="b ${f.severity==='minor'?'':'gate'}">${esc(f.severity)}: ${esc((f.issue||'').slice(0,70))}</span>`).join('')}); });
  ev.sort((a,b)=>(b.t||1e12)-(a.t||1e12));
  $('#feed').innerHTML = ev.length ? ev.map(e=>`<div class="ev"><div class="t">${e.t?clock(e.t):''}</div><div><div class="h" style="${e.c?'color:'+col(e.c):''}">${esc(e.h)}</div>${e.n?`<div class="n">${esc(String(e.n).slice(0,400))}</div>`:''}${e.acts?`<div class="acts">${e.acts}</div>`:''}</div></div>`).join('') : '<div class="empty">No activity yet.</div>';
  // agreed
  const list = (title, xs) => `<div><h2 style="margin:0">${title}</h2>${xs.length?`<ul>${xs.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<div class="dim" style="margin-top:6px">–</div>'}</div>`;
  $('#agreed').innerHTML = list('Acceptance', D.acceptance) + list('Decisions', D.decisions) + list('Non-goals', D.non_goals) + list('Final verify', D.verify);
}
function tile(k,v,s,extra=''){ return `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div>${s?`<div class="s">${s}</div>`:''}${extra}</div>`; }
function card(t){
  const s = t.state||{}, st = s.status||'pending', steps = s.steps||[];
  const time = s.started ? (s.status==='running' ? `<span data-run="${esc(t.id)}">${dur(nowS()-s.started)}</span>` : dur((s.ended||nowS())-s.started)) : '';
  return `<div class="card ${st} ${SEL===t.id?'sel':''}" id="c-${esc(t.id)}" style="--c:${col(st)}" onclick="openTask('${esc(t.id)}')">
    <div class="ct"><div><div class="cid">${esc(t.id)}</div><div class="ctitle">${esc(t.title||'')}</div></div>
    <div class="st"><span class="dot ${st==='running'?'live':''}"></span>${esc(LABEL[st]||st)}</div></div>
    <div class="badges"><span class="b tier">${esc(t.tier||'')}</span><span class="b">${esc(t.type||'')}</span>
      ${t.gate?'<span class="b gate">gate</span>':''}${s.added_by==='manager'?'<span class="b mgr">added by manager</span>':''}${s.manager_retries?`<span class="b mgr">manager retry ×${s.manager_retries}</span>`:''}</div>
    <div class="cfoot"><span>${steps.map(x=>esc(x.config)).join(' → ') || (st==='running'?'working…':'')}</span><span>${time}${s.cost_usd?` · ${money(s.cost_usd)}`:''}</span></div></div>`;
}
function edges(){
  const box = $('#dag'), svg = $('#edges'), cols = $('#cols'), o = box.getBoundingClientRect();
  svg.setAttribute('width', cols.scrollWidth + 40); svg.setAttribute('height', cols.scrollHeight + 40);
  const pos = id => { const el = document.getElementById('c-'+id); if(!el) return null; const r = el.getBoundingClientRect();
    return {l:r.left-o.left+box.scrollLeft, r:r.right-o.left+box.scrollLeft, y:r.top-o.top+box.scrollTop+r.height/2}; };
  let p = '';
  D.tasks.forEach(t => (t.depends_on||[]).forEach(d => { const a=pos(d), b=pos(t.id); if(!a||!b) return;
    const st = (D.tasks.find(x=>x.id===d)||{state:{}}).state.status, x1=a.r, x2=b.l, mx=(x1+x2)/2;
    p += `<path d="M${x1},${a.y} C${mx},${a.y} ${mx},${b.y} ${x2},${b.y}" fill="none" stroke="${st==='done'?'#2fbf7166':'#3a455a'}" stroke-width="1.6"/>`; }));
  svg.innerHTML = p;
}
function tick(){
  if(!D) return; const n = nowS();
  document.querySelectorAll('[data-run]').forEach(el => { const t = D.tasks.find(x=>x.id===el.dataset.run); if(t&&t.state.started) el.textContent = dur(n - t.state.started); });
  document.querySelectorAll('[data-dur]').forEach(el => { const t = D.tasks.find(x=>x.id===el.dataset.dur); if(t&&t.state.status==='running') el.textContent = dur(n - t.state.started); });
  const e = $('#elapsed'); if(e && D.created && D.status==='running') e.textContent = dur(n - D.created);
}
setInterval(tick, 1000);
window.addEventListener('resize', () => D && edges());

function openTask(id){ SEL = id; TAB = 'overview'; document.querySelectorAll('.card.sel').forEach(c=>c.classList.remove('sel'));
  const c = document.getElementById('c-'+id); if(c) c.classList.add('sel'); $('#drawer').classList.add('open'); renderDrawer(true); }
function closeDrawer(){ SEL = null; $('#drawer').classList.remove('open'); document.querySelectorAll('.card.sel').forEach(c=>c.classList.remove('sel')); }
document.addEventListener('keydown', e => { if(e.key==='Escape') closeDrawer(); });
async function renderDrawer(full){
  const t = D.tasks.concat(D.extra).find(x=>x.id===SEL); if(!t) return;
  const s = t.state||{}, rep = s.report||{};
  $('#dtitle').innerHTML = `<div class="cid" style="font-size:16px">${esc(t.id)}</div><div class="muted">${esc(t.title||'')}</div><div style="margin-top:8px">${pill(s.status||'pending', s.status==='running')}</div>`;
  if(!full && TAB==='overview' && s.status!=='running') return;
  const tabs = ['overview','log','brief'].map(x=>`<button class="tab ${TAB===x?'on':''}" onclick="TAB='${x}';renderDrawer(true)">${x}</button>`).join('');
  let body = `<div class="tabs">${tabs}</div>`;
  if(TAB==='overview'){
    const list = xs => xs && xs.length ? `<ul style="margin:0;padding-left:18px">${xs.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : '<span class="dim">–</span>';
    body += `<h3>Goal</h3><div>${esc(t.goal||'')}</div>
      ${t.notes&&t.notes.length?`<h3>Notes from the manager</h3>${list(t.notes)}`:''}
      <h3>Owns</h3>${list((t.owns||[]).map(x=>x))}<h3>Depends on</h3>${list(t.depends_on)}
      <h3>Done looks like</h3>${list(t.done)}<h3>Verify</h3>${list(t.verify)}
      <h3>Model path</h3><div class="steps">${(s.steps||[]).map(x=>`<div class="step"><span class="chip ${x.ok?'ok':'bad'}">${esc(x.config)}</span>${x.cause?`<span class="muted">${esc(x.cause)}: ${esc(x.evidence||'')}</span>`:x.ok?'<span class="muted">passed</span>':''}${x.cost_usd?`<span class="dim">${money(x.cost_usd)}</span>`:''}</div>`).join('') || '<span class="dim">–</span>'}</div>
      ${rep.summary?`<h3>Report</h3><div>${esc(rep.summary)}</div>`:''}
      ${rep.interfaces?`<h3>Interfaces</h3><div class="muted">${esc(typeof rep.interfaces==='string'?rep.interfaces:JSON.stringify(rep.interfaces))}</div>`:''}
      ${rep.open_issues&&rep.open_issues.length?`<h3>Open issues</h3>${list(rep.open_issues)}`:''}
      ${s.error?`<h3>Error</h3><pre>${esc(s.error)}</pre>`:''}
      ${s.files&&s.files.length?`<h3>Files merged (${s.files.length})</h3>${list(s.files)}`:''}`;
  } else {
    const txt = await (await fetch(`/api/job/${encodeURIComponent(JOB)}/${TAB}/${encodeURIComponent(t.id)}?n=60000`)).text();
    body += `<pre style="max-height:calc(100vh - 230px)">${esc(txt || (TAB==='log'?'(no log yet)':'(no brief yet)'))}</pre>`;
  }
  const db = $('#dbody'), keep = db.scrollTop; db.innerHTML = body; db.scrollTop = keep;
  if(TAB==='log'){ const pre = db.querySelector('pre'); if(pre) pre.scrollTop = pre.scrollHeight; }
}
function copy(el){ navigator.clipboard && navigator.clipboard.writeText(el.textContent.trim()); el.style.opacity=.6; setTimeout(()=>el.style.opacity=1,300); }
loadJobs().then(load); setInterval(load, 2000); setInterval(loadJobs, 10000);
</script></body></html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", nargs="?", help="job to show first (default: most recent)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (keep 127.0.0.1 unless you mean it)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    a = ap.parse_args(argv)
    Handler.default_job = a.job
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}/" + (f"?job={a.job}" if a.job else "")
    print(f"orchestrate dashboard: {url}  (state: {JOBS})", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
