"""Contract tests for `argocd-pr-env-deploy/action.yml`.

When a `Deploys:` link cannot be resolved, the action refuses the deploy and
comments on the PR. That comment is where the author learns how to retry, so
it has to name a retry that keeps the env: removing the `deploy` label deletes
the env and its database. The comment is rendered with the action's own
`printf` line, run through bash, so the test reads the text authors see. A
`Deploys: argocd-envs#N` line gets a comment naming the label that replaced it.

The staff console is opt-in: it is selected by a mindshub_stafftools anchor or
a `Deploys: mindshub_stafftools#...` line, and by nothing else. Those tests run
the action's own shell, from the anchor's parameters through the `Deploys:`
link loop, with GitHub stubbed, and read what the action hands the writer.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION_PATH = Path(__file__).resolve().parents[1] / "argocd-pr-env-deploy" / "action.yml"
ACTION = yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))
RUN = "\n".join(step.get("run", "") for step in ACTION["runs"]["steps"])
UNRESOLVED_BRANCH = 'if [ "${#unresolved_links[@]}" -gt 0 ]; then'


def unresolved_links_comment(links: str) -> str:
    """Render the comment the action posts for unresolvable `Deploys:` links."""
    branch = RUN.split(UNRESOLVED_BRANCH, 1)[1]
    line = next(line.strip() for line in branch.splitlines() if line.strip().startswith('comment="$(printf'))
    script = f'links="$1"\n{line}\nprintf "%s" "$comment"'
    return subprocess.run(["bash", "-c", script, "_", links], capture_output=True, text=True, check=True).stdout


class TestUnresolvedLinkComment:
    def test_names_every_refused_link(self):
        comment = unresolved_links_comment("mindsdb/api#5, mindsdb/web#9")
        assert comment.startswith("**PR-env deploy blocked.**")
        assert "the deploy was refused: mindsdb/api#5, mindsdb/web#9\n" in comment

    def test_tells_the_author_how_to_fix_a_conflicted_link(self):
        comment = unresolved_links_comment("mindsdb/api#5")
        assert "in conflict with its base branch" in comment
        assert "\n1. Fix each link above. For a PR in conflict, merge or rebase its base branch into it" in comment

    def test_retries_by_re_running_the_failed_jobs(self):
        comment = unresolved_links_comment("mindsdb/api#5")
        assert "\n2. Re-run the failed jobs from the Checks tab of this PR." in comment

    def test_warns_against_retrying_with_the_deploy_label(self):
        comment = unresolved_links_comment("mindsdb/api#5")
        assert "toggling the `deploy` label" not in comment
        assert "Do not remove the `deploy` label to retry. Removing it deletes this env" in comment


# The action runs under the runner's bash 5. macOS's /bin/bash 3.2 misparses the
# `case` patterns inside the link loop's process substitution, so the tests that
# run that loop need bash 4 or later.
BASH_MAJOR = int(
    subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=True).stdout
)
NEEDS_RUNNER_BASH = pytest.mark.skipif(BASH_MAJOR < 4, reason="the action's link loop needs bash 4+, as on the runner")

STAFF_FUNCTIONS_START = "is_revision_only_repo() {"
LINKS_START = 'if is_revision_only_repo "$OWN_SHORT"; then'
LINKS_END = "\n\n# Post a comment on the anchor PR"
ENVS_BRANCH = 'if [ "${#envs_links[@]}" -gt 0 ]; then'
SETTINGS_START = "# What this run resolved, as tags.<repo>=<tag>"
SETTINGS_END = "\n\n# Write the environment's file in argocd-state."

# Every linked PR resolves, and an argocd-envs link resolves to a branch on
# argocd-envs itself, so the link loop runs without GitHub.
GH_API_STUB = (
    "gh_api() { printf '%s' '{\"merge_commit_sha\": \"m\", \"head\": {\"sha\": \"h\", \"ref\": \"b\", "
    "\"repo\": {\"full_name\": \"mindsdb/argocd-envs\"}}}'; }"
)


def run_slice(start: str, end: str) -> str:
    """The action's shell from the line starting with `start` up to `end`."""
    begin = RUN.index(start)
    return RUN[begin : RUN.index(end, begin)]


def deploy_script(*tail: str) -> str:
    """The action's own parameters and `Deploys:` link loop, then `tail`.

    GitHub is stubbed and the ECR checks are off, so `set_args` comes out of
    the action's own parser exactly as a deploy would build it.
    """
    return "\n".join(
        [
            "set -euo pipefail",
            GH_API_STUB,
            "ecr_tag_exists() { return 0; }",
            "ECR_CHECKS=off",
            run_slice(STAFF_FUNCTIONS_START, LINKS_START),
            run_slice(LINKS_START, LINKS_END),
            *tail,
        ]
    )


def deploy_env(own_short: str, body: str) -> dict[str, str]:
    return {
        **os.environ,
        "OWN_REPO": f"mindsdb/{own_short}",
        "OWN_SHORT": own_short,
        "OWN_MERGE_SHA": "merge",
        "OWN_HEAD_SHA": "head",
        "PR_BODY": body,
    }


def staff_console_selected(own_short: str, body: str) -> bool:
    # check=True, so a harness error fails the test rather than reading as "not selected".
    script = deploy_script("if staff_console_selected; then echo selected; else echo off; fi")
    run = subprocess.run(
        ["bash", "-c", script], env=deploy_env(own_short, body), capture_output=True, text=True, check=True
    )
    return run.stdout.splitlines()[-1] == "selected"


