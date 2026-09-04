"""Whether an alert posts, and how it reads.

This is the decision the `notify-pipeline-status` composite used to make in two
`if:` expressions and a shell `if/elif/else`. It moved here for the same reason
`freeze_state.py` and `branch_health.py` did: it is the whole behaviour of the
alerting system, it has five inputs and three outcomes, and none of it was
reachable by a test while it lived in YAML.

Splitting it also fixed a real defect. The composite decided "always post, skip
the prior-run check" from ``github.event_name == 'workflow_dispatch'``, which was
written for the smoke-test dispatch of ``notify-main-failure.yml`` itself. A
called workflow inherits the CALLER's context, so that condition was also true
whenever anyone manually ran one of the four release-train wrappers — and every
one of them declares ``workflow_dispatch``. A successful manual "Staging Freeze"
therefore posted a green ``Recovered`` for a failure that had never happened.
The smoke test now says so explicitly with ``--force-post`` instead of being
inferred from an event name that cannot tell the two cases apart.

The three outcomes:

``silenced``
    A freeze-scoped caller while the window is closed. No red, no green, no grey.
    Mid-week staging is the integration branch, and posting only the recovery
    half of a story the channel was never told is worse than posting neither.

``alert``
    A failure whose predecessor was not already failing. The freeze veto above is
    the only other thing that may suppress one. A failure after a failure posts
    nothing: the channel has already been told, and a standing breakage that
    re-pages on every run is how a real alert gets missed. ENG-2324's public web
    probe runs every five minutes, so one unfixed route was worth about 220 Slack
    messages a day until this rule existed.

``recovered``
    A success whose predecessor failed. Posts only on that evidence, or when
    ``--force-post`` says this is the smoke test. A green run after a green run
    posts nothing, which is what keeps the channel worth reading.

Both directions therefore turn on the same evidence, and they fail open in
opposite directions on purpose. No evidence posts an alert and withholds a
recovery, because an unreported failure is the expensive mistake and a duplicate
alert is the cheap one.

``--prev-conclusion`` is the conclusion of the previous conclusive run, or of the
previous attempt of this run. Empty means "no evidence": either the lookup was
never made because nothing was going to post anyway, or it was refused. Refused
is the common one — the lookup needs ``actions: read`` on the calling job — and
it has to read as "not a recovery" rather than as an error, because a notify step
must never turn a green pipeline red.
"""

from __future__ import annotations

import argparse
import os
import sys

# Conclusions a `success` can be a recovery FROM. `cancelled` and `skipped` are
# not evidence that anything was broken, so recovering from them is not news.
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

STYLES: dict[str, dict[str, str]] = {
    "silenced": {"color": "", "icon": "", "verb": "", "prefix": ""},
    "recovered": {"color": "#00C851", "icon": ":white_check_mark:", "verb": "recovered", "prefix": "Recovered"},
    "alert": {"color": "#FF4444", "icon": ":rotating_light:", "verb": "failed", "prefix": "FAILED"},
}


def decide(
    *,
    status: str,
    freeze_scoped: str,
    frozen: str,
    force_post: str,
    prev_conclusion: str,
) -> dict[str, str]:
    """The full decision, as the flat strings the Slack payload interpolates.

    Every argument arrives as a string because that is what a composite action's
    inputs and a step's outputs are. ``frozen`` is ``""`` when the freeze step did
    not run, which is not the same as ``"false"``: only an actually-thawed
    freeze-scoped caller is silenced.
    """
    silenced = freeze_scoped == "true" and frozen == "false"

    if silenced:
        level, post = "silenced", False
    elif status == "recovered":
        level = "recovered"
        post = force_post == "true" or prev_conclusion in FAILED_CONCLUSIONS
    else:
        level = "alert"
        # Only evidence of an already-failing predecessor suppresses an alert. An
        # empty conclusion is absence of evidence, so it still pages.
        post = force_post == "true" or prev_conclusion not in FAILED_CONCLUSIONS

    return {"post": "true" if post else "false", "level": level, **STYLES[level]}


def why(decision: dict[str, str], *, status: str, force_post: str, prev_conclusion: str) -> str:
    """One line for the run log, because a silent no-op is hard to debug."""
    if decision["level"] == "silenced":
        return "Freeze-scoped and the window is closed, so nothing posts in either direction."
    if decision["level"] == "alert":
        if decision["post"] == "true":
            return "Posting a failure."
        return (
            f"Previous run also concluded '{prev_conclusion}', so this breakage has already "
            "been reported. Staying quiet until it recovers."
        )
    if force_post == "true":
        return "Posting a recovery unconditionally (--force-post: this is the smoke test)."
    if decision["post"] == "true":
        return f"Previous run concluded '{prev_conclusion}', so this is a recovery. Posting."
    return (
        f"Previous run concluded '{prev_conclusion or 'unknown'}', which is not a failure, "
        "so this is an ordinary green run. Staying quiet."
    )


def emit(decision: dict[str, str]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in decision.items():
                handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="failed", help="'failed' or 'recovered'")
    parser.add_argument("--freeze-scoped", default="false")
    parser.add_argument(
        "--frozen",
        default="",
        help="'true'/'false' from freeze_state.py, or empty when the freeze was not read",
    )
    parser.add_argument(
        "--force-post",
        default="false",
        help="post without evidence in either direction; set only by the notify workflow's own smoke-test dispatch",
    )
    parser.add_argument(
        "--prev-conclusion",
        default="",
        help="empty means no evidence either way, which posts an alert and withholds a recovery",
    )
    args = parser.parse_args(argv)

    decision = decide(
        status=args.status,
        freeze_scoped=args.freeze_scoped,
        frozen=args.frozen,
        force_post=args.force_post,
        prev_conclusion=args.prev_conclusion,
    )
    emit(decision)
    print(why(decision, status=args.status, force_post=args.force_post, prev_conclusion=args.prev_conclusion))
    print(f"level={decision['level']} post={decision['post']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
