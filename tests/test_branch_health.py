"""Unit tests for the red-branch sweep (``scripts/branch_health.py``).

The sweep exists because of a real incident: cowork-server's staging publish
failed on the commit that fixed a broken release candidate, the failure was
deliberately silent because staging was not frozen, and the only thing the
engineering channel ever heard was the green *recovered* message from the re-run
three and a half hours later. Nobody was told the rc stream had stalled.

The selection rules are the whole behaviour, so they are what these tests pin. In
particular the four that are easy to get backwards: staging is silent while it is
thawed no matter how red it is, the freeze window opening re-reports a branch that
has been red for days, a fresh failure is left to the pipeline's own notify job
rather than echoed, and the freeze workflow's runs are not on the staging branch.
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "branch_health.py"
_spec = importlib.util.spec_from_file_location("branch_health", _PATH)
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(minutes=90)
SETTLED = NOW - timedelta(minutes=30)


def run(
    *,
    path="publish-staging.yml",
    name="Staging pre-release and publish to PyPI",
    conclusion="failure",
    minutes_ago=45,
    run_id=1,
):
    created = NOW - timedelta(minutes=minutes_ago)
    return {
        "id": run_id,
        "path": path,
        "name": name,
        "conclusion": conclusion,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "head_sha": "abcdef1234567890",
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
        "triggering_actor": {"login": "someone"},
        "display_title": "a commit",
    }


def freeze_run(*, conclusion="success", minutes_ago=5):
    created = NOW - timedelta(minutes=minutes_ago)
    return {"conclusion": conclusion, "created_at": created.isoformat().replace("+00:00", "Z")}


def select(runs, *, branch="staging", frozen=True, freeze_runs=()):
    return health.select_red(
        runs,
        branch=branch,
        cutoff=CUTOFF,
        settled=SETTLED,
        frozen=frozen,
        staging_branch="staging",
        freeze_runs=freeze_runs,
    )


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestNewestConclusivePerWorkflow:
    def test_takes_the_newest_run_of_each_workflow(self):
        runs = [
            run(run_id=1, conclusion="failure", minutes_ago=60),
            run(run_id=2, conclusion="success", minutes_ago=40),
        ]
        assert [r["id"] for r in health.newest_conclusive_per_workflow(runs)] == [2]

    def test_ignores_cancelled_and_skipped(self):
        """Neither is evidence about the branch, so neither may hide a failure."""
        runs = [
            run(run_id=1, conclusion="failure", minutes_ago=60),
            run(run_id=2, conclusion="cancelled", minutes_ago=10),
            run(run_id=3, conclusion="skipped", minutes_ago=5),
        ]
        assert [r["id"] for r in health.newest_conclusive_per_workflow(runs)] == [1]

    def test_ignores_startup_failure(self):
        """That case is the other sweep's, and two alerts per finding is worse."""
        assert health.newest_conclusive_per_workflow([run(conclusion="startup_failure")]) == []

    def test_keyed_on_path_so_a_run_name_override_does_not_split_it(self):
        runs = [
            run(run_id=1, name="Publish", conclusion="failure", minutes_ago=60),
            run(run_id=2, name="Publish rc8 for #311", conclusion="success", minutes_ago=40),
        ]
        assert [r["id"] for r in health.newest_conclusive_per_workflow(runs)] == [2]

    def test_workflows_are_tracked_independently(self):
        runs = [
            run(path="a.yml", run_id=1, conclusion="failure"),
            run(path="b.yml", run_id=2, conclusion="success"),
        ]
        assert len(health.newest_conclusive_per_workflow(runs)) == 2


class TestStagingIsSilentWhileThawed:
    def test_red_staging_reports_nothing_outside_the_freeze(self):
        """Mid-week staging is the integration branch; this is the whole policy."""
        assert select([run(conclusion="failure")], frozen=False) == []

    def test_not_even_a_recovery_worth_of_noise(self):
        """No red, no green, no grey while thawed."""
        assert select([run(conclusion="success")], frozen=False) == []
        assert select([run(conclusion="success")], frozen=True) == []

    def test_a_freeze_that_just_opened_cannot_speak_for_a_thawed_branch(self):
        """`frozen` is the authority; a stale freeze run must not override it."""
        assert select([run(conclusion="failure")], frozen=False, freeze_runs=[freeze_run()]) == []

    def test_red_staging_reports_once_frozen(self):
        assert len(select([run(conclusion="failure")], frozen=True)) == 1


