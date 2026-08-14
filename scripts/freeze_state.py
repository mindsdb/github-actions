"""The one place that knows how a repository's release freeze is stored.

A release freeze is a pre-provisioned repository ruleset (by default named
``staging-freeze``) whose ``enforcement`` field is flipped between ``active`` and
``disabled``. Four workflows care about that fact and they used to each carry
their own copy of it: the freeze and unfreeze workflows flipped it, and the two
alerting workflows read it to decide whether a red staging branch is worth
interrupting anyone for.

Three copies agreed by luck rather than by contract. Only two of them took the
ruleset name as an input, so renaming the ruleset in one repository moved the
freeze and left the alerting reading a name that no longer existed. That failure
is silent in the direction that hurts: the reader escalates when it cannot
establish the state, so every ordinary mid-week staging red would have paged the
channel forever and the cause would have looked like a Slack problem.

So the contract lives here, once, and the workflows call it.

Two modes, because the two callers want opposite things from a failure.

``read`` answers "is this repository frozen right now" for an alerting workflow.
Its ``--on-error escalate`` default reports frozen and exits 0, because an alert
path that cannot establish the state must escalate rather than silently downgrade
a real release-blocking failure, and because a notify job must never turn a green
pipeline red.

``set`` flips enforcement for the freeze and unfreeze workflows, and fails loudly.
A freeze that could not be applied has to stop the release train rather than let
the window appear to open.

The flip is read-modify-write against the whole ruleset. A partial ``PUT`` is not
guaranteed to preserve the fields it omits, and the fields being omitted here are
the bypass actors and the branch conditions, so getting that wrong unlocks the
branch it was asked to lock. The body is written to a file and never echoed:
three of the consuming repositories are public, and a ruleset body names its
bypass actors.
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

ENFORCEMENT_ACTIVE = "active"
ENFORCEMENT_DISABLED = "disabled"


class LookupError_(Exception):
    """The ruleset could not be read. Carries the message the caller should print."""


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def fetch_ruleset(repo: str, name: str, *, runner: Runner = _run) -> dict:
    """The ruleset named ``name`` in ``repo``, as a dict.

    Raises ``LookupError_`` when the API call fails or no ruleset carries that
    name. The two are distinct messages on purpose: "the token cannot read
    rulesets" and "provisioning has drifted" get fixed by different people.
    """
    listing = runner(["gh", "api", f"repos/{repo}/rulesets"])
    if listing.returncode != 0:
        raise LookupError_(f"Could not read rulesets: {listing.stderr.strip() or listing.stdout.strip()}")

    try:
        rulesets = json.loads(listing.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise LookupError_(f"Ruleset listing was not JSON: {exc}") from exc

    for ruleset in rulesets:
        if ruleset.get("name") == name:
            return ruleset
    raise LookupError_(f"Ruleset '{name}' not found in {repo}")


def is_frozen(repo: str, name: str, *, runner: Runner = _run) -> bool:
    """True when the freeze ruleset is enforced, i.e. the branch is locked."""
    return fetch_ruleset(repo, name, runner=runner).get("enforcement") == ENFORCEMENT_ACTIVE


def set_enforcement(
    repo: str,
    name: str,
    enforcement: str,
    *,
    body_path: str,
    runner: Runner = _run,
) -> int:
    """Flip the ruleset's enforcement, preserving every other field.

    Returns the ruleset id so the caller can name it in its log line.
    """
    ruleset = fetch_ruleset(repo, name, runner=runner)
    ruleset_id = ruleset["id"]

    # Re-read the full ruleset rather than reusing the listing entry: the list
    # endpoint returns a summary that omits `rules` and `bypass_actors`, and
    # PUTting that summary back would drop them.
    detail = runner(["gh", "api", f"repos/{repo}/rulesets/{ruleset_id}"])
    if detail.returncode != 0:
        raise LookupError_(f"Could not read ruleset {ruleset_id}: {detail.stderr.strip()}")

    full = json.loads(detail.stdout)
    payload = {
        "name": full["name"],
        "target": full["target"],
        "enforcement": enforcement,
        "bypass_actors": full.get("bypass_actors", []),
        "conditions": full.get("conditions", {}),
        "rules": full.get("rules", []),
    }
    with open(body_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    put = runner(
        ["gh", "api", "--method", "PUT", f"repos/{repo}/rulesets/{ruleset_id}", "--input", body_path]
    )
    if put.returncode != 0:
        raise LookupError_(f"Could not update ruleset {ruleset_id}: {put.stderr.strip()}")
    return ruleset_id


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
    parser.add_argument("--ruleset-name", default="staging-freeze")
    parser.add_argument(
        "--enforcement",
        choices=[ENFORCEMENT_ACTIVE, ENFORCEMENT_DISABLED],
        help="set mode only: the enforcement to write",
    )
    parser.add_argument(
        "--on-error",
        choices=["escalate", "fail"],
        default="escalate",
        help="read mode only: what an unreadable ruleset means",
    )
    parser.add_argument("--body-path", default=None, help="set mode only: where to stage the PUT body")
    args = parser.parse_args(argv)

    if args.mode == "read":
        try:
            frozen = is_frozen(args.repo, args.ruleset_name, runner=runner)
        except LookupError_ as exc:
            if args.on_error == "fail":
                print(f"::error::{exc}", file=sys.stderr)
                return 1
            # Escalating is the safe direction: treating an unknown state as
            # frozen costs one unnecessary alert, treating it as thawed costs
            # the release-blocking alert this exists to send.
            print(f"::warning::{exc}. Treating the branch as frozen so failures still escalate.")
            emit("frozen", "true")
            return 0
        emit("frozen", "true" if frozen else "false")
        return 0

    if not args.enforcement:
        parser.error("set mode requires --enforcement")
    body_path = args.body_path or os.path.join(os.environ.get("RUNNER_TEMP", "."), "ruleset.json")
    try:
        ruleset_id = set_enforcement(
            args.repo, args.ruleset_name, args.enforcement, body_path=body_path, runner=runner
        )
    except LookupError_ as exc:
        print(f"::error::{exc} — provisioning has drifted.", file=sys.stderr)
        return 1
    print(f"Ruleset '{args.ruleset_name}' (#{ruleset_id}) enforcement set to {args.enforcement}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
