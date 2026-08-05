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

Four reusable workflows automate the weekly `staging → main` release cycle.
They live in `.github/workflows/` and are called from ~25-line per-repo wrappers
(same pattern as `stale-deploy-label.yml`):

| Reusable workflow | Name (keep identical in callers) | What it does |
|---|---|---|
| `release-freeze.yml` | `Staging Freeze` | Activates the `staging-freeze` ruleset to lock staging (skips if staging == main) |
| `release-pr.yml` | `Create staging to main release PR` | Opens the `staging → main` PR (idempotent) |
| `release-unfreeze.yml` | `Staging Unfreeze` | Disables the ruleset when the release PR merges, then syncs `main` back into `staging` |
| `sync-main-to-staging.yml` | `Sync main to staging` | Merges `main` into `staging` after **any** push to main, not just the release merge |

`release-unfreeze.yml` syncs main back only on the release path, because its wrapper's guard requires the merged PR's head branch to be `staging`. A commit that reaches main any other way (a hotfix PR, a revert, a direct merge) fires nothing, and on a squash-merge repo that leaves the next release PR diffed against a `main` that `staging` does not contain. `sync-main-to-staging.yml` closes that window on `push: main`.

Both push `staging` as the release-train App, and merging the release PR fires both, so a caller that installs both **must put them in the same `concurrency` group**:

```yaml
concurrency:
  group: sync-main-to-staging
  cancel-in-progress: false
```

The sync is idempotent (it exits 0 when `staging` already contains `main`), so whichever run loses the race no-ops.

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

### Scoping staging alerts to the freeze window

A staging failure is not the same event all week. Once the freeze window is open, `staging` is the release candidate and a red pipeline blocks the release. Before it, `staging` is the integration branch and the same red is routine, so paging the channel for it is what teaches people to scroll past the channel.

`freeze-scoped: true` on a **staging** caller implements that policy:

| Where it failed | Freeze window | Result |
|---|---|---|
| `main` | n/a | red alert |
| `staging` | open | red alert |
| `staging` | not open | nothing posted |

```yaml
    with:
      env-name: "staging build+deploy"
      status: ${{ contains(needs.*.result, 'failure') && 'failed' || 'recovered' }}
      freeze-scoped: true
```

Add `outside-freeze: notice` to post a muted grey `:warning:` message instead of nothing, for a caller that wants the trail. Silence is the default; the run is still red in the Actions tab either way, and `pipeline-watchdog.yml` still covers a staging branch that stops building at all.

A workflow triggered on **both** main and staging from one notify job must not hardcode `true`, or main failures get silenced too. Pass an expression:

```yaml
      freeze-scoped: ${{ github.ref_name == 'staging' }}
```

Freeze state is read from the `staging-freeze` ruleset itself, not inferred from workflow history — a freeze that skipped itself because staging had nothing unreleased still concludes `success`, and history cannot tell that apart from a real freeze. Reading rulesets needs admin, so this reuses the same App that toggles them (`vars.RELEASE_APP_CLIENT_ID` + `secrets.RELEASE_APP_PRIVATE_KEY`); no extra `permissions:` on the caller. If the App token or the ruleset lookup fails, it escalates to the red alert rather than downgrading, so a lookup problem can never silence a real release-blocking failure.

Leave `freeze-scoped` off for prod and freeze/unfreeze callers: a failure there is always worth interrupting for.

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

### The one failure it cannot see: a run that never started

`notify-main-failure` is a job *inside* the pipeline, so it covers every failure where the run exists. It cannot cover `conclusion: startup_failure` — GitHub rejecting the run at load time, with **zero jobs** — because there is no job to put the alert in. The run appears in the Actions tab and nowhere else, and the branch is not deployed.

That is not a corner case. It is produced by a caller job granting narrower `permissions:` than a called workflow's jobs declare (the cap is checked when the file *loads*, not when a job runs), by a `uses: ./.github/workflows/x.yml` path that does not exist on the ref, and by malformed YAML. It has already cost auth `staging` ten hours of running the previous image across two merges, with the recovery message being the first thing the channel heard about it.

`notify-startup-failure.yml` watches from outside, on a schedule. It sweeps the deploy branches and alerts when the newest conclusive run of a workflow is a `startup_failure` inside the lookback window, restricted to runs where no job ran — precisely the set the in-run notify job could not have reported:

```yaml
name: Pipeline watchdog
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  startup-failures:
    permissions:
      contents: read
      actions: read                     # run + job history for the sweep
    uses: mindsdb/github-actions/.github/workflows/notify-startup-failure.yml@main
    with:
      branches: "main staging"
      lookback-minutes: 90
    secrets: inherit
```

Alerts repeat, by design: one failing push produces about three messages at that cadence and window, and then silence. Alerting only on the *transition* into a broken state gives exactly one message per break, which was rejected after replaying it against the auth incident — it would have said nothing about the second failing push, since the run before that one had also failed to start. A stateless sweep cannot be exactly-once, so the choice is a message that can be missed or a few that cannot, and the window is what bounds the few.

Two things to know when adding it. GitHub only runs `schedule` triggers from a repository's **default branch**, so a watchdog merged to `staging` and no further is inert — `workflow_dispatch` is there to prove it before it reaches `main`. And widening `lookback-minutes` on a manual dispatch replays a past incident, which is how to check it would have caught one.

## Workflow lint

`workflow-lint.yml` lints the CALLING repo's workflows. Three layers, only one of which is ours:

| Layer | Blocking | What it is |
| --- | --- | --- |
| `actionlint` | yes | The established syntax + expression + shellcheck linter |
| permission check | yes | `scripts/workflow_graph.py` — the one gap neither tool covers |
| `zizmor` | advisory by default | The established Actions *security* auditor (template injection, credential persistence, unpinned actions) |

```yaml
  workflow-lint:
    permissions:
      contents: read
    uses: mindsdb/github-actions/.github/workflows/workflow-lint.yml@main
    with:
      default-permissions: read     # Settings -> Actions -> Workflow permissions
    secrets: inherit
```

`zizmor` is advisory because pointing it at existing pipelines surfaces a backlog, and a lint job that is red on arrival gets ignored rather than fixed. Flip `zizmor-blocking: true` per repo once its backlog is triaged.

**The permission check is not redundant with either tool**, which was verified against a tree that had already broken a deploy branch: `actionlint` reported nothing, and `zizmor --persona=auditor` reported only its generic "no `permissions:` block" note, equally true of jobs that work fine. What it checks is that no called workflow declares a permission its caller lacks. GitHub caps a called workflow at the calling job's grant and enforces that cap when the workflow **file is loaded**, so a job in a shared reusable naming a scope one caller does not grant does not run with less — it rejects that caller's whole run as a `startup_failure` with **zero jobs**, which means the pipeline's own notify job cannot report it either. In `mindsdb/auth` that cost two silently undeployed merges to `staging` and ten hours of serving the previous image.

The rule it enforces: **a job in a shared reusable declares only what all its callers grant, and inherits anything only one of them needs.** Inheriting is the only thing that composes — the PR caller grants `pull-requests: write` and the comment posts, the push callers grant `contents: read` and the same job runs without a scope it never needed. And the reason it has to be a gate rather than a convention: a pull request only exercises the PR caller, which is usually the one that *does* grant the scope, so the mistake merges green and breaks on the merge commit.

It also refuses to read cluster secrets by hand — see `k8s-secret` below.

Blind spot to know about: it can only read local (`./.github/workflows/...`) callees, and lists remote ones as unchecked. The cap applies to those too, so a scope added to a reusable *here* must be granted by every consumer's calling job.

## Reading a Kubernetes secret

`k8s-secret` fetches one key from a Secret, fails when it is absent, masks it, and only then exports it — in that order, which is the part a hand-rolled fetch gets wrong, because `::add-mask::` only scrubs output that comes *after* it.

```yaml
      - uses: mindsdb/github-actions/k8s-secret@main
        with:
          namespace: pr-<repo>-123
          secret: <secret-name>
          key: <KEY_IN_THAT_SECRET>
          env-var: PR_KEYCLOAK_CLIENT_SECRET
```

### Which source a secret should come from

There is no blanket answer, and "prefer Kubernetes because it is the source of truth" is wrong for the credentials that matter most. Pick by what the value is:

| The value is | Source | Why |
| --- | --- | --- |
| Ephemeral and namespace-local (a per-PR environment's own credentials) | **Kubernetes**, via `k8s-secret` | No GitHub Environment can exist for `pr-<repo>-204`, so a copy is impossible; the namespace is the only source there is |
| A permanent environment's real credential (prod/staging DB, Stripe live, prod vendor tokens) | **GitHub Environment** | A job can only read it by declaring `environment: prod`, and that environment requires a reviewer. A k8s Secret has no such gate — *any* job on a runner with cluster read can fetch it, unreviewed |
| Needed to reach the cluster, or used on a GitHub-hosted runner | **GitHub secret** | Package-install tokens, the ArgoCD token that creates the namespace, Snyk, Slack. Reading these from the cluster is circular |

The middle row is the one worth being explicit about, because the intuition points the wrong way. Moving prod credentials out of a GitHub Environment and into a cluster read would *remove* the required-reviewer gate standing in front of them today, and it would force the jobs that use them onto `mdb-prod` (nothing else can reach newprod), which means granting *more* jobs prod cluster access. Both are the wrong direction.

What actually goes wrong with a GitHub secret is different and has a cheaper fix: a reference to a secret that does not exist resolves to the **empty string** rather than failing, so a rename reaches the vendor as no credential and comes back as an unexplained 401. The fix for that is to assert the value is non-empty and name it in the error, not to migrate its storage.

`k8s-secret` is also not a containment boundary. The ability to read Secrets belongs to the self-hosted runner, which holds cluster credentials because it deploys. What bounds *that* is who can trigger a run on such a runner — hence a `pull_request`-triggered job on a public repo needs `if: github.event.pull_request.head.repo.full_name == github.repository` — and the runner ServiceAccount's RBAC.

## PR environment comments

`pr-env-comment.yml` posts and keeps updating one comment saying where a PR's environment is and how to sign in. The account, Secret name, namespace, and hosts arrive as **inputs** — this repo is public, and while none of those is a credential, together they are a free recon package. The mechanism is shared; the facts stay in the private caller. A reusable that cannot be described without naming our infrastructure has not earned promotion here.

```yaml
  pr-env-comment:
    needs: [deploy-pr-env]
    # No `permissions:` here — see the permission rule above. The dev caller grants
    # `pull-requests: write`; the push callers must not have to.
    uses: mindsdb/github-actions/.github/workflows/pr-env-comment.yml@main
    with:
      env-name: pr-<repo>-${{ github.event.pull_request.number }}
      login-email: someone@example.com
      password-secret: some-secret
      password-key: SOME_KEY
      links: '[{"label":"Console","url":"https://..."}]'
    secrets: inherit
```

Layout is fixed and deliberate: who to sign in as and the one command that prints the password come first, links next, everything else in a collapsed `<details>`. It never posts the password itself — a PR comment is permanent, org-wide, and un-redactable.

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
