"""Behaviour tests for `argocd-pr-env-deploy/action.yml`.

The action used to work only inside a `pull_request` run, because it read the PR
from the event. A scheduled sync of open PRs now passes `repository`,
`pr-number` and `head-sha` instead. These tests run the real script with `curl`,
`aws` and `argocd` faked and pin four things: a `pull_request` run deploys
exactly what it did before, a run with inputs deploys the PR it was told to,
each image tag is checked in the one ECR repository that holds that repo's PR
images, and input that is not a repo name, a number or a SHA stops the script
before any API, ECR or ArgoCD call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "argocd-pr-env-deploy" / "action.yml").read_text(encoding="utf-8")
)
STEP = ACTION["runs"]["steps"][0]

_BASH_MAJOR = int(
    subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False).stdout
    or 0
)
runs_the_script = pytest.mark.skipif(
    _BASH_MAJOR < 4,
    reason="bash 3.2 (macOS) cannot parse the script's `case` inside a process substitution; GitHub runners run bash 5",
)

MERGE_SHA = "a" * 40
HEAD_SHA = "b" * 40
LINKED_HEAD_SHA = "c" * 40
LINKED_MERGE_SHA = "d" * 40
AWS_REGION = "us-east-1"


def own_pr(repository: str, body: str = "") -> dict:
    """The deployed PR as the API returns it: a branch of `repository` itself."""
    return {"body": body, "head": {"repo": {"full_name": repository}}}

CURL = """
url=""; post=0
for arg in "$@"; do
  case "$arg" in
    https://*) url="$arg" ;;
    POST) post=1 ;;
  esac
done
if [ "$post" = 1 ]; then cat >/dev/null; exit 0; fi
fixture="$STUB_FIXTURES/github/${url#https://api.github.com/}.json"
[ -f "$fixture" ] || exit 22
cat "$fixture"
"""

# `aws ecr describe-images` over three fixture files: the repositories that
# exist, their `<repository>:<tag>` images, and repositories that answer
# AccessDenied. Without --image-ids the call lists a repository's images, so it
# succeeds exactly when the repository exists.
AWS = """
repo=""; tag=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repository-name) repo="$2"; shift ;;
    --image-ids) tag="${2#imageTag=}"; shift ;;
  esac
  shift
done
if grep -qxF "$repo" "$STUB_FIXTURES/ecr-denied"; then
  echo "An error occurred (AccessDeniedException) when calling the DescribeImages operation" >&2
  exit 254
fi
if ! grep -qxF "$repo" "$STUB_FIXTURES/ecr-repos"; then
  echo "An error occurred (RepositoryNotFoundException) when calling the DescribeImages operation" >&2
  exit 254
fi
[ -z "$tag" ] && exit 0
grep -qxF "$repo:$tag" "$STUB_FIXTURES/ecr-images" && exit 0
echo "An error occurred (ImageNotFoundException) when calling the DescribeImages operation" >&2
exit 254
"""

ARGOCD = """
if [ "$1 $2" = "app terminate-op" ]; then
  echo "Unable to terminate operation. No operation is in progress" >&2
  exit 1
