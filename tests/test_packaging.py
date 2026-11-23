#!/usr/bin/env python3
"""Packaging and docs-vs-code checks: offline, stdlib only, no `claude` calls.

- The plugin version is semver, and `tests/check_release.py` (make release-check) fails a
  release whose plugin files changed without a version bump.
- The `S=` locator in skills/orchestrate/SKILL.md picks the right installed copy.
- The docs agree with the code: exit codes, the manager's actions, schema enums, every
  `orchestrate.py|run_task.py|router.py|dashboard.py|ui_review.py <sub> --flag` the docs name
  (README, every SKILL.md, skills/*/references/*.md, evals/README.md), each role's default
  model and effort in the four places that list them, the skill list, and the size of the
  skill bodies pasted into every reviewer, manager and planner prompt.
  The truth comes from the code (argparse, EXIT, schemas, models.ROLES), so a new command or
  field only needs its docs.
- CI checks that Chrome is there before `make test`: the browser tests skip without it.

Run from the repo root: python3 tests/test_packaging.py -v
"""
from __future__ import annotations  # `str | None` on Python 3.9

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SCRIPTS = SKILLS / "orchestrate" / "scripts"
README = ROOT / "README.md"
MAIN_SKILL = SKILLS / "orchestrate" / "SKILL.md"
INTERNALS = SKILLS / "orchestrate" / "references" / "internals.md"
PLAN_SKILL = SKILLS / "orchestrate-plan" / "SKILL.md"
EVALS_README = ROOT / "evals" / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

sys.path.insert(0, str(SCRIPTS))
import models  # noqa: E402  (pure data, no side effects)
import schemas  # noqa: E402  (pure data, no side effects)

# Codes in orchestrate.EXIT that the outcome table (what the parent does after `wait`) leaves
# out on purpose. Each needs a reason; anything else in EXIT needs a row.
EXIT_NOT_IN_OUTCOME_TABLE = {
    "invalid": "only `validate` and `plan` return it, before any job exists; never `wait`",
    "interrupted": "only a Ctrl-C on a foreground `run`/`resume`; `resume <job>` continues",
}

# The bodies skill_text() pastes into prompts, with a ceiling about 1.25x their size when the
# check was added. Raise one only after moving what can go to references/.
BODY_CEILING = {
    "orchestrate-review": (20_400, "integrator, reviewer and UI reviewer"),
    "orchestrate-manage": (17_000, "manager"),
    "orchestrate-plan": (25_200, "planner"),
}

# How the docs name each role in models.ROLES (lowercase). The README table, the SKILL.md
# "Models." line, the orchestrate-plan schema comment and internals.md must agree with ROLES.
ROLE_NAMES = {
    "advisor": "advisor", "planner": "planner", "manager": "manager", "briefer": "briefer",
    "worker": "worker", "workers": "worker", "reviewer": "reviewer",
    "area reviewer": "area_reviewer", "area reviewers": "area_reviewer", "area_reviewer": "area_reviewer",
    "ui reviewer": "ui_reviewer", "ui_reviewer": "ui_reviewer", "cleanup": "cleanup",
    "integrator": "integrator", "integrator (conflicts)": "integrator",
    "classifier": "classifier", "failure classifier": "classifier",
    "lessons": "lessons", "lessons extractor": "lessons",
}
# Roles the short SKILL.md "Models." line leaves out on purpose: the parent never deals with
# them. README, internals.md and the plan schema comment list every role.
NOT_IN_SKILL_MODELS_LINE = {
    "area_reviewer": "runs only inside a review round; README and internals.md list it",
    "integrator": "only resolves merge conflicts inside a job",
    "classifier": "only sorts failed attempts inside run_task.py",
    "lessons": "only proposes AGENTS.md notes after a job",
}
MODEL_WORD = r"(Haiku|Sonnet|Opus|Fable)(?:\s+\d+(?:\.\d+)?)?"

