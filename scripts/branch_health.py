"""Find deploy branches whose newest pipeline run is red, from outside the run.

The pipeline's own terminal notify job reports a failure once, at the moment it
happens, and that is almost always enough. Three cases it does not cover, all of
which have now happened:

1. Nobody was going to be told in the first place. A staging failure outside the
   release-freeze window is deliberately silent, because mid-week staging is the
   integration branch and paging the channel for it is what teaches people to
   scroll past the channel. But the branch is still red when Friday's freeze turns
   it into the release candidate, and at that moment nothing says so.

2. The alert was sent and then the branch stayed red. One message at the moment of
   breakage is easy to miss, and there is no second one.

3. The re-run did not re-notify. GitHub's "Re-run failed jobs" re-runs the failed
   job and everything downstream of it, so the terminal notify job fires again and
   reports the recovery. The per-job "Re-run this job" button does not: it re-runs
   that job alone, so a run can go from red to green with the notify job never
   running a second time. There is no hook to fix that from inside the run.

All three are the same shape: the truth about the branch is in the run history,
and nothing is reading it. So this reads it, on the schedule the pipeline watchdog
already runs on.

**It is a backstop, not an echo.** A red pipeline has already alerted from inside
its own run, so repeating that within seconds would train people to ignore both.
A finding has to be at least ``--min-age-minutes`` old, which turns the message
from "this failed" into "this is STILL failing and nobody has touched it". The one
exception is the freeze window opening, where the finding is old by definition and
the news is the window, not the failure.

What it deliberately does NOT report is ``startup_failure``. That is the case
where GitHub rejected the run at load time and no job ran at all, which
``notify-startup-failure`` already sweeps for with a different message and a
different thing to check. Splitting them keeps one finding from producing two
alerts.

When it fires, per branch:

- **main** — the newest conclusive run of a workflow is red, it started inside the
  lookback window, and it is at least ``--min-age-minutes`` old. Bounded
  repetition, the same reading the startup sweep uses: a couple of messages per
  breakage at a 30-minute cadence and a 90-minute window, then silence, because a
  stateless sweep cannot be exactly-once and a missed alert is the failure being
  fixed.

- **staging** — the same, and additionally only while the release freeze is on,
  because that is the window in which a red staging blocks a release. The window
  opening is itself a trigger: a branch that went red on Tuesday and is still red
  when Friday's freeze lands is reported then, which is the entire point, and the
  age floor does not apply to it.

The freeze workflow's own runs are read separately rather than from a branch
listing. A ``schedule`` trigger runs from the repository's DEFAULT branch, so the
freeze runs are attributed to ``main`` and are not in the ``staging`` history at
all. Looking for them there finds nothing, forever, silently.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# Conclusions that say something about the branch. `cancelled` and `skipped` are
# evidence of nothing, so they may not hide a failure behind them.
#
# `startup_failure` is CONCLUSIVE but not RED, and the asymmetry is deliberate.
# It belongs to the other sweep, so this one never reports it — but it is still the
# newest thing that happened, so it has to be able to supersede an older failure.
# Leaving it out of CONCLUSIVE entirely made the sweep reach past it and report a
# failure that a later run had already replaced, giving two alerts for one branch.
CONCLUSIVE = ("success", "failure", "timed_out", "startup_failure")
RED = ("failure", "timed_out")

# The freeze workflow's per-repo wrapper keeps this name verbatim, because the
# release-PR workflow chains off it by name. That makes it a stable handle for
# "the window just opened" without this file knowing anything about the schedule,
# which is the point: move the freeze and the alerting follows.
FREEZE_WORKFLOW_NAME = "Staging Freeze"


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def started_at(run: dict) -> datetime:
    """When the run's CURRENT attempt began.

    Not ``created_at``, which stays pinned to attempt 1 forever. A re-run hours
    later keeps the original ``created_at``, so an age window measured from it
    filters out the attempt that just failed — measured against live runs, a
    cowork-server attempt 2 had ``created_at`` 22:26:37 and ``run_started_at``
    23:47:11, already 80 minutes outside a 90-minute lookback before it started.
    Since a re-run is the most common way a failure gets fixed (and re-broken),
    that was the case this sweep most needed to see.

    ``created_at`` is the fallback because the freeze-run listing this module also
    parses is trimmed to the fields it needs.
    """
    return parse_time(run.get("run_started_at") or run["created_at"])


def newest_conclusive_per_workflow(runs: Iterable[dict]) -> list[dict]:
    """One run per workflow file: the most recent that concluded either way.

    Keyed on ``path`` rather than ``name`` because a ``run-name:`` override
    changes the display name and the path is stable.
    """
    newest: dict[str, dict] = {}
    for run in runs:
        if run.get("conclusion") not in CONCLUSIVE:
            continue
        path = run.get("path") or run.get("name") or ""
        current = newest.get(path)
        if current is None or started_at(run) > started_at(current):
            newest[path] = run
    return sorted(newest.values(), key=lambda run: run["path"])


def freeze_opened_within(freeze_runs: Iterable[dict], *, cutoff: datetime) -> bool:
    """True when the freeze workflow last succeeded inside the lookback window.

    This is what turns "staging has been red since Tuesday" into an alert on
    Friday. It reads the freeze workflow's own run history, so the trigger moves
    whenever the freeze moves.
    """
    for run in freeze_runs:
        if run.get("conclusion") != "success":
            continue
        if parse_time(run["created_at"]) >= cutoff:
            return True
    return False


def in_scope(path: str, *, only: Sequence[str], exclude: Sequence[str]) -> bool:
    """Whether this workflow file is one the sweep speaks about.

    ``only`` empty means every workflow on the branch, which is the default and is
    the widest this gets. It is worth knowing how wide that is: the sweep reads run
    history, not the notify wiring, so it reports any red workflow on a deploy
    branch and not only the pipelines that opted into an in-run alert. A repo that
    wants it narrowed passes `workflows:` with the paths that matter.

    ``exclude`` always carries the sweep's own workflow. A watchdog whose own run
    went red would otherwise report itself on the next tick, which reads as a
    pipeline failure and is really just the watchdog.
    """
    if path in exclude:
        return False
    return not only or path in only


def select_red(
    runs: list[dict],
    *,
    branch: str,
    cutoff: datetime,
    settled: datetime,
    frozen: bool,
    staging_branch: str,
    freeze_runs: Iterable[dict] = (),
    only: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> list[dict]:
    """The red pipelines on this branch that are worth a message right now.

    ``cutoff`` bounds how far back a finding may be (so the sweep stops eventually).
    ``settled`` bounds how RECENT it may be (so the sweep is not an echo of the
    alert the run already sent for itself).
    """
    if branch == staging_branch and not frozen:
        return []

    just_froze = branch == staging_branch and freeze_opened_within(freeze_runs, cutoff=cutoff)

    findings = []
    for run in newest_conclusive_per_workflow(runs):
        if run["conclusion"] not in RED:
            continue
        if not in_scope(run["path"], only=only, exclude=exclude):
            continue
        began = started_at(run)
        if not just_froze and not (cutoff <= began <= settled):
            continue
        findings.append(
            {
                "id": run["id"],
                "path": run["path"],
                "name": run["name"],
                "conclusion": run["conclusion"],
                "branch": branch,
                "head_sha": run["head_sha"],
                "html_url": run["html_url"],
                "actor": (run.get("triggering_actor") or {}).get("login", "unknown"),
                "title": run.get("display_title", ""),
                # Says which of the two reasons produced this finding, so the
                # message can explain itself rather than looking like a repeat.
                "reason": "freeze-opened" if just_froze else "still-red",
            }
        )
    return findings


def api(runner: Runner, path: str):
    result = runner(["gh", "api", path])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def fetch_runs(repo: str, branch: str, *, runner: Runner = _run) -> list[dict]:
    payload = api(runner, f"repos/{repo}/actions/runs?branch={branch}&status=completed&per_page=100")
    return payload.get("workflow_runs", [])


def fetch_freeze_runs(repo: str, *, runner: Runner = _run) -> list[dict]:
    """Recent runs of the freeze workflow, found by its name rather than a path.

    Queried through the workflow's own runs endpoint because a scheduled run is
    attributed to the default branch, so these never appear in a `staging`
    listing. Returns an empty list when the repo has no freeze workflow, which is
    the normal state for a repo that is not on the release train.
    """
    workflows = api(runner, f"repos/{repo}/actions/workflows?per_page=100").get("workflows", [])
    for workflow in workflows:
        if workflow.get("name") == FREEZE_WORKFLOW_NAME:
            payload = api(
                runner, f"repos/{repo}/actions/workflows/{workflow['id']}/runs?status=completed&per_page=10"
            )
            return payload.get("workflow_runs", [])
    return []


def main(argv: list[str] | None = None, *, runner: Runner = _run, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branches", required=True, help="space separated")
    parser.add_argument("--staging-branch", default="staging")
    parser.add_argument("--lookback-minutes", type=int, default=90)
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=30,
        help="how long a failure must have stood before this repeats it, so the sweep is a backstop rather than an echo",
    )
    parser.add_argument(
        "--frozen", default="false", help="freeze state of the staging branch, from freeze_state.py"
    )
    parser.add_argument(
        "--workflows",
        default="",
        help="space-separated workflow paths to report on; empty means every workflow on the branch",
    )
    parser.add_argument(
        "--self-path",
        default="",
        help="this sweep's own workflow path, never reported (a watchdog that went red reports itself otherwise)",
    )
    parser.add_argument("--out", default="red-branches.json")
    args = parser.parse_args(argv)

    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(minutes=args.lookback_minutes)
    settled = moment - timedelta(minutes=args.min_age_minutes)
    frozen = args.frozen == "true"

    branches = args.branches.split()
    only = args.workflows.split()
    exclude = [args.self_path] if args.self_path else []
    freeze_runs: list[dict] = []
    if frozen and args.staging_branch in branches:
        try:
            freeze_runs = fetch_freeze_runs(args.repo, runner=runner)
        except (RuntimeError, json.JSONDecodeError, KeyError) as exc:
            print(f"::warning::Could not read freeze workflow history: {exc}")

    findings: list[dict] = []
    unreadable: list[str] = []
    for branch in branches:
        try:
            runs = fetch_runs(args.repo, branch, runner=runner)
        except (RuntimeError, json.JSONDecodeError) as exc:
            # Loud in the summary, quiet in the exit code. A watchdog that
            # reddens the repo when the API is briefly unhappy gets muted, and a
            # muted watchdog is worse than no watchdog.
            #
            # Recorded rather than only logged, because the step summary used to
            # print "No deploy branch is sitting red" on this path — an affirmative
            # all-clear for a branch the sweep knows nothing about.
            print(f"::warning::Could not read run history for {branch}: {exc}")
            unreadable.append(branch)
            continue
        findings.extend(
            select_red(
                runs,
                branch=branch,
                cutoff=cutoff,
                settled=settled,
                frozen=frozen,
                staging_branch=args.staging_branch,
                freeze_runs=freeze_runs,
                only=only,
                exclude=exclude,
            )
        )

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(findings, handle)

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"count={len(findings)}\n")
            handle.write(f"unreadable={' '.join(unreadable)}\n")
    print(f"count={len(findings)} unreadable={' '.join(unreadable) or 'none'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
