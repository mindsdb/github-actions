"""Merge main into the checked-out staging branch, landing hotfixes staging already holds.

A hotfix lands on staging first and is then cherry-picked onto main, so by the
time main's push fires the sync, staging usually holds the same change already.
`git merge` still conflicts when staging has since edited a neighboring line,
which is what stopped cowork's sync on 17 Nov: both sides add the same import,
and the line under it differs. Nobody can land that merge by hand on a
squash-only staging, so the train stays red until someone works around it.

So on a conflict this checks whether every commit main gained since the merge
base already has an equal patch on staging (`git cherry` marks each one `-`). If
so, it records main as merged and keeps staging's content (`merge -s ours`):
staging holds every change main brings, so nothing is lost. If main holds any
change staging lacks, or a merge commit whose content `git cherry` cannot
compare, it fails as before, and names the conflicting files and the commits
staging lacks.

Run it inside the staging checkout, with the merge identity configured. Exit 0
means HEAD is a new merge commit ready to push; exit 1 means a person must merge.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

MERGED = "merged"  # an ordinary merge, no conflict
MERGED_AS_HELD = "merged-as-held"  # a conflict on changes staging already holds; staging's content kept
NEEDS_A_PERSON = "needs-a-person"  # a conflict on a change staging lacks


@dataclass(frozen=True)
class SyncResult:
    outcome: str
    conflicts: tuple[str, ...] = ()  # files the plain merge conflicted on
    held: tuple[str, ...] = ()  # "<sha> <subject>" of main commits staging already holds
    lacking: tuple[str, ...] = ()  # "<sha> <subject>" of main commits staging lacks


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _git(runner: Runner, *args: str) -> str:
    result = runner(["git", *args])
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _subject(runner: Runner, sha: str) -> str:
    return f"{sha[:12]} {_git(runner, 'log', '-1', '--format=%s', sha).strip()}"


def sync(runner: Runner, base_ref: str, message: str) -> SyncResult:
    merge = runner(["git", "merge", "--no-ff", base_ref, "-m", message])
    if merge.returncode == 0:
        return SyncResult(MERGED)

    conflicts = tuple(_git(runner, "diff", "--name-only", "--diff-filter=U").split())
    runner(["git", "merge", "--abort"])
    if not conflicts:
        # The merge failed for some other reason (a dirty tree, a missing ref): not ours to judge.
        raise RuntimeError(f"git merge {base_ref} failed: {merge.stderr.strip() or merge.stdout.strip()}")

    merge_base = _git(runner, "merge-base", "HEAD", base_ref).strip()
    merges = _git(runner, "rev-list", "--merges", f"{merge_base}..{base_ref}").split()
    # `+ <sha>`: main commit with no equal patch on staging; `- <sha>`: staging holds it.
    marks = [line.split() for line in _git(runner, "cherry", "HEAD", base_ref, merge_base).splitlines() if line]
    held = tuple(_subject(runner, sha) for mark, sha in marks if mark == "-")
    lacking = tuple(_subject(runner, sha) for mark, sha in marks if mark == "+")
    lacking += tuple(f"{_subject(runner, sha)} (a merge commit, which cannot be compared)" for sha in merges)

    if lacking or not held:
        return SyncResult(NEEDS_A_PERSON, conflicts=conflicts, held=held, lacking=lacking)

    _git(runner, "merge", "--no-ff", "-s", "ours", base_ref, "-m", message)
    return SyncResult(MERGED_AS_HELD, conflicts=conflicts, held=held)


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the ref to merge in, e.g. origin/main")
    parser.add_argument("--message", required=True, help="the merge commit's message")
    args = parser.parse_args(argv)

    result = sync(runner, args.base, args.message)
    if result.outcome == MERGED:
        print(f"Merged {args.base}.")
        return 0
    if result.outcome == MERGED_AS_HELD:
        print(
            f"::notice::{args.base} conflicted on {', '.join(result.conflicts)}, but staging already holds "
            f"every change it brings ({'; '.join(result.held)}). Recorded the merge and kept staging's content."
        )
        return 0
    print(
        f"::error::{args.base} conflicts with staging on {', '.join(result.conflicts)}. "
        f"Staging lacks {'; '.join(result.lacking) or 'no commit git cherry can name'}. "
        "A person resolves it: see the README's sync section."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