# Run each script's main() up to argparse and dump its subcommands and options, plus EXIT.
INTROSPECT = r'''
import argparse, importlib.util, json, sys
path = sys.argv[1]
sys.path.insert(0, path.rsplit("/", 1)[0])
class Stop(Exception):
    pass
seen = {}
def stop(self, args=None, namespace=None):
    seen.setdefault("p", self)
    raise Stop
argparse.ArgumentParser.parse_known_args = stop
spec = importlib.util.spec_from_file_location("introspected", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    m.main([])
except Stop:
    pass
opts = lambda p: sorted(o for a in p._actions for o in a.option_strings)
p = seen["p"]
out = {"top": opts(p), "subs": {}, "exit": getattr(m, "EXIT", None)}
for a in p._actions:
    if isinstance(a, argparse._SubParsersAction):
        out["subs"] = {name: opts(sp) for name, sp in a.choices.items()}
print(json.dumps(out))
'''

_CLI = {}


def cli(script: str) -> dict:
    """{'top': [...options], 'subs': {name: [...options]}, 'exit': {...}|None} for a script."""
    if script not in _CLI:
        with tempfile.TemporaryDirectory(prefix="pack-cli-") as d:
            env = {**os.environ, "ORCHESTRATE_DIR": d, "ORCHESTRATE_DASHBOARD": "0"}
            p = subprocess.run([sys.executable, "-c", INTROSPECT, str(SCRIPTS / script)],
                               capture_output=True, text=True, env=env, cwd=d, timeout=60)
        if p.returncode != 0:
            raise AssertionError(f"could not read {script}'s argparse setup:\n{p.stderr[-2000:]}")
        _CLI[script] = json.loads(p.stdout.strip().splitlines()[-1])
    return _CLI[script]


