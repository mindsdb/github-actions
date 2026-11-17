"""Unit tests for the main-to-staging merge (``scripts/sync_staging.py``).

Each test builds the history in a scratch repository and runs real git. The case
that matters is 17 Nov's: a hotfix lands on staging first, the same change is
cherry-picked onto main, and staging then edits the line next to it. A plain
`git merge` conflicts there, and the old step stopped on it. A merge that keeps
staging's content is only safe when staging holds every change main brings, so
the mixed case must still fail, and so must a change whose equal patch staging
made releases ago or has since reverted.
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

Outcome = sync_staging.Outcome

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
        self.write("flags.js", "NEW_BILLING=true\n")
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

    def release(self) -> None:
        """Squash staging into main, as the release PR does, then sync main back into staging."""
        self.checkout("main")
        self.git("merge", "-q", "--squash", "staging")
        self.commit("release: staging -> main")
        self.checkout("staging")
        self.git("merge", "-q", "--no-ff", "main", "-m", "Sync main back into staging after release")

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


def hold_a_conflicting_hotfix(repo: Repo) -> None:
    """17 Nov's shape: the hotfix on staging, its cherry-pick on main, then staging edits the next line."""
    fix = add_greeting_on_staging(repo)
    repo.cherry_pick_to_main(fix)
    edit_next_line_on_staging(repo)


def turn_billing_off_and_on_across_two_releases(repo: Repo) -> None:
    """Staging turns a flag off, ships it, turns it back on, and ships that too."""
    repo.checkout("staging")
    repo.write("flags.js", "NEW_BILLING=false\n")
    repo.commit("chore: turn new billing off")
    repo.release()
    repo.checkout("staging")
    repo.write("flags.js", "NEW_BILLING=true\n")
    repo.commit("feat: new billing back on")
    repo.release()


def turn_billing_off_on_main(repo: Repo) -> None:
    """A production hotfix with the same patch as the old staging commit."""
    repo.checkout("main")
    repo.write("flags.js", "NEW_BILLING=false\n")
    repo.commit("hotfix: turn new billing off")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_a_hotfix_staging_already_holds_lands_and_keeps_staging_content(repo):
    hold_a_conflicting_hotfix(repo)
    repo.checkout("staging")
    staging_tree = repo.git("rev-parse", "HEAD^{tree}")

    # The step before this change: a plain merge, which conflicts.
    assert subprocess.run(
        ["git", "merge", "--no-ff", "main", "-m", "x"], cwd=repo.path, capture_output=True
    ).returncode != 0
    repo.git("merge", "--abort")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.MERGED_AS_HELD
    assert result.conflicts == ("settings.js",)
    assert [commit.subject for commit in result.held] == ["fix: translate the greeting"]
    assert repo.git("rev-parse", "HEAD^{tree}") == staging_tree
    assert subprocess.run(["git", "merge-base", "--is-ancestor", "main", "HEAD"], cwd=repo.path).returncode == 0
    assert repo.git("log", "-1", "--format=%P").count(" ") == 1  # a real two-parent merge
    assert repo.git("log", "-1", "--format=%s") == "Sync main into staging"
    assert "fix: translate the greeting" in repo.git("log", "-1", "--format=%b")


def test_a_held_hotfix_lands_after_a_release(repo):
    repo.checkout("staging")
    repo.write("other.js", "one\ntwo\n")
    repo.commit("feat: other file")
    repo.release()
    hold_a_conflicting_hotfix(repo)
    repo.checkout("staging")
    staging_tree = repo.git("rev-parse", "HEAD^{tree}")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.MERGED_AS_HELD
    assert repo.git("rev-parse", "HEAD^{tree}") == staging_tree


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

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert result.conflicts == ("settings.js",)
    assert [commit.subject for commit in result.lacking] == ["fix: main-only change"]
    assert repo.git("rev-parse", "HEAD") == before
    assert repo.git("status", "--porcelain") == ""


def test_an_old_staging_commit_with_the_same_patch_does_not_vouch_for_a_main_change(repo):
    turn_billing_off_and_on_across_two_releases(repo)
    hold_a_conflicting_hotfix(repo)
    turn_billing_off_on_main(repo)
    repo.checkout("staging")
    before = repo.git("rev-parse", "HEAD")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert [commit.subject for commit in result.lacking] == ["hotfix: turn new billing off"]
    assert repo.git("rev-parse", "HEAD") == before


