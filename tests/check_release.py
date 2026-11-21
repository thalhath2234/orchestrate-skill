#!/usr/bin/env python3
"""Release check (`make release-check`): did the plugin version move with the plugin's files?

Claude Code updates a plugin install only when `version` in .claude-plugin/plugin.json changes.
A fix pushed without a bump never reaches plugin users. This script fails when:
- plugin.json's version (at HEAD) is not semver X.Y.Z, or
- the version is lower than the last vX.Y.Z tag's (installs never move backwards), or
- skills/, hooks/ or .claude-plugin/ changed between the last vX.Y.Z tag and HEAD, and the
  version still equals that tag's.
With no vX.Y.Z tag yet, it prints a skip and passes. Not part of `make test`, so everyday work
isn't blocked; run it (`make release-check`, with `make validate-plugin`) before tagging a release.

Usage: python3 tests/check_release.py [--repo <dir>]   (exit 0 ok or skipped, 1 failed)
"""
from __future__ import annotations  # `str | None` on Python 3.9

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
WATCHED = ["skills", "hooks", ".claude-plugin"]
MANIFEST = ".claude-plugin/plugin.json"


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"release-check: git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def last_tag(repo: Path) -> str | None:
    """The highest vX.Y.Z tag reachable from HEAD (by version, not by name)."""
    tags = [t for t in git(repo, "tag", "--merged", "HEAD").split() if TAG.match(t)]
    return max(tags, key=lambda t: tuple(int(n) for n in TAG.match(t).groups()), default=None)


def check(repo: Path) -> tuple[int, str]:
    try:
        version = json.loads(git(repo, "show", f"HEAD:{MANIFEST}")).get("version")
    except json.JSONDecodeError as e:
        return 1, f"FAIL: {MANIFEST} at HEAD is not valid JSON: {e}"
    if not isinstance(version, str) or not SEMVER.match(version):
        return 1, f"FAIL: {MANIFEST} version {version!r} is not semver X.Y.Z"
    tag = last_tag(repo)
    if tag is None:
        return 0, f"skip: no vX.Y.Z tag yet (plugin.json is {version}); tag the release v{version}"
    have = tuple(int(n) for n in SEMVER.match(version).groups())
    tagged = tuple(int(n) for n in TAG.match(tag).groups())
    if have < tagged:
        return 1, (f"FAIL: {MANIFEST} is {version}, lower than the last release {tag}. "
                   f"Versions only go up: set it above {tag[1:]}.")
    changed = git(repo, "diff", "--name-only", f"{tag}..HEAD", "--", *WATCHED).split()
    if changed and have == tagged:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        return 1, (f"FAIL: {shown} changed since {tag}, but {MANIFEST} is still {version}. "
                   f"Bump the version: plugin installs only update when it changes.")
    if not changed:
        return 0, f"ok: nothing under {', '.join(WATCHED)} changed since {tag}"
    return 0, f"ok: {len(changed)} plugin file(s) changed since {tag}, version moved to {version}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    a = ap.parse_args(argv)
    code, msg = check(a.repo)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
