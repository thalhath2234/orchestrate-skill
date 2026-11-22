#!/usr/bin/env python3
"""jail.py: every escape found in the v0.8.0 audit, and what a worker still needs.

Run from the repo root: python3 tests/test_jail.py -v
The work dir is under the home directory on purpose: /tmp would hide mistakes about $HOME.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "orchestrate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import jail  # noqa: E402

WHY = jail.available()


@unittest.skipIf(WHY, f"no jail here: {WHY}")
class JailTest(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.d = Path(tempfile.mkdtemp(prefix="orch-jail-test-", dir=cache)).resolve()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.old = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL")}
        self.addCleanup(self._restore)
        home = self.home = self.d / "home"
        for p in (".claude/hooks", ".claude/skills", ".agents/skills", ".cache", ".config/git", ".ssh", ".local/bin"):
            (home / p).mkdir(parents=True)
        for p in (".claude/settings.json", ".claude/CLAUDE.md", ".bashrc", ".gitconfig", ".ssh/authorized_keys"):
            (home / p).write_text("")
        os.environ["HOME"] = str(home)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        self.repo, self.state = self.d / "dev" / "repo", home / ".local/share/orchestrate"
        self.job = self.state / "jobs" / "j"
        self.wt, self.sibling = self.job / "wt" / "a", self.job / "wt" / "b"
        self.repo.mkdir(parents=True)
        self.job.mkdir(parents=True)
        (self.job / "job.json").write_text("{}")
        self.g("init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("f\n")
        self.g("add", "-A")
        self.g("commit", "-qm", "base")
        self.g("worktree", "add", "-q", "-b", "a", str(self.wt))
        self.g("worktree", "add", "-q", "-b", "b", str(self.sibling))
        self.gitdir = Path(subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=self.wt, text=True).strip())

    def _restore(self):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def g(self, *a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=self.repo, check=True,
                       capture_output=True)

    def jailed(self, *cmd, wt=None):
        return subprocess.run(jail.wrap(list(cmd), wt or self.wt, self.state), capture_output=True, text=True, timeout=30)

    def py(self, code):
        return self.jailed(sys.executable, "-c", code).returncode == 0

    def writes(self, path):
        return self.py(f"open({str(path)!r}, 'w').write('x')")

    def unchanged_by(self, path, code=None):
        """What the machine sees at `path` after a jailed attack: a write into the jail's own copy is no escape."""
        look = lambda: (os.path.lexists(path), Path(path).read_bytes() if os.path.isfile(path) else None)
        was = look()
        self.py(code or f"import os; os.makedirs({str(Path(path).parent)!r}, exist_ok=True); open({str(path)!r}, 'w').write('x')")
        return was == look()

    def test_a_worker_keeps_what_it_needs(self):
        self.assertTrue(self.writes(self.wt / "new.txt"))
        cached = self.home / ".cache" / "tool" / "x"
        self.assertTrue(self.py(f"import os; os.makedirs({str(cached.parent)!r}); open({str(cached)!r}, 'w').write('x')"))
        self.assertTrue(self.py(f"open({str(cached)!r}).read()"), "a package cache outlives one command")
        self.assertFalse(cached.exists(), "and is not the user's cache")
        self.assertNotEqual(self.jailed("cat", str(cached), wt=self.sibling).returncode, 0, "or another task's")
        self.assertTrue(self.py("import tempfile; tempfile.NamedTemporaryFile().write(b'x')"), "temp files")
        for args in (("status", "--porcelain"), ("diff",), ("checkout", "--", "f.txt")):
            p = self.jailed("git", *args)
            self.assertEqual(p.returncode, 0, (args, p.stderr))
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True).start()
        self.assertTrue(self.py(f"import socket; socket.create_connection(('127.0.0.1', {srv.getsockname()[1]}), 3)"),
                        "a local database or dev server")

    def test_it_cannot_write_what_it_is_judged_by(self):
        h, common = self.home, self.repo / ".git"
        for p in (self.job / "job.json", self.job / "new-claim", self.state / "routing.json", self.sibling / "x",
                  self.repo / "f.txt", common / "hooks" / "pre-commit", common / "config", common / "refs/heads/evil",
                  h / ".claude/settings.json", h / ".claude/hooks/h", h / ".claude/skills/s", h / ".claude/CLAUDE.md",
                  h / ".agents/skills/s", h / ".bashrc", h / ".gitconfig", h / ".config/git/config",
                  h / ".ssh/authorized_keys", h / ".local/bin/claude",
                  self.wt / ".git", self.gitdir / "commondir", self.gitdir / "gitdir"):
            with self.subTest(str(p)):
                self.assertTrue(self.unchanged_by(p))

    def test_only_the_worktree_changes_on_the_machine(self):
        h = self.home
        version = h / ".local/share/claude/versions/9.9.9"
        version.parent.mkdir(parents=True)
        version.write_text("the binary the harness runs")
        cases = {"the claude binary": version,
                 "a git earlier on PATH": h / "go/bin/git",
                 "a .pth in the user site": h / ".local/lib/python3.99/site-packages/evil.pth",
                 "a config that didn't exist": h / ".npmrc",
                 "claude settings that didn't exist": h / ".claude/settings.local.json",
                 "a service": h / ".config/systemd/user/evil.service",
                 "autostart": h / ".config/autostart/evil.desktop",
                 "somewhere else in the home": self.d / "dev" / "other-project.txt"}
        for what, p in cases.items():
            with self.subTest(what):
                self.assertTrue(self.unchanged_by(p))

    def test_it_cannot_delete_the_repos_objects(self):
        self.py(f"import shutil; shutil.rmtree({str(self.repo / '.git/objects')!r}, ignore_errors=True)")
        self.g("cat-file", "-p", "main:f.txt")

    def test_what_it_writes_in_its_gitdir_never_reaches_the_harness(self):
        head = self.gitdir / "HEAD"
        self.assertTrue(self.unchanged_by(head, f"open({str(head)!r}, 'w').write('ref: refs/heads/main\\n')"))
        log, victim = self.gitdir / "logs" / "HEAD", self.home / "victim"
        self.py(f"import os; os.makedirs({str(log.parent)!r}, exist_ok=True); "
                f"os.path.lexists({str(log)!r}) and os.unlink({str(log)!r}); os.symlink({str(victim)!r}, {str(log)!r})")
        self.assertFalse(log.is_symlink())

    def test_it_cannot_reach_the_users_session_or_processes(self):
        run = Path(f"/run/user/{os.getuid()}")
        if run.is_dir() and os.listdir(run):
            self.assertFalse(self.py(f"import os, sys; sys.exit(0 if os.listdir({str(run)!r}) else 1)"), "session bus, agents")
        fd, name = tempfile.mkstemp(prefix="orch-jail-host-", dir="/tmp")
        os.close(fd)
        self.addCleanup(os.unlink, name)
        self.assertFalse(self.py(f"open({name!r}).read()"), "sockets in the host's /tmp")
        other = subprocess.Popen(["sleep", "20"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.py(f"import os; os.kill({other.pid}, 9)")
        time.sleep(0.3)
        self.assertIsNone(other.poll(), "a process outside the jail")

    def test_it_cannot_drive_the_users_agents_through_their_sockets(self):
        for rel in (".agent.sock", ".config/ide/daemon/d.sock", ".gateway/g.sock"):
            with self.subTest(rel):
                p = self.home / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                (p.parent / "beside").write_text("still here")
                srv = socket.socket(socket.AF_UNIX)
                self.addCleanup(srv.close)
                if len(str(p)) >= 108:
                    self.skipTest("the home's path is too long for a socket")
                srv.bind(str(p))
                srv.listen(1)
                self.assertFalse(self.py(f"import os, socket; os.chdir({str(p.parent)!r}); "
                                         f"socket.socket(socket.AF_UNIX).connect({p.name!r})"))
                self.assertTrue(self.py(f"assert open({str(p.parent / 'beside')!r}).read() == 'still here'"))

    def test_it_cannot_commit_or_move_refs(self):
        for args in (("-c", "user.email=w@w", "-c", "user.name=w", "commit", "--allow-empty", "-m", "x"),
                     ("update-ref", "refs/heads/evil", "HEAD"), ("config", "x.y", "1")):
            self.assertNotEqual(self.jailed("git", *args).returncode, 0, args)

    def test_it_cannot_swap_a_protected_path_by_renaming_what_holds_it(self):
        h = self.home
        for p in (self.state, self.state.parent, self.repo, self.repo.parent, h / ".claude", h / ".config", h / ".local"):
            with self.subTest(str(p)):
                self.py(f"import os; os.rename({str(p)!r}, {str(p) + '-moved'!r})")
                self.assertTrue(p.exists())
        settings = h / ".claude/settings.json"
        self.assertTrue(self.unchanged_by(settings, f"import os; open('t','w').write('x'); os.replace('t', {str(settings)!r})"))

    def test_it_cannot_undo_the_mounts(self):
        j = self.job / "job.json"
        self.jailed("sh", "-c", f"umount '{self.state}'; echo pwn > '{j}'")
        self.jailed("sh", "-c", f"mount -o remount,rw,bind '{self.state}'; echo pwn > '{j}'")
        self.jailed("unshare", "-Urm", "sh", "-c", f"umount '{self.state}'; echo pwn > '{j}'")
        self.writes(f"/proc/{os.getpid()}/root{j}")
        self.assertEqual(j.read_text(), "{}")

    def test_in_a_main_checkout_hooks_and_config_stay_read_only(self):
        common = self.repo / ".git"
        ok = lambda p: self.jailed(sys.executable, "-c", f"open({str(p)!r}, 'w').write('x')", wt=self.repo).returncode == 0
        self.assertTrue(ok(self.repo / "new.txt"))
        self.assertFalse(ok(common / "config"))
        self.assertFalse(ok(common / "hooks" / "pre-commit"))
        self.assertFalse(ok(common / "HEAD"))
        self.assertFalse(ok(common / "objects" / "x"))
        self.assertFalse(ok(self.job / "job.json"))

    def test_it_dies_with_the_process_that_started_it(self):
        started, late = self.wt / "started", self.wt / "late"
        cmd = jail.wrap(["sh", "-c", f"touch '{started}'; sleep 3; touch '{late}'"], self.wt, self.state)
        parent = subprocess.Popen([sys.executable, "-c", "import subprocess, sys; subprocess.run(sys.argv[1:])", *cmd])
        end = time.time() + 15
        while not started.exists() and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(started.exists())
        parent.kill()
        parent.wait()
        time.sleep(3.5)
        self.assertFalse(late.exists())


if __name__ == "__main__":
    unittest.main()
