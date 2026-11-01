"""Contract tests for `argocd-pr-env-deploy/action.yml`.

When a `Deploys:` link cannot be resolved, the action refuses the deploy and
comments on the PR. That comment is where the author learns how to retry, so
it has to name a retry that keeps the env: removing the `deploy` label deletes
the env and its database. The comment is rendered with the action's own
`printf` line, run through bash, so the test reads the text authors see.

The staff console is opt-in, and `app set` only ever adds parameters, so the
action unsets the two staff console parameters when nothing selects the
console any more. Those tests run the action's own shell against a fake
`argocd` that records its arguments.
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


STAFF_FUNCTIONS_START = "staff_console_selected() {"
STAFF_FUNCTIONS_END = "\n\nif is_revision_only_repo"
STAFF_OFF_START = "# Turn the staff console off when nothing selects it any more."
STAFF_OFF_END = "\n\n# Retry `app set` directly"
APP_SET_CALL = '"$ARGOCD_BIN" app set "$PARENT_APP"'
PARENT_GET_CALL = '"$ARGOCD_BIN" app get "$PARENT_APP"'


def run_slice(start: str, end: str) -> str:
    """The action's shell from the line starting with `start` up to `end`."""
    begin = RUN.index(start)
    return RUN[begin : RUN.index(end, begin)]


def staff_console_selected(own_short: str, body: str) -> bool:
    script = f'set -euo pipefail\n{run_slice(STAFF_FUNCTIONS_START, STAFF_FUNCTIONS_END)}\nstaff_console_selected "$1" "$2"'
    return subprocess.run(["bash", "-c", script, "_", own_short, body], check=False).returncode == 0


def argocd_calls(tmp_path: Path, own_short: str, body: str, parent_json: str) -> list[str]:
    """Run the switch-off block and return each `argocd` call it made."""
    calls = tmp_path / "calls"
    fake = tmp_path / "argocd"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$CALLS"\n', encoding="utf-8")
    fake.chmod(0o755)
    script = "\n".join(
        [
            "set -euo pipefail",
            "ARGOCD_FLAGS=(--grpc-web --plaintext)",
            run_slice(STAFF_FUNCTIONS_START, STAFF_FUNCTIONS_END),
            run_slice(STAFF_OFF_START, STAFF_OFF_END),
        ]
    )
    env = {
        **os.environ,
        "CALLS": str(calls),
        "ARGOCD_BIN": str(fake),
        "PARENT_APP": "pr-mindshub-frontend-7",
        "OWN_SHORT": own_short,
        "PR_BODY": body,
        "PARENT_JSON": parent_json,
    }
    subprocess.run(["bash", "-c", script], env=env, check=True)
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def parent_with(*names: str) -> str:
    params = ",".join(f'{{"name": "{name}", "value": "x"}}' for name in names)
    return f'{{"spec": {{"source": {{"helm": {{"parameters": [{params}]}}}}}}}}'


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
            "Follow-up to mindshub_stafftools#12",
            "Deploys: other_mindshub_stafftools#1",
            "Deploys: mindshub_stafftools_v2#1",
        ],
    )
    def test_nothing_else_selects_it(self, body):
        assert not staff_console_selected("mindshub_frontend", body)


class TestStaffConsoleSwitchOff:
    def test_unsets_only_the_two_staff_console_parameters(self, tmp_path):
        parent = parent_with(
            "tags.auth", "tags.mindshub_stafftools", "revisions.mindshub_stafftools", "revisions.mindshub_services"
        )
        assert argocd_calls(tmp_path, "mindshub_frontend", "Deploys: auth#5", parent) == [
            "app unset pr-mindshub-frontend-7 --grpc-web --plaintext"
            " -p tags.mindshub_stafftools -p revisions.mindshub_stafftools"
        ]

    def test_leaves_a_parent_without_staff_console_parameters_alone(self, tmp_path):
        # A customer env pins stafftools in its argocd-envs branch's values,
        # not as parameters, so its console stays.
        parent = parent_with("tags.mindshub_frontend", "revisions.mindshub_services")
        assert argocd_calls(tmp_path, "mindshub_frontend", "Deploys: argocd-envs#34", parent) == []

    def test_keeps_a_selected_console(self, tmp_path):
        parent = parent_with("tags.mindshub_stafftools", "revisions.mindshub_stafftools")
        body = "Deploys: mindshub_stafftools#staging"
        assert argocd_calls(tmp_path, "mindshub_frontend", body, parent) == []
        assert argocd_calls(tmp_path, "mindshub_stafftools", "", parent) == []

    def test_does_nothing_before_the_parent_exists(self, tmp_path):
        assert argocd_calls(tmp_path, "mindshub_frontend", "", "") == []

    def test_runs_after_reading_the_parent_and_before_app_set(self):
        assert RUN.index(PARENT_GET_CALL) < RUN.index(STAFF_OFF_START) < RUN.index(APP_SET_CALL)