fi
"""


def run_deploy(
    stubs,
    tmp_path: Path,
    *,
    event: str,
    repository: str,
    pr_number: str,
    head_sha: str,
    prs: dict[str, dict] | None = None,
    ecr_repos: tuple[str, ...] = (),
    ecr_images: tuple[str, ...] = (),
    ecr_denied: tuple[str, ...] = (),
):
    """Run the step's script as GitHub would after resolving the inputs.

    `repository`, `pr_number` and `head_sha` are the resolved input values:
    the event's own in a pull_request run, whatever the caller passed otherwise.
    `prs` maps an API path such as `repos/mindsdb/auth/pulls/7` to its response.
    `ecr_denied` lists repositories whose every describe-images call fails with
    AccessDenied.
    """
    fixtures = tmp_path / "fixtures"
    for api_path, body in (prs or {}).items():
        target = fixtures / "github" / f"{api_path}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body), encoding="utf-8")
    fixtures.mkdir(exist_ok=True)
    (fixtures / "ecr-repos").write_text("".join(f"{r}\n" for r in ecr_repos), encoding="utf-8")
    (fixtures / "ecr-images").write_text("".join(f"{i}\n" for i in ecr_images), encoding="utf-8")
    (fixtures / "ecr-denied").write_text("".join(f"{r}\n" for r in ecr_denied), encoding="utf-8")

    stubs.add("curl", CURL)
    stubs.add("aws", AWS)
    stubs.add("argocd", ARGOCD)
    return stubs.run(
        STEP["run"],
        {
            "STUB_FIXTURES": str(fixtures),
            "ARGOCD_SERVER": ACTION["inputs"]["argocd-server"]["default"],
            "ARGOCD_PLAINTEXT": "auto",
            "ARGOCD_AUTH_TOKEN": "argocd-token",
            "ARGOCD_VERSION": "v0.0.0",
            "GH_TOKEN": "gh-token",
            "STACKS_DISPATCH_TOKEN": "gh-token",
            "WAIT_TIMEOUT": "60",
            "PR_REPOSITORY": repository,
            "PR_NUMBER": pr_number,
            "PR_HEAD_SHA": head_sha,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_SHA": MERGE_SHA,
            "GITHUB_REPOSITORY": "mindsdb/example-caller" if event != "pull_request" else repository,
            "RUNNER_TEMP": str(tmp_path),
            "AWS_REGION": AWS_REGION,
        },
        tmp_path,
    )


def app_set(stubs) -> list[str]:
    calls = [argv for argv in stubs.calls("argocd") if argv[1:3] == ["app", "set"]]
    assert len(calls) == 1, stubs.calls("argocd")
    return calls[0]


def repository_lookup(repository: str) -> list[str]:
    """The call that asks ECR whether `repository` exists. It asks for one
    image, because without `--max-items 1` the CLI pages through every image in
    the repository, one request per 100 images."""
    return [
        "aws", "ecr", "describe-images", "--repository-name", repository,
        "--max-items", "1", "--region", AWS_REGION,
    ]


def tag_lookup(repository: str, tag: str) -> list[str]:
    """The call that asks ECR whether `repository` holds `tag`."""
    return [
        "aws", "ecr", "describe-images", "--repository-name", repository,
        "--image-ids", f"imageTag={tag}", "--region", AWS_REGION,
    ]


class TestInputsDefaultToThePullRequestEvent:
    """A caller in a pull_request run passes nothing and gets today's values."""

    @pytest.mark.parametrize(
        ("name", "default", "env"),
        [
            ("repository", "${{ github.repository }}", "PR_REPOSITORY"),
            ("pr-number", "${{ github.event.pull_request.number }}", "PR_NUMBER"),
            ("head-sha", "${{ github.event.pull_request.head.sha }}", "PR_HEAD_SHA"),
        ],
    )
    def test_input_defaults_to_the_event_and_reaches_the_script_through_env(self, name, default, env):
        assert ACTION["inputs"][name]["required"] is False
        assert ACTION["inputs"][name]["default"] == default
        assert STEP["env"][env] == f"${{{{ inputs.{name} }}}}"

    def test_the_script_expands_no_template_itself(self):
        """Every input reaches bash as an environment variable, never as text
        spliced into the script, which is what makes refusing bad input enough."""
        assert "${{" not in STEP["run"]


