"""Merge main into the checked-out staging branch, landing hotfixes staging already holds.

A hotfix lands on staging first and is then cherry-picked onto main, so by the
time main's push fires the sync, staging usually holds the same change already.
`git merge` still conflicts when staging has since edited a neighboring line,
which is what stopped cowork's sync on 17 Nov: both sides add the same import,
and the line under it differs. Nobody can land that merge by hand on a
squash-only staging, so the train stays red until someone works around it.

So on a conflict this records main as merged and keeps staging's content
(`merge -s ours`), but only when two checks show staging holds every change
main brings:

1. Every commit main gained since the merge base has an equal patch among the
   staging commits made since main last came in. Releases squash staging into
   main, so older staging commits never join main's history. Comparing with
   them would let a commit from releases ago vouch for a change staging has
   since undone.
2. Main's changes that merge cleanly change nothing: with each conflict
   resolved to staging's side, the merge gives staging's own tree. This catches
   a change staging applied and then reverted, which still has its equal patch.

If main holds a change staging lacks, a merge commit with no patch to compare,
or a clean change staging's tree doesn't have, it fails as before, and names the
conflicting files, the commits staging lacks and the files that differ.

Run it inside the staging checkout, with the merge identity configured and git
2.43 or later (`merge-tree -X`). Exit 0 means HEAD is a new merge commit ready
to push; exit 1 means a person must merge.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class Outcome(Enum):
    MERGED = "merged"  # an ordinary merge, no conflict
    MERGED_AS_HELD = "merged-as-held"  # a conflict on changes staging already holds; staging's content kept
    NEEDS_A_PERSON = "needs-a-person"  # a conflict on a change staging lacks


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str

    def __str__(self) -> str:
        return f"{self.sha[:12]} {self.subject}"


@dataclass(frozen=True)
class SyncResult:
    outcome: Outcome
    conflicts: tuple[str, ...] = ()  # files the plain merge conflicted on
    held: tuple[Commit, ...] = ()  # main commits with an equal patch among staging's commits since the last sync
    lacking: tuple[Commit, ...] = ()  # main commits with none
    merges: tuple[Commit, ...] = ()  # merge commits on main, which have no patch to compare
    differs: tuple[str, ...] = ()  # files main's side would still change with every conflict resolved to staging's


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _git(runner: Runner, *args: str) -> str:
    result = runner(["git", *args])
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _commit(runner: Runner, sha: str) -> Commit:
    return Commit(sha, _git(runner, "log", "-1", "--format=%s", sha).strip())


def _files_main_would_change(runner: Runner, base_ref: str) -> tuple[str, ...]:
    """Files whose content changes when main merges in with every conflict resolved to staging's side.

    `-s ours` drops all of main's side, its clean hunks included, so only an
    empty answer lets it keep staging's content without losing anything.
    """
    merged = runner(["git", "merge-tree", "--write-tree", "--name-only", "-X", "ours", "HEAD", base_ref])
    if merged.returncode not in (0, 1):
        raise RuntimeError(f"git merge-tree HEAD {base_ref} failed: {merged.stderr.strip() or merged.stdout.strip()}")
    tree, _, rest = merged.stdout.partition("\n")
    if merged.returncode == 1:
        # Conflicts `-X ours` can't settle, such as modify/delete: staging's side is not a whole answer.
        return tuple(rest.split("\n\n", 1)[0].splitlines())
    return tuple(_git(runner, "diff", "--name-only", "HEAD", tree.strip()).splitlines())


def sync(runner: Runner, base_ref: str, message: str) -> SyncResult:
    merge = runner(["git", "merge", "--no-ff", base_ref, "-m", message])
    if merge.returncode == 0:
        return SyncResult(Outcome.MERGED)

    conflicts = tuple(_git(runner, "diff", "--name-only", "--diff-filter=U").splitlines())
    runner(["git", "merge", "--abort"])
    if not conflicts:
        # The merge failed for some other reason (a dirty tree, a missing ref): not ours to judge.
        raise RuntimeError(f"git merge {base_ref} failed: {merge.stderr.strip() or merge.stdout.strip()}")

    merge_base = _git(runner, "merge-base", "HEAD", base_ref).strip()
    merges = tuple(_commit(runner, sha) for sha in _git(runner, "rev-list", "--merges", f"{merge_base}..{base_ref}").split())
    # Compare only with staging commits made since main last came in: those after the oldest merge
    # on staging's first-parent line that contains the merge base, or after the merge base itself
    # when staging has never merged main.
    synced = _git(runner, "rev-list", "--first-parent", "--ancestry-path", "--merges", f"{merge_base}..HEAD").split()
    since = synced[-1] if synced else merge_base
    # `=<sha>`: a main commit with an equal patch on staging; `+<sha>`: one without.
    marks = _git(runner, "rev-list", "--cherry-mark", "--right-only", "--no-merges", f"HEAD...{base_ref}", f"^{since}").split()
    held = tuple(_commit(runner, mark[1:]) for mark in marks if mark[0] == "=")
    lacking = tuple(_commit(runner, mark[1:]) for mark in marks if mark[0] == "+")
    differs = _files_main_would_change(runner, base_ref)

    if lacking or merges or differs or not held:
        return SyncResult(
            Outcome.NEEDS_A_PERSON, conflicts=conflicts, held=held, lacking=lacking, merges=merges, differs=differs
        )

    kept = "Kept staging's content: staging already holds " + "; ".join(str(commit) for commit in held) + "."
    _git(runner, "merge", "--no-ff", "-s", "ours", base_ref, "-m", message, "-m", kept)
    return SyncResult(Outcome.MERGED_AS_HELD, conflicts=conflicts, held=held)


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the ref to merge in, e.g. origin/main")
    parser.add_argument("--message", required=True, help="the merge commit's message")
    args = parser.parse_args(argv)

    result = sync(runner, args.base, args.message)
    if result.outcome is Outcome.MERGED:
        print(f"Merged {args.base}.")
        return 0
    if result.outcome is Outcome.MERGED_AS_HELD:
        print(
            f"::notice::{args.base} conflicted on {', '.join(result.conflicts)}, but staging already holds "
            f"every change it brings ({'; '.join(map(str, result.held))}). Recorded the merge and kept staging's content."
        )
        return 0

    reasons = []
    if result.lacking:
        reasons.append(f"Staging lacks {'; '.join(map(str, result.lacking))}.")
    if result.merges:
        reasons.append(f"Main has merge commits, which have no patch to compare: {'; '.join(map(str, result.merges))}.")
    if result.differs:
        reasons.append(f"Main's side would still change staging's {', '.join(result.differs)}.")
    if not reasons:
        reasons.append("Staging has no patch equal to any of main's commits.")
    print(
        f"::error::{args.base} conflicts with staging on {', '.join(result.conflicts)}. {' '.join(reasons)} "
        "A person resolves it: see the README's sync section."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
