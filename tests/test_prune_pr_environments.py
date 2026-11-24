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


def no_sleep(_seconds):
    pass


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeGh:
    """Answers `gh api` calls from per-repo tables and records every call."""

    def __init__(self, envs, prs, fail_prs=(), fail_delete=(), closed=None, comments=None, fail_closed=(), fail_comments=(),
                 flaky_closed=None, fail_post=()):
        self.envs, self.prs = envs, prs
        self.fail_prs, self.fail_delete = set(fail_prs), set(fail_delete)
        self.closed, self.comments = closed or {}, comments or {}
        self.fail_closed, self.fail_comments = set(fail_closed), set(fail_comments)
        # repo -> how many closed-PR listings fail (a truncated page) before one succeeds.
        self.flaky_closed = dict(flaky_closed or {})
        self.fail_post = set(fail_post)
        self.calls = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        path = next(a for a in argv if a.startswith("repos/"))
        repo = "/".join(path.split("/")[1:3])
        if "-X" in argv:
            if argv[argv.index("-X") + 1] == "POST":
                number = int(path.split("/issues/")[1].split("/")[0])
                if (repo, number) in self.fail_post:
                    return completed(stderr="HTTP 502", returncode=1)
                return completed()
            name = path.split("/environments/")[1]
            if name in self.fail_delete:
                return completed(stderr="HTTP 403", returncode=1)
            return completed()
        if "/environments" in path:
            return completed("\n".join(self.envs.get(repo, [])) + "\n")
        if "/pulls?state=closed" in path:
            if repo in self.fail_closed:
                return completed(stderr="HTTP 502", returncode=1)
            if self.flaky_closed.get(repo, 0) > 0:
                self.flaky_closed[repo] -= 1
                return completed(stderr="unexpected end of JSON input", returncode=1)
            # Mirrors the script's jq: both labels, every page.
            lines = []
            for pr in self.closed.get(repo, []) if "--paginate" in argv else []:
                customer = [n for n in pr["labels"] if n.startswith("customer:")]
                if customer and "deploy" in pr["labels"]:
                    lines.append(f"{pr['number']} {customer[0]}")
            return completed("\n".join(lines) + "\n")
        if "/comments" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            if (repo, number) in self.fail_comments:
                return completed(stderr="HTTP 502", returncode=1)
            # Page 1 holds no marker; later pages are only reached with --paginate.
            bodies = self.comments.get((repo, number), []) if "--paginate" in argv else []
            return completed("\n".join(bodies) + "\n")
        if repo in self.fail_prs:
            return completed(stderr="HTTP 502", returncode=1)
        return completed("\n".join(str(n) for n in self.prs.get(repo, [])) + "\n")

    def deleted(self):
        return [a[-1].removeprefix("repos/") for a in self.calls if "DELETE" in a]

    def _posts(self):
        return [a for a in self.calls if "POST" in a]

    def closed_listings(self):
        return [c for c in self.calls if any("/pulls?state=closed" in a for a in c)]

    def posted(self):
        out = []
        for a in self._posts():
            path = next(x for x in a if x.startswith("repos/"))
            out.append(("/".join(path.split("/")[1:3]), int(path.split("/issues/")[1].split("/")[0])))
        return out

    def posted_bodies(self):
        return [next(x for x in a if x.startswith("body=")).removeprefix("body=") for a in self._posts()]


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
        assert prune.prune(["mindsdb/auth", "mindsdb/cowork"], dry_run=False, runner=gh, sleep=no_sleep) == 1
        assert gh.deleted() == ["mindsdb/cowork/environments/pr-cowork-9"]

    def test_failed_delete_does_not_stop_the_rest(self):
        gh = FakeGh(envs={"mindsdb/auth": ["pr-auth-1", "pr-auth-2"]}, prs={"mindsdb/auth": []}, fail_delete={"pr-auth-1"})
        assert prune.prune(["mindsdb/auth"], dry_run=False, runner=gh) == 1
        assert gh.deleted() == ["mindsdb/auth/environments/pr-auth-1", "mindsdb/auth/environments/pr-auth-2"]


