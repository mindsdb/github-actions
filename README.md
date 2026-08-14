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
| `release-pr.yml` | `Create staging to main release PR` | Keeps the `staging → main` PR open and current: a draft that lists what is queued, marked ready for review when the freeze opens |
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

### The release PR is open all week

The release queue used to be invisible until Friday: the only way to know what the next release would ship was to diff two branches by hand. So the release-PR wrapper also fires after each staging pipeline, and the reusable **updates** an open PR rather than skipping it. The commit list and contributor list are rewritten every run, so the PR is a live view of what would ship right now, from the first merge after an unfreeze.

**It opens as a draft and becomes ready at freeze time.** An always-open `staging → main` PR is otherwise a merge button sitting next to production all week, and the point of the freeze window is that staging is only a release candidate inside it. Draft is what makes the rolling PR safe, and the draft → ready transition is a visible signal that the window opened. It only ever moves draft → ready: re-drafting a PR a human deliberately marked ready would fight them.

The second trigger is `workflow_run`, not `push: staging`, deliberately. A second workflow on `push: staging` would be a second disconnected run tree, which the workflow lint forbids. Add the repo's own staging pipeline by name:

```yaml
on:
  workflow_run:
    workflows: ["Staging Freeze", "Staging - Build and Deploy on push to staging"]
    types: [completed]
```

### Wrappers carry no notify job

All four reusables **post their own Slack alert** as a final step. Wrappers used to own that job, written `if: failure()` with the default `status: failed`, which made all twenty-one of them structurally incapable of reporting that a failure had been FIXED. On 2026-08-14 cowork's `main → staging` sync failed and alerted, the re-run succeeded, and the notify job was *skipped* — leaving the channel holding a red alert for a sync that had already recovered.

So a wrapper is now triggers plus a `uses:`, and it grants `actions: read` so the recovery lookup works:

```yaml
jobs:
  freeze:
    permissions:
      contents: read
      actions: read                     # turns the "recovered" message on
    uses: mindsdb/github-actions/.github/workflows/release-freeze.yml@main
    secrets: inherit
```

The reusables do **not** declare `actions: read` themselves. Declaring it would make every caller that has not yet granted it fail to *load* rather than merely degrade, and these wrappers reach a repo's default branch only at the next weekly release, so the two can never be merged in step. Without the grant the lookup is refused, says so in the log, and stays quiet.

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

`freeze-scoped: true` on a **staging** caller implements that policy, in *both* directions:

| Where it happened | Freeze window | Result |
|---|---|---|
| `main`, failed or recovered | n/a | posted |
| `staging`, failed or recovered | open | posted |
| `staging`, failed or recovered | not open | **nothing posted** |

```yaml
    with:
      env-name: "staging build+deploy"
      status: ${{ contains(needs.*.result, 'failure') && 'failed' || 'recovered' }}
      freeze-scoped: true
```

**That last row means both halves.** It used to mean only the failure half, and the recovery posted regardless — so the channel was told a staging pipeline had recovered from a failure it was never told about. On 2026-08-13 cowork-server's staging publish failed on the commit that *fixed* a broken release candidate; the failure was correctly silent, and the re-run three and a half hours later posted a green "Recovered". The only thing anyone heard about a stalled rc stream was the good news. Half a story is worse than none: it reads as noise at best and as a resolved prod incident at worst.

There is no muted middle setting. Outside the window a staging pipeline posts nothing at all — no red, no green, no grey. The run is still red in the Actions tab, and `pipeline-watchdog.yml` covers both a staging branch that stops building entirely and one that is **still red when the freeze opens**.

A workflow triggered on **both** main and staging from one notify job must not hardcode `true`, or main failures get silenced too. Pass an expression:

```yaml
      freeze-scoped: ${{ github.ref_name == 'staging' }}
```

Freeze state is read from the `staging-freeze` ruleset itself, not inferred from workflow history — a freeze that skipped itself because staging had nothing unreleased still concludes `success`, and history cannot tell that apart from a real freeze. Reading rulesets needs admin, so this reuses the same App that toggles them (`vars.RELEASE_APP_CLIENT_ID` + `secrets.RELEASE_APP_PRIVATE_KEY`); no extra `permissions:` on the caller. If the App token or the ruleset lookup fails, it escalates to the red alert rather than downgrading, so a lookup problem can never silence a real release-blocking failure.

