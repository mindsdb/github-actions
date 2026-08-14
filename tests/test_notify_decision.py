"""Unit tests for the alert decision (``scripts/notify_decision.py``).

This logic lived in two `if:` expressions and a shell `if/elif/else` inside
`notify-pipeline-status/action.yml`, where nothing could reach it. The defect that
survived there is the one pinned first below: a manual run of any release-train
wrapper posted a green ``Recovered`` for a failure that had never happened,
because the composite inferred "this is the smoke test" from
``github.event_name == 'workflow_dispatch'`` and a called workflow inherits the
CALLER's event.

The policy these pin, in the order it is easy to get wrong:

- staging outside the freeze window is silent in BOTH directions
- a green run whose predecessor was also green posts nothing
- a failure always posts, and the freeze veto is the only thing that may stop it
- an unreadable run history reads as "not a recovery", never as an error
"""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "notify_decision.py"
_spec = importlib.util.spec_from_file_location("notify_decision", _PATH)
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)


def decide(**kwargs):
    """`decide` with the defaults a caller that passes nothing would get."""
    return notify.decide(
        **{
            "status": "failed",
            "freeze_scoped": "false",
            "frozen": "",
            "force_post": "false",
            "prev_conclusion": "",
            **kwargs,
        }
    )


class TestTheDispatchEscapeHatch:
    """The defect this split exists to fix."""

    def test_a_green_run_does_not_post_just_because_a_human_started_it(self):
        """The regression: a manual "Staging Freeze" reported a recovery from nothing.

        The four release-train reusables pass no `force-post`, and all their
        wrappers declare `workflow_dispatch`. Nothing about the event may reach
        this decision.
        """
        assert decide(status="recovered", prev_conclusion="success")["post"] == "false"

    def test_the_smoke_test_still_posts_on_demand(self):
        """`force-post` is how the notify workflow's own dispatch says so."""
        assert decide(status="recovered", force_post="true")["post"] == "true"

    def test_force_post_needs_no_evidence_at_all(self):
        assert decide(status="recovered", force_post="true", prev_conclusion="")["post"] == "true"

    def test_force_post_does_not_override_the_freeze_veto(self):
        """Silence outside the window is the stronger rule; a smoke test is not an
        excuse to page the channel about a thawed staging branch."""
        d = decide(status="recovered", force_post="true", freeze_scoped="true", frozen="false")
        assert d["post"] == "false"
        assert d["level"] == "silenced"


class TestRecoveryNeedsAPriorFailure:
    @pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
    def test_posts_when_the_previous_run_failed(self, conclusion):
        assert decide(status="recovered", prev_conclusion=conclusion)["post"] == "true"

    @pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", ""])
    def test_stays_quiet_otherwise(self, conclusion):
        """`cancelled` and `skipped` are not evidence anything was broken, and an
        empty conclusion means the lookup was refused or never made."""
        assert decide(status="recovered", prev_conclusion=conclusion)["post"] == "false"

    def test_an_unreadable_history_is_not_a_recovery_and_not_an_error(self):
        """A missing `actions: read` grant must degrade to silence. A notify step
        that fails here would turn a green pipeline red."""
        assert decide(status="recovered", prev_conclusion="")["post"] == "false"


class TestFailuresAlwaysPost:
    def test_a_failure_posts_with_no_history_lookup_at_all(self):
        assert decide(status="failed")["post"] == "true"

    def test_a_failure_posts_while_the_freeze_window_is_open(self):
        assert decide(status="failed", freeze_scoped="true", frozen="true")["post"] == "true"

    def test_the_freeze_veto_is_the_only_thing_that_silences_a_failure(self):
        assert decide(status="failed", freeze_scoped="true", frozen="false")["post"] == "false"


class TestFreezeScoping:
    def test_silent_in_both_directions_while_thawed(self):
        """The whole policy: half a story is worse than none."""
        assert decide(status="failed", freeze_scoped="true", frozen="false")["post"] == "false"
        assert decide(status="recovered", freeze_scoped="true", frozen="false",
                      prev_conclusion="failure")["post"] == "false"

    def test_both_directions_post_while_frozen(self):
        assert decide(status="failed", freeze_scoped="true", frozen="true")["post"] == "true"
        assert decide(status="recovered", freeze_scoped="true", frozen="true",
                      prev_conclusion="failure")["post"] == "true"

    def test_an_unscoped_caller_is_never_silenced_by_an_empty_frozen(self):
        """`frozen` is '' when the freeze step did not run, which is NOT 'false'.
        Collapsing the two would silence every prod and main caller."""
        assert decide(status="failed", freeze_scoped="false", frozen="")["post"] == "true"

    def test_an_unestablished_freeze_state_escalates(self):
        """freeze_state.py reports frozen=true when it cannot read the ruleset, so
        the failure still escalates. Being wrong this way costs one alert."""
        assert decide(status="failed", freeze_scoped="true", frozen="true")["post"] == "true"


class TestStyling:
    def test_a_failure_is_red_and_shouts(self):
        d = decide(status="failed")
        assert (d["level"], d["color"], d["prefix"], d["verb"]) == ("alert", "#FF4444", "FAILED", "failed")

    def test_a_recovery_is_green(self):
        d = decide(status="recovered", prev_conclusion="failure")
        assert (d["level"], d["color"], d["prefix"], d["verb"]) == (
            "recovered", "#00C851", "Recovered", "recovered",
        )

    def test_a_silenced_decision_carries_no_styling_to_leak(self):
        d = decide(status="failed", freeze_scoped="true", frozen="false")
        assert (d["color"], d["icon"], d["verb"], d["prefix"]) == ("", "", "", "")

    def test_every_outcome_emits_the_same_keys(self):
        """The Slack payload interpolates all of them, so a missing one renders as
        an empty string in the message rather than failing loudly."""
        expected = {"post", "level", "color", "icon", "verb", "prefix"}
        assert set(decide(status="failed")) == expected
        assert set(decide(status="recovered")) == expected
        assert set(decide(status="failed", freeze_scoped="true", frozen="false")) == expected


class TestMainWritesTheStepOutputs:
    def test_writes_every_key_to_github_output(self, tmp_path, monkeypatch):
        out = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        assert notify.main(["--status", "failed"]) == 0
        written = dict(line.split("=", 1) for line in out.read_text().strip().splitlines())
        assert written["post"] == "true"
        assert written["level"] == "alert"
        assert written["prefix"] == "FAILED"

    def test_survives_no_github_output(self):
        """Runs locally and in `act` without a step-output file."""
        assert notify.main(["--status", "recovered"]) == 0

    def test_explains_itself_in_the_log(self, capsys):
        notify.main(["--status", "recovered", "--prev-conclusion", "success"])
        assert "ordinary green run" in capsys.readouterr().out

    def test_the_defaults_match_the_composite_action_inputs(self):
        """`notify-pipeline-status/action.yml` defaults status to `failed`,
        freeze-scoped and force-post to "false", and passes an empty
        prev-conclusion when the lookup step was skipped."""
        assert notify.main([]) == 0
        assert decide()["level"] == "alert"
