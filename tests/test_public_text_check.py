"""Synthetic fixtures only: never put actual restricted terms in this file."""

import copy
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import public_text_check as ptc

RX = re.compile("zqcanary7731", re.I)
PR_PATH = "repos/o/r/pulls/7"
SHA = "a" * 40
BASE = "b" * 40
MERGED = "e" * 40


def pr():
    return {
        "title": "fix", "body": "", "number": 7, "commits": 1, "changed_files": 1,
        "head": {"ref": "topic", "sha": SHA, "repo": {"full_name": "o/r"}},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": "o/r"}},
    }


def merged_path(branch="topic"):
    return f"repos/o/r/pulls?state=closed&base=main&head={quote('o:' + branch, safe='')}&per_page=100"


def compare_path(base, head=SHA):
    return f"repos/o/r/compare/{base}...{head}?per_page=100"


def compare_pages(commits, merge_base):
    chunks = [commits[i:i + 100] for i in range(0, len(commits), 100)] or [[]]
    return [{"total_commits": len(commits), "merge_base_commit": {"sha": merge_base}, "commits": chunk}
            for chunk in chunks]


def commit(n, message="clean"):
    return {"sha": f"{n:040x}", "commit": {"message": message}}


def responses():
    return {
        PR_PATH: pr(),
        PR_PATH + "/commits?per_page=100": [
            {"sha": SHA, "commit": {"message": "clean"}}
        ],
        PR_PATH + "/files?per_page=100": [
            {"filename": "file.txt", "status": "added", "additions": 1, "deletions": 0,
             "sha": "c" * 40, "patch": "@@ -0,0 +1 @@\n+clean"}
        ],
        merged_path(): [],
    }


def mock_api(monkeypatch, data):
    monkeypatch.setattr(ptc, "gh_api", lambda path: copy.deepcopy(data[path]))
    monkeypatch.setattr(ptc, "gh_pages", lambda path: copy.deepcopy(data[path]))


def run_main(monkeypatch, tmp_path, event_name="pull_request", event=None, pattern="zqcanary7731"):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event or {"pull_request": pr()}))
    for name, value in {
        "REPO": "o/r", "EVENT_NAME": event_name, "EVENT_PATH": str(path),
        "PUBLIC_TEXT_DENYLIST": pattern,
    }.items():
        monkeypatch.setenv(name, value)
    return ptc.main()


def test_missing_secret_fails_same_repository(monkeypatch, tmp_path):
    assert run_main(monkeypatch, tmp_path, pattern="") == 2


def test_release_train_scans_commits(monkeypatch):
    data = responses()
    data[PR_PATH]["head"]["ref"] = "staging"
    data[PR_PATH + "/commits?per_page=100"][0]["commit"]["message"] = "zqcanary7731"
    data[merged_path("staging")] = []
    mock_api(monkeypatch, data)
    assert any("message" in f.where for f in ptc.check_pr(RX, repo="o/r", number=7))


def test_changed_text_without_patch_never_passes(monkeypatch, tmp_path):
    # GitHub omits the patch of a large text diff; the new blob alone cannot
    # tell which of its lines the PR added.
    data = responses()
    blob(data, b"clean\n")
    data[PR_PATH + "/files?per_page=100"][0]["status"] = "modified"
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


def train(data, carried_message, new_message, merge_base=MERGED):
    """A staging train whose first commit an earlier merged train already carried."""
    data[PR_PATH]["head"]["ref"] = "staging"
    data[PR_PATH]["commits"] = 2
    carried, new = commit(1, carried_message), commit(2, new_message)
    data[PR_PATH + "/commits?per_page=100"] = [carried, new]
    data[merged_path("staging")] = [
        {"number": 5, "merged_at": "2026-10-19T08:00:00Z", "head": {"sha": MERGED, "repo": {"full_name": "o/r"}}},
    ]
    # Compare from the merged head also lists base-branch commits merged back
    # into the train, which are outside the PR and must never be scanned.
    data[compare_path(MERGED)] = compare_pages([new, commit(3, "zqcanary7731")], merge_base)


def test_train_skips_commits_an_earlier_merged_train_carried(monkeypatch, tmp_path):
    data = responses()
    train(data, carried_message="zqcanary7731", new_message="clean")
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


def test_train_scans_commits_no_earlier_train_carried(monkeypatch):
    data = responses()
    train(data, carried_message="clean", new_message="zqcanary7731")
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["commit 2 message, line 1"]


def test_train_that_left_the_merged_head_scans_every_commit(monkeypatch):
    data = responses()
    train(data, carried_message="zqcanary7731", new_message="clean", merge_base="d" * 40)
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["commit 1 message, line 1"]


