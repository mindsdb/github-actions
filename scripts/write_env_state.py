"""Record a deploy in mindsdb/argocd-state.

Argo CD deploys each long-lived environment from one file per service in that
repository, ``<env>/<service>.yaml`` (its README has the keys). After a build,
CI sets two of them, ``tag`` and ``revision``, and Argo CD does the rest. Every
repository's deploy job does the same thing, so it is written once here and
the ``write-env-state`` action calls it.

The file is edited line by line, not parsed and dumped, so the keys set when
the service was added keep their order and comments. It has to exist already:
adding a service means choosing its chart path and release name, which is a
reviewed change in argocd-state, not something a deploy creates on the way.

Two deploys can land at once, so the push is retried: each attempt starts from
the branch as it is on the remote, applies the edit again and pushes. A push
the remote refuses because the branch moved is the only thing retried.

    write_env_state.py --repo-dir <clone> --env dev --service scratchpad-controller \
        --tag development-head-<sha> --revision <sha>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The same shapes argocd-state's validate.py accepts.
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class StateError(Exception):
    """A reason the deploy cannot be recorded, in words for the job log."""


def check(env: str, service: str, tag: str, revision: str) -> None:
    for name, value, pattern, shape in (
        ("env", env, LABEL, "a namespace name"),
        ("service", service, LABEL, "a service name"),
        ("tag", tag, TAG, "an image tag"),
        ("revision", revision, COMMIT, "a 40-character commit sha"),
    ):
        if not pattern.match(value):
            raise StateError(f"{name} {value!r} is not {shape}")


def updated_text(text: str, tag: str, revision: str) -> str:
    """The state file with its tag and revision lines replaced, everything else as it was."""
    out = []
    seen = set()
    for line in text.splitlines(keepends=True):
        key = line.split(":", 1)[0] if ":" in line else None
        if key in ("tag", "revision") and not line.startswith((" ", "#")):
            seen.add(key)
            value = tag if key == "tag" else revision
            line = f"{key}: {value}\n"
        out.append(line)
    missing = {"tag", "revision"} - seen
    if missing:
        raise StateError(f"the file has no {' or '.join(sorted(missing))} line")
    return "".join(out)


def git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, check=check)


def record(repo_dir: Path, branch: str, env: str, service: str, tag: str, revision: str, attempts: int = 5) -> str:
    """Commit and push the edit; the message says what happened. Raises StateError when it can't."""
    check(env, service, tag, revision)
    path = Path(env) / f"{service}.yaml"
    for attempt in range(1, attempts + 1):
        git(repo_dir, "fetch", "--quiet", "origin", branch)
        git(repo_dir, "reset", "--quiet", "--hard", f"origin/{branch}")
        file = repo_dir / path
        if not file.is_file():
            raise StateError(f"{path} does not exist in argocd-state; adding a service is a reviewed change there, not a deploy")
        text = file.read_text(encoding="utf-8")
        new = updated_text(text, tag, revision)
        if new == text:
            return f"{path} already records {tag} at {revision}"
        file.write_text(new, encoding="utf-8")
        git(repo_dir, "add", str(path))
        git(repo_dir, "commit", "--quiet", "-m", f"{env}/{service}: {tag}")
        push = git(repo_dir, "push", "--quiet", "origin", f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            return f"{path}: tag {tag}, revision {revision} (attempt {attempt})"
        if "rejected" not in push.stderr and "fetch first" not in push.stderr and "non-fast-forward" not in push.stderr:
            raise StateError(f"push to {branch} failed: {push.stderr.strip()}")
    raise StateError(f"{branch} kept moving; gave up after {attempts} attempts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo-dir", type=Path, required=True, help="a clone of argocd-state with origin set")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--env", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        print(record(args.repo_dir, args.branch, args.env, args.service, args.tag, args.revision, args.attempts))
    except StateError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