def doc_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md")) + sorted(SKILLS.glob("*/references/*.md")) + [README, EVALS_README]


# The scripts whose commands and flags the docs may name, checked against their argparse setup.
CLI_SCRIPTS = ("orchestrate", "run_task", "router", "dashboard", "ui_review")
_SCRIPT = "|".join(CLI_SCRIPTS)


def command_problems(paths) -> list[str]:
    """Every `<script>.py <sub> --flag` in these docs that the script's argparse doesn't have."""
    scripts = {f"{s}.py": cli(f"{s}.py") for s in CLI_SCRIPTS}
    orch_subs = set(scripts["orchestrate.py"]["subs"])
    problems = []

    def check(where: str, script: str, args: str):
        info = scripts[script]
        args = re.sub(r"\"[^\"]*\"|'[^']*'", " ", args)  # quoted values may hold anything
        tokens = args.split()
        sub = None
        if info["subs"]:
            if not tokens or not re.match(r"^[a-z][a-z-]*$", tokens[0]):
                return  # the script named in prose, without a command
            sub = tokens.pop(0)
            if sub not in info["subs"]:
                problems.append(f"{where}: `{script} {sub}`: no such command (it has: {', '.join(sorted(info['subs']))})")
                return
        for i, tok in enumerate(tokens):
            if tok == "|" and i + 1 < len(tokens) and tokens[i + 1] in info["subs"]:
                sub = tokens[i + 1]  # `router.py proposals | approve <id> | ...`
                continue
            m = re.match(r"^\[?(--[a-z][a-z0-9-]*)", tok)
            if m:
                known = info["subs"][sub] if sub else info["top"]
                if m.group(1) not in known:
                    problems.append(f"{where}: `{script}{' ' + sub if sub else ''}` has no option {m.group(1)} "
                                    f"(it has: {', '.join(known)})")

    for path in paths:
        for no, text, fence in doc_lines(path):
            where = f"{rel(path)}:{no}"
            if fence:
                code = re.split(r"\s#\s", text, maxsplit=1)[0]
                for m in re.finditer(rf"\b({_SCRIPT})\.py\b(.*?)(?=$|&&|;|\b(?:{_SCRIPT})\.py\b)", code):
                    check(where, m.group(1) + ".py", m.group(2))
                continue
            for span in re.findall(r"`([^`]+)`", text):
                m = re.search(rf"\b({_SCRIPT})\.py\b(.*)$", span)
                if m:
                    check(where, m.group(1) + ".py", m.group(2))
                    continue
                first = span.split(" ", 1)
                if len(first) == 2 and first[0] in orch_subs and re.match(r"^(<|--|\S+\.json\b)", first[1]):
                    check(where, "orchestrate.py", span)  # `resume <job> --approve <id>`
    return problems


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)  # a scratch doc in a test


def doc_lines(path: Path):
    """(line number, text, in_code_fence) with backslash continuations joined."""
    lines = path.read_text().splitlines()
    fence, i = False, 0
    while i < len(lines):
        no, text = i + 1, lines[i]
        if text.lstrip().startswith("```"):
            fence = not fence
            i += 1
            continue
        while fence and text.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            text = text.rstrip()[:-1] + " " + lines[i].strip()
        yield no, text, fence
        i += 1


def table_rows(text: str, header: str) -> list[list[str]]:
    """Cells of the markdown table whose header row starts with `header`."""
    rows, inside = [], False
    for line in text.splitlines():
        if line.startswith(header):
            inside = True
            continue
        if inside:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            rows.append(cells)
    return rows


def role_of(name: str, where: str, problems: list) -> str | None:
    role = ROLE_NAMES.get(re.sub(r"\s+", " ", name.strip().strip("`").lower()))
    if role is None:
        problems.append(f"{where}: role {name.strip()!r} is not a role in models.ROLES (or add it to ROLE_NAMES)")
    return role


def compare_roles(where: str, found: dict, roles: dict, problems: list, exempt=()):
    """found: {role: (model, effort)} read from a doc."""
    for role, spec in found.items():
        if role in roles and spec != tuple(roles[role]):
            problems.append(f"{where}: says {role} is {spec[0]}/{spec[1]}; models.ROLES has "
                            f"{roles[role][0]}/{roles[role][1]}")
    for role in roles:
        if role not in found and role not in exempt:
            problems.append(f"{where}: role {role} (models.ROLES: {roles[role][0]}/{roles[role][1]}) is missing")


def split_names(text: str) -> list[str]:
    return [n for n in re.split(r",\s*|\s+and\s+", text.strip()) if n.strip()]


def readme_model_problems(text: str, roles: dict) -> list[str]:
    """The README "Which model does what" table: | Role | Model / effort | What it does |."""
    problems, found = [], {}
    rows = table_rows(text, "| Role | Model / effort |")
    if not rows:
        return ["README.md: the model table (| Role | Model / effort | ...) is gone"]
    for cells in rows:
        where = f"README.md model table row {cells[0]!r}"
        role = role_of(cells[0], where, problems)
        m = re.match(rf"{MODEL_WORD}\s*/\s*([a-z]+)\b", cells[1])
        if not m:
            problems.append(f"{where}: can't read a model / effort from {cells[1]!r}")
            continue
        if role:
            found[role] = (m.group(1).lower(), m.group(2))
            if role == "reviewer" and re.search(r"area reviewers?", " ".join(cells[2:]), re.I):
                found["area_reviewer"] = found[role]  # "(area reviewers too)"
    compare_roles("README.md model table", found, roles, problems)
    return problems


def prose_model_problems(where: str, text: str, roles: dict, exempt=()) -> list[str]:
    """Prose like `advisor Fable 5.2 at medium (...), manager and worker Opus 5.5 at medium`
    or `Advisor: Fable 5.2 at medium. Manager, workers: Opus 5.5 at medium.`"""
    problems, found = [], {}
    text = re.sub(r"\([^)]*\)", " ", text)
    for chunk in re.split(r"\.\s+", text):
        start = 0
        for m in re.finditer(rf"{MODEL_WORD}\s+at\s+([a-z]+)\b", chunk):
            names, start = chunk[start:m.start()].strip().strip(",:").strip(), m.end()
            for name in split_names(names):
                if re.search(r"\b(?:re-asked|asked|at)\b", name):
                    continue  # "re-asked at high when ..." is not a role
                role = role_of(name, where, problems)
                if role:
                    found[role] = (m.group(1).lower(), m.group(2))
    compare_roles(where, found, roles, problems, exempt)
    return problems


