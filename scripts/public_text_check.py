#!/usr/bin/env python3
"""Check public GitHub text without echoing text, paths or the private pattern.

Exit 0: clean or the explicit fork exception. Exit 1: match. Exit 2: incomplete.
Only API data is read. No contributor code is checked out or executed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import quote


class ScanError(Exception):
    """A scan cannot establish a complete, stable result."""


@dataclass(frozen=True)
class Finding:
    where: str


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str
    branch: str
    head: str
    base: str
    base_branch: str
    head_repo: str
    commits: int
    files: int

    @classmethod
    def read(cls, value: dict) -> PullRequest:
        return cls(
            text(value["title"]), text(value.get("body")), text(value["head"]["ref"]),
            sha(value["head"]["sha"]), sha(value["base"]["sha"]), text(value["base"]["ref"]),
            text(value["head"]["repo"]["full_name"]),
            count(value["commits"]), count(value["changed_files"]),
        )


@dataclass(frozen=True)
class Comparison:
    merge_base: str
    commits: tuple[dict, ...]


def text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ScanError("invalid text")
    return value


def count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ScanError("invalid count")
    return value


def identifier(value: object) -> int:
    if count(value) == 0:
        raise ScanError("invalid identifier")
    return value


def sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ScanError("invalid revision")
    return value


def gh_pages(path: str) -> list:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        check=True, capture_output=True, text=True, timeout=90,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or not pages:
        raise ScanError("empty response")
    return pages


def gh_api(path: str) -> list | dict:
    pages = gh_pages(path)
    if all(isinstance(page, list) for page in pages):
        return [item for page in pages for item in page]
    if len(pages) == 1 and isinstance(pages[0], dict):
        return pages[0]
    raise ScanError("invalid pagination")


def check_text(rx: re.Pattern[str], *, label: str, text: str) -> list[Finding]:
    lines = sorted({text.count("\n", 0, match.start()) + 1 for match in rx.finditer(text)})
    return [Finding(f"{label}, line {line}") for line in lines]


def added_lines(patch: str) -> list[tuple[int, str]]:
    """Parse complete unified hunks, rejecting omitted or malformed content."""
    added: list[tuple[int, str]] = []
    old_left = new_left = 0
    new_line = 0
    in_hunk = False
    for line in patch.split("\n"):
        hunk = re.fullmatch(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", line)
        if hunk:
            if old_left or new_left:
                raise ScanError("truncated hunk")
            old_left = int(hunk[1] or 1)
            new_line = int(hunk[2])
            new_left = int(hunk[3] or 1)
            in_hunk = True
        elif line == r"\ No newline at end of file":
            continue
        elif not line and not old_left and not new_left:
            continue
        elif not in_hunk or not line:
            raise ScanError("invalid hunk")
        elif line[0] == "+":
            added.append((new_line, line[1:]))
            new_line += 1
            new_left -= 1
        elif line[0] == "-":
            old_left -= 1
        elif line[0] == " ":
            old_left -= 1
            new_left -= 1
            new_line += 1
        else:
            raise ScanError("invalid hunk line")
        if old_left < 0 or new_left < 0:
            raise ScanError("hunk overflow")
    if old_left or new_left or not in_hunk:
        raise ScanError("incomplete patch")
    return added


def check_added(rx: re.Pattern[str], lines: list[tuple[int, str]], ordinal: int) -> list[Finding]:
    blocks: list[list[tuple[int, str]]] = []
    for line in lines:
        if not blocks or line[0] != blocks[-1][-1][0] + 1:
            blocks.append([])
        blocks[-1].append(line)
    findings = []
    for block in blocks:
        content = "\n".join(value for _, value in block)
        numbers = {block[0][0] + content.count("\n", 0, match.start()) for match in rx.finditer(content)}
        findings.extend(Finding(f"file {ordinal}, added line {line}") for line in sorted(numbers))
    return findings


def decode_patchless(content: bytes) -> str:
    """Decode text without losing bytes; only a PDF binary marker may use Latin-1."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        # A PDF header may be followed by a binary marker comment. Preserve and
        # scan its high bytes one-to-one; everything else still requires UTF-8.
        marker = re.match(rb"(%PDF-(?:1\.[0-7]|2\.0)\r?\n)(%[\x80-\xff]{4,})(\r?\n)", content)
        if marker:
            try:
                return (marker[1].decode("ascii") + marker[2].decode("latin-1")
                        + content[marker.end(2):].decode("utf-8"))
            except UnicodeDecodeError:
                pass
        raise ScanError("unsupported text encoding") from None


