"""What a worker, and any command that runs code a worker wrote, may write (Linux, bubblewrap).

A pre-approved `python3` or `make` can write anywhere the user can, so allow-lists of commands don't
contain a worker. The machine is read-only here. What is writable:

  the command's own worktree
  a copy of that worktree's git dir: `git status`, `diff` and `checkout -- f` work, and the harness's
      git never reads what was written there. The repo's git dir, objects included, is read-only, so
      workers can't add, commit, move refs or delete history
  /tmp and /var/tmp of its own (the host's hold sockets: tmux, agents)
  package caches and Claude Code's session dirs of its own, kept per worktree under <state>/jail
  new files in some home directories, which vanish with the command
  paths the user lists in ORCHESTRATE_SANDBOX_WRITE (separated by ':')

/run/user (session bus, systemd, agents) is empty, sockets bound in the home are hidden, and other
processes can't be seen or signalled.
Claude Code's config dir is read-only, so a jailed CLI can't refresh the login: claude_cli does that
outside while it runs.

Claude Code's own sandbox isn't used for this: by default a command may leave it
(dangerouslyDisableSandbox), and the strict form has no localhost on Linux, so tests that need a
local database fail.

This is not a VM. Still reachable: the network and every localhost service (an IDE's or an agent's
too, with whatever token is readable), the Docker socket, abstract sockets, sockets bound by a
relative path or after the jail started, and everything the user can read. A file hard-linked into
the worktree before the jail started (a package store) is the same file outside.
"""
from __future__ import annotations

import atexit
import contextlib
import functools
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The harness runs tools with a worktree as cwd: an empty or relative PATH entry would find a worker's.
if os.environ.get("PATH"):
    os.environ["PATH"] = os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if os.path.isabs(d))

# The harness's own git runs outside the jail, on trees workers wrote: never a hook, fsmonitor,
# dirty-submodule scan, replaced object or guessed directory rename from them. `-c` reaches the git
# processes git starts, too.
GIT = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
       "-c", "diff.ignoreSubmodules=dirty", "-c", "merge.directoryRenames=false"]
MODES = ("auto", "required", "off")
FLAGS = ("--die-with-parent", "--new-session", "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev",
         "--proc", "/proc")
CACHES = (".cache", ".npm", ".pnpm-store", ".local/share/pnpm/store", ".yarn/berry", ".bun/install/cache",
          "go/pkg", ".cargo/registry", ".cargo/git", ".gradle/caches", ".gradle/daemon", ".gradle/wrapper/dists",
          ".m2/repository", ".nuget/packages")
BROWSERS = (".cache/ms-playwright", ".cache/puppeteer", ".cache/Cypress")  # big, and only read
OPEN = (".config", ".local/share", ".local/state")  # tools create their own dirs in these
SESSION = ("projects", "session-env", "shell-snapshots", "todos", "tasks")


@functools.lru_cache(maxsize=None)
def available() -> str | None:
    """None when the jail works here, else why not."""
    exe = shutil.which("bwrap")
    if not exe:
        return "bubblewrap (bwrap) is not installed"
    try:
        p = subprocess.run([exe, *FLAGS, "--tmpfs", "/tmp", "--", "/bin/sh", "-c", ":"],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return f"bwrap can't start: {e}"
    if p.returncode:
        return ("bwrap can't create a sandbox here (Ubuntu 24.04+ restricts unprivileged user namespaces): "
                + p.stderr.strip()[-300:])
    return None


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, timeout=30)
    return os.fsdecode(p.stdout) if p.returncode == 0 else ""


def _box(wt: Path, state: Path) -> Path:
    return state / "jail" / hashlib.sha256(os.fsencode(str(wt))).hexdigest()[:24]


def _remove(top: Path) -> None:
    """rmtree for what a jail wrote: read-only dirs (Go's module cache), and links never followed."""
    if top.is_symlink() or not top.is_dir():
        return
    with contextlib.suppress(OSError):
        os.chmod(top, 0o700)
    for at, dirs, _ in os.walk(top):
        for d in dirs:
            p = os.path.join(at, d)
            if not os.path.islink(p):
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o700)
    shutil.rmtree(top, ignore_errors=True)


