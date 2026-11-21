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
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migration_freshness.py"
_spec = importlib.util.spec_from_file_location("migration_freshness", _PATH)
mf = importlib.util.module_from_spec(_spec)
# Dataclasses resolve their annotations through sys.modules.
sys.modules["migration_freshness"] = mf
_spec.loader.exec_module(mf)

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


class FakeGh:
    """Answers `gh api` calls by endpoint and records every call."""

    def __init__(self, *, tip=TIP, files=None, behind=None, pulls=(), statuses=None, fail=()):
        self.tip = tip
        self.files = files or {}  # pull number -> list of file dicts
        self.behind = behind or {}  # head sha -> behind_by
        self.pulls = list(pulls)  # (number, head sha)
        self.statuses = statuses or {}  # head sha -> list of status dicts, newest first
        self.fail = set(fail)  # endpoint substrings that answer with an error
        self.calls = []
        self.posted = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        assert argv[:2] == ["gh", "api"]
        args = argv[2:]
        endpoint = next(a for a in args if a.startswith("repos/"))
        for marker in self.fail:
            if marker in endpoint:
                return completed(stdout='{"message":"Server Error"}', returncode=1)
        if "--method" in args:
            fields = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f")
            self.posted.append((endpoint.rsplit("/", 1)[1], fields))
            return completed(stdout="{}")
        if "/git/ref/heads/" in endpoint:
            return completed(stdout=json.dumps({"object": {"sha": self.tip}}))
        if "/compare/" in endpoint:
            head = endpoint.rsplit("...", 1)[1]
            return completed(stdout=json.dumps({"behind_by": self.behind.get(head, 0)}))
        if "/files" in endpoint:
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            return completed(stdout=json.dumps([self.files.get(number, [])]))
        if "/pulls?" in endpoint:
            page = [{"number": n, "head": {"sha": sha}} for n, sha in self.pulls]
            return completed(stdout=json.dumps([page]))
        if "/statuses" in endpoint:
            sha = endpoint.split("/commits/")[1].split("/")[0]
            return completed(stdout=json.dumps(self.statuses.get(sha, [])))
        raise AssertionError(f"unexpected call {argv}")

    def endpoints(self):
        return [next(a for a in call[2:] if a.startswith("repos/")) for call in self.calls]


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


def test_a_constant_file_without_a_patch_counts_as_protected():
    # GitHub drops the patch of a large diff; the constant may hide in it.
    assert mf.scope(files(changed("app/services/skills.py", patch=None)), SKILLS).protected


def test_a_listing_at_the_files_limit_counts_as_protected():
    many = files(*[changed(f"docs/page{i}.md") for i in range(mf.FILES_LIMIT)])
    assert mf.scope(many, ALEMBIC).protected


def test_constant_file_and_pattern_go_together():
    with pytest.raises(ValueError):
        mf.Rules(paths=(), constant_file="app/services/skills.py")


# --- pr mode -----------------------------------------------------------------


def run_pr(gh, tmp_path, base="staging", rules_paths="app/db/alembic/versions/*"):
    output = tmp_path / "output"
    code = mf.main(
        ["pr", "--repo", REPO, "--base", base, "--paths", rules_paths, "--pr", "7", "--head-sha", HEAD,
         "--github-output", str(output)],
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


# --- refresh and backfill ----------------------------------------------------


def run_refresh(gh, mode="refresh"):
    return mf.main([mode, "--repo", REPO, "--base", "staging", "--paths",
                    "app/db/alembic/versions/*"], runner=gh)


def test_refresh_marks_a_protected_pull_request_the_push_left_behind():
    gh = FakeGh(pulls=[(7, HEAD)], files={7: [changed("app/db/alembic/versions/new.py")]}, behind={HEAD: 1})
    assert run_refresh(gh) == 0
    assert gh.posted == [(HEAD, {"state": "failure", "context": "migration-freshness",
                                 "description": f"staging moved to {TIP[:7]}; merge staging into this branch and push"})]


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


def test_a_retarget_refreshes_the_pull_requests_new_base():
    # On pull_request_target, github.ref names the default branch, so a refresh
    # that read it would check the wrong base after a retarget onto staging.
    action = (Path(__file__).resolve().parents[1] / "migration-freshness" / "action.yml").read_text()
    assert "PR_BASE: ${{ github.event.pull_request.base.ref }}" in action
    assert 'args+=(--base "${PR_BASE:-$REF_NAME}")' in action
