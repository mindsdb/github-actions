"""The one place that knows how a repository's release freeze is stored.

A release freeze is the repository custom property ``staging_frozen`` (a
``true_false`` property defined once at the org level, in terraform). A single
org ruleset, also in terraform, targets ``refs/heads/staging`` in every repo
whose ``staging_frozen`` is ``true`` and carries one ``update`` rule with the
``mindsdb-release-train`` App as a bypass actor. Freezing a repo is therefore
setting its property to ``true``; unfreezing is setting it to ``false``. The
ruleset itself is never touched, which is what lets terraform own it with an
empty plan while the workflows flip repos in and out of it all week.

Four workflows care about that fact and they used to each carry their own copy
of it: the freeze and unfreeze workflows flipped it, and the two alerting
workflows read it to decide whether a red staging branch is worth interrupting
anyone for. Three copies agreed by luck rather than by contract, so the contract
lives here, once, and the workflows call it.

Two modes, because the two callers want opposite things from a failure.

``read`` answers "is this repository frozen right now" for an alerting workflow.
Its ``--on-error escalate`` default reports frozen and exits 0, because an alert
path that cannot establish the state must escalate rather than silently downgrade
a real release-blocking failure, and because a notify job must never turn a green
pipeline red.

``set`` writes the property for the freeze and unfreeze workflows, and fails
loudly. A freeze that could not be applied has to stop the release train rather
than let the window appear to open.

A property that the org does not define is a lookup error in both modes, not a
"false": that is provisioning drift, and the reader escalating on it is what
makes the drift visible instead of silently thawing every repo.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Callable, Sequence

# A `gh api` invocation, injectable so the tests do not need a network or a token.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

DEFAULT_PROPERTY = "staging_frozen"

# GitHub stores a `true_false` property's value as the string "true" / "false",
# and returns null for a repo the property has never been set on.
FROZEN = "true"
THAWED = "false"


class LookupError_(Exception):
    """The property could not be read or written. Carries the message the caller should print."""


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _failure(prefix: str, result: "subprocess.CompletedProcess[str]") -> LookupError_:
    # Both streams, because `gh` reports an API error body on stdout while
    # writing its own diagnostics to stderr, and a release-train-stopping
    # error with no cause in it is the worst kind to be paged about.
    return LookupError_(f"{prefix}: {result.stderr.strip() or result.stdout.strip()}")


def fetch_property(repo: str, name: str, *, runner: Runner = _run) -> str | None:
    """The value of custom property ``name`` on ``repo``: "true", "false" or None.

    Raises ``LookupError_`` when the API call fails or the org defines no such
    property. The two are distinct messages on purpose: "the token cannot read
    properties" and "provisioning has drifted" get fixed by different people.
    """
    listing = runner(["gh", "api", f"repos/{repo}/properties/values"])
    if listing.returncode != 0:
        raise _failure("Could not read custom properties", listing)

    try:
        values = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise LookupError_(f"Custom property listing was not JSON: {exc}") from exc

    for entry in values:
        if entry.get("property_name") == name:
            value = entry.get("value")
            # Tolerate a real boolean in case the API ever stops stringifying.
            if isinstance(value, bool):
                return FROZEN if value else THAWED
            return value
    raise LookupError_(f"Custom property '{name}' is not defined for {repo}")


def is_frozen(repo: str, name: str, *, runner: Runner = _run) -> bool:
    """True when the property is "true", i.e. the org freeze ruleset applies here."""
    return fetch_property(repo, name, runner=runner) == FROZEN


def set_frozen(
    repo: str,
    name: str,
    frozen: bool,
    *,
    body_path: str,
    runner: Runner = _run,
) -> str:
    """Write the property. Returns the value written so the caller can log it."""
    value = FROZEN if frozen else THAWED
    payload = {"properties": [{"property_name": name, "value": value}]}
    with open(body_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    patch = runner(
        ["gh", "api", "--method", "PATCH", f"repos/{repo}/properties/values", "--input", body_path]
    )
    if patch.returncode != 0:
        raise _failure(f"Could not set custom property '{name}'", patch)
    return value


def emit(key: str, value: str) -> None:
    """Write a step output, and echo it so the run log shows the decision."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    print(f"{key}={value}")


def main(argv: list[str] | None = None, *, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["read", "set"])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--property-name", default=DEFAULT_PROPERTY)
    parser.add_argument(
        "--frozen",
        choices=[FROZEN, THAWED],
        help="set mode only: the value to write",
    )
    parser.add_argument(
        "--on-error",
        choices=["escalate", "fail"],
        default="escalate",
        help="read mode only: what an unreadable property means",
    )
    parser.add_argument("--body-path", default=None, help="set mode only: where to stage the PATCH body")
    args = parser.parse_args(argv)

    if args.mode == "read":
        try:
            frozen = is_frozen(args.repo, args.property_name, runner=runner)
        except LookupError_ as exc:
            if args.on_error == "fail":
                # No ::error:: annotation: every caller of this mode catches the
                # non-zero exit and degrades deliberately (release-pr.yml leaves
                # the PR a draft), so annotating would mark a run red that the
                # workflow treats as handled. The caller emits its own ::warning::.
                print(f"Could not establish the freeze state: {exc}", file=sys.stderr)
                return 1
            # Escalating is the safe direction: treating an unknown state as
            # frozen costs one unnecessary alert, treating it as thawed costs
            # the release-blocking alert this exists to send.
            print(f"::warning::{exc}. Treating the branch as frozen so failures still escalate.")
            emit("frozen", "true")
            return 0
        emit("frozen", "true" if frozen else "false")
        return 0

    if not args.frozen:
        parser.error("set mode requires --frozen")
    body_path = args.body_path or os.path.join(os.environ.get("RUNNER_TEMP", "."), "freeze-property.json")
    try:
        value = set_frozen(
            args.repo, args.property_name, args.frozen == FROZEN, body_path=body_path, runner=runner
        )
    except LookupError_ as exc:
        print(f"::error::{exc} — provisioning has drifted.", file=sys.stderr)
        return 1
    print(f"Custom property '{args.property_name}' set to {value} on {args.repo}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