@pytest.mark.parametrize("position", [0, 1])
def test_newest_merged_train_decides_what_was_carried(monkeypatch, tmp_path, position):
    data = responses()
    train(data, carried_message="zqcanary7731", new_message="clean")
    data[merged_path("staging")].insert(position, {
        "number": 4, "merged_at": "2026-10-12T08:00:00Z", "head": {"sha": "d" * 40, "repo": {"full_name": "o/r"}},
    })
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


def long_train(data):
    """150 train commits, 140 of them new since the merged train: two compare pages."""
    commits = [commit(n) for n in range(1, 151)]
    commits[-1]["commit"]["message"] = "zqcanary7731"
    data[PR_PATH]["head"]["ref"] = "staging"
    data[PR_PATH]["commits"] = len(commits)
    data[PR_PATH + "/commits?per_page=100"] = commits
    data[merged_path("staging")] = [
        {"number": 5, "merged_at": "2026-10-19T08:00:00Z", "head": {"sha": MERGED, "repo": {"full_name": "o/r"}}},
    ]
    data[compare_path(MERGED)] = compare_pages(commits[10:], MERGED)


def test_new_train_commits_on_a_later_compare_page_are_checked(monkeypatch):
    data = responses()
    long_train(data)
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["commit 150 message, line 1"]


@pytest.mark.parametrize("damage", ["missing page", "page total", "duplicate"])
def test_incomplete_comparison_never_marks_new_commits_carried(monkeypatch, tmp_path, damage):
    data = responses()
    long_train(data)
    pages = data[compare_path(MERGED)]
    if damage == "missing page":
        pages.pop()
    elif damage == "page total":
        for page in pages:
            page["total_commits"] = 141
    else:
        pages[1]["commits"][-1] = pages[0]["commits"][0]
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


@pytest.mark.parametrize("change", ["unmerged", "fork", "deleted fork", "same PR"])
def test_only_this_repository_s_other_merged_prs_count_as_carried(monkeypatch, change):
    data = responses()
    train(data, carried_message="zqcanary7731", new_message="clean")
    pull = data[merged_path("staging")][0]
    if change == "unmerged":
        pull["merged_at"] = None
    elif change == "fork":
        pull["head"]["repo"] = {"full_name": "outside/r"}
    elif change == "deleted fork":
        pull["head"]["repo"] = None
    else:
        pull["number"] = 7
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["commit 1 message, line 1"]


def test_merged_pr_lookup_failure_fails(monkeypatch, tmp_path):
    data = responses()
    train(data, carried_message="clean", new_message="clean")
    del data[merged_path("staging")]
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


def big_train(data, total=251):
    """A PR past the PR endpoint's 250-commit limit, which lists only the newest 250."""
    commits = [commit(n) for n in range(1, total + 1)]
    data[PR_PATH]["commits"] = total
    data[PR_PATH + "/commits?per_page=100"] = commits[-250:]
    data[compare_path(BASE)] = compare_pages(commits, BASE)
    return commits


def test_commits_past_250_are_listed_through_compare(monkeypatch):
    data = responses()
    commits = big_train(data)
    commits[0]["commit"]["message"] = "zqcanary7731"
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["commit 1 message, line 1"]


@pytest.mark.parametrize("damage", ["missing page", "page total", "merge base", "list pages", "foreign commit", "no compare"])
def test_commits_past_250_without_a_complete_comparison_fail(monkeypatch, tmp_path, damage):
    data = responses()
    big_train(data)
    pages = data[compare_path(BASE)]
    if damage == "missing page":
        pages.pop()
    elif damage == "page total":
        pages[0]["total_commits"] = 252
    elif damage == "merge base":
        pages[1]["merge_base_commit"]["sha"] = "d" * 40
    elif damage == "list pages":
        data[compare_path(BASE)] = [page["commits"] for page in pages]
    elif damage == "foreign commit":
        data[PR_PATH + "/commits?per_page=100"][0] = commit(999)
    else:
        del data[compare_path(BASE)]
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


def test_clean_paths_are_not_logged(monkeypatch):
    data = responses()
    data[PR_PATH + "/files?per_page=100"][0].update(
        filename="attacker%0A::notice::secret.txt", patch="@@ -0,0 +1 @@\n+zqcanary7731"
    )
    mock_api(monkeypatch, data)
    found = ptc.check_pr(RX, repo="o/r", number=7)
    assert found and all("attacker" not in f.where for f in found)