class TestBackstopNotEcho:
    def test_a_fresh_failure_is_left_to_the_pipelines_own_notify_job(self):
        """Repeating an alert seconds after it was sent trains people to ignore both."""
        assert select([run(conclusion="failure", minutes_ago=2)], branch="main", frozen=False) == []

    def test_a_failure_that_has_stood_is_reported(self):
        findings = select([run(conclusion="failure", minutes_ago=45)], branch="main", frozen=False)
        assert findings[0]["reason"] == "still-red"

    def test_the_age_floor_does_not_apply_when_the_window_opens(self):
        """The news there is the freeze, not the failure, so freshness is irrelevant."""
        runs = [run(conclusion="failure", minutes_ago=2)]
        findings = select(runs, frozen=True, freeze_runs=[freeze_run()])
        assert findings[0]["reason"] == "freeze-opened"


class TestFreezeWindowOpening:
    def test_a_branch_red_since_tuesday_is_reported_when_the_freeze_lands(self):
        """The incident this exists for: stale red, freeze opens, nobody knows."""
        runs = [run(conclusion="failure", minutes_ago=60 * 72)]
        findings = select(runs, frozen=True, freeze_runs=[freeze_run(minutes_ago=5)])
        assert [f["id"] for f in findings] == [1]
        assert findings[0]["reason"] == "freeze-opened"

    def test_a_stale_red_is_not_reported_forever_after_the_window_opened(self):
        """Without a fresh freeze run it falls back to the bounded lookback."""
        runs = [run(conclusion="failure", minutes_ago=60 * 72)]
        assert select(runs, frozen=True, freeze_runs=[freeze_run(minutes_ago=60 * 24)]) == []

    def test_a_failed_freeze_run_does_not_count_as_the_window_opening(self):
        runs = [run(conclusion="failure", minutes_ago=60 * 72)]
        assert select(runs, frozen=True, freeze_runs=[freeze_run(conclusion="failure")]) == []

    def test_no_freeze_workflow_at_all_degrades_to_the_lookback(self):
        """A repo off the release train still gets the ordinary bounded sweep."""
        assert len(select([run(conclusion="failure", minutes_ago=45)], frozen=True, freeze_runs=[])) == 1


class TestMainAlwaysReports:
    def test_main_reports_regardless_of_freeze_state(self):
        """A red main is always worth interrupting for; the freeze is irrelevant."""
        assert len(select([run(conclusion="failure")], branch="main", frozen=False)) == 1

    def test_main_respects_the_lookback_so_it_does_not_repeat_forever(self):
        assert select([run(conclusion="failure", minutes_ago=200)], branch="main", frozen=False) == []

    def test_timed_out_counts_as_red(self):
        assert len(select([run(conclusion="timed_out")], branch="main", frozen=False)) == 1


class TestFetchFreezeRuns:
    """A scheduled run is attributed to the DEFAULT branch, never to staging.

    Reading the freeze workflow out of a `staging` run listing finds nothing,
    forever, silently, which would have made the freeze-opened trigger dead code.
    """

    def test_finds_the_freeze_workflow_by_name_and_reads_its_own_runs(self):
        workflows = json.dumps(
            {"workflows": [{"id": 3, "name": "Tests"}, {"id": 8, "name": health.FREEZE_WORKFLOW_NAME}]}
        )
        runs = json.dumps({"workflow_runs": [{"conclusion": "success", "created_at": "2026-08-14T11:55:00Z"}]})
        calls = []

        def runner(argv):
            calls.append(argv[-1])
            return completed(workflows if "actions/workflows?" in argv[-1] else runs)

        assert len(health.fetch_freeze_runs("o/r", runner=runner)) == 1
        assert "actions/workflows/8/runs" in calls[1]
        assert "branch=" not in calls[1], "the freeze runs are not on the staging branch"

    def test_no_freeze_workflow_returns_empty_rather_than_raising(self):
        runner = lambda argv: completed(json.dumps({"workflows": [{"id": 3, "name": "Tests"}]}))
        assert health.fetch_freeze_runs("o/r", runner=runner) == []


class TestFindingShape:
    def test_carries_what_the_message_needs(self):
        finding = select([run(conclusion="failure")], frozen=True)[0]
        assert finding["branch"] == "staging"
        assert finding["actor"] == "someone"
        assert finding["head_sha"].startswith("abcdef")
        assert finding["html_url"].endswith("/1")

    def test_missing_triggering_actor_does_not_crash(self):
        raw = run(conclusion="failure")
        raw["triggering_actor"] = None
        assert select([raw], frozen=True)[0]["actor"] == "unknown"