class TestCustomerReminders:
    def test_posts_once_per_closed_customer_pr(self):
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/auth": [{"number": 5, "labels": ["deploy", "customer:acme"]}, {"number": 6, "labels": ["deploy"]}]}, comments={})
        assert prune.customer_reminders("mindsdb/auth", gh, dry_run=False) is True
        assert gh.posted() == [("mindsdb/auth", 5)]
        body = gh.posted_bodies()[0]
        assert "<!-- customer-env-close-reminder -->" in body
        assert "`customer-envs/acme/`" in body
        assert "pr-auth-5" in body

    def test_reminder_names_the_envs_sam_stack(self):
        # The stack may already be gone when the reminder posts, so the line
        # says to delete it only if it is still there.
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/web_app": [{"number": 7, "labels": ["deploy", "customer:acme"]}]}, comments={})
        prune.customer_reminders("mindsdb/web_app", gh, dry_run=False)
        sam_line = next(line for line in gh.posted_bodies()[0].splitlines() if line.startswith("- SAM stack:"))
        assert "`anton-services-pr-web-app-7`" in sam_line
        assert sam_line.endswith("if it still exists")

    def test_reminder_asks_for_nothing_the_backup_vault_refuses(self):
        # The backup vault is locked, so no one can delete its recovery points.
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/web_app": [{"number": 7, "labels": ["deploy", "customer:acme"]}]}, comments={})
        prune.customer_reminders("mindsdb/web_app", gh, dry_run=False)
        postgres_line = next(line for line in gh.posted_bodies()[0].splitlines() if line.startswith("- Postgres:"))
        assert "recovery points" not in postgres_line
        assert postgres_line.endswith("(`kubectl get pv | grep pr-web-app-7`)")

    def test_customer_pr_that_never_deployed_is_not_reminded(self):
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/auth": [{"number": 5, "labels": ["customer:acme"]}]}, comments={})
        assert prune.customer_reminders("mindsdb/auth", gh, dry_run=False) is True
        assert gh.posted() == []

    def test_closed_prs_are_paginated(self):
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/auth": [{"number": 5, "labels": ["deploy", "customer:acme"]}]}, comments={})
        prune.customer_reminders("mindsdb/auth", gh, dry_run=False)
        listing = next(c for c in gh.calls if any("/pulls?state=closed" in a for a in c))
        assert "--paginate" in listing
        assert gh.posted() == [("mindsdb/auth", 5)]

    def test_unreadable_comments_skip_that_pr_and_fail_the_run(self):
        closed = {"mindsdb/auth": [{"number": 5, "labels": ["deploy", "customer:acme"]}, {"number": 6, "labels": ["deploy", "customer:beta"]}]}
        gh = FakeGh(envs={}, prs={}, closed=closed, comments={}, fail_comments={("mindsdb/auth", 5)})
        assert prune.customer_reminders("mindsdb/auth", gh, dry_run=False, sleep=no_sleep) is False
        assert gh.posted() == [("mindsdb/auth", 6)]

    def test_skips_prs_already_reminded(self):
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/auth": [{"number": 5, "labels": ["deploy", "customer:acme"]}]}, comments={("mindsdb/auth", 5): ["<!-- customer-env-close-reminder -->\nolder"]})
        assert prune.customer_reminders("mindsdb/auth", gh, dry_run=False) is True
        assert gh.posted() == []

    def test_dry_run_posts_nothing(self):
        gh = FakeGh(envs={}, prs={}, closed={"mindsdb/auth": [{"number": 5, "labels": ["deploy", "customer:acme"]}]}, comments={})
        assert prune.customer_reminders("mindsdb/auth", gh, dry_run=True) is True
        assert gh.posted() == []

    def test_failed_list_returns_false_and_posts_nothing(self):
        gh = FakeGh(envs={}, prs={}, closed={}, comments={}, fail_closed={"mindsdb/auth"})
        assert prune.customer_reminders("mindsdb/auth", gh, dry_run=False, sleep=no_sleep) is False
        assert gh.posted() == []

    def test_prune_runs_reminders_and_folds_failure(self):
        gh = FakeGh(envs={"mindsdb/auth": []}, prs={"mindsdb/auth": []}, fail_closed={"mindsdb/auth"})
        assert prune.prune(["mindsdb/auth"], dry_run=False, runner=gh, sleep=no_sleep) == 1

    def test_prune_posts_reminder_for_closed_customer_pr(self):
        gh = FakeGh(envs={"mindsdb/auth": []}, prs={"mindsdb/auth": []}, closed={"mindsdb/auth": [{"number": 5, "labels": ["deploy", "customer:acme"]}]})
        assert prune.prune(["mindsdb/auth"], dry_run=False, runner=gh) == 0
        assert gh.posted() == [("mindsdb/auth", 5)]


class TestRetries:
    def test_truncated_closed_pr_page_is_retried(self, capsys):
        # 21 Nov: one truncated --paginate page failed the whole hourly run.
        closed = {"mindsdb/example": [{"number": 5, "labels": ["deploy", "customer:acme"]}]}
        gh = FakeGh(envs={"mindsdb/example": []}, prs={"mindsdb/example": []}, closed=closed, flaky_closed={"mindsdb/example": 1})
        pauses = []
        assert prune.prune(["mindsdb/example"], dry_run=False, runner=gh, sleep=pauses.append) == 0
        assert len(gh.closed_listings()) == 2
        assert pauses == [2]
        assert "::error" not in capsys.readouterr().out
        assert gh.posted() == [("mindsdb/example", 5)]

    def test_read_failing_every_attempt_reports_as_before(self, capsys):
        gh = FakeGh(envs={"mindsdb/example": []}, prs={"mindsdb/example": []}, fail_closed={"mindsdb/example"})
        pauses = []
        assert prune.prune(["mindsdb/example"], dry_run=False, runner=gh, sleep=pauses.append) == 1
        assert len(gh.closed_listings()) == 3
        assert pauses == [2, 4]
        errors = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error")]
        assert errors == ["::error title=mindsdb/example::could not list closed PRs: HTTP 502"]

    def test_failed_reminder_post_is_not_repeated(self):
        closed = {"mindsdb/example": [{"number": 5, "labels": ["deploy", "customer:acme"]}]}
        gh = FakeGh(envs={}, prs={}, closed=closed, comments={}, fail_post={("mindsdb/example", 5)})
        assert prune.customer_reminders("mindsdb/example", gh, dry_run=False, sleep=no_sleep) is False
        assert gh.posted() == [("mindsdb/example", 5)]

    def test_failed_delete_is_not_repeated(self):
        gh = FakeGh(envs={"mindsdb/example": ["pr-example-1"]}, prs={"mindsdb/example": []}, fail_delete={"pr-example-1"})
        assert prune.prune(["mindsdb/example"], dry_run=False, runner=gh, sleep=no_sleep) == 1
        assert gh.deleted() == ["mindsdb/example/environments/pr-example-1"]
