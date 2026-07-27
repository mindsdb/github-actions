"""Two static checks on a repo's workflow graph that no existing tool performs.

CHECK 1 — one run tree per event.

A push or a pull request should produce ONE run whose tree shows everything that
happened, not several disconnected checks a reviewer has to find and correlate.
Two entry-point workflows on the same event means two run trees, and the second
one is the one nobody looks at: `auto-release.yml` and `prod-build-deploy.yml`
both fired on push to `main` for months, so every release was two runs, and the
tag could be cut for a commit whose deploy had been cancelled.

Nothing warns about this. GitHub is perfectly happy to run ten workflows on one
push, and each looks healthy in isolation. So: workflows sharing an event key are
an error, and the fix is to fold the side one in as a `workflow_call` job.

Schedules and `workflow_dispatch` are exempt — they have no pipeline to belong to,
which is the whole reason a nightly drift check or a manual smoke test is allowed
its own run.

It also reports reusables nothing calls, as notes rather than errors, since a
library repo's reusables are called from other repos by design
(`--allow-external-reusables` silences that).

CHECK 2 — a called workflow may never declare a permission its caller lacks.

GitHub caps a called workflow's token at the calling job's grant, and it checks
that cap when the workflow FILE IS LOADED, not when a job runs. So a job in a
reusable workflow that names a scope one of its callers does not grant does not
quietly run with less: it rejects that caller's entire run as a
``startup_failure``, before anything is scheduled.

That failure mode is uniquely bad. The run has zero jobs, so the pipeline's own
terminal notify job cannot fire, and nothing anywhere says the push did not
deploy. It has already cost a consuming repo two undeployed merges to its deploy
branch, where the first thing the engineering channel heard about it was the
*recovery* message from the run that fixed it.

No existing tool catches it, which is the only reason this file exists. Verified
against the failing tree: ``actionlint`` reports nothing (it does not resolve the
caller/callee permission relationship at all), and ``zizmor --persona=auditor``
reports only its generic ``excessive-permissions`` note about a job with no
``permissions:`` block, which is equally true of jobs that work fine. Both run
alongside this check rather than being replaced by it.

The other half of why it goes unnoticed: a pull request only ever exercises the
PR caller, and the PR caller is usually the one that DOES grant the scope. So the
change merges green and breaks on the merge commit.

The rule this enforces, for every ``uses: ./.github/workflows/*.yml`` call:

    every permission declared anywhere in the callee's local subgraph must be
    within the calling job's effective grant

which in practice means a job in a shared reusable declares only what ALL its
callers grant. Anything only ONE caller needs does not belong in a shared reusable
at all: move that job into the caller that has the grant.

Do not reach for "just inherit it" — that was tried and it is a trap. A job with no
``permissions:`` block declares nothing for this check to compare, so it is
invisible here, and a caller that forgot the grant fails at RUN time with an opaque
``HTTP 403`` on its first API call rather than failing the lint. Declaring the scope
is what makes the relationship checkable at all.

The blind spot that remains: this check can see what a job DECLARES, never what a
job NEEDS. A job that quietly requires a scope nobody granted is not detectable
statically, which is the second reason to declare rather than inherit.

Remote callees (``uses: org/repo/.github/workflows/x.yml@ref``) cannot be read
from here and are reported as unchecked rather than assumed fine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# Ordered, so "is this grant enough" is a comparison rather than a table of cases.
LEVELS: dict[str, int] = {"none": 0, "read": 1, "write": 2}

# Every scope GitHub accepts in a `permissions:` block. Named explicitly so a
# typo in a workflow is a finding here rather than a scope that silently never
# applies.
SCOPES: frozenset[str] = frozenset(
    {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
)

# What the workflow token carries when nothing declares anything. This is the
# repo's "Read repository contents and packages permissions" setting; confirm with
#   gh api repos/<owner>/<repo>/actions/permissions/workflow
DEFAULT_GRANTS: dict[str, dict[str, str]] = {
    "read": {"contents": "read", "packages": "read"},
    "write": {scope: "write" for scope in SCOPES},
}

LOCAL_PREFIX = "./.github/workflows/"


class Violation:
    """One caller/callee pair whose permissions cannot compose."""

    def __init__(
        self,
        *,
        caller: str,
        caller_job: str,
        callee: str,
        callee_job: str | None,
        scope: str,
        declared: str,
        granted: str,
        path: tuple[str, ...],
    ) -> None:
        self.caller = caller
        self.caller_job = caller_job
        self.callee = callee
        self.callee_job = callee_job
        self.scope = scope
        self.declared = declared
        self.granted = granted
        self.path = path

    def __str__(self) -> str:
        where = f"{self.callee} job `{self.callee_job}`" if self.callee_job else f"{self.callee} (workflow level)"
        chain = " -> ".join(self.path)
        return (
            f"{self.caller} job `{self.caller_job}` grants {self.scope}: {self.granted}, "
            f"but {where} declares {self.scope}: {self.declared}\n"
            f"    call chain: {chain}\n"
            f"    every run of {self.caller} would be rejected as a startup_failure, with no job to report it.\n"
            f"    fix: grant {self.scope}: {self.declared} on `{self.caller_job}` if EVERY caller should hold it, "
            f"or move that callee job into the one caller that needs it. Do not just delete the callee's "
            f"`permissions:` block: it silences this check without giving the job the scope, and the failure "
            f"comes back at run time as an opaque 403."
        )


class UncheckedCall:
    """A remote callee, recorded so the report never implies more coverage than it has."""

    def __init__(self, *, caller: str, caller_job: str, uses: str) -> None:
        self.caller = caller
        self.caller_job = caller_job
        self.uses = uses

    def __str__(self) -> str:
        return f"{self.caller} job `{self.caller_job}` calls {self.uses} (remote, not readable from here)"


def triggers(workflow: dict) -> dict:
    """The `on:` block, which YAML 1.1 parses as the boolean key ``True``.

    `on` is a YAML boolean, so ``yaml.safe_load("on: push")`` yields ``{True:
    'push'}``. Reading `"on"` alone silently finds nothing, which here would make
    every workflow look like a reusable and skip the whole check.
    """
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(event): None for event in raw}
    return {}


def is_entry_point(workflow: dict) -> bool:
    """True when something other than another workflow can start this one.

    Only an entry point's own `permissions:` decide a grant. A `workflow_call`-only
    workflow has no grant of its own — it runs with whatever its caller allowed —
    so treating one as a root would compare its callees against a repo default
    that never applies, and report violations that cannot happen.
    """
    return any(event != "workflow_call" for event in triggers(workflow))


def normalize(block: object) -> dict[str, str] | None:
    """Turn a `permissions:` value into {scope: level}, or None when absent.

    Handles the two shorthands (`read-all`, `write-all`) and the explicit
    all-none form (`permissions: {}`), which is a real and different thing from
    declaring nothing at all: `{}` overrides an inherited grant down to nothing.
    """
    if block is None:
        return None
    if isinstance(block, str):
        if block == "read-all":
            return {scope: "read" for scope in SCOPES}
        if block == "write-all":
            return {scope: "write" for scope in SCOPES}
        raise ValueError(f"unknown permissions shorthand: {block!r}")
    if isinstance(block, dict):
        return {str(scope): str(level) for scope, level in block.items()}
    raise ValueError(f"unreadable permissions block: {block!r}")


def level_of(grants: dict[str, str], scope: str) -> str:
    return grants.get(scope, "none")


# A workflow that must keep its own run tree says so, with a reason, the same way
# the secret-hygiene rule is opted out of. Written into the file rather than a
# central list, because the reason belongs next to the thing it excuses.
RUN_TREE_EXEMPT = "run-tree-ok:"

EXEMPT: set[str] = set()


def load_workflows(workflow_dir: Path) -> dict[str, dict]:
    """Parse every workflow, keyed by the `./.github/workflows/x.yml` form callers use."""
    workflows: dict[str, dict] = {}
    EXEMPT.clear()
    for path in sorted(workflow_dir.glob("*.y*ml")):
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            key = f"{LOCAL_PREFIX}{path.name}"
            workflows[key] = parsed
            if RUN_TREE_EXEMPT in raw:
                EXEMPT.add(key)
    return workflows


def declared_permissions(workflow: dict) -> list[tuple[str | None, dict[str, str]]]:
    """Every permission block in one workflow: (job name or None for workflow level, grants)."""
    blocks: list[tuple[str | None, dict[str, str]]] = []
    top = normalize(workflow.get("permissions"))
    if top is not None:
        blocks.append((None, top))
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        job_block = normalize(job.get("permissions"))
        if job_block is not None:
            blocks.append((str(job_name), job_block))
    return blocks


def local_calls(workflow: dict) -> list[tuple[str, str]]:
    """(job name, callee key) for each job calling a local reusable workflow."""
    calls: list[tuple[str, str]] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith(LOCAL_PREFIX):
            calls.append((str(job_name), uses))
    return calls


def check(workflows: dict[str, dict], default_grants: dict[str, str]) -> tuple[list[Violation], list[UncheckedCall]]:
    violations: list[Violation] = []
    unchecked: list[UncheckedCall] = []

    for caller_key, caller in workflows.items():
        # Roots only. A reusable workflow's callees are checked through the walk
        # below, against the grant of whichever entry point reached them.
        if not is_entry_point(caller):
            continue
        caller_top = normalize(caller.get("permissions"))
        for job_name, job in (caller.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if not isinstance(uses, str):
                continue
            if not uses.startswith(LOCAL_PREFIX):
                unchecked.append(UncheckedCall(caller=caller_key, caller_job=str(job_name), uses=uses))
                continue

            # The calling job's effective grant: its own block, else the
            # workflow's, else whatever the repo hands an undeclared token.
            grant = normalize(job.get("permissions"))
            if grant is None:
                grant = caller_top if caller_top is not None else dict(default_grants)

            # The cap flows all the way down, so walk the whole local subgraph
            # rather than the immediate callee alone.
            violations.extend(
                _walk(
                    workflows=workflows,
                    caller_key=caller_key,
                    caller_job=str(job_name),
                    grant=grant,
                    callee_key=uses,
                    path=(caller_key, uses),
                    seen={caller_key},
                )
            )

    return violations, unchecked


def _walk(
    *,
    workflows: dict[str, dict],
    caller_key: str,
    caller_job: str,
    grant: dict[str, str],
    callee_key: str,
    path: tuple[str, ...],
    seen: set[str],
) -> list[Violation]:
    if callee_key in seen:  # a cycle would not load on GitHub either; do not hang on it
        return []
    callee = workflows.get(callee_key)
    if callee is None:
        return []

    found: list[Violation] = []
    for callee_job, declared in declared_permissions(callee):
        for scope, level in declared.items():
            if scope not in SCOPES:
                found.append(
                    Violation(
                        caller=caller_key,
                        caller_job=caller_job,
                        callee=callee_key,
                        callee_job=callee_job,
                        scope=scope,
                        declared=str(level),
                        granted="not a permission scope",
                        path=path,
                    )
                )
                continue
            if LEVELS.get(str(level), 0) > LEVELS.get(level_of(grant, scope), 0):
                found.append(
                    Violation(
                        caller=caller_key,
                        caller_job=caller_job,
                        callee=callee_key,
                        callee_job=callee_job,
                        scope=scope,
                        declared=str(level),
                        granted=level_of(grant, scope),
                        path=path,
                    )
                )

    for _, nested in local_calls(callee):
        found.extend(
            _walk(
                workflows=workflows,
                caller_key=caller_key,
                caller_job=caller_job,
                grant=grant,
                callee_key=nested,
                path=path + (nested,),
                seen=seen | {callee_key},
            )
        )
    return found


def event_keys(workflow: dict) -> list[str]:
    """The events that can start this workflow, keyed finely enough to spot a clash.

    `push` is keyed per branch, because a workflow on `push: staging` and one on
    `push: main` do not collide. Everything else keys on the event name alone.

    `schedule`, `workflow_dispatch`, and `workflow_call` are deliberately absent: a
    schedule or a manual run has no pipeline to belong to, and a `workflow_call` is
    not an entry point at all.
    """
    keys: list[str] = []
    for event, config in triggers(workflow).items():
        if event in ("schedule", "workflow_dispatch", "workflow_call"):
            continue
        branches = None
        if isinstance(config, dict):
            branches = config.get("branches")
        if isinstance(branches, list) and branches:
            keys.extend(f"{event}:{branch}" for branch in branches)
        else:
            keys.append(str(event))
    return keys


def check_run_trees(
    workflows: dict[str, dict], *, allow_external_reusables: bool = False
) -> tuple[list[str], list[str]]:
    """Find events that produce more than one run, and reusables nothing calls.

    Returns (errors, notes).
    """
    errors: list[str] = []
    notes: list[str] = []

    by_event: dict[str, list[str]] = {}
    for key, workflow in workflows.items():
        if not is_entry_point(workflow) or key in EXEMPT:
            continue
        for event in event_keys(workflow):
            by_event.setdefault(event, []).append(key)

    for event, owners in sorted(by_event.items()):
        if len(owners) > 1:
            names = ", ".join(sorted(owners))
            errors.append(
                f"`{event}` starts {len(owners)} workflows, so it produces {len(owners)} "
                f"disconnected run trees: {names}\n"
                f"    Fold the side ones into the event's pipeline as `workflow_call` jobs, so one run "
                f"shows everything that happened. A job that should not pay for itself on every run "
                f"gates on a path check; a job behind an `environment:` approval needs an ungated plan "
                f"job before it, since an approval blocks a job from starting.\n"
                f"    If a workflow genuinely has to keep its own run tree (a vendor OIDC claim bound to its "
                f"filename, a release-train hook on someone else's event), add a `run-tree-ok: <reason>` "
                f"comment to it."
            )

    if not allow_external_reusables:
        called = {callee for workflow in workflows.values() for _, callee in local_calls(workflow)}
        for key, workflow in workflows.items():
            if is_entry_point(workflow) or key in called:
                continue
            if "workflow_call" in triggers(workflow):
                notes.append(f"{key} is a reusable that nothing in this repo calls")

    return errors, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-dir",
        type=Path,
        # Relative to the working directory, because this runs against whichever
        # repo checked itself out beside this script, not against this one.
        default=Path(".github/workflows"),
    )
    parser.add_argument(
        "--default-permissions",
        choices=sorted(DEFAULT_GRANTS),
        default="read",
        help="what an undeclared workflow token carries in this repo (Settings -> Actions)",
    )
    parser.add_argument(
        "--allow-external-reusables",
        action="store_true",
        help="this repo's reusables are called from other repos, so do not report them as uncalled",
    )
    args = parser.parse_args(argv)

    workflows = load_workflows(args.workflow_dir)
    if not workflows:
        print(f"No workflows found under {args.workflow_dir}", file=sys.stderr)
        return 1

    violations, unchecked = check(workflows, DEFAULT_GRANTS[args.default_permissions])
    tree_errors, tree_notes = check_run_trees(
        workflows, allow_external_reusables=args.allow_external_reusables
    )

    for call in unchecked:
        print(f"note: {call}")
    for note in tree_notes:
        print(f"note: {note}")

    if tree_errors:
        print(f"\n{len(tree_errors)} event(s) produce more than one run tree:\n", file=sys.stderr)
        for error in tree_errors:
            print(f"  {error}\n", file=sys.stderr)

    if violations:
        print(f"\n{len(violations)} permission mismatch(es) would fail a run before it starts:\n", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}\n", file=sys.stderr)

    if tree_errors or violations:
        return 1

    print(
        f"\nChecked {len(workflows)} workflow(s): every local reusable call composes, "
        f"and every event produces exactly one run tree."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
