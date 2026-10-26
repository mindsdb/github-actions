# Public text guard

The guard fails a PR or text-audit run when current public text matches the private `PUBLIC_TEXT_DENYLIST` regular expression. It reports a field, ordinal and line number without quoting the text or its filename. A failure catches a disclosure after GitHub saves it; the guard cannot prevent publication or erase older revisions.

## Coverage

| Event | Text read from the API | Result context |
| --- | --- | --- |
| PR opened, edited, synchronized or reopened | Current title, description, head branch, commit messages an earlier merged PR from the branch did not carry, current added lines and paths | `public-text / check` (verify on the first live run) |
| Issue opened or edited | Current title and description | `public-text-audit / check` |
| Discussion comment created or edited | Current comment body | `public-text-audit / check` |
| Review submitted or edited | Current review summary | `public-text-audit / check` |
| Inline review comment created or edited | Current comment body | `public-text-audit / check` |

The PR and audit callers have separate workflow names, job names and concurrency groups. A successful comment audit cannot satisfy the PR check. Reruns read current text. The scanner reads the same fields again before reporting success, and fails if they change. A PR run for an older head fails instead of reporting on a newer head under the old check.

Release trains receive the same scan as any other PR. The scanner compares paginated collection sizes with the PR's counts. The PR commits endpoint stops at 250 commits, so a larger PR lists its commits through the compare endpoint, which must return the PR's exact count and every commit the PR endpoint listed. More than 3,000 files exceeds the files endpoint's limit and fails as incomplete. Duplicate data, malformed hunks and added-line count mismatches also fail.

A squash merge never makes the head branch's commits part of the base, so a release train's next PR lists every earlier train's commits again. The scanner skips commits that the newest earlier merged PR from the same branch into the same base already carried, when the current head still descends from that PR's head. Those messages merged once and stay in the branch's public history; failing on them would block every later train on text nobody can change. Every other commit message is checked. If the head no longer descends from the merged head, every commit is checked. The release-train PR description lists the same new commits, so a carried commit's subject does not return through the description.

Removed paths are excluded so a cleanup can remove a disclosure. Renames check only the new path and added content. A modified or renamed file with zero additions and zero deletions can omit its patch only when the scanner verifies that its blob equals the base file's blob, or verifies empty/binary content. This permits mode-only changes and unchanged renames without trusting line counts alone. A missing base path fails conservatively.

For other missing patches, the scanner reads the blob as data. It accepts an empty file, and treats a blob of up to 10 MiB with a NUL byte within the first 8,000 bytes as binary, checking only its path. GitHub also omits patches for files it renders as binary without a NUL byte, such as small PDFs. For an added file of up to 1 MiB with no NUL byte, the whole content is its added text, so the scanner checks all of it. Any other missing patch fails and needs maintainer review: a larger blob, or a modified file whose new content has no patch. Text stored with NUL bytes, such as UTF-16, counts as binary.

Added text is checked in contiguous blocks so a wrapped phrase can match. The scanner does not join additions across intervening unchanged lines.

## Trust and exceptions

The reusable checks out only `mindsdb/github-actions` at `job.workflow_sha`, the commit of the called workflow. It never checks out contributor code. The token has only contents, issues and pull-request read permissions. Callers pass only the named secret. Existing repository convention makes callers track `@main`; a changed shared workflow takes effect for every caller after merge.

| Situation | Behavior |
| --- | --- |
| Same-repository PR with missing or invalid secret | Fails, unscanned |
| Dependabot event without the configured secret | Fails, unscanned; Dependabot runs receive only Dependabot secrets, so configure a Dependabot copy of the pattern or handle each Dependabot PR manually |
| Fork PR, review or inline comment | Explicit successful exception labeled `NOT SCANNED`; maintainer inspection required |
| Discussion comment on a fork PR | Default-branch audit reads and checks the comment |
| API failure, deleted text, unavailable head, incomplete data or concurrent edit | Fails, incomplete or superseded |

