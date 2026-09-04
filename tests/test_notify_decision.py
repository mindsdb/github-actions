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
- a red run whose predecessor was also red posts nothing
- an unreadable run history posts an alert and withholds a recovery, never errors
"""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "notify_decision.py"
_spec = importlib.util.spec_from_file_location("notify_decision", _PATH)
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)


def decide(**kwargs):
    """`decide` with the defaults a caller that passes nothing would get.

    The streak clock defaults to the middle of a reminder window: a failing
    streak that began at 12:00, a previous run at 12:30 and now 12:40, all inside
    the first 60-minute window. Deduplication tests therefore read as
    deduplication, and a test that cares about the reminder sets its own clock.
    """
    return notify.decide(
        **{
            "status": "failed",
            "freeze_scoped": "false",
            "frozen": "",
            "force_post": "false",
            "prev_conclusion": "",
            "streak_started_at": "2026-09-04T12:00:00Z",
            "prev_started_at": "2026-09-04T12:30:00Z",
            "now": "2026-09-04T12:40:00Z",
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


class TestTheFirstFailurePosts:
    def test_a_failure_after_a_green_run_posts(self):
        assert decide(status="failed", prev_conclusion="success")["post"] == "true"

    def test_a_failure_posts_while_the_freeze_window_is_open(self):
        assert decide(status="failed", freeze_scoped="true", frozen="true")["post"] == "true"

    def test_the_freeze_veto_silences_a_failure(self):
        assert decide(status="failed", freeze_scoped="true", frozen="false")["post"] == "false"

    @pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped"])
    def test_anything_that_is_not_a_prior_failure_lets_the_alert_through(self, conclusion):
        """`cancelled` and `skipped` are not evidence the channel was already told,
        for the same reason they are not evidence anything was broken."""
        assert decide(status="failed", prev_conclusion=conclusion)["post"] == "true"


class TestRepeatFailuresStayQuiet:
    """The ENG-2324 regression: one standing breakage paged every five minutes.

    The public web probe runs on a `*/5` cron. Production `/cowork` and `/assets/`
    had been returning 301 and 403 since before the probe existed, so every run
    re-reported the same two routes: about 220 Slack messages a day into the
    channel that also carries the paging Cloudflare checks. Nothing suppressed a
    failure except the freeze veto, and a cron probe is never freeze-scoped.
    """

    @pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
    def test_a_failure_whose_predecessor_also_failed_posts_nothing(self, conclusion):
        assert decide(status="failed", prev_conclusion=conclusion)["post"] == "false"

    def test_the_suppressed_alert_still_reads_as_an_alert(self):
        """Only `post` changes. A deduped failure is not a recovery and not a
        silenced freeze, and the log line has to be able to say which it was."""
        assert decide(status="failed", prev_conclusion="failure")["level"] == "alert"

    def test_no_evidence_still_pages(self):
        """Absence of evidence is not evidence the channel was already told. The
        lookup needs `actions: read`, and a caller that forgot the grant must
        still get its first alert rather than silence."""
        assert decide(status="failed", prev_conclusion="")["post"] == "true"

    def test_the_smoke_test_overrides_the_dedupe(self):
        assert decide(status="failed", force_post="true", prev_conclusion="failure")["post"] == "true"

    def test_the_log_line_says_why_it_stayed_quiet(self):
        d = decide(status="failed", prev_conclusion="failure")
        reason = notify.why(d, status="failed", force_post="false", prev_conclusion="failure")
        assert "already been reported" in reason

    def test_a_repeat_still_reports_itself_once_the_reminder_is_due(self):
        """The reason this is a digest and not a mute.

        A conclusion says THAT a run failed, never WHAT failed. Two endpoints
        down and twenty-one endpoints down are both `failure`, so suppressing
        every repeat would let an outage grow behind an alert already sent. The
        reminder bounds how long that can go unmentioned.
        """
        quiet = decide(
            status="failed",
            prev_conclusion="failure",
            streak_started_at="2026-09-04T12:00:00Z",
            prev_started_at="2026-09-04T12:40:00Z",
            now="2026-09-04T12:50:00Z",
            repeat_after_minutes=60,
        )
        due = decide(
            status="failed",
            prev_conclusion="failure",
            streak_started_at="2026-09-04T12:00:00Z",
            prev_started_at="2026-09-04T12:55:00Z",
            now="2026-09-04T13:05:00Z",
            repeat_after_minutes=60,
        )
        assert (quiet["post"], due["post"]) == ("false", "true")

    def test_the_reminder_fires_once_per_window_not_every_run_after_it(self):
        """A five-minute cron crossing the hour must not start paging again."""
        assert decide(
            status="failed",
            prev_conclusion="failure",
            streak_started_at="2026-09-04T12:00:00Z",
            prev_started_at="2026-09-04T13:05:00Z",
            now="2026-09-04T13:10:00Z",
            repeat_after_minutes=60,
        )["post"] == "false"

    @pytest.mark.parametrize(
        "streak_started_at,prev_started_at",
        [("", "2026-09-04T12:40:00Z"), ("2026-09-04T12:00:00Z", ""), ("nonsense", "nonsense")],
    )
    def test_unreadable_streak_timestamps_page_rather_than_stay_quiet(
        self, streak_started_at, prev_started_at
    ):
        """A reminder that fires early costs one message. One that never fires
        hides a growing outage."""
        assert decide(
            status="failed",
            prev_conclusion="failure",
            streak_started_at=streak_started_at,
            prev_started_at=prev_started_at,
            now="2026-09-04T12:50:00Z",
        )["post"] == "true"

    def test_a_nonpositive_interval_cannot_be_used_to_mute_the_channel(self):
        """`repeat-alert-after-minutes: 0` must not mean 'never remind'."""
        assert decide(
            status="failed",
            prev_conclusion="failure",
            streak_started_at="2026-09-04T12:00:00Z",
            prev_started_at="2026-09-04T12:40:00Z",
            now="2026-09-04T12:50:00Z",
            repeat_after_minutes=0,
        )["post"] == "true"

    def test_a_standing_breakage_pages_once_then_recovers_once(self):
        """The whole point, as the sequence the channel actually sees: one red on
        the run that breaks, silence while it stays broken, one green when it is
        fixed. Three messages for a three-day outage, not eight hundred."""
        posts = [
            decide(status="failed", prev_conclusion="success")["post"],
            decide(status="failed", prev_conclusion="failure")["post"],
            decide(status="failed", prev_conclusion="failure")["post"],
            decide(status="recovered", prev_conclusion="failure")["post"],
            decide(status="recovered", prev_conclusion="success")["post"],
        ]
        assert posts == ["true", "false", "false", "true", "false"]


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
