"""Contract tests for `notify-pipeline-status/action.yml`.

`scripts/notify_decision.py` decides whether an alert posts, and
`tests/test_notify_decision.py` pins that decision. This file pins the wiring
that feeds it, because the decision is only as good as the evidence it is handed
and two ENG-2324 defects lived entirely in the wiring:

- the prior-run lookup was gated on `inputs.status == 'recovered'`, so a failure
  never had a predecessor to be compared against and the dedupe below it could
  never fire
- the lookup read the branch's most recent fifty runs and then filtered by
  workflow, so a frequent cron on the same branch crowded every other workflow
  out of its own history and their recovery messages were silently dropped

Neither is reachable from Python, so they are asserted against the YAML.
"""

import yaml
from pathlib import Path

ACTION_PATH = Path(__file__).resolve().parents[1] / "notify-pipeline-status" / "action.yml"
ACTION = yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))
STEPS = ACTION["runs"]["steps"]


def step(step_id: str) -> dict:
    matches = [candidate for candidate in STEPS if candidate.get("id") == step_id]
    assert len(matches) == 1, f"expected exactly one step with id '{step_id}'"
    return matches[0]


class TestThePriorRunLookupRunsForBothDirections:
    """A failure needs a predecessor too, or the dedupe cannot fire."""

    def test_the_lookup_is_not_gated_on_the_recovered_status(self):
        assert "inputs.status" not in step("prev")["if"]

    def test_the_smoke_test_still_skips_the_lookup(self):
        """`force-post` posts without evidence in either direction, so reading the
        history would only cost an API call."""
        assert "inputs.force-post != 'true'" in step("prev")["if"]

    def test_a_thawed_freeze_scoped_caller_still_skips_the_lookup(self):
        assert "steps.freeze.outputs.frozen != 'false'" in step("prev")["if"]

    def test_the_lookup_result_reaches_the_decision(self):
        decide = step("msg")
        assert decide["env"]["PREV_CONCLUSION"] == "${{ steps.prev.outputs.prev_conclusion }}"
        assert "--prev-conclusion" in decide["run"]


class TestTheLookupAsksForOneWorkflowsOwnHistory:
    """The branch-wide page is what a five-minute cron crowds out."""

    def test_it_calls_the_per_workflow_runs_endpoint(self):
        assert "actions/workflows/${WF_PATH##*/}/runs" in step("prev")["run"]

    def test_the_per_workflow_call_is_scoped_to_the_branch_and_conclusive_runs(self):
        run = step("prev")["run"]
        assert "branch=${BRANCH}&status=completed" in run

    def test_a_caller_outside_this_repo_keeps_the_branch_scan(self):
        """`workflow_ref` can name another repo's workflow, which has no endpoint
        here. That case still has to resolve to something rather than crash."""
        run = step("prev")["run"]
        assert "repos/${REPO}/actions/runs?branch=" in run
        assert "select(.path == env.WF_PATH or .name == env.WORKFLOW)" in run

    def test_the_current_run_is_still_excluded(self):
        assert "select((.id|tostring) != env.RUN_ID)" in step("prev")["run"]

    def test_cancelled_and_skipped_runs_are_still_not_evidence(self):
        run = step("prev")["run"]
        assert '.conclusion == "success"' in run
        assert '.conclusion == "cancelled"' not in run
        assert '.conclusion == "skipped"' not in run

    def test_an_unreadable_history_degrades_instead_of_failing_the_job(self):
        """A notify step must never turn a green pipeline red, so a refused lookup
        reports no evidence and exits 0."""
        run = step("prev")["run"]
        assert "no evidence either way" in run
        assert "exit 0" in run

    def test_a_rerun_still_checks_the_previous_attempt_first(self):
        """Attempts share a run id, so the failing attempt is the very run the
        history lookup excludes as 'the current one'."""
        assert "attempts/${PREV_ATTEMPT}" in step("prev")["run"]


class TestTheCallerGrantsTheLookupItsPermission:
    def test_the_action_documents_the_actions_read_requirement(self):
        assert "actions: read" in ACTION_PATH.read_text(encoding="utf-8")