def plan_comment_model_problems(text: str, roles: dict) -> list[str]:
    """The orchestrate-plan schema comment: `defaults: advisor fable/medium (...), ... lessons haiku/medium`."""
    where = "skills/orchestrate-plan/SKILL.md \"models\" comment"
    m = re.search(r'"models":.*?defaults:(.*?)\(scripts/models\.py', text, re.S)
    if not m:
        return [f"{where}: the role defaults (`defaults: ... (scripts/models.py has the sources)`) are gone"]
    body = re.sub(r"//", " ", m.group(1))
    body = re.sub(r"\([^)]*\)", " ", body)
    problems, found, pending = [], {}, []
    for word in re.findall(r"[a-z_]+/[a-z]+|[a-z_]+", body):
        if "/" in word:
            model, effort = word.split("/")
            if not pending:
                problems.append(f"{where}: {word} names no role")
            for name in pending:
                role = role_of(name, where, problems)
                if role:
                    found[role] = (model, effort)
            pending = []
        elif word != "and":
            pending.append(word)
    if pending:
        problems.append(f"{where}: {pending} have no model/effort")
    compare_roles(where, found, roles, problems)
    return problems


def model_doc_problems(roles: dict, readme: str, skill: str, plan: str, internals: str) -> list[str]:
    problems = readme_model_problems(readme, roles)
    m = re.search(r"^\*\*Models\.\*\*(.*)$", skill, re.M)
    if m:
        problems += prose_model_problems("skills/orchestrate/SKILL.md \"Models.\" line", m.group(1), roles,
                                         exempt=NOT_IN_SKILL_MODELS_LINE)
    else:
        problems.append("skills/orchestrate/SKILL.md: the **Models.** line is gone")
    problems += plan_comment_model_problems(plan, roles)
    m = re.search(r"^Role models live in `scripts/models\.py`:(.*)$", internals, re.M)
    if m:
        problems += prose_model_problems("skills/orchestrate/references/internals.md role models", m.group(1), roles)
    else:
        problems.append("skills/orchestrate/references/internals.md: the `Role models live in` paragraph is gone")
    return problems


def enum(schema: dict, *path: str) -> set:
    node = schema
    for k in path:
        node = node[k]
    return set(node["enum"])


class Workflow(unittest.TestCase):
    def test_ci_fails_without_chrome(self):
        # The browser tests in test_dashboard.py and test_ui_review.py skip when no Chrome is
        # found, so a runner without one would pass `make test` without exercising them.
        text = WORKFLOW.read_text()
        check = text.find("ui_review.py chrome")
        self.assertNotEqual(check, -1, f"{rel(WORKFLOW)}: add a step that runs `ui_review.py chrome` before `make test`")
        self.assertLess(check, text.index("run: make test"), "the Chrome check must run before `make test`")
        p = subprocess.run([sys.executable, str(SCRIPTS / "ui_review.py"), "chrome"], capture_output=True, text=True,
                           env={**os.environ, "ORCHESTRATE_CHROME": "/nonexistent/chrome"}, timeout=60)
        self.assertEqual(p.returncode, 1, "`ui_review.py chrome` must fail when there is no Chrome, or the CI step checks nothing")


class PluginVersion(unittest.TestCase):
    def test_manifest_version_is_semver(self):
        v = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text()).get("version")
        self.assertRegex(str(v), SEMVER, ".claude-plugin/plugin.json needs a semver version: "
                         "Claude Code updates plugin installs only when it changes")


