#!/usr/bin/env python3
"""Live smoke test: under real Claude Code, the plugin's approval hook stops an `--approve`.

The README's main safety claim (approvals go through the user) rests on Claude Code loading
hooks/hooks.json from the plugin, resolving ${CLAUDE_PLUGIN_ROOT}, and turning the hook's "ask"
into a block under `claude -p`. Unit tests stop at approval_gate.py. This test runs the real CLI
once, on Haiku (about a cent):

- A copy of the plugin, renamed orchestrate-test-<hex>, with approval_gate.py patched to leave a
  marker file when it asks. The marker proves this copy's hook ran, not an installed one.
- User settings are left out (--setting-sources project,local) and no MCP servers load, so a hook
  installed with `orchestrate.py guard-hook` or an installed orchestrate plugin can't fake a pass.
- The model runs two harmless commands that each create a file. The control must run (so the
  model really used Bash); the probe carries `orchestrate ... --approve` and must not run.

Skipped unless ORCHESTRATE_LIVE=1. Run it with `make live-smoke` before each release and after a
Claude Code update that changes hooks or -p. Never part of `make test` or CI.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_HOOK_LINE = "    if out:\n        print(json.dumps(out))\n"


@unittest.skipUnless(os.environ.get("ORCHESTRATE_LIVE") == "1",
                     "live: costs one Haiku call; run with ORCHESTRATE_LIVE=1 (make live-smoke)")
class PluginApprovalHook(unittest.TestCase):
    def setUp(self):
        self.assertTrue(shutil.which("claude"), "the `claude` CLI is not on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="orchestrate-live-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hex = uuid.uuid4().hex[:8]
        self.plugin = self.tmp / "plugin"
        shutil.copytree(ROOT, self.plugin, ignore=shutil.ignore_patterns(
            ".git", ".github", "evals", "tests", "docs", "__pycache__"))
        manifest = self.plugin / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        data["name"] = f"orchestrate-test-{self.hex}"
        manifest.write_text(json.dumps(data, indent=2))
        self.fired = self.tmp / f"hook-fired-{self.hex}"
        gate = self.plugin / "skills" / "orchestrate" / "scripts" / "approval_gate.py"
        src = gate.read_text()
        self.assertIn(GATE_HOOK_LINE, src, "approval_gate.py changed shape: update GATE_HOOK_LINE so the "
                                           "copy can leave its marker")
        gate.write_text(src.replace(GATE_HOOK_LINE, GATE_HOOK_LINE.replace(
            "if out:\n", f"if out:\n        open({str(self.fired)!r}, 'w').close()\n")))
        self.work = self.tmp / "work"
        self.work.mkdir()

    def test_approve_is_stopped_by_the_plugin_hook(self):
        ctl, probe = self.tmp / "ran-ctl", self.tmp / "ran-probe"
        ctl_cmd = f"python3 -c \"open('{ctl}', 'w')\" orchestrate status x"
        probe_cmd = f"python3 -c \"open('{probe}', 'w')\" orchestrate resume x --approve y"
        prompt = ("This is a test of a permission hook. Use the Bash tool to run these two commands exactly "
                  "as written, one at a time, in this order. Each one only creates an empty file. Run nothing "
                  "else, don't retry a command that is denied, then reply DONE.\n\n"
                  f"1. {ctl_cmd}\n2. {probe_cmd}\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCHESTRATE_", "CLAUDE_PLUGIN"))}
        env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"  # a user's advisorModel would add a paid second model
        p = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku", "--output-format", "json",
             "--plugin-dir", str(self.plugin), "--setting-sources", "project,local",
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--allowedTools", "Bash", "--no-session-persistence", "--max-budget-usd", "0.10"],
            cwd=self.work, env=env, capture_output=True, text=True, timeout=300)
        out = p.stdout.strip()
        self.assertTrue(ctl.exists(), "the control command never ran, so this run proves nothing: the model "
                                      f"didn't use Bash. Re-run.\nexit {p.returncode}\n{out[-2000:]}\n{p.stderr[-1000:]}")
        self.assertTrue(self.fired.exists(), "the copy's approval hook never asked: Claude Code didn't load "
                                             f"hooks/hooks.json or resolve ${{CLAUDE_PLUGIN_ROOT}}.\n{out[-2000:]}")
        self.assertFalse(probe.exists(), "the `--approve` command ran: the hook's \"ask\" did not stop it "
                                         f"under claude -p.\n{out[-2000:]}")
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            return  # the side effects above are the test; the JSON is a bonus check
        denials = result.get("permission_denials")
        if denials:
            cmds = [str((d.get("tool_input") or {}).get("command", "")) for d in denials]
            self.assertTrue(any("ran-probe" in c for c in cmds), f"denials don't include the probe: {cmds}")


if __name__ == "__main__":
    unittest.main()