def test_rerun_reads_current_comment(monkeypatch, tmp_path):
    mock_api(monkeypatch, {"repos/o/r/issues/comments/12": {"body": "zqcanary7731"}})
    assert run_main(monkeypatch, tmp_path, "issue_comment",
                    {"comment": {"id": 12, "body": "formerly clean"}}) == 1


def test_removed_path_can_clean_existing_disclosure(monkeypatch):
    data = responses()
    data[PR_PATH + "/files?per_page=100"][0].update(
        filename="zqcanary7731.txt", status="removed", additions=0,
        patch="@@ -1 +0,0 @@\n-zqcanary7731"
    )
    mock_api(monkeypatch, data)
    assert ptc.check_pr(RX, repo="o/r", number=7) == []


@pytest.mark.parametrize("field", ["title", "body", "branch", "commit", "path", "added"])
def test_all_pr_fields_fail_without_echoing_text(monkeypatch, tmp_path, capsys, field):
    data = responses()
    if field in {"title", "body"}:
        data[PR_PATH][field] = "zqcanary7731"
    elif field == "branch":
        data[PR_PATH]["head"]["ref"] = "fix/zqcanary7731"
        data[merged_path("fix/zqcanary7731")] = []
    elif field == "commit":
        data[PR_PATH + "/commits?per_page=100"][0]["commit"]["message"] = "zqcanary7731"
    elif field == "path":
        data[PR_PATH + "/files?per_page=100"][0]["filename"] = "zqcanary7731"
    else:
        data[PR_PATH + "/files?per_page=100"][0]["patch"] = "@@ -0,0 +1 @@\n+zqcanary7731"
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 1
    output = capsys.readouterr()
    assert "zqcanary7731" not in output.out + output.err


def test_clean_pr_and_generic_deployment_terminology_pass(monkeypatch, tmp_path):
    data = responses()
    data[PR_PATH]["body"] = "self-hosted deployment with COWORK_DEPLOYMENT_KIND=self-hosted"
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


@pytest.mark.parametrize("event_name", ["pull_request", "pull_request_review", "pull_request_review_comment"])
def test_explicit_fork_exception_without_secret(monkeypatch, tmp_path, capsys, event_name):
    pull = pr()
    pull["head"]["repo"]["full_name"] = "outside/fork"
    assert run_main(monkeypatch, tmp_path, event_name, {"pull_request": pull}, pattern="") == 0
    assert "NOT SCANNED" in capsys.readouterr().out


def test_fork_discussion_comment_is_scanned(monkeypatch, tmp_path):
    mock_api(monkeypatch, {"repos/o/r/issues/comments/12": {"body": "zqcanary7731"}})
    event = {"comment": {"id": 12}, "issue": {"pull_request": {}}}
    assert run_main(monkeypatch, tmp_path, "issue_comment", event) == 1


def test_dependabot_without_configuration_fails(monkeypatch, tmp_path):
    event = {"pull_request": pr(), "sender": {"login": "dependabot[bot]"}}
    assert run_main(monkeypatch, tmp_path, event=event, pattern="") == 2


@pytest.mark.parametrize("pattern", ["", "   ", "zqcanary7731("])
def test_invalid_configuration_never_echoes_pattern(monkeypatch, tmp_path, capsys, pattern):
    assert run_main(monkeypatch, tmp_path, pattern=pattern) == 2
    assert "zqcanary7731" not in capsys.readouterr().out


@pytest.mark.parametrize("field,value", [("commits", 2), ("changed_files", 2), ("changed_files", 3001)])
def test_incomplete_collections_fail(monkeypatch, tmp_path, field, value):
    data = responses()
    data[PR_PATH][field] = value
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


@pytest.mark.parametrize("collection,field", [("commits", "commits"), ("files", "changed_files")])
def test_duplicate_page_data_fails(monkeypatch, tmp_path, collection, field):
    data = responses()
    data[PR_PATH][field] = 2
    data[PR_PATH + f"/{collection}?per_page=100"] *= 2
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


def test_truncated_patch_fails(monkeypatch, tmp_path):
    data = responses()
    data[PR_PATH + "/files?per_page=100"][0]["additions"] = 2
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


@pytest.mark.parametrize("patch", [
    "@@ -0,0 +1,2 @@\n+clean", "truncated", "@@ -0,0 +1 @@\n+one\n+extra",
    "@@ -1 +1 @@\n", "@@ -0,0 +1,2 @@\n+clean\n@@ -0,0 +3 @@\n+clean",
])
def test_malformed_hunks_fail(patch):
    with pytest.raises(ptc.ScanError):
        ptc.added_lines(patch)