class ReleaseCheck(unittest.TestCase):
    """tests/check_release.py against throwaway repos."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="pack-release-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.git("init", "-q", "-b", "main")
        (self.repo / "skills" / "orchestrate").mkdir(parents=True)
        (self.repo / "skills" / "orchestrate" / "SKILL.md").write_text("v1\n")
        (self.repo / "README.md").write_text("readme\n")
        self.set_version("0.1.0")
        self.commit("init")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       cwd=self.repo, check=True, capture_output=True)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)

    def set_version(self, v):
        (self.repo / ".claude-plugin").mkdir(exist_ok=True)
        (self.repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "x", "version": v}))

    def check(self):
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "check_release.py"), "--repo", str(self.repo)],
                           capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def test_no_tag_skips(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("skip", out)

    def test_plugin_change_without_bump_fails(self):
        self.git("tag", "v0.1.0")
        (self.repo / "skills" / "orchestrate" / "SKILL.md").write_text("v2\n")
        self.commit("fix")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("skills/orchestrate/SKILL.md", out)
        self.assertIn("Bump", out)
        self.set_version("0.1.1")
        self.commit("bump")
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_change_outside_the_plugin_needs_no_bump(self):
        self.git("tag", "v0.1.0")
        (self.repo / "README.md").write_text("more\n")
        self.commit("docs")
        self.assertEqual(self.check()[0], 0)

    def test_hook_change_counts(self):
        self.git("tag", "v0.1.0")
        (self.repo / "hooks").mkdir()
        (self.repo / "hooks" / "hooks.json").write_text("{}")
        self.commit("hook")
        self.assertEqual(self.check()[0], 1)

    def test_last_tag_is_by_version_not_by_name(self):
        # Sorted by name, v0.9.0 comes after v0.10.0 and the forgotten bump would pass.
        self.git("tag", "v0.9.0")
        self.set_version("0.10.0")
        self.commit("0.10.0")
        self.git("tag", "v0.10.0")
        (self.repo / "skills" / "orchestrate" / "SKILL.md").write_text("v3\n")
        self.commit("fix")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("v0.10.0", out)

    def test_version_below_the_last_tag_fails(self):
        # Moved, but backwards: installs at the tagged version would never update.
        self.set_version("0.8.0")
        self.commit("0.8.0")
        self.git("tag", "v0.8.0")
        (self.repo / "skills" / "orchestrate" / "SKILL.md").write_text("v2\n")
        self.set_version("0.7.9")
        self.commit("fix, wrong bump")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("lower than the last release v0.8.0", out)
        self.set_version("0.10.0")  # by number, not by string: 0.10.0 > 0.8.0
        self.commit("right bump")
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_version_must_be_semver(self):
        self.set_version("0.8")
        self.commit("bad")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("semver", out)


class InstallLayout(unittest.TestCase):
    """The first bash fence in skills/orchestrate/SKILL.md sets S. Run it against fake HOMEs."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="pack-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        m = re.search(r"```bash\n(.*?)```", MAIN_SKILL.read_text(), re.S)
        self.assertIsNotNone(m, "skills/orchestrate/SKILL.md has no bash fence that sets S")
        self.snippet = m.group(1)

    def make(self, rel_path: str) -> str:
        d = self.home / rel_path
        d.mkdir(parents=True)
        return str(d)

    def cache(self, version: str) -> str:
        return self.make(f".claude/plugins/cache/orchestrate-skill/orchestrate/{version}/skills/orchestrate/scripts")

    def pick(self, snippet=None, **env) -> str:
        e = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
        p = subprocess.run(["bash", "-c", snippet or self.snippet], capture_output=True, text=True, env=e, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [l for l in p.stdout.splitlines() if l.startswith("S=")]
        self.assertTrue(lines, f"the snippet must print the copy it picked (S=...); got {p.stdout!r}")
        return lines[-1][2:]

    def test_newest_cached_version_wins(self):
        self.cache("0.6.0")
        newest = self.cache("0.7.0")
        self.assertEqual(self.pick(), newest)

    def test_versions_compare_as_numbers(self):
        self.cache("0.9.0")
        newest = self.cache("0.10.0")
        self.assertEqual(self.pick(), newest)

    def test_npx_install_alone(self):
        npx = self.make(".agents/skills/orchestrate/scripts")
        self.assertEqual(self.pick(), npx)

    def test_manual_install_beats_a_stale_cache(self):
        self.cache("0.6.0")
        manual = self.make(".claude/skills/orchestrate/scripts")
        self.assertEqual(self.pick(), manual)

    def test_running_plugin_wins_over_everything(self):
        self.cache("0.6.0")
        self.make(".agents/skills/orchestrate/scripts")
        self.make(".claude/skills/orchestrate/scripts")
        root = self.home / "plugin-root"
        running = self.make("plugin-root/skills/orchestrate/scripts")
        self.assertEqual(self.pick(CLAUDE_PLUGIN_ROOT=str(root)), running)
        # Claude Code substitutes ${CLAUDE_PLUGIN_ROOT} in a plugin's skill text before the agent
        # runs it, so the variable need not be set in the shell.
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", self.snippet)
        substituted = self.snippet.replace("${CLAUDE_PLUGIN_ROOT}", str(root))
        self.assertEqual(self.pick(substituted), running)

    def test_nothing_installed(self):
        self.assertEqual(self.pick(), "")


class DocsMatchCode(unittest.TestCase):
    """What the docs tell an agent to run or emit must exist in the code."""

    def test_every_exit_code_has_an_outcome(self):
        exit_map = cli("orchestrate.py")["exit"]
        text = MAIN_SKILL.read_text()
        rows = {int(r[0]): r[1] for r in table_rows(text, "| Code |") if r[0].isdigit()}
        self.assertTrue(rows, "skills/orchestrate/SKILL.md: outcome table (| Code | Status | ...) not found")
        problems = []
        for name, code in exit_map.items():
            if name in EXIT_NOT_IN_OUTCOME_TABLE:
                continue
            if code not in rows:
                problems.append(f"EXIT[{name!r}] = {code} has no row in the outcome table of skills/orchestrate/SKILL.md "
                                f"(add one, or add it to EXIT_NOT_IN_OUTCOME_TABLE with a reason)")
            elif rows[code] != name:
                problems.append(f"outcome table row {code} says {rows[code]!r}; orchestrate.EXIT calls it {name!r}")
        for code, status in rows.items():
            if code not in exit_map.values():
                problems.append(f"outcome table row {code} ({status}) is not a value of orchestrate.EXIT")
        for name in EXIT_NOT_IN_OUTCOME_TABLE:
            if name not in exit_map:
                problems.append(f"EXIT_NOT_IN_OUTCOME_TABLE names {name!r}, which orchestrate.EXIT no longer has")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_wait_codes_match_between_skill_and_readme(self):
        src = (SCRIPTS / "orchestrate.py").read_text()
        wait_src = src[src.index("def cmd_wait"):]
        wait_src = wait_src[:wait_src.index("\ndef ", 1)]
        # wait's own codes, outside orchestrate.EXIT: 1 (the job process is gone) and 75 (still running).
        extra = {int(c) for c in re.findall(r"return (\d+)\b", wait_src)}
        self.assertEqual(extra, {1, 75}, "cmd_wait's own exit codes changed: update this test and the docs")
        text = MAIN_SKILL.read_text()
        for c in extra:
            self.assertRegex(text, rf"wait[^\n]*\b{c}\b",
                             f"skills/orchestrate/SKILL.md: `wait` can exit {c}; say what that means on a line that names `wait`")
        table = {int(r[0]) for r in table_rows(text, "| Code |") if r[0].isdigit()}
        line = next((t for _, t, fence in doc_lines(README) if fence and re.search(r"orchestrate\.py wait\b.*#", t)), None)
        self.assertIsNotNone(line, "README.md: the `orchestrate.py wait` line with its exit-code comment is gone")
        listed = {int(c) for c in re.findall(r"(?:^|[#·])\s*(\d+)\s", line.split("#", 1)[1] + " ")}
        want = table | extra
        self.assertEqual(listed, want, f"README.md `wait` comment lists {sorted(listed)}; the outcome table of "
                                       f"skills/orchestrate/SKILL.md plus `wait`'s own codes are {sorted(want)}")

    def test_manager_action_table_matches_schema(self):
        actions = {a["properties"]["action"]["const"] for a in schemas.MANAGER["properties"]["actions"]["items"]["anyOf"]}
        path = SKILLS / "orchestrate-manage" / "SKILL.md"
        rows = table_rows(path.read_text(), "| Action |")
        documented = {re.sub(r"[`\s]", "", r[0]) for r in rows}
        self.assertTrue(documented, f"{rel(path)}: action table (| Action | Fields | ...) not found")
        self.assertEqual(documented, actions,
                         f"{rel(path)} action table vs schemas.MANAGER: missing from the table "
                         f"{sorted(actions - documented)}, not in the schema {sorted(documented - actions)}")

    def test_enum_words_in_the_docs_are_schema_values(self):
        severities = enum(schemas.FINDING, "properties", "severity")
        confidences = enum(schemas.FINDING, "properties", "confidence")
        kinds = enum(schemas.OPEN_ISSUE, "properties", "kind")
        causes = enum(schemas.CLASSIFIER, "properties", "cause")
        problems = []
        for name in ("orchestrate-review", "orchestrate-manage", "orchestrate-plan", "orchestrate"):
            path = SKILLS / name / "SKILL.md"
            lines = path.read_text().splitlines()
            for no, line in enumerate(lines, 1):
                for field, allowed in (("severity", severities), ("confidence", confidences), ("kind", kinds)):
                    for v in re.findall(rf'"{field}":\s*"([^"]+)"', line):
                        if v not in allowed:
                            problems.append(f"{rel(path)}:{no}: \"{field}\": \"{v}\" is not one of {sorted(allowed)}")
                for v in re.findall(r"`(needs_[a-z_]+)`", line):
                    if v not in kinds:
                        problems.append(f"{rel(path)}:{no}: open-issue kind `{v}` is not one of {sorted(kinds)}")
                m = re.search(r"\bkind:\s*([a-z_]+(?:\s*\|\s*[a-z_]+)+)", line)
                if m:
                    listed = {w.strip() for w in m.group(1).split("|")}
                    if listed != kinds:
                        problems.append(f"{rel(path)}:{no}: lists open-issue kinds {sorted(listed)}; the schema has {sorted(kinds)}")
                for v in re.findall(r"\b[Cc]ause `([a-z_]+)`", line):
                    if v not in causes:
                        problems.append(f"{rel(path)}:{no}: failure cause `{v}` is not one of {sorted(causes)}")
            # A run of "- **word:**" bullets that names a severity is the severity list: it must
            # name exactly the schema's severities.
            run = []
            for no, line in enumerate(lines + [""], 1):
                m = re.match(r"- \*\*([a-z_]+):\*\*", line)
                if m:
                    run.append(m.group(1))
                    continue
                if run and set(run) & severities and set(run) != severities:
                    problems.append(f"{rel(path)}:{no - 1}: severity list {run} vs schema {sorted(severities)}")
                run = []
        self.assertEqual(problems, [], "\n".join(problems))

    def test_documented_commands_and_flags_exist(self):
        problems = command_problems(doc_files())
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_doc_with_commands_is_checked(self):
        files = set(doc_files())
        for must in (README, MAIN_SKILL, INTERNALS, EVALS_README):
            self.assertIn(must, files, f"{rel(must)} names commands and flags; doc_files() must include it")

    def test_a_wrong_flag_or_command_is_caught(self):
        # The checker itself: each script, in prose and in a fence, with a bad flag or command.
        with tempfile.TemporaryDirectory(prefix="pack-doc-") as d:
            doc = Path(d) / "doc.md"
            doc.write_text("\n".join([
                "Run `orchestrate.py capacity --bogus-flag` first.",
                "```bash",
                "python3 $S/dashboard.py --port 9000 --no-such-option",
                "python3 $S/ui_review.py capture --url http://x --out d --nope",
                "python3 $S/ui_review.py screenshot --url http://x",
                "python3 $S/router.py ladder --wrong",
                "```",
                "And `run_task.py --zzz`.",
                "Fine: `dashboard.py --allow-host NAME` and `ui_review.py chrome`.",
            ]) + "\n")
            got = "\n".join(command_problems([doc]))
        for want in ("`orchestrate.py capacity` has no option --bogus-flag",
                     "`dashboard.py` has no option --no-such-option",
                     "`ui_review.py capture` has no option --nope",
                     "`ui_review.py screenshot`: no such command",
                     "`router.py ladder` has no option --wrong",
                     "`run_task.py` has no option --zzz"):
            self.assertIn(want, got)
        self.assertNotIn("doc.md:9:", got, "real options and commands must pass")
        self.assertEqual(len(got.splitlines()), 6, got)

    def model_docs(self):
        return (README.read_text(), MAIN_SKILL.read_text(), PLAN_SKILL.read_text(), INTERNALS.read_text())

    def test_documented_role_models_match_models_py(self):
        problems = model_doc_problems(models.ROLES, *self.model_docs())
        self.assertEqual(problems, [], "\n".join(problems))
        for role in NOT_IN_SKILL_MODELS_LINE:
            self.assertIn(role, models.ROLES, f"NOT_IN_SKILL_MODELS_LINE names {role!r}, which models.ROLES no longer has")

    def test_a_role_model_drift_is_caught(self):
        readme, skill, plan, internals = self.model_docs()
        # models.py changes and no doc follows.
        roles = {**models.ROLES, "ui_reviewer": ("sonnet", "low")}
        got = model_doc_problems(roles, readme, skill, plan, internals)
        self.assertEqual(len([p for p in got if "ui_reviewer is opus/medium; models.ROLES has sonnet/low" in p]), 4,
                         "\n".join(got))
        # A new role nobody documents.
        got = model_doc_problems({**models.ROLES, "judge": ("opus", "high")}, readme, skill, plan, internals)
        self.assertEqual(len([p for p in got if "role judge" in p and "is missing" in p]), 4, "\n".join(got))
        # The README loses its UI reviewer row.
        row = next(l for l in readme.splitlines() if l.startswith("| UI reviewer |"))
        got = model_doc_problems(models.ROLES, readme.replace(row + "\n", ""), skill, plan, internals)
        self.assertEqual(got, ["README.md model table: role ui_reviewer (models.ROLES: opus/medium) is missing"])
        # The SKILL.md line drops the UI reviewer, or gets an effort wrong.
        got = model_doc_problems(models.ROLES, readme, skill.replace("reviewer, UI reviewer and cleanup",
                                                                     "reviewer and cleanup"), plan, internals)
        self.assertEqual(len(got), 1, got)
        self.assertIn("SKILL.md", got[0])
        self.assertIn("ui_reviewer", got[0])
        got = model_doc_problems(models.ROLES, readme, skill, plan,
                                 internals.replace("failure classifier Haiku 5.5 at low", "failure classifier Haiku 5.5 at high"))
        self.assertEqual(len(got), 1, got)
        self.assertIn("classifier is haiku/high", got[0])
        got = model_doc_problems(models.ROLES, readme, skill, plan.replace("lessons haiku/medium", "lessons haiku/low"),
                                 internals)
        self.assertEqual(len(got), 1, got)
        self.assertIn("lessons is haiku/low", got[0])

    def test_readme_skill_lists_match_skills_dir(self):
        skills = sorted(d.name for d in SKILLS.iterdir() if (d / "SKILL.md").is_file())
        text = README.read_text()
        table = sorted(re.sub(r"[`\s]", "", r[0]) for r in table_rows(text, "| Skill |"))
        self.assertEqual(table, skills, "README.md skill table vs skills/")
        m = re.search(r"npx skills update\b([^\n]*)", text)
        self.assertIsNotNone(m, "README.md: the `npx skills update` line is gone")
        listed = sorted(w for w in m.group(1).split() if not w.startswith("-"))
        self.assertEqual(listed, skills, "README.md `npx skills update` line vs skills/: "
                                         "a skill left out there is never updated")

    def test_injected_skill_bodies_stay_small(self):
        src = (SCRIPTS / "orchestrate.py").read_text()
        injected = set(re.findall(r"""skill_text\(\s*["'](orchestrate-[a-z-]+)["']""", src))
        self.assertTrue(injected, "orchestrate.py no longer calls skill_text(): update this test")
        problems = []
        for name in sorted(injected):
            if name not in BODY_CEILING:
                problems.append(f"skills/{name}/SKILL.md is pasted into prompts by skill_text(); "
                                f"give it a ceiling in BODY_CEILING")
                continue
            ceiling, role = BODY_CEILING[name]
            size = len((SKILLS / name / "SKILL.md").read_bytes())
            if size > ceiling:
                problems.append(f"skills/{name}/SKILL.md is {size} bytes, over its {ceiling}-byte ceiling. "
                                f"{name} is injected into every {role} prompt; move detail to references/")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