The guard protects against accidental disclosures. Contributors who can alter trusted workflows or use privileged repository settings can bypass it. A fork exception is not a clean scan. This design does not add a privileged `pull_request_target` workflow.

## Public-writing policy

Use neutral roles and public component contracts. Do not identify customers, partners or users, or disclose private repository names, links, PR or issue references, release versions or schedules. Generic deployment terminology and public configuration values remain allowed.

Maintainers supply restricted terms and aliases through the private regular expression. Do not put real terms in this repository's code, tests, workflow configuration, examples or logs. Fixtures use synthetic text only. The regular expression detects configured strings; it cannot infer that an otherwise ordinary version number or schedule refers to a private project. Human review must check that context.

The secret keeps the configured list out of source, but public pass/fail results can reveal whether a guessed term matches. The check is not a secrecy guarantee for the list. Use a bounded, reviewed expression; pathological regular expressions can exhaust the job timeout.

## Rollout

1. Merge the shared scanner, tests, reusable and this repository's callers first. The initial implementation PR cannot resolve its new `@main` reusable until that merge; use the existing CI and local checks for bootstrap review. Do not require the new context before it exists. Until step 2, this repository's own PRs report the missing-secret failure.
2. An operator configures `PUBLIC_TEXT_DENYLIST` as an organization Actions secret visible only to selected repositories, including this one, and a Dependabot secret with the same value for the same repositories. The pattern includes a synthetic canary such as `zqcanary7731`. Never put the pattern into a public ruleset or shell command history.
3. Merge each application caller into its integration branch, then promote it to the default branch. Issues and discussion-comment events depend on default-branch workflow availability. Callers that land before step 2 turn every same-repository PR red.
4. Verify a same-repository PR fails on the canary and passes after an edit removes it. Put the canary only in a description or comment, never in a commit message or branch name, and remove it before merge: a squash merge can copy the description or commit messages into history that later trains carry. Observe the actual Actions check context before requiring it for default-branch and staging merges.
5. Verify an audit can fail without replacing the PR check. Edit the audited text, rerun the old event, and verify that the latest body is checked. Test the explicit fork exception and missing-secret failure.
6. The operator installs the verified PR context as a required GitHub Actions check. Require the full `public-text / check` context with the GitHub Actions source, never a bare `check`, which the audit job shares. Automation that pushes the protected branch directly, such as a back-merge from the default branch into staging, needs a bypass entry. Required-workflow rules alone do not cover edited PR descriptions.

Workflow authors do not run these operator steps automatically. Missing configuration deliberately blocks same-repository runs. Coordinate the rollout before making the check required. A release train's first scan after activation checks every commit since the last merged train, so a disclosure there can block a merge even when the current diff is clean; it needs a maintainer decision because pushed history cannot change.

## Limits and response

GitHub may not deliver these events in every case. PR workflows do not run on merge conflicts; supported CI-skip directives can suppress runs; most writes made with `GITHUB_TOKEN` do not trigger a new workflow. Review these cases manually before merge. A required check cannot make an event happen.

The scanner does not read intermediate commit file content, binary content, release descriptions, old text revisions, commit messages an earlier merged PR from the same branch already carried, or every existing issue and comment. Added-then-removed content can remain in commit history. A clean result concerns the configured pattern and scanned fields only. Editing a current description leaves older revisions visible; the repository owner handles history cleanup and pushed-commit rewrites separately.

Actions notifications depend on each person's notification settings. A failed audit creates a failed run, not a guaranteed message to its author. This workflow posts no public comments and sends no chat messages.

## Local verification

```sh
uv run --with pyyaml --with pytest --python 3.12 -m pytest tests/ -q
actionlint
uv run --with pyyaml --python 3.12 scripts/workflow_graph.py --workflow-dir .github/workflows --allow-external-reusables
zizmor .github/workflows
```

Unit tests use synthetic API responses and patterns, exercise incomplete and stale data, and assert that failures never echo sensitive text. The live event matrix remains an operator acceptance step after publication.
