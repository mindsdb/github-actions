"""Unit tests for the main-to-staging merge (``scripts/sync_staging.py``).

Each test builds the history in a scratch repository and runs real git. The case
that matters is 17 Nov's: a hotfix lands on staging first, the same change is
cherry-picked onto main, and staging then edits the line next to it. A plain
`git merge` conflicts there, and the old step stopped on it. A merge that keeps
staging's content is only safe when staging holds every change main brings, so
the mixed case must still fail.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_staging.py"
_spec = importlib.util.spec_from_file_location("sync_staging", _PATH)
sync_staging = importlib.util.module_from_spec(_spec)
# A dataclass looks its module up in sys.modules while it is defined.
sys.modules["sync_staging"] = sync_staging
_spec.loader.exec_module(sync_staging)

IMPORTS = "import a\nimport urls_v1\nimport b\n"


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Release Bot")
        self.git("config", "commit.gpgsign", "false")
        self.write("settings.js", IMPORTS)
        self.write("other.js", "one\n")
        self.commit("initial")
        self.git("branch", "staging")

    def git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, check=check)
        return result.stdout.strip()

    def write(self, name: str, text: str) -> None:
        (self.path / name).write_text(text)

    def read(self, name: str) -> str:
        return (self.path / name).read_text()

    def commit(self, subject: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def checkout(self, branch: str) -> None:
        self.git("checkout", "-q", branch)

    def cherry_pick_to_main(self, sha: str) -> None:
        self.checkout("main")
        self.git("cherry-pick", "-x", sha)

    def runner(self, argv):
        return subprocess.run(list(argv), cwd=self.path, capture_output=True, text=True, check=False)


def add_greeting_on_staging(repo: Repo) -> str:
    """The hotfix's staging PR: add an import above the urls line."""
    repo.checkout("staging")
    repo.write("settings.js", "import a\nimport greeting\nimport urls_v1\nimport b\n")
    return repo.commit("fix: translate the greeting")


def edit_next_line_on_staging(repo: Repo) -> None:
    """A later staging PR rewrites the line under the hotfix's import."""
    repo.checkout("staging")
    repo.write("settings.js", "import a\nimport greeting\nimport urls_v2\nimport b\n")
    repo.commit("feat: api keys in the menu")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_a_hotfix_staging_already_holds_lands_and_keeps_staging_content(repo):
    fix = add_greeting_on_staging(repo)
    repo.cherry_pick_to_main(fix)
    edit_next_line_on_staging(repo)
    repo.checkout("staging")
    staging_tree = repo.git("rev-parse", "HEAD^{tree}")

    # The step before this change: a plain merge, which conflicts.
    assert subprocess.run(
        ["git", "merge", "--no-ff", "main", "-m", "x"], cwd=repo.path, capture_output=True
    ).returncode != 0
    repo.git("merge", "--abort")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome == sync_staging.MERGED_AS_HELD
    assert result.conflicts == ("settings.js",)
    assert [line.split(" ", 1)[1] for line in result.held] == ["fix: translate the greeting"]
    assert repo.git("rev-parse", "HEAD^{tree}") == staging_tree
    assert subprocess.run(["git", "merge-base", "--is-ancestor", "main", "HEAD"], cwd=repo.path).returncode == 0
    assert repo.git("log", "-1", "--format=%P").count(" ") == 1  # a real two-parent merge


def test_a_conflict_on_a_change_staging_lacks_fails_and_leaves_staging_unchanged(repo):
    fix = add_greeting_on_staging(repo)
    repo.cherry_pick_to_main(fix)
    # Main also gets a change staging never saw, on the same lines.
    repo.write("settings.js", "import a\nimport greeting\nimport urls_main_only\nimport b\n")
    repo.commit("fix: main-only change")
    edit_next_line_on_staging(repo)
    repo.checkout("staging")
    before = repo.git("rev-parse", "HEAD")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome == sync_staging.NEEDS_A_PERSON
    assert result.conflicts == ("settings.js",)
    assert [line.split(" ", 1)[1] for line in result.lacking] == ["fix: main-only change"]
    assert repo.git("rev-parse", "HEAD") == before
    assert repo.git("status", "--porcelain") == ""


def test_a_conflict_with_a_merge_commit_on_main_fails(repo):
    fix = add_greeting_on_staging(repo)
    repo.checkout("main")
    repo.git("checkout", "-q", "-b", "hotfix")
    repo.git("cherry-pick", "-x", fix)
    repo.checkout("main")
    repo.git("merge", "-q", "--no-ff", "hotfix", "-m", "Merge hotfix")
    edit_next_line_on_staging(repo)
    repo.checkout("staging")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome == sync_staging.NEEDS_A_PERSON
    assert any("merge commit" in line for line in result.lacking)


def test_a_clean_merge_is_an_ordinary_merge(repo):
    repo.checkout("main")
    repo.write("other.js", "one\ntwo\n")
    repo.commit("fix: other file")
    edit_next_line_on_staging(repo)
    repo.checkout("staging")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome == sync_staging.MERGED
    assert repo.read("other.js") == "one\ntwo\n"
    assert repo.read("settings.js") == "import a\nimport greeting\nimport urls_v2\nimport b\n"


def test_main_exits_1_with_an_error_naming_files_and_commits(repo, capsys):
    fix = add_greeting_on_staging(repo)
    repo.cherry_pick_to_main(fix)
    repo.write("settings.js", "import a\nimport greeting\nimport urls_main_only\nimport b\n")
    repo.commit("fix: main-only change")
    edit_next_line_on_staging(repo)
    repo.checkout("staging")

    code = sync_staging.main(["--base", "main", "--message", "Sync main into staging"], runner=repo.runner)

    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("::error::")
    assert "settings.js" in out
    assert "fix: main-only change" in out


def test_a_merge_that_fails_without_conflicts_raises(repo):
    repo.checkout("staging")

    with pytest.raises(RuntimeError, match="no-such-ref"):
        sync_staging.sync(repo.runner, "no-such-ref", "Sync")
