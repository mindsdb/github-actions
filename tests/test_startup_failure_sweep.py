"""Tests for the startup-failure sweep's selection, the ``jq`` program inside
``notify-startup-failure.yml``.

The program is read out of the workflow file and run with the real ``jq``, so the
tests check the code that ships rather than a copy of it.

The case that matters is anton's 9 Nov 2026 run 42654716938. GitHub's Actions
policy refused a ``pull_request_target`` CLA run on the release pull request
whose head is ``staging``. GitHub gives a pull request run its head branch, so the
``staging`` listing returned it, and the sweep reported "the branch is not
deployed" while the push to ``staging`` from the same commit had deployed eight
seconds earlier. A pull request or comment run says nothing about a deploy
branch, so the sweep keeps only the events a branch starts for itself.
"""

import importlib.util
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "notify-startup-failure.yml"

_spec = importlib.util.spec_from_file_location("branch_health", _ROOT / "scripts" / "branch_health.py")
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)

# Wide enough to cover 9 Nov 2026 from any date the tests run on.
CUTOFF = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

# anton's two runs on `staging` from commit 42d4dde, 9 Nov 2026, trimmed to the
# fields the sweep reads. The push deployed; the CLA run never started.
ANTON_PUSH = {
    "id": 42654693935,
    "path": ".github/workflows/publish-staging.yml",
    "name": "Staging pre-release and publish to PyPI",
    "event": "push",
    "head_branch": "staging",
    "conclusion": "success",
    "created_at": "2026-11-09T16:25:54Z",
    "head_sha": "42d4dde3214fe4b9d5b9973a2fd201ba8f94b6fc",
    "html_url": "https://github.com/mindsdb/anton/actions/runs/42654693935",
    "display_title": "Merge pull request #765 from mindsdb/bugfix/day-dated-rows-last-week",
}
ANTON_CLA = {
    "id": 42654716938,
    "path": ".github/workflows/cla.yml",
    "name": "MindsDB Anton CLA Assistant",
    "event": "pull_request_target",
    "head_branch": "staging",
    "conclusion": "startup_failure",
    "created_at": "2026-11-09T16:26:02Z",
    "head_sha": "42d4dde3214fe4b9d5b9973a2fd201ba8f94b6fc",
    "html_url": "https://github.com/mindsdb/anton/actions/runs/42654716938",
    "display_title": "MindsDB Anton CLA Assistant",
}

# Events seen on deploy branches that are not the branch starting a run for
# itself (run listings for staging and main across seven repositories, 11 Nov
# 2026), plus the pull request events a listing can return.
NOT_DEPLOY_EVENTS = (
    "pull_request",
    "pull_request_target",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
    "merge_group",
)


def sweep_program() -> str:
    """The ``jq`` program the sweep step runs on each branch's run listing."""
    jobs = yaml.safe_load(_WORKFLOW.read_text())["jobs"]
    step = next(s for s in jobs["sweep"]["steps"] if s.get("id") == "find")
    match = re.search(r"jq --argjson cutoff \S+ --arg branch \S+ '(.*?)' \"runs-", step["run"], re.DOTALL)
    assert match, "the sweep's jq program moved; update sweep_program()"
    return match.group(1)


def sweep(runs: list[dict], *, branch: str = "staging") -> list[dict]:
    result = subprocess.run(
        ["jq", "--argjson", "cutoff", str(CUTOFF), "--arg", "branch", branch, sweep_program()],
        input=json.dumps({"workflow_runs": runs}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def alert_payload(findings: list[dict], tmp_path: Path) -> dict:
    """The Slack payload the alert step builds from the sweep's findings."""
    jobs = yaml.safe_load(_WORKFLOW.read_text())["jobs"]
    step = next(s for s in jobs["sweep"]["steps"] if s.get("name") == "Post the alert")
    match = re.search(r"--arg sweep \"\$SWEEP_URL\" '(.*?)' > payload.json", step["run"], re.DOTALL)
    assert match, "the alert's jq program moved; update alert_payload()"
    reportable = tmp_path / "reportable.json"
    reportable.write_text(json.dumps(findings))
    args = {"repo": "mindsdb/anton", "repo_name": "anton", "server": "https://github.com", "channel": "C1", "sweep": "https://sweep"}
    argv = ["jq", "-n", "--slurpfile", "findings", str(reportable)]
    for name, value in args.items():
        argv += ["--arg", name, value]
    result = subprocess.run([*argv, match.group(1)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def startup_failure(*, event: str, run_id: int = 1, path: str = ".github/workflows/deploy.yml") -> dict:
    return {**ANTON_PUSH, "id": run_id, "path": path, "event": event, "conclusion": "startup_failure"}


class TestAnton9Nov:
    def test_the_refused_cla_run_is_not_a_staging_run(self):
        assert sweep([ANTON_CLA, ANTON_PUSH]) == []

    def test_a_push_that_never_started_is_still_reported(self):
        findings = sweep([startup_failure(event="push", run_id=7), ANTON_CLA])

        assert [(f["id"], f["branch"]) for f in findings] == [(7, "staging")]


class TestEvents:
    def test_the_jq_event_list_is_deploy_events(self):
        match = re.search(r"select\(\.event \| IN\(([^)]*)\)\)", sweep_program())

        assert match, "the sweep's event filter moved; update this test"
        assert tuple(json.loads(f"[{match.group(1)}]")) == health.DEPLOY_EVENTS

    def test_every_deploy_event_is_reported(self):
        runs = [
            startup_failure(event=event, run_id=i, path=f".github/workflows/{event}.yml")
            for i, event in enumerate(health.DEPLOY_EVENTS)
        ]

        assert len(sweep(runs)) == len(health.DEPLOY_EVENTS)

    def test_no_pull_request_or_comment_event_is_reported(self):
        runs = [
            startup_failure(event=event, run_id=i, path=f".github/workflows/{event}.yml")
            for i, event in enumerate(NOT_DEPLOY_EVENTS)
        ]

        assert sweep(runs) == []

    def test_a_later_pull_request_run_does_not_hide_a_push_that_never_started(self):
        push = startup_failure(event="push", run_id=1)
        later_pr = {**push, "id": 2, "event": "pull_request", "conclusion": "success", "created_at": "2026-11-09T17:00:00Z"}

        assert [f["id"] for f in sweep([push, later_pr])] == [1]


class TestMessage:
    def test_names_both_causes_links_the_run_and_claims_no_deploy(self, tmp_path):
        findings = sweep([startup_failure(event="push", run_id=7)])
        payload = alert_payload(findings, tmp_path)
        texts = json.dumps(payload)

        assert "permissions:" in texts
        assert "workflows.actions_policy_violation" in texts
        assert "https://github.com/mindsdb/anton/actions/runs/7" in texts
        assert "Started by" in texts
        assert "not deployed" not in texts