def check_patchless(rx: re.Pattern[str], repo: str, file: dict, ordinal: int) -> list[Finding]:
    """Check a file GitHub lists without a patch, or raise when its new text cannot be read whole."""
    # Never trust the extension. NUL in the first 8 KiB follows Git's binary
    # heuristic. Larger blobs need human review.
    blob = gh_api(f"repos/{repo}/git/blobs/{sha(file['sha'])}")
    size = count(blob["size"])
    if size > 10 * 1_048_576 or blob["encoding"] != "base64":
        raise ScanError("blob too large")
    content = base64.b64decode("".join(text(blob["content"]).split()), validate=True)
    if len(content) != size:
        raise ScanError("incomplete blob")
    if b"\0" in content[:8000] or (not content and count(file["additions"]) == 0):
        return []
    # GitHub also omits patches for files it renders as binary, such as small
    # PDFs with no NUL byte. A new file's whole content is its added text.
    if file["status"] != "added" or size > 1_048_576:
        raise ScanError("missing text patch")
    lines = decode_patchless(content).split("\n")
    return check_added(rx, list(enumerate(lines, 1)), ordinal)


def compare(repo: str, base: str, head: str) -> Comparison:
    # Unlike the PR commits endpoint, compare pages past 250 commits. Every
    # page repeats the total, which proves that no page went missing.
    pages = gh_pages(f"repos/{repo}/compare/{sha(base)}...{sha(head)}?per_page=100")
    if not all(isinstance(page, dict) for page in pages):
        raise ScanError("invalid pagination")
    totals = {count(page["total_commits"]) for page in pages}
    merge_bases = {sha(page["merge_base_commit"]["sha"]) for page in pages}
    commits = tuple(commit for page in pages for commit in page["commits"])
    if len(totals) != 1 or len(merge_bases) != 1 or len(commits) != totals.pop():
        raise ScanError("incomplete commits")
    if len({sha(commit["sha"]) for commit in commits}) != len(commits):
        raise ScanError("duplicate commits")
    return Comparison(merge_bases.pop(), commits)


def pr_commits(repo: str, endpoint: str, pr: PullRequest) -> list[dict]:
    listed = gh_api(endpoint + "/commits?per_page=100")
    if not isinstance(listed, list):
        raise ScanError("incomplete commits")
    commits = listed
    if pr.commits > 250:
        # The PR endpoint stops at 250 commits. Compare lists the same range
        # in full, and must contain every commit the PR endpoint did list.
        commits = list(compare(repo, pr.base, pr.head).commits)
        if not {sha(c["sha"]) for c in listed} <= {sha(c["sha"]) for c in commits}:
            raise ScanError("inconsistent commits")
    if len(commits) != pr.commits:
        raise ScanError("incomplete commits")
    if len({sha(commit["sha"]) for commit in commits}) != len(commits):
        raise ScanError("duplicate commits")
    return commits


def merged_head(repo: str, number: int, pr: PullRequest) -> str | None:
    """Head commit of the newest other merged PR from this branch into the same base."""
    head = quote(f"{repo.split('/')[0]}:{pr.branch}", safe="")
    pulls = gh_api(f"repos/{repo}/pulls?state=closed&base={quote(pr.base_branch, safe='')}"
                   f"&head={head}&per_page=100")
    if not isinstance(pulls, list):
        raise ScanError("invalid pull list")
    merged = [pull for pull in pulls
              if pull["merged_at"] and identifier(pull["number"]) != number
              and (pull["head"]["repo"] or {}).get("full_name") == repo]
    if not merged:
        return None
    return sha(max(merged, key=lambda pull: text(pull["merged_at"]))["head"]["sha"])


def carried_commits(repo: str, number: int, pr: PullRequest, commits: list[dict]) -> set[str]:
    """SHAs of this PR's commits that an earlier merged PR from the same branch already carried.

    A squash merge never makes the branch's commits part of the base, so a
    release train's next PR lists every earlier train's commits again. Those
    messages merged once and stay in the branch's public history; rescanning
    them would fail every later train on text nobody can change.
    """
    previous = merged_head(repo, number, pr)
    if previous is None:
        return set()
    since = compare(repo, previous, pr.head)
    if since.merge_base != previous:
        # The branch no longer descends from the merged head: scan everything.
        return set()
    new = {sha(commit["sha"]) for commit in since.commits}
    return {sha(commit["sha"]) for commit in commits} - new


def content_unchanged(repo: str, base: str, file: dict) -> bool:
    if (file["status"] not in {"modified", "renamed", "changed"}
            or count(file["additions"]) or count(file["deletions"])):
        return False
    path = text(file["previous_filename"] if file["status"] == "renamed" else file["filename"])
    original = gh_api(f"repos/{repo}/contents/{quote(path, safe='/')}?ref={base}")
    return original["type"] == "file" and sha(original["sha"]) == sha(file["sha"])