def settings(own_short: str, body: str) -> list[str]:
    """What the action hands the writer for this anchor and PR body: the lines after `Deploying <env> with:`."""
    script = deploy_script("post_blocking_comment() { :; }", run_slice(ENVS_BRANCH, SETTINGS_START), run_slice(SETTINGS_START, SETTINGS_END))
    env = {**deploy_env(own_short, body), "PARENT_APP": f"pr-{own_short.replace('_', '-')}-7"}
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True)
    lines = run.stdout.splitlines()
    return [line.strip() for line in lines[lines.index(f"Deploying pr-{own_short.replace('_', '-')}-7 with:") + 1:]]


def envs_links_comment(links: str) -> str:
    """Render the comment the action posts for a `Deploys: argocd-envs#N` line."""
    branch = RUN.split(ENVS_BRANCH, 1)[1]
    line = next(line.strip() for line in branch.splitlines() if line.strip().startswith('comment="$(printf'))
    script = f'links="$1"\n{line}\nprintf "%s" "$comment"'
    return subprocess.run(["bash", "-c", script, "_", links], capture_output=True, text=True, check=True).stdout


class TestArgocdEnvsLinkComment:
    def test_names_the_label_that_replaced_the_line(self):
        comment = envs_links_comment("mindsdb/argocd-envs#40")
        assert comment.startswith("**PR-env deploy blocked.**")
        assert "is no longer read: mindsdb/argocd-envs#40\n" in comment
        assert "`argocd-envs:<branch>`" in comment
        assert "Removing the label puts the environment back on `main`." in comment


@NEEDS_RUNNER_BASH
class TestStaffConsoleSelection:
    def test_a_stafftools_anchor_selects_its_own_console(self):
        assert staff_console_selected("mindshub_stafftools", "")

    @pytest.mark.parametrize(
        "line",
        [
            "Deploys: mindshub_stafftools#staging",
            "Deploys: mindshub_stafftools#main",
            "Deploys: mindshub_stafftools#12",
            "Deploys: mindsdb/mindshub_stafftools#12",
        ],
    )
    def test_a_deploys_line_selects_it(self, line):
        assert staff_console_selected("mindshub_frontend", f"Some text\n{line}\nDeploys: auth#5")

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Deploys: auth#5",
            "Deploys: argocd-envs#5",
            "Follow-up to mindshub_stafftools#12",
            "Deploys: other_mindshub_stafftools#1",
            "Deploys: mindshub_stafftools_v2#1",
        ],
    )
    def test_nothing_else_selects_it(self, body):
        assert not staff_console_selected("mindshub_frontend", body)


@NEEDS_RUNNER_BASH
class TestWhatTheWriterGets:
    def test_the_anchors_own_image_and_chart_commit(self):
        assert settings("mindshub_frontend", "") == ["tags.mindshub_frontend=development-merge", "revisions.mindshub_frontend=head"]

    def test_every_kind_of_link(self):
        body = "Deploys: auth#5\nDeploys: cowork-server#staging\nDeploys: mindshub_services#9\nDeploys: mindshub_stafftools#main"
        assert settings("mindshub_frontend", body) == [
            "tags.mindshub_frontend=development-merge", "revisions.mindshub_frontend=head",
            "tags.auth=development-m", "revisions.auth=h",
            "tags.cowork-server=staging", "revisions.cowork-server=staging",
            "revisions.mindshub_services=h",
            "tags.mindshub_stafftools=production", "revisions.mindshub_stafftools=main",
        ]

    def test_a_services_anchor_has_a_revision_and_no_image(self):
        assert settings("mindshub_services", "") == ["revisions.mindshub_services=head"]

    def test_an_unselected_console_has_no_keys_to_keep(self):
        # The writer replaces the file's maps with these, so the console's keys go with its line.
        assert not any("mindshub_stafftools" in line for line in settings("mindshub_frontend", "Deploys: auth#5"))

    def test_an_argocd_envs_link_is_refused_before_anything_is_written(self):
        script = deploy_script("post_blocking_comment() { echo \"commented: $1\"; }", run_slice(ENVS_BRANCH, SETTINGS_START))
        env = {**deploy_env("mindshub_frontend", "Deploys: argocd-envs#40\nDeploys: auth#5"), "PARENT_APP": "pr-mindshub-frontend-7"}
        run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        assert run.returncode == 1
        assert "commented: **PR-env deploy blocked.**" in run.stdout
        assert "::error::Refusing to deploy: Deploys: argocd-envs links (mindsdb/argocd-envs#40)" in run.stdout


def test_the_customer_label_is_refused_before_the_links_are_read():
    assert RUN.index('if [ -n "$LABEL_HOST" ]; then') < RUN.index(STAFF_FUNCTIONS_START)
    assert "A customer environment's file, pr-envs/${PARENT_APP}.yaml in argocd-state, is written by hand" in RUN


def test_the_status_is_set_only_on_a_push_and_waited_on_either_way():
    assert RUN.index("grep -qx 'pushed=true'") < RUN.index('context: "argocd/pr-env"') < RUN.index('select(.context == "argocd/pr-env")')
    assert "argocd app" not in RUN
