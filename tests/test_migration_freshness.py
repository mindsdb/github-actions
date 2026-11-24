"""Unit tests for the migration-freshness rule (``scripts/migration_freshness.py``).

What is worth pinning is the direction of each mistake. A check that calls a
protected pull request unprotected, or a stale head fresh, lets two migration
heads land on the base, which is the outage this exists to stop. A check that
calls an unrelated pull request protected makes it update for nothing, which is
the cost the rule was scoped to avoid. A lookup that fails must fail the check,
never pass it.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import yaml

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migration_freshness.py"
_spec = importlib.util.spec_from_file_location("migration_freshness", _PATH)
mf = importlib.util.module_from_spec(_spec)
# Dataclasses resolve their annotations through sys.modules.
sys.modules["migration_freshness"] = mf
_spec.loader.exec_module(mf)
# The wait a real run uses, kept before the `slept` fixture replaces it in every test.
REAL_SLEEP = mf.sleep

REPO = "acme/app"
TIP = "a" * 40
HEAD = "b" * 40
OTHER_HEAD = "c" * 40

ALEMBIC = mf.Rules(paths=("app/db/alembic/versions/*",))
DJANGO = mf.Rules(paths=("*/migrations/*", "docs/openapi.yaml"))
SKILLS = mf.Rules(
    paths=("app/skills_builtin/*",),
    constant_file="app/services/skills.py",
    constant_pattern=r"^(CODE_)?BUILTIN_SKILLS_VERSION\s*=",
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def pages(items):
    """A listing as `gh api --paginate --slurp` prints it, one item per page."""
    return [[item] for item in items] or [[]]


class FakeGh:
    """Answers `gh api` calls by endpoint and records every call.

    It routes on the endpoint's path and checks the query only where a wrong
    one asks another question: the open pull requests into this base. Each
    listing must ask for every page, and comes back one item per page, so a
    reader that stops at the first page misses the rest.
    """

    def __init__(self, *, tip=TIP, base="staging", files=None, behind=None, pulls=(), statuses=None, fail=(),
                 flaky=None):
        self.tip = tip
        self.base = base  # the base the open pull request listing must ask for
        self.files = files or {}  # pull number -> list of file dicts
        self.behind = behind or {}  # head sha -> behind_by
        self.pulls = list(pulls)  # (number, head sha)
        self.statuses = statuses or {}  # head sha -> list of status dicts, newest first
        self.fail = set(fail)  # endpoint substrings that always answer with an error
        self.flaky = dict(flaky or {})  # endpoint substring -> errors to answer before it succeeds
        self.calls = []
        self.posted = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        assert argv[:2] == ["gh", "api"]
        args = argv[2:]
        # Without --slurp, gh prints the pages one after another, not as one array.
        assert "--paginate" not in args or "--slurp" in args, argv
        endpoint = next(a for a in args if a.startswith("repos/"))
        path, _, query = endpoint.partition("?")
        for marker in self.fail:
            if marker in endpoint:
                return completed(stdout='{"message":"Server Error"}', returncode=1)
        for marker, left in self.flaky.items():
            if marker in endpoint and left:
                self.flaky[marker] = left - 1
                return completed(stdout='{"message":"Bad Gateway"}', returncode=1)
        if "--method" in args:
            fields = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f")
            self.posted.append((path.rsplit("/", 1)[1], fields))
            return completed(stdout="{}")
        if "/git/ref/heads/" in path:
            return completed(stdout=json.dumps({"object": {"sha": self.tip}}))
        if "/compare/" in path:
            head = path.rsplit("...", 1)[1]
            return completed(stdout=json.dumps({"behind_by": self.behind.get(head, 0)}))
        if path.endswith("/files"):
            assert "--paginate" in args, argv
            number = int(path.split("/pulls/")[1].split("/")[0])
            return completed(stdout=json.dumps(pages(self.files.get(number, []))))
        if path.endswith("/pulls"):
            assert "--paginate" in args, argv
            params = parse_qs(query)
            assert (params.get("state"), params.get("base")) == (["open"], [self.base]), endpoint
            return completed(stdout=json.dumps(pages([{"number": n, "head": {"sha": sha}} for n, sha in self.pulls])))
        if path.endswith("/statuses"):
            sha = path.split("/commits/")[1].split("/")[0]
            return completed(stdout=json.dumps(self.statuses.get(sha, [])))
        raise AssertionError(f"unexpected call {argv}")

    def endpoints(self):
        """Each call's endpoint path, without its query."""
        return [next(a for a in call[2:] if a.startswith("repos/")).partition("?")[0] for call in self.calls]


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Every wait a retry asks for, recorded instead of slept."""
    waits = []
    monkeypatch.setattr(mf, "sleep", waits.append)
    return waits


def changed(filename, patch="@@ -1 +1 @@\n-x\n+y", previous_filename=None):
    item = {"filename": filename, "patch": patch}
    if previous_filename:
        item["previous_filename"] = previous_filename
    return item


def files(*items):
    return [mf.ChangedFile(i["filename"], i.get("previous_filename"), i.get("patch")) for i in items]


# --- scope -------------------------------------------------------------------


def test_a_new_alembic_revision_is_protected():
    assert mf.scope(files(changed("app/db/alembic/versions/7b2d4f6a8c1e_add.py")), ALEMBIC).protected


def test_an_unrelated_change_is_not_protected():
    found = mf.scope(files(changed("README.md"), changed("app/api/routes.py")), ALEMBIC)
    assert not found.protected


def test_a_migration_renamed_out_of_the_folder_is_protected():
    moved = changed("app/db/old_revision.py", previous_filename="app/db/alembic/versions/old_revision.py")
    assert mf.scope(files(moved), ALEMBIC).protected


def test_every_django_app_and_depth_is_covered():
    for name in ("keys/migrations/0063_a.py", "billing/sub/migrations/0001_initial.py", "docs/openapi.yaml"):
        assert mf.scope(files(changed(name)), DJANGO).protected, name
    assert not mf.scope(files(changed("keys/models.py")), DJANGO).protected


def test_a_version_constant_bump_is_protected():
    patch = "@@ -140,3 +140,3 @@\n-BUILTIN_SKILLS_VERSION = 41\n+BUILTIN_SKILLS_VERSION = 42\n"
    assert mf.scope(files(changed("app/services/skills.py", patch)), SKILLS).protected
    patch = "@@ -1 +1 @@\n-CODE_BUILTIN_SKILLS_VERSION = 3\n+CODE_BUILTIN_SKILLS_VERSION = 4\n"
    assert mf.scope(files(changed("app/services/skills.py", patch)), SKILLS).protected


def test_an_edit_beside_the_constant_is_not_protected():
    # The constant appears only as an unchanged context line.
    patch = "@@ -139,3 +139,3 @@\n BUILTIN_SKILLS_VERSION = 41\n-def load():\n+def load_all():\n"
    assert not mf.scope(files(changed("app/services/skills.py", patch)), SKILLS).protected


def test_the_constant_in_another_file_is_not_protected():
    patch = "@@ -1 +1 @@\n-BUILTIN_SKILLS_VERSION = 41\n+BUILTIN_SKILLS_VERSION = 42\n"
    assert not mf.scope(files(changed("tests/test_skills.py", patch)), SKILLS).protected


def test_a_changed_line_that_starts_with_two_dashes_is_read():
    # A per-file patch has no ---/+++ headers, so "--- ..." is a removed line whose text starts with "--".
    rules = mf.Rules(paths=("db/migrations/*",), constant_file="db/version.sql", constant_pattern=r"^-- SCHEMA_VERSION")
    patch = "@@ -1,2 +1 @@\n--- SCHEMA_VERSION 3\n keep\n"
    assert mf.scope(files(changed("db/version.sql", patch)), rules).protected


def test_a_constant_bump_in_a_renamed_constant_file_is_protected():
    patch = "@@ -1 +1 @@\n-BUILTIN_SKILLS_VERSION = 41\n+BUILTIN_SKILLS_VERSION = 42\n"
    moved = changed("app/services/skill_store.py", patch, previous_filename="app/services/skills.py")
    assert mf.scope(files(moved), SKILLS).protected


def test_a_constant_file_without_a_patch_counts_as_protected():
    # GitHub drops the patch of a large diff; the constant may hide in it.
    assert mf.scope(files(changed("app/services/skills.py", patch=None)), SKILLS).protected


def test_a_listing_at_the_files_limit_counts_as_protected():
    many = files(*[changed(f"docs/page{i}.md") for i in range(mf.FILES_LIMIT)])
    assert mf.scope(many, ALEMBIC).protected


def test_constant_file_and_pattern_go_together():
    with pytest.raises(ValueError, match="go together"):
        mf.Rules(paths=("app/skills_builtin/*",), constant_file="app/services/skills.py")


def test_rules_that_protect_nothing_are_refused():
    with pytest.raises(ValueError, match="empty"):
        mf.Rules(paths=())
    # fnmatch matches no file against a folder written with a trailing slash.
    with pytest.raises(ValueError, match="ends in /"):
        mf.Rules(paths=("app/db/alembic/versions/*", "app/db/alembic/versions/"))


@pytest.mark.parametrize("paths", ["app/db/alembic/versions/", " \n "], ids=["folder", "blank"])
@pytest.mark.parametrize("mode", ["pr", "refresh"])
def test_paths_that_protect_nothing_stop_the_run_before_any_lookup(mode, paths):
    gh = FakeGh()
    with pytest.raises(SystemExit) as stopped:
        mf.main([mode, "--repo", REPO, "--base", "staging", "--paths", paths, "--pr", "7", "--head-sha", HEAD],
                runner=gh)
    assert stopped.value.code == 2
    assert gh.calls == []


# --- pr mode -----------------------------------------------------------------


def run_pr(gh, tmp_path, base="staging", rules_paths="app/db/alembic/versions/*", extra=()):
    output = tmp_path / "output"
    code = mf.main(
        ["pr", "--repo", REPO, "--base", base, "--paths", rules_paths, "--pr", "7", "--head-sha", HEAD,
         "--github-output", str(output), *extra],
        runner=gh,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return code, values


def test_an_unprotected_pull_request_passes_without_reading_the_base(tmp_path):
    gh = FakeGh(files={7: [changed("README.md")]}, behind={HEAD: 20})
    code, out = run_pr(gh, tmp_path)
    assert (code, out["protected"]) == (0, "false")
    assert not [e for e in gh.endpoints() if "/compare/" in e or "/git/ref/" in e]


def test_a_protected_pull_request_that_contains_the_tip_passes(tmp_path):
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 0})
    code, out = run_pr(gh, tmp_path)
    assert (code, out["protected"], out["tip"]) == (0, "true", TIP)


def test_a_protected_pull_request_behind_the_tip_fails(tmp_path):
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 1})
    code, out = run_pr(gh, tmp_path)
    assert (code, out["protected"]) == (1, "true")


def test_the_comparison_asks_what_the_head_lacks_from_the_live_tip(tmp_path):
    # Swapping the two sides reads "ahead" commits as "behind" and passes every stale head.
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 1})
    run_pr(gh, tmp_path)
    assert f"repos/{REPO}/compare/{TIP}...{HEAD}" in gh.endpoints()
    assert f"repos/{REPO}/git/ref/heads/staging" in gh.endpoints()


def test_the_comparison_asks_for_one_commit_page():
    # Only behind_by is read; the full commit list of a release pull request runs to megabytes.
    gh = FakeGh(behind={HEAD: 3})
    assert mf.behind_by(REPO, TIP, HEAD, gh) == 3
    assert gh.calls == [["gh", "api", f"repos/{REPO}/compare/{TIP}...{HEAD}?per_page=1"]]


def test_pr_mode_reads_the_constant_inputs(tmp_path):
    patch = "@@ -1 +1 @@\n-BUILTIN_SKILLS_VERSION = 41\n+BUILTIN_SKILLS_VERSION = 42\n"
    gh = FakeGh(files={7: [changed("app/services/skills.py", patch)]})
    code, out = run_pr(gh, tmp_path, rules_paths="app/skills_builtin/*",
                       extra=["--constant-file", "app/services/skills.py",
                              "--constant-pattern", r"^BUILTIN_SKILLS_VERSION\s*="])
    assert (code, out["protected"]) == (0, "true")


def test_a_base_outside_the_gated_ones_passes_without_any_lookup(tmp_path):
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 5})
    code, out = run_pr(gh, tmp_path, base="feat/stacked")
    assert (code, out["protected"]) == (0, "false")
    assert gh.calls == []


def test_a_failed_lookup_fails_the_check(tmp_path):
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, fail={"/compare/"})
    code, _ = run_pr(gh, tmp_path)
    assert code == 1
    gh = FakeGh(fail={"/files"})
    code, _ = run_pr(gh, tmp_path)
    assert code == 1


def test_a_failed_call_is_tried_three_times_before_it_fails_the_check(tmp_path, slept):
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, fail={"/compare/"})
    code, _ = run_pr(gh, tmp_path)
    assert code == 1
    assert [e for e in gh.endpoints() if "/compare/" in e] == [f"repos/{REPO}/compare/{TIP}...{HEAD}"] * 3
    assert slept == [1, 4]


def test_a_transient_failure_is_retried_and_the_check_passes(tmp_path, slept):
    gh = FakeGh(files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 0}, flaky={"/compare/": 1})
    code, out = run_pr(gh, tmp_path)
    assert (code, out["protected"]) == (0, "true")
    assert slept == [1]


def test_a_real_run_waits_between_tries():
    assert REAL_SLEEP is time.sleep


# --- refresh and backfill ----------------------------------------------------


def run_refresh(gh, mode="refresh"):
    return mf.main([mode, "--repo", REPO, "--base", "staging", "--paths",
                    "app/db/alembic/versions/*"], runner=gh)


def test_refresh_marks_a_protected_pull_request_the_push_left_behind():
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 1})
    assert run_refresh(gh) == 0
    # A status belongs to the head commit, which a pull request into another base can share.
    stale = f"#7 into staging: staging moved to {TIP[:7]}; merge staging into that branch and push"
    assert gh.posted == [(HEAD, {"state": "failure", "context": "migration-freshness", "description": stale})]


def test_refresh_reads_the_constant_inputs():
    patch = "@@ -1 +1 @@\n-BUILTIN_SKILLS_VERSION = 41\n+BUILTIN_SKILLS_VERSION = 42\n"
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/services/skills.py", patch)]}, behind={HEAD: 1})
    code = mf.main(["refresh", "--repo", REPO, "--base", "staging", "--paths", "app/skills_builtin/*",
                    "--constant-file", "app/services/skills.py",
                    "--constant-pattern", r"^BUILTIN_SKILLS_VERSION\s*="], runner=gh)
    assert code == 0
    assert [fields["state"] for _, fields in gh.posted] == ["failure"]


def test_refresh_reads_the_base_tip_once():
    gh = FakeGh(pulls=[(7, HEAD), (8, OTHER_HEAD)],
                files={7: [changed("app/db/alembic/versions/a.py")], 8: [changed("app/db/alembic/versions/b.py")]},
                behind={HEAD: 1, OTHER_HEAD: 1})
    run_refresh(gh)
    assert sum("/git/ref/heads/" in e for e in gh.endpoints()) == 1


def test_refresh_never_touches_an_unprotected_or_fresh_pull_request():
    gh = FakeGh(pulls=[(7, HEAD), (8, OTHER_HEAD)],
                files={7: [changed("README.md")], 8: [changed("app/db/alembic/versions/b.py")]},
                behind={HEAD: 9, OTHER_HEAD: 0})
    assert run_refresh(gh) == 0
    assert gh.posted == []
    assert f"repos/{REPO}/compare/{TIP}...{HEAD}" not in gh.endpoints()


def test_refresh_does_not_repeat_a_failure_already_written():
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 2},
                statuses={HEAD: [{"context": "migration-freshness", "state": "failure"}]})
    assert run_refresh(gh) == 0
    assert gh.posted == []


def test_refresh_ignores_a_failure_from_another_check():
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 2},
                statuses={HEAD: [{"context": "ci/build", "state": "failure"}]})
    assert run_refresh(gh) == 0
    assert [fields["state"] for _, fields in gh.posted] == ["failure"]


def test_refresh_reads_every_page_of_each_listing():
    # The second pull request, and its protected file, come on later pages.
    gh = FakeGh(pulls=[(7, HEAD), (8, OTHER_HEAD)],
                files={7: [changed("README.md")], 8: [changed("docs/x.md"), changed("app/db/alembic/versions/b.py")]},
                behind={OTHER_HEAD: 1})
    assert run_refresh(gh) == 0
    assert [sha for sha, _ in gh.posted] == [OTHER_HEAD]


def test_refresh_never_writes_success():
    gh = FakeGh(pulls=[(7, HEAD), (8, OTHER_HEAD)],
                files={7: [changed("README.md")], 8: [changed("app/db/alembic/versions/b.py")]},
                behind={OTHER_HEAD: 0})
    run_refresh(gh)
    assert not [fields for _, fields in gh.posted if fields["state"] == "success"]


def test_one_failed_lookup_does_not_stop_the_others_but_fails_the_run():
    gh = FakeGh(pulls=[(7, HEAD), (8, OTHER_HEAD)],
                files={8: [changed("app/db/alembic/versions/b.py")]}, behind={OTHER_HEAD: 1},
                fail={"/pulls/7/files"})
    assert run_refresh(gh) == 1
    assert [sha for sha, _ in gh.posted] == [OTHER_HEAD]


def test_a_status_write_that_keeps_failing_fails_the_run(slept):
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 1},
                fail={f"statuses/{HEAD}"})
    assert run_refresh(gh) == 1
    assert gh.endpoints().count(f"repos/{REPO}/statuses/{HEAD}") == 3
    assert slept == [1, 4]


def test_a_status_write_that_fails_once_is_written_on_the_retry(slept):
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 1},
                flaky={f"statuses/{HEAD}": 1})
    assert run_refresh(gh) == 0
    assert [fields["state"] for _, fields in gh.posted] == ["failure"]
    assert slept == [1]


@pytest.mark.parametrize("mode", ["refresh", "backfill"])
def test_a_refresh_of_a_base_outside_the_gated_ones_writes_nothing(tmp_path, capsys, mode):
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 1})
    summary = tmp_path / "summary"
    code = mf.main([mode, "--repo", REPO, "--base", "feat/x", "--paths", "app/db/alembic/versions/*",
                    "--gated-bases", "staging main", "--step-summary", str(summary)], runner=gh)
    assert code == 0
    assert gh.calls == []
    assert capsys.readouterr().out.startswith(f"::notice::migration-freshness {mode}: `feat/x` is not one of")
    assert "wrote nothing" in summary.read_text()


def test_backfill_passes_unprotected_pull_requests_that_have_no_status():
    gh = FakeGh(pulls=[(7, HEAD), (8, OTHER_HEAD)],
                files={7: [changed("README.md")], 8: [changed("docs/x.md")]},
                statuses={OTHER_HEAD: [{"context": "migration-freshness", "state": "failure"}]})
    assert run_refresh(gh, mode="backfill") == 0
    assert gh.posted == [(HEAD, {"state": "success", "context": "migration-freshness",
                                 "description": "No protected path changed"})]


def test_backfill_still_marks_stale_protected_pull_requests():
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 3})
    run_refresh(gh, mode="backfill")
    assert [fields["state"] for _, fields in gh.posted] == ["failure"]


def test_backfill_writes_nothing_on_a_fresh_protected_pull_request():
    # Its next push runs the graph check; a success here would pass it without one.
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/a.py")]}, behind={HEAD: 0})
    assert run_refresh(gh, mode="backfill") == 0
    assert gh.posted == []


# --- action.yml ----------------------------------------------------------------

ACTION_DIR = Path(__file__).resolve().parents[1] / "migration-freshness"
ACTION_STEPS = yaml.safe_load((ACTION_DIR / "action.yml").read_text())["runs"]["steps"]
CHECK_STEP = next(step for step in ACTION_STEPS if step.get("id") == "check")

# The github context each event gives the check step. On a push or a dispatch
# the pull request fields are absent, and GitHub evaluates them to "".
PULL_REQUEST_EVENT = {
    "github.ref_name": "7/merge",
    "github.event.pull_request.number": "7",
    "github.event.pull_request.base.ref": "staging",
    "github.event.pull_request.head.sha": HEAD,
}
PUSH_EVENT = {"github.ref_name": "main"}
DISPATCH_EVENT = {"github.ref_name": "staging"}

INPUTS = {
    "protected-paths": "app/db/alembic/versions/*\n",
    "constant-file": "app/services/skills.py",
    "constant-pattern": "^BUILTIN_SKILLS_VERSION =",
    "gated-bases": "staging main",
    "token": "token",
}


def run_check_step(tmp_path, mode, event):
    """Run the check step's own bash, with its env evaluated for ``event``, and return the script's argv."""
    context = {
        **{f"inputs.{name}": value for name, value in {**INPUTS, "mode": mode}.items()},
        **event,
        "github.repository": REPO,
        "github.action_path": str(ACTION_DIR),
    }

    def evaluate(value):
        # Each env value in the step is one `${{ name }}` expression.
        name = re.fullmatch(r"\$\{\{ ([\w.-]+) \}\}", value).group(1)
        return context.get(name, "") if name.startswith("github.event.") else context[name]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text('#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "$ARGV_FILE"\n')
    fake_python.chmod(0o755)
    env = {name: evaluate(value) for name, value in CHECK_STEP["env"].items()}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        ARGV_FILE=str(tmp_path / "argv"),
        GITHUB_OUTPUT=str(tmp_path / "output"),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
    )
    # The runner's own invocation for `shell: bash`.
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", CHECK_STEP["run"]], env=env, check=True)
    script, *argv = (tmp_path / "argv").read_text().split("\0")[:-1]
    assert Path(script).resolve() == _PATH
    return argv


def step_args(tmp_path):
    return [
        "--repo", REPO, "--paths", INPUTS["protected-paths"],
        "--constant-file", INPUTS["constant-file"], "--constant-pattern", INPUTS["constant-pattern"],
        "--gated-bases", INPUTS["gated-bases"],
        "--github-output", str(tmp_path / "output"), "--step-summary", str(tmp_path / "summary"),
    ]


def test_the_action_checks_a_pull_request_against_its_own_base_and_head(tmp_path):
    argv = run_check_step(tmp_path, "pr", PULL_REQUEST_EVENT)
    assert argv == ["pr", *step_args(tmp_path), "--base", "staging", "--pr", "7", "--head-sha", HEAD]


@pytest.mark.parametrize(
    "event", [PUSH_EVENT, DISPATCH_EVENT, PULL_REQUEST_EVENT], ids=["push", "dispatch", "pull_request"]
)
def test_the_action_refreshes_the_branch_the_run_is_on(tmp_path, event):
    # Only the pull_request case sets the pull request's base and head, so a
    # refresh step that reads either one fails here.
    argv = run_check_step(tmp_path, "refresh", event)
    assert argv == ["refresh", *step_args(tmp_path), "--base", event["github.ref_name"]]
