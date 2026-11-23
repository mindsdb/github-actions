"""Keep a pull request that changes order-sensitive files from merging on an old base.

Two branches can each add a database migration on the same parent. Each passes
its own CI, because each ran against the base as it was at its last push, and
the second one to merge leaves the base with two migration heads. GitHub never
re-runs a pull request's checks when its base moves, and its "require branches
to be up to date" setting applies to every pull request, not only to the few
that touch migrations.

So the rule lives here, as one required check named ``migration-freshness``:

* A pull request is *protected* when it changes a path that matches one of the
  calling repository's patterns, or when it changes a line matching
  ``constant_pattern`` in ``constant_file`` (a version constant two pull
  requests can both bump from the same value).
* A protected pull request is *fresh* when its head contains the live tip of
  its base branch. Its head tree is then the tree the merge produces, so the
  repository's own migration-graph check can run on the head and see what the
  base will see.

``pr`` runs in a workflow of its own, on a pull request's opened, reopened,
synchronize and edited events. It passes an unprotected pull request at once
and fails a protected one whose head lacks the tip. The calling job runs its
graph check after it, on the head, only when the output ``protected`` is
``true``. GitHub sends ``edited`` when a pull request's base changes, so a
retargeted pull request is checked against its new base.

``refresh`` runs on every push to a gated base, and on a manual dispatch. A
green check run stays green after the base moves, so it writes a ``failure``
commit status with the same name on the head of every open protected pull
request that the push left behind. GitHub requires a check run and a commit
status that share a required name to both pass, so that pull request cannot
merge until its author merges the base and the check runs again on the new
head. It never writes ``success``: a head that lacks the tip once lacks it for
good, because the base only moves forward.

``backfill`` is ``refresh`` for the day the rule turns on. Open pull requests
from before then have no ``migration-freshness`` check run at all, so it also
writes ``success`` on the heads of the unprotected ones, which would otherwise
wait for a push they have no reason to make.

Patterns use ``fnmatch``, where ``*`` also matches ``/``: ``*/migrations/*``
covers every app's migrations folder at any depth.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Callable, Sequence

# A `gh` invocation, injectable so the tests need no network and no token.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

CONTEXT = "migration-freshness"

# Seconds to wait before the second and the third try of a failed `gh api`
# call. One refresh makes dozens of calls, and a single 502 or other transient
# answer must not leave a stale pull request green until the next push.
RETRY_DELAYS = (1, 4)

# The wait between tries; the tests replace it so they never really sleep.
sleep = time.sleep

# GitHub's pull request files endpoint stops at 3000 files and drops the
# `patch` of a file whose diff is too large. Either one hides what changed, so
# both count as protected rather than as clean.
FILES_LIMIT = 3000


class GitHubError(Exception):
    """A `gh` call failed. Carries the message the job should print."""


@dataclass(frozen=True)
class Rules:
    """What the calling repository protects."""

    paths: tuple[str, ...]
    constant_file: str = ""
    constant_pattern: str = ""

    def __post_init__(self) -> None:
        # A rule that protects nothing passes every pull request without a word.
        if not self.paths:
            raise ValueError("protected-paths is empty, so nothing would be protected")
        folders = [pattern for pattern in self.paths if pattern.endswith("/")]
        if folders:
            raise ValueError(f"{', '.join(folders)} ends in / and matches no file; add a * to cover the folder")
        if bool(self.constant_file) != bool(self.constant_pattern):
            raise ValueError("constant-file and constant-pattern go together")


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    previous_filename: str | None
    patch: str | None


@dataclass(frozen=True)
class Scope:
    """Whether a pull request is protected, and which file made it so."""

    protected: bool
    reason: str


@dataclass(frozen=True)
class OpenPull:
    number: int
    head_sha: str


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _gh_json(args: Sequence[str], runner: Runner) -> object:
    """Run `gh api`, trying again after each of RETRY_DELAYS, and parse its JSON.

    Every call here reads, except the status write, which GitHub accepts again
    without harm, so each one is safe to repeat.
    """
    result = runner(["gh", "api", *args])
    for delay in RETRY_DELAYS:
        if result.returncode == 0:
            break
        sleep(delay)
        result = runner(["gh", "api", *args])
    if result.returncode != 0:
        # Both streams: `gh` prints the API's error body on stdout and its own
        # diagnostics on stderr.
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitHubError(f"gh api {' '.join(args)}: {detail}")
    return json.loads(result.stdout)


def _paginated(path: str, runner: Runner) -> list[dict]:
    pages = _gh_json(["--paginate", "--slurp", path], runner)
    return [item for page in pages for item in page]


def base_tip(repo: str, base: str, runner: Runner) -> str:
    ref = _gh_json([f"repos/{repo}/git/ref/heads/{base}"], runner)
    return ref["object"]["sha"]


def changed_files(repo: str, number: int, runner: Runner) -> list[ChangedFile]:
    return [
        ChangedFile(
            filename=item["filename"],
            previous_filename=item.get("previous_filename"),
            patch=item.get("patch"),
        )
        for item in _paginated(f"repos/{repo}/pulls/{number}/files?per_page=100", runner)
    ]


def behind_by(repo: str, tip: str, head_sha: str, runner: Runner) -> int:
    """How many of the base tip's commits the head lacks."""
    # One commit per page: the count is all this reads, and a release pull
    # request's full commit list runs to megabytes.
    comparison = _gh_json([f"repos/{repo}/compare/{tip}...{head_sha}?per_page=1"], runner)
    return int(comparison["behind_by"])


