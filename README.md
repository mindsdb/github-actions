# github-actions

Use an action from this repo in your workflow like this:

```
- name: Pull MindsDB Github Actions
  uses: actions/checkout@v4
  with:
    repository: mindsdb/github-actions
    path: github-actions
- uses: ./github-actions/<action-name>
```

**NOTE: This needs to go AFTER any `actions/checkout` step for the current repo**

## Release-train reusable workflows

Three reusable workflows automate the weekly `staging → main` release cycle.
They live in `.github/workflows/` and are called from ~25-line per-repo wrappers
(same pattern as `stale-deploy-label.yml`):

| Reusable workflow | Name (keep identical in callers) | What it does |
|---|---|---|
| `release-freeze.yml` | `Staging Freeze` | Activates the `staging-freeze` ruleset to lock staging (skips if staging == main) |
| `release-pr.yml` | `Create staging to main release PR` | Opens the `staging → main` PR (idempotent) |
| `release-unfreeze.yml` | `Staging Unfreeze` | Disables the ruleset when the release PR merges, then syncs `main` back into `staging` |

The chain is event-driven: `Staging Freeze` finishing fires the release-PR
workflow via `workflow_run`; merging that PR fires `Staging Unfreeze`. The
`workflow_run` link matches on the **caller** workflow's name, so callers must
keep the names above verbatim.

## Merge-to-main panic alerts

`notify-main-failure.yml` posts a "pipeline failed" alert to the engineering
Slack channel when a workflow that runs on push to `main`/`staging` (or the
freeze/unfreeze cycle) fails, and a green "recovered" message on the first run
that goes green again afterwards. It posts its own message (repo, pipeline label,
workflow, branch, commit, triggering actor, run link) rather than the
deploy-notification composite, whose copy is deploy-specific and reads wrong for
non-deploy pipelines. It uses the same Slack bot token.

Add **one** terminal job per push-triggered workflow that depends on **every**
job in the workflow. Depend on all jobs, not just the leaves: a mid-graph failure
skips its dependents (which is not a failure), so a notify job that only needs
the leaves could itself be skipped.

A `uses:` job cannot branch on status, so instead of one job per outcome the
single job runs unless the workflow was cancelled and derives the outcome from
`needs.*.result`:

```yaml
  notify:
    needs: [linter, run-unit-tests, build, scan, migrate, deploy, integration-tests]  # every job
    if: ${{ !cancelled() && !contains(needs.*.result, 'cancelled') }}
    permissions:
      contents: read
      actions: read                     # the prior-run lookup behind recovery
    uses: mindsdb/github-actions/.github/workflows/notify-main-failure.yml@<sha> # v1
    with:
      env-name: "prod build+deploy"     # short label for the pipeline
      status: ${{ contains(needs.*.result, 'failure') && 'failed' || 'recovered' }}
    secrets: inherit
```

`recovered` is not the same as green: the reusable looks up the previous
conclusive run of the same workflow on the same branch and stays silent unless it
failed, so a routine green merge posts nothing. That lookup needs `actions: read`
on this job, because the default workflow token carries contents + packages read
only and a called workflow can never hold more than its caller grants. Without
it the lookup is refused and the job stays silent (it never fails the run), so a
missing recovery message is the symptom to look for.

For a freeze/unfreeze wrapper, keep it failure-only: `needs:` its single job,
`if: failure()`, the default `status: failed`, no `permissions:` block (the
prior-run lookup only runs in recovered mode), and label it accordingly
(`env-name: "staging freeze"`). Pass `runs-on: ubuntu-latest` for repos without
the self-hosted `mdb-dev` runner. For a workflow that also runs on
`pull_request`, add `&& github.event_name == 'push'` to the `if:` so PR-run
failures (which the author already sees) don't alert the channel.

Requires two org secrets, reaching the workflow via `secrets: inherit`:
`SLACK_ENG_CHANNEL_ID` (the engineering channel; distinct from the deploy-chatter
`SLACK_DEPLOYMENTS_CHANNEL_ID`) and `GH_ACTIONS_SLACK_BOT_TOKEN`. The Slack bot
must be a member of that channel.

`workflow_dispatch` on the reusable itself is a smoke test: it posts a sample
message in either style, skipping the prior-run lookup so `recovered` always
posts.