Leave `freeze-scoped` off for prod and freeze/unfreeze callers: a failure there is always worth interrupting for.

**Release-train wrappers no longer add a notify job at all** — `release-freeze.yml`, `release-unfreeze.yml`, `sync-main-to-staging.yml` and `release-pr.yml` each end with one. See "Wrappers carry no notify job" above for why, and for the `actions: read` grant they need.

The alert itself lives in the `notify-pipeline-status` **composite action**, which both shapes of caller run. A pipeline needs a terminal *job* that `needs:` every other job, so it calls this reusable workflow; a release-train workflow owns a single job and simply ends with the composite step. Same copy, same freeze scoping, same recovery detection, one implementation.

The notify job runs on `ubuntu-latest` by default. It posts a Slack message and needs no cluster, no cloud and no self-hosted anything, and an alert that runs on the self-hosted fleet cannot report that the self-hosted fleet is down — the one outage where every pipeline goes red at once is the one where it would have nothing to run on.

For a workflow that also runs on `pull_request`, add `&& github.event_name == 'push'` to the `if:` so PR-run failures (which the author already sees) don't alert the channel.

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

### The second thing it watches: a branch that is STILL red

The same watchdog runs a second sweep, `red-branch-sweep` (on by default), for the case where a pipeline failed and *nobody is being told*. Three ways that happens, all of which have:

| Case | Why the in-run notify job did not cover it |
|---|---|
| Staging broke mid-week | The alert was deliberately silenced, and the branch is still red when Friday's freeze makes it the release candidate |
| It was reported once and stayed broken | One message is easy to miss and there is no second one |
| A single job was re-run | "Re-run failed jobs" re-runs dependents, so notify fires again. The per-job **"Re-run this job"** button does not, so a run can go red to green with notify never running twice |

It is a **backstop, not an echo**: a finding must be at least `min-age-minutes` old (default 30), which turns the message from "this failed" — which the pipeline already said — into "this is still failing and nobody has touched it". On `staging` it only speaks while the freeze is on, and **the window opening is itself a trigger**, so a branch that broke on Tuesday is reported on Friday. That is keyed on a successful run of the `Staging Freeze` workflow rather than on a clock, so moving the freeze moves the alerting with it and neither has to know about the other.

`startup_failure` is reported only by the first sweep and excluded from this one, so one incident never produces two alerts. Selection logic lives in `scripts/branch_health.py` with unit tests, because it is date arithmetic plus a two-way trigger and that does not belong in a jq expression.

Alerts repeat, by design: one failing push produces about three messages at that cadence and window, and then silence. Alerting only on the *transition* into a broken state gives exactly one message per break, which was rejected after replaying it against the auth incident — it would have said nothing about the second failing push, since the run before that one had also failed to start. A stateless sweep cannot be exactly-once, so the choice is a message that can be missed or a few that cannot, and the window is what bounds the few.

Two things to know when adding it. GitHub only runs `schedule` triggers from a repository's **default branch**, so a watchdog merged to `staging` and no further is inert — `workflow_dispatch` is there to prove it before it reaches `main`. And widening `lookback-minutes` on a manual dispatch replays a past incident, which is how to check it would have caught one.

## Workflow lint

`workflow-lint.yml` lints the CALLING repo's workflows. Three layers, only one of which is ours:

| Layer | Blocking | What it is |
| --- | --- | --- |
| `actionlint` | yes | The established syntax + expression + shellcheck linter |
| permission check | yes | `scripts/workflow_graph.py` — the two gaps neither tool covers: one run tree per event, and a callee that declares a permission its caller lacks |
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

## The release-freeze contract

A freeze is one thing: the `enforcement` field of a pre-provisioned repository ruleset, flipped between `active` and `disabled`. Four workflows care — freeze and unfreeze write it, and the two alerting paths read it to decide whether a red staging is worth interrupting anyone for.

They used to carry three separate copies of that knowledge, and only two of them took the ruleset name as an input; the alerting path hardcoded it. Renaming the ruleset in one repo would therefore have moved the freeze and left the alerting reading a name that no longer existed — silent in the direction that hurts, because a reader that cannot establish the state escalates, so every ordinary mid-week staging red would have paged the channel forever and the cause would have looked like a Slack problem.

