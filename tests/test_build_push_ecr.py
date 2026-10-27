"""Behaviour tests for `build-push-ecr/action.yml`.

Private repositories call this action on `mdb-dev` with the default
`builder: remote`, and their builds have to keep running exactly the commands
they ran before `local` existed. So the remote case pins the full argv of every
`aws` and `docker` call, and the local case is asserted as a difference from it:
no repository setup, no remote builder, and the GitHub Actions cache flags added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ACTION_DIR = Path(__file__).resolve().parents[1] / "build-push-ecr"
ACTION = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
STEPS = ACTION["runs"]["steps"]
POLICY = (ACTION_DIR / "shared-ecr-policy.json").read_text(encoding="utf-8").rstrip("\n")

REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
MERGE_SHA = "a" * 40
HEAD_SHA = "b" * 40
EXTRA_BUILD_ARGS = "-f docker/Dockerfile --secret id=github_token,env=GITHUB_TOKEN"


def step(step_id: str) -> dict:
    matches = [candidate for candidate in STEPS if candidate.get("id") == step_id]
    assert len(matches) == 1, f"expected exactly one step with id '{step_id}'"
    return matches[0]


def render(script: str, values: dict[str, str]) -> str:
    """Substitute `${{ ... }}` the way the runner does before bash sees the script.

    An expression missing from `values` fails the test, so a new one added to the
    action has to be given a value here rather than silently rendering empty.
    """

    def substitute(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        assert expression in values, f"no test value for ${{{{ {expression} }}}}"
        return values[expression]

    return re.sub(r"\$\{\{(.*?)\}\}", substitute, script)


def run_build(stubs, tmp_path: Path, *, builder: str, pr_head_sha: str = HEAD_SHA):
    stubs.add("aws")
    stubs.add("docker")
    script = render(
        step("build")["run"],
        {
            "inputs.src-path": "",
            "inputs.image-ref": "",
            "inputs.module-name": "mindsdb-example",
            "steps.login-ecr.outputs.registry": REGISTRY,
            "inputs.build-for-environment": "development",
            "env.ENV_NAME": "example-feature",
            "github.event.pull_request.head.sha": pr_head_sha,
            "github.action_path": str(ACTION_DIR),
            "inputs.extra-build-args": EXTRA_BUILD_ARGS,
        },
    )
    output = tmp_path / "github_output"
    output.touch()
    result = stubs.run(
        script,
        {"BUILDER": builder, "CI_SHA": MERGE_SHA, "GITHUB_OUTPUT": str(output)},
        tmp_path,
    )
    return result, output.read_text(encoding="utf-8")


IMAGE = f"{REGISTRY}/mindsdb-example"
BUILD_ARGV = [
    "docker", "buildx", "build",
    *EXTRA_BUILD_ARGS.split(),
    "--build-arg", "BUILD_FOR_ENVIRONMENT=development",
    "--build-arg", f"IMAGE_TAG=development-{MERGE_SHA}",
    "-t", f"{IMAGE}:development-head-{HEAD_SHA}",
    "-t", f"{IMAGE}:development-{MERGE_SHA}",
    "-t", f"{IMAGE}:development",
    "-t", f"{IMAGE}:latest",
    "--push", ".",
]
CACHE_ARGV = [
    "--cache-from", "type=gha,scope=mindsdb-example",
    "--cache-to", "type=gha,scope=mindsdb-example,mode=max",
]


class TestRemoteKeepsTodaysCommands:
    """The default every private caller gets, argv for argv."""

    def test_builder_defaults_to_remote(self):
        assert ACTION["inputs"]["builder"]["default"] == "remote"

    def test_remote_runs_the_same_aws_and_docker_calls(self, stubs, tmp_path):
        result, output = run_build(stubs, tmp_path, builder="remote")

        assert result.returncode == 0, result.stderr
        assert stubs.calls() == [
            ["aws", "ecr", "create-repository", "--repository-name", "mindsdb-example"],
            ["aws", "ecr", "set-repository-policy", "--repository-name", "mindsdb-example", "--policy-text", POLICY],
            [
                "docker", "buildx", "create", "--name=remote-buildkit-agent", "--driver=remote", "--use",
                "tcp://remote-buildkit-agent.infrastructure.svc.cluster.local:80",
            ],
            BUILD_ARGV,
        ]
        assert output == f"image={IMAGE}:development-{MERGE_SHA}\n"

    def test_a_push_build_gets_no_head_tag(self, stubs, tmp_path):
        result, _ = run_build(stubs, tmp_path, builder="remote", pr_head_sha="")

        assert result.returncode == 0, result.stderr
        build = stubs.calls("docker")[-1]
        assert not any("-head-" in arg for arg in build)

    def test_the_cache_step_is_skipped(self):
        cache_steps = [s for s in STEPS if "crazy-max/ghaction-github-runtime@" in s.get("uses", "")]
        assert len(cache_steps) == 1
        assert cache_steps[0]["if"] == "inputs.builder == 'local'"


class TestLocalBuildsOnTheRunner:
    """For GitHub-hosted runners: no in-cluster builder, no repository setup."""

    def test_local_makes_no_aws_call_and_no_remote_builder(self, stubs, tmp_path):
        result, _ = run_build(stubs, tmp_path, builder="local")

        assert result.returncode == 0, result.stderr
        assert stubs.calls("aws") == []
        assert [argv[:3] for argv in stubs.calls("docker")] == [["docker", "buildx", "build"]]

    def test_local_adds_only_the_cache_flags(self, stubs, tmp_path):
        result, output = run_build(stubs, tmp_path, builder="local")

        assert result.returncode == 0, result.stderr
        build = stubs.calls("docker")[-1]
        assert [arg for arg in build if arg not in CACHE_ARGV] == BUILD_ARGV
        cache_at = build.index("--cache-from")
        assert build[cache_at:cache_at + len(CACHE_ARGV)] == CACHE_ARGV
        assert output == f"image={IMAGE}:development-{MERGE_SHA}\n"

    def test_the_runtime_is_exposed_before_the_build(self):
        names = [s.get("uses", s.get("id", "")) for s in STEPS]
        runtime = next(i for i, n in enumerate(names) if n.startswith("crazy-max/ghaction-github-runtime@"))
        assert runtime < names.index("build")


@pytest.mark.parametrize("builder", ["", "Local", "cluster"])
def test_an_unknown_builder_fails_before_any_aws_or_docker_call(stubs, tmp_path, builder):
    result, output = run_build(stubs, tmp_path, builder=builder)

    assert result.returncode != 0
    assert "builder must be 'remote' or 'local'" in result.stdout
    assert stubs.calls() == []
    assert output == ""