def test_hunks_handle_literal_prefixes_crlf_and_no_newline():
    patch = "@@ -1,2 +1,3 @@\n ctx\r\n-old\r\n+++new\r\n+tail\n\\ No newline at end of file"
    assert ptc.added_lines(patch) == [(2, "++new\r"), (3, "tail")]


def blob(data, content):
    data["repos/o/r/git/blobs/" + "c" * 40] = {
        "size": len(content), "encoding": "base64",
        "content": base64.b64encode(content).decode(),
    }
    data[PR_PATH + "/files?per_page=100"][0].pop("patch", None)


def test_binary_content_is_excluded_but_path_is_checked(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"binary\0zqcanary7731")
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0
    data[PR_PATH + "/files?per_page=100"][0]["filename"] = "zqcanary7731.png"
    assert run_main(monkeypatch, tmp_path) == 1


def test_text_with_binary_extension_is_checked_as_text(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"zqcanary7731")
    data[PR_PATH + "/files?per_page=100"][0]["filename"] = "logo.png"
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 1


def test_new_file_without_patch_or_nul_is_checked_whole(monkeypatch, tmp_path):
    # GitHub shows small PDFs as binary although they carry no NUL byte.
    data = responses()
    blob(data, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nzqcanary7731\n%%EOF\n")
    data[PR_PATH + "/files?per_page=100"][0].update(filename="report.pdf", additions=0)
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["file 1, added line 3"]
    blob(data, b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nclean\n%%EOF\n")
    assert run_main(monkeypatch, tmp_path) == 0


def test_whole_file_check_reads_past_the_binary_probe(monkeypatch):
    data = responses()
    blob(data, b"a" * 9000 + b"\nzqcanary7731\n")
    mock_api(monkeypatch, data)
    assert [f.where for f in ptc.check_pr(RX, repo="o/r", number=7)] == ["file 1, added line 2"]


def test_nul_after_the_first_8000_bytes_is_still_text(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"zqcanary7731\n" + b"a" * 9000 + b"\0")
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 1


@pytest.mark.parametrize("status", ["modified", "renamed"])
def test_changed_binary_without_patch_checks_only_its_path(monkeypatch, tmp_path, status):
    data = responses()
    blob(data, b"\x89PNG\r\n\x1a\n\0zqcanary7731")
    file = data[PR_PATH + "/files?per_page=100"][0]
    file.update(status=status, additions=0, deletions=0, previous_filename="old.png")
    data["repos/o/r/contents/old.png?ref=" + BASE] = {"type": "file", "sha": "d" * 40}
    data["repos/o/r/contents/file.txt?ref=" + BASE] = {"type": "file", "sha": "d" * 40}
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


def test_new_text_over_1_mib_without_patch_fails(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"a" * (1_048_576 + 1))
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


def test_binary_up_to_10_mib_passes_and_larger_blobs_fail(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"\x1f\x8b\x08\0" + b"a" * (2 * 1_048_576))
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0
    blob(data, b"\x1f\x8b\x08\0" + b"a" * (10 * 1_048_576))
    assert run_main(monkeypatch, tmp_path) == 2


def test_empty_file_passes(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"")
    data[PR_PATH + "/files?per_page=100"][0]["additions"] = 0
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


def test_rename_away_from_restricted_path_passes(monkeypatch, tmp_path):
    data = responses()
    file = data[PR_PATH + "/files?per_page=100"][0]
    file.update(status="renamed", previous_filename="zqcanary7731.txt", additions=0, deletions=0)
    file.pop("patch")
    data["repos/o/r/contents/zqcanary7731.txt?ref=" + "b" * 40] = {"type": "file", "sha": "c" * 40}
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


@pytest.mark.parametrize("change", ["title", "body", "head", "base", "branch"])
def test_pr_changes_during_scan_fail(monkeypatch, tmp_path, change):
    data = responses()
    reads = 0

    def api(path):
        nonlocal reads
        value = copy.deepcopy(data[path])
        if path == PR_PATH:
            reads += 1
            if reads > 1:
                if change in {"title", "body"}:
                    value[change] = "zqcanary7731"
                elif change == "branch":
                    value["head"]["ref"] = "changed"
                else:
                    value[change]["sha"] = "d" * 40
        return value

    monkeypatch.setattr(ptc, "gh_api", api)
    assert run_main(monkeypatch, tmp_path) == 2


def test_old_head_rerun_cannot_pass_for_a_new_head(monkeypatch, tmp_path):
    data = responses()
    data[PR_PATH]["head"]["sha"] = "d" * 40
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


@pytest.mark.parametrize("name,event,endpoint,value", [
    ("issues", {"issue": {"number": 3}}, "issues/3", {"title": "zqcanary7731", "body": ""}),
    ("pull_request_review", {"pull_request": pr(), "review": {"id": 4}},
     "pulls/7/reviews/4", {"body": "zqcanary7731"}),
    ("pull_request_review_comment", {"pull_request": pr(), "comment": {"id": 5}},
     "pulls/comments/5", {"body": "zqcanary7731"}),
])
def test_current_text_audits(monkeypatch, tmp_path, name, event, endpoint, value):
    mock_api(monkeypatch, {"repos/o/r/" + endpoint: value})
    assert run_main(monkeypatch, tmp_path, name, event) == 1


def test_comment_changes_during_scan_fail(monkeypatch, tmp_path):
    values = iter([{"body": "clean"}, {"body": "zqcanary7731"}])
    monkeypatch.setattr(ptc, "gh_api", lambda _: next(values))
    assert run_main(monkeypatch, tmp_path, "issue_comment", {"comment": {"id": 12}}) == 2


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, ["gh"], stderr="zqcanary7731\n::notice::poison"),
    ValueError("zqcanary7731"),
    KeyError("zqcanary7731"),
    subprocess.TimeoutExpired("zqcanary7731", 90),
])
def test_failures_never_echo_data(monkeypatch, tmp_path, capsys, error):
    def fail(_):
        raise error
    monkeypatch.setattr(ptc, "gh_api", fail)
    assert run_main(monkeypatch, tmp_path) == 2
    output = capsys.readouterr()
    assert "zqcanary7731" not in output.out + output.err
    assert "poison" not in output.out + output.err


