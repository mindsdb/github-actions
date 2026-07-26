"""Unit tests for the workflow permission gate (``scripts/workflow_permissions.py``).

The gate exists because of a real incident in ``mindsdb/auth``:
``config-apply.yml``'s ``detect`` and ``plan`` jobs declared a ``pull-requests``
scope that the staging and prod push callers did not grant, GitHub rejected those
runs at file load with ``startup_failure`` and zero jobs, and two merges to
``staging`` sat undeployed for ten hours with nothing said — the terminal notify
job cannot fire in a run that never started.

These tests pin the rule and, at the bottom, replay that exact shape, so the class
cannot come back the next time someone adds a scope to a shared reusable, in any
of the repos that call ``workflow-lint.yml``.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "workflow_permissions.py"
_spec = importlib.util.spec_from_file_location("workflow_permissions", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

READ_DEFAULT = gate.DEFAULT_GRANTS["read"]


def write(directory: Path, name: str, body: dict) -> None:
    (directory / name).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def caller(job_permissions: dict | None = None, *, workflow_permissions: dict | None = None) -> dict:
    job: dict = {"uses": "./.github/workflows/callee.yml"}
    if job_permissions is not None:
        job["permissions"] = job_permissions
    body: dict = {"on": {"push": None}, "jobs": {"call": job}}
    if workflow_permissions is not None:
        body["permissions"] = workflow_permissions
    return body


def callee(job_permissions: dict | None = None, *, workflow_permissions: dict | None = None) -> dict:
    job: dict = {"runs-on": "mdb-dev", "steps": [{"run": "true"}]}
    if job_permissions is not None:
        job["permissions"] = job_permissions
    body: dict = {"on": {"workflow_call": None}, "jobs": {"work": job}}
    if workflow_permissions is not None:
        body["permissions"] = workflow_permissions
    return body


def run(tmp_path: Path, default: str = "read"):
    workflows = gate.load_workflows(tmp_path)
    return gate.check(workflows, gate.DEFAULT_GRANTS[default])


class TestNormalize:
    def test_absent_block_is_none_not_empty(self):
        """Declaring nothing and declaring `{}` are different: only one overrides."""
        assert gate.normalize(None) is None
        assert gate.normalize({}) == {}

    def test_read_all_and_write_all_shorthands_expand(self):
        assert gate.normalize("read-all") == {scope: "read" for scope in gate.SCOPES}
        assert gate.normalize("write-all") == {scope: "write" for scope in gate.SCOPES}

    def test_explicit_block_is_stringified(self):
        assert gate.normalize({"contents": "read"}) == {"contents": "read"}

    def test_unknown_shorthand_is_an_error_not_a_silent_pass(self):
        with pytest.raises(ValueError, match="unknown permissions shorthand"):
            gate.normalize("all-of-it")

    def test_unreadable_block_is_an_error(self):
        with pytest.raises(ValueError, match="unreadable permissions block"):
            gate.normalize(["contents: read"])


class TestGrantResolution:
    def test_callee_declaring_nothing_always_composes(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(tmp_path, "callee.yml", callee())
        violations, _ = run(tmp_path)
        assert violations == []

    def test_callee_within_the_grant_composes(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "read", "pull-requests": "write"}))
        write(tmp_path, "callee.yml", callee({"pull-requests": "write"}))
        violations, _ = run(tmp_path)
        assert violations == []

    def test_read_is_enough_for_a_read_declaration(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "write"}))
        write(tmp_path, "callee.yml", callee({"contents": "read"}))
        violations, _ = run(tmp_path)
        assert violations == []

    def test_read_is_not_enough_for_a_write_declaration(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"pull-requests": "read"}))
        write(tmp_path, "callee.yml", callee({"pull-requests": "write"}))
        violations, _ = run(tmp_path)
        assert len(violations) == 1
        assert violations[0].scope == "pull-requests"
        assert (violations[0].granted, violations[0].declared) == ("read", "write")

    def test_job_block_wins_over_the_workflow_block(self, tmp_path):
        """A job-level block replaces the workflow-level one rather than adding to it."""
        write(
            tmp_path,
            "caller.yml",
            caller({"contents": "read"}, workflow_permissions={"pull-requests": "write"}),
        )
        write(tmp_path, "callee.yml", callee({"pull-requests": "write"}))
        violations, _ = run(tmp_path)
        assert len(violations) == 1, "the workflow-level pull-requests grant must not rescue the job"

    def test_workflow_block_applies_when_the_job_declares_nothing(self, tmp_path):
        write(tmp_path, "caller.yml", caller(None, workflow_permissions={"pull-requests": "write"}))
        write(tmp_path, "callee.yml", callee({"pull-requests": "write"}))
        violations, _ = run(tmp_path)
        assert violations == []

    def test_repo_default_applies_when_nothing_is_declared(self, tmp_path):
        write(tmp_path, "caller.yml", caller())
        write(tmp_path, "callee.yml", callee({"contents": "read"}))
        violations, _ = run(tmp_path)
        assert violations == [], "contents: read is inside the default read token"

    def test_repo_default_does_not_cover_other_scopes(self, tmp_path):
        write(tmp_path, "caller.yml", caller())
        write(tmp_path, "callee.yml", callee({"issues": "read"}))
        violations, _ = run(tmp_path)
        assert len(violations) == 1
        assert violations[0].granted == "none"

    def test_a_write_all_repo_default_covers_everything(self, tmp_path):
        write(tmp_path, "caller.yml", caller())
        write(tmp_path, "callee.yml", callee({"issues": "write"}))
        violations, _ = run(tmp_path, default="write")
        assert violations == []

    def test_callee_workflow_level_block_is_checked_too(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(tmp_path, "callee.yml", callee(None, workflow_permissions={"packages": "write"}))
        violations, _ = run(tmp_path)
        assert len(violations) == 1
        assert violations[0].callee_job is None


class TestTransitiveAndEdges:
    def test_the_cap_reaches_a_nested_callee(self, tmp_path):
        """The ceiling flows the whole way down, so the walk has to as well."""
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(
            tmp_path,
            "callee.yml",
            {
                "on": {"workflow_call": None},
                "jobs": {"nest": {"uses": "./.github/workflows/deep.yml"}},
            },
        )
        write(tmp_path, "deep.yml", callee({"pull-requests": "write"}))
        violations, _ = run(tmp_path)
        assert len(violations) == 1
        assert violations[0].path == (
            "./.github/workflows/caller.yml",
            "./.github/workflows/callee.yml",
            "./.github/workflows/deep.yml",
        )

    def test_remote_callees_are_reported_unchecked_not_assumed_safe(self, tmp_path):
        write(
            tmp_path,
            "caller.yml",
            {
                "on": {"push": None},
                "jobs": {"call": {"uses": "mindsdb/github-actions/.github/workflows/notify.yml@main"}},
            },
        )
        violations, unchecked = run(tmp_path)
        assert violations == []
        assert len(unchecked) == 1
        assert "remote" in str(unchecked[0])

    def test_a_cycle_terminates(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(
            tmp_path,
            "callee.yml",
            {
                "on": {"workflow_call": None},
                "jobs": {"back": {"uses": "./.github/workflows/caller.yml"}},
            },
        )
        violations, _ = run(tmp_path)
        assert violations == []

    def test_a_missing_callee_file_is_not_a_permission_finding(self, tmp_path):
        """A bad path is its own startup failure; actionlint and GitHub both say so."""
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        violations, _ = run(tmp_path)
        assert violations == []

    def test_a_typo_in_a_scope_name_is_a_finding(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(tmp_path, "callee.yml", callee({"pull_requests": "write"}))
        violations, _ = run(tmp_path)
        assert len(violations) == 1
        assert violations[0].granted == "not a permission scope"

    def test_non_dict_jobs_are_skipped_rather_than_crashing(self, tmp_path):
        write(tmp_path, "caller.yml", {"on": {"push": None}, "jobs": {"weird": "not-a-mapping"}})
        write(tmp_path, "callee.yml", callee())
        violations, unchecked = run(tmp_path)
        assert (violations, unchecked) == ([], [])

    def test_non_mapping_workflow_files_are_ignored(self, tmp_path):
        (tmp_path / "notes.yml").write_text("- just a list\n", encoding="utf-8")
        assert gate.load_workflows(tmp_path) == {}

    def test_a_reusable_workflow_is_not_treated_as_a_root(self, tmp_path):
        """Its ceiling comes from its caller, so the repo default must not be applied to it.

        Without this, every reusable that calls another reusable is measured
        against a grant it never runs with, and the gate cries wolf.
        """
        write(tmp_path, "middle.yml", {"on": {"workflow_call": None}, "jobs": {"nest": {"uses": "./.github/workflows/deep.yml"}}})
        write(tmp_path, "deep.yml", callee({"pull-requests": "write"}))
        violations, _ = run(tmp_path)
        assert violations == [], "nothing can start middle.yml on its own"


class TestTriggerParsing:
    def test_an_unquoted_on_key_parses_as_the_boolean_true(self, tmp_path):
        """The trap: YAML 1.1 resolves a bare `on` to a boolean.

        Every hand-written workflow says `on:` unquoted, so the parsed key is
        ``True`` rather than ``"on"``. A gate that only looked up ``"on"`` would
        decide every workflow was a reusable and check nothing at all. (Note
        ``yaml.safe_dump`` emits ``'on':`` quoted, so a fixture built through it
        would hide this — hence the raw text here.)
        """
        (tmp_path / "hand-written.yml").write_text(
            "name: Hand written\non:\n  push:\n    branches: [main]\njobs:\n"
            "  call:\n    permissions:\n      contents: read\n"
            "    uses: ./.github/workflows/callee.yml\n",
            encoding="utf-8",
        )
        raw = yaml.safe_load((tmp_path / "hand-written.yml").read_text(encoding="utf-8"))
        assert True in raw and "on" not in raw
        assert gate.is_entry_point(raw)
        assert gate.triggers(raw) == {"push": {"branches": ["main"]}}

    def test_a_quoted_on_key_is_read_too(self, tmp_path):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        raw = yaml.safe_load((tmp_path / "caller.yml").read_text(encoding="utf-8"))
        assert "on" in raw
        assert gate.is_entry_point(raw)

    def test_a_string_trigger_is_an_entry_point(self):
        assert gate.is_entry_point({"on": "push"})

    def test_a_list_of_triggers_is_an_entry_point(self):
        assert gate.is_entry_point({"on": ["push", "workflow_dispatch"]})

    def test_workflow_call_alone_is_not_an_entry_point(self):
        assert not gate.is_entry_point({"on": {"workflow_call": None}})

    def test_a_workflow_call_plus_dispatch_is_an_entry_point(self):
        assert gate.is_entry_point({"on": {"workflow_call": None, "workflow_dispatch": None}})

    def test_a_missing_trigger_block_is_not_an_entry_point(self):
        assert not gate.is_entry_point({})


class TestTheIncident:
    """Replay of the shape that took staging down, as a regression test."""

    @staticmethod
    def _incident_tree(tmp_path: Path) -> None:
        # config-apply.yml as it was: `detect` and `plan` naming a PR scope so the
        # dev caller's rolling comment would work.
        write(
            tmp_path,
            "config-apply.yml",
            {
                "on": {"workflow_call": None},
                "jobs": {
                    "detect": {
                        "runs-on": "mdb-dev",
                        "permissions": {"contents": "read", "pull-requests": "read"},
                        "steps": [{"run": "true"}],
                    },
                    "plan": {
                        "runs-on": "mdb-dev",
                        "permissions": {"contents": "read", "pull-requests": "write"},
                        "steps": [{"run": "true"}],
                    },
                },
            },
        )
        # The dev caller granted the scope, so every PR was green.
        write(
            tmp_path,
            "dev-build-deploy.yml",
            {
                "on": {"pull_request": None},
                "jobs": {
                    "config-apply": {
                        "permissions": {"contents": "read", "pull-requests": "write"},
                        "uses": "./.github/workflows/config-apply.yml",
                    }
                },
            },
        )
        # The push callers did not, and neither run ever started.
        for name in ("staging-build-deploy.yml", "prod-build-deploy.yml"):
            write(
                tmp_path,
                name,
                {
                    "on": {"push": None},
                    "jobs": {"config-apply": {"uses": "./.github/workflows/config-apply.yml"}},
                },
            )

    def test_the_original_shape_is_caught_on_both_push_paths(self, tmp_path):
        self._incident_tree(tmp_path)
        violations, _ = run(tmp_path)

        callers = {violation.caller for violation in violations}
        assert callers == {
            "./.github/workflows/staging-build-deploy.yml",
            "./.github/workflows/prod-build-deploy.yml",
        }, "the PR path was fine, which is exactly why this merged"
        assert all(violation.scope == "pull-requests" for violation in violations)

    def test_the_finding_says_what_would_happen_and_how_to_fix_it(self, tmp_path):
        self._incident_tree(tmp_path)
        violations, _ = run(tmp_path)
        message = str(violations[0])
        assert "startup_failure" in message
        assert "no job to report it" in message
        assert "inherits the caller's ceiling" in message

    def test_inheriting_in_the_callee_is_what_resolves_it(self, tmp_path):
        """The fix that composes: the callee stops naming the scope only one caller grants."""
        self._incident_tree(tmp_path)
        write(
            tmp_path,
            "config-apply.yml",
            {
                "on": {"workflow_call": None},
                "jobs": {
                    "detect": {"runs-on": "mdb-dev", "steps": [{"run": "true"}]},
                    "plan": {"runs-on": "mdb-dev", "steps": [{"run": "true"}]},
                },
            },
        )
        violations, _ = run(tmp_path)
        assert violations == []


class TestCli:
    def test_clean_tree_exits_zero(self, tmp_path, capsys):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(tmp_path, "callee.yml", callee())
        assert gate.main(["--workflow-dir", str(tmp_path)]) == 0
        assert "every local reusable call composes" in capsys.readouterr().out

    def test_violations_exit_nonzero(self, tmp_path, capsys):
        write(tmp_path, "caller.yml", caller({"contents": "read"}))
        write(tmp_path, "callee.yml", callee({"pull-requests": "write"}))
        assert gate.main(["--workflow-dir", str(tmp_path)]) == 1
        assert "would fail a run before it starts" in capsys.readouterr().err

    def test_an_empty_workflow_dir_is_a_failure_not_a_pass(self, tmp_path, capsys):
        """A gate that reports success when it found nothing to check is worse than none."""
        assert gate.main(["--workflow-dir", str(tmp_path)]) == 1
        assert "No workflows found" in capsys.readouterr().err

    def test_the_repo_default_is_selectable(self, tmp_path):
        write(tmp_path, "caller.yml", caller())
        write(tmp_path, "callee.yml", callee({"issues": "write"}))
        assert gate.main(["--workflow-dir", str(tmp_path), "--default-permissions", "write"]) == 0
        assert gate.main(["--workflow-dir", str(tmp_path), "--default-permissions", "read"]) == 1

    def test_this_repos_own_workflows_pass(self):
        """The default target is `.github/workflows` under the working directory.

        Run from this repo's root that is this repo's own reusables, which is both
        a smoke test of the default and the dogfood check: the repo that defines
        everyone's pipelines should pass its own gate.
        """
        assert gate.main(["--workflow-dir", str(_GATE_PATH.parents[1] / ".github" / "workflows")]) == 0
