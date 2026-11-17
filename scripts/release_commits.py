"""What the release PR lists: the commits on staging that no release has shipped.

`git log main..staging` is that list only while releases merge with a merge
commit. Most callers allow squash merges only, and a squashed release puts one
new commit on main: the staging commits it shipped never become ancestors of
main, so `main..staging` keeps every commit ever released. cowork's release
title reached 868 commits, and only 18 of them were unreleased. Its body passed
GitHub's 65,536 character limit, so `gh pr create` failed on every staging build.

So the list starts at the last release instead: the head of the newest merged
`staging -> main` PR. A commit is listed when it is on staging and reachable
from neither main nor that head. With merge-commit releases the head is already
on main, so the list is `main..staging`, exactly as before. With no previous
release, or a head staging no longer contains (staging was rebuilt), it falls
back to `main..staging` and says why.

The Changes list is cut to a character budget, so a long but correct list (a
long freeze, a first release) still opens the PR. The full list goes to the
run's step summary, and the caller links that run when the list was cut.
GitHub's compare view is no substitute: it fails to render a range that long.

Run it inside the caller's checkout, with `origin/<base>` and
`origin/<staging>` fetched at full depth. It prints one JSON object.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Callable, Sequence

# A command runner, injectable so the tests can answer `gh` without a network.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# GitHub refuses a PR body over 65,536 characters. The rest of the body (title
# line, contributors, checklist, state line) stays well under 5,000, so the
# commit list gets the remainder.
DEFAULT_MAX_CHARS = 60_000

# Enough merged release PRs to find the newest even when `gh pr list` orders
# them by creation rather than by merge time.
RELEASE_LOOKBACK = 20


@dataclass(frozen=True)
class ReleaseCommits:
    """The unreleased commits, as the release PR shows them."""

    since: str  # the commit the list starts after: the last release head, or origin/<base>
    since_reason: str  # why that start point, for the run log
    lookup_failed: bool  # the merged-PR lookup errored, so the list fell back to origin/<base>
    count: int  # every unreleased non-merge commit, listed or not
    commits: str  # `git log --oneline` lines, cut to the budget
    all_commits: str  # every unreleased `git log --oneline` line, uncut, for the run summary
    omitted: int  # commits cut from `commits` by the budget
    contributors: str  # "- <author>" lines of every unreleased commit, listed or cut, sorted and unique


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _git(runner: Runner, *args: str) -> str:
    result = runner(["git", *args])
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def last_release_head(runner: Runner, repo: str, base: str, staging: str) -> str | None:
    """The head commit of the newest merged `staging -> base` PR, or None if there is none.

    A failed lookup raises: "no release yet" and "could not ask" lead to the same
    fallback, but only the second deserves a warning in the run log.
    """
    result = runner(
        [
            "gh", "pr", "list",
            "--repo", repo,
            "--base", base,
            "--head", staging,
            "--state", "merged",
            "--limit", str(RELEASE_LOOKBACK),
            "--json", "headRefOid,mergedAt",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {result.stderr.strip()}")
    merged = [pr for pr in json.loads(result.stdout or "[]") if pr.get("mergedAt")]
    if not merged:
        return None
    return max(merged, key=lambda pr: pr["mergedAt"])["headRefOid"]


def _is_ancestor(runner: Runner, commit: str, of: str) -> bool:
    result = runner(["git", "merge-base", "--is-ancestor", commit, of])
    if result.returncode in (0, 1):
        return result.returncode == 0
    # 128 and the rest: the commit is not in this clone, so staging cannot contain it.
    return False


def release_commits(
    runner: Runner,
    repo: str,
    base: str,
    staging: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ReleaseCommits:
    base_ref = f"origin/{base}"
    staging_ref = f"origin/{staging}"

    lookup_failed = False
    try:
        head = last_release_head(runner, repo, base, staging)
    except RuntimeError as exc:
        head, lookup_failed = None, True
        reason = f"could not look up the last release ({exc}); listing {base_ref}..{staging_ref}"
    else:
        if head is None:
            reason = f"no merged {staging} -> {base} PR yet; listing {base_ref}..{staging_ref}"
        elif not _is_ancestor(runner, head, staging_ref):
            reason = f"last release head {head[:12]} is not on {staging_ref}; listing {base_ref}..{staging_ref}"
            head = None
        else:
            reason = f"last release head {head[:12]}"

    # On staging, and reachable from neither main nor the last release head.
    excluded = [f"^{base_ref}"] + ([f"^{head}"] if head else [])
    since = head or _git(runner, "rev-parse", base_ref).strip()

    lines = _git(runner, "log", "--oneline", "--no-merges", staging_ref, *excluded).splitlines()
    authors = _git(runner, "log", "--format=%an", "--no-merges", staging_ref, *excluded).splitlines()

    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1

    return ReleaseCommits(
        since=since,
        since_reason=reason,
        lookup_failed=lookup_failed,
        count=len(lines),
        commits="\n".join(kept),
        all_commits="\n".join(lines),
        omitted=len(lines) - len(kept),
        contributors="\n".join(f"- {name}" for name in sorted(set(authors))),
    )


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name, for the merged-PR lookup")
    parser.add_argument("--base", required=True, help="the release target branch, e.g. main")
    parser.add_argument("--staging", required=True, help="the release source branch, e.g. staging")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    args = parser.parse_args(argv)

    result = release_commits(runner, args.repo, args.base, args.staging, args.max_chars)
    level = "warning" if result.lookup_failed else "notice"
    print(f"::{level}::Release PR lists from {result.since_reason}: {result.count} commit(s).", file=sys.stderr)

    # The run summary holds every commit, so the PR body can link it when the
    # list there was cut. A step summary holds 1 MiB, about 12,000 lines.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(
                f"### Release PR list: {result.count} commit(s)\n\n"
                f"Start point: {result.since_reason}.\n\n```\n{result.all_commits}\n```\n"
            )

    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