def _socket_dirs(home: Path) -> set[Path]:
    """Dirs in the home where a socket is bound: an IDE's, an agent's or a gateway's, which would run
    commands outside for whoever connects. Read-only mounts don't stop connecting."""
    found = set()
    with contextlib.suppress(OSError):
        for line in Path("/proc/net/unix").read_bytes().split(b"\n")[1:]:
            f = line.split(None, 7)
            if len(f) == 8 and f[7].startswith(b"/"):
                d = Path(os.fsdecode(f[7])).parent
                if home in d.parents and d.is_dir():
                    found.add(d.resolve())
    return found


def _keep_links(paths: list[str], scratch: Path) -> None:
    """/tmp is the jail's own, so a path that goes through a symlink there (a TMPDIR) needs the
    link made again in it."""
    for raw in paths:
        if not os.path.isabs(raw):
            continue
        for a in [*reversed(Path(raw).parents), Path(raw)]:
            at = a.parent.resolve() / a.name
            if at.parts[1:2] != ("tmp",) and at.parts[1:3] != ("var", "tmp"):
                continue
            if a.is_symlink():
                copy = scratch / at.relative_to("/")
                copy.parent.mkdir(parents=True, exist_ok=True)
                if not os.path.lexists(copy):
                    os.symlink(os.readlink(a), copy)


def forget(wt: Path, state: Path) -> None:
    """Drop a worktree's private caches and sessions, when the worktree itself goes."""
    _remove(_box(Path(wt).resolve(), Path(state).resolve()))


def mounts(wt: Path, state: Path, box: Path, scratch: Path, program: str = "") -> list[tuple[str, ...]]:
    """bwrap arguments, parents before children. For one path the decision added last wins."""
    home = Path.home().resolve()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser().resolve()
    if wt == Path("/") or wt == home or wt in home.parents:
        raise RuntimeError(f"can't confine work in {wt}: it holds the home directory. Use a project directory")
    ops: dict[Path, tuple[str, ...]] = {}

    def ro(p: Path) -> None:
        ops[p] = ("--ro-bind-try", str(p), str(p))

    def empty(p: Path) -> None:
        ops[p] = ("--tmpfs", str(p))

    def own(p: Path, src: Path) -> None:
        src.mkdir(parents=True, exist_ok=True)
        ops[p] = ("--bind", str(src), str(p))

    # the home: empty and disposable, with what exists mounted read-only and caches of the jail's own
    caches = {(home / c).resolve() for c in CACHES}
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg and os.path.isabs(xdg):
        caches.add(Path(xdg).resolve())
    caches = {c for c in caches if home in c.parents}
    opened = {home}
    for p in [*caches, *((home / o / "_").resolve() for o in OPEN)]:
        opened |= {a for a in p.parents if home in a.parents}
    socketed = _socket_dirs(home)
    for d in socketed:  # rebuilt with everything but the sockets
        if home in d.parents and d != wt and wt not in d.parents and not any(c == d or c in d.parents for c in caches):
            opened.add(d)
    # Claude's dir must stay one read-only mount: the login is refreshed by replacing a file in it
    opened = {d for d in opened if d != claude and claude not in d.parents}
    for d in sorted(opened):
        empty(d)
        try:
            items = list(os.scandir(d))
        except OSError:
            continue
        if d in socketed and len(items) > 300:
            continue  # left empty: bwrap takes 9000 arguments in all
        for e in items:
            p = Path(e.path)
            if p in opened or p in caches:
                continue
            with contextlib.suppress(OSError):
                if e.is_symlink():
                    ops[p] = ("--symlink", os.readlink(p), str(p))
                elif e.is_dir() or e.is_file():  # a socket left out can't be connected to
                    ro(p)
    for c in caches:
        own(c, box / "cache" / hashlib.sha256(os.fsencode(str(c))).hexdigest()[:24])

    tmps = [Path("/tmp"), Path("/var/tmp")]
    for t in tmps:
        if t.is_dir() and not t.is_symlink():
            own(t, scratch / t.relative_to("/"))
    run = Path(f"/run/user/{os.getuid()}")
    if run.is_dir():
        empty(run)

    for extra in os.environ.get("ORCHESTRATE_SANDBOX_WRITE", "").split(os.pathsep):
        if os.path.isabs(extra) and os.path.exists(extra):
            p = Path(extra).resolve()
            ops[p] = ("--bind", str(p), str(p))

    # read-only again, whatever writable or private place they happen to be in
    private = [*caches, *tmps]
    for b in BROWSERS:
        ro((home / b).resolve())
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.isabs(d) and any(t in Path(d).parents for t in private):
            ro(Path(d))
    if os.path.isabs(program):
        ro(Path(program).resolve())
    for prefix in {sys.prefix, sys.base_prefix}:  # the scope check runs on this Python, wherever it is
        if any(x in Path(prefix).resolve().parents for x in private):
            ro(Path(prefix).resolve())
    ro(Path(__file__).resolve().parent)
    ro(state)

    claude.mkdir(parents=True, exist_ok=True)
    ro(claude)
    for name in SESSION:
        (claude / name).mkdir(exist_ok=True)  # only what exists can be mounted on
        own((claude / name).resolve(), box / "claude" / name)

    ops[wt] = ("--bind", str(wt), str(wt))
    common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").rstrip("\n")
    if common:
        common_dir = Path(common).resolve()
        gitdir = Path(_git(wt, "rev-parse", "--absolute-git-dir").rstrip("\n")).resolve()
        for item in _git(wt, "worktree", "list", "--porcelain").split("\n"):
            if item.startswith("worktree ") and Path(item[9:]).resolve() != wt:
                ro(Path(item[9:]).resolve())
        ro(common_dir)
        if (common_dir / "lfs" / "tmp").is_dir():
            empty(common_dir / "lfs" / "tmp")  # git-lfs writes here on `git status`
        if gitdir != common_dir:
            shutil.copytree(gitdir, scratch / "git", symlinks=True)
            ops[gitdir] = ("--bind", str(scratch / "git"), str(gitdir))
            ro(wt / ".git")  # an edited one would point the harness's git at a repo the worker made
    return [ops[p] for p in sorted(ops, key=lambda p: (len(p.parts), str(p)))]