## CalVer releases

`calver-release.yml` cuts the `v<major>.<yy>.<m>.<d>.<seq>` tag and its GitHub
Release for the calling repo, picking the next unused sequence for today so
several releases a day are fine. Call it as the first job of a push-to-main
release pipeline and read `tag` / `version` from its outputs:

```yaml
  auto-release:
    permissions:
      contents: write                   # tag push + release creation
    uses: mindsdb/github-actions/.github/workflows/calver-release.yml@<sha> # v1
    with:
      calver-major: "2"                 # per-repo constant
      runs-on: mdb-dev                  # optional, defaults to ubuntu-latest

  build:
    needs: auto-release
    uses: ./.github/workflows/prod-build.yml
    with:
      tag: ${{ needs.auto-release.outputs.tag }}
```

Two guards stay in the caller: `concurrency:` (one release at a time, caller owns
the group name) and skipping auto-version commits with
`if: "!contains(github.event.head_commit.message, '[skip ci]')"`.

## CLA assistant

`cla-assistant.yml` runs the contributor-agreement check on the public repos. The
wrapper keeps the `issue_comment` + `pull_request_target` triggers (the action
reads those payloads directly) and the four write permissions, and passes the
per-repo agreement URL and allowlist:

```yaml
  cla:
    uses: mindsdb/github-actions/.github/workflows/cla-assistant.yml@<sha> # v1
    with:
      path-to-document: 'https://github.com/mindsdb/mindsdb/blob/main/assets/contributions-agreement/individual-contributor.md'
      allowlist: bot*, ZoranPandovski, ...
```

Signatures are committed to the calling repo's own `cla` branch, so each repo
keeps its own ledger.

### Prerequisites (provisioned once, org level, scoped to the release-train repos)

- **`mindsdb-release-train` GitHub App** with `Administration`, `Contents`, and
  `Pull requests: write`, installed on each repo, and set as a **bypass actor**
  on the `staging` ruleset. Per-job tokens are minted with
  `actions/create-github-app-token`.
- **`vars.RELEASE_APP_CLIENT_ID`** (org variable) and
  **`secrets.RELEASE_APP_PRIVATE_KEY`** (org secret) — the private key reaches
  the reusable workflows via `secrets: inherit` in the caller.
- A **pre-provisioned `staging-freeze` ruleset** in each repo: one `update` rule
  targeting `staging`, created `disabled`, with the App as bypass actor. The
  workflows only flip its `enforcement` between `active` and `disabled` — they
  never touch the underlying branch protection.

### Caller wrappers

Drop these three files into each repo's `.github/workflows/`. Adjust the cron
per repo if desired; branch names default to `staging`/`main`.

> **Pinning:** the examples below use `@main` for readability. For production,
> pin each `uses:` to a full commit SHA with a version comment
> (e.g. `…/release-freeze.yml@<sha> # v1`) so Dependabot can manage bumps.

`staging-freeze.yml`:

```yaml
name: Staging Freeze
on:
  schedule:
    # Friday 13:47 UTC — off the top of the hour (GitHub's documented high-load
    # slot) and off a DST-sensitive "6am PST" wording.
    - cron: '47 13 * * 5'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  freeze:
    uses: mindsdb/github-actions/.github/workflows/release-freeze.yml@main
    secrets: inherit
```

`weekly-merge-staging.yml`:

```yaml
name: Create staging to main release PR
on:
  workflow_run:
    workflows: ["Staging Freeze"]
    types: [completed]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  create-pr:
    if: >
      github.event_name == 'workflow_dispatch' ||
      github.event.workflow_run.conclusion == 'success'
    uses: mindsdb/github-actions/.github/workflows/release-pr.yml@main
    secrets: inherit
```

`staging-unfreeze.yml`:

```yaml
name: Staging Unfreeze
on:
  pull_request:
    types: [closed]
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  unfreeze:
    if: >
      github.event_name == 'workflow_dispatch' ||
      (github.event.pull_request.merged == true &&
       github.event.pull_request.head.ref == 'staging' &&
       github.event.pull_request.head.repo.full_name == github.repository)
    uses: mindsdb/github-actions/.github/workflows/release-unfreeze.yml@main
    secrets: inherit
```