@runs_the_script
class TestAPullRequestRunIsUnchanged:
    def test_it_deploys_the_merge_sha_image_this_run_built(self, stubs, tmp_path):
        result = run_deploy(
            stubs, tmp_path,
            event="pull_request", repository="mindsdb/auth", pr_number="7", head_sha=HEAD_SHA,
            prs={"repos/mindsdb/auth/pulls/7": own_pr("mindsdb/auth", "No links.")},
            ecr_repos=("mindsdb-auth",),
            ecr_images=(f"mindsdb-auth:development-{MERGE_SHA}",),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert app_set(stubs) == [
            "argocd", "app", "set", "pr-auth-7", "--grpc-web", "--plaintext",
            "--helm-set", f"tags.auth=development-{MERGE_SHA}",
            "--helm-set", f"revisions.auth={HEAD_SHA}",
            "--revision", "main",
        ]
        wait = [argv for argv in stubs.calls("argocd") if argv[1:3] == ["app", "wait"]]
        assert wait[0][3:5] == ["-l", "pr-env.mindsdb.com/anchor-repo=auth,pr-env.mindsdb.com/pr-number=7"]


@runs_the_script
class TestARunWithInputsDeploysTheNamedPr:
    """What a scheduled or manually dispatched sync of open PRs does: no PR event,
    no build in this run."""

    @pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
    def test_it_deploys_the_head_image_of_the_pr_it_names(self, stubs, tmp_path, event):
        result = run_deploy(
            stubs, tmp_path,
            event=event, repository="mindsdb/cowork", pr_number="12", head_sha=HEAD_SHA,
            prs={"repos/mindsdb/cowork/pulls/12": own_pr("mindsdb/cowork")},
            ecr_repos=("mindsdb-cowork-dev", "mindsdb-cowork"),
            ecr_images=(f"mindsdb-cowork-dev:development-head-{HEAD_SHA}",),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert app_set(stubs) == [
            "argocd", "app", "set", "pr-cowork-12", "--grpc-web", "--plaintext",
            "--helm-set", f"tags.cowork=development-head-{HEAD_SHA}",
            "--helm-set", f"revisions.cowork={HEAD_SHA}",
            "--revision", "main",
        ]
        gets = [argv[-1] for argv in stubs.calls("curl") if "POST" not in argv]
        assert gets == ["https://api.github.com/repos/mindsdb/cowork/pulls/12"]

    @pytest.mark.parametrize(
        "head_repo",
        [{"full_name": "someone/cowork"}, None],
        ids=["fork", "deleted-fork"],
    )
    def test_a_pr_from_a_fork_is_refused_before_any_ecr_or_argocd_call(self, stubs, tmp_path, head_repo):
        """The caller names the PR, so the script checks where its branch lives.
        A fork's branch would otherwise pick the chart revision ArgoCD renders."""
        result = run_deploy(
            stubs, tmp_path,
            event="schedule", repository="mindsdb/cowork", pr_number="12", head_sha=HEAD_SHA,
            prs={"repos/mindsdb/cowork/pulls/12": {"body": "", "head": {"repo": head_repo}}},
            ecr_repos=("mindsdb-cowork-dev",),
            ecr_images=(f"mindsdb-cowork-dev:development-head-{HEAD_SHA}",),
        )

        assert result.returncode == 1
        assert (
            "::error::PR #12 comes from a fork; only PRs from mindsdb/cowork's own branches deploy.\n"
        ) in result.stdout
        assert stubs.calls("aws") == []
        assert stubs.calls("argocd") == []


LINKS_COWORK_5 = {
    "repos/mindsdb/auth/pulls/7": own_pr("mindsdb/auth", "Deploys: cowork#5"),
    "repos/mindsdb/cowork/pulls/5": {"merge_commit_sha": LINKED_MERGE_SHA, "head": {"sha": LINKED_HEAD_SHA}},
}


@runs_the_script
class TestEachTagIsCheckedInOneRepository:
    """A repo with a dev tier pushes its PR images to `mindsdb-<repo>-dev` and
    nowhere else, so when that repository exists a tag is checked there alone.
    Otherwise it is checked in `mindsdb-<repo>`. Whether `-dev` exists decides
    it, never whether `-dev` holds the tag."""

    def test_a_repo_with_a_dev_tier_is_checked_there_only(self, stubs, tmp_path):
        """The image sits in `mindsdb-cowork` alone. A check that went on to
        that repository after missing in `-dev` would find it and deploy."""
        result = run_deploy(
            stubs, tmp_path,
            event="schedule", repository="mindsdb/cowork", pr_number="12", head_sha=HEAD_SHA,
            prs={"repos/mindsdb/cowork/pulls/12": own_pr("mindsdb/cowork")},
            ecr_repos=("mindsdb-cowork-dev", "mindsdb-cowork"),
            ecr_images=(f"mindsdb-cowork:development-head-{HEAD_SHA}",),
        )

        assert result.returncode == 1
        assert f"::error::ECR image mindsdb-cowork-dev:development-head-{HEAD_SHA} not found." in result.stdout
        assert stubs.calls("aws") == [
            repository_lookup("mindsdb-cowork-dev"),
            tag_lookup("mindsdb-cowork-dev", f"development-head-{HEAD_SHA}"),
        ]
        assert stubs.calls("argocd") == []

    def test_a_repo_without_a_dev_tier_is_checked_in_its_own_repository(self, stubs, tmp_path):
        result = run_deploy(
            stubs, tmp_path,
            event="schedule", repository="mindsdb/mindshub_inference", pr_number="3", head_sha=HEAD_SHA,
            prs={"repos/mindsdb/mindshub_inference/pulls/3": own_pr("mindsdb/mindshub_inference")},
            ecr_repos=("mindsdb-mindshub-inference",),
            ecr_images=(f"mindsdb-mindshub-inference:development-head-{HEAD_SHA}",),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert stubs.calls("aws") == [
            repository_lookup("mindsdb-mindshub-inference-dev"),
            tag_lookup("mindsdb-mindshub-inference", f"development-head-{HEAD_SHA}"),
        ]
        assert f"tags.mindshub_inference=development-head-{HEAD_SHA}" in app_set(stubs)

    def test_a_linked_pr_image_is_found_in_its_dev_tier(self, stubs, tmp_path):
        result = run_deploy(
            stubs, tmp_path,
            event="pull_request", repository="mindsdb/auth", pr_number="7", head_sha=HEAD_SHA,
            prs=LINKS_COWORK_5,
            ecr_repos=("mindsdb-auth", "mindsdb-cowork-dev", "mindsdb-cowork"),
            ecr_images=(
                f"mindsdb-auth:development-{MERGE_SHA}",
                f"mindsdb-cowork-dev:development-head-{LINKED_HEAD_SHA}",
            ),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert f"tags.cowork=development-head-{LINKED_HEAD_SHA}" in app_set(stubs)
        assert stubs.calls("aws")[-2:] == [
            repository_lookup("mindsdb-cowork-dev"),
            tag_lookup("mindsdb-cowork-dev", f"development-head-{LINKED_HEAD_SHA}"),
        ]

    def test_a_linked_pr_image_outside_its_dev_tier_does_not_count(self, stubs, tmp_path):
        """Both of the linked PR's tags sit in `mindsdb-cowork` alone, so neither
        counts, and the script never asks that repository."""
        result = run_deploy(
            stubs, tmp_path,
            event="pull_request", repository="mindsdb/auth", pr_number="7", head_sha=HEAD_SHA,
            prs=LINKS_COWORK_5,
            ecr_repos=("mindsdb-auth", "mindsdb-cowork-dev", "mindsdb-cowork"),
            ecr_images=(
                f"mindsdb-auth:development-{MERGE_SHA}",
                f"mindsdb-cowork:development-head-{LINKED_HEAD_SHA}",
                f"mindsdb-cowork:development-{LINKED_MERGE_SHA}",
            ),
        )

        assert result.returncode == 1
        assert (
            f"neither development-head-{LINKED_HEAD_SHA} nor development-{LINKED_MERGE_SHA}"
            " exists in mindsdb-cowork-dev."
        ) in result.stdout
        assert stubs.calls("aws") == [
            repository_lookup("mindsdb-auth-dev"),
            tag_lookup("mindsdb-auth", f"development-{MERGE_SHA}"),
            repository_lookup("mindsdb-cowork-dev"),
            tag_lookup("mindsdb-cowork-dev", f"development-head-{LINKED_HEAD_SHA}"),
            repository_lookup("mindsdb-cowork-dev"),
            tag_lookup("mindsdb-cowork-dev", f"development-{LINKED_MERGE_SHA}"),
        ]
        assert stubs.calls("argocd") == []

    def test_an_ecr_error_while_picking_the_repository_turns_the_checks_off(self, stubs, tmp_path):
        """AccessDenied says nothing about whether `-dev` exists. It turns the
        checks off like any other ECR error, rather than sending the check to
        `mindsdb-cowork` as if cowork had no dev tier."""
        result = run_deploy(
            stubs, tmp_path,
            event="schedule", repository="mindsdb/cowork", pr_number="12", head_sha=HEAD_SHA,
            prs={"repos/mindsdb/cowork/pulls/12": own_pr("mindsdb/cowork")},
            ecr_repos=("mindsdb-cowork-dev", "mindsdb-cowork"),
            ecr_denied=("mindsdb-cowork-dev",),
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert stubs.calls("aws") == [repository_lookup("mindsdb-cowork-dev")]
        assert result.stdout.count("::warning::ECR describe-images failed") == 1
        assert f"tags.cowork=development-head-{HEAD_SHA}" in app_set(stubs)


BAD_REPOSITORY = "::error::repository must be owner/name.\n"
BAD_PR_NUMBER = (
    "::error::pr-number must be a PR number. Outside a pull_request event, "
    "pass repository, pr-number and head-sha.\n"
)
BAD_HEAD_SHA = "::error::head-sha must be a full 40-character commit SHA.\n"


@runs_the_script
class TestInputThatIsNotAPrIsRefused:
    @pytest.mark.parametrize(
        ("repository", "pr_number", "head_sha", "message"),
        [
            ("mindsdb/cowork", "12; curl attacker.example", HEAD_SHA, BAD_PR_NUMBER),
            ("mindsdb/cowork", "12\n::add-mask::x", HEAD_SHA, BAD_PR_NUMBER),
            ("mindsdb/cowork", "12", HEAD_SHA[:7], BAD_HEAD_SHA),
            ("mindsdb/cowork", "12", HEAD_SHA.upper(), BAD_HEAD_SHA),
            ("mindsdb/cowork/../auth", "12", HEAD_SHA, BAD_REPOSITORY),
            ("mindsdb", "12", HEAD_SHA, BAD_REPOSITORY),
            ("mindsdb/..", "12", HEAD_SHA, BAD_REPOSITORY),
            ("mindsdb/.", "12", HEAD_SHA, BAD_REPOSITORY),
            ("../cowork", "12", HEAD_SHA, BAD_REPOSITORY),
        ],
    )
    def test_it_stops_before_any_call_and_prints_only_the_rule(
        self, stubs, tmp_path, repository, pr_number, head_sha, message
    ):
        """The one output line is the rule, never the value: a value copied from
        a PR could otherwise carry a workflow command into the log."""
        result = run_deploy(
            stubs, tmp_path,
            event="schedule", repository=repository, pr_number=pr_number, head_sha=head_sha,
        )

        assert result.returncode == 1
        assert (result.stdout, result.stderr) == (message, "")
        assert stubs.calls() == []

    def test_a_run_outside_a_pull_request_without_inputs_says_what_to_pass(self, stubs, tmp_path):
        """Outside a pull_request event the defaults resolve to empty strings."""
        result = run_deploy(
            stubs, tmp_path,
            event="workflow_dispatch", repository="mindsdb/example-caller", pr_number="", head_sha="",
        )

        assert result.returncode == 1
        assert result.stdout == BAD_PR_NUMBER
        assert stubs.calls() == []