def test_an_old_staging_commit_does_not_vouch_for_a_main_change_that_conflicts_too(repo):
    turn_billing_off_and_on_across_two_releases(repo)
    hold_a_conflicting_hotfix(repo)
    # Staging adds a flag under the old one, so main's change to it conflicts and `-X ours` would hide it.
    repo.checkout("staging")
    repo.write("flags.js", "NEW_BILLING=true\nNEW_SEARCH=true\n")
    repo.commit("feat: new search flag")
    turn_billing_off_on_main(repo)
    repo.checkout("staging")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert result.conflicts == ("flags.js", "settings.js")
    assert [commit.subject for commit in result.lacking] == ["hotfix: turn new billing off"]
    assert result.differs == ()


def test_a_change_staging_reverted_fails_although_its_patch_matches(repo):
    repo.checkout("staging")
    repo.write("flags.js", "NEW_BILLING=false\n")
    off = repo.commit("chore: turn new billing off")
    repo.cherry_pick_to_main(off)
    repo.checkout("staging")
    repo.git("revert", "--no-edit", off)
    hold_a_conflicting_hotfix(repo)
    repo.checkout("staging")
    before = repo.git("rev-parse", "HEAD")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert result.lacking == ()
    assert result.differs == ("flags.js",)
    assert repo.git("rev-parse", "HEAD") == before


def test_a_conflict_ours_cannot_settle_needs_a_person(repo):
    # Main gets a change staging made, then staging deletes the file: a modify/delete conflict.
    repo.checkout("staging")
    repo.write("other.js", "one\ntwo\n")
    change = repo.commit("feat: other file")
    repo.cherry_pick_to_main(change)
    repo.checkout("staging")
    repo.git("rm", "-q", "other.js")
    repo.commit("chore: drop other file")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert [commit.subject for commit in result.held] == ["feat: other file"]
    assert result.differs == ("other.js",)


def test_a_deletion_staging_undid_needs_a_person(repo):
    # Main gets a deletion staging made, then staging brings the file back edited: delete/modify.
    # `-X ours` leaves staging's copy in the tree, so only the unsettled conflict shows the gap.
    repo.checkout("staging")
    repo.git("rm", "-q", "other.js")
    drop = repo.commit("chore: drop other file")
    repo.cherry_pick_to_main(drop)
    repo.checkout("staging")
    repo.write("other.js", "one\ntwo\n")
    repo.commit("feat: other file is back")
    hold_a_conflicting_hotfix(repo)
    repo.checkout("staging")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert not result.lacking
    assert result.differs == ("other.js",)


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

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert [commit.subject for commit in result.merges] == ["Merge hotfix"]


def test_a_conflicting_path_with_a_space_stays_one_name(repo):
    repo.checkout("staging")
    repo.write("app settings.js", "staging\n")
    repo.commit("feat: staging's settings")
    repo.checkout("main")
    repo.write("app settings.js", "main\n")
    repo.commit("fix: main's settings")
    repo.checkout("staging")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.NEEDS_A_PERSON
    assert result.conflicts == ("app settings.js",)


def test_a_clean_merge_is_an_ordinary_merge(repo):
    repo.checkout("main")
    repo.write("other.js", "one\ntwo\n")
    repo.commit("fix: other file")
    edit_next_line_on_staging(repo)
    repo.checkout("staging")

    result = sync_staging.sync(repo.runner, "main", "Sync main into staging")

    assert result.outcome is Outcome.MERGED
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


def test_main_names_the_files_a_reverted_change_would_alter(repo, capsys):
    repo.checkout("staging")
    repo.write("flags.js", "NEW_BILLING=false\n")
    off = repo.commit("chore: turn new billing off")
    repo.cherry_pick_to_main(off)
    repo.checkout("staging")
    repo.git("revert", "--no-edit", off)
    hold_a_conflicting_hotfix(repo)
    repo.checkout("staging")

    code = sync_staging.main(["--base", "main", "--message", "Sync main into staging"], runner=repo.runner)

    out = capsys.readouterr().out
    assert code == 1
    assert "would still change staging's flags.js" in out


def test_a_merge_that_fails_without_conflicts_raises(repo):
    repo.checkout("staging")

    with pytest.raises(RuntimeError, match="no-such-ref"):
        sync_staging.sync(repo.runner, "no-such-ref", "Sync")
