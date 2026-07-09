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
| `staging-freeze.yml` | `Staging Freeze` | Activates the `staging-freeze` ruleset to lock staging (skips if staging == main) |
| `weekly-merge-staging.yml` | `Create staging to main release PR` | Opens the `staging → main` PR (idempotent) |
| `staging-unfreeze.yml` | `Staging Unfreeze` | Disables the ruleset when the release PR merges, then syncs `main` back into `staging` |

The chain is event-driven: `Staging Freeze` finishing fires the release-PR
workflow via `workflow_run`; merging that PR fires `Staging Unfreeze`. The
`workflow_run` link matches on the **caller** workflow's name, so callers must
keep the names above verbatim.

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
    uses: mindsdb/github-actions/.github/workflows/staging-freeze.yml@main
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
    uses: mindsdb/github-actions/.github/workflows/weekly-merge-staging.yml@main
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
    uses: mindsdb/github-actions/.github/workflows/staging-unfreeze.yml@main
    secrets: inherit
```