def check_pr(rx: re.Pattern[str], *, repo: str, number: int, expected_head: str | None = None) -> list[Finding]:
    endpoint = f"repos/{repo}/pulls/{identifier(number)}"
    before = PullRequest.read(gh_api(endpoint))
    if before.head_repo != repo or (expected_head and before.head != expected_head):
        raise ScanError("superseded or foreign head")
    # REST caps the files collection even when pagination follows every page.
    if before.files > 3000:
        raise ScanError("collection cap")
    findings = check_text(rx, label="PR title", text=before.title)
    findings += check_text(rx, label="PR description", text=before.body)
    findings += check_text(rx, label="head branch name", text=before.branch)
    commits = pr_commits(repo, endpoint, before)
    carried = carried_commits(repo, number, before, commits)
    files = gh_api(endpoint + "/files?per_page=100")
    if not isinstance(files, list) or len(files) != before.files:
        raise ScanError("incomplete files")
    if len({text(file["filename"]) for file in files}) != len(files):
        raise ScanError("duplicate files")
    for i, commit in enumerate(commits, 1):
        if sha(commit["sha"]) in carried:
            continue
        findings += check_text(rx, label=f"commit {i} message", text=text(commit["commit"]["message"]))
    for i, file in enumerate(files, 1):
        if file["status"] == "removed":
            continue
        if file["status"] not in {"added", "modified", "renamed", "copied", "changed", "unchanged"}:
            raise ScanError("unknown file status")
        findings += check_text(rx, label=f"file {i} path", text=text(file["filename"]))
        patch = file.get("patch")
        if not patch:
            if not content_unchanged(repo, before.base, file):
                findings += check_patchless(rx, repo, file, i)
            continue
        lines = added_lines(text(patch))
        if len(lines) != count(file["additions"]):
            raise ScanError("incomplete added lines")
        findings += check_added(rx, lines, i)
    if before != PullRequest.read(gh_api(endpoint)):
        raise ScanError("PR changes during scan")
    return findings


def is_fork_event(repo: str, event_name: str, event: dict) -> bool:
    if event_name not in {"pull_request", "pull_request_review", "pull_request_review_comment"}:
        return False
    pull = event["pull_request"]
    # A deleted head repository is not evidence of a fork exception.
    head_repo = pull["head"]["repo"]["full_name"]
    base_repo = pull["base"]["repo"]["full_name"]
    if not head_repo or base_repo != repo:
        raise ScanError("invalid repository identity")
    return head_repo != repo


def check_event(rx: re.Pattern[str], *, repo: str, event_name: str, event: dict) -> list[Finding]:
    if event_name == "pull_request":
        pull = event["pull_request"]
        return check_pr(rx, repo=repo, number=pull["number"], expected_head=sha(pull["head"]["sha"]))
    if event_name == "issues":
        endpoint = f"repos/{repo}/issues/{identifier(event['issue']['number'])}"
        labels = (("title", "issue title"), ("body", "issue description"))
    elif event_name == "issue_comment":
        endpoint = f"repos/{repo}/issues/comments/{identifier(event['comment']['id'])}"
        labels = (("body", "comment"),)
    elif event_name == "pull_request_review":
        endpoint = (f"repos/{repo}/pulls/{identifier(event['pull_request']['number'])}"
                    f"/reviews/{identifier(event['review']['id'])}")
        labels = (("body", "review summary"),)
    elif event_name == "pull_request_review_comment":
        endpoint = f"repos/{repo}/pulls/comments/{identifier(event['comment']['id'])}"
        labels = (("body", "review comment"),)
    else:
        raise ScanError("unsupported event")
    before = gh_api(endpoint)
    fields = tuple(text(before[key]) for key, _ in labels)
    findings = [finding for (_, label), value in zip(labels, fields)
                for finding in check_text(rx, label=label, text=value)]
    after = gh_api(endpoint)
    if fields != tuple(text(after[key]) for key, _ in labels):
        raise ScanError("text changes during scan")
    return findings


def main() -> int:
    try:
        repo, event_name = os.environ["REPO"], os.environ["EVENT_NAME"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ScanError("invalid repository")
        with open(os.environ["EVENT_PATH"], encoding="utf-8") as handle:
            event = json.load(handle)
        if is_fork_event(repo, event_name, event):
            print("::notice::Fork event: NOT SCANNED. A maintainer must inspect public text before merging.")
            return 0
        pattern = os.environ.get("PUBLIC_TEXT_DENYLIST", "")
        if not pattern.strip():
            print("::error::Public-text configuration is missing. This event is NOT SCANNED.")
            return 2
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            print("::error::Public-text configuration is invalid. This event is NOT SCANNED.")
            return 2
        findings = check_event(rx, repo=repo, event_name=event_name, event=event)
    except Exception:
        # API stderr, malformed input and exception messages can contain restricted
        # text or annotation syntax. Do not print any part of them, even on failure.
        print("::error::Public-text scan is incomplete or superseded. Review configuration, API access and data limits; rerun on current text.")
        return 2
    for finding in findings:
        print(f"::error::Restricted text in {finding.where}")
    if findings:
        print("Replace restricted text with a neutral description. Older revisions and pushed history need separate owner review.")
        return 1
    print("Current public text passes the configured scan. Binary content and intermediate commit file content are outside its scope.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