def test_pagination_flattens_every_page(monkeypatch):
    def run(args, **kwargs):
        assert args[:4] == ["gh", "api", "--paginate", "--slurp"]
        assert kwargs["capture_output"] and kwargs["timeout"]
        return subprocess.CompletedProcess(args, 0, json.dumps([[{"id": 1}], [{"id": 2}]]))
    monkeypatch.setattr(ptc.subprocess, "run", run)
    assert ptc.gh_api("repos/o/r/issues") == [{"id": 1}, {"id": 2}]


def test_mode_only_text_change_passes(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"#!/bin/sh\necho clean\n")
    data[PR_PATH + "/files?per_page=100"][0].update(status="modified", additions=0, deletions=0)
    data["repos/o/r/contents/file.txt?ref=" + "b" * 40] = {"type": "file", "sha": "c" * 40}
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 0


def test_multiline_pattern_matches_contiguous_added_lines(monkeypatch):
    data = responses()
    data[PR_PATH + "/files?per_page=100"][0].update(
        additions=2, patch="@@ -0,0 +1,2 @@\n+zqcanary\n+7731"
    )
    mock_api(monkeypatch, data)
    found = ptc.check_pr(re.compile(r"zqcanary\s+7731"), repo="o/r", number=7)
    assert [f.where for f in found] == ["file 1, added line 1"]


def test_multiline_pattern_does_not_join_across_context(monkeypatch):
    data = responses()
    data[PR_PATH + "/files?per_page=100"][0].update(
        additions=2, patch="@@ -1 +1,3 @@\n+zqcanary\n context\n+7731"
    )
    mock_api(monkeypatch, data)
    assert ptc.check_pr(re.compile(r"zqcanary\s+7731"), repo="o/r", number=7) == []


def test_zero_counts_do_not_exempt_new_text_classified_as_binary(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"zqcanary7731")
    data[PR_PATH + "/files?per_page=100"][0].update(additions=0, deletions=0)
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 1


def test_zero_counts_with_changed_blob_fail(monkeypatch, tmp_path):
    data = responses()
    blob(data, b"zqcanary7731")
    data[PR_PATH + "/files?per_page=100"][0].update(status="modified", additions=0, deletions=0)
    data["repos/o/r/contents/file.txt?ref=" + "b" * 40] = {"type": "file", "sha": "d" * 40}
    mock_api(monkeypatch, data)
    assert run_main(monkeypatch, tmp_path) == 2


def workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def test_pr_gate_and_audit_never_share_a_check_or_queue():
    gate, audit = workflow("public-text.yml"), workflow("public-text-audit.yml")
    # PyYAML reads the bare `on` key as True.
    assert set(gate[True]) == {"pull_request"}
    assert "pull_request" not in audit[True]
    assert gate["name"] != audit["name"]
    assert set(gate["jobs"]).isdisjoint(audit["jobs"])
    gate_group, audit_group = (wf["concurrency"]["group"].split("${{")[0] for wf in (gate, audit))
    assert not gate_group.startswith(audit_group) and not audit_group.startswith(gate_group)
