"""Unit tests for the release PR's commit list (``scripts/release_commits.py``).

Each test builds a real git history in a scratch repository and answers only the
`gh pr list` call from a fake. The history that matters is the squash-merge
release: staging's shipped commits never reach main, so a list built from
`main..staging` grows with every release. The three-release test fails on that
range and passes on the fixed one.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release_commits.py"
_spec = importlib.util.spec_from_file_location("release_commits", _PATH)
release_commits = importlib.util.module_from_spec(_spec)
# A dataclass looks its module up in sys.modules while it is defined.
sys.modules["release_commits"] = release_commits
_spec.loader.exec_module(release_commits)

REPO = "mindsdb/example"


class History:
    """A scratch repository with `main` and `staging`, published as `origin/*` refs."""

    def __init__(self, path: Path):
        self.path = path
        self.released_heads: list[str] = []
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Release Bot")
        self.git("config", "commit.gpgsign", "false")
        self.commit("initial", author="Founder")
        self.git("branch", "staging")
        self.publish()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def commit(self, subject: str, author: str = "Dev One") -> str:
        (self.path / f"{subject.replace(' ', '_')}.txt").write_text(subject)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject, "--author", f"{author} <{author.replace(' ', '')}@example.com>")
        return self.git("rev-parse", "HEAD")

    def on_staging(self, *subjects: str, author: str = "Dev One") -> None:
        self.git("checkout", "-q", "staging")
        for subject in subjects:
            self.commit(subject, author=author)
        self.publish()

    def squash_release(self) -> None:
        """Squash staging onto main, then sync main back into staging, as the train does."""
        head = self.git("rev-parse", "staging")
        tree = self.git("rev-parse", "staging^{tree}")
        main = self.git("rev-parse", "main")
        squash = self.git("commit-tree", tree, "-p", main, "-m", "Release: staging -> main")
        self.git("update-ref", "refs/heads/main", squash)
        self.git("checkout", "-q", "staging")
        self.git("merge", "-q", "--no-ff", "main", "-m", "Sync main into staging")
        self.released_heads.append(head)
        self.publish()

    def merge_release(self) -> None:
        """Merge staging into main with a merge commit, then sync main back into staging."""
        head = self.git("rev-parse", "staging")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "staging", "-m", "Release: staging -> main")
        self.git("checkout", "-q", "staging")
        self.git("merge", "-q", "--ff-only", "main")
        self.released_heads.append(head)
        self.publish()

    def publish(self) -> None:
        for branch in ("main", "staging"):
            self.git("update-ref", f"refs/remotes/origin/{branch}", self.git("rev-parse", branch))

    def unreleased(self, *excluded: str) -> list[str]:
        return self.git("log", "--format=%s", "--no-merges", "origin/staging", *excluded).splitlines()


def runner_for(history: History, gh_answer=None, gh_fails=False):
    """Run git for real inside the scratch repository and answer `gh pr list` from the history."""
    calls = []

    def runner(argv):
        argv = list(argv)
        calls.append(argv)
        if argv[0] == "gh":
            if gh_fails:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 502")
            prs = gh_answer if gh_answer is not None else [
                {"headRefOid": sha, "mergedAt": f"2026-11-{10 + i:02d}T00:00:00Z"}
                for i, sha in enumerate(history.released_heads)
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(prs), "")
        return subprocess.run(argv, cwd=history.path, capture_output=True, text=True, check=False)

    runner.calls = calls
    return runner


def subjects(result) -> list[str]:
    return [line.split(" ", 1)[1] for line in result.commits.splitlines()]


@pytest.fixture
def history(tmp_path):
    return History(tmp_path)


def test_squash_releases_list_only_what_the_last_release_did_not_ship(history):
    for week in range(3):
        history.on_staging(f"week {week} a", f"week {week} b")
        history.squash_release()
    history.on_staging("after release c", "after release d", author="Dev Two")

    # The range the workflow used before: every commit since the first release.
    assert len(history.unreleased("^origin/main")) == 8

    result = release_commits.release_commits(runner_for(history), REPO, "main", "staging")

    assert subjects(result) == ["after release d", "after release c"]
    assert result.count == 2
    assert result.omitted == 0
    assert result.since == history.released_heads[-1]
    assert result.contributors == "- Dev Two"
    assert result.lookup_failed is False


def test_merge_commit_releases_list_the_same_commits_as_main_dot_dot_staging(history):
    history.on_staging("week 0 a", "week 0 b")
    history.merge_release()
    history.on_staging("after release c")

    result = release_commits.release_commits(runner_for(history), REPO, "main", "staging")

    assert subjects(result) == history.unreleased("^origin/main") == ["after release c"]


def test_a_hotfix_main_already_has_is_not_listed_twice(history):
    history.on_staging("week 0 a")
    history.squash_release()
    history.on_staging("fix x on staging")
    # The hotfix lands on main as its own commit, and the sync merges it into staging.
    history.git("checkout", "-q", "main")
    history.commit("fix x on main")
    history.git("checkout", "-q", "staging")
    history.git("merge", "-q", "--no-ff", "main", "-m", "Sync main into staging")
    history.publish()

    result = release_commits.release_commits(runner_for(history), REPO, "main", "staging")

    assert subjects(result) == ["fix x on staging"]


def test_no_previous_release_lists_main_dot_dot_staging(history):
    history.on_staging("first a", "first b")

    result = release_commits.release_commits(runner_for(history, gh_answer=[]), REPO, "main", "staging")

    assert result.count == 2
    assert result.since == history.git("rev-parse", "origin/main")
    assert "no merged staging -> main PR yet" in result.since_reason


def test_a_release_head_staging_no_longer_holds_falls_back_to_main(history):
    history.on_staging("week 0 a")
    history.squash_release()
    history.on_staging("after release b")
    gone = [{"headRefOid": "0" * 40, "mergedAt": "2026-11-20T00:00:00Z"}]

    result = release_commits.release_commits(runner_for(history, gh_answer=gone), REPO, "main", "staging")

    assert result.count == 2
    assert "is not on origin/staging" in result.since_reason
    assert result.lookup_failed is False


def test_a_failed_lookup_falls_back_to_main_and_is_flagged(history):
    history.on_staging("week 0 a")
    history.squash_release()
    history.on_staging("after release b")

    result = release_commits.release_commits(runner_for(history, gh_fails=True), REPO, "main", "staging")

    assert result.lookup_failed is True
    assert result.count == 2
    assert "HTTP 502" in result.since_reason


def test_the_newest_merged_release_wins_whatever_order_gh_returns(history):
    history.on_staging("week 0 a")
    history.squash_release()
    history.on_staging("week 1 a")
    history.squash_release()
    history.on_staging("after release b")
    older, newer = history.released_heads
    answer = [
        {"headRefOid": newer, "mergedAt": "2026-11-15T23:34:06Z"},
        {"headRefOid": older, "mergedAt": "2026-11-09T03:31:12Z"},
    ][::-1]

    result = release_commits.release_commits(runner_for(history, gh_answer=answer), REPO, "main", "staging")

    assert result.since == newer
    assert subjects(result) == ["after release b"]


def test_a_list_over_the_budget_is_cut_and_counts_what_it_cut(history):
    history.on_staging(*[f"change {i:03d} with a reasonably long subject line" for i in range(40)])

    result = release_commits.release_commits(
        runner_for(history, gh_answer=[]), REPO, "main", "staging", max_chars=500
    )

    assert len(result.commits) <= 500
    assert result.count == 40
    assert result.omitted == 40 - len(result.commits.splitlines())
    assert result.omitted > 0


def test_main_prints_one_json_object(history, capsys, monkeypatch):
    # CI sets this for its own steps; the test must not write into that summary.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    history.on_staging("first a")

    assert release_commits.main(
        ["--repo", REPO, "--base", "main", "--staging", "staging"], runner=runner_for(history, gh_answer=[])
    ) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert set(out) == {
        "since", "since_reason", "lookup_failed", "count", "commits", "all_commits", "omitted", "contributors"
    }


def test_a_cut_list_still_reaches_the_run_summary_in_full(history, capsys, monkeypatch, tmp_path_factory):
    history.on_staging(*[f"change {i:03d} with a reasonably long subject line" for i in range(40)])
    summary = tmp_path_factory.mktemp("summary") / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert release_commits.main(
        ["--repo", REPO, "--base", "main", "--staging", "staging", "--max-chars", "500"],
        runner=runner_for(history, gh_answer=[]),
    ) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["omitted"] > 0
    written = summary.read_text(encoding="utf-8")
    assert "### Release PR list: 40 commit(s)" in written
    assert sum("reasonably long subject line" in line for line in written.splitlines()) == 40