def scope(files: Sequence[ChangedFile], rules: Rules) -> Scope:
    if len(files) >= FILES_LIMIT:
        return Scope(True, f"{len(files)} files, at GitHub's listing limit, so the list may be partial")
    constant = re.compile(rules.constant_pattern) if rules.constant_pattern else None
    for changed in files:
        for name in filter(None, (changed.filename, changed.previous_filename)):
            for pattern in rules.paths:
                if fnmatchcase(name, pattern):
                    return Scope(True, f"{name} matches {pattern}")
        if constant is not None and rules.constant_file in (changed.filename, changed.previous_filename):
            if changed.patch is None:
                return Scope(True, f"{changed.filename} has no diff to read, so a constant may have changed")
            # A per-file patch has no ---/+++ headers, so every +/- line is content.
            for line in changed.patch.splitlines():
                if line.startswith(("+", "-")) and constant.search(line[1:]):
                    return Scope(True, f"{changed.filename} changes {line[1:].strip()}")
    return Scope(False, "no protected path changed")


def open_pulls(repo: str, base: str, runner: Runner) -> list[OpenPull]:
    return [
        OpenPull(number=item["number"], head_sha=item["head"]["sha"])
        for item in _paginated(f"repos/{repo}/pulls?state=open&base={base}&per_page=100", runner)
    ]


def latest_state(repo: str, sha: str, runner: Runner) -> str | None:
    """The newest ``migration-freshness`` commit status on ``sha``, or None."""
    statuses = _gh_json([f"repos/{repo}/commits/{sha}/statuses?per_page=100"], runner)
    for status in statuses:  # newest first
        if status["context"] == CONTEXT:
            return status["state"]
    return None


def set_status(repo: str, sha: str, state: str, description: str, runner: Runner) -> None:
    _gh_json(
        [
            "--method",
            "POST",
            f"repos/{repo}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={CONTEXT}",
            "-f",
            f"description={description[:140]}",
        ],
        runner,
    )


@dataclass(frozen=True)
class PrResult:
    gated: bool
    scope: Scope
    tip: str = ""
    behind: int = 0

    @property
    def fresh(self) -> bool:
        return self.behind == 0


def check_pr(
    repo: str, number: int, base: str, head_sha: str, gated_bases: Sequence[str], rules: Rules, runner: Runner
) -> PrResult:
    if base not in gated_bases:
        return PrResult(gated=False, scope=Scope(False, f"{base} is not a gated base"))
    found = scope(changed_files(repo, number, runner), rules)
    if not found.protected:
        return PrResult(gated=True, scope=found)
    tip = base_tip(repo, base, runner)
    return PrResult(gated=True, scope=found, tip=tip, behind=behind_by(repo, tip, head_sha, runner))


@dataclass(frozen=True)
class RefreshAction:
    number: int
    head_sha: str
    state: str  # "failure" or "success" to write, "" to write nothing, "error" when the lookup failed
    description: str
    reason: str


@dataclass(frozen=True)
class RefreshPlan:
    tip: str
    actions: tuple[RefreshAction, ...]


def plan_refresh(repo: str, base: str, rules: Rules, backfill: bool, runner: Runner) -> RefreshPlan:
    """Decide what to write on each open pull request into ``base``.

    One pull request's failed lookup does not stop the others; it is reported
    and makes the run fail, so a refresher that skipped someone shows red.
    """
    tip = base_tip(repo, base, runner)
    actions: list[RefreshAction] = []
    for pull in open_pulls(repo, base, runner):
        try:
            found = scope(changed_files(repo, pull.number, runner), rules)
            if not found.protected:
                if backfill and latest_state(repo, pull.head_sha, runner) is None:
                    actions.append(
                        RefreshAction(pull.number, pull.head_sha, "success", "No protected path changed", found.reason)
                    )
                continue
            if behind_by(repo, tip, pull.head_sha, runner) == 0:
                note = "fresh; its next push reports the check" if backfill else "fresh"
                actions.append(RefreshAction(pull.number, pull.head_sha, "", note, found.reason))
            elif latest_state(repo, pull.head_sha, runner) == "failure":
                actions.append(RefreshAction(pull.number, pull.head_sha, "", "already marked stale", found.reason))
            else:
                # A status belongs to the head commit, which another pull request
                # into another base can share, so the text names this one.
                stale = f"#{pull.number} into {base}: {base} moved to {tip[:7]}; merge {base} into that branch and push"
                actions.append(RefreshAction(pull.number, pull.head_sha, "failure", stale, found.reason))
        except GitHubError as error:
            actions.append(RefreshAction(pull.number, pull.head_sha, "error", str(error), ""))
    return RefreshPlan(tip=tip, actions=tuple(actions))