def mode_of(plan: dict) -> str:
    """plan.sandbox: true = required, false = off, absent = confined wherever bwrap works. Anything
    else is required: validate refuses it, and a typo must not be what turns confinement off."""
    v = (plan or {}).get("sandbox")
    return "auto" if v is None else "off" if v is False else "required"


def confinement(mode: str, state: Path) -> Path | None:
    """The state dir to confine commands against, or None to run them as they are. `required`
    raises where the jail can't work: it never runs unconfined."""
    if mode == "off":
        return None
    why = available()
    if why and mode == "required":
        raise RuntimeError(f"sandbox required, but {why}")
    return None if why else Path(state)


def shell(cmd: str, cwd: Path, state: Path | None) -> list[str]:
    """`sh -c cmd`, jailed when `state` is given."""
    argv = ["/bin/sh", "-c", cmd]
    return wrap(argv, cwd, state) if state else argv


def wrap(cmd: list[str], wt: Path, state: Path) -> list[str]:
    """`cmd` inside the jail. Call available() first."""
    given = [str(wt), str(state), cmd[0], str(Path.home()),
             *os.environ.get("ORCHESTRATE_SANDBOX_WRITE", "").split(os.pathsep)]
    wt, state = Path(wt).resolve(), Path(state).resolve()
    box = _box(wt, state)
    box.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="run-", dir=box))
    atexit.register(_remove, scratch)
    _keep_links(given, scratch)
    out = [shutil.which("bwrap") or "bwrap", *FLAGS]
    for op in mounts(wt, state, box, scratch, cmd[0]):
        out += op
    for var in ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK"):
        out += ["--unsetenv", var]
    return out + ["--setenv", "TMPDIR", "/tmp", "--chdir", str(wt), "--", *cmd]
