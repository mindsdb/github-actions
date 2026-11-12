"""scripts/write_env_state.py against a local bare repository standing in for argocd-state."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import write_env_state  # noqa: E402

SHA = "8b69e1a864b0f730e4552cb5616f92a9578288fa"
OTHER_SHA = "d90ad897ac744f54b0eeb69f21deefdceeda2366"
STATE = (
    "repo: https://github.com/mindsdb/scratchpad-controller.git\n"
    "chart: deployment/scratchpad-controller\n"
    "release: scratchpad-controller\n"
    "# the image CI last built\n"
    f"tag: development-head-{OTHER_SHA}\n"
    f"revision: {OTHER_SHA}\n"
)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def remote(tmp_path):
    """A bare argocd-state with main holding dev/scratchpad-controller.yaml, and a clone of it."""
    bare = tmp_path / "argocd-state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
    git(seed, "config", "user.name", "seed")
    git(seed, "config", "user.email", "seed@example.com")
    git(seed, "checkout", "--quiet", "-b", "main")
    (seed / "dev").mkdir()
    (seed / "dev" / "scratchpad-controller.yaml").write_text(STATE)
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "push", "--quiet", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(clone)], check=True)
    git(clone, "config", "user.name", "bot")
    git(clone, "config", "user.email", "bot@example.com")
    return bare, seed, clone


def main_file(bare, path="dev/scratchpad-controller.yaml"):
    return subprocess.run(["git", "-C", str(bare), "show", f"main:{path}"], capture_output=True, text=True, check=True).stdout


class TestUpdatedText:
    def test_replaces_only_the_two_lines(self):
        out = write_env_state.updated_text(STATE, f"development-head-{SHA}", SHA)
        assert out == STATE.replace(OTHER_SHA, SHA)
        assert "# the image CI last built\n" in out

    def test_a_file_without_both_lines_is_refused(self):
        with pytest.raises(write_env_state.StateError, match="no revision line"):
            write_env_state.updated_text("tag: x\n", "y", SHA)
        with pytest.raises(write_env_state.StateError, match="no revision or tag line"):
            write_env_state.updated_text("repo: x\n", "y", SHA)


class TestCheck:
    @pytest.mark.parametrize("field, value", [
        ("env", "Dev"), ("service", "my_service"), ("tag", "-x"), ("tag", "a b"),
        ("revision", "main"), ("revision", SHA[:7]), ("revision", SHA.upper()),
    ])
    def test_each_value_has_a_shape(self, field, value):
        values = {"env": "dev", "service": "scratchpad-controller", "tag": "development-head-" + SHA, "revision": SHA}
        values[field] = value
        with pytest.raises(write_env_state.StateError, match=f"{field} "):
            write_env_state.check(**values)


class TestRecord:
    def test_commits_and_pushes_the_edit(self, remote):
        bare, _, clone = remote
        message = write_env_state.record(clone, "main", "dev", "scratchpad-controller", f"development-head-{SHA}", SHA)
        assert message.startswith("dev/scratchpad-controller.yaml: tag development-head-")
        assert main_file(bare) == STATE.replace(OTHER_SHA, SHA)
        assert git(bare, "log", "-1", "--format=%s", "main") == f"dev/scratchpad-controller: development-head-{SHA}\n"

    def test_nothing_to_do_pushes_nothing(self, remote):
        bare, _, clone = remote
        before = git(bare, "rev-parse", "main")
        message = write_env_state.record(clone, "main", "dev", "scratchpad-controller", f"development-head-{OTHER_SHA}", OTHER_SHA)
        assert message.endswith(f"already records development-head-{OTHER_SHA} at {OTHER_SHA}")
        assert git(bare, "rev-parse", "main") == before

    def test_a_missing_file_is_refused_not_created(self, remote):
        bare, _, clone = remote
        with pytest.raises(write_env_state.StateError, match="dev/auth.yaml does not exist in argocd-state; adding a service is a reviewed change"):
            write_env_state.record(clone, "main", "dev", "auth", "x", SHA)
        assert "dev/auth.yaml" not in git(bare, "ls-tree", "-r", "--name-only", "main")

    def test_starts_from_the_remote_branch_not_the_stale_clone(self, remote):
        # Another service deployed after this clone was made: both edits land.
        bare, seed, clone = remote
        (seed / "dev" / "auth.yaml").write_text(STATE)
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "dev/auth: x")
        git(seed, "push", "--quiet", "origin", "main")
        write_env_state.record(clone, "main", "dev", "scratchpad-controller", f"development-head-{SHA}", SHA)
        assert "dev/auth.yaml" in git(bare, "ls-tree", "-r", "--name-only", "main")
        assert main_file(bare) == STATE.replace(OTHER_SHA, SHA)

    def test_retries_when_the_branch_moves_during_the_push(self, remote, monkeypatch):
        # The remote gains a commit between this attempt's fetch and its push, as a
        # concurrent deploy does; the push is rejected and the next attempt lands on top.
        bare, seed, clone = remote
        real_git = write_env_state.git
        raced = []

        def racing_git(repo_dir, *args, check=True):
            if args[0] == "push" and not raced:
                raced.append(True)
                (seed / "dev" / "auth.yaml").write_text(STATE)
                git(seed, "add", "-A")
                git(seed, "commit", "--quiet", "-m", "dev/auth: x")
                git(seed, "push", "--quiet", "origin", "main")
            return real_git(repo_dir, *args, check=check)

        monkeypatch.setattr(write_env_state, "git", racing_git)
        message = write_env_state.record(clone, "main", "dev", "scratchpad-controller", f"development-head-{SHA}", SHA)
        assert message.endswith("(attempt 2)")
        assert main_file(bare) == STATE.replace(OTHER_SHA, SHA)
        assert "dev/auth.yaml" in git(bare, "ls-tree", "-r", "--name-only", "main")

    def test_gives_up_after_the_attempts(self, remote, monkeypatch):
        bare, seed, clone = remote
        real_git = write_env_state.git
        pushes = []

        def always_racing_git(repo_dir, *args, check=True):
            if args[0] == "push":
                pushes.append(True)
                (seed / "dev" / f"auth{len(pushes)}.yaml").write_text(STATE)
                git(seed, "add", "-A")
                git(seed, "commit", "--quiet", "-m", "race")
                git(seed, "push", "--quiet", "origin", "main")
            return real_git(repo_dir, *args, check=check)

        monkeypatch.setattr(write_env_state, "git", always_racing_git)
        with pytest.raises(write_env_state.StateError, match="gave up after 2 attempts"):
            write_env_state.record(clone, "main", "dev", "scratchpad-controller", f"development-head-{SHA}", SHA, attempts=2)
        assert len(pushes) == 2


class TestMain:
    def test_a_refusal_is_an_error_annotation_and_exit_1(self, remote, capsys):
        _, _, clone = remote
        rc = write_env_state.main(["--repo-dir", str(clone), "--env", "dev", "--service", "scratchpad-controller", "--tag", "x", "--revision", "main"])
        assert rc == 1
        assert capsys.readouterr().out.startswith("::error::revision 'main' is not a 40-character commit sha")


class TestAction:
    def test_the_action_calls_the_script_with_every_input(self):
        import yaml
        action = yaml.safe_load((Path(__file__).resolve().parents[1] / "write-env-state" / "action.yml").read_text())
        assert set(action["inputs"]) == {"env", "service", "tag", "revision", "token", "repository", "branch"}
        run = action["runs"]["steps"][-1]["run"]
        for flag in ("--repo-dir argocd-state", '--branch "$BRANCH"', '--env "$ENV_NAME"', '--service "$SERVICE"', '--tag "$TAG"', '--revision "$REVISION"'):
            assert flag in run
