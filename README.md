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
freeze/unfreeze cycle) fails. It posts its own failure-shaped message (repo,
pipeline label, workflow, branch, commit, triggering actor, run link) rather
than the deploy-notification composite, whose copy is deploy-specific and reads
wrong for non-deploy pipelines. It uses the same Slack bot token.

Add one terminal job per push-triggered workflow that depends on **every** job
in the workflow and runs only `if: failure()`. Depend on all jobs, not just the
leaves: a mid-graph failure skips its dependents (which is not a failure), so a
notify job that only needs the leaves could itself be skipped.

```yaml
  notify-failure:
    needs: [linter, run-unit-tests, build, scan, migrate, deploy, integration-tests]  # every job
    if: failure()
    uses: mindsdb/github-actions/.github/workflows/notify-main-failure.yml@<sha> # v1
    with:
      env-name: "prod build+deploy"     # short label for the failing pipeline
    secrets: inherit
```

For a freeze/unfreeze wrapper, `needs:` its single job and label it accordingly
(`env-name: "staging freeze"`). Pass `runs-on: ubuntu-latest` for repos without
the self-hosted `mdb-dev` runner. For a workflow that also runs on
`pull_request`, guard the job with `if: failure() && github.event_name == 'push'`
so PR-run failures (which the author already sees) don't alert the channel.

Requires two org secrets, reaching the workflow via `secrets: inherit`:
`SLACK_ENG_CHANNEL_ID` (the engineering channel; distinct from the deploy-chatter
`SLACK_DEPLOYMENTS_CHANNEL_ID`) and `GH_ACTIONS_SLACK_BOT_TOKEN`. The Slack bot
must be a member of that channel.

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
