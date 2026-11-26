"""scripts/write_pr_env_state.py against a local bare repository standing in for argocd-state."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import write_pr_env_state  # noqa: E402
from write_env_state import StateError  # noqa: E402

SHA = "8b69e1a864b0f730e4552cb5616f92a9578288fa"
OTHER_SHA = "d90ad897ac744f54b0eeb69f21deefdceeda2366"
ENV = "pr-cowork-server-12"
OWN = [f"tags.cowork-server=development-{SHA}", f"revisions.cowork-server={SHA}"]
EXISTING = {
    "tags": {"cowork-server": f"development-{OTHER_SHA}", "mindshub_stafftools": "staging"},
    "revisions": {"cowork-server": OTHER_SHA, "mindshub_stafftools": "staging", "mindshub_services": "main"},
    "services": {"apiHost": "abc123.execute-api.us-east-1.amazonaws.com"},
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def remote(tmp_path):
    """A bare argocd-state whose main holds one PR environment's file, and a clone of it."""
    bare = tmp_path / "argocd-state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
    git(seed, "config", "user.name", "seed")
    git(seed, "config", "user.email", "seed@example.com")
    git(seed, "checkout", "--quiet", "-b", "main")
    (seed / "pr-envs").mkdir()
    (seed / "pr-envs" / f"{ENV}.yaml").write_text(yaml.safe_dump(EXISTING, sort_keys=False))
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "push", "--quiet", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(clone)], check=True)
    git(clone, "config", "user.name", "bot")
    git(clone, "config", "user.email", "bot@example.com")
    return bare, seed, clone


def main_file(bare, env=ENV):
    out = subprocess.run(["git", "-C", str(bare), "show", f"main:pr-envs/{env}.yaml"], capture_output=True, text=True)
    return yaml.safe_load(out.stdout) if out.returncode == 0 else None


class TestParseSettings:
    def test_two_maps_from_the_settings(self):
        parsed = write_pr_env_state.parse_settings(OWN + ["tags.auth=staging", "revisions.auth=staging", "revisions.mindshub_services=main"])
        assert parsed == {
            "tags": {"cowork-server": f"development-{SHA}", "auth": "staging"},
            "revisions": {"cowork-server": SHA, "auth": "staging", "mindshub_services": "main"},
        }

    @pytest.mark.parametrize("setting", [
        "tags.auth", "auth=staging", "hostBase=x", "services.apiHost=x", "tags.Auth=staging",
        "tags.auth=-x", "revisions.auth=feature/x", "revisions.auth=d90ad89",
    ])
    def test_each_setting_has_a_shape(self, setting):
        with pytest.raises(StateError):
            write_pr_env_state.parse_settings([setting])

    def test_a_repeated_key_is_refused(self):
        with pytest.raises(StateError, match="set twice"):
            write_pr_env_state.parse_settings(["tags.auth=staging", "tags.auth=main"])


class TestUpdated:
    def test_replaces_both_maps_whole_and_keeps_the_rest(self):
        new = write_pr_env_state.updated(EXISTING, write_pr_env_state.parse_settings(OWN))
        assert new == {
            "tags": {"cowork-server": f"development-{SHA}"},
            "revisions": {"cowork-server": SHA},
            "services": EXISTING["services"],
        }
        assert list(new) == ["tags", "revisions", "services"]

    def test_a_dropped_link_drops_its_pin(self):
        # The staff console's two keys went with its Deploys: line; mindshub_services' revision with its line.
        new = write_pr_env_state.updated(EXISTING, write_pr_env_state.parse_settings(OWN))
        assert "mindshub_stafftools" not in new["tags"]
        assert "mindshub_services" not in new["revisions"]

    def test_host_base_comes_first_and_is_kept(self):
        new = write_pr_env_state.updated({"hostBase": "acme", "services": {"apiHost": ""}}, write_pr_env_state.parse_settings(OWN))
        assert list(new) == ["hostBase", "tags", "revisions", "services"]
        assert new["hostBase"] == "acme"

    def test_an_empty_map_is_left_out(self):
        new = write_pr_env_state.updated({}, write_pr_env_state.parse_settings(["revisions.mindshub_services=main"]))
        assert new == {"revisions": {"mindshub_services": "main"}}


class TestRecord:
    def test_commits_and_pushes_the_file(self, remote):
        bare, _seed, clone = remote
        pushed, message = write_pr_env_state.record(clone, "main", ENV, OWN)
        assert pushed and message.startswith(f"pr-envs/{ENV}.yaml: tags.cowork-server=development-{SHA}")
        assert main_file(bare) == {"tags": {"cowork-server": f"development-{SHA}"}, "revisions": {"cowork-server": SHA}, "services": EXISTING["services"]}
        assert git(bare, "log", "-1", "--format=%s", "main").strip() == f"{ENV}: development-{SHA}"

    def test_creates_the_file_on_the_first_deploy(self, remote):
        bare, _seed, clone = remote
        pushed, _ = write_pr_env_state.record(clone, "main", "pr-auth-3", ["tags.auth=development-abc", f"revisions.auth={SHA}"])
        assert pushed
        assert main_file(bare, "pr-auth-3") == {"tags": {"auth": "development-abc"}, "revisions": {"auth": SHA}}

    def test_nothing_to_do_pushes_nothing(self, remote):
        bare, _seed, clone = remote
        settings = [f"tags.cowork-server=development-{OTHER_SHA}", "tags.mindshub_stafftools=staging",
                    f"revisions.cowork-server={OTHER_SHA}", "revisions.mindshub_stafftools=staging", "revisions.mindshub_services=main"]
        before = git(bare, "rev-parse", "main")
        pushed, message = write_pr_env_state.record(clone, "main", ENV, settings)
        assert not pushed and "already records" in message
        assert git(bare, "rev-parse", "main") == before

    def test_the_env_name_and_the_settings_are_checked_before_any_git(self, tmp_path):
        with pytest.raises(StateError, match="not pr-<anchor>-<number>"):
            write_pr_env_state.record(tmp_path, "main", "cowork-server-12", OWN)
        with pytest.raises(StateError, match="not tags"):
            write_pr_env_state.record(tmp_path, "main", ENV, ["hostBase=acme"])

    def test_retries_when_the_branch_moves_during_the_push(self, remote, monkeypatch):
        bare, seed, clone = remote
        real_git = write_pr_env_state.push_change.__globals__["git"]
        raced = []

        def racing_git(repo_dir, *args, check=True):
            if args[0] == "push" and not raced:
                raced.append(True)
                (seed / "pr-envs" / "pr-auth-9.yaml").write_text("tags:\n  auth: staging\n")
                real_git(seed, "add", "-A")
                real_git(seed, "commit", "--quiet", "-m", "another deploy")
                real_git(seed, "push", "--quiet", "origin", "main")
            return real_git(repo_dir, *args, check=check)

        monkeypatch.setitem(write_pr_env_state.push_change.__globals__, "git", racing_git)
        pushed, message = write_pr_env_state.record(clone, "main", ENV, OWN)
        assert pushed and message.endswith("(attempt 2)")
        assert main_file(bare, "pr-auth-9") == {"tags": {"auth": "staging"}}
        assert main_file(bare)["tags"] == {"cowork-server": f"development-{SHA}"}
