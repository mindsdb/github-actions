"""Unit tests for ``scripts/prune_pr_environments.py``.

The behaviour worth pinning is what is NOT deleted: environments of live PR
envs, environments that are not PR envs at all, and everything in a repo whose
PR list could not be read.
"""

import importlib.util
import subprocess
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prune_pr_environments.py"
_spec = importlib.util.spec_from_file_location("prune_pr_environments", _PATH)
prune = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeGh:
    """Answers `gh api` calls from per-repo tables and records every call."""

    def __init__(self, envs, prs, fail_prs=(), fail_delete=()):
        self.envs, self.prs = envs, prs
        self.fail_prs, self.fail_delete = set(fail_prs), set(fail_delete)
        self.calls = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if "-X" in argv:
            path = argv[-1]
            repo, name = path.removeprefix("repos/").split("/environments/")
            if name in self.fail_delete:
                return completed(stderr="HTTP 403", returncode=1)
            return completed()
        path = next(a for a in argv if a.startswith("repos/"))
        repo = "/".join(path.split("/")[1:3])
        if "/environments" in path:
            return completed("\n".join(self.envs.get(repo, [])) + "\n")
        if repo in self.fail_prs:
            return completed(stderr="HTTP 502", returncode=1)
        return completed("\n".join(str(n) for n in self.prs.get(repo, [])) + "\n")

    def deleted(self):
        return [a[-1].removeprefix("repos/") for a in self.calls if "-X" in a]


class TestStaleEnvironments:
    def test_keeps_live_prs_and_ignores_non_pr_envs(self):
        names = ["dev", "staging", "pr-auth-110", "pr-auth-111", "pr-auth-12x", "pr-cowork-5", "pr-auth-"]
        assert prune.stale_environments("mindsdb/auth", names, keep={110}) == ["pr-auth-111"]

    def test_underscore_repo_uses_the_slug(self):
        names = ["pr-mindshub-frontend-7", "pr-mindshub_frontend-8", "pr-mindshub-frontend-9"]
        assert prune.stale_environments("mindsdb/mindshub_frontend", names, keep={9}) == ["pr-mindshub-frontend-7"]

    def test_anchor_that_prefixes_another_is_not_confused(self):
        # pr-cowork-server-588 belongs to cowork-server, not to cowork PR "server-588".
        assert prune.stale_environments("mindsdb/cowork", ["pr-cowork-server-588", "pr-cowork-3"], keep=set()) == ["pr-cowork-3"]


class TestPrune:
    def test_deletes_only_stale_environments(self):
        gh = FakeGh(
            envs={"mindsdb/auth": ["dev", "pr-auth-1", "pr-auth-2"], "mindsdb/cowork": ["pr-cowork-9"]},
            prs={"mindsdb/auth": [2], "mindsdb/cowork": []},
        )
        assert prune.prune(["mindsdb/auth", "mindsdb/cowork"], dry_run=False, runner=gh) == 0
        assert gh.deleted() == ["mindsdb/auth/environments/pr-auth-1", "mindsdb/cowork/environments/pr-cowork-9"]

    def test_environments_are_listed_before_prs(self):
        # An env created after the PR list was read must not be judged stale.
        gh = FakeGh(envs={"mindsdb/auth": []}, prs={"mindsdb/auth": []})
        prune.prune(["mindsdb/auth"], dry_run=False, runner=gh)
        paths = [next(a for a in c if a.startswith("repos/")) for c in gh.calls]
        assert "/environments" in paths[0] and "/pulls" in paths[1]

    def test_dry_run_deletes_nothing(self):
        gh = FakeGh(envs={"mindsdb/auth": ["pr-auth-1"]}, prs={"mindsdb/auth": []})
        assert prune.prune(["mindsdb/auth"], dry_run=True, runner=gh) == 0
        assert gh.deleted() == []

    def test_unreadable_pr_list_skips_that_repo_only(self):
        gh = FakeGh(
            envs={"mindsdb/auth": ["pr-auth-1"], "mindsdb/cowork": ["pr-cowork-9"]},
            prs={"mindsdb/cowork": []},
            fail_prs={"mindsdb/auth"},
        )
        assert prune.prune(["mindsdb/auth", "mindsdb/cowork"], dry_run=False, runner=gh) == 1
        assert gh.deleted() == ["mindsdb/cowork/environments/pr-cowork-9"]

    def test_failed_delete_does_not_stop_the_rest(self):
        gh = FakeGh(envs={"mindsdb/auth": ["pr-auth-1", "pr-auth-2"]}, prs={"mindsdb/auth": []}, fail_delete={"pr-auth-1"})
        assert prune.prune(["mindsdb/auth"], dry_run=False, runner=gh) == 1
        assert gh.deleted() == ["mindsdb/auth/environments/pr-auth-1", "mindsdb/auth/environments/pr-auth-2"]
