"""Guards on ``config/cla-actions-policy.json``, the org Actions event policy.

GitHub blocks ``pull_request_target`` in public repositories by default unless an
Actions event policy allows it. This policy allows it for the CLA wrapper alone.
Both of its edges are security decisions, and nothing else fails when they drift:

- Widen the workflow target and every ``pull_request_target`` workflow in the org
  runs again, which is the exposure GitHub's block exists to close.
- Drop ``issue_comment`` and the policy's allow-list refuses the comment runs, so
  nobody can sign or ``recheck``.
"""

import json
from pathlib import Path

_POLICY = Path(__file__).resolve().parents[1] / "config" / "cla-actions-policy.json"

CLA_WRAPPER = ".github/workflows/cla.yml"


def load_policy() -> dict:
    return json.loads(_POLICY.read_text())


def test_targets_the_cla_wrapper_alone():
    workflow_path = load_policy()["conditions"]["workflow_path"]

    assert workflow_path["include"] == [CLA_WRAPPER]
    assert workflow_path["exclude"] == []


def test_allows_exactly_the_wrapper_triggers():
    rules = load_policy()["rules"]

    assert [rule["type"] for rule in rules] == ["restrict_action_events"]
    assert sorted(rules[0]["parameters"]["allowed_events"]) == [
        "issue_comment",
        "pull_request_target",
    ]


def test_is_enforced():
    assert load_policy()["enforcement"] == "active"
