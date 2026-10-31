"""Contract tests for the blocked-deploy comment in `argocd-pr-env-deploy/action.yml`.

When a `Deploys:` link cannot be resolved, the action refuses the deploy and
comments on the PR. That comment is where the author learns how to retry, so
it has to name a retry that keeps the env: removing the `deploy` label deletes
the env and its database. The comment is rendered with the action's own
`printf` line, run through bash, so the test reads the text authors see.
"""

import subprocess
from pathlib import Path

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
