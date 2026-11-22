"""What a worker, and any command that runs code a worker wrote, may write (Linux, bubblewrap).

A pre-approved `python3` or `make` can write anywhere the user can, so allow-lists don't contain a
worker. Here the machine stays as it is (caches, network, localhost services) except:

  read-only   orchestrate's state dir (job state, plans, approvals, other jobs), the repo's other
              checkouts, git refs, hooks and config, Claude Code's settings, hooks, skills, plugins
              and rules, shell start-up files, ~/.ssh, git's global config, ~/.local/bin
  writable    the command's own worktree, its git index, git objects

Claude Code's own sandbox isn't used for this: by default a command may leave it
(dangerouslyDisableSandbox), and the strict form has no localhost on Linux, so tests that need a
local database fail. The jail dies with the process that started it (--die-with-parent).

This stops accidents and injected instructions, not a determined attacker. Still writable or
reachable: the Docker socket, the user's systemd and other localhost services, ~/.claude.json (Claude
Code rewrites it), PATH dirs outside ~/.local/bin, and protected files that didn't exist at the start.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

# The harness's own git runs outside the jail, on trees workers wrote: never a hook, fsmonitor or
# dirty-submodule scan from them. `-c` reaches the git processes git starts, too.
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.ignoreSubmodules=dirty"]
MODES = ("auto", "required", "off")
HOME_READ_ONLY = (".agents", ".ssh", ".gitconfig", ".config/git", ".config/systemd", ".config/autostart",
                  ".bashrc", ".bash_profile", ".profile", ".zshrc", ".zshenv", ".zprofile", ".local/bin")
CLAUDE_READ_ONLY = ("settings.json", "settings.local.json", "hooks", "skills", "plugins", "agents", "commands",
                    "rules", "CLAUDE.md")


@functools.lru_cache(maxsize=None)
def available() -> str | None:
    """None when the jail works here, else why not."""
    exe = shutil.which("bwrap")
    if not exe:
        return "bubblewrap (bwrap) is not installed"
    try:
        p = subprocess.run([exe, "--dev-bind", "/", "/", "--die-with-parent", "--", "true"],
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


def mounts(wt: Path, state: Path) -> list[tuple[str, Path]]:
    """(bwrap flag, path) for a command that works in `wt`, parents before children."""
    wt, state, home = Path(wt).resolve(), Path(state).resolve(), Path.home().resolve()
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser().resolve()
    ro = {state, Path(__file__).resolve().parent}
    ro |= {home / p for p in HOME_READ_ONLY} | {claude / p for p in CLAUDE_READ_ONLY}
    rw, inner = {wt}, set()
    common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").rstrip("\n")
    if common:
        common_dir = Path(common).resolve()
        own = Path(_git(wt, "rev-parse", "--absolute-git-dir").rstrip("\n")).resolve()
        for item in _git(wt, "worktree", "list", "--porcelain").split("\n"):
            if item.startswith("worktree ") and Path(item[9:]).resolve() != wt:
                ro.add(Path(item[9:]).resolve())
        rw.add(common_dir / "objects")
        if own == common_dir:  # working in the main checkout: protect the parts that run code
            inner |= {own / "hooks", own / "config"}
        else:
            ro.add(common_dir)
            rw.add(own)
            # an edited one of these would point git, run later by the harness, at a repo the worker made
            inner |= {wt / ".git", own / "commondir", own / "gitdir", own / "config.worktree"}
    kinds = {p.resolve(): "--ro-bind" for p in ro if p.exists()}
    kinds.update({p: "--bind" for p in rw if p.exists()})
    kinds.update({p.resolve(): "--ro-bind" for p in inner if p.exists()})
    # A protected path's parent could be renamed away and a new one put in its place. Each ancestor
    # below / that the user owns becomes a mount point, which can't be renamed (EBUSY).
    uid = os.getuid()
    for p in list(kinds):
        for anc in p.parents:
            if anc in kinds or anc == Path("/"):
                continue
            try:
                if anc.stat().st_uid != uid:
                    continue
            except OSError:
                continue
            # inside something read-only it stays read-only; the nearest decision above it wins
            above = next((kinds[a] for a in anc.parents if a in kinds), "--bind")
            kinds[anc] = above
    return sorted(((k, p) for p, k in kinds.items()), key=lambda m: (len(m[1].parts), str(m[1])))


def mode_of(plan: dict) -> str:
    """plan.sandbox: true = required, false = off, absent = confined wherever bwrap works."""
    v = (plan or {}).get("sandbox")
    return "required" if v is True else "off" if v is False else "auto"


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
    Path(state).mkdir(parents=True, exist_ok=True)  # only what exists can be mounted read-only
    out = [shutil.which("bwrap") or "bwrap", "--dev-bind", "/", "/", "--die-with-parent"]
    for flag, p in mounts(wt, state):
        out += [flag, str(p), str(p)]
    return out + ["--chdir", str(Path(wt).resolve()), "--", *cmd]