`scripts/freeze_state.py` now owns it, with two modes that fail in opposite directions on purpose:

| Mode | Used by | On a lookup failure |
|---|---|---|
| `read --on-error escalate` | the alert paths | report frozen, exit 0 — never downgrade a real release-blocking failure, and never redden a green run |
| `read --on-error fail` | `release-pr.yml` | leave the PR a draft — the safe direction there is the opposite one, since "ready" invites a merge of an unvalidated branch |
| `set --enforcement …` | freeze and unfreeze | fail loudly — a freeze that did not apply must stop the release train rather than let the window appear to open |

The flip is a read-modify-write of the whole ruleset: a partial `PUT` is not guaranteed to preserve the fields it omits, and the omitted fields are the bypass actors and branch conditions, so getting it wrong unlocks the branch it was asked to lock. The body goes to a file and is never echoed, because three of the consuming repos are public and a ruleset body names its bypass actors.

Every consumer checks these scripts out at `github.job_workflow_sha` — the commit of the reusable itself, not of `main`. Pinning a workflow while its scripts float is not a pin.

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

Drop these four files into each repo's `.github/workflows/`. Adjust the cron per
repo if desired; branch names default to `staging`/`main`. Each is triggers plus
a `uses:` — **no notify job**, because every reusable posts its own.

> **Pinning:** these call `@main`, matching every other call site in this repo.
> The reusables check their own scripts and composite out at
> `github.job_workflow_sha`, so a caller that *does* pin to a SHA gets that
> SHA's behaviour end to end rather than a pinned workflow running `main`'s
> logic. Note that the previously pinned wrappers had gone two commits stale and
> were still running a `release-unfreeze.yml` that skipped the sync-back on a
> manual dispatch, months after that was fixed here.

`staging-freeze.yml`:

```yaml
name: Staging Freeze
on:
  schedule:
    - cron: '47 13 * * 5'          # Friday 13:47 UTC, off the top of the hour
  workflow_dispatch:

permissions:
  contents: read

jobs:
  freeze:
    permissions:
      contents: read
      actions: read                # turns the "recovered" message on
    uses: mindsdb/github-actions/.github/workflows/release-freeze.yml@main
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

concurrency:                       # shared with sync-main-to-staging.yml
  group: sync-main-to-staging
  cancel-in-progress: false

jobs:
  unfreeze:
    if: >
      github.event_name == 'workflow_dispatch' ||
      (github.event.pull_request.merged == true &&
       github.event.pull_request.head.ref == 'staging' &&
       github.event.pull_request.head.repo.full_name == github.repository)
    permissions:
      contents: read
      actions: read
    uses: mindsdb/github-actions/.github/workflows/release-unfreeze.yml@main
    secrets: inherit
```

`sync-main-to-staging.yml`:

```yaml
name: Sync main to staging

# run-tree-ok: a release-train hook, not a stage of this repo's push:main
# pipeline. Folding it in would make the sync conditional on that pipeline
# succeeding, which is the exact failure it exists to prevent.

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:                       # shared with staging-unfreeze.yml
  group: sync-main-to-staging
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  sync:
    permissions:
      contents: read
      actions: read
    uses: mindsdb/github-actions/.github/workflows/sync-main-to-staging.yml@main
    secrets: inherit
```

`weekly-merge-staging.yml` — note the second `workflow_run` source, which is
this repo's own staging pipeline **by name**, and is what keeps the release PR
current all week:

```yaml
name: Create staging to main release PR
on:
  workflow_run:
    workflows: ["Staging Freeze", "Staging - Build and Deploy on push to staging"]
    types: [completed]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  create-pr:
    # A failed FREEZE means staging was never locked, so the release PR would be
    # premature. A failed staging PIPELINE says nothing about what is queued, so
    # the refresh still runs.
    if: >
      github.event_name == 'workflow_dispatch' ||
      github.event.workflow_run.name != 'Staging Freeze' ||
      github.event.workflow_run.conclusion == 'success'
    permissions:
      contents: read
      actions: read
    uses: mindsdb/github-actions/.github/workflows/release-pr.yml@main
    secrets: inherit
```
