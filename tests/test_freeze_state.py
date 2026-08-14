"""Unit tests for the release-freeze contract (``scripts/freeze_state.py``).

The behaviour worth pinning is the asymmetry: a reader that cannot establish the
freeze state must escalate, and a writer that cannot apply the freeze must fail.
Getting either backwards is silent in production. An escalating writer would
report a freeze that never happened; a failing reader would redden a green
pipeline from its notify job.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "freeze_state.py"
_spec = importlib.util.spec_from_file_location("freeze_state", _PATH)
freeze_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(freeze_state)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def fake_runner(responses):
    """Return a runner that answers each `gh api` call from `responses` in order."""
    calls = []

    def runner(argv):
        calls.append(list(argv))
        return responses[len(calls) - 1]

    runner.calls = calls
    return runner


RULESET_LIST = json.dumps(
    [
        {"id": 7, "name": "staging-freeze", "enforcement": "disabled"},
        {"id": 9, "name": "something-else", "enforcement": "active"},
    ]
)

RULESET_DETAIL = json.dumps(
    {
        "id": 7,
        "name": "staging-freeze",
        "target": "branch",
        "enforcement": "disabled",
        "bypass_actors": [{"actor_id": 1, "actor_type": "Integration"}],
        "conditions": {"ref_name": {"include": ["refs/heads/staging"]}},
        "rules": [{"type": "update"}],
    }
)


class TestIsFrozen:
    def test_active_enforcement_is_frozen(self):
        listing = json.dumps([{"id": 7, "name": "staging-freeze", "enforcement": "active"}])
        assert freeze_state.is_frozen("o/r", "staging-freeze", runner=fake_runner([completed(listing)]))

    def test_disabled_enforcement_is_not_frozen(self):
        assert not freeze_state.is_frozen(
            "o/r", "staging-freeze", runner=fake_runner([completed(RULESET_LIST)])
        )

    def test_matches_on_name_not_position(self):
        """A repo carries several rulesets; only the named one decides the freeze."""
        runner = fake_runner([completed(RULESET_LIST)])
        assert freeze_state.is_frozen("o/r", "something-else", runner=runner)

    def test_missing_ruleset_raises(self):
        with pytest.raises(freeze_state.LookupError_, match="not found"):
            freeze_state.is_frozen("o/r", "absent", runner=fake_runner([completed(RULESET_LIST)]))

    def test_api_failure_raises(self):
        runner = fake_runner([completed(stderr="HTTP 403", returncode=1)])
        with pytest.raises(freeze_state.LookupError_, match="Could not read rulesets"):
            freeze_state.is_frozen("o/r", "staging-freeze", runner=runner)


class TestReadMode:
    """The alerting path. It must never fail the job and never under-report."""

    def test_reports_false_when_thawed(self, capsys):
        code = freeze_state.main(
            ["read", "--repo", "o/r"], runner=fake_runner([completed(RULESET_LIST)])
        )
        assert code == 0
        assert "frozen=false" in capsys.readouterr().out

    def test_unreadable_ruleset_escalates_to_frozen(self, capsys):
        """A lookup problem must not silence a real release-blocking failure."""
        runner = fake_runner([completed(stderr="HTTP 403", returncode=1)])
        code = freeze_state.main(["read", "--repo", "o/r"], runner=runner)
        out = capsys.readouterr().out
        assert code == 0, "a notify job must never redden a green run"
        assert "frozen=true" in out
        assert "::warning::" in out

    def test_missing_ruleset_escalates_to_frozen(self, capsys):
        runner = fake_runner([completed(RULESET_LIST)])
        code = freeze_state.main(["read", "--repo", "o/r", "--ruleset-name", "renamed"], runner=runner)
        assert code == 0
        assert "frozen=true" in capsys.readouterr().out

    def test_on_error_fail_is_available_for_non_alerting_callers(self):
        runner = fake_runner([completed(stderr="HTTP 403", returncode=1)])
        assert freeze_state.main(["read", "--repo", "o/r", "--on-error", "fail"], runner=runner) == 1

    def test_writes_github_output(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        freeze_state.main(["read", "--repo", "o/r"], runner=fake_runner([completed(RULESET_LIST)]))
        assert out.read_text().strip() == "frozen=false"


class TestSetMode:
    """The freeze/unfreeze path. It must preserve the ruleset and fail loudly."""

    def test_put_preserves_bypass_actors_conditions_and_rules(self, tmp_path):
        body = tmp_path / "ruleset.json"
        runner = fake_runner([completed(RULESET_LIST), completed(RULESET_DETAIL), completed("{}")])
        freeze_state.set_enforcement(
            "o/r", "staging-freeze", "active", body_path=str(body), runner=runner
        )
        written = json.loads(body.read_text())
        assert written["enforcement"] == "active"
        assert written["bypass_actors"] == [{"actor_id": 1, "actor_type": "Integration"}]
        assert written["rules"] == [{"type": "update"}]
        assert written["conditions"]["ref_name"]["include"] == ["refs/heads/staging"]

    def test_reads_the_detail_endpoint_not_the_listing(self, tmp_path):
        """The listing omits rules and bypass actors; PUTting it back drops them."""
        runner = fake_runner([completed(RULESET_LIST), completed(RULESET_DETAIL), completed("{}")])
        freeze_state.set_enforcement(
            "o/r", "staging-freeze", "active", body_path=str(tmp_path / "b.json"), runner=runner
        )
        assert runner.calls[1] == ["gh", "api", "repos/o/r/rulesets/7"]

    def test_failed_put_raises(self, tmp_path):
        runner = fake_runner(
            [completed(RULESET_LIST), completed(RULESET_DETAIL), completed(stderr="HTTP 422", returncode=1)]
        )
        with pytest.raises(freeze_state.LookupError_, match="Could not update"):
            freeze_state.set_enforcement(
                "o/r", "staging-freeze", "active", body_path=str(tmp_path / "b.json"), runner=runner
            )

    def test_missing_ruleset_fails_the_job(self, tmp_path):
        """Never escalate here: a freeze that did not apply must stop the train."""
        code = freeze_state.main(
            ["set", "--repo", "o/r", "--enforcement", "active", "--body-path", str(tmp_path / "b.json")],
            runner=fake_runner([completed("[]")]),
        )
        assert code == 1
