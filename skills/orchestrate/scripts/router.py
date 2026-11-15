#!/usr/bin/env python3
"""Model routing for Claude Code workers.

Learns, per task type, which (model, effort) a task should START at. Nothing in
the policy changes on its own: failures only create *proposals*; a human
approves or rejects them (`router.py proposals|approve|reject`).

State lives OUTSIDE the skill so updates/reinstalls keep it:
  $ORCHESTRATE_DIR, else $XDG_DATA_HOME/orchestrate, else ~/.local/share/orchestrate
    ledger.jsonl    append-only log, one line per finished task outcome
    routing.json    approved floors (the only thing `route` obeys)
    proposals.json  pending / decided proposals
    aliases.json    last seen alias -> concrete model id (for rule expiry)
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import fcntl
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path

DIR = Path(os.environ.get("ORCHESTRATE_DIR")
           or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "orchestrate")
ROUTING = DIR / "routing.json"
LEDGER = DIR / "ledger.jsonl"
TASK_COSTS = DIR / "task_costs.jsonl"  # per orchestrate task: subagent + coordination cost
PROPOSALS = DIR / "proposals.json"
ALIASES = DIR / "aliases.json"
LOCK = DIR / ".lock"

# Cheapest -> most capable. Fable is deliberately not on the ladder: human only.
LADDER: list[tuple[str, str]] = [
    ("haiku", "low"),
    ("haiku", "medium"),
    ("sonnet", "medium"),
    ("sonnet", "high"),
    ("opus", "high"),
    ("opus", "xhigh"),
]
TIER_DEFAULT = {"T0": 0, "T1": 2, "T2": 3, "T3": 4}
CATEGORIES = {"rename", "docs", "config", "bugfix", "feature", "refactor",
              "test", "migration", "review", "research"}
AREA_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")  # your own areas: api, web, mobile, cross...
CAUSES = {"environment", "context", "effort", "capability", "unknown"}
NON_MODEL_CAUSES = {"environment", "context", "unknown"}  # never evidence against a model

WINDOW_S = int(os.environ.get("ROUTER_WINDOW_DAYS", "30")) * 86400
MIN_FAILS = 2          # task-level failures needed before proposing a raise
FAIL_RATE = 0.5        # ...and failure rate strictly above this
PROBE_RATE = float(os.environ.get("ROUTER_PROBE_RATE", "0.10"))
PROBE_WINS = 3         # clean probe successes needed before proposing a lower floor



# ---------------------------------------------------------------- storage ---
@contextlib.contextmanager
def locked():
    DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- helpers ---
def cfg(rung: int) -> tuple[str, str]:
    return LADDER[rung]


def label(rung: int | None) -> str:
    return "-" if rung is None else "/".join(LADDER[rung])


def validate_type(task_type: str) -> str:
    parts = task_type.split(":")
    if len(parts) != 2 or parts[0] not in CATEGORIES or not AREA_RE.match(parts[1]):
        raise SystemExit(
            f"bad task type {task_type!r}: expected <category>:<area>, "
            f"category in {sorted(CATEGORIES)}, area a short lowercase slug (api, web, cross...)")
    return task_type


def next_rung(rung: int, cause: str) -> int:
    """Where to go after failing at `rung` for `cause`."""
    model = LADDER[rung][0]
    later = range(rung + 1, len(LADDER))
    if cause == "effort":  # same model, more effort; else fall through to next model
        same = [k for k in later if LADDER[k][0] == model]
        if same:
            return same[0]
    other = [k for k in later if LADDER[k][0] != model]
    if other:
        return other[0]
    return min(rung + 1, len(LADDER) - 1)


def note_alias(alias: str, model_id: str | None) -> None:
    if not model_id:
        return
    with locked():
        data = _read(ALIASES, {})
        data[alias] = model_id
        _write(ALIASES, data)


def rule_state(rule: dict, now: float | None = None) -> tuple[bool, str]:
    now = now or time.time()
    if now - rule["approved_at"] > WINDOW_S:
        return False, "expired (older than window)"
    current = _read(ALIASES, {}).get(rule.get("model_alias", ""))
    if rule.get("model_id") and current and current != rule["model_id"]:
        return False, f"expired ({rule['model_alias']} is now {current}, rule was for {rule['model_id']})"
    return True, "active"


# ----------------------------------------------------------------- route ---
def route(task_type: str, tier: str, rng: random.Random | None = None) -> dict:
    validate_type(task_type)
    rng = rng or random.Random()
    default = TIER_DEFAULT[tier]
    rule = _read(ROUTING, {}).get(task_type)
    out = {"task_type": task_type, "tier": tier, "rung": default, "source": "tier-default",
           "probe": False}
    if rule:
        active, why = rule_state(rule)
        out["rule"] = why
        if active and rule["floor"] > default:
            out.update(rung=rule["floor"], source="learned")
            if rng.random() < PROBE_RATE:  # occasionally try one rung cheaper
                out.update(rung=rule["floor"] - 1, source="probe", probe=True)
    out["model"], out["effort"] = cfg(out["rung"])
    return out


# ---------------------------------------------------------------- record ---
def record(*, task_id: str, task_type: str, tier: str, rung: int, ok: bool,
           cause: str | None, model_id: str | None, probe: bool = False,
           cost_usd: float | None = None, note: str = "") -> list[dict]:
    """Log one finished task outcome at one config. Returns any new proposals.

    Call once per (task, config) — retries at the same config are ONE data point.
    """
    validate_type(task_type)
    if cause is not None and cause not in CAUSES:
        raise ValueError(f"cause must be one of {CAUSES}")
    entry = {"t": time.time(), "task_id": task_id, "type": task_type, "tier": tier,
             "rung": rung, "model": cfg(rung)[0], "effort": cfg(rung)[1],
             "model_id": model_id, "ok": ok, "cause": None if ok else cause,
             "probe": probe, "cost_usd": cost_usd, "note": note[:300]}
    with locked():
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    note_alias(cfg(rung)[0], model_id)

    if ok:
        return _check_probe_wins(task_type, rung) if probe else []
    if cause in NON_MODEL_CAUSES:
        return []
    return _check_raise(task_type, rung, model_id)


def record_task_total(*, job: str, task_id: str, task_type: str, tier: str, status: str,
                      config: str | None, task_cost: float, coord_cost: float) -> None:
    """Cost of one orchestrate task including the manager, review and fix work it caused (P4.2).
    Kept apart from the ledger, which only drives floors."""
    entry = {"t": time.time(), "job": job, "task_id": task_id, "type": task_type, "tier": tier,
             "status": status, "config": config, "task_cost_usd": round(task_cost, 4),
             "coord_cost_usd": round(coord_cost, 4)}
    with locked():
        with open(TASK_COSTS, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


def _task_costs() -> list[dict]:
    if not TASK_COSTS.exists():
        return []
    return [json.loads(line) for line in TASK_COSTS.read_text().splitlines() if line.strip()]


def _check_raise(task_type: str, rung: int, model_id: str | None) -> list[dict]:
    now = time.time()
    rows = [e for e in _ledger()
            if e["type"] == task_type and e["rung"] == rung and now - e["t"] < WINDOW_S
            and e.get("cause") not in NON_MODEL_CAUSES
            and (not model_id or not e.get("model_id") or e["model_id"] == model_id)]
    fails = [e for e in rows if not e["ok"]]
    if len(fails) < MIN_FAILS or len(fails) / len(rows) <= FAIL_RATE:
        return []
    cause = collections.Counter(e["cause"] for e in fails).most_common(1)[0][0]
    target = next_rung(rung, cause)
    if target == rung:
        return [propose("needs_human", task_type,
                        {"rung": rung},
                        f"{len(fails)}/{len(rows)} failures at the top of the ladder "
                        f"({label(rung)}); consider Fable or splitting these tasks")]
    rule = _read(ROUTING, {}).get(task_type)
    if rule and rule_state(rule)[0] and rule["floor"] >= target:
        return []
    return [propose("raise_floor", task_type,
                    {"floor": target, "from_rung": rung, "model_alias": cfg(rung)[0],
                     "model_id": model_id, "cause": cause},
                    f"{len(fails)}/{len(rows)} tasks failed at {label(rung)} "
                    f"(mostly {cause}) in the last {WINDOW_S // 86400}d -> start at {label(target)}")]


def _check_probe_wins(task_type: str, rung: int) -> list[dict]:
    rule = _read(ROUTING, {}).get(task_type)
    if not rule or not rule_state(rule)[0] or rung >= rule["floor"]:
        return []
    probes = [e for e in _ledger() if e["type"] == task_type and e["rung"] == rung
              and e.get("probe") and e["t"] >= rule["approved_at"]]
    if any(not e["ok"] and e.get("cause") not in NON_MODEL_CAUSES for e in probes):
        return []
    wins = sum(e["ok"] for e in probes)
    if wins < PROBE_WINS:
        return []
    return [propose("lower_floor", task_type, {"floor": rung},
                    f"{wins} clean probe successes at {label(rung)} since the floor "
                    f"{label(rule['floor'])} was approved -> lower it")]


# ------------------------------------------------------------- proposals ---
def propose(kind: str, task_type: str, payload: dict, reason: str) -> dict:
    with locked():
        props = _read(PROPOSALS, [])
        key = (kind, task_type, json.dumps(payload.get("file")), payload.get("floor"))
        for p in props:
            if p["status"] == "pending" and (p["kind"], p["task_type"],
                                             json.dumps(p["payload"].get("file")),
                                             p["payload"].get("floor")) == key:
                p.update(reason=reason, updated=time.time(), seen=p.get("seen", 1) + 1)
                _write(PROPOSALS, props)
                break
        else:
            p = {"id": uuid.uuid4().hex[:8], "kind": kind, "task_type": task_type,
                 "payload": payload, "reason": reason, "status": "pending",
                 "created": time.time(), "seen": 1}
            props.append(p)
            _write(PROPOSALS, props)
    return p


def propose_context(task_type: str, file: str, text: str, evidence: str) -> dict:
    """A failure caused by missing/stale context: suggest a doc note, not a model change."""
    return propose("agents_md", task_type, {"file": file, "text": text.strip()},
                   f"missing context: {evidence[:300]}")


def _find(pid: str) -> tuple[list[dict], dict]:
    props = _read(PROPOSALS, [])
    for p in props:
        if p["id"] == pid:
            return props, p
    raise SystemExit(f"no proposal {pid}")


def approve(pid: str) -> dict:
    with locked():
        props, p = _find(pid)
        if p["status"] != "pending":
            raise SystemExit(f"proposal {pid} is already {p['status']}")
        rules = _read(ROUTING, {})
        kind, tt, pay = p["kind"], p["task_type"], p["payload"]
        if kind == "raise_floor":
            rules[tt] = {"floor": pay["floor"], "approved_at": time.time(),
                         "model_alias": pay["model_alias"], "model_id": pay.get("model_id"),
                         "reason": p["reason"], "proposal": pid}
        elif kind == "lower_floor":
            old = rules.get(tt, {})
            rules[tt] = {**old, "floor": pay["floor"], "approved_at": time.time(),
                         "reason": p["reason"], "proposal": pid}
        elif kind == "agents_md":
            path = Path(pay["file"]).expanduser()
            text = path.read_text() if path.exists() else ""
            heading = "## Agent notes (learned)"
            if heading not in text:
                text = text.rstrip() + f"\n\n{heading}\n"
            path.write_text(text.rstrip() + f"\n- {pay['text']}\n")
        elif kind == "needs_human":
            pass  # acknowledgement only
        _write(ROUTING, rules)
        p.update(status="approved", decided=time.time())
        _write(PROPOSALS, props)
    return p


def reject(pid: str, why: str = "") -> dict:
    with locked():
        props, p = _find(pid)
        p.update(status="rejected", decided=time.time(), decision_note=why)
        _write(PROPOSALS, props)
    return p


def pending() -> list[dict]:
    return [p for p in _read(PROPOSALS, []) if p["status"] == "pending"]


# ------------------------------------------------------------------- cli ---
def _status() -> None:
    rules = _read(ROUTING, {})
    print("Learned floors:")
    for tt, r in sorted(rules.items()) or [("(none)", None)]:
        print(f"  {tt:22} {label(r['floor']) if r else '':14} {rule_state(r)[1] if r else ''}")
    stats: dict = collections.defaultdict(lambda: [0, 0, 0.0])
    for e in _ledger():
        s = stats[(e["type"], label(e["rung"]))]
        s[0] += 1
        s[1] += bool(e["ok"])
        s[2] += e.get("cost_usd") or 0
    print("\nLedger (type, config): ok/total, cost")
    for (tt, lab), (n, ok, cost) in sorted(stats.items()):
        print(f"  {tt:22} {lab:14} {ok}/{n}  ${cost:.2f}")
    per: dict = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for e in _task_costs():
        if e.get("status") == "done":
            s = per[(e["type"], e.get("config") or "-")]
            s[0] += 1
            s[1] += e.get("task_cost_usd") or 0
            s[2] += e.get("coord_cost_usd") or 0
    if per:
        print("\nCost per completed orchestrate task (type, final config): n, subagent + coordination")
        for (tt, lab), (n, tc, cc) in sorted(per.items()):
            print(f"  {tt:22} {lab:14} {n}  ${tc / n:.2f} + ${cc / n:.2f} = ${(tc + cc) / n:.2f}")
    print(f"\nPending proposals: {len(pending())}   (state: {DIR})")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route", help="print the starting model/effort for a task")
    r.add_argument("--type", required=True)
    r.add_argument("--tier", required=True, choices=TIER_DEFAULT)
    sub.add_parser("status")
    sub.add_parser("proposals")
    a = sub.add_parser("approve"); a.add_argument("id")
    j = sub.add_parser("reject"); j.add_argument("id"); j.add_argument("--why", default="")
    args = ap.parse_args(argv)

    if args.cmd == "route":
        print(json.dumps(route(args.type, args.tier)))
    elif args.cmd == "status":
        _status()
    elif args.cmd == "proposals":
        items = pending()
        if not items:
            print("No pending proposals.")
        for p in items:
            print(f"[{p['id']}] {p['kind']} {p['task_type']}  (seen {p.get('seen', 1)}x)\n"
                  f"    {p['reason']}\n    payload: {json.dumps(p['payload'])}")
    elif args.cmd == "approve":
        print(json.dumps(approve(args.id), indent=2))
    elif args.cmd == "reject":
        print(json.dumps(reject(args.id, args.why), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
