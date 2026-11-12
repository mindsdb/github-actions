"""Record a deploy in mindsdb/argocd-state.

Argo CD deploys each long-lived environment from one file per service in that
repository, ``<env>/<service>.yaml`` (its README has the keys). After a build,
CI sets two of them from the PR's head commit: ``revision`` is that sha and
``tag`` is the ``<build environment>-head-<sha>`` tag build-push-ecr pushes
for it, so one value names both. Every repository's deploy job does the same
thing, so it is written once here and the ``write-env-state`` action calls it.

The file is edited line by line, not parsed and dumped, so the keys set when
the service was added keep their order and comments. It has to exist already:
adding a service means choosing its chart path and release name, which is a
reviewed change in argocd-state, not something a deploy creates on the way.

Two deploys can land at once, so the push is retried: each attempt starts from
the branch as it is on the remote, applies the edit again and pushes. A push
the remote refuses because the branch moved is the only thing retried.

    write_env_state.py --repo-dir <clone> --env dev --service scratchpad-controller --sha <head sha>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The same shapes argocd-state's validate.py accepts.
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# build-push-ecr's build-for-environment for each environment's namespace.
BUILD_ENVIRONMENT = {"dev": "development", "staging": "staging", "prod": "production"}


class StateError(Exception):
    """A reason the deploy cannot be recorded, in words for the job log."""


def check(env: str, service: str, sha: str) -> None:
    for name, value, pattern, shape in (
        ("env", env, LABEL, "a namespace name"),
        ("service", service, LABEL, "a service name"),
        ("sha", sha, COMMIT, "a 40-character commit sha"),
    ):
        if not pattern.match(value):
            raise StateError(f"{name} {value!r} is not {shape}")
    if env not in BUILD_ENVIRONMENT:
        raise StateError(f"env {env!r} has no build environment; known: {', '.join(BUILD_ENVIRONMENT)}")


def image_tag(env: str, sha: str) -> str:
    """The tag build-push-ecr gave this commit's image on its pull_request build."""
    return f"{BUILD_ENVIRONMENT[env]}-head-{sha}"


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


def record(repo_dir: Path, branch: str, env: str, service: str, sha: str, attempts: int = 5) -> str:
    """Commit and push the edit; the message says what happened. Raises StateError when it can't."""
    check(env, service, sha)
    tag, revision = image_tag(env, sha), sha
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
    parser.add_argument("--sha", required=True, help="the PR's head commit: the chart revision, and the image tag's suffix")
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        print(record(args.repo_dir, args.branch, args.env, args.service, args.sha, args.attempts))
    except StateError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
