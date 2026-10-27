"""Third-party actions inside the build and deploy composites are pinned to commits.

These composites run next to the caller's AWS, cluster or ArgoCD credentials,
and some of their callers are public repositories. `setup-env` is here because
it runs in the same build job as `build-push-ecr`, and any step in a job that
grants `id-token: write` can request that job's OIDC token. A tag is mutable,
so a compromised upstream account could change what runs there without any
change here. A full commit SHA cannot move, and the `# vX.Y.Z` comment says
which release it is. In-org actions are exempt: they float on `main` by
decision, behind this repo's code-owner review (see `zizmor.yml`).

The Snyk CLI that `snyk-docker-scan` installs is pinned to a release for the
same reason: it runs with the job's AWS credentials in its environment. So is
the BuildKit image that `build-push-ecr` builds on in `local` mode, which holds
the ECR registry credentials while it pushes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"\s+#\s+v\d+\.\d+\.\d+\s*$")


@pytest.mark.parametrize(
    "action", ["build-push-ecr", "snyk-docker-scan", "helm-deploy", "argocd-pr-env-deploy", "setup-env"]
)
def test_every_third_party_action_is_pinned_to_a_commit_with_its_version(action):
    lines = (ROOT / action / "action.yml").read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        match = USES.match(line)
        if not match or match["ref"].startswith(("./", "mindsdb/")):
            continue
        assert PINNED.fullmatch(match["ref"]), f"{action}/action.yml:{number} is not pinned to a commit: {line.strip()}"
        assert VERSION_COMMENT.fullmatch(match["rest"]), f"{action}/action.yml:{number} lacks a '# vX.Y.Z' comment"


def test_snyk_docker_scan_installs_one_snyk_cli_release():
    """`snyk/actions/setup` installs the newest release unless `snyk-version` names one."""
    action = yaml.safe_load((ROOT / "snyk-docker-scan" / "action.yml").read_text(encoding="utf-8"))
    setups = [step for step in action["runs"]["steps"] if step.get("uses", "").startswith("snyk/actions/setup@")]
    assert len(setups) == 1, "expected exactly one snyk/actions/setup step"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", setups[0].get("with", {}).get("snyk-version", "")), (
        "snyk/actions/setup must pass snyk-version: vX.Y.Z"
    )


BUILDKIT_DRIVER_OPTS = re.compile(
    r"\$\{\{ inputs\.builder == 'local'"
    r" && 'image=moby/buildkit:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}'"
    r" \|\| '' \}\}"
)


def test_build_push_ecr_local_mode_runs_one_buildkit_release_by_digest():
    """Without an `image=` driver option, setup-buildx starts buildx's default,
    the moving `moby/buildkit:buildx-stable-1` tag. `local` mode must name a
    release and its multi-arch index digest, so neither a moved tag nor a new
    release changes the container that pushes. `remote` builds elsewhere and
    keeps the default, so its setup stays as it was."""
    action = yaml.safe_load((ROOT / "build-push-ecr" / "action.yml").read_text(encoding="utf-8"))
    setups = [
        step for step in action["runs"]["steps"] if step.get("uses", "").startswith("docker/setup-buildx-action@")
    ]
    assert len(setups) == 1, "expected exactly one docker/setup-buildx-action step"
    assert BUILDKIT_DRIVER_OPTS.fullmatch((setups[0].get("with") or {}).get("driver-opts", "")), (
        "docker/setup-buildx-action must pass driver-opts: image=moby/buildkit:vX.Y.Z@sha256:<64 hex digits>"
        " when builder is local, and nothing otherwise"
    )
