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


def values(frozen):
    return json.dumps(
        [
            {"property_name": "team", "value": "devops"},
            {"property_name": "staging_frozen", "value": frozen},
        ]
    )


class TestIsFrozen:
    def test_true_string_is_frozen(self):
        assert freeze_state.is_frozen("o/r", "staging_frozen", runner=fake_runner([completed(values("true"))]))

    def test_false_string_is_not_frozen(self):
        assert not freeze_state.is_frozen(
            "o/r", "staging_frozen", runner=fake_runner([completed(values("false"))])
        )

    def test_never_set_is_not_frozen(self):
        """A repo the property was never written on comes back null, not absent."""
        assert not freeze_state.is_frozen(
            "o/r", "staging_frozen", runner=fake_runner([completed(values(None))])
        )

    def test_real_boolean_is_tolerated(self):
        assert freeze_state.is_frozen("o/r", "staging_frozen", runner=fake_runner([completed(values(True))]))

    def test_matches_on_name_not_position(self):
        """A repo carries several properties; only the named one decides the freeze."""
        listing = json.dumps(
            [
                {"property_name": "staging_frozen", "value": "false"},
                {"property_name": "other_freeze", "value": "true"},
            ]
        )
        assert freeze_state.is_frozen("o/r", "other_freeze", runner=fake_runner([completed(listing)]))

    def test_undefined_property_raises(self):
        """Absent from the listing means the org does not define it: drift, not thawed."""
        with pytest.raises(freeze_state.LookupError_, match="not defined"):
            freeze_state.is_frozen("o/r", "absent", runner=fake_runner([completed(values("true"))]))

    def test_api_failure_raises(self):
        runner = fake_runner([completed(stderr="HTTP 403", returncode=1)])
        with pytest.raises(freeze_state.LookupError_, match="Could not read custom properties"):
            freeze_state.is_frozen("o/r", "staging_frozen", runner=runner)

    def test_non_json_listing_is_a_lookup_error_not_a_traceback(self):
        runner = fake_runner([completed("<html>502</html>")])
        with pytest.raises(freeze_state.LookupError_, match="was not JSON"):
            freeze_state.is_frozen("o/r", "staging_frozen", runner=runner)


class TestReadMode:
    """The alerting path. It must never fail the job and never under-report."""

    def test_reports_false_when_thawed(self, capsys):
        code = freeze_state.main(["read", "--repo", "o/r"], runner=fake_runner([completed(values("false"))]))
        assert code == 0
        assert "frozen=false" in capsys.readouterr().out

    def test_reads_the_repo_property_values_endpoint(self):
        runner = fake_runner([completed(values("false"))])
        freeze_state.main(["read", "--repo", "o/r"], runner=runner)
        assert runner.calls == [["gh", "api", "repos/o/r/properties/values"]]

    def test_unreadable_property_escalates_to_frozen(self, capsys):
        """A lookup problem must not silence a real release-blocking failure."""
        runner = fake_runner([completed(stderr="HTTP 403", returncode=1)])
        code = freeze_state.main(["read", "--repo", "o/r"], runner=runner)
        out = capsys.readouterr().out
        assert code == 0, "a notify job must never redden a green run"
        assert "frozen=true" in out
        assert "::warning::" in out

    def test_undefined_property_escalates_to_frozen(self, capsys):
        runner = fake_runner([completed(values("false"))])
        code = freeze_state.main(["read", "--repo", "o/r", "--property-name", "renamed"], runner=runner)
        assert code == 0
        assert "frozen=true" in capsys.readouterr().out

    def test_on_error_fail_is_available_for_non_alerting_callers(self):
        runner = fake_runner([completed(stderr="HTTP 403", returncode=1)])
        assert freeze_state.main(["read", "--repo", "o/r", "--on-error", "fail"], runner=runner) == 1

    def test_writes_github_output(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        freeze_state.main(["read", "--repo", "o/r"], runner=fake_runner([completed(values("false"))]))
        assert out.read_text().strip() == "frozen=false"


class TestSetMode:
    """The freeze/unfreeze path. It must write exactly the property and fail loudly."""

    def test_patches_the_property_as_a_string(self, tmp_path):
        body = tmp_path / "prop.json"
        runner = fake_runner([completed("")])
        freeze_state.set_frozen("o/r", "staging_frozen", True, body_path=str(body), runner=runner)
        assert json.loads(body.read_text()) == {
            "properties": [{"property_name": "staging_frozen", "value": "true"}]
        }
        assert runner.calls == [
            ["gh", "api", "--method", "PATCH", "repos/o/r/properties/values", "--input", str(body)]
        ]

    def test_unfreeze_writes_false_not_null(self, tmp_path):
        """Null would read back as thawed too, but leaves no trace that a freeze ever ran."""
        body = tmp_path / "prop.json"
        freeze_state.set_frozen("o/r", "staging_frozen", False, body_path=str(body), runner=fake_runner([completed("")]))
        assert json.loads(body.read_text())["properties"][0]["value"] == "false"

    def test_failed_patch_raises(self, tmp_path):
        runner = fake_runner([completed(stderr="HTTP 422", returncode=1)])
        with pytest.raises(freeze_state.LookupError_, match="Could not set"):
            freeze_state.set_frozen("o/r", "staging_frozen", True, body_path=str(tmp_path / "b.json"), runner=runner)

    def test_an_error_body_on_stdout_still_reaches_the_message(self, tmp_path):
        """`gh` puts the API's error body on stdout and its own noise on stderr, so
        taking stderr alone can print an error with no cause in it."""
        runner = fake_runner(
            [completed(stdout='{"message":"Resource not accessible by integration"}', returncode=1)]
        )
        with pytest.raises(freeze_state.LookupError_, match="not accessible by integration"):
            freeze_state.set_frozen("o/r", "staging_frozen", True, body_path=str(tmp_path / "b.json"), runner=runner)

    def test_failed_set_fails_the_job(self, tmp_path):
        """Never escalate here: a freeze that did not apply must stop the train."""
        code = freeze_state.main(
            ["set", "--repo", "o/r", "--frozen", "true", "--body-path", str(tmp_path / "b.json")],
            runner=fake_runner([completed(stderr="HTTP 404", returncode=1)]),
        )
        assert code == 1

    def test_set_requires_a_value(self, tmp_path):
        with pytest.raises(SystemExit):
            freeze_state.main(["set", "--repo", "o/r"], runner=fake_runner([]))