def _write_lines(path: str | None, lines: Sequence[str]) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("".join(f"{line}\n" for line in lines))


def _rules(args: argparse.Namespace) -> Rules:
    return Rules(
        paths=tuple(line.strip() for line in args.paths.splitlines() if line.strip()),
        constant_file=args.constant_file,
        constant_pattern=args.constant_pattern,
    )


def run_pr(args: argparse.Namespace, rules: Rules, runner: Runner) -> int:
    result = check_pr(args.repo, args.pr, args.base, args.head_sha, args.gated_bases.split(), rules, runner)
    protected = result.gated and result.scope.protected
    _write_lines(args.github_output, [f"protected={'true' if protected else 'false'}", f"tip={result.tip}"])
    if not protected:
        message = f"Not protected: {result.scope.reason}."
    elif result.fresh:
        message = f"Protected ({result.scope.reason}); the head contains {args.base} at {result.tip[:7]}."
    else:
        message = (
            f"Stale: {result.scope.reason}, and the head is {result.behind} commit(s) behind {args.base} "
            f"at {result.tip[:7]}. Merge {args.base} into this branch and push, so the migration checks "
            f"run on the tree this pull request would merge into."
        )
    print(message)
    _write_lines(args.step_summary, [f"**{CONTEXT}:** {message}"])
    return 0 if (not protected or result.fresh) else 1


def run_refresh(args: argparse.Namespace, rules: Rules, runner: Runner, backfill: bool) -> int:
    mode = "backfill" if backfill else "refresh"
    if args.base not in args.gated_bases.split():
        message = f"`{args.base}` is not one of the gated bases ({args.gated_bases}), so the {mode} wrote nothing."
        print(f"::notice::{CONTEXT} {mode}: {message}")
        _write_lines(args.step_summary, [f"**{CONTEXT} {mode}:** {message}"])
        return 0
    plan = plan_refresh(args.repo, args.base, rules, backfill, runner)
    failed = 0
    lines = [f"**{CONTEXT} {mode}** of open pull requests into `{args.base}` at {plan.tip[:7]}:", ""]
    for action in plan.actions:
        if action.state in ("failure", "success"):
            try:
                set_status(args.repo, action.head_sha, action.state, action.description, runner)
                lines.append(f"- #{action.number}: wrote {action.state} ({action.reason})")
            except GitHubError as error:
                failed += 1
                lines.append(f"- #{action.number}: could not write {action.state}: {error}")
        elif action.state == "error":
            failed += 1
            lines.append(f"- #{action.number}: could not check: {action.description}")
        else:
            lines.append(f"- #{action.number}: {action.description} ({action.reason})")
    if not plan.actions:
        lines.append("- no open pull request needed a status")
    print("\n".join(lines))
    _write_lines(args.step_summary, lines)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("pr", "refresh", "backfill"))
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--base", required=True, help="the base branch")
    parser.add_argument("--paths", required=True, help="protected path patterns, one per line")
    parser.add_argument("--constant-file", default="")
    parser.add_argument("--constant-pattern", default="")
    parser.add_argument("--pr", type=int, help="pr mode: the pull request number")
    parser.add_argument("--head-sha", default="", help="pr mode: the pull request's head commit")
    parser.add_argument("--gated-bases", default="staging main", help="the base branches the rule covers")
    parser.add_argument("--github-output")
    parser.add_argument("--step-summary")
    args = parser.parse_args(argv)
    try:
        rules = _rules(args)
    except ValueError as error:
        parser.error(str(error))
    try:
        if args.mode == "pr":
            if args.pr is None or not args.head_sha:
                parser.error("pr mode needs --pr and --head-sha")
            return run_pr(args, rules, runner)
        return run_refresh(args, rules, runner, backfill=args.mode == "backfill")
    except GitHubError as error:
        # Fail closed: a check that cannot read the pull request must not pass it.
        print(f"::error::{CONTEXT} could not read GitHub: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
